"""As-of payment and order projection (SPEC.md §7, implementation guide §4).

Evidence accumulation, not "last event wins". A projection is a function of the
*set* of visible observations: arrival order and event IDs never decide meaning,
so a same-time contradiction is a conflict whichever event ID sorts first.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from recovery_workbench import storage
from recovery_workbench.contracts import ContractError, ExclusionReason, PaymentObservation, PaymentStatus

_SUCCESS_STATUSES = frozenset({PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.REFUNDED})
_CAPTURE_STATUSES = frozenset({PaymentStatus.CAPTURED, PaymentStatus.REFUNDED})
_UNRESOLVED_STATUSES = frozenset({PaymentStatus.UNKNOWN, PaymentStatus.CREATED, PaymentStatus.AUTHORIZED})


class ConflictReason(StrEnum):
    """Why one payment's visible evidence needs reconciliation before anyone acts on it."""

    SAME_TIME_CONTRADICTION = "same_time_contradiction"
    FAILURE_AFTER_SUCCESS = "failure_after_success"
    AMOUNT_DISAGREEMENT = "amount_disagreement"
    CURRENCY_DISAGREEMENT = "currency_disagreement"
    ORDER_DISAGREEMENT = "order_disagreement"


@dataclass(frozen=True)
class PaymentProjection:
    payment_id: str | None  # None only when nothing at all is known about the payment
    as_of_ms: int
    status: PaymentStatus
    conflict_reasons: tuple[ConflictReason, ...]
    late_authorization: bool  # a failure was later followed by authorisation or capture
    stale: bool  # no observation known within stale_after_ms of as_of_ms; never changes status
    last_known_at_ms: int | None
    amount_paise: int | None  # the amount every visible observation agrees on; None if none or disagreement
    currency: str | None
    order_ids: tuple[str, ...]  # orders named by the visible observations
    amount_refunded_paise: int
    history: tuple[PaymentObservation, ...]  # visible observations, ordered for display only

    @property
    def needs_reconciliation(self) -> bool:
        return bool(self.conflict_reasons)

    @property
    def has_capture_evidence(self) -> bool:
        return self.status in _CAPTURE_STATUSES


def project_payment(
    observations: Iterable[PaymentObservation],
    as_of_ms: int,
    stale_after_ms: int | None = None,
    *,
    payment_id: str | None = None,
) -> PaymentProjection:
    """Project one payment's state as of ``as_of_ms`` from its observations.

    Visible means known by ``as_of_ms`` and not occurring after it. Nothing visible
    means ``unknown``, never ``failed``. ``stale_after_ms=None`` disables staleness.
    """
    supplied = tuple(observations)
    payment_ids = {observation.payment_id for observation in supplied}
    if payment_id is not None:
        payment_ids.add(payment_id)
    if len(payment_ids) > 1:
        raise ContractError(f"project_payment takes one payment's observations, got payments {sorted(payment_ids)}")
    visible = tuple(
        sorted(
            (item for item in supplied if item.known_at_ms <= as_of_ms and item.occurred_at_ms <= as_of_ms),
            key=lambda item: (item.occurred_at_ms, item.known_at_ms, item.source.value, item.observation_id),
        )
    )
    # An observation whose status is itself "unknown" carries no state evidence.
    evidence = tuple(item for item in visible if item.status is not PaymentStatus.UNKNOWN)
    last_known_at_ms = max((item.known_at_ms for item in evidence), default=None)
    amounts = {item.amount_paise for item in evidence}
    currencies = {item.currency for item in evidence}
    return PaymentProjection(
        payment_id=next(iter(payment_ids), None),
        as_of_ms=as_of_ms,
        status=_strongest_status(evidence),
        conflict_reasons=_conflict_reasons(evidence),
        late_authorization=_has_late_authorization(evidence),
        stale=stale_after_ms is not None and (last_known_at_ms is None or as_of_ms - last_known_at_ms > stale_after_ms),
        last_known_at_ms=last_known_at_ms,
        amount_paise=next(iter(amounts)) if len(amounts) == 1 else None,
        currency=next(iter(currencies)) if len(currencies) == 1 else None,
        order_ids=tuple(sorted({item.order_id for item in evidence})),
        amount_refunded_paise=max((item.amount_refunded_paise for item in evidence), default=0),
        history=visible,
    )


def _strongest_status(evidence: Sequence[PaymentObservation]) -> PaymentStatus:
    # Success evidence is never erased: an older or later failure, authorisation or
    # creation cannot lower the status. A failure only shows when nothing succeeded.
    statuses = {item.status for item in evidence}
    if PaymentStatus.REFUNDED in statuses or any(item.amount_refunded_paise > 0 for item in evidence):
        return PaymentStatus.REFUNDED
    for status in (PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED, PaymentStatus.FAILED, PaymentStatus.CREATED):
        if status in statuses:
            return status
    return PaymentStatus.UNKNOWN


def _success_and_failure_times(evidence: Sequence[PaymentObservation]) -> tuple[set[int], set[int]]:
    successes = {item.occurred_at_ms for item in evidence if item.status in _SUCCESS_STATUSES}
    failures = {item.occurred_at_ms for item in evidence if item.status is PaymentStatus.FAILED}
    return successes, failures


def _conflict_reasons(evidence: Sequence[PaymentObservation]) -> tuple[ConflictReason, ...]:
    successes, failures = _success_and_failure_times(evidence)
    found: set[ConflictReason] = set()
    if successes & failures:
        found.add(ConflictReason.SAME_TIME_CONTRADICTION)
    if successes and any(failed_at > min(successes) for failed_at in failures):
        found.add(ConflictReason.FAILURE_AFTER_SUCCESS)
    if len({item.amount_paise for item in evidence}) > 1:
        found.add(ConflictReason.AMOUNT_DISAGREEMENT)
    if len({item.currency for item in evidence}) > 1:
        found.add(ConflictReason.CURRENCY_DISAGREEMENT)
    if len({item.order_id for item in evidence}) > 1:
        found.add(ConflictReason.ORDER_DISAGREEMENT)
    return tuple(reason for reason in ConflictReason if reason in found)


def _has_late_authorization(evidence: Sequence[PaymentObservation]) -> bool:
    successes, failures = _success_and_failure_times(evidence)
    return bool(failures) and any(succeeded_at > min(failures) for succeeded_at in successes)


# --------------------------------------------------------------------------- order projection


@dataclass(frozen=True)
class OrderProjection:
    run_id: str
    order_id: str
    as_of_ms: int
    order: storage.OrderRecord | None
    payments: tuple[PaymentProjection, ...]  # every known attempt and every observed payment for the order
    conflicting_delivery_ids: tuple[int, ...]
    fulfilment: storage.FulfilmentRecord | None  # as of as_of_ms
    exclusions: tuple[ExclusionReason, ...]

    @property
    def captured_payments(self) -> tuple[PaymentProjection, ...]:
        return tuple(payment for payment in self.payments if payment.has_capture_evidence)

    @property
    def single_captured_payment_id(self) -> str | None:
        captured = self.captured_payments
        return captured[0].payment_id if len(captured) == 1 else None

    @property
    def is_recovery_eligible(self) -> bool:
        """Every exclusion check passed: exactly one clean capture and nothing unresolved."""
        return not self.exclusions and self.single_captured_payment_id is not None


def project_order(
    *,
    run_id: str,
    order_id: str,
    as_of_ms: int,
    order: storage.OrderRecord | None,
    attempts: Sequence[storage.PaymentAttemptRecord],
    payments: Sequence[PaymentProjection],
    conflicting_delivery_ids: Sequence[int] = (),
    fulfilment: storage.FulfilmentRecord | None = None,
) -> OrderProjection:
    """Combine all attempts and payment projections for one order into exclusion reasons.

    ``attempts`` holds the attempt rows for the order and for every payment in
    ``payments``; each attempt for this order must have a projection.
    """
    projected_ids = {payment.payment_id for payment in payments}
    unprojected = sorted({a.payment_id for a in attempts if a.order_id == order_id} - projected_ids)
    if unprojected:
        raise ContractError(f"attempts {unprojected} of order {order_id!r} have no payment projection")
    found: set[ExclusionReason] = set()
    attempt_order = {attempt.payment_id: attempt.order_id for attempt in attempts}
    for payment in payments:
        named_orders = set(payment.order_ids) | ({attempt_order[payment.payment_id]} if payment.payment_id in attempt_order else set())
        if payment.needs_reconciliation or named_orders - {order_id}:
            found.add(ExclusionReason.CONFLICTING_EVIDENCE)
        if payment.status is PaymentStatus.REFUNDED:
            found.add(ExclusionReason.REFUNDED)
        if payment.stale:
            found.add(ExclusionReason.STALE_SNAPSHOT)
    if conflicting_delivery_ids:
        found.add(ExclusionReason.CONFLICTING_EVIDENCE)
    if order is None:
        found.add(ExclusionReason.UNKNOWN_ORDER)
    else:
        found |= _order_exclusions(order, payments, as_of_ms, fulfilment)
    return OrderProjection(
        run_id=run_id,
        order_id=order_id,
        as_of_ms=as_of_ms,
        order=order,
        payments=tuple(sorted(payments, key=lambda payment: payment.payment_id or "")),
        conflicting_delivery_ids=tuple(sorted(conflicting_delivery_ids)),
        fulfilment=fulfilment,
        exclusions=tuple(reason for reason in ExclusionReason if reason in found),
    )


def _order_exclusions(
    order: storage.OrderRecord,
    payments: Sequence[PaymentProjection],
    as_of_ms: int,
    fulfilment: storage.FulfilmentRecord | None,
) -> set[ExclusionReason]:
    found: set[ExclusionReason] = set()
    if fulfilment is not None:
        found.add(ExclusionReason.ALREADY_FULFILLED)
    if order.cancelled_at_ms is not None and order.cancelled_at_ms <= as_of_ms:
        found.add(ExclusionReason.CANCELLED)
    if order.refunded:
        found.add(ExclusionReason.REFUNDED)
    captured = [payment for payment in payments if payment.has_capture_evidence]
    if not captured:
        found.add(ExclusionReason.NOT_CAPTURED)
    if len(captured) > 1:
        found.add(ExclusionReason.MULTIPLE_CAPTURES)
    for payment in captured:
        if payment.amount_paise != order.amount_paise:
            found.add(ExclusionReason.AMOUNT_MISMATCH)
        if payment.currency != order.currency:
            found.add(ExclusionReason.CURRENCY_MISMATCH)
    if any(payment.status in _UNRESOLVED_STATUSES for payment in payments if not payment.has_capture_evidence):
        found.add(ExclusionReason.UNRESOLVED_ATTEMPT)
    return found


def load_order_projection(
    conn: sqlite3.Connection,
    run_id: str,
    order_id: str,
    as_of_ms: int,
    *,
    stale_after_ms: int | None = None,
) -> OrderProjection:
    """Read every attempt, observation and conflicting delivery for the order as of ``as_of_ms``.

    Raises ``ScopeError`` for an unknown run. An order missing from this run, including
    one that belongs to another run, projects with ``unknown_order``.
    """
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        order = storage.get_order(conn, run_id=run_id, order_id=order_id)
        order_attempts = storage.list_payment_attempts(conn, run_id=run_id, order_id=order_id, as_of_ms=as_of_ms)
        naming_order = storage.list_observations(conn, run_id=run_id, as_of_ms=as_of_ms, order_id=order_id)
        payment_ids = sorted({a.payment_id for a in order_attempts} | {o.payment_id for o in naming_order})
        observations = storage.list_observations(conn, run_id=run_id, as_of_ms=as_of_ms, payment_ids=payment_ids)
        attempts = storage.get_payment_attempts(conn, run_id=run_id, payment_ids=payment_ids, as_of_ms=as_of_ms)
        conflict_ids = storage.conflicting_delivery_ids(
            conn, run_id=run_id, as_of_ms=as_of_ms, order_id=order_id, payment_ids=payment_ids
        )
        fulfilment = storage.get_fulfilment(conn, run_id=run_id, order_id=order_id)
    by_payment: defaultdict[str, list[PaymentObservation]] = defaultdict(list)
    for observation in observations:
        by_payment[observation.payment_id].append(observation)
    payments = tuple(
        project_payment(by_payment[payment_id], as_of_ms, stale_after_ms, payment_id=payment_id)
        for payment_id in payment_ids
    )
    return project_order(
        run_id=run_id,
        order_id=order_id,
        as_of_ms=as_of_ms,
        order=order,
        attempts=attempts,
        payments=payments,
        conflicting_delivery_ids=conflict_ids,
        fulfilment=fulfilment if fulfilment is not None and fulfilment.fulfilled_at_ms <= as_of_ms else None,
    )
