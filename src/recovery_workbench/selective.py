"""Selective diagnosis (phase 11b): the rules path by default, the local model only for deployment-attribution cases.

* ``rules_v3``: rules-v2's answer (``rules.diagnose``) with attribution-v1's ``faulting_change_id``
  (``attribution_rules.attribute``), made consistent with ``val-6`` by fixed rules (``Resolution``).
* ``route``: MODEL or RULES from observable packet evidence only, with a machine-readable reason.
* ``diagnose_selective``: ``rules_v3`` on the RULES route; on the MODEL route the existing local diagnosis
  (``diagnosis.diagnose``: one structured call, at most one repair, ``val-6``, ``needs_review`` when refused twice).
* ``agreement``: a variant that is computed, not adopted. On the MODEL route it accepts the model's answer only when
  its ``faulting_change_id`` equals ``rules_v3``'s and sends it to review otherwise. ``agreement_from_stored`` computes
  it from a stored model answer.

**Fairness.** Everything here reads one ``EvidencePacket``. It reads no database, oracle, family or variant label,
expected answer, run ID or incident ID. The router uses attribution-v1's candidate list: which deployments precede
the failure, and whether each is relevant to the failing component. It never uses that comparator's answer, reason
or points: routing on its confidence would pick cases by how the rules path is likely to score.

**The router** (EXTENSION_PLAN.md, phase 11b). A packet goes to the model only when all four criteria hold:

1. *A live merchant-side failure with adequate coverage.* The worker failed on at least ``MIN_FAILED_EVENTS``
   distinct committed events, or the receiver rejected at least ``MIN_REJECTED_EVENTS`` distinct valid deliveries.
   Rejections count only when read as rules-v2 reads them: the rejection detector alerted, or nothing was accepted
   since the first rejection. The receiver counts because it is the first stage of the merchant's processing. A
   failing fixture check without live failures is a latent defect with no onset to attribute. Provider observation
   coverage is deliberately not used: it measures the view of payment outcomes, not of the merchant's pipeline, and
   a receiver failure lowers it by itself.
2. *An attribution question.* More than one plausible candidate deployment precedes the failure (attribution-v1's
   candidates whose relevance is direct or shared), or the failure began more than ``DELAYED_ONSET_MS`` after the
   latest plausible candidate. The delay is measured from a plausible candidate because an unrelated deployment's
   timing says nothing about this failure.
3. *Something to read.* A plausible candidate's deployment carries a diff hunk, or the failure carries a stack trace.
4. *No payment-state conflict shown.* Contradictory provider evidence stays below rules-v2's own floor for an
   ambiguous state (``rules.MIN_CONFLICTED_PAYMENTS`` and ``rules.MIN_CONFLICTED_SHARE``). A packet that cannot show
   this stays on the rules path.

Every threshold was fixed, with its reason, before any routing rate or result was computed. They are not tuned.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from pydantic import JsonValue, model_validator

from recovery_workbench import attribution_rules, diagnosis, rules
from recovery_workbench.attribution_rules import ATTRIBUTION_RULES_VERSION, Attribution, Relevance
from recovery_workbench.contracts import (
    CHOICE_NONE,
    CHOICE_UNKNOWN,
    CheckType,
    ContractError,
    DiagnosisOutput,
    DiagnosisStatus,
    EvidenceItem,
    EvidenceKind,
    EvidenceLevel,
    EvidencePacket,
    Hypothesis,
    Operation,
    StrictModel,
)

ROUTER_VERSION = "router-v1"
RULES_V3_VERSION = "rules-v3"

# Criterion 1. The floors are the matching detector's own (metrics.IncidentDetectorSettings), which judges no fewer.
MIN_FAILED_EVENTS = 5  # processing_min_pending: fewer failed events are too few to call a live regression
MIN_REJECTED_EVENTS = 10  # rejection_min_count, on distinct events so a sender's retries of one event count once
# Criterion 2. A deployment that breaks live processing fails on the next events it handles. A first failure more than
# one detector window (metrics.MeasureWindows.window_ms) after the latest plausible deployment is not explained by
# that deployment's timing.
DELAYED_ONSET_MS = 30 * 60_000
MIN_PLAUSIBLE_CANDIDATES = 2  # "more than one plausible candidate": the plan's definition, not a tuned value
# Criterion 4 uses rules.MIN_CONFLICTED_PAYMENTS and rules.MIN_CONFLICTED_SHARE, so that both paths agree on whether
# a conflict is shown. Below that floor a conflict is a per-order exclusion for the planner, not an incident question.

WORKER_ERRORS_ID = "ev_worker_errors"
DELIVERY_ID = "ev_delivery"
FIXTURE_ID = "ev_fixture"
PROVIDER_STATE_ID = "ev_provider_state"
TRIGGER_ID = "ev_trigger"
REJECTION_DETECTOR = "delivery_rejection_spike"
REJECTION_COUNTS = ("rejected_signature", "rejected_schema", "rejected_size")
_TRACE_FRAME = re.compile(r'^\s*File "[^"]+", line \d+', re.MULTILINE)
_DIFF_HUNK = re.compile(r"^@@ ", re.MULTILINE)


# --------------------------------------------------------------------------- the router


class RouteTarget(StrEnum):
    MODEL = "model"
    RULES = "rules"


class RouteReason(StrEnum):
    """Why a packet took its route.

    On the MODEL route: the form of the attribution question that made the packet eligible. On the RULES route: a
    criterion the packet failed. Criteria are checked in the order below, and every failed one is recorded.
    """

    SEVERAL_PLAUSIBLE_CANDIDATES = "several_plausible_candidates"
    DELAYED_ONSET = "delayed_onset"
    NO_LIVE_MERCHANT_FAILURE = "no_live_merchant_failure"
    LIVE_FAILURE_BELOW_FLOOR = "live_failure_below_floor"
    NO_PLAUSIBLE_CANDIDATE = "no_plausible_candidate"
    ONE_CANDIDATE_PROMPT_ONSET = "one_candidate_prompt_onset"
    NO_CODE_OR_TRACE_EVIDENCE = "no_code_or_trace_evidence"
    PAYMENT_STATE_CONFLICT = "payment_state_conflict"
    PAYMENT_STATE_UNREADABLE = "payment_state_unreadable"


MODEL_REASONS = frozenset({RouteReason.SEVERAL_PLAUSIBLE_CANDIDATES, RouteReason.DELAYED_ONSET})


class Route(StrictModel):
    """Where one packet goes and why; ``measurements`` holds every value the decision read."""

    router_version: str = ROUTER_VERSION
    target: RouteTarget
    reason: RouteReason
    unmet: tuple[RouteReason, ...]  # every failed criterion in check order; empty exactly on the MODEL route
    measurements: dict[str, JsonValue]

    @model_validator(mode="after")
    def _reason_matches_target(self) -> Route:
        if (self.target is RouteTarget.MODEL) != (not self.unmet):
            raise ValueError("a packet goes to the model exactly when it fails no criterion")
        if (self.reason in MODEL_REASONS) != (self.target is RouteTarget.MODEL):
            raise ValueError(f"reason {self.reason.value!r} does not belong to the {self.target.value} route")
        if self.unmet and self.reason is not self.unmet[0]:
            raise ValueError("a RULES route reports the first criterion it failed")
        return self


def route(packet: EvidencePacket) -> Route:
    """MODEL or RULES for one packet, deterministic and from observable evidence only."""
    return _route(packet, attribution_rules.attribute(packet))


def _route(packet: EvidencePacket, attribution: Attribution) -> Route:
    failed_events = _int_fact(_item(packet, WORKER_ERRORS_ID), "failed_events") or 0
    rejected_events = _rejected_events(packet)
    plausible = [c for c in attribution.candidates if c.relevance is not Relevance.UNRELATED]
    latest = max((c.deployed_at_ms for c in plausible), default=None)
    delay = None if attribution.onset_ms is None or latest is None else attribution.onset_ms - latest
    with_diff = [c.deploy_id for c in plausible if _DIFF_HUNK.search(_text_of(packet, c.evidence_id))]
    has_trace = any(_TRACE_FRAME.search(_text_of(packet, item_id)) for item_id in (WORKER_ERRORS_ID, FIXTURE_ID))
    conflicted = _int_fact(_item(packet, PROVIDER_STATE_ID), "payments_needing_reconciliation")
    attempts = packet.incident.summary_counts.get("payment_attempts", 0)
    unmet = [
        *_failure_unmet(failed_events, rejected_events),
        *_question_unmet(len(plausible), delay),
        *([] if with_diff or has_trace else [RouteReason.NO_CODE_OR_TRACE_EVIDENCE]),
        *_conflict_unmet(conflicted, attempts),
    ]
    measurements: dict[str, JsonValue] = {
        "failed_events": failed_events,
        "distinct_events_rejected": rejected_events,
        "onset_ms": attribution.onset_ms,
        "plausible_candidates": [c.deploy_id for c in plausible],
        "latest_plausible_deployed_at_ms": latest,
        "onset_delay_ms": delay,
        "candidates_with_diff": with_diff,
        "failure_trace": has_trace,
        "conflicted_payments": conflicted,
        "payment_attempts": attempts,
    }
    if unmet:
        return Route(target=RouteTarget.RULES, reason=unmet[0], unmet=tuple(unmet), measurements=measurements)
    several = len(plausible) >= MIN_PLAUSIBLE_CANDIDATES
    reason = RouteReason.SEVERAL_PLAUSIBLE_CANDIDATES if several else RouteReason.DELAYED_ONSET
    return Route(target=RouteTarget.MODEL, reason=reason, unmet=(), measurements=measurements)


def _failure_unmet(failed_events: int, rejected_events: int) -> list[RouteReason]:
    """Criterion 1: a live failure, seen on at least its detector's floor of distinct events."""
    if failed_events >= MIN_FAILED_EVENTS or rejected_events >= MIN_REJECTED_EVENTS:
        return []
    if failed_events > 0 or rejected_events > 0:
        return [RouteReason.LIVE_FAILURE_BELOW_FLOOR]
    return [RouteReason.NO_LIVE_MERCHANT_FAILURE]


def _question_unmet(plausible: int, delay_ms: int | None) -> list[RouteReason]:
    """Criterion 2: several plausible candidates, or one whose timing does not explain the onset."""
    if plausible >= MIN_PLAUSIBLE_CANDIDATES:
        return []
    if plausible == 0:
        return [RouteReason.NO_PLAUSIBLE_CANDIDATE]
    if delay_ms is not None and delay_ms > DELAYED_ONSET_MS:
        return []
    return [RouteReason.ONE_CANDIDATE_PROMPT_ONSET]


def _conflict_unmet(conflicted: int | None, attempts: int) -> list[RouteReason]:
    """Criterion 4: contradictory provider evidence below rules-v2's ambiguity floor, read fail-closed."""
    if conflicted is None:
        return [RouteReason.PAYMENT_STATE_UNREADABLE]
    shown = (conflicted >= rules.MIN_CONFLICTED_PAYMENTS
             and conflicted / max(1, attempts) >= rules.MIN_CONFLICTED_SHARE)
    return [RouteReason.PAYMENT_STATE_CONFLICT] if shown else []


def _rejected_events(packet: EvidencePacket) -> int:
    """Distinct rejected deliveries, counted only when the receiver is rejecting as rules-v2 reads it."""
    delivery = _item(packet, DELIVERY_ID)
    rejected = sum(_int_fact(delivery, key) or 0 for key in REJECTION_COUNTS)
    if delivery is None or rejected == 0 or _int_fact(delivery, "first_rejection_ms") is None:
        return 0
    alerted = REJECTION_DETECTOR in (diagnosis.alert_history(packet) or frozenset())
    if not alerted and _int_fact(delivery, "accepted_since_first_rejection") != 0:
        return 0
    return _int_fact(delivery, "distinct_events_rejected") or 0


def _item(packet: EvidencePacket, evidence_id: str) -> EvidenceItem | None:
    return next((item for item in packet.items if item.evidence_id == evidence_id), None)


def _text_of(packet: EvidencePacket, evidence_id: str) -> str:
    found = _item(packet, evidence_id)
    if found is None or found.text is None:
        return ""
    return found.text


def _int_fact(item: EvidenceItem | None, key: str) -> int | None:
    """An integer fact, or None when the item or the fact is missing or not an integer (a bool is not one)."""
    value = item.facts.get(key) if item is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------- rules-v3


class Resolution(StrEnum):
    """How rules-v3 made attribution-v1's deployment consistent with rules-v2's hypothesis, checked in this order."""

    AS_ATTRIBUTED = "as_attributed"  # attribution-v1's answer stands
    HYPOTHESIS_DENIES_A_CHANGE = "hypothesis_denies_a_change"  # change consistency: the hypothesis names no change
    UNKNOWN_NAMES_NO_CHANGE = "unknown_names_no_change"  # val-6 (a): an unknown hypothesis names no deployment
    MERCHANT_FAILURE_NOT_FOUND = "merchant_failure_not_found"  # merchant-side hypothesis, but attribution found none
    ESCALATED = "escalated"  # the merged answer still failed val-6: escalate with no hypothesis


MERCHANT_SIDE_HYPOTHESES = frozenset({Hypothesis.MERCHANT_WORKER_REGRESSION, Hypothesis.RECEIVER_REJECTING_DELIVERIES})
_NO_CHANGE = (CHOICE_NONE, CHOICE_UNKNOWN)


class RulesV3Answer(StrictModel):
    """rules-v3's answer with what it was made from, so every difference from rules-v2 can be traced."""

    rules_version: str = RULES_V3_VERSION
    output: DiagnosisOutput
    rules_v2_change: str
    attributed_change: str
    attribution: dict[str, JsonValue]  # Attribution.as_json(): reason, candidates, links and points
    resolution: Resolution
    escalation_reasons: tuple[str, ...] = ()  # val-6's reasons, when the merged answer was refused

    @model_validator(mode="after")
    def _reasons_only_when_escalated(self) -> RulesV3Answer:
        if (self.resolution is Resolution.ESCALATED) != bool(self.escalation_reasons):
            raise ValueError("escalation reasons are recorded exactly when the answer was escalated")
        return self


def rules_v3(packet: EvidencePacket) -> DiagnosisOutput:
    """rules-v2's diagnosis with attribution-v1's faulting deployment; always passes ``val-6`` on its packet."""
    return answer_rules_v3(packet).output


def answer_rules_v3(packet: EvidencePacket) -> RulesV3Answer:
    return _answer_rules_v3(packet, attribution_rules.attribute(packet))


def _answer_rules_v3(packet: EvidencePacket, attribution: Attribution) -> RulesV3Answer:
    previous = rules.diagnose(packet)
    change, resolution = _resolved_change(previous.hypothesis, attribution.faulting_change_id)
    merged = previous
    if change != previous.faulting_change_id:
        merged = _with_change(packet, previous, attribution, change, resolution)
    try:
        output = diagnosis.validate_diagnosis(merged.model_dump(mode="json"), packet)
    except diagnosis.DiagnosisContractError as refused:
        # rules-v2 was built before val-6, so its own fields can fail it (a receiver diagnosis backed only by a failing
        # fixture check, with no rejected delivery). Handled, not hidden: the answer escalates and keeps the reasons.
        return RulesV3Answer(
            output=_escalation(packet, previous, attribution), rules_v2_change=previous.faulting_change_id,
            attributed_change=attribution.faulting_change_id, attribution=attribution.as_json(),
            resolution=Resolution.ESCALATED, escalation_reasons=refused.reasons,
        )
    return RulesV3Answer(
        output=output, rules_v2_change=previous.faulting_change_id, attributed_change=attribution.faulting_change_id,
        attribution=attribution.as_json(), resolution=resolution,
    )


def _resolved_change(hypothesis: Hypothesis, attributed: str) -> tuple[str, Resolution]:
    """attribution-v1's answer, unless rules-v2's hypothesis contradicts it; a contradiction becomes ``unknown``.

    The two deterministic components then disagree about the cause, and disagreement is an abstention on the
    deployment, never a choice between them. rules-v2 keeps the hypothesis and the operation.
    """
    if not diagnosis.change_consistent(hypothesis, attributed):
        return CHOICE_UNKNOWN, Resolution.HYPOTHESIS_DENIES_A_CHANGE
    if not diagnosis.unknown_names_no_deployment(hypothesis, attributed):
        return CHOICE_UNKNOWN, Resolution.UNKNOWN_NAMES_NO_CHANGE
    if hypothesis in MERCHANT_SIDE_HYPOTHESES and attributed == CHOICE_NONE:
        return CHOICE_UNKNOWN, Resolution.MERCHANT_FAILURE_NOT_FOUND
    return attributed, Resolution.AS_ATTRIBUTED


def _with_change(packet: EvidencePacket, previous: DiagnosisOutput, attribution: Attribution, change: str,
                 resolution: Resolution) -> DiagnosisOutput:
    """rules-v2's answer with another faulting change, and its deployment citation and rationale made to match.

    Every decision field except ``faulting_change_id`` stays rules-v2's: hypothesis, method, evidence level,
    operation and checks. The caller validates the result.
    """
    deployments = {item.evidence_id for item in packet.items if item.kind is EvidenceKind.DEPLOYMENT}
    attributed = [evidence_id for evidence_id in attribution.evidence_ids if evidence_id in deployments]
    cited = _citations(previous.supporting_evidence_ids, deployments,
                       attributed if resolution is Resolution.AS_ATTRIBUTED else [])
    note = _attribution_note(attribution, change, resolution, previous.hypothesis)
    room = diagnosis.RATIONALE_MAX_CHARS - len(note) - 1
    return previous.model_copy(update={
        "faulting_change_id": change,
        "supporting_evidence_ids": cited,
        "rationale": f"{previous.rationale[:room]} {note}",
    })


def _citations(supporting: Sequence[str], deployments: set[str], attributed: Sequence[str]) -> tuple[str, ...]:
    """rules-v2's citations with its deployment citation replaced, in place, by attribution-v1's deployment items."""
    kept = [evidence_id for evidence_id in supporting if evidence_id not in deployments]
    first = next((index for index, evidence_id in enumerate(supporting) if evidence_id in deployments), len(supporting))
    position = sum(evidence_id not in deployments for evidence_id in supporting[:first])
    return tuple(dict.fromkeys([*kept[:position], *attributed, *kept[position:]]))[: diagnosis.MAX_CITED_IDS]


def _attribution_note(attribution: Attribution, change: str, resolution: Resolution, hypothesis: Hypothesis) -> str:
    """One sentence that says where the faulting change came from; it quotes only IDs and fixed codes."""
    source = f"Deployment attribution ({ATTRIBUTION_RULES_VERSION}, {attribution.reason.value})"
    named = attribution.faulting_change_id
    unknown_because = "so the faulting change is unknown."
    if resolution is Resolution.HYPOTHESIS_DENIES_A_CHANGE:
        return f"{source} names {named}, but {hypothesis.value} names no deployment cause, {unknown_because}"
    if resolution is Resolution.UNKNOWN_NAMES_NO_CHANGE:
        return f"{source} names {named}, but an unknown hypothesis cannot name a deployment, {unknown_because}"
    if resolution is Resolution.MERCHANT_FAILURE_NOT_FOUND:
        return f"{source} finds no merchant-side failure, which contradicts {hypothesis.value}, {unknown_because}"
    if change == CHOICE_UNKNOWN:
        return f"{source} cannot single out one deployment, {unknown_because}"
    if change == CHOICE_NONE:
        return f"{source} finds no merchant-side failure, so no deployment is named."
    return f"{source} names {change} from the diff and failure evidence instead."


def _escalation(packet: EvidencePacket, previous: DiagnosisOutput, attribution: Attribution) -> DiagnosisOutput:
    """rules-v2's own no-rule answer: escalate with the evidence collected, naming no hypothesis and no deployment."""
    named = attribution.faulting_change_id
    escalation = DiagnosisOutput(
        hypothesis=Hypothesis.UNKNOWN,
        faulting_change_id=named if named in _NO_CHANGE else CHOICE_UNKNOWN,
        affected_method=CHOICE_NONE,
        evidence_level=EvidenceLevel.INSUFFICIENT,
        supporting_evidence_ids=tuple(item.evidence_id for item in packet.items if item.evidence_id == TRIGGER_ID),
        contradicting_evidence_ids=(),
        missing_checks=(CheckType.INSPECT_RECENT_CHANGES, CheckType.INSPECT_ORDER_JOURNEY),
        next_check=CheckType.INSPECT_RECENT_CHANGES,
        proposed_operation=Operation.ESCALATE,
        rationale=(f"rules-v2 answered {previous.hypothesis.value} with {previous.proposed_operation.value}, which "
                   "val-6 refuses on this packet; escalating with the evidence collected instead of proposing an "
                   "operation."),
    )
    try:
        return diagnosis.validate_diagnosis(escalation.model_dump(mode="json"), packet)
    except diagnosis.DiagnosisContractError as refused:
        raise ContractError(f"rules-v3's escalation answer fails val-6, which it must never do: {refused}") from refused


# --------------------------------------------------------------------------- selective diagnosis


class SelectiveDiagnosis(StrictModel):
    """The route taken and why, and the answer that route produced.

    ``rules_v3`` is computed on both routes: it is the RULES route's answer, and on the MODEL route it is kept so that
    the agreement variant can be computed from this record alone.
    """

    route: Route
    status: DiagnosisStatus
    output: DiagnosisOutput | None
    rules_v3: RulesV3Answer
    model_run: diagnosis.DiagnosisRun | None  # exactly on the MODEL route
    packet_hash: str

    @model_validator(mode="after")
    def _answer_matches_route(self) -> SelectiveDiagnosis:
        if (self.route.target is RouteTarget.MODEL) != (self.model_run is not None):
            raise ValueError("a model run is recorded exactly on the MODEL route")
        if self.model_run is None:
            route_answer = (self.rules_v3.output, DiagnosisStatus.VALIDATED)
        else:
            route_answer = (self.model_run.output, self.model_run.status)
        if (self.output, self.status) != route_answer:
            raise ValueError("the answer must be the route's own: rules-v3 on RULES, the model run on MODEL")
        return self


def diagnose_selective(
    packet: EvidencePacket,
    client: diagnosis.ChatClient,
    *,
    think: bool,
    deadline_s: float = diagnosis.DEFAULT_DEADLINE_S,
) -> SelectiveDiagnosis:
    """rules-v3 on the RULES route; on the MODEL route one structured call, at most one repair, val-6.

    A MODEL-route answer refused twice, or a model that cannot be reached in time, ends in ``needs_review``, as
    ``diagnosis.diagnose`` returns it. It never falls back to rules-v3: the route decides who answers.
    """
    attribution = attribution_rules.attribute(packet)
    chosen = _route(packet, attribution)
    rules_answer = _answer_rules_v3(packet, attribution)
    if chosen.target is RouteTarget.RULES:
        return SelectiveDiagnosis(route=chosen, status=DiagnosisStatus.VALIDATED, output=rules_answer.output,
                                  rules_v3=rules_answer, model_run=None, packet_hash=packet.packet_hash())
    run = diagnosis.diagnose(packet, client, think=think, deadline_s=deadline_s, max_repairs=1, enforce_policies=True)
    return SelectiveDiagnosis(route=chosen, status=run.status, output=run.output, rules_v3=rules_answer,
                              model_run=run, packet_hash=packet.packet_hash())


# --------------------------------------------------------------------------- the agreement variant (computed only)


class AgreementVerdict(StrEnum):
    ACCEPTED = "accepted"  # MODEL route: the model's faulting change equals rules-v3's
    CHANGES_DIFFER = "changes_differ"  # MODEL route: they differ, so a person reviews it
    MODEL_NEEDS_REVIEW = "model_needs_review"  # MODEL route: the model run itself ended in needs_review
    MODEL_ANSWER_REFUSED = "model_answer_refused"  # MODEL route: a stored answer that today's validator refuses
    RULES_ROUTE = "rules_route"  # not applicable: rules-v3's answer stands


class Agreement(StrictModel):
    verdict: AgreementVerdict
    status: DiagnosisStatus
    output: DiagnosisOutput | None
    model_change: str | None
    rules_v3_change: str
    reasons: tuple[str, ...] = ()  # today's validator's reasons for refusing a stored answer


def agreement(selective: SelectiveDiagnosis) -> Agreement:
    """The agreement variant for one selective diagnosis record."""
    model_output = selective.model_run.output if selective.model_run is not None else None
    return _agreement(selective.route, selective.rules_v3.output, model_output)


def agreement_from_stored(
    packet: EvidencePacket, model_output: DiagnosisOutput | None, *, answered_packet_hash: str,
) -> Agreement:
    """The agreement variant for a model answer stored earlier for this exact packet (None: it ended in needs_review).

    The stored answer is checked against today's validator first, as a recorded replay is, because an answer from an
    earlier validator version may not pass ``val-6``.
    """
    if answered_packet_hash != packet.packet_hash():
        raise ContractError(
            f"the stored answer is for packet {answered_packet_hash[:16]}, not {packet.packet_hash()[:16]}; "
            "the agreement check compares answers to one identical packet only")
    attribution = attribution_rules.attribute(packet)
    chosen = _route(packet, attribution)
    rules_output = _answer_rules_v3(packet, attribution).output
    if chosen.target is RouteTarget.MODEL and model_output is not None:
        try:
            diagnosis.validate_diagnosis(model_output.model_dump(mode="json"), packet)
        except diagnosis.DiagnosisContractError as refused:
            # A refused answer is a verdict of its own here, with its reasons, so the variant counts it.
            return Agreement(verdict=AgreementVerdict.MODEL_ANSWER_REFUSED, status=DiagnosisStatus.NEEDS_REVIEW,
                             output=None, model_change=model_output.faulting_change_id,
                             rules_v3_change=rules_output.faulting_change_id, reasons=refused.reasons)
    return _agreement(chosen, rules_output, model_output)


def _agreement(chosen: Route, rules_output: DiagnosisOutput, model_output: DiagnosisOutput | None) -> Agreement:
    rules_change = rules_output.faulting_change_id
    if chosen.target is RouteTarget.RULES:
        return Agreement(verdict=AgreementVerdict.RULES_ROUTE, status=DiagnosisStatus.VALIDATED, output=rules_output,
                         model_change=None, rules_v3_change=rules_change)
    if model_output is None:
        return Agreement(verdict=AgreementVerdict.MODEL_NEEDS_REVIEW, status=DiagnosisStatus.NEEDS_REVIEW,
                         output=None, model_change=None, rules_v3_change=rules_change)
    if model_output.faulting_change_id != rules_change:
        return Agreement(verdict=AgreementVerdict.CHANGES_DIFFER, status=DiagnosisStatus.NEEDS_REVIEW, output=None,
                         model_change=model_output.faulting_change_id, rules_v3_change=rules_change)
    return Agreement(verdict=AgreementVerdict.ACCEPTED, status=DiagnosisStatus.VALIDATED, output=model_output,
                     model_change=model_output.faulting_change_id, rules_v3_change=rules_change)
