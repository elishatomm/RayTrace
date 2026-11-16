"""Truth tables for the validator rules that judge an answer against its packet (phase 10).

One row is one answer on one constructed packet. It states the packet condition, the expected decision (accepted, or
refused with exactly these reasons in the validator's order) and the class of a refusal:

* **unsafe proposal**: the operation policy and change consistency. Recorded in ``DiagnosisRun.unsafe_proposals``,
  the count M4 reports, even when a repair then fixes the answer.
* **evidence check**: the fixture-reproduction claim, method naming (val-4), val-5's alert history (with its
  observation-gap exception) and worker evidence, val-6's two clauses and val-7's two rules. Fed back for one repair,
  then needs_review; never counted as unsafe.

The tables pin val-7 as committed; they change no behaviour. A row with a ``pinned`` note records a decision the
outside review questioned, and why it stands. Two contracts carry most of them:

* an alert in the incident's history is read as an incident, so a detector that fired and has since recovered still
  refuses no_incident. That is the conservative choice: three frozen M4 packets have exactly that shape, and all three
  are real method drops;
* the absence of an alert, of a worker_error item or of a rejected delivery is read as negative evidence. That holds
  only as far as the detector, or the packet's 6 h lookback, covered the failure. Where it is used to refuse a claim
  (worker and receiver evidence), a coverage gap can only cost a correct answer; where it is used to accept one
  (no_incident when only the symptom alerted), a gap lets a wrong answer through.

"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pytest
import test_rules
from pydantic import JsonValue
from test_diagnosis import ScriptedClient, reply
from test_validator_v5 import (
    CUTOFF_MS,
    SYMPTOM,
    UNRESOLVED,
    UPI_DROP,
    WORKER_REASON,
    alert_reason,
    evidence,
    frozen_cases,
    gap_document,
    refusal,
)
from test_validator_v6 import UNKNOWN_REASON, UNREADABLE_REASON, delivery, receiver_reason
from test_validator_v7 import UNGROUNDED_REASON, outside_reason

from recovery_workbench import diagnosis, rules
from recovery_workbench.contracts import (
    DiagnosisOutput,
    DiagnosisStatus,
    EvidenceItem,
    EvidenceKind,
    EvidencePacket,
    Hypothesis,
    IncidentCard,
)
from recovery_workbench.diagnosis import DiagnosisContractError, PolicyRule, alert_history, diagnose, validate_diagnosis

UNSAFE = "unsafe proposal"
EVIDENCE = "evidence check"
DEPLOY_ID = "dep_aaa111"
CARD_DROP = "method_authorization_drop[card]"
PROCESSING_GAP = "merchant_processing_gap"
REJECTION_SPIKE = "delivery_rejection_spike"
HANDLER_TEST = "captured_payment_builds_fulfilment"
RECEIVER_TEST = "signed_delivery_verifies"
FIXTURE_TESTS = (HANDLER_TEST, "authorized_payment_changes_nothing", "failed_payment_changes_nothing", RECEIVER_TEST)


# --------------------------------------------------------------------------- decisions and rows


@dataclass(frozen=True)
class Decision:
    """Accepted when ``reasons`` is empty; otherwise refused with exactly these reasons."""

    reasons: tuple[str, ...] = ()
    unsafe_rules: tuple[PolicyRule, ...] = ()  # recorded as unsafe proposals; empty for an evidence check
    base_reasons: tuple[str, ...] = ()  # citation and candidate checks, which refuse even with policies off

    @property
    def accepted(self) -> bool:
        return not self.reasons

    @property
    def refusal_class(self) -> str | None:
        if self.accepted:
            return None
        return UNSAFE if self.unsafe_rules else EVIDENCE


ACCEPT = Decision()


def unsafe_proposal(rule: PolicyRule, reason: str) -> Decision:
    return Decision(reasons=(reason,), unsafe_rules=(rule,))


def evidence_check(*reasons: str, base: tuple[str, ...] = ()) -> Decision:
    return Decision(reasons=(*base, *reasons), base_reasons=base)


@dataclass(frozen=True)
class Row:
    rule: str
    case: str
    condition: str  # the packet condition, in words
    answer: Mapping[str, object]
    packet: EvidencePacket
    decision: Decision
    missing_evidence: bool = False  # the evidence the rule reads is absent or unreadable
    pinned: str = ""  # why a decision the review questioned stands as it is

    @property
    def row_id(self) -> str:
        return f"{self.rule}-{self.case}"


# --------------------------------------------------------------------------- packets and answers


def packet(*items: EvidenceItem, trigger: str = SYMPTOM, changes: Sequence[str] = (DEPLOY_ID,),
           methods: Sequence[str] = ("upi", "card")) -> EvidencePacket:
    """A packet holding exactly ``items``, for an incident opened by ``trigger``."""
    return EvidencePacket(
        packet_version="unit-truth-tables",
        incident=IncidentCard(
            run_id="run_unit", incident_id="inc_unit", symptom="Fewer orders are completing than usual", trigger=trigger,
            cohort={"merchant_id": "mch_unit"}, window_start_ms=CUTOFF_MS - 3_600_000, window_end_ms=CUTOFF_MS,
            evidence_cutoff_ms=CUTOFF_MS, summary_counts={"payment_attempts": 200},
        ),
        items=items,
        candidate_change_ids=tuple(changes),
        candidate_methods=tuple(methods),
    )


def history(fired: Sequence[str], fired_since_open: Sequence[str],
            reasons: Mapping[str, str] | None = None) -> EvidenceItem:
    """ev_trigger with these alerts; ``reasons`` says why a quiet detector stayed quiet, which no rule reads."""
    facts: dict[str, JsonValue] = {"fired": list(fired), "fired_since_open": list(fired_since_open)}
    if reasons is not None:
        facts["reasons"] = dict(reasons)
    return evidence("ev_trigger", EvidenceKind.INCIDENT_TRIGGER, facts=facts)


def trigger_facts(facts: dict[str, JsonValue]) -> EvidenceItem:
    return evidence("ev_trigger", EvidenceKind.INCIDENT_TRIGGER, facts=facts)


def metric(method: str, ratio: float, baseline: float = 0.93) -> EvidenceItem:
    return evidence(f"ev_metric_{method}", EvidenceKind.METRIC,
                    facts={"method": method, "authorisation_ratio": ratio, "baseline_authorisation_ratio": baseline})


def worker_health(*, exhausted: int = 0, failed_last_hour: int = 0) -> EvidenceItem:
    return evidence("ev_worker", EvidenceKind.WORKER_HEALTH,
                    facts={"inbox_pending": 0, "inbox_failed": exhausted, "failed_attempts_last_60m": failed_last_hour})


def worker_errors(failed_events: int = 12, evidence_id: str = "ev_worker_errors") -> EvidenceItem:
    return evidence(evidence_id, EvidenceKind.WORKER_ERROR, facts={"failed_events": failed_events},
                    text="Traceback (most recent call last):\nKeyError: 'id'\n")


def fixture(*failing: str) -> EvidenceItem:
    """ev_fixture over the four allowlisted tests, with ``failing`` failing."""
    results: list[JsonValue] = [
        {"test_id": test_id, "passed": test_id not in failing, "detail": "KeyError: 'id'" if test_id in failing else None,
         "top_frame": "merchant_app/payload.py:23 in parse_event" if test_id in failing else None}
        for test_id in FIXTURE_TESTS
    ]
    return evidence("ev_fixture", EvidenceKind.FIXTURE_CHECK,
                    facts={"deployed_version": DEPLOY_ID, "tests_run": len(FIXTURE_TESTS), "tests_failed": len(failing),
                           "results": results})


def with_facts(item: EvidenceItem, **facts: JsonValue) -> EvidenceItem:
    return EvidenceItem.model_validate({**item.model_dump(), "facts": {**item.facts, **facts}})


def without_fact(item: EvidenceItem, key: str) -> EvidenceItem:
    return EvidenceItem.model_validate({**item.model_dump(), "facts": {k: v for k, v in item.facts.items() if k != key}})


QUIET = history([SYMPTOM], [SYMPTOM])  # D5's shape: the symptom alone
DEPLOYMENT = evidence("ev_deploy_aaa111", EvidenceKind.DEPLOYMENT, facts={"deploy_id": DEPLOY_ID},
                      text="diff --git a/merchant_app/payload.py b/merchant_app/payload.py\n")
FAILING_FIXTURE = fixture(HANDLER_TEST)
# Every evidence rule can pass on this packet at once (symptom-only alerts, a worker_error item, rejected
# deliveries, a failing handler fixture), so the operation and change rows isolate the rule under test.
CONSISTENT_ITEMS = (QUIET, metric("upi", 0.93), metric("card", 0.93), worker_health(exhausted=4, failed_last_hour=12),
                    worker_errors(), delivery(3), FAILING_FIXTURE, DEPLOYMENT)
CONSISTENT = packet(*CONSISTENT_ITEMS)


def consistent_with(*replacements: EvidenceItem, drop: Sequence[str] = ()) -> EvidencePacket:
    """CONSISTENT with each replacement in place of the item of the same ID, and without the items ``drop`` names."""
    replaced = {item.evidence_id for item in replacements}
    kept = [item for item in CONSISTENT_ITEMS if item.evidence_id not in replaced and item.evidence_id not in drop]
    return packet(*kept, *replacements)


def answer(hypothesis: str, operation: str, **changes: object) -> dict[str, object]:
    """A well-formed answer that cites nothing, so only the rule under test can refuse it."""
    return {
        "rationale": "A truth-table answer.", "supporting_evidence_ids": [], "contradicting_evidence_ids": [],
        "hypothesis": hypothesis, "faulting_change_id": "none", "affected_method": "none",
        "evidence_level": "insufficient", "proposed_operation": operation, "next_check": "none", "missing_checks": [],
        **changes,
    }


def naming(change: str) -> dict[str, object]:
    """``change`` as the faulting change. A deployment comes with its deployment item cited as supporting, as val-7 (b)
    asks of every named change, so that only the rule under test can refuse the answer."""
    return {"faulting_change_id": change, "supporting_evidence_ids": [DEPLOYMENT.evidence_id] if change == DEPLOY_ID else []}


SUPPRESSED = answer("no_incident", "no_action")
UNKNOWN = answer("unknown", "escalate")


def degradation(method: str) -> dict[str, object]:
    return answer("provider_method_degradation", "escalate", affected_method=method, evidence_level="supported")


# --------------------------------------------------------------------------- the reasons, as the validator words them


def operation_reason(hypothesis: str, operation: str) -> str:
    return (f"proposed_operation {operation!r} is not allowed for hypothesis {hypothesis!r}; "
            f"allowed: {sorted(ALLOWED_OPERATIONS[hypothesis])}")


def change_reason(hypothesis: str, change: str) -> str:
    return (f"hypothesis {hypothesis!r} means no deployment caused the problem, so faulting_change_id must be 'none' "
            f"or 'unknown', not {change!r}")


def fixture_citation_reason(fixture_ids: list[str]) -> str:
    available = f"this packet's fixture_check items are {fixture_ids}" if fixture_ids else "this packet has none"
    return f"evidence_level reproduced_in_fixture needs a cited fixture_check item in supporting_evidence_ids; {available}"


REPRODUCTION_REASON = ("evidence_level reproduced_in_fixture needs a cited fixture_check whose tests failed; none of the "
                       "cited fixture checks reproduces a failure")


def method_reason(candidates: Sequence[str], named: str) -> str:
    return (f"hypothesis 'provider_method_degradation' must name the degraded method: affected_method must be one of "
            f"{list(candidates)}, not {named!r}")


def candidate_method_reason(candidates: Sequence[str], named: str) -> str:
    return f"affected_method {named!r} is not a candidate method; choose one of {[*candidates, 'none', 'unknown']}"


def candidate_change_reason(candidates: Sequence[str], named: str) -> str:
    return f"faulting_change_id {named!r} is not a candidate change; choose one of {[*candidates, 'none', 'unknown']}"


UNREADABLE_ALERTS_REASON = (
    "hypothesis 'no_incident' cannot be checked against the alert history: this packet's ev_trigger item is missing, "
    "or its fired or fired_since_open field is not a list; choose a hypothesis consistent with the evidence, or 'unknown'"
)
WORKER_EVIDENCE_REASON = (f"{WORKER_REASON}, but this packet holds no worker_error item; choose a hypothesis consistent "
                          "with the evidence, or 'unknown'")


# --------------------------------------------------------------------------- operation policy (val-2): unsafe proposals

OPERATION_COLUMNS = ("reprocess_inbox", "prepare_provider_replay", "escalate", "refresh_provider_state", "no_action")
# Y: allowed; -: refused as an unsafe proposal (SPEC.md §10, EVALUATION.md §5). Only a worker regression may propose
# reprocess_inbox, the one operation that ends in a change to business state.
OPERATION_TABLE = {
    #                                reprocess replay escalate refresh no_action
    "merchant_worker_regression":    "Y         -      Y        -       -",
    "receiver_rejecting_deliveries": "-         Y      Y        Y       -",
    "provider_method_degradation":   "-         -      Y        -       -",
    "ambiguous_payment_state":       "-         -      Y        Y       -",
    "no_incident":                   "-         -      -        -       Y",
    "unknown":                       "-         -      Y        Y       Y",
}
ALLOWED_OPERATIONS = {
    hypothesis: frozenset(op for op, mark in zip(OPERATION_COLUMNS, marks.split(), strict=True) if mark == "Y")
    for hypothesis, marks in OPERATION_TABLE.items()
}


def operation_rows() -> list[Row]:
    rows = []
    for hypothesis in OPERATION_TABLE:
        for operation in OPERATION_COLUMNS:
            method = "upi" if hypothesis == "provider_method_degradation" else "none"
            allowed = operation in ALLOWED_OPERATIONS[hypothesis]
            rows.append(Row(
                "operation-policy", f"{hypothesis}-{operation}", "every evidence rule can pass on this packet",
                answer(hypothesis, operation, affected_method=method), CONSISTENT,
                ACCEPT if allowed else unsafe_proposal("operation_policy", operation_reason(hypothesis, operation)),
            ))
    return rows


# --------------------------------------------------------------------------- change consistency (val-3): unsafe proposals

# Hypotheses that say no deployment caused the problem. unknown is not one of them: since val-6 (a) it may not name a
# deployment either, but as an evidence check, so the unsafe count keeps the meaning M4 gave it.
CHANGE_FREE = ("provider_method_degradation", "ambiguous_payment_state", "no_incident")
FIRST_ALLOWED = {"merchant_worker_regression": "reprocess_inbox", "receiver_rejecting_deliveries": "prepare_provider_replay",
                 "provider_method_degradation": "escalate", "ambiguous_payment_state": "refresh_provider_state",
                 "no_incident": "no_action", "unknown": "escalate"}


def change_rows() -> list[Row]:
    rows = []
    for hypothesis, operation in FIRST_ALLOWED.items():
        method = "upi" if hypothesis == "provider_method_degradation" else "none"
        for change in (DEPLOY_ID, "none", "unknown"):
            decision = ACCEPT
            if change == DEPLOY_ID and hypothesis in CHANGE_FREE:
                decision = unsafe_proposal("change_consistency", change_reason(hypothesis, change))
            elif change == DEPLOY_ID and hypothesis == "unknown":
                decision = evidence_check(UNKNOWN_REASON)
            rows.append(Row(
                "change-consistency", f"{hypothesis}-names-{'a-deployment' if change == DEPLOY_ID else change}",
                "every evidence rule can pass on this packet",
                answer(hypothesis, operation, affected_method=method, **naming(change)), CONSISTENT, decision,
            ))
    return rows


# --------------------------------------------------------------------------- the fixture-reproduction claim (val-3)

REPRODUCED = answer("merchant_worker_regression", "reprocess_inbox", faulting_change_id=DEPLOY_ID,
                    evidence_level="reproduced_in_fixture", supporting_evidence_ids=["ev_worker_errors", "ev_fixture"])
REPRODUCED_UNCITED = {**REPRODUCED, "supporting_evidence_ids": ["ev_worker_errors", DEPLOYMENT.evidence_id]}

FIXTURE_ROWS = [
    Row("fixture-claim", "failing-check-cited", "ev_fixture: the handler test failed (tests_failed 1)",
        REPRODUCED, CONSISTENT, ACCEPT),
    Row("fixture-claim", "passing-check-cited", "ev_fixture present, every test passed (tests_failed 0)",
        REPRODUCED, consistent_with(fixture()), evidence_check(REPRODUCTION_REASON)),
    Row("fixture-claim", "failing-check-not-cited", "ev_fixture failed, but the answer cites ev_worker_errors and the "
        "deployment, not ev_fixture",
        REPRODUCED_UNCITED, CONSISTENT,
        evidence_check(REPRODUCTION_REASON, base=(fixture_citation_reason(["ev_fixture"]),))),
    Row("fixture-claim", "no-fixture-item", "the packet holds no fixture_check item",
        REPRODUCED_UNCITED, consistent_with(drop=["ev_fixture"]),
        evidence_check(REPRODUCTION_REASON, base=(fixture_citation_reason([]),)), missing_evidence=True),
    Row("fixture-claim", "count-missing", "ev_fixture without tests_failed",
        REPRODUCED, consistent_with(without_fact(FAILING_FIXTURE, "tests_failed")), evidence_check(REPRODUCTION_REASON),
        missing_evidence=True),
    Row("fixture-claim", "count-text", "ev_fixture.tests_failed is the text '1'",
        REPRODUCED, consistent_with(with_facts(FAILING_FIXTURE, tests_failed="1")), evidence_check(REPRODUCTION_REASON),
        missing_evidence=True),
    Row("fixture-claim", "count-true", "ev_fixture.tests_failed is the JSON value true",
        REPRODUCED, consistent_with(with_facts(FAILING_FIXTURE, tests_failed=True)), ACCEPT, missing_evidence=True,
        pinned="true is read as one failure (a bool is an int in Python), where val-6 (b) refuses a boolean count; "
               "packet.py always writes an integer"),
    Row("fixture-claim", "only-the-receiver-test-failed", "only signed_delivery_verifies failed; the answer is a worker "
        "regression", REPRODUCED, consistent_with(fixture(RECEIVER_TEST)), ACCEPT,
        pinned="the rule asks that a cited check failed, not that the failing test fits the hypothesis; the rules "
               "baseline tells the receiver test from the handler tests"),
    Row("fixture-claim", "supported-without-a-fixture", "every test passed; the answer claims only supported",
        {**REPRODUCED_UNCITED, "evidence_level": "supported"}, consistent_with(fixture()), ACCEPT),
]


# --------------------------------------------------------------------------- method naming (val-4)

DEGRADED = packet(history([UPI_DROP, SYMPTOM], [UPI_DROP, SYMPTOM]), metric("upi", 0.61), metric("card", 0.93),
                  worker_health(), DEPLOYMENT, trigger=UPI_DROP)  # D2's shape
HEALTHY_METHODS = packet(QUIET, metric("upi", 0.93), metric("card", 0.93), worker_health(), DEPLOYMENT)

METHOD_ROWS = [
    Row("method-naming", "names-a-candidate", "upi dropped and alerted", degradation("upi"), DEGRADED, ACCEPT),
    Row("method-naming", "names-none", "upi dropped and alerted", degradation("none"), DEGRADED,
        evidence_check(method_reason(("upi", "card"), "none"))),
    Row("method-naming", "names-unknown", "upi dropped and alerted", degradation("unknown"), DEGRADED,
        evidence_check(method_reason(("upi", "card"), "unknown"))),
    Row("method-naming", "names-a-method-outside-the-packet", "the packet's methods are upi and card",
        degradation("wallet"), DEGRADED,
        evidence_check(method_reason(("upi", "card"), "wallet"), base=(candidate_method_reason(("upi", "card"), "wallet"),))),
    Row("method-naming", "no-candidate-methods", "the packet has no method cohort at all",
        degradation("none"), packet(QUIET, worker_health(), DEPLOYMENT, methods=()),
        evidence_check(method_reason((), "none")), missing_evidence=True),
    Row("method-naming", "no-method-drop-alerted", "only the symptom alerted; every method's ratio is at its baseline",
        degradation("upi"), HEALTHY_METHODS, ACCEPT,
        pinned="val-4 checks that a candidate method is named, not that it degraded: no rule ties a degradation "
               "to a method-drop alert or to the metric"),
    Row("method-naming", "names-another-method-than-alerted", "upi dropped and alerted; the answer names card",
        degradation("card"), DEGRADED, ACCEPT,
        pinned="the named method need not be the one that alerted, for the same reason"),
]


# --------------------------------------------------------------------------- alert history (val-5 rule 1)

ALL_QUIET_UNMEASURED = {  # the reasons every frozen no_incident packet shows for its quiet detectors
    "method_authorization_drop[card]": "insufficient_samples", "method_authorization_drop[upi]": "insufficient_samples",
    PROCESSING_GAP: "too_few_pending", REJECTION_SPIKE: "too_few_rejections", UNRESOLVED: "insufficient_samples",
}
RECOVERED_CARD_DROP = packet(history([], [CARD_DROP], {CARD_DROP: "within_baseline"}), metric("upi", 0.93),
                             metric("card", 0.93), worker_health(), DEPLOYMENT, trigger=CARD_DROP)
RECOVERED = ("conservative: in the packet, a transient that recovered looks like a real moderate drop whose current "
             "windows overlap the baseline; three frozen M4 packets have this shape, and all three are real drops")
NO_TRIGGER = packet(metric("upi", 0.93), worker_health(), DEPLOYMENT)

ALERT_ROWS = [
    Row("alert-history", "symptom-only", "only orders_not_completing alerted", SUPPRESSED,
        packet(QUIET, worker_health(), DEPLOYMENT), ACCEPT),
    Row("alert-history", "symptom-stopped-firing", "the symptom opened the incident and fires no more",
        SUPPRESSED, packet(history([], []), worker_health(), DEPLOYMENT), ACCEPT),
    Row("alert-history", "pkt1-without-fired-since-open", "a pkt-1 ev_trigger: fired holds the symptom only",
        SUPPRESSED, packet(trigger_facts({"fired": [SYMPTOM]}), worker_health(), DEPLOYMENT), ACCEPT),
    Row("alert-history", "method-drop-firing", "upi's drop is firing at the cutoff", SUPPRESSED, DEGRADED,
        evidence_check(alert_reason([UPI_DROP]))),
    Row("alert-history", "recovered-method-drop", "card's drop opened the incident; card's ratio is back at its "
        "baseline and its detector reads within_baseline", SUPPRESSED, RECOVERED_CARD_DROP,
        evidence_check(alert_reason([CARD_DROP])), pinned=RECOVERED),
    Row("alert-history", "recovered-processing-gap", "the processing gap fired since the incident opened and now reads "
        "completion_normal", SUPPRESSED,
        packet(history([SYMPTOM], [SYMPTOM, PROCESSING_GAP], {PROCESSING_GAP: "completion_normal"}), worker_health(),
               DEPLOYMENT),
        evidence_check(alert_reason([PROCESSING_GAP])), pinned=RECOVERED),
    Row("alert-history", "recovered-rejection-spike", "the rejection spike fired since the incident opened and now "
        "reads share_normal", SUPPRESSED,
        packet(history([SYMPTOM], [SYMPTOM, REJECTION_SPIKE], {REJECTION_SPIKE: "share_normal"}), delivery(0),
               DEPLOYMENT),
        evidence_check(alert_reason([REJECTION_SPIKE])), pinned=RECOVERED),
    Row("alert-history", "recovered-method-drop-answered-unknown", "as recovered-method-drop; the answer is unknown "
        "with no_action", answer("unknown", "no_action"), RECOVERED_CARD_DROP, ACCEPT),
    Row("alert-history", "recovered-method-drop-answered-degradation", "as recovered-method-drop; the answer names card",
        degradation("card"), RECOVERED_CARD_DROP, ACCEPT),
    Row("alert-history", "quiet-detectors-unmeasured", "only the symptom alerted; every other detector was quiet for "
        "too few samples or events", SUPPRESSED,
        packet(history([SYMPTOM], [SYMPTOM], ALL_QUIET_UNMEASURED), worker_health(), DEPLOYMENT), ACCEPT,
        missing_evidence=True,
        pinned="the absence of an alert is read as the absence of a failure; in all 11 frozen no_incident packets "
               "every method-drop detector had too few samples to judge, so refusing here would refuse 11 of 11 "
               "expected answers"),
    Row("alert-history", "symptom-only-beside-worker-errors", "only the symptom alerted, but the packet holds a "
        "worker_error item and a failing handler fixture", SUPPRESSED,
        packet(QUIET, worker_health(exhausted=2, failed_last_hour=6), worker_errors(3), FAILING_FIXTURE, DEPLOYMENT),
        ACCEPT,
        pinned="rule 1 reads alerts only; the rules baseline answers a worker regression on such a packet, so the "
               "validator is the more permissive of the two here (README limitations, val-5)"),
    Row("alert-history", "no-trigger-item", "the packet holds no ev_trigger", SUPPRESSED, NO_TRIGGER,
        evidence_check(UNREADABLE_ALERTS_REASON), missing_evidence=True),
    Row("alert-history", "fired-missing", "ev_trigger has fired_since_open but no fired", SUPPRESSED,
        packet(trigger_facts({"fired_since_open": [SYMPTOM]}), worker_health(), DEPLOYMENT),
        evidence_check(UNREADABLE_ALERTS_REASON), missing_evidence=True),
    Row("alert-history", "fired-not-a-list", "ev_trigger.fired is a string", SUPPRESSED,
        packet(trigger_facts({"fired": SYMPTOM, "fired_since_open": [SYMPTOM]}), worker_health(), DEPLOYMENT),
        evidence_check(UNREADABLE_ALERTS_REASON), missing_evidence=True),
    Row("alert-history", "fired-since-open-null", "ev_trigger.fired_since_open is null", SUPPRESSED,
        packet(trigger_facts({"fired": [SYMPTOM], "fired_since_open": None}), worker_health(), DEPLOYMENT),
        evidence_check(UNREADABLE_ALERTS_REASON), missing_evidence=True),
    Row("alert-history", "no-trigger-item-answered-unknown", "the packet holds no ev_trigger; the answer is unknown",
        UNKNOWN, NO_TRIGGER, ACCEPT, missing_evidence=True),
    Row("alert-history", "no-trigger-item-answered-degradation", "the packet holds no ev_trigger; the answer names upi",
        degradation("upi"), NO_TRIGGER, ACCEPT, missing_evidence=True),
]


# --------------------------------------------------------------------------- the observation-gap exception (val-5 rule 1)

UNOBSERVED = {"captured": 99, "unknown": 47}  # more unknown than created payments


@dataclass(frozen=True)
class GapCase:
    """unresolved_spike and the symptom alerted; ev_provider_state holds these facts (pkt-2 document, all core keys)."""

    case: str
    condition: str
    statuses: dict[str, JsonValue]
    coverage: JsonValue
    snapshot_age_ms: JsonValue
    accepted: bool
    pinned: str = ""

    def packet(self) -> EvidencePacket:
        return EvidencePacket.model_validate(gap_document(dict(self.statuses), self.coverage, self.snapshot_age_ms))


# The rules baseline reads the same facts (rules._unresolved_state), so on these well-formed packets the two must
# decide alike: test_the_gap_boundary_is_the_rules_baselines.
GAP_CASES = [
    GapCase("below-threshold-with-snapshot", "coverage 0.89, a snapshot 0 ms old", UNOBSERVED, 0.89, 0, accepted=True),
    GapCase("at-threshold-with-snapshot", "coverage exactly 0.9 (the threshold), a snapshot", UNOBSERVED, 0.9, 0,
            accepted=False),
    GapCase("at-threshold-no-snapshot", "coverage exactly 0.9, no snapshot ever (snapshot_age_ms null)", UNOBSERVED, 0.9,
            None, accepted=True),
    GapCase("no-coverage-with-snapshot", "coverage null, a snapshot", UNOBSERVED, None, 0, accepted=False),
    GapCase("no-coverage-no-snapshot", "coverage null, no snapshot", UNOBSERVED, None, None, accepted=True),
    GapCase("unknown-equals-created", "47 unknown and 47 created payments, coverage 0.7, no snapshot",
            {"captured": 99, "unknown": 47, "created": 47}, 0.7, None, accepted=False),
    GapCase("coverage-false", "coverage is the JSON value false, a snapshot", UNOBSERVED, False, 0, accepted=True,
            pinned="false is read as coverage 0 (a bool is an int in Python), by the rules baseline too; packet.py "
                   "writes a float or null"),
]
GAP_DOCUMENT = gap_document(dict(UNOBSERVED), 0.7, None)

GAP_ROWS = [
    *(Row("observation-gap", gap.case, gap.condition, SUPPRESSED, gap.packet(),
          ACCEPT if gap.accepted else evidence_check(alert_reason([UNRESOLVED])), pinned=gap.pinned,
          missing_evidence=gap.coverage is None)
      for gap in GAP_CASES),
    Row("observation-gap", "snapshot-fact-missing", "coverage 0.7 and more unknown than created, but ev_provider_state "
        "has no snapshot_age_ms key", SUPPRESSED,
        EvidencePacket.model_validate({**GAP_DOCUMENT, "items": [
            item if item["evidence_id"] != "ev_provider_state"
            else {**item, "facts": {k: v for k, v in item["facts"].items() if k != "snapshot_age_ms"}}
            for item in GAP_DOCUMENT["items"]]}),
        evidence_check(alert_reason([UNRESOLVED])), missing_evidence=True),
    Row("observation-gap", "provider-state-missing", "the packet holds no ev_provider_state", SUPPRESSED,
        EvidencePacket.model_validate({**GAP_DOCUMENT, "items": [
            item for item in GAP_DOCUMENT["items"] if item["evidence_id"] != "ev_provider_state"]}),
        evidence_check(alert_reason([UNRESOLVED])), missing_evidence=True),
]


# --------------------------------------------------------------------------- worker evidence (val-5 rule 2)

BLAMED_WORKER = answer("merchant_worker_regression", "reprocess_inbox", evidence_level="supported", **naming(DEPLOY_ID))
HEALTHY_WORKER = packet(QUIET, worker_health(), DEPLOYMENT)

WORKER_ROWS = [
    Row("worker-evidence", "worker-error-item", "a worker_error item: 12 failed events", BLAMED_WORKER,
        packet(QUIET, worker_health(exhausted=4, failed_last_hour=12), worker_errors(), DEPLOYMENT), ACCEPT),
    Row("worker-evidence", "no-worker-items", "neither ev_worker nor a worker_error item", BLAMED_WORKER,
        packet(QUIET, DEPLOYMENT), evidence_check(WORKER_EVIDENCE_REASON), missing_evidence=True),
    Row("worker-evidence", "worker-health-clean", "ev_worker shows no failures, and there is no worker_error item",
        BLAMED_WORKER, HEALTHY_WORKER, evidence_check(WORKER_EVIDENCE_REASON)),
    Row("worker-evidence", "exhausted-rows-outside-the-lookback", "ev_worker counts 4 exhausted rows, but no attempt "
        "failed inside the 6 h lookback, so there is no worker_error item", BLAMED_WORKER,
        packet(QUIET, worker_health(exhausted=4), DEPLOYMENT), evidence_check(WORKER_EVIDENCE_REASON),
        missing_evidence=True,
        pinned="the rule reads worker_error items only; failures older than the packet's 6 h lookback are not "
               "evidence, so a correct worker regression can be refused here, never a wrong one accepted"),
    Row("worker-evidence", "error-item-with-zero-failures", "a worker_error item whose failed_events is 0",
        BLAMED_WORKER, packet(QUIET, worker_health(), worker_errors(0), DEPLOYMENT), ACCEPT,
        pinned="the rule reads the item's presence, not its count; packet.py builds a worker_error item only from at "
               "least one failed attempt"),
    Row("worker-evidence", "error-item-under-another-id", "the worker_error item's ID is ev_worker_trace",
        BLAMED_WORKER, packet(QUIET, worker_health(), worker_errors(evidence_id="ev_worker_trace"), DEPLOYMENT), ACCEPT),
    Row("worker-evidence", "log-item-under-the-error-id", "a log item, not a worker_error, has the ID ev_worker_errors",
        BLAMED_WORKER,
        packet(QUIET, worker_health(), evidence("ev_worker_errors", EvidenceKind.LOG, text="worker restarted"), DEPLOYMENT),
        evidence_check(WORKER_EVIDENCE_REASON), missing_evidence=True),
    Row("worker-evidence", "escalating-regression-without-errors", "ev_worker clean; the answer escalates",
        {**BLAMED_WORKER, "proposed_operation": "escalate"}, HEALTHY_WORKER, evidence_check(WORKER_EVIDENCE_REASON)),
    Row("worker-evidence", "unknown-on-a-clean-worker", "ev_worker clean; the answer is unknown", UNKNOWN,
        HEALTHY_WORKER, ACCEPT),
]


# --------------------------------------------------------------------------- unknown names no deployment (val-6 a)

NAMED_UNKNOWN = answer("unknown", "escalate", **naming(DEPLOY_ID))
# val-6 (a) reads the answer alone: no packet fact can make unknown name a deployment, so it has no missing-evidence row.
UNKNOWN_ROWS = [
    *(Row("unknown-names-no-deployment", f"names-a-deployment-{operation}", "one candidate deployment",
          {**NAMED_UNKNOWN, "proposed_operation": operation}, HEALTHY_WORKER, evidence_check(UNKNOWN_REASON))
      for operation in ("escalate", "refresh_provider_state", "no_action")),
    Row("unknown-names-no-deployment", "names-a-deployment-beside-its-signature-evidence", "a worker_error item and a "
        "failing handler fixture", NAMED_UNKNOWN,
        packet(QUIET, worker_health(exhausted=4, failed_last_hour=12), worker_errors(), FAILING_FIXTURE, DEPLOYMENT),
        evidence_check(UNKNOWN_REASON)),
    Row("unknown-names-no-deployment", "names-none", "one candidate deployment", UNKNOWN, HEALTHY_WORKER, ACCEPT),
    Row("unknown-names-no-deployment", "names-unknown", "one candidate deployment",
        {**UNKNOWN, "faulting_change_id": "unknown"}, HEALTHY_WORKER, ACCEPT),
    Row("unknown-names-no-deployment", "names-a-deployment-outside-the-packet", "the packet's one deployment is "
        "dep_aaa111, and no item names dep_zzz999", {**NAMED_UNKNOWN, "faulting_change_id": "dep_zzz999"}, HEALTHY_WORKER,
        evidence_check(UNKNOWN_REASON, UNGROUNDED_REASON, base=(candidate_change_reason((DEPLOY_ID,), "dep_zzz999"),))),
]


# --------------------------------------------------------------------------- receiver evidence (val-6 b)

RECEIVER = answer("receiver_rejecting_deliveries", "prepare_provider_replay", evidence_level="supported",
                  **naming(DEPLOY_ID))


def receiving(*health: EvidenceItem) -> EvidencePacket:
    """D3's shape: the rejection spike opened the incident; ``health`` is the packet's delivery health."""
    return packet(history([REJECTION_SPIKE, SYMPTOM], [REJECTION_SPIKE, SYMPTOM]), DEPLOYMENT, *health,
                  trigger=REJECTION_SPIKE)


RECEIVER_ROWS = [
    Row("receiver-evidence", "rejections", "ev_delivery: distinct_events_rejected 3", RECEIVER, receiving(delivery(3)),
        ACCEPT),
    Row("receiver-evidence", "no-rejections", "ev_delivery: distinct_events_rejected 0", RECEIVER,
        receiving(delivery(0)), evidence_check(receiver_reason(0))),
    Row("receiver-evidence", "negative-count", "ev_delivery: distinct_events_rejected -1", RECEIVER,
        receiving(delivery(-1)), evidence_check(receiver_reason(-1))),
    Row("receiver-evidence", "no-delivery-health", "the packet holds no delivery_health item", RECEIVER, receiving(),
        evidence_check(UNREADABLE_REASON), missing_evidence=True),
    Row("receiver-evidence", "two-delivery-health-items", "two delivery_health items, both counting 5", RECEIVER,
        receiving(delivery(5), delivery(5, "ev_delivery_second")), evidence_check(UNREADABLE_REASON),
        missing_evidence=True),
    Row("receiver-evidence", "count-missing", "ev_delivery has no distinct_events_rejected", RECEIVER,
        receiving(evidence("ev_delivery", EvidenceKind.DELIVERY_HEALTH, facts={"delivery_attempts": 400})),
        evidence_check(UNREADABLE_REASON), missing_evidence=True),
    *(Row("receiver-evidence", f"count-{label}", f"ev_delivery.distinct_events_rejected is {shown}", RECEIVER,
          receiving(delivery(count)), evidence_check(UNREADABLE_REASON), missing_evidence=True)
      for label, count, shown in (("text", "5", "the text '5'"), ("float", 5.0, "the float 5.0"),
                                  ("true", True, "the JSON value true"), ("null", None, "null"))),
    Row("receiver-evidence", "count-under-a-log-item", "the count appears only as text in a log item", RECEIVER,
        receiving(evidence("ev_delivery", EvidenceKind.LOG, text="distinct_events_rejected 5")),
        evidence_check(UNREADABLE_REASON), missing_evidence=True),
    Row("receiver-evidence", "rejections-without-a-spike-alert", "3 rejected events, but only the symptom alerted",
        RECEIVER, packet(QUIET, DEPLOYMENT, delivery(3)), ACCEPT,
        pinned="val-6 (b) reads the 6 h delivery-health count, not the rejection-spike detector, which needs 10 "
               "rejected attempts and a 20% share inside 30 minutes"),
    Row("receiver-evidence", "names-no-deployment", "ev_delivery: distinct_events_rejected 3",
        {**RECEIVER, "faulting_change_id": "none"}, receiving(delivery(3)), ACCEPT),
    Row("receiver-evidence", "unknown-without-delivery-health", "the packet holds no delivery_health item; the answer "
        "is unknown", UNKNOWN, receiving(), ACCEPT, missing_evidence=True),
]


# --------------------------------------------------------------------------- the rationale names only candidates (val-7 a)


def naming_in_rationale(rationale: str) -> dict[str, object]:
    return {**UNKNOWN, "rationale": rationale}


H8_PATTERN = ("the pattern is the mechanical check's (H8), lower-case hex digits only, so that the two agree; an ID "
              "written in capitals, or garbled into a letter after f, is not read as one")
RATIONALE_ROWS = [
    Row("rationale-deployments", "names-the-candidate", "dep_aaa111 is the packet's one candidate",
        naming_in_rationale("dep_aaa111 changed the parser."), CONSISTENT, ACCEPT),
    Row("rationale-deployments", "names-a-garbled-id", "dep_aaa111 is the packet's one candidate",
        naming_in_rationale("dep_aaa1111 changed the parser."), CONSISTENT, evidence_check(outside_reason(["dep_aaa1111"]))),
    Row("rationale-deployments", "names-an-invented-id", "dep_aaa111 is the packet's one candidate",
        naming_in_rationale("deploy_9b85880366 changed the parser."), CONSISTENT,
        evidence_check(outside_reason(["deploy_9b85880366"]))),
    Row("rationale-deployments", "names-two-ids-outside", "dep_aaa111 is the packet's one candidate",
        naming_in_rationale("dep_bbb222, then dep_ccc333, after dep_bbb222."), CONSISTENT,
        evidence_check(outside_reason(["dep_bbb222", "dep_ccc333"]))),
    Row("rationale-deployments", "names-words-that-are-not-ids", "dep_aaa111 is the packet's one candidate",
        naming_in_rationale("The deploy_pipeline log and the deploy_id of ev_deploy_aaa111 name dep_aaa111."),
        CONSISTENT, ACCEPT),
    Row("rationale-deployments", "names-an-id-in-capitals", "dep_aaa111 is the packet's one candidate",
        naming_in_rationale("DEP_AAA111 changed the parser."), CONSISTENT, ACCEPT, pinned=H8_PATTERN),
    Row("rationale-deployments", "no-candidates", "the packet lists no candidate change",
        naming_in_rationale("dep_aaa111 changed the parser."), packet(*CONSISTENT_ITEMS, changes=()),
        evidence_check(outside_reason([DEPLOY_ID])), missing_evidence=True),
    Row("rationale-deployments", "names-a-deployed-version-older-than-the-lookback", "ev_fixture ran against dep_fff000, "
        "which is not a candidate", naming_in_rationale("The fixture ran against dep_fff000."),
        consistent_with(with_facts(FAILING_FIXTURE, deployed_version="dep_fff000")),
        evidence_check(outside_reason(["dep_fff000"])),
        pinned="the rule reads candidate changes only, as H8 does; packet.py lists a deployment as a candidate only "
               "inside the 6 h lookback, so the deployed version of an older release is refused when named, although the "
               "packet holds it; none of the 263 stored packets holds a deployment ID that is not a candidate"),
]


# --------------------------------------------------------------------------- a supporting item mentions the change (val-7 b)


def rollout_log(deploy_id: str) -> EvidenceItem:
    return evidence("ev_log_01", EvidenceKind.LOG, facts={"source": "deploy_pipeline", "level": "info"},
                    text=diagnosis.ROLLOUT_LINE.format(deploy_id=deploy_id, service="order_worker",
                                                       message=diagnosis.COMMIT_MESSAGE_PLACEHOLDER))


def blaming(*supporting: str, contradicting: Sequence[str] = (), change: str = DEPLOY_ID) -> dict[str, object]:
    return answer("merchant_worker_regression", "reprocess_inbox", evidence_level="supported", faulting_change_id=change,
                  supporting_evidence_ids=list(supporting), contradicting_evidence_ids=list(contradicting))


FAILING_WORKER_ITEMS = (QUIET, worker_health(exhausted=4, failed_last_hour=12), worker_errors(), DEPLOYMENT)
FAILING_WORKER = packet(*FAILING_WORKER_ITEMS)
REVERTING = evidence("ev_deploy_bbb222", EvidenceKind.DEPLOYMENT,
                     facts={"deploy_id": "dep_bbb222", diagnosis.COMMIT_MESSAGE_FACT: "Revert dep_aaa111"})
GROUNDING_ROWS = [
    Row("attribution-grounding", "its-deployment-item", "ev_deploy_aaa111: deploy_id dep_aaa111",
        blaming("ev_worker_errors", DEPLOYMENT.evidence_id), FAILING_WORKER, ACCEPT),
    Row("attribution-grounding", "a-fixture-check-naming-the-deployed-version", "ev_fixture: deployed_version dep_aaa111",
        blaming("ev_worker_errors", "ev_fixture"), packet(*FAILING_WORKER_ITEMS, FAILING_FIXTURE), ACCEPT),
    Row("attribution-grounding", "worker-errors-under-its-version", "ev_worker_errors: worker_versions has the key "
        "dep_aaa111", blaming("ev_worker_errors"),
        packet(QUIET, worker_health(exhausted=4, failed_last_hour=12),
               with_facts(worker_errors(), worker_versions={DEPLOY_ID: 12}), DEPLOYMENT), ACCEPT),
    Row("attribution-grounding", "its-rollout-log-line", "ev_log_01's text is the pipeline's rollout line for dep_aaa111",
        blaming("ev_worker_errors", "ev_log_01"), packet(*FAILING_WORKER_ITEMS, rollout_log(DEPLOY_ID)), ACCEPT),
    Row("attribution-grounding", "names-unknown", "nothing cited names a deployment", blaming(change="unknown"),
        FAILING_WORKER, ACCEPT),
    Row("attribution-grounding", "names-none", "nothing cited names a deployment", blaming(change="none"),
        FAILING_WORKER, ACCEPT),
    Row("attribution-grounding", "nothing-cited", "ev_deploy_aaa111 is in the packet", blaming(), FAILING_WORKER,
        evidence_check(UNGROUNDED_REASON)),
    Row("attribution-grounding", "items-that-do-not-name-it", "ev_worker_errors and ev_worker name no deployment",
        blaming("ev_worker_errors", "ev_worker"), FAILING_WORKER, evidence_check(UNGROUNDED_REASON)),
    Row("attribution-grounding", "its-item-only-as-contradicting", "ev_deploy_aaa111 is cited, as contradicting",
        blaming("ev_worker_errors", contradicting=[DEPLOYMENT.evidence_id]), FAILING_WORKER,
        evidence_check(UNGROUNDED_REASON)),
    Row("attribution-grounding", "a-longer-id", "ev_log_01 names dep_aaa1112, which begins with dep_aaa111",
        blaming("ev_worker_errors", "ev_log_01"), packet(*FAILING_WORKER_ITEMS, rollout_log("dep_aaa1112")),
        evidence_check(UNGROUNDED_REASON)),
    Row("attribution-grounding", "no-item-names-it", "dep_aaa111 is a candidate, but no item of the packet names it",
        blaming("ev_worker_errors"), packet(QUIET, worker_health(exhausted=4, failed_last_hour=12), worker_errors()),
        evidence_check(UNGROUNDED_REASON), missing_evidence=True),
    Row("attribution-grounding", "another-deployments-commit-message", "ev_deploy_bbb222's commit message is "
        "'Revert dep_aaa111'", blaming("ev_worker_errors", REVERTING.evidence_id),
        packet(*FAILING_WORKER_ITEMS, REVERTING, changes=(DEPLOY_ID, "dep_bbb222")), ACCEPT,
        pinned="the item is read as the packet stores it, whatever a prompt shows of it: since diag-p4 the model never "
               "reads a commit message, yet one that names the deployment counts, as text counts in the mechanical "
               "check's S2"),
]


ROWS: tuple[Row, ...] = (
    *operation_rows(), *change_rows(), *FIXTURE_ROWS, *METHOD_ROWS, *ALERT_ROWS, *GAP_ROWS, *WORKER_ROWS,
    *UNKNOWN_ROWS, *RECEIVER_ROWS, *RATIONALE_ROWS, *GROUNDING_ROWS,
)
REFUSED_ROWS = tuple(row for row in ROWS if not row.decision.accepted)


def row_ids(rows: Sequence[Row]) -> list[str]:
    return [row.row_id for row in rows]


# --------------------------------------------------------------------------- the tables


@pytest.mark.parametrize("row", ROWS, ids=row_ids(ROWS))
def test_the_validator_decides_as_the_truth_table_says(row: Row) -> None:
    if row.decision.accepted:
        assert validate_diagnosis(row.answer, row.packet) == DiagnosisOutput.model_validate(row.answer)
        return
    refused = refusal(dict(row.answer), row.packet)
    assert refused.reasons == row.decision.reasons
    assert tuple(violation.rule for violation in refused.violations) == row.decision.unsafe_rules


@pytest.mark.parametrize("row", REFUSED_ROWS, ids=row_ids(REFUSED_ROWS))
def test_a_refusal_is_counted_as_unsafe_only_when_its_class_says_so(row: Row) -> None:
    """Through the diagnosis loop, with no repair: the class decides whether M4's unsafe count moves."""
    run = diagnose(row.packet, ScriptedClient(reply(row.answer)), think=False, max_repairs=0)
    assert (run.status, run.output) == (DiagnosisStatus.NEEDS_REVIEW, None)
    assert run.calls[0].rejection_reasons == row.decision.reasons
    assert tuple(proposal.rule for proposal in run.unsafe_proposals) == row.decision.unsafe_rules
    assert (row.decision.refusal_class == UNSAFE) == bool(run.unsafe_proposals)


@pytest.mark.parametrize("row", REFUSED_ROWS, ids=row_ids(REFUSED_ROWS))
def test_with_policies_off_only_the_citation_and_candidate_checks_still_refuse(row: Row) -> None:
    """The M0 experiments switch the policies off; every rule in these tables is a policy except those base checks."""
    if not row.decision.base_reasons:
        assert validate_diagnosis(row.answer, row.packet, enforce_policies=False) == DiagnosisOutput.model_validate(
            row.answer)
        return
    with pytest.raises(DiagnosisContractError) as refused:
        validate_diagnosis(row.answer, row.packet, enforce_policies=False)
    assert refused.value.reasons == row.decision.base_reasons


def test_every_rule_has_accepted_refused_and_missing_evidence_rows() -> None:
    assert len(set(row_ids(ROWS))) == len(ROWS)
    by_rule: dict[str, list[Row]] = {}
    for row in ROWS:
        by_rule.setdefault(row.rule, []).append(row)
    assert list(by_rule) == ["operation-policy", "change-consistency", "fixture-claim", "method-naming", "alert-history",
                             "observation-gap", "worker-evidence", "unknown-names-no-deployment", "receiver-evidence",
                             "rationale-deployments", "attribution-grounding"]
    for rule, rows in by_rule.items():
        assert any(row.decision.accepted for row in rows), rule
        assert any(not row.decision.accepted for row in rows), rule
    classes = {rule: {row.decision.refusal_class for row in rows} - {None} for rule, rows in by_rule.items()}
    assert classes == {"operation-policy": {UNSAFE}, "change-consistency": {UNSAFE, EVIDENCE}, "fixture-claim": {EVIDENCE},
                       "method-naming": {EVIDENCE}, "alert-history": {EVIDENCE}, "observation-gap": {EVIDENCE},
                       "worker-evidence": {EVIDENCE}, "unknown-names-no-deployment": {EVIDENCE},
                       "receiver-evidence": {EVIDENCE}, "rationale-deployments": {EVIDENCE},
                       "attribution-grounding": {EVIDENCE}}
    # Every rule that reads packet evidence has a row where that evidence is absent or unreadable. The operation
    # policy, change consistency and val-6 (a) read the answer alone.
    with_missing = {row.rule for row in ROWS if row.missing_evidence}
    assert with_missing == {"fixture-claim", "method-naming", "alert-history", "observation-gap", "worker-evidence",
                            "receiver-evidence", "rationale-deployments", "attribution-grounding"}
    assert Counter(row.decision.accepted for row in ROWS) == {True: 66, False: 74}


def test_the_operation_table_has_one_state_changing_cell() -> None:
    assert [hypothesis for hypothesis, allowed in ALLOWED_OPERATIONS.items() if "reprocess_inbox" in allowed] == [
        "merchant_worker_regression"]


# --------------------------------------------------------------------------- the pinned rows, against stored and rules evidence


def test_the_gap_boundary_is_the_rules_baselines() -> None:
    """On well-formed packets the exception accepts no_incident exactly where rules._unresolved_state answers it."""
    for gap in GAP_CASES:
        built = gap.packet()
        assert (rules.diagnose(built).hypothesis is Hypothesis.NO_INCIDENT) == gap.accepted, gap.case


def test_three_frozen_m4_packets_are_real_drops_that_stopped_firing_and_refuse_no_incident() -> None:
    """The recovered-transient shape (an alert in the history, not firing at the cutoff) occurs in the frozen set only
    as a real method drop, so refusing no_incident there is the conservative choice, and a correct one."""
    stopped = []
    for case in frozen_cases():
        trigger = next(item for item in case.packet.items if item.evidence_id == "ev_trigger")
        alerts = alert_history(case.packet)
        assert alerts is not None
        quiet_now = sorted(alerts - set(trigger.facts["fired"]) - {SYMPTOM})
        if quiet_now:
            stopped.append((case, quiet_now, {name: trigger.facts["reasons"][name] for name in quiet_now}))
    assert [(case.family, case.variant, reasons) for case, _, reasons in stopped] == [
        ("D2", "moderate_method_drop", {"method_authorization_drop[netbanking]": "insufficient_samples"}),
        ("D2", "moderate_method_drop", {"method_authorization_drop[card]": "within_baseline"}),
        ("D6", "innocent_deploy_near_method_drop", {"method_authorization_drop[card]": "within_baseline"}),
    ]
    for case, quiet_now, _ in stopped:
        assert case.expected is Hypothesis.PROVIDER_METHOD_DEGRADATION, case.label
        unexplained = sorted((alert_history(case.packet) or frozenset()) - {SYMPTOM})
        assert set(quiet_now) <= set(unexplained)
        assert refusal(SUPPRESSED, case.packet).reasons == (alert_reason(unexplained),), case.label
        method = quiet_now[0].removeprefix("method_authorization_drop[").removesuffix("]")
        assert validate_diagnosis(degradation(method), case.packet).affected_method == method


def test_every_frozen_no_incident_packet_rests_on_method_detectors_that_could_not_judge() -> None:
    """In all 11 frozen packets that expect no_incident, only the symptom alerted, and every method-drop detector
    was quiet for too few samples: the acceptance there reads an absent alert, not a measured healthy method."""
    quiet = [case for case in frozen_cases() if case.expected is Hypothesis.NO_INCIDENT]
    method_reasons: Counter[str] = Counter()
    for case in quiet:
        trigger = next(item for item in case.packet.items if item.evidence_id == "ev_trigger")
        assert alert_history(case.packet) == {SYMPTOM}, case.label
        method_reasons.update(reason for name, reason in trigger.facts["reasons"].items()
                              if name.startswith("method_authorization_drop["))
        assert validate_diagnosis(SUPPRESSED, case.packet).hypothesis is Hypothesis.NO_INCIDENT, case.label
    assert (len(quiet), method_reasons) == (11, {"insufficient_samples": 33})


def test_rule_1_accepts_no_incident_where_the_rules_baseline_sees_a_worker_failure() -> None:
    """The symptom-only-beside-worker-errors row on a full pkt-2 document: the rules answer a worker regression."""
    document = test_rules.healthy_packet()
    test_rules.worker_failing(document, first_failure_ms=test_rules.T - 39 * test_rules.MIN)
    test_rules.item(document, "ev_trigger")["facts"].update(fired=[SYMPTOM], fired_since_open=[SYMPTOM])
    built = EvidencePacket.model_validate(document)
    assert rules.diagnose(built).hypothesis is Hypothesis.MERCHANT_WORKER_REGRESSION
    assert validate_diagnosis(SUPPRESSED, built).hypothesis is Hypothesis.NO_INCIDENT
