"""A deliberately strong rules baseline (EVALUATION.md §9) over ``pkt-1`` packets.

It reads only the core keys listed in docs/PACKET_SCHEMA.md: which detectors fired, the
structured exception category, provider error and state fields, the latest deployment before
the first failure or rejection, top-frame blame from the stack trace or the fixture results,
and the fixture results themselves. The model receives exactly the same packet. Rules are
ordered from the most specific signal to the least; the first that applies decides.
Every output passes ``diagnosis.validate_diagnosis(..., enforce_policies=True)``.
Thresholds were set on hand-built packets and development-split runs only, and no rule reads a
run or incident ID.

CHANGES (one development-split tuning pass, phase 4; the test split was never read):

* rules-v2: every rule reads the incident's alert history (``fired`` + ``fired_since_open`` +
  ``incident.trigger``). Dev evidence: in the moderate card-drop runs (seeds 1012, 1053) the
  detector that opened the incident had stopped firing by the cutoff, so ``fired`` was empty and
  rules-v1 answered unknown (orchestrator's re-score on eval-v1 dev: 16/18, these two the misses).
  rules-v2 answers both correctly; on eval-v2 dev it scores 17/18, the miss being the new
  delayed-onset run below.
* Considered and rejected: "prefer a configuration change when the traced code did not change", which
  would fix the one remaining dev miss (delayed-onset config change, seed 1003, where an innocent
  deploy lands between the fault and the first failure). One example is too little evidence, and the
  rule has a real failure mode: an upstream code fault plus an unrelated earlier config change.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from recovery_workbench.contracts import (
    CHOICE_NONE,
    CHOICE_UNKNOWN,
    CheckType,
    DiagnosisOutput,
    EvidenceItem,
    EvidenceKind,
    EvidenceLevel,
    EvidencePacket,
    Hypothesis,
    Operation,
)

RULES_VERSION = "rules-v2"  # pkt-2 alert history; see CHANGES below

# Two deployments this close before the first failure are told apart by top-frame blame.
TIE_WINDOW_MS = 10 * 60_000
# Contradictory provider evidence on at least this many payments (and this share) means ambiguous state.
MIN_CONFLICTED_PAYMENTS = 3
MIN_CONFLICTED_SHARE = 0.02
# A mature cohort with this many attempts and this share unresolved is ambiguous even before the detector confirms.
MIN_UNRESOLVED_ATTEMPTS = 20
MIN_UNRESOLVED_SHARE = 0.15
# Below this coverage, or with no recent snapshot, unresolved attempts are an observation gap.
MIN_OBSERVATION_COVERAGE = 0.9
# A method whose share of attempts moved this much explains an aggregate drop without degradation.
MIN_MIX_SHIFT = 0.2
SPARSE_MATURE_ATTEMPTS = 30

_METHOD_DROP = re.compile(r"^method_authorization_drop\[(?P<method>[a-z_]+)\]$")
_TRACE_FILE = re.compile(r'File "(?P<path>merchant_app/[A-Za-z0-9_]+\.py)", line \d+')  # NOSONAR(S6353) \w would also match non-ASCII letters
_FIXTURE_FRAME = re.compile(r"^(?P<path>merchant_app/[A-Za-z0-9_]+\.py):\d+")  # NOSONAR(S6353) \w would also match non-ASCII letters
_RECEIVER_TEST = "signed_delivery_verifies"


@dataclass(frozen=True)
class _Packet:
    """Core-key accessors over one packet; optional items are None when absent."""

    packet: EvidencePacket

    def item(self, evidence_id: str) -> EvidenceItem | None:
        return next((item for item in self.packet.items if item.evidence_id == evidence_id), None)

    def facts(self, evidence_id: str) -> Mapping[str, JsonValue]:
        found = self.item(evidence_id)
        if found is None:
            raise KeyError(f"packet {self.packet.packet_version} lacks the core item {evidence_id!r}")
        return found.facts

    @property
    def alerts(self) -> frozenset[str]:
        """Every detector that alerted for this incident: firing now, fired since it opened, or opened it.

        pkt-2: a detector that opened the incident can stop firing before the cutoff; the
        alert history is evidence an on-call engineer has, so the rules read all three.
        """
        trigger = self.facts("ev_trigger")
        names = {str(name) for name in trigger["fired"]}  # type: ignore[union-attr]
        names |= {str(name) for name in trigger.get("fired_since_open", [])}  # type: ignore[union-attr]
        return frozenset(names | {self.packet.incident.trigger})

    def deployments(self) -> list[EvidenceItem]:
        items = [item for item in self.packet.items if item.kind is EvidenceKind.DEPLOYMENT]
        return sorted(items, key=lambda item: int(item.facts["deployed_at_ms"]))  # type: ignore[arg-type]

    def fixture_results(self) -> list[Mapping[str, JsonValue]]:
        return list(self.facts("ev_fixture")["results"])  # type: ignore[arg-type]

    def failing_fixture_tests(self, *, receiver: bool) -> list[Mapping[str, JsonValue]]:
        return [r for r in self.fixture_results() if not r["passed"] and (r["test_id"] == _RECEIVER_TEST) == receiver]

    def present(self, *evidence_ids: str) -> tuple[str, ...]:
        """Only IDs this packet holds, in order and without repeats: citations must be visible."""
        visible = set(self.packet.evidence_ids())
        return tuple(dict.fromkeys(evidence_id for evidence_id in evidence_ids if evidence_id in visible))


def diagnose(packet: EvidencePacket) -> DiagnosisOutput:
    """The first rule that applies decides; with no rule, an explicit unknown with escalation."""
    view = _Packet(packet)
    rules: Sequence[Callable[[_Packet], DiagnosisOutput | None]] = (
        _receiver_rejecting, _worker_regression, _method_degradation, _contradictory_state, _unresolved_state,
        _no_incident,
    )
    for rule in rules:
        output = rule(view)
        if output is not None:
            return output
    return _output(
        Hypothesis.UNKNOWN, view.present("ev_trigger"), Operation.ESCALATE, EvidenceLevel.INSUFFICIENT,
        missing=(CheckType.INSPECT_ORDER_JOURNEY, CheckType.FETCH_PROVIDER_SNAPSHOT),
        next_check=CheckType.INSPECT_ORDER_JOURNEY,
        rationale="Detectors fired but no rule's signal pattern is present; escalating with the evidence collected.",
    )


# --------------------------------------------------------------------------- rules


def _receiver_rejecting(view: _Packet) -> DiagnosisOutput | None:
    delivery = view.facts("ev_delivery")
    rejected = int(delivery["rejected_signature"]) + int(delivery["rejected_schema"])  # type: ignore[arg-type]
    first = delivery["first_rejection_ms"]
    receiver_fixture_failed = bool(view.failing_fixture_tests(receiver=True))
    spike = "delivery_rejection_spike" in view.alerts
    nothing_accepted_since = delivery["accepted_since_first_rejection"] == 0
    rejecting = rejected > 0 and first is not None and (spike or nothing_accepted_since)
    if not (receiver_fixture_failed or rejecting):
        return None
    change = _latest_before(view, int(first)) if first is not None else None  # type: ignore[arg-type]
    level = EvidenceLevel.REPRODUCED_IN_FIXTURE if receiver_fixture_failed else EvidenceLevel.SUPPORTED
    supporting = view.present("ev_trigger", "ev_delivery", *( [change.evidence_id] if change else []),
                              *(["ev_fixture"] if receiver_fixture_failed else []), "ev_provider_state")
    return _output(
        Hypothesis.RECEIVER_REJECTING_DELIVERIES, supporting, Operation.PREPARE_PROVIDER_REPLAY, level,
        change=_change_id(change), missing=(CheckType.VALIDATE_CORRECTED_HANDLER,),
        next_check=CheckType.VALIDATE_CORRECTED_HANDLER,
        rationale=(f"{rejected} signed deliveries were rejected before durable receipt since {first}; "
                   f"{'the deployed receiver fails the signed-delivery fixture; ' if receiver_fixture_failed else ''}"
                   f"the latest change before the first rejection is {_change_id(change)}. Rejected events never reached "
                   "the inbox, so the provider must resend them."),
    )


def _worker_regression(view: _Packet) -> DiagnosisOutput | None:
    errors = view.item("ev_worker_errors")
    handler_failures = view.failing_fixture_tests(receiver=False)
    if errors is None or int(errors.facts["failed_events"]) == 0:  # type: ignore[arg-type]
        return None
    first_failure = int(errors.facts["first_failure_ms"])  # type: ignore[arg-type]
    frame = _top_frame_file(errors.text, handler_failures)
    change = _latest_before(view, first_failure, prefer_file=frame)
    level = EvidenceLevel.REPRODUCED_IN_FIXTURE if handler_failures else EvidenceLevel.SUPPORTED
    category = errors.facts.get("exception_category")
    supporting = view.present("ev_trigger", "ev_worker_errors", *([change.evidence_id] if change else []),
                              *(["ev_fixture"] if handler_failures else []), "ev_metric_processing")
    return _output(
        Hypothesis.MERCHANT_WORKER_REGRESSION, supporting, Operation.REPROCESS_INBOX, level,
        change=_change_id(change), missing=(CheckType.VALIDATE_CORRECTED_HANDLER,),
        next_check=CheckType.VALIDATE_CORRECTED_HANDLER,
        rationale=(f"{errors.facts['failed_events']} committed events fail in the worker"
                   f"{f' ({category})' if category else ''}, first at {first_failure}, top frame {frame or 'unknown'}; "
                   f"{len(handler_failures)} handler fixture checks fail on the deployed code. The latest change before "
                   f"the first failure is {_change_id(change)}. Deliveries are accepted, so the payments are recorded."),
    )


def _method_degradation(view: _Packet) -> DiagnosisOutput | None:
    dropped = sorted(match.group("method") for name in view.alerts if (match := _METHOD_DROP.match(name)))
    if not dropped:
        return None
    method = max(dropped, key=lambda m: _decline(view.facts(f"ev_metric_{m}")))
    metric = view.facts(f"ev_metric_{method}")
    return _output(
        Hypothesis.PROVIDER_METHOD_DEGRADATION,
        view.present("ev_trigger", f"ev_metric_{method}", "ev_provider_state", "ev_worker"),
        Operation.ESCALATE, EvidenceLevel.SUPPORTED, method=method,
        missing=(CheckType.COMPARE_METHOD_COHORTS,), next_check=CheckType.COMPARE_METHOD_COHORTS,
        rationale=(f"The {method} authorisation detector alerted for this incident (ratio {metric['authorisation_ratio']} "
                   f"against a baseline of {metric['baseline_authorisation_ratio']}) while other methods held; "
                   "processing of captured payments is healthy. The cause is provider or issuer side: escalate."),
    )


def _contradictory_state(view: _Packet) -> DiagnosisOutput | None:
    provider = view.facts("ev_provider_state")
    conflicted = int(provider["payments_needing_reconciliation"])  # type: ignore[arg-type]
    attempts = max(1, int(view.packet.incident.summary_counts.get("payment_attempts", 0)))
    if conflicted < MIN_CONFLICTED_PAYMENTS or conflicted / attempts < MIN_CONFLICTED_SHARE:
        return None
    return _output(
        Hypothesis.AMBIGUOUS_PAYMENT_STATE, view.present("ev_trigger", "ev_provider_state", "ev_metric_processing"),
        Operation.REFRESH_PROVIDER_STATE, EvidenceLevel.SUPPORTED,
        missing=(CheckType.FETCH_PROVIDER_SNAPSHOT,), next_check=CheckType.FETCH_PROVIDER_SNAPSHOT,
        rationale=(f"{conflicted} payments carry contradictory provider evidence "
                   f"({_compact(provider['reconciliation_reasons'])}); orders are held rather than fulfilled on a guess. "
                   "Refresh provider state before any recovery."),
    )


def _unresolved_state(view: _Packet) -> DiagnosisOutput | None:
    overall = view.facts("ev_metric_all")
    provider = view.facts("ev_provider_state")
    attempts, unresolved = int(overall["attempts"]), int(overall["unresolved"])  # type: ignore[arg-type]
    spike = "unresolved_spike" in view.alerts
    if not spike and not (attempts >= MIN_UNRESOLVED_ATTEMPTS and unresolved / attempts >= MIN_UNRESOLVED_SHARE):
        return None
    statuses: Mapping[str, JsonValue] = provider["payments_by_status"]  # type: ignore[assignment]
    coverage = provider["observation_coverage"]
    unobserved = int(statuses.get("unknown", 0)) > int(statuses.get("created", 0))  # type: ignore[arg-type]
    if unobserved and ((coverage is not None and float(coverage) < MIN_OBSERVATION_COVERAGE)  # type: ignore[arg-type]
                       or provider["snapshot_age_ms"] is None):
        return _output(
            Hypothesis.NO_INCIDENT, view.present("ev_trigger", "ev_provider_state", "ev_metric_all"),
            Operation.NO_ACTION, EvidenceLevel.INSUFFICIENT,
            missing=(CheckType.FETCH_PROVIDER_SNAPSHOT, CheckType.WAIT_FOR_MATURE_COHORT),
            next_check=CheckType.FETCH_PROVIDER_SNAPSHOT,
            rationale=(f"{unresolved} of {attempts} mature attempts have no provider evidence at all and observation "
                       f"coverage is {coverage}: the gap is in our observations, not in the payments. No degradation "
                       "claim until provider state is fetched."),
        )
    return _output(
        Hypothesis.AMBIGUOUS_PAYMENT_STATE, view.present("ev_trigger", "ev_metric_all", "ev_provider_state"),
        Operation.REFRESH_PROVIDER_STATE, EvidenceLevel.SUPPORTED,
        missing=(CheckType.FETCH_PROVIDER_SNAPSHOT,), next_check=CheckType.FETCH_PROVIDER_SNAPSHOT,
        rationale=(f"{unresolved} of {attempts} mature attempts are still unresolved at the provider; "
                   f"{provider['failed_then_authorised_later']} failed payments later authorised and "
                   f"{provider['stale_payments']} have stale state. A failure is not final here: refresh before acting."),
    )


def _no_incident(view: _Packet) -> DiagnosisOutput | None:
    if view.alerts != {"orders_not_completing"}:
        return None
    processing = view.facts("ev_metric_processing")
    if int(processing["pending_captured"]) > 0 or view.failing_fixture_tests(receiver=False):  # type: ignore[arg-type]
        return None
    overall = view.facts("ev_metric_all")
    shifted = _largest_share_shift(view)
    sparse = int(overall["attempts"]) < SPARSE_MATURE_ATTEMPTS  # type: ignore[arg-type]
    if shifted is not None and abs(shifted[1]) >= MIN_MIX_SHIFT:
        method, change = shifted
        next_check, reason = CheckType.COMPARE_METHOD_COHORTS, (
            f"the {method} share of attempts moved by {change:+.2f} while within-method authorisation held")
    elif sparse:
        next_check, reason = CheckType.WAIT_FOR_MATURE_COHORT, (
            f"only {overall['attempts']} mature attempts, with payments and processing healthy")
    else:
        next_check, reason = CheckType.INSPECT_ORDER_JOURNEY, "payments and processing look healthy"
    return _output(
        Hypothesis.NO_INCIDENT, view.present("ev_trigger", "ev_metric_all", "ev_metric_processing", "ev_provider_state"),
        Operation.NO_ACTION, EvidenceLevel.INSUFFICIENT, missing=(next_check,), next_check=next_check,
        rationale=f"Only the order-completion symptom fired: {reason}. No degradation claim on this evidence.",
    )


# --------------------------------------------------------------------------- helpers


def _latest_before(view: _Packet, first_ms: int, *, prefer_file: str | None = None) -> EvidenceItem | None:
    """The latest deployment at or before ``first_ms``; within the tie window, one touching ``prefer_file``."""
    before = [item for item in view.deployments() if int(item.facts["deployed_at_ms"]) <= first_ms]  # type: ignore[arg-type]
    if not before:
        return None
    latest_at = int(before[-1].facts["deployed_at_ms"])  # type: ignore[arg-type]
    close = [
        item for item in before if latest_at - int(item.facts["deployed_at_ms"]) <= TIE_WINDOW_MS  # type: ignore[arg-type]
    ]
    touching = [
        item for item in close if prefer_file and prefer_file in item.facts.get("files_changed", [])  # type: ignore[operator]
    ]
    return (touching or close)[-1]


def _change_id(item: EvidenceItem | None) -> str:
    return str(item.facts["deploy_id"]) if item is not None else CHOICE_UNKNOWN


def _top_frame_file(trace: str | None, failing: list[Mapping[str, JsonValue]]) -> str | None:
    """The innermost merchant frame of the stack trace, else of a failing fixture result."""
    frames = _TRACE_FILE.findall(trace or "")
    if frames:
        return frames[-1]
    for result in failing:
        match = _FIXTURE_FRAME.match(str(result.get("top_frame") or ""))
        if match:
            return match.group("path")
    return None


def _decline(metric: Mapping[str, JsonValue]) -> float:
    baseline, current = metric.get("baseline_authorisation_ratio"), metric.get("authorisation_ratio")
    if baseline is None or current is None:
        return 0.0
    return float(baseline) - float(current)  # type: ignore[arg-type]


def _largest_share_shift(view: _Packet) -> tuple[str, float] | None:
    shifts = []
    for method in view.packet.candidate_methods:
        metric = view.item(f"ev_metric_{method}")
        if metric is None:
            continue
        share, baseline = metric.facts.get("share_of_attempts"), metric.facts.get("baseline_share_of_attempts")
        if share is not None and baseline is not None:
            shifts.append((method, float(share) - float(baseline)))  # type: ignore[arg-type]
    return max(shifts, key=lambda shift: abs(shift[1]), default=None)


def _compact(value: JsonValue) -> str:
    if isinstance(value, Mapping):
        return ", ".join(f"{key} {count}" for key, count in sorted(value.items()))
    return str(value)


def _output(
    hypothesis: Hypothesis,
    supporting: tuple[str, ...],
    operation: Operation,
    level: EvidenceLevel,
    *,
    missing: tuple[CheckType, ...],
    next_check: CheckType,
    rationale: str,
    change: str = CHOICE_NONE,
    method: str = CHOICE_NONE,
    contradicting: tuple[str, ...] = (),
) -> DiagnosisOutput:
    return DiagnosisOutput(
        hypothesis=hypothesis,
        faulting_change_id=change,
        affected_method=method,
        evidence_level=level,
        supporting_evidence_ids=supporting[:8],
        contradicting_evidence_ids=tuple(item for item in contradicting if item not in supporting)[:8],
        missing_checks=tuple(dict.fromkeys(missing))[:4],
        next_check=next_check,
        proposed_operation=operation,
        rationale=rationale[:800],
    )
