"""Per-order recovery queue (phase 11): one case state, every blocker and the next step for each order.

The queue is a pure classification at replay time T over records the workbench already keeps, and it
reads only what was known by T: payment observations known and occurring by T, orders and payment
attempts created by T, inbox rows received by T, worker attempts finished by T, deliveries and
conflicting deliveries received by T, fulfilments made by T, and the provider's delivery attempts made
by T and read by a refresh by T. An inbox row is judged by its worker attempts, never by its mutable
``processing_status`` column. The one undated input, the order's ``refunded`` flag, is read only when T
is at or after the run's replay clock (``OrderQueue.refund_flag_read``).

It builds on ``state.OrderProjection``: every exclusion the projection gives an order is one of its
blockers, and the reprocess target is chosen and checked as ``plans.build_reprocess_plan`` does
(earliest unsettled ``payment.captured`` event; ``policy.PlanExclusion.TARGET_NOT_THE_CAPTURE``). So an
order the planner would target is here ``captured_processing_incomplete`` with no blocker, and an order
it excludes carries the planner's reason. The one exception is a failed dry run of the corrected handler:
that is a property of the fix, not of the evidence, so such an order still reads as eligible here.
Nothing here is written, and nothing reads the simulator's oracle, family or variant labels.

**Population.** Every order created by T, plus every order ID that payment evidence visible at T names
but the orders table does not hold (``order_known`` false, blocker ``unknown_order``).

**State precedence** (``STATE_PRECEDENCE``, first match wins). Conditions that need a person whatever
the provider says come first (mismatch, multiple captures, cancellation or refund); then conditions a
provider refresh may settle (contradictions, stale or open payment state, an unresolved extra attempt);
then the states that follow from clean evidence. Every blocker is recorded whichever state wins.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import model_validator

from recovery_workbench import ingestion, metrics, policy, state, storage
from recovery_workbench.contracts import (
    SUPPORTED_ORDER_CURRENCY,
    AmountStatus,
    CaseBlocker,
    CaseState,
    ContractError,
    ExclusionReason,
    NextStep,
    ObservationSource,
    PaymentStatus,
    StrictModel,
    WorkerResult,
)

# Bumped whenever a state, a blocker or the precedence changes, so a stored queue says which rules made it.
# v2 (owner decision Q29 (b)): notification_acknowledged_not_stored and its blocker.
CLASSIFIER_VERSION = "cases-v2"


NEXT_STEP: Mapping[CaseState, NextStep] = MappingProxyType(
    {
        CaseState.PAYMENT_ORDER_MISMATCH: NextStep.MANUAL_REVIEW,
        CaseState.MULTIPLE_CAPTURES: NextStep.MANUAL_REVIEW,
        CaseState.BUSINESS_STATE_CONFLICT: NextStep.MANUAL_REVIEW,
        CaseState.PAYMENT_STATE_UNRESOLVED: NextStep.REFRESH_THEN_REEVALUATE,
        CaseState.CAPTURE_NOT_CONFIRMED: NextStep.HOLD,
        CaseState.COMPLETE: NextStep.NONE,
        CaseState.CAPTURED_PROCESSING_CLOSED: NextStep.MANUAL_REVIEW,
        # The provider refuses to resend an event it recorded as answered 2xx, so only a person can recover it.
        CaseState.NOTIFICATION_ACKNOWLEDGED_NOT_STORED: NextStep.MANUAL_REVIEW,
        CaseState.NOTIFICATION_NOT_ACCEPTED: NextStep.PREPARE_PROVIDER_REPLAY,
        CaseState.CAPTURED_PROCESSING_INCOMPLETE: NextStep.VALIDATE_FIX_THEN_REPROCESS,
        CaseState.PAYMENT_FAILED: NextStep.NONE,
        CaseState.INSUFFICIENT_EVIDENCE: NextStep.NONE,
    }
)

# States whose orders still need an action or a hold; the unresolved value is summed over these.
UNRESOLVED_STATES: frozenset[CaseState] = frozenset(
    set(CaseState) - {CaseState.COMPLETE, CaseState.PAYMENT_FAILED, CaseState.INSUFFICIENT_EVIDENCE}
)


TARGET_NOT_THE_CAPTURE = policy.PlanExclusion.TARGET_NOT_THE_CAPTURE.value
# Every value a blocker can take, in the order a case lists them.
BLOCKER_ORDER: tuple[str, ...] = (
    *(reason.value for reason in ExclusionReason),
    TARGET_NOT_THE_CAPTURE,
    *(blocker.value for blocker in CaseBlocker),
)


_OPEN_STATUSES = frozenset({PaymentStatus.UNKNOWN, PaymentStatus.CREATED, PaymentStatus.AUTHORIZED})
_MISMATCH_BLOCKERS = frozenset(
    {ExclusionReason.UNKNOWN_ORDER.value, ExclusionReason.AMOUNT_MISMATCH.value, ExclusionReason.CURRENCY_MISMATCH.value}
)
_BUSINESS_STATE_BLOCKERS = frozenset({ExclusionReason.CANCELLED.value, ExclusionReason.REFUNDED.value})
_CONTRADICTION_BLOCKERS = frozenset({ExclusionReason.CONFLICTING_EVIDENCE.value, TARGET_NOT_THE_CAPTURE})
_AMOUNT_CONFLICTS = frozenset({state.ConflictReason.AMOUNT_DISAGREEMENT, state.ConflictReason.CURRENCY_DISAGREEMENT})
_MONEY_FIELDS = frozenset({"amount", "currency"})


# --------------------------------------------------------------------------- state precedence


@dataclass(frozen=True)
class CaseFacts:
    """What the state choice reads about one order, all as of T."""

    blockers: frozenset[str]
    has_capture: bool  # a payment with capture evidence (captured or refunded)
    fulfilled: bool
    payment_statuses: tuple[PaymentStatus, ...]
    # An open payment (unknown, created or authorised) whose newest evidence, an observation or its attempt
    # row, is older than the freshness threshold. A final status (captured, failed, refunded) is old, not stale.
    stale_open_payment: bool
    unsettled_capture_event: bool


@dataclass(frozen=True)
class PrecedenceRule:
    state: CaseState
    condition: str  # the rule in words, for the report and the queue's readers
    applies: Callable[[CaseFacts], bool]


def _mismatch(facts: CaseFacts) -> bool:
    return not facts.blockers.isdisjoint(_MISMATCH_BLOCKERS)


def _multiple_captures(facts: CaseFacts) -> bool:
    return ExclusionReason.MULTIPLE_CAPTURES.value in facts.blockers


def _business_state_conflict(facts: CaseFacts) -> bool:
    cancelled_or_refunded = not facts.blockers.isdisjoint(_BUSINESS_STATE_BLOCKERS)
    return (facts.has_capture and cancelled_or_refunded) or (facts.fulfilled and not facts.has_capture)


def _payment_state_unresolved(facts: CaseFacts) -> bool:
    if not facts.blockers.isdisjoint(_CONTRADICTION_BLOCKERS):
        return True
    if facts.has_capture:
        # Staleness cannot reopen a fulfilled order: capture evidence is never erased, and nothing is pending.
        stale = not facts.fulfilled and ExclusionReason.STALE_SNAPSHOT.value in facts.blockers
        return stale or ExclusionReason.UNRESOLVED_ATTEMPT.value in facts.blockers
    return facts.stale_open_payment


def _capture_not_confirmed(facts: CaseFacts) -> bool:
    return not facts.has_capture and PaymentStatus.AUTHORIZED in facts.payment_statuses


def _complete(facts: CaseFacts) -> bool:
    return facts.fulfilled


def _processing_closed(facts: CaseFacts) -> bool:
    return CaseBlocker.CAPTURE_EVENT_SETTLED_WITHOUT_FULFILMENT.value in facts.blockers


def _notification_acknowledged_not_stored(facts: CaseFacts) -> bool:
    return CaseBlocker.CAPTURE_EVENT_ACKNOWLEDGED_NOT_STORED.value in facts.blockers


def _notification_not_accepted(facts: CaseFacts) -> bool:
    return CaseBlocker.CAPTURE_EVENT_NOT_RECEIVED.value in facts.blockers


def _processing_incomplete(facts: CaseFacts) -> bool:
    # "No blocker" is not part of the condition: every blocker a captured order can carry belongs to an earlier
    # state, so precedence guarantees it, and choose_state refuses an order that reaches here with one.
    return facts.has_capture and facts.unsettled_capture_event


def _payment_failed(facts: CaseFacts) -> bool:
    statuses = facts.payment_statuses
    return not facts.has_capture and bool(statuses) and all(status is PaymentStatus.FAILED for status in statuses)


def _insufficient_evidence(facts: CaseFacts) -> bool:
    return not facts.has_capture


STATE_PRECEDENCE: tuple[PrecedenceRule, ...] = (
    PrecedenceRule(
        CaseState.PAYMENT_ORDER_MISMATCH,
        "payment evidence names an order the merchant has no record of, or a capture differs from the order in "
        "amount or currency",
        _mismatch,
    ),
    PrecedenceRule(CaseState.MULTIPLE_CAPTURES, "more than one payment of the order has capture evidence", _multiple_captures),
    PrecedenceRule(
        CaseState.BUSINESS_STATE_CONFLICT,
        "captured, but the order is cancelled or refund activity is observed; or fulfilled with no capture evidence",
        _business_state_conflict,
    ),
    PrecedenceRule(
        CaseState.PAYMENT_STATE_UNRESOLVED,
        "contradictory evidence (conflicting observations or deliveries, or a captured event for a payment that is "
        "not the capture); or, beside a capture, an unresolved extra attempt or stale provider state of an unfulfilled "
        "order; or, with no capture, an open payment whose newest evidence is stale",
        _payment_state_unresolved,
    ),
    PrecedenceRule(CaseState.CAPTURE_NOT_CONFIRMED, "a payment is authorised and none is captured", _capture_not_confirmed),
    PrecedenceRule(CaseState.COMPLETE, "fulfilled, with one clean capture", _complete),
    PrecedenceRule(
        CaseState.CAPTURED_PROCESSING_CLOSED,
        "captured and unfulfilled, and the worker settled every captured event without a fulfilment",
        _processing_closed,
    ),
    PrecedenceRule(
        CaseState.NOTIFICATION_ACKNOWLEDGED_NOT_STORED,
        "captured at the provider and no payment.captured event naming the order was durably received, but the "
        "provider's delivery log, read by a refresh, shows a 2xx answer to that capture's payment.captured event: the "
        "receiver acknowledged it without storing it, and the provider will not resend it",
        _notification_acknowledged_not_stored,
    ),
    PrecedenceRule(
        CaseState.NOTIFICATION_NOT_ACCEPTED,
        "captured at the provider, but no payment.captured event naming the order was durably received",
        _notification_not_accepted,
    ),
    PrecedenceRule(
        CaseState.CAPTURED_PROCESSING_INCOMPLETE,
        "captured, with a durable captured event the worker has not settled (pending or failed); every earlier "
        "state is ruled out, so the order has one clean capture and no blocker",
        _processing_incomplete,
    ),
    PrecedenceRule(CaseState.PAYMENT_FAILED, "every payment attempt failed, and none is open", _payment_failed),
    PrecedenceRule(
        CaseState.INSUFFICIENT_EVIDENCE,
        "no capture and nothing above: no payment at all, or open payments whose evidence is still fresh",
        _insufficient_evidence,
    ),
)


def choose_state(facts: CaseFacts) -> CaseState:
    """The first state in ``STATE_PRECEDENCE`` whose condition holds."""
    chosen = next((rule.state for rule in STATE_PRECEDENCE if rule.applies(facts)), None)
    # Either gap means a blocker no rule names, such as a new exclusion reason: refuse rather than call it eligible.
    if chosen is None or (chosen is CaseState.CAPTURED_PROCESSING_INCOMPLETE and facts.blockers):
        raise ContractError(
            f"no case state fits blockers {sorted(facts.blockers)} on a captured order; "
            "add a precedence rule for the new blocker to order_cases.STATE_PRECEDENCE"
        )
    return chosen


# --------------------------------------------------------------------------- the records


class PaymentEvidence(StrictModel):
    """One payment of the order as the projection sees it at T."""

    payment_id: str
    status: PaymentStatus
    amount_paise: int | None  # the amount every visible observation agrees on; None if none or they disagree
    currency: str | None
    attempt_order_id: str | None  # the order the merchant's attempt row names; None without an attempt row
    late_authorization: bool
    conflict_reasons: tuple[str, ...]
    last_known_at_ms: int | None
    stale: bool  # no observation within the freshness threshold (PaymentProjection.stale)
    webhook_observations: int
    snapshot_observations: int


class CaptureProcessing(StrictModel):
    """The order's durable payment.captured events and what the worker did with them by T."""

    captured_inbox_ids: tuple[int, ...]  # oldest received first
    unsettled_inbox_ids: tuple[int, ...]  # no settling worker attempt by T: pending, retrying or failed
    target_inbox_id: int | None  # the earliest unsettled one, which a reprocess plan would choose
    worker_failures: int  # failed worker attempts on the captured events, finished by T
    settled_results: tuple[WorkerResult, ...]  # the result that settled each settled captured event


class EvidenceFreshness(StrictModel):
    newest_observation_at_ms: int | None  # newest provider observation of any of the order's payments
    age_ms: int | None  # T minus newest_observation_at_ms
    stale_payment_ids: tuple[str, ...]  # PaymentProjection.stale: the projection's own rule, any status
    stale_open_payment_ids: tuple[str, ...]  # open payments whose newest evidence (observation or attempt) is stale


class EvidenceCoverage(StrictModel):
    """Which sources the case had, as counts and IDs; never a score."""

    payments_with_attempt_row: int
    payments_seen_by_webhook: int
    payments_seen_by_snapshot: int
    unobserved_payment_ids: tuple[str, ...]  # no observation that carries a status
    captured_events_received: int


class EvidenceRefs(StrictModel):
    """The existing records the case relied on, by their own IDs."""

    payment_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    inbox_ids: tuple[int, ...]  # every durable event naming the order, any type
    worker_attempt_ids: tuple[int, ...]
    delivery_attempt_ids: tuple[int, ...]
    conflicting_delivery_ids: tuple[int, ...]


class OrderCase(StrictModel):
    order_id: str
    order_known: bool  # false when only payment evidence names the order
    amount_paise: int | None  # the order's amount; None without an order record
    currency: str | None
    amount_status: AmountStatus
    state: CaseState
    next_step: NextStep
    blockers: tuple[str, ...]  # ExclusionReason values, target_not_the_capture and CaseBlocker values, in BLOCKER_ORDER
    payments: tuple[PaymentEvidence, ...]
    capture_processing: CaptureProcessing
    fulfilment: storage.FulfilmentRecord | None
    freshness: EvidenceFreshness
    coverage: EvidenceCoverage
    evidence: EvidenceRefs

    @model_validator(mode="after")
    def _consistent(self) -> OrderCase:
        has_amount = self.amount_paise is not None and self.currency is not None
        if self.order_known != has_amount or self.order_known == (self.amount_status is AmountStatus.UNKNOWN):
            raise ValueError("an order record, its amount and currency, and a known amount status come together")
        if self.next_step is not NEXT_STEP[self.state]:
            raise ValueError(f"state {self.state.value} takes next step {NEXT_STEP[self.state].value}")
        if self.state is CaseState.CAPTURED_PROCESSING_INCOMPLETE and self.blockers:
            raise ValueError("an order with a blocker is never captured_processing_incomplete")
        return self


class StateTotal(StrictModel):
    state: CaseState
    orders: int
    asserted_value_paise: int
    unasserted_orders: int  # amount unknown, conflicting or in another currency: counted, never summed


class QueueTotals(StrictModel):
    """Integer paise in ``currency`` only. Restored processing is never new money; nothing here is netted."""

    currency: str
    captured_unprocessed_orders: int  # unique orders eligible for a reprocess plan
    captured_unprocessed_value_paise: int
    unresolved_orders: int  # orders in UNRESOLVED_STATES
    unresolved_value_paise: int
    unresolved_unasserted_orders: int
    by_state: tuple[StateTotal, ...]  # every state in precedence order, zeros included


class QueueCoverage(StrictModel):
    """What the whole classification read, as of T."""

    orders: int
    unknown_orders: int
    payments: int
    observations: int
    inbox_rows: int
    worker_attempts: int
    delivery_attempts: int
    rejected_deliveries: int
    # Delivery attempts whose event ID has no inbox row: unverifiable, rejected or never stored, and, since Q32 (a)
    # and Q34, acknowledged duplicates whose body is already stored under another event ID or held as a conflicting
    # delivery. Context for notification_not_accepted, not a count of lost deliveries.
    deliveries_without_inbox_row: int
    newest_observation_at_ms: int | None


class OrderQueue(StrictModel):
    run_id: str
    as_of_ms: int
    stale_after_ms: int
    classifier_version: str
    # The orders table's ``refunded`` column says a refund happened, not when. It is read only when as_of_ms is
    # at or after the run's replay clock, when every flag in the table was set by then. At an earlier time it may
    # have been set later, so it is not read, and only dated refund evidence (a refunded observation) counts.
    refund_flag_read: bool
    cases: tuple[OrderCase, ...]  # exactly one per order, sorted by order ID
    totals: QueueTotals
    coverage: QueueCoverage


# --------------------------------------------------------------------------- classification


@dataclass(frozen=True)
class _InboxEvent:
    """A durable inbox row with the payment, order, amount and currency its verified body names."""

    row: storage.InboxRecord
    payment_id: str
    order_id: str
    amount_paise: int
    currency: str

    @property
    def is_capture(self) -> bool:
        return self.row.event_type == policy.CAPTURE_EVENT_TYPE


@dataclass(frozen=True)
class _RunContext:
    view: metrics.RunView
    stale_after_ms: int
    refund_flag_read: bool
    conflicts: Mapping[int, storage.ConflictingDeliveryRecord]
    # Built once per classification, as metrics._project_orders does for the orders the run holds: each signed delivery
    # may name a fresh order, and a scan of the run's payments and conflicts per unknown order made the queue quadratic.
    payments_by_named_order: Mapping[str, tuple[state.PaymentProjection, ...]]  # under each order their history names
    conflicts_by_order: Mapping[str, tuple[storage.ConflictingDeliveryRecord, ...]]  # under either side's order
    conflicts_by_payment: Mapping[str, tuple[storage.ConflictingDeliveryRecord, ...]]  # under either side's payment
    events_by_order: Mapping[str, tuple[_InboxEvent, ...]]
    attempts_by_payment: Mapping[str, storage.PaymentAttemptRecord]
    worker_attempts_by_inbox: Mapping[int, tuple[storage.WorkerAttemptRecord, ...]]
    deliveries_by_event: Mapping[tuple[str, str], tuple[storage.DeliveryAttemptRecord, ...]]
    # Payments whose payment.captured event the provider's delivery log shows answered 2xx, with no inbox row for it.
    acknowledged_unstored_captures: frozenset[str]


def classify_orders(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int, stale_after_ms: int
) -> OrderQueue:
    """The recovery queue of ``run_id`` at replay time ``as_of_ms``: one case per order, with totals.

    ``stale_after_ms`` must be the freshness threshold a reprocess plan uses (``WORKBENCH_STALE_AFTER_SECONDS``),
    or the two disagree about stale orders. Reads one snapshot and writes nothing. Raises ``ScopeError`` for
    an unknown run and ``ContractError`` for an invalid time.
    """
    _require_ms("as_of_ms", as_of_ms, minimum=0)
    _require_ms("stale_after_ms", stale_after_ms, minimum=1)
    with storage.read_transaction(conn):
        run = storage.require_run(conn, run_id)
        view = metrics.load_run_view(conn, run_id, as_of_ms, stale_after_ms=stale_after_ms)
        conflicts = storage.list_conflicting_deliveries(conn, run_id=run_id, as_of_ms=as_of_ms)
        acknowledged = storage.acknowledged_unstored_payment_ids(conn, run_id=run_id, as_of_ms=as_of_ms,
                                                                 event_type=policy.CAPTURE_EVENT_TYPE)
    context = _RunContext(
        view=view,
        stale_after_ms=stale_after_ms,
        refund_flag_read=as_of_ms >= run.clock_ms,
        conflicts={conflict.id: conflict for conflict in conflicts},
        payments_by_named_order=_grouped_under_each(
            view.payments.values(), lambda payment: [item.order_id for item in payment.history]),
        conflicts_by_order=_grouped_under_each(
            conflicts, lambda conflict: (conflict.order_id, conflict.original_order_id)),
        conflicts_by_payment=_grouped_under_each(
            conflicts, lambda conflict: (conflict.payment_id, conflict.original_payment_id)),
        events_by_order=_events_by_order(view.inbox),
        attempts_by_payment={attempt.payment_id: attempt for attempt in view.attempts},
        worker_attempts_by_inbox=_grouped(view.worker_attempts, lambda attempt: attempt.inbox_id),
        deliveries_by_event=_grouped(
            (delivery for delivery in view.deliveries if delivery.event_id is not None),
            lambda delivery: (delivery.integration_id, str(delivery.event_id)),
        ),
        acknowledged_unstored_captures=acknowledged,
    )
    unknown_ids = _unknown_order_ids(context)
    cases = [_order_case(context, order.order_id, order, _known_order_projection(context, order))
             for order in view.orders.values()]
    cases.extend(_order_case(context, order_id, None, _unknown_order_projection(context, order_id))
                 for order_id in unknown_ids)
    ordered = tuple(sorted(cases, key=lambda case: case.order_id))
    return OrderQueue(
        run_id=run_id,
        as_of_ms=as_of_ms,
        stale_after_ms=stale_after_ms,
        classifier_version=CLASSIFIER_VERSION,
        refund_flag_read=context.refund_flag_read,
        cases=ordered,
        totals=queue_totals(ordered),
        coverage=_queue_coverage(context, unknown_orders=len(unknown_ids)),
    )


def queue_totals(cases: Sequence[OrderCase]) -> QueueTotals:
    """Asserted totals per state, summed in the supported currency; other amounts are only counted."""
    by_state = tuple(_state_total(state_value, cases) for state_value in CaseState)
    eligible = [case for case in cases if case.state is CaseState.CAPTURED_PROCESSING_INCOMPLETE]
    unresolved = [total for total in by_state if total.state in UNRESOLVED_STATES]
    return QueueTotals(
        currency=SUPPORTED_ORDER_CURRENCY,
        captured_unprocessed_orders=len(eligible),
        captured_unprocessed_value_paise=_asserted_paise(eligible),
        unresolved_orders=sum(total.orders for total in unresolved),
        unresolved_value_paise=sum(total.asserted_value_paise for total in unresolved),
        unresolved_unasserted_orders=sum(total.unasserted_orders for total in unresolved),
        by_state=by_state,
    )


def _state_total(state_value: CaseState, cases: Sequence[OrderCase]) -> StateTotal:
    members = [case for case in cases if case.state is state_value]
    asserted = [case for case in members if case.amount_status is AmountStatus.ASSERTED]
    return StateTotal(
        state=state_value, orders=len(members), asserted_value_paise=_asserted_paise(asserted),
        unasserted_orders=len(members) - len(asserted),
    )


def _asserted_paise(cases: Iterable[OrderCase]) -> int:
    total = 0
    for case in cases:
        if case.amount_status is not AmountStatus.ASSERTED:
            continue
        if case.amount_paise is None:  # OrderCase's validator ties an amount to every order record
            raise ContractError(f"order {case.order_id!r} is asserted but has no amount")
        total += case.amount_paise
    return total


def _require_ms(name: str, value: int, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{name} must be an integer of milliseconds no smaller than {minimum}, got {value!r}")


def _grouped[Key, Item](items: Iterable[Item], key: Callable[[Item], Key]) -> dict[Key, tuple[Item, ...]]:
    grouped: defaultdict[Key, list[Item]] = defaultdict(list)
    for item in items:
        grouped[key(item)].append(item)
    return {group: tuple(members) for group, members in grouped.items()}


def _grouped_under_each[Key, Item](
    items: Iterable[Item], keys: Callable[[Item], Iterable[Key]]
) -> dict[Key, tuple[Item, ...]]:
    """As ``_grouped``, but an item goes under each key it names, once per key, in the items' order."""
    grouped: defaultdict[Key, list[Item]] = defaultdict(list)
    for item in items:
        for key in dict.fromkeys(keys(item)):
            grouped[key].append(item)
    return {group: tuple(members) for group, members in grouped.items()}


def _events_by_order(rows: Iterable[storage.InboxRecord]) -> dict[str, tuple[_InboxEvent, ...]]:
    """Each durable event under the order its verified body names, oldest received first (the planner's order)."""
    events: list[_InboxEvent] = []
    for row in rows:
        try:
            payment = ingestion.parse_payment_event(row.raw_body).payment
        except ContractError as unreadable:
            raise ContractError(
                f"inbox row {row.inbox_id} of run {row.run_id!r} no longer parses ({unreadable}); it was verified on "
                "receipt, so its stored body has changed since. Restore the row before building the queue"
            ) from unreadable
        events.append(_InboxEvent(row=row, payment_id=payment.id, order_id=payment.order_id,
                                  amount_paise=payment.amount, currency=payment.currency))
    events.sort(key=lambda event: (event.row.received_at_ms, event.row.inbox_id))
    return _grouped(events, lambda event: event.order_id)


def _unknown_order_ids(context: _RunContext) -> list[str]:
    """Order IDs that visible payment evidence or a durable event names, but no order created by T holds."""
    view = context.view
    named = {item.order_id for payment in view.payments.values() for item in payment.history}
    named |= set(context.events_by_order)
    return sorted(named - set(view.orders))


def _project(
    context: _RunContext,
    order_id: str,
    order: storage.OrderRecord | None,
    payments: Sequence[state.PaymentProjection],
    conflicting_delivery_ids: Sequence[int],
    fulfilment: storage.FulfilmentRecord | None,
) -> state.OrderProjection:
    return state.project_order(
        run_id=context.view.run_id,
        order_id=order_id,
        as_of_ms=context.view.as_of_ms,
        order=order,
        attempts=[context.attempts_by_payment[str(p.payment_id)] for p in payments
                  if str(p.payment_id) in context.attempts_by_payment],
        payments=payments,
        conflicting_delivery_ids=conflicting_delivery_ids,
        fulfilment=fulfilment,
    )


def _unknown_order_projection(context: _RunContext, order_id: str) -> state.OrderProjection:
    """As ``state.load_order_projection`` reads an order the orders table lacks: the payments naming it."""
    payments = context.payments_by_named_order.get(order_id, ())
    held = [*context.conflicts_by_order.get(order_id, ()),
            *(conflict for payment in payments
              for conflict in context.conflicts_by_payment.get(str(payment.payment_id), ()))]
    conflict_ids = sorted({conflict.id for conflict in held})
    return _project(context, order_id, None, payments, conflict_ids, context.view.fulfilments.get(order_id))


def _known_order_projection(context: _RunContext, order: storage.OrderRecord) -> state.OrderProjection:
    projection = context.view.order_projections[order.order_id]
    if not order.refunded or context.refund_flag_read:
        return projection
    # Before the run's clock the flag may have been set after T, so the order is projected as if it were unset.
    undated = order.model_copy(update={"refunded": False})
    return _project(context, order.order_id, undated, projection.payments, projection.conflicting_delivery_ids,
                    projection.fulfilment)


def _order_case(
    context: _RunContext, order_id: str, order: storage.OrderRecord | None, projection: state.OrderProjection
) -> OrderCase:
    events = context.events_by_order.get(order_id, ())
    captured = tuple(event for event in events if event.is_capture)
    unsettled = tuple(event for event in captured if event.row.inbox_id not in context.view.settled)
    blockers = _blockers(projection, captured, unsettled, context.acknowledged_unstored_captures)
    stale_open = _stale_open_payment_ids(context, projection.payments)
    facts = CaseFacts(
        blockers=frozenset(blockers),
        has_capture=bool(projection.captured_payments),
        fulfilled=projection.fulfilment is not None,
        payment_statuses=tuple(payment.status for payment in projection.payments),
        stale_open_payment=bool(stale_open),
        unsettled_capture_event=bool(unsettled),
    )
    chosen = choose_state(facts)
    return OrderCase(
        order_id=order_id,
        order_known=order is not None,
        amount_paise=None if order is None else order.amount_paise,
        currency=None if order is None else order.currency,
        amount_status=_amount_status(context, order, blockers, projection),
        state=chosen,
        next_step=NEXT_STEP[chosen],
        blockers=blockers,
        payments=tuple(_payment_evidence(context, payment) for payment in projection.payments),
        capture_processing=_capture_processing(context, captured, unsettled),
        fulfilment=projection.fulfilment,
        freshness=_freshness(context, projection, stale_open),
        coverage=_coverage(context, projection, captured),
        evidence=_evidence_refs(context, projection, events),
    )


def _blockers(
    projection: state.OrderProjection, captured: Sequence[_InboxEvent], unsettled: Sequence[_InboxEvent],
    acknowledged_unstored: frozenset[str],
) -> tuple[str, ...]:
    found = {reason.value for reason in projection.exclusions}
    single_capture = projection.single_captured_payment_id
    # The planner's own checks on the event it would reprocess (policy.assess_target).
    if unsettled and single_capture is not None and single_capture != unsettled[0].payment_id:
        found.add(TARGET_NOT_THE_CAPTURE)
    if unsettled:
        target = unsettled[0]
        found |= {reason.value for reason in policy.mismatches_with_order(target.amount_paise, target.currency,
                                                                          projection.order)}
    has_capture = bool(projection.captured_payments)
    if has_capture and not captured:
        found.add(CaseBlocker.CAPTURE_EVENT_NOT_RECEIVED.value)
        # The replay plan holds the same captures (plans._replay_hold_reasons), from the same query.
        if any(str(payment.payment_id) in acknowledged_unstored for payment in projection.captured_payments):
            found.add(CaseBlocker.CAPTURE_EVENT_ACKNOWLEDGED_NOT_STORED.value)
    if has_capture and captured and not unsettled and projection.fulfilment is None:
        found.add(CaseBlocker.CAPTURE_EVENT_SETTLED_WITHOUT_FULFILMENT.value)
    return tuple(value for value in BLOCKER_ORDER if value in found)


def _newest_evidence_at_ms(context: _RunContext, payment: state.PaymentProjection) -> int | None:
    """The newest thing the workbench knows of the payment: an observation, or the merchant's attempt row."""
    times = [item.known_at_ms for item in payment.history]
    attempt = context.attempts_by_payment.get(str(payment.payment_id))
    if attempt is not None:
        times.append(attempt.created_at_ms)
    return max(times, default=None)


def _stale_open_payment_ids(context: _RunContext, payments: Sequence[state.PaymentProjection]) -> tuple[str, ...]:
    """Open payments the provider has said nothing new about for longer than the threshold.

    An unobserved attempt younger than the threshold is still within the time the provider has to report it,
    so it is not stale here, though ``PaymentProjection.stale`` (no observation at all) already says it is.
    """
    as_of_ms = context.view.as_of_ms
    stale = []
    for payment in payments:
        if payment.status not in _OPEN_STATUSES:
            continue
        newest = _newest_evidence_at_ms(context, payment)
        if newest is None or as_of_ms - newest > context.stale_after_ms:
            stale.append(str(payment.payment_id))
    return tuple(sorted(stale))


def _amount_status(
    context: _RunContext, order: storage.OrderRecord | None, blockers: Sequence[str], projection: state.OrderProjection
) -> AmountStatus:
    if order is None:
        return AmountStatus.UNKNOWN
    if not _MISMATCH_BLOCKERS.isdisjoint(blockers):
        return AmountStatus.CONFLICTING
    if any(not _AMOUNT_CONFLICTS.isdisjoint(payment.conflict_reasons) for payment in projection.payments):
        return AmountStatus.CONFLICTING
    held = (context.conflicts[conflict_id] for conflict_id in projection.conflicting_delivery_ids)
    if any(not _MONEY_FIELDS.isdisjoint(conflict.differing_fields) for conflict in held):
        return AmountStatus.CONFLICTING
    if order.currency != SUPPORTED_ORDER_CURRENCY:
        return AmountStatus.OTHER_CURRENCY
    return AmountStatus.ASSERTED


def _payment_evidence(context: _RunContext, payment: state.PaymentProjection) -> PaymentEvidence:
    attempt = context.attempts_by_payment.get(str(payment.payment_id))
    return PaymentEvidence(
        payment_id=str(payment.payment_id),
        status=payment.status,
        amount_paise=payment.amount_paise,
        currency=payment.currency,
        attempt_order_id=None if attempt is None else attempt.order_id,
        late_authorization=payment.late_authorization,
        conflict_reasons=tuple(reason.value for reason in payment.conflict_reasons),
        last_known_at_ms=payment.last_known_at_ms,
        stale=payment.stale,
        webhook_observations=sum(item.source is ObservationSource.WEBHOOK for item in payment.history),
        snapshot_observations=sum(item.source is ObservationSource.SNAPSHOT for item in payment.history),
    )


def _capture_processing(
    context: _RunContext, captured: Sequence[_InboxEvent], unsettled: Sequence[_InboxEvent]
) -> CaptureProcessing:
    settled = context.view.settled
    attempts = [attempt for event in captured for attempt in context.worker_attempts_by_inbox.get(event.row.inbox_id, ())]
    return CaptureProcessing(
        captured_inbox_ids=tuple(event.row.inbox_id for event in captured),
        unsettled_inbox_ids=tuple(event.row.inbox_id for event in unsettled),
        target_inbox_id=unsettled[0].row.inbox_id if unsettled else None,
        worker_failures=sum(attempt.result is WorkerResult.FAILED for attempt in attempts),
        settled_results=tuple(settled[event.row.inbox_id][1] for event in captured if event.row.inbox_id in settled),
    )


def _freshness(
    context: _RunContext, projection: state.OrderProjection, stale_open: tuple[str, ...]
) -> EvidenceFreshness:
    newest = max((item.known_at_ms for payment in projection.payments for item in payment.history), default=None)
    return EvidenceFreshness(
        newest_observation_at_ms=newest,
        age_ms=None if newest is None else context.view.as_of_ms - newest,
        stale_payment_ids=tuple(sorted(str(payment.payment_id) for payment in projection.payments if payment.stale)),
        stale_open_payment_ids=stale_open,
    )


def _coverage(
    context: _RunContext, projection: state.OrderProjection, captured: Sequence[_InboxEvent]
) -> EvidenceCoverage:
    payments = projection.payments

    def seen_by(source: ObservationSource) -> int:
        return sum(any(item.source is source for item in payment.history) for payment in payments)

    return EvidenceCoverage(
        payments_with_attempt_row=sum(str(payment.payment_id) in context.attempts_by_payment for payment in payments),
        payments_seen_by_webhook=seen_by(ObservationSource.WEBHOOK),
        payments_seen_by_snapshot=seen_by(ObservationSource.SNAPSHOT),
        unobserved_payment_ids=tuple(sorted(str(p.payment_id) for p in payments if p.last_known_at_ms is None)),
        captured_events_received=len(captured),
    )


def _evidence_refs(
    context: _RunContext, projection: state.OrderProjection, events: Sequence[_InboxEvent]
) -> EvidenceRefs:
    rows = [event.row for event in events]
    return EvidenceRefs(
        payment_ids=tuple(str(payment.payment_id) for payment in projection.payments),
        observation_ids=tuple(sorted(item.observation_id for payment in projection.payments for item in payment.history)),
        inbox_ids=tuple(row.inbox_id for row in rows),
        worker_attempt_ids=tuple(sorted(
            attempt.id for row in rows for attempt in context.worker_attempts_by_inbox.get(row.inbox_id, ())
        )),
        delivery_attempt_ids=tuple(sorted(
            delivery.id for row in rows for delivery in context.deliveries_by_event.get((row.integration_id, row.event_id), ())
        )),
        conflicting_delivery_ids=projection.conflicting_delivery_ids,
    )


def _queue_coverage(context: _RunContext, *, unknown_orders: int) -> QueueCoverage:
    view = context.view
    stored_events = {(row.integration_id, row.event_id) for row in view.inbox}
    observed = [item.known_at_ms for payment in view.payments.values() for item in payment.history]
    return QueueCoverage(
        orders=len(view.orders),
        unknown_orders=unknown_orders,
        payments=len(view.payments),
        observations=len(observed),
        inbox_rows=len(view.inbox),
        worker_attempts=len(view.worker_attempts),
        delivery_attempts=len(view.deliveries),
        rejected_deliveries=sum(delivery.outcome in metrics.REJECTED_OUTCOMES for delivery in view.deliveries),
        deliveries_without_inbox_row=sum(
            (delivery.integration_id, delivery.event_id) not in stored_events for delivery in view.deliveries
        ),
        newest_observation_at_ms=max(observed, default=None),
    )
