"""Shared vocabulary: enums, strict contracts, canonical hashing and error types.

Every module imports its cross-module types from here so the two implementation
tracks agree on names. Conventions used everywhere:

* amounts are positive integer paise (``*_paise``); rupees appear only in the UI;
* times are integer milliseconds since the Unix epoch, UTC (``*_ms``);
* every stored record belongs to a ``run_id``.

Family and variant labels (D1..D6) are oracle concepts. They live in the
simulator/evaluation code and must never appear in these runtime contracts.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Paise = Annotated[int, Field(strict=True, gt=0)]
NonNegativePaise = Annotated[int, Field(strict=True, ge=0)]
EpochMs = Annotated[int, Field(strict=True, ge=0)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
SUPPORTED_ORDER_CURRENCY = "INR"

# Values allowed in every per-call enum alongside the IDs visible in a packet.
CHOICE_NONE = "none"
CHOICE_UNKNOWN = "unknown"


class StrictModel(BaseModel):
    """Contract base: immutable and unknown fields rejected.

    Integers that carry money or time use strict annotated types above, so a
    string such as "100" is never coerced into paise or milliseconds. Enum
    fields accept their string values so JSON and plain dicts validate alike.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


def canonical_json(value: JsonValue) -> bytes:
    """The one canonical serialisation used for every hash (plans, packets, evidence).

    Sorted keys, no insignificant whitespace, ASCII only and no NaN/Infinity.
    Callers sort any list whose order is not meaningful (for example target IDs).
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode(
        "ascii"
    )


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- errors


class WorkbenchError(Exception):
    """Base class for expected, reportable failures."""


class ScopeError(WorkbenchError):
    """Access or effect outside the caller's run, merchant or integration (HTTP 403)."""


class StaleStateError(WorkbenchError):
    """Evidence, approval or plan no longer matches current state (HTTP 409)."""


class ContractError(WorkbenchError):
    """Input that violates a contract (HTTP 422)."""


# --------------------------------------------------------------------------- payment state


class PaymentStatus(StrEnum):
    UNKNOWN = "unknown"  # no usable observation; never coerced into "failed"
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class ObservationSource(StrEnum):
    WEBHOOK = "webhook"  # verified, durably received event
    SNAPSHOT = "snapshot"  # provider-state fetch by the reconciler


class PaymentObservation(StrictModel):
    """One append-only observation of provider payment state."""

    run_id: str
    observation_id: str
    payment_id: str
    order_id: str
    status: PaymentStatus
    amount_paise: Paise
    currency: CurrencyCode
    method: str | None = None
    source: ObservationSource
    source_ref: str  # inbox event ID or snapshot ID
    occurred_at_ms: EpochMs  # when the provider says the transition happened
    known_at_ms: EpochMs  # when the workbench could first see it
    amount_refunded_paise: NonNegativePaise = 0
    error_code: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None


# --------------------------------------------------------------------------- inbox and worker


class ReceiveOutcome(StrEnum):
    ACCEPTED = "accepted"  # new verified event committed; acknowledge 2xx
    DUPLICATE = "duplicate"  # same bytes already committed, under this event ID or another; 2xx, nothing new stored
    CONFLICTING_ID = "conflicting_id"  # same event ID, different bytes: stored, 2xx, never auto-processed
    REJECTED_SIGNATURE = "rejected_signature"  # 401; no business state change
    REJECTED_SCHEMA = "rejected_schema"  # 400; signed but unsupported or malformed
    REJECTED_SIZE = "rejected_size"  # 413
    UNKNOWN_INTEGRATION = "unknown_integration"  # 404


class InboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"  # bounded retries exhausted; waits for approved reprocessing
    MANUAL_REVIEW = "manual_review"  # an invariant check stopped automatic processing


class WorkerResult(StrEnum):
    FULFILLED = "fulfilled"
    ALREADY_FULFILLED = "already_fulfilled"
    NO_EFFECT = "no_effect"  # event type with no business effect, e.g. payment.failed
    FAILED = "failed"  # the merchant handler raised
    MANUAL_REVIEW = "manual_review"  # state, amount, currency or cancellation check failed


class HandlerAction(StrEnum):
    FULFIL = "fulfil"
    IGNORE = "ignore"


class HandlerOutcome(StrictModel):
    """What a merchant handler asks the worker to do with one event.

    The worker re-checks every field against the order and payment projection
    before any effect; a handler cannot force a fulfilment.
    """

    action: HandlerAction
    order_id: str | None = None
    payment_id: str | None = None
    amount_paise: Paise | None = None
    currency: CurrencyCode | None = None


class FailureReport(StrictModel):
    """How a merchant handler failure is recorded as evidence."""

    exception_category: str | None  # None when the merchant app emits only free text
    stack_trace: str  # logical repository paths only; never host paths or fixture variant names


class MerchantHandler(Protocol):
    """A deployed merchant event handler (implemented by ``simulator.merchant``)."""

    @property
    def version(self) -> str:
        """Identifier recorded on worker attempts, e.g. the active deployment ID."""
        ...

    def handle(self, event: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        """Turn one parsed event into a HandlerOutcome-shaped mapping, or raise."""
        ...

    def describe_failure(self, exc: BaseException) -> FailureReport:
        """Render a handler exception as recorded evidence."""
        ...


# --------------------------------------------------------------------------- evidence packet


class EvidenceKind(StrEnum):
    INCIDENT_TRIGGER = "incident_trigger"  # which detector fired, and which did not
    METRIC = "metric"  # cohort counts and ratios with denominators and freshness
    PROVIDER_STATE = "provider_state"  # observed payment states and snapshot freshness
    DELIVERY_HEALTH = "delivery_health"  # acknowledgements, duplicates, conflicts, rejections
    WORKER_HEALTH = "worker_health"  # backlog, backlog age, processed fraction
    WORKER_ERROR = "worker_error"  # optional exception category plus stack trace text
    DEPLOYMENT = "deployment"  # deployment record plus bounded code or config diff
    LOG = "log"  # free-text merchant or platform log line; untrusted data
    FIXTURE_CHECK = "fixture_check"  # currently deployed handler on an allowlisted fixture
    ORDER_JOURNEY = "order_journey"  # attempts and observations for one order


EvidenceId = Annotated[str, Field(pattern=r"^ev_[a-z0-9_]{1,60}$")]


class EvidenceItem(StrictModel):
    evidence_id: EvidenceId
    kind: EvidenceKind
    known_at_ms: EpochMs
    title: Annotated[str, Field(max_length=160)]
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    # Untrusted free text (log lines, stack traces, diffs). Rendered as text, never as instructions.
    text: Annotated[str, Field(max_length=4000)] | None = None


class IncidentCard(StrictModel):
    run_id: str
    incident_id: str
    symptom: str
    trigger: str
    cohort: dict[str, JsonValue]
    window_start_ms: EpochMs
    window_end_ms: EpochMs
    evidence_cutoff_ms: EpochMs
    summary_counts: dict[str, int]


class EvidencePacket(StrictModel):
    """The bounded, cutoff-filtered evidence a diagnosis may read and cite."""

    packet_version: str
    incident: IncidentCard
    items: tuple[EvidenceItem, ...]
    candidate_change_ids: tuple[str, ...]  # deployment IDs visible in this packet
    candidate_methods: tuple[str, ...]  # payment-method cohorts visible in this packet

    @model_validator(mode="after")
    def _evidence_is_bounded_by_cutoff(self) -> EvidencePacket:
        ids = [item.evidence_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence IDs must be unique within a packet")
        cutoff = self.incident.evidence_cutoff_ms
        late = [item.evidence_id for item in self.items if item.known_at_ms > cutoff]
        if late:
            raise ValueError(f"evidence known after the cutoff {cutoff} cannot enter the packet: {late}")
        reserved = {CHOICE_NONE, CHOICE_UNKNOWN} & (set(self.candidate_change_ids) | set(self.candidate_methods))
        if reserved:
            raise ValueError(f"candidate IDs may not use reserved values {sorted(reserved)}")
        return self

    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.items)

    def packet_hash(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))


# --------------------------------------------------------------------------- diagnosis


class Hypothesis(StrEnum):
    MERCHANT_WORKER_REGRESSION = "merchant_worker_regression"
    PROVIDER_METHOD_DEGRADATION = "provider_method_degradation"
    RECEIVER_REJECTING_DELIVERIES = "receiver_rejecting_deliveries"
    AMBIGUOUS_PAYMENT_STATE = "ambiguous_payment_state"
    NO_INCIDENT = "no_incident"
    UNKNOWN = "unknown"


class Operation(StrEnum):
    REPROCESS_INBOX = "reprocess_inbox"
    PREPARE_PROVIDER_REPLAY = "prepare_provider_replay"
    ESCALATE = "escalate"
    REFRESH_PROVIDER_STATE = "refresh_provider_state"  # read-only snapshot fetch
    NO_ACTION = "no_action"


class EvidenceLevel(StrEnum):
    """An evidence label, not a probability. Never display invented percentages."""

    INSUFFICIENT = "insufficient"
    SUPPORTED = "supported"
    REPRODUCED_IN_FIXTURE = "reproduced_in_fixture"


class CheckType(StrEnum):
    """Checks a careful engineer would run next; used for missing and next checks."""

    VALIDATE_CORRECTED_HANDLER = "validate_corrected_handler"
    FETCH_PROVIDER_SNAPSHOT = "fetch_provider_snapshot"
    INSPECT_WORKER_ERRORS = "inspect_worker_errors"
    INSPECT_RECENT_CHANGES = "inspect_recent_changes"
    INSPECT_DELIVERY_HEALTH = "inspect_delivery_health"
    INSPECT_ORDER_JOURNEY = "inspect_order_journey"
    COMPARE_METHOD_COHORTS = "compare_method_cohorts"
    WAIT_FOR_MATURE_COHORT = "wait_for_mature_cohort"
    NONE = "none"


class DiagnosisOutput(StrictModel):
    """Structured diagnosis returned by the model or the rules baseline.

    Per-call constraints (cited IDs visible in the packet, candidate change and
    method IDs) are enforced by diagnosis.py against the packet; this static
    contract only fixes the shape. The model has no field that approves anything.
    """

    hypothesis: Hypothesis
    faulting_change_id: str  # a packet candidate_change_id, "none" or "unknown"
    affected_method: str  # a packet candidate_method, "none" or "unknown"
    evidence_level: EvidenceLevel
    supporting_evidence_ids: Annotated[tuple[str, ...], Field(max_length=8)]
    contradicting_evidence_ids: Annotated[tuple[str, ...], Field(max_length=8)]
    missing_checks: Annotated[tuple[CheckType, ...], Field(max_length=4)]
    next_check: CheckType
    proposed_operation: Operation
    rationale: Annotated[str, Field(max_length=800)]


class ModelMode(StrEnum):
    LOCAL = "local"  # fresh inference against local Ollama
    RECORDED = "recorded"  # replay of stored model output; always labelled as recorded
    RULES = "rules"  # deterministic rules baseline; no model
    HOSTED = "hosted"  # fresh inference by a hosted OpenAI-compatible API; never labelled local; outside M4


class DiagnosisStatus(StrEnum):
    VALIDATED = "validated"  # passed schema and server checks; still only a proposal for a human
    NEEDS_REVIEW = "needs_review"  # model failed, timed out or stayed invalid; deterministic evidence only


# --------------------------------------------------------------------------- plans and actions


class PlanStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    EXECUTED = "executed"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class ActionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"  # a precondition failed inside the write transaction; nothing changed
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"  # reserved for future external adapters


class ExclusionReason(StrEnum):
    """Why an order is not an automatic recovery target."""

    ALREADY_FULFILLED = "already_fulfilled"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    MULTIPLE_CAPTURES = "multiple_captures"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    UNRESOLVED_ATTEMPT = "unresolved_attempt"
    NOT_CAPTURED = "not_captured"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    STALE_SNAPSHOT = "stale_snapshot"
    UNKNOWN_ORDER = "unknown_order"


# --------------------------------------------------------------------------- order recovery queue (order_cases.py)


class CaseState(StrEnum):
    """Exactly one per order. Declared in precedence order; ``order_cases.STATE_PRECEDENCE`` holds the conditions."""

    PAYMENT_ORDER_MISMATCH = "payment_order_mismatch"
    MULTIPLE_CAPTURES = "multiple_captures"
    BUSINESS_STATE_CONFLICT = "business_state_conflict"
    PAYMENT_STATE_UNRESOLVED = "payment_state_unresolved"
    CAPTURE_NOT_CONFIRMED = "capture_not_confirmed"
    COMPLETE = "complete"
    CAPTURED_PROCESSING_CLOSED = "captured_processing_closed"
    NOTIFICATION_ACKNOWLEDGED_NOT_STORED = "notification_acknowledged_not_stored"
    NOTIFICATION_NOT_ACCEPTED = "notification_not_accepted"
    CAPTURED_PROCESSING_INCOMPLETE = "captured_processing_incomplete"
    PAYMENT_FAILED = "payment_failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class NextStep(StrEnum):
    """The permissible next step. Only ``validate_fix_then_reprocess`` ends in an effect the workbench applies itself."""

    MANUAL_REVIEW = "manual_review"  # a person decides; plans.escalate records it
    REFRESH_THEN_REEVALUATE = "refresh_then_reevaluate"  # read-only provider refresh, then classify again
    HOLD = "hold"  # no fulfilment until a capture is confirmed
    PREPARE_PROVIDER_REPLAY = "prepare_provider_replay"  # instructions only; no provider call
    VALIDATE_FIX_THEN_REPROCESS = "validate_fix_then_reprocess"  # fix validation, then an approved reprocess plan
    NONE = "none"


class CaseBlocker(StrEnum):
    """Blockers beyond the projection's exclusion reasons and the planner's target check."""

    # Capture evidence exists, but no payment.captured event naming the order was durably received.
    CAPTURE_EVENT_NOT_RECEIVED = "capture_event_not_received"
    # As above, and the provider's delivery log, read by a refresh, shows a 2xx answer to the payment.captured event of
    # the order's capture: the receiver acknowledged it and never stored it, so the provider will not resend it (Q29).
    CAPTURE_EVENT_ACKNOWLEDGED_NOT_STORED = "capture_event_acknowledged_not_stored"
    # Every captured event of an unfulfilled order was settled by the worker without a fulfilment (held for
    # review, or processed with no effect), so neither the worker nor a reprocess plan will pick it up again.
    CAPTURE_EVENT_SETTLED_WITHOUT_FULFILMENT = "capture_event_settled_without_fulfilment"


class AmountStatus(StrEnum):
    """Whether an order's amount may enter asserted totals; anything else is counted, never summed."""

    ASSERTED = "asserted"  # the order's own amount, in the totals' currency, and no evidence disagrees with it
    CONFLICTING = "conflicting"  # a capture, an observation or a held redelivery disagrees in amount or currency
    UNKNOWN = "unknown"  # no order record, so no amount to assert
    OTHER_CURRENCY = "other_currency"  # consistent, but not in the currency the totals are summed in
