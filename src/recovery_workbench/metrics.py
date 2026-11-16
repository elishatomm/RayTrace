"""Detector arithmetic and capture accounting (spike port; extended in later phases).

Ratios use floats only for the Wilson interval; the decline threshold is compared
with exact fractions so a decline of exactly 10 points is never lost to rounding.
Money stays integer paise throughout, and captured, refunded and restored-processing
values are always reported separately (EVALUATION.md §8): never one netted number.
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from pydantic import JsonValue

from recovery_workbench import state, storage
from recovery_workbench.contracts import (
    ContractError,
    PaymentObservation,
    PaymentStatus,
    ReceiveOutcome,
    WorkerResult,
)
from recovery_workbench.state import PaymentProjection
from recovery_workbench.worker import WORKER_BLOCKING_EXCLUSIONS


@dataclass(frozen=True)
class DetectorSettings:
    """EVALUATION.md §7 initial development defaults; heuristics, not a calibrated test."""

    min_baseline_attempts: int = 200
    min_current_attempts: int = 50
    min_absolute_decline: Fraction = Fraction(10, 100)
    z: float = 1.96  # 95% Wilson intervals


DEFAULT_DETECTOR = DetectorSettings()


def _check_counts(successes: int, total: int) -> None:
    for name, value in (("successes", successes), ("total", total)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(f"{name} must be a non-negative integer, got {value!r}")
    if successes > total:
        raise ContractError(f"successes ({successes}) cannot exceed total ({total})")


def wilson_interval(successes: int, total: int, z: float = DEFAULT_DETECTOR.z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion; ``(0.0, 1.0)`` when ``total`` is 0."""
    _check_counts(successes, total)
    if total == 0:
        return (0.0, 1.0)
    ratio = successes / total
    denominator = 1 + z * z / total
    centre = (ratio + z * z / (2 * total)) / denominator
    half_width = z * math.sqrt(ratio * (1 - ratio) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - half_width), min(1.0, centre + half_width))


def is_degraded(
    baseline_successes: int,
    baseline_total: int,
    current_successes: int,
    current_total: int,
    settings: DetectorSettings = DEFAULT_DETECTOR,
) -> bool:
    """True when the current cohort's ratio fell enough, on enough attempts, with separated intervals.

    Small cohorts are suppressed rather than reported as healthy or degraded.
    """
    _check_counts(baseline_successes, baseline_total)
    _check_counts(current_successes, current_total)
    if baseline_total < settings.min_baseline_attempts or current_total < settings.min_current_attempts:
        return False
    decline = Fraction(baseline_successes, baseline_total) - Fraction(current_successes, current_total)
    if decline < settings.min_absolute_decline:
        return False
    current_upper = wilson_interval(current_successes, current_total, settings.z)[1]
    baseline_lower = wilson_interval(baseline_successes, baseline_total, settings.z)[0]
    return current_upper < baseline_lower


# --------------------------------------------------------------------------- capture accounting


@dataclass(frozen=True)
class CaptureLedger:
    """Captured and refunded values over unique payments, never netted against each other."""

    captured_paise: int
    refunded_paise: int
    captured_payment_ids: tuple[str, ...]
    # Payments with capture evidence whose observations disagree on amount: excluded from
    # the sums and listed instead, so a conflict is visible rather than silently counted.
    unreconciled_payment_ids: tuple[str, ...]


def capture_ledger(payments: Iterable[PaymentProjection]) -> CaptureLedger:
    """Sum capture and refund evidence once per payment ID (delivery retries never double count)."""
    by_id: dict[str, PaymentProjection] = {}
    for payment in payments:
        if payment.payment_id is None:
            continue  # nothing is known about this payment, so it has no capture evidence
        previous = by_id.get(payment.payment_id)
        if previous is not None and previous != payment:
            raise ContractError(f"payment {payment.payment_id!r} appears twice with different projections")
        by_id[payment.payment_id] = payment
    captured = [payment for payment in by_id.values() if payment.has_capture_evidence]
    unreconciled = sorted(str(payment.payment_id) for payment in captured if payment.amount_paise is None)
    counted = [payment for payment in captured if payment.amount_paise is not None]
    return CaptureLedger(
        captured_paise=sum(int(payment.amount_paise or 0) for payment in counted),
        refunded_paise=sum(payment.amount_refunded_paise for payment in counted),
        captured_payment_ids=tuple(sorted(str(payment.payment_id) for payment in counted)),
        unreconciled_payment_ids=tuple(unreconciled),
    )


@dataclass(frozen=True)
class CaptureChange:
    """New capture and new refunds between two ledgers, reported separately."""

    additional_captured_paise: int
    additional_refunded_paise: int
    new_captured_payment_ids: tuple[str, ...]


def capture_change(before: CaptureLedger, after: CaptureLedger) -> CaptureChange:
    """What changed in provider-side money; internal reprocessing never appears here."""
    lost = sorted(set(before.captured_payment_ids) - set(after.captured_payment_ids))
    if lost:
        raise ContractError(f"capture evidence cannot disappear between ledgers; missing afterwards: {lost}")
    return CaptureChange(
        additional_captured_paise=after.captured_paise - before.captured_paise,
        additional_refunded_paise=after.refunded_paise - before.refunded_paise,
        new_captured_payment_ids=tuple(sorted(set(after.captured_payment_ids) - set(before.captured_payment_ids))),
    )


@dataclass(frozen=True)
class ProcessedOrder:
    """An order the workbench newly marked processed during an intervention."""

    order_id: str
    amount_paise: int
    capture_known_at_ms: int | None  # when its capture was first observed; None if never


def restored_processing_value(newly_processed: Iterable[ProcessedOrder], intervention_at_ms: int) -> int:
    """Sum of unique order amounts newly processed whose capture was observed before the intervention.

    This is restored order processing, not new money: it is never added to capture.
    """
    by_order: dict[str, ProcessedOrder] = {}
    for order in newly_processed:
        previous = by_order.get(order.order_id)
        if previous is not None and previous != order:
            raise ContractError(f"order {order.order_id!r} appears twice with different values")
        by_order[order.order_id] = order
    return sum(
        order.amount_paise
        for order in by_order.values()
        if order.capture_known_at_ms is not None and order.capture_known_at_ms < intervention_at_ms
    )


# --------------------------------------------------------------------------- run measures (phase 2)

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
_AUTHORISATION_EVIDENCE = frozenset({PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.REFUNDED})
_CAPTURE_EVIDENCE = frozenset({PaymentStatus.CAPTURED, PaymentStatus.REFUNDED})
_FINAL_PROVIDER_STATUSES = frozenset({PaymentStatus.CAPTURED, PaymentStatus.FAILED, PaymentStatus.REFUNDED})
_UNRESOLVED_STATUSES = frozenset({PaymentStatus.UNKNOWN, PaymentStatus.CREATED})
_SETTLING_RESULTS = frozenset(
    {WorkerResult.FULFILLED, WorkerResult.ALREADY_FULFILLED, WorkerResult.NO_EFFECT, WorkerResult.MANUAL_REVIEW}
)
REJECTED_OUTCOMES = frozenset(
    {
        ReceiveOutcome.REJECTED_SIGNATURE,
        ReceiveOutcome.REJECTED_SCHEMA,
        ReceiveOutcome.REJECTED_SIZE,
        ReceiveOutcome.UNKNOWN_INTEGRATION,
    }
)


@dataclass(frozen=True)
class MeasureWindows:
    """Mature-cohort windows (EVALUATION.md §7), sized for this simulator's volumes.

    A cohort is the attempts created in ``[T-H-W, T-H)``. H (10 min) lets late evidence
    arrive before a cohort is judged. W (30 min, as in the M0 packets) holds at least 50
    attempts of a 50%-share method at the demo rate of 4 attempts a minute. The baseline
    is the B (3 h) before the two most recent windows. These are toy settings, not
    payment finality rules; late evidence can still revise a cohort.
    """

    horizon_ms: int = 10 * MINUTE_MS
    window_ms: int = 30 * MINUTE_MS
    baseline_ms: int = 3 * HOUR_MS
    stale_after_ms: int = 30 * MINUTE_MS  # provider state older than this is not "coverage"

    def current(self, as_of_ms: int) -> tuple[int, int]:
        end = as_of_ms - self.horizon_ms
        return (end - self.window_ms, end)

    def previous(self, as_of_ms: int) -> tuple[int, int]:
        start, _ = self.current(as_of_ms)
        return (start - self.window_ms, start)

    def baseline(self, as_of_ms: int) -> tuple[int, int]:
        start, _ = self.previous(as_of_ms)
        return (start - self.baseline_ms, start)


DEFAULT_WINDOWS = MeasureWindows()


@dataclass(frozen=True)
class Ratio:
    """A ratio that always carries its denominator; ``value`` is None for an empty cohort."""

    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return None if self.denominator == 0 else self.numerator / self.denominator

    def as_json(self) -> dict[str, JsonValue]:
        value = self.value
        return {"numerator": self.numerator, "denominator": self.denominator,
                "value": None if value is None else round(value, 4)}


@dataclass(frozen=True)
class RunView:
    """Everything the measures need, read once and filtered to what was known at ``as_of_ms``."""

    run_id: str
    as_of_ms: int
    integration_ids: tuple[str, ...]
    orders: Mapping[str, storage.OrderRecord]
    attempts: tuple[storage.PaymentAttemptRecord, ...]
    payments: Mapping[str, PaymentProjection]
    order_projections: Mapping[str, state.OrderProjection]
    capture_known_at_ms: Mapping[str, int]  # payment ID -> when capture evidence first became visible
    fulfilments: Mapping[str, storage.FulfilmentRecord]
    inbox: tuple[storage.InboxRecord, ...]
    settled: Mapping[int, tuple[int, WorkerResult]]  # inbox ID -> (first settling attempt time, result)
    worker_attempts: tuple[storage.WorkerAttemptRecord, ...]
    deliveries: tuple[storage.DeliveryAttemptRecord, ...]


def load_run_view(
    conn: sqlite3.Connection, run_id: str, as_of_ms: int, *, stale_after_ms: int | None = None
) -> RunView:
    """Read the run as of ``as_of_ms`` in one snapshot; project every payment and order in memory."""
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        integration_ids = tuple(item.integration_id for item in storage.list_integrations(conn, run_id=run_id))
        orders = {o.order_id: o for o in storage.list_orders(conn, run_id=run_id) if o.created_at_ms <= as_of_ms}
        attempts = storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=as_of_ms)
        observations = storage.list_observations(conn, run_id=run_id, as_of_ms=as_of_ms)
        conflicts = storage.list_conflicting_deliveries(conn, run_id=run_id, as_of_ms=as_of_ms)
        fulfilments = {f.order_id: f for f in storage.list_fulfilments(conn, run_id=run_id) if f.fulfilled_at_ms <= as_of_ms}
        inbox = tuple(row for row in storage.list_inbox_rows(conn, run_id=run_id) if row.received_at_ms <= as_of_ms)
        worker_attempts = tuple(
            item for item in storage.list_worker_attempts(conn, run_id=run_id) if item.finished_at_ms <= as_of_ms
        )
        deliveries = tuple(
            item for item in storage.list_delivery_attempts(conn, run_id=run_id) if item.received_at_ms <= as_of_ms
        )
    by_payment: defaultdict[str, list[PaymentObservation]] = defaultdict(list)
    for observation in observations:
        by_payment[observation.payment_id].append(observation)
    payment_ids = sorted({attempt.payment_id for attempt in attempts} | set(by_payment))
    payments = {
        payment_id: state.project_payment(by_payment[payment_id], as_of_ms, stale_after_ms, payment_id=payment_id)
        for payment_id in payment_ids
    }
    capture_known = {
        payment_id: min(item.known_at_ms for item in items if item.status in _CAPTURE_EVIDENCE)
        for payment_id, items in by_payment.items()
        if any(item.status in _CAPTURE_EVIDENCE for item in items)
    }
    settled: dict[int, tuple[int, WorkerResult]] = {}
    for item in sorted(worker_attempts, key=lambda attempt: (attempt.finished_at_ms, attempt.id)):
        if item.result in _SETTLING_RESULTS and item.inbox_id not in settled:
            settled[item.inbox_id] = (item.finished_at_ms, item.result)
    return RunView(
        run_id=run_id,
        as_of_ms=as_of_ms,
        integration_ids=integration_ids,
        orders=orders,
        attempts=attempts,
        payments=payments,
        order_projections=_project_orders(run_id, as_of_ms, orders, attempts, payments, conflicts, fulfilments),
        capture_known_at_ms=capture_known,
        fulfilments=fulfilments,
        inbox=inbox,
        settled=settled,
        worker_attempts=worker_attempts,
        deliveries=deliveries,
    )


def _project_orders(
    run_id: str,
    as_of_ms: int,
    orders: Mapping[str, storage.OrderRecord],
    attempts: Sequence[storage.PaymentAttemptRecord],
    payments: Mapping[str, PaymentProjection],
    conflicts: Sequence[storage.ConflictingDeliveryRecord],
    fulfilments: Mapping[str, storage.FulfilmentRecord],
) -> dict[str, state.OrderProjection]:
    """The same projection as ``state.load_order_projection``, for every order without per-order queries."""
    attempt_by_payment = {attempt.payment_id: attempt for attempt in attempts}
    payments_by_order: defaultdict[str, set[str]] = defaultdict(set)
    for attempt in attempts:
        payments_by_order[attempt.order_id].add(attempt.payment_id)
    for payment_id, projection in payments.items():
        for order_id in projection.order_ids:
            payments_by_order[order_id].add(payment_id)
    conflicts_by_order: defaultdict[str, set[int]] = defaultdict(set)
    conflicts_by_payment: defaultdict[str, set[int]] = defaultdict(set)
    for conflict in conflicts:
        conflicts_by_order[conflict.order_id].add(conflict.id)
        conflicts_by_order[conflict.original_order_id].add(conflict.id)
        conflicts_by_payment[conflict.payment_id].add(conflict.id)
        conflicts_by_payment[conflict.original_payment_id].add(conflict.id)
    projections: dict[str, state.OrderProjection] = {}
    for order_id, order in orders.items():
        payment_ids = sorted(payments_by_order.get(order_id, ()))
        conflict_ids = set(conflicts_by_order.get(order_id, ()))
        for payment_id in payment_ids:
            conflict_ids |= conflicts_by_payment.get(payment_id, set())
        projections[order_id] = state.project_order(
            run_id=run_id,
            order_id=order_id,
            as_of_ms=as_of_ms,
            order=order,
            attempts=[attempt_by_payment[p] for p in payment_ids if p in attempt_by_payment],
            payments=[payments[p] for p in payment_ids],
            conflicting_delivery_ids=sorted(conflict_ids),
            fulfilment=fulfilments.get(order_id),
        )
    return projections


@dataclass(frozen=True)
class ProviderMeasures:
    """Provider-side ratios for one cohort of distinct attempts (never delivery counts)."""

    cohort: str  # a method, or "all"
    window_start_ms: int
    window_end_ms: int
    as_of_ms: int
    authorization: Ratio  # attempts with authorisation evidence by as_of (including captured or refunded)
    capture: Ratio
    coverage: Ratio  # attempts with a final provider status, or provider state no older than stale_after_ms
    unresolved: Ratio  # attempts with no outcome evidence at all (unknown or created)
    freshest_known_at_ms: int | None

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "cohort": self.cohort,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "as_of_ms": self.as_of_ms,
            "authorization": self.authorization.as_json(),
            "capture": self.capture.as_json(),
            "coverage": self.coverage.as_json(),
            "unresolved": self.unresolved.as_json(),
            "freshness_ms": None if self.freshest_known_at_ms is None else self.as_of_ms - self.freshest_known_at_ms,
        }


def provider_measures(
    view: RunView,
    window: tuple[int, int],
    *,
    method: str | None = None,
    stale_after_ms: int = DEFAULT_WINDOWS.stale_after_ms,
) -> ProviderMeasures:
    start, end = window
    cohort = [a for a in view.attempts if start <= a.created_at_ms < end and (method is None or a.method == method)]
    projections = [view.payments[attempt.payment_id] for attempt in cohort]
    total = len(projections)

    def covered(payment: PaymentProjection) -> bool:
        if payment.last_known_at_ms is None:
            return False
        return payment.status in _FINAL_PROVIDER_STATUSES or view.as_of_ms - payment.last_known_at_ms <= stale_after_ms

    known_times = [payment.last_known_at_ms for payment in projections if payment.last_known_at_ms is not None]
    return ProviderMeasures(
        cohort=method or "all",
        window_start_ms=start,
        window_end_ms=end,
        as_of_ms=view.as_of_ms,
        authorization=Ratio(sum(p.status in _AUTHORISATION_EVIDENCE for p in projections), total),
        capture=Ratio(sum(p.has_capture_evidence for p in projections), total),
        coverage=Ratio(sum(covered(p) for p in projections), total),
        unresolved=Ratio(sum(p.status in _UNRESOLVED_STATUSES for p in projections), total),
        freshest_known_at_ms=max(known_times, default=None),
    )


@dataclass(frozen=True)
class MerchantMeasures:
    as_of_ms: int
    completion: Ratio  # processed / eligible orders whose clean capture was visible for at least the grace period
    pending_captured_orders: int
    pending_captured_value_paise: int
    logical_events: int  # durable inbox rows (one per event ID), not delivery attempts
    delivery_attempts: int
    acknowledged_attempts: int
    rejected_attempts: int
    attempts_by_outcome: dict[str, int]
    backlog_rows: int  # committed events not yet settled by the worker
    rows_with_failures: int
    manual_review_rows: int
    worker_failures: int
    oldest_backlog_age_ms: int | None

    def as_json(self) -> dict[str, JsonValue]:
        values: dict[str, JsonValue] = {name: getattr(self, name) for name in self.__dataclass_fields__}
        values["completion"] = self.completion.as_json()
        return values


def merchant_measures(view: RunView, *, grace_ms: int = 5 * MINUTE_MS, lookback_ms: int | None = None) -> MerchantMeasures:
    """Merchant processing measures; ``lookback_ms`` limits completion to recently visible captures."""
    eligible = processed = pending_value = 0
    for order_id, projection in view.order_projections.items():
        payment_id = projection.single_captured_payment_id
        if payment_id is None or set(projection.exclusions) & WORKER_BLOCKING_EXCLUSIONS or projection.order is None:
            continue
        known_at = view.capture_known_at_ms.get(payment_id)
        if known_at is None or known_at > view.as_of_ms - grace_ms:
            continue
        if lookback_ms is not None and known_at < view.as_of_ms - lookback_ms:
            continue
        eligible += 1
        if order_id in view.fulfilments:
            processed += 1
        else:
            pending_value += projection.order.amount_paise
    by_outcome: dict[str, int] = {}
    for delivery in view.deliveries:
        by_outcome[delivery.outcome.value] = by_outcome.get(delivery.outcome.value, 0) + 1
    backlog = [row for row in view.inbox if row.inbox_id not in view.settled]
    failed_rows = {item.inbox_id for item in view.worker_attempts if item.result is WorkerResult.FAILED}
    return MerchantMeasures(
        as_of_ms=view.as_of_ms,
        completion=Ratio(processed, eligible),
        pending_captured_orders=eligible - processed,
        pending_captured_value_paise=pending_value,
        logical_events=len(view.inbox),
        delivery_attempts=len(view.deliveries),
        acknowledged_attempts=sum(200 <= item.http_status < 300 for item in view.deliveries),
        rejected_attempts=sum(item.outcome in REJECTED_OUTCOMES for item in view.deliveries),
        attempts_by_outcome=dict(sorted(by_outcome.items())),
        backlog_rows=len(backlog),
        rows_with_failures=len({row.inbox_id for row in backlog} & failed_rows),
        manual_review_rows=sum(result is WorkerResult.MANUAL_REVIEW for _, result in view.settled.values()),
        worker_failures=sum(item.result is WorkerResult.FAILED for item in view.worker_attempts),
        oldest_backlog_age_ms=view.as_of_ms - min(row.received_at_ms for row in backlog) if backlog else None,
    )


# --------------------------------------------------------------------------- detectors (phase 2)

METHOD_DROP = "method_authorization_drop"
PROCESSING_GAP = "merchant_processing_gap"
REJECTION_SPIKE = "delivery_rejection_spike"
UNRESOLVED_SPIKE = "unresolved_spike"
ORDERS_NOT_COMPLETING = "orders_not_completing"
# Reported at every evaluation, fired or not.
DETECTORS: tuple[str, ...] = (ORDERS_NOT_COMPLETING, PROCESSING_GAP, METHOD_DROP, REJECTION_SPIKE, UNRESOLVED_SPIKE)
# Reported only for an integration whose deliveries show its pattern (detect_signature_rejections).
SIGNATURE_REJECTIONS = "signature_rejections_while_accepting"


@dataclass(frozen=True)
class IncidentDetectorSettings:
    """Initial development defaults (EVALUATION.md §7); heuristics, not calibrated tests."""

    windows: MeasureWindows = DEFAULT_WINDOWS
    method: DetectorSettings = DEFAULT_DETECTOR  # baseline >= 200, current >= 50, -10 points, Wilson
    # The order-completion symptom runs on whole orders, which are rarer than per-method attempts.
    symptom: DetectorSettings = field(default_factory=lambda: DetectorSettings(min_baseline_attempts=30,
                                                                               min_current_attempts=10))
    processing_grace_ms: int = 5 * MINUTE_MS
    processing_lookback_ms: int = 60 * MINUTE_MS
    processing_min_pending: int = 5
    processing_max_completion: Fraction = Fraction(4, 5)
    rejection_min_count: int = 10
    rejection_min_share: Fraction = Fraction(1, 5)
    # The rejection spike's minimum count without its share: at a busy integration a rotation's old-signature retries
    # stay far under a fifth of its deliveries (Q25's world: 10 to 30 a window, 2.5-7% of them, no incident).
    signature_rejection_min_count: int = 10
    unresolved_min_attempts: int = 30
    unresolved_min_share: Fraction = Fraction(15, 100)
    unresolved_min_rise: Fraction = Fraction(10, 100)


DEFAULT_INCIDENT_DETECTORS = IncidentDetectorSettings()


@dataclass(frozen=True)
class DetectorResult:
    detector: str
    cohort: dict[str, str]
    fired: bool
    reason: str  # why it fired or did not, e.g. "insufficient_samples"
    window_start_ms: int
    window_end_ms: int
    measurements: dict[str, JsonValue]

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "detector": self.detector, "cohort": dict(self.cohort), "fired": self.fired, "reason": self.reason,
            "window_start_ms": self.window_start_ms, "window_end_ms": self.window_end_ms,
            "measurements": self.measurements,
        }


def _counts(successes: int, total: int) -> dict[str, JsonValue]:
    return Ratio(successes, total).as_json()


def _degradation_reason(baseline: Ratio, current: Ratio, settings: DetectorSettings) -> str:
    if baseline.denominator < settings.min_baseline_attempts or current.denominator < settings.min_current_attempts:
        return "insufficient_samples"
    if is_degraded(baseline.numerator, baseline.denominator, current.numerator, current.denominator, settings):
        return "degraded"
    return "within_baseline"


def detect_method_drop(view: RunView, settings: IncidentDetectorSettings) -> list[DetectorResult]:
    """Per method: two successive mature windows both degraded against the baseline."""
    windows = settings.windows
    results = []
    for method in sorted({attempt.method for attempt in view.attempts}):
        baseline = provider_measures(view, windows.baseline(view.as_of_ms), method=method).authorization
        previous = provider_measures(view, windows.previous(view.as_of_ms), method=method).authorization
        current_measures = provider_measures(view, windows.current(view.as_of_ms), method=method)
        current = current_measures.authorization
        reason = _two_window_reason(
            (_degradation_reason(baseline, previous, settings.method),
             _degradation_reason(baseline, current, settings.method)),
            "degraded", "degraded_in_two_successive_windows",
        )
        results.append(DetectorResult(
            detector=METHOD_DROP, cohort={"method": method}, fired=reason.endswith("two_successive_windows"),
            reason=reason,
            window_start_ms=windows.previous(view.as_of_ms)[0], window_end_ms=current_measures.window_end_ms,
            measurements={"baseline": baseline.as_json(), "previous": previous.as_json(), "current": current.as_json()},
        ))
    return results


def detect_processing_gap(view: RunView, settings: IncidentDetectorSettings) -> DetectorResult:
    """Captured, eligible orders the merchant has not processed within the grace period."""
    measures = merchant_measures(view, grace_ms=settings.processing_grace_ms, lookback_ms=settings.processing_lookback_ms)
    completion = measures.completion
    enough = measures.pending_captured_orders >= settings.processing_min_pending
    low = completion.denominator > 0 and (
        Fraction(completion.numerator, completion.denominator) <= settings.processing_max_completion
    )
    return DetectorResult(
        detector=PROCESSING_GAP, cohort={"scope": "captured_orders"}, fired=enough and low,
        reason=_threshold_reason(enough=enough, crossed=low, fired="captured_orders_unprocessed",
                                 too_few="too_few_pending", normal="completion_normal"),
        window_start_ms=view.as_of_ms - settings.processing_lookback_ms,
        window_end_ms=view.as_of_ms - settings.processing_grace_ms,
        measurements={"completion": completion.as_json(), "pending_captured_orders": measures.pending_captured_orders,
                      "pending_captured_value_paise": measures.pending_captured_value_paise},
    )


def detect_delivery_rejections(view: RunView, settings: IncidentDetectorSettings) -> list[DetectorResult]:
    """Per integration: rejected delivery attempts in the latest window."""
    start, end = view.as_of_ms - settings.windows.window_ms, view.as_of_ms
    results = []
    for integration_id in view.integration_ids:
        recent = [d for d in view.deliveries if d.integration_id == integration_id and start <= d.received_at_ms <= end]
        rejected = sum(d.outcome in REJECTED_OUTCOMES for d in recent)
        enough = rejected >= settings.rejection_min_count
        high = bool(recent) and Fraction(rejected, len(recent)) >= settings.rejection_min_share
        results.append(DetectorResult(
            detector=REJECTION_SPIKE, cohort={"integration_id": integration_id}, fired=enough and high,
            reason=_threshold_reason(enough=enough, crossed=high, fired="rejections_high",
                                     too_few="too_few_rejections", normal="share_normal"),
            window_start_ms=start, window_end_ms=end,
            measurements={"rejected": _counts(rejected, len(recent))},
        ))
    return results


def detect_signature_rejections(view: RunView, settings: IncidentDetectorSettings) -> list[DetectorResult]:
    """Per integration: deliveries rejected for their signature in the latest window while others are still accepted.

    The rejection spike's blind spot. During a provider secret rotation, retries of events first sent before it keep
    the old signature and are refused while new events verify, so a busy integration's rejections stay far under the
    spike's share and no incident opened. An integration is reported only when the window holds a
    signature rejection and, at or after the first one, an accepted delivery: a receiver that refuses everything is the
    spike's case. A run without that pattern is evaluated exactly as before this detector existed. It fires on the
    spike's minimum count, ``signature_rejection_min_count``, whatever their share.
    """
    start, end = view.as_of_ms - settings.windows.window_ms, view.as_of_ms
    results = []
    for integration_id in view.integration_ids:
        recent = [d for d in view.deliveries if d.integration_id == integration_id and start <= d.received_at_ms <= end]
        rejected = [d for d in recent if d.outcome is ReceiveOutcome.REJECTED_SIGNATURE]
        if not rejected:
            continue
        first = min(d.received_at_ms for d in rejected)
        accepted_since = sum(d.outcome is ReceiveOutcome.ACCEPTED and d.received_at_ms >= first for d in recent)
        if not accepted_since:
            continue
        enough = len(rejected) >= settings.signature_rejection_min_count
        results.append(DetectorResult(
            detector=SIGNATURE_REJECTIONS, cohort={"integration_id": integration_id}, fired=enough,
            reason="rejections_high" if enough else "too_few_rejections",
            window_start_ms=start, window_end_ms=end,
            measurements={"rejected_signature": _counts(len(rejected), len(recent)),
                          "distinct_bodies_rejected": len({d.body_sha256 for d in rejected}),
                          "accepted_since_first_rejection": accepted_since},
        ))
    return results


def _threshold_reason(*, enough: bool, crossed: bool, fired: str, too_few: str, normal: str) -> str:
    """A single-window detector's reason: too few events to judge, threshold crossed, or normal."""
    if not enough:
        return too_few
    return fired if crossed else normal


def _two_window_reason(reasons: tuple[str, str], fired_reason: str, confirmed: str) -> str:
    """An alert needs both successive windows affected (EVALUATION.md §7); otherwise say why not."""
    if reasons == (fired_reason, fired_reason):
        return confirmed
    return reasons[1] if reasons[1] != fired_reason else reasons[0]


def detect_unresolved_spike(view: RunView, settings: IncidentDetectorSettings) -> DetectorResult:
    """Mature attempts with no outcome evidence, in two successive windows, against the baseline share."""
    windows = settings.windows
    baseline = provider_measures(view, windows.baseline(view.as_of_ms)).unresolved
    baseline_share = Fraction(baseline.numerator, baseline.denominator) if baseline.denominator else Fraction(0)

    def reason_for(cohort: Ratio) -> str:
        if cohort.denominator < settings.unresolved_min_attempts:
            return "insufficient_samples"
        share = Fraction(cohort.numerator, cohort.denominator)
        high = share >= settings.unresolved_min_share and share - baseline_share >= settings.unresolved_min_rise
        return "unresolved_high" if high else "within_baseline"

    previous = provider_measures(view, windows.previous(view.as_of_ms)).unresolved
    current = provider_measures(view, windows.current(view.as_of_ms)).unresolved
    reason = _two_window_reason((reason_for(previous), reason_for(current)), "unresolved_high",
                                "unresolved_high_in_two_successive_windows")
    return DetectorResult(
        detector=UNRESOLVED_SPIKE, cohort={"method": "all"}, fired=reason.endswith("two_successive_windows"),
        reason=reason, window_start_ms=windows.previous(view.as_of_ms)[0], window_end_ms=windows.current(view.as_of_ms)[1],
        measurements={"baseline": baseline.as_json(), "previous": previous.as_json(), "current": current.as_json()},
    )


def detect_orders_not_completing(view: RunView, settings: IncidentDetectorSettings) -> DetectorResult:
    """The business symptom: fewer mature orders reach fulfilment than in the baseline, two windows running."""
    windows = settings.windows

    def completed(window: tuple[int, int]) -> Ratio:
        start, end = window
        cohort = [order_id for order_id, order in view.orders.items() if start <= order.created_at_ms < end]
        return Ratio(sum(order_id in view.fulfilments for order_id in cohort), len(cohort))

    baseline = completed(windows.baseline(view.as_of_ms))
    previous = completed(windows.previous(view.as_of_ms))
    current = completed(windows.current(view.as_of_ms))
    reason = _two_window_reason(
        (_degradation_reason(baseline, previous, settings.symptom),
         _degradation_reason(baseline, current, settings.symptom)),
        "degraded", "degraded_in_two_successive_windows",
    )
    return DetectorResult(
        detector=ORDERS_NOT_COMPLETING, cohort={"scope": "all_orders"}, fired=reason.endswith("two_successive_windows"),
        reason=reason, window_start_ms=windows.previous(view.as_of_ms)[0], window_end_ms=windows.current(view.as_of_ms)[1],
        measurements={"baseline": baseline.as_json(), "previous": previous.as_json(), "current": current.as_json()},
    )


def evaluate_detectors(
    view: RunView, settings: IncidentDetectorSettings = DEFAULT_INCIDENT_DETECTORS
) -> tuple[DetectorResult, ...]:
    """Every detector on every cohort, fired or not; which ones did not fire is evidence too.

    The exception is ``SIGNATURE_REJECTIONS``, reported only for an integration whose deliveries show its pattern.
    """
    return (
        detect_orders_not_completing(view, settings),
        detect_processing_gap(view, settings),
        *detect_method_drop(view, settings),
        *detect_delivery_rejections(view, settings),
        *detect_signature_rejections(view, settings),
        detect_unresolved_spike(view, settings),
    )
