"""Inbox worker: one ``BEGIN IMMEDIATE`` transaction per inbox row.

Inside that transaction the worker claims the oldest pending row, calls the deployed
merchant handler, re-checks the handler's request against the event and the order
projection, and then fulfils, stops for manual review, or records the handler
failure. A crash anywhere before COMMIT leaves the row pending, so work resumes on
restart; the one-fulfilment-per-order key makes a repeated effect impossible.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from recovery_workbench import ingestion, state, storage
from recovery_workbench.contracts import (
    ContractError,
    ExclusionReason,
    FailureReport,
    HandlerAction,
    HandlerOutcome,
    InboxStatus,
    MerchantHandler,
    WorkerResult,
)

# Exclusions that stop ordinary processing of an event. Unresolved other attempts and
# stale snapshots stop recovery plans (M2) but not the processing of a fresh capture;
# ALREADY_FULFILLED is decided separately (same payment is harmless, another is review).
WORKER_BLOCKING_EXCLUSIONS: frozenset[ExclusionReason] = frozenset(
    {
        ExclusionReason.UNKNOWN_ORDER,
        ExclusionReason.NOT_CAPTURED,
        ExclusionReason.MULTIPLE_CAPTURES,
        ExclusionReason.AMOUNT_MISMATCH,
        ExclusionReason.CURRENCY_MISMATCH,
        ExclusionReason.CANCELLED,
        ExclusionReason.REFUNDED,
        ExclusionReason.CONFLICTING_EVIDENCE,
    }
)

# Bounded retry schedule: wait 1 minute after the first failure and 5 after the second; the
# third failure (default max_attempts) marks the row failed. The simulator uses it so worker
# evidence is spread over time. IMMEDIATE_RETRY keeps the phase-1 behaviour (every attempt due
# at once) for callers that ask for it, and is the default until existing callers move over.
DEFAULT_RETRY_BACKOFF_MS: tuple[int, ...] = (60_000, 300_000)
IMMEDIATE_RETRY: tuple[int, ...] = ()

# Recorded as the exception category when a handler returns something that is not a
# valid HandlerOutcome. The worker raised that error, not the merchant code, so it is
# not passed to describe_failure (which renders merchant stack traces).
HANDLER_CONTRACT_VIOLATION = "handler_contract_violation"


class HandlerMismatch(StrEnum):
    """A fulfil request that disagrees with the verified event or the order."""

    ORDER_NOT_IN_EVENT = "order_not_in_event"
    PAYMENT_NOT_IN_EVENT = "payment_not_in_event"
    PAYMENT_NOT_THE_CAPTURE = "payment_not_the_capture"
    AMOUNT_NOT_ORDER_AMOUNT = "amount_not_order_amount"
    CURRENCY_NOT_ORDER_CURRENCY = "currency_not_order_currency"
    FULFILLED_BY_OTHER_PAYMENT = "fulfilled_by_other_payment"


@dataclass(frozen=True)
class WorkerStep:
    """What one ``process_next`` call did to one inbox row."""

    inbox_id: int
    event_id: str
    result: WorkerResult
    inbox_status: InboxStatus
    attempts: int
    review_reasons: tuple[str, ...] = ()  # ExclusionReason and HandlerMismatch values
    exception_category: str | None = None


@dataclass(frozen=True)
class _Attempt:
    """The row being processed plus the facts every recorded attempt needs."""

    conn: sqlite3.Connection
    row: storage.InboxRecord
    worker_version: str
    now_ms: int
    retry_backoff_ms: tuple[int, ...]


def process_next(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    handler: MerchantHandler,
    now_ms: int,
    max_attempts: int = 3,
    stale_after_ms: int | None = None,
    retry_backoff_ms: Sequence[int] = IMMEDIATE_RETRY,
) -> WorkerStep | None:
    """Process the oldest pending inbox row that is due at ``now_ms``; None when there is none.

    After a failure the row waits ``retry_backoff_ms[n - 1]`` before attempt n + 1 (the last
    entry repeats; an empty schedule retries at once) until ``max_attempts`` marks it failed.
    """
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ContractError(f"now_ms must be a non-negative integer of replay time, got {now_ms!r}")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ContractError(f"max_attempts must be a positive integer, got {max_attempts!r}")
    backoff = tuple(retry_backoff_ms)
    if any(isinstance(delay, bool) or not isinstance(delay, int) or delay < 0 for delay in backoff):
        raise ContractError(f"retry_backoff_ms must hold non-negative integers of milliseconds, got {backoff!r}")
    storage.require_run(conn, run_id)
    with storage.write_transaction(conn):
        row = storage.next_pending_inbox_row(conn, run_id=run_id, received_by_ms=now_ms)
        if row is None:
            return None
        attempt = _Attempt(conn=conn, row=row, worker_version=handler.version, now_ms=now_ms, retry_backoff_ms=backoff)
        return _process(attempt, handler, max_attempts=max_attempts, stale_after_ms=stale_after_ms)


def run_worker(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    handler: MerchantHandler,
    now_ms: int,
    max_attempts: int = 3,
    stale_after_ms: int | None = None,
    max_steps: int = 10_000,
    retry_backoff_ms: Sequence[int] = IMMEDIATE_RETRY,
) -> tuple[WorkerStep, ...]:
    """Process due rows until none is left or ``max_steps`` rows were attempted.

    Every step either settles its row, spends one of its bounded attempts, or schedules it
    for later, so the loop ends; ``max_steps`` only bounds a single call's work.
    """
    steps: list[WorkerStep] = []
    for _ in range(max_steps):
        step = process_next(
            conn, run_id=run_id, handler=handler, now_ms=now_ms, max_attempts=max_attempts,
            stale_after_ms=stale_after_ms, retry_backoff_ms=retry_backoff_ms,
        )
        if step is None:
            break
        steps.append(step)
    return tuple(steps)


def _process(
    attempt: _Attempt, handler: MerchantHandler, *, max_attempts: int, stale_after_ms: int | None
) -> WorkerStep:
    # The raw body was verified and schema-checked on receipt; parsing it again gives the
    # handler a fresh plain mapping and the worker a typed view to check the handler against.
    verified_event = ingestion.parse_payment_event(attempt.row.raw_body)
    try:
        handler_reply = handler.handle(json.loads(attempt.row.raw_body))
    except Exception as handler_error:  # any merchant-code failure is recorded evidence
        report = FailureReport.model_validate(handler.describe_failure(handler_error))
        return _record_failure(
            attempt, max_attempts=max_attempts, category=report.exception_category, stack_trace=report.stack_trace,
            detail=None,
        )
    try:
        outcome = _validate_outcome(handler_reply)
    except ContractError as violation:
        return _record_failure(
            attempt, max_attempts=max_attempts, category=HANDLER_CONTRACT_VIOLATION, stack_trace=None,
            detail=str(violation),
        )
    if outcome.action is HandlerAction.IGNORE:
        return _settle(attempt, WorkerResult.NO_EFFECT)
    return _fulfil(attempt, outcome, verified_event, stale_after_ms=stale_after_ms)


def _validate_outcome(handler_reply: object) -> HandlerOutcome:
    candidate = dict(handler_reply) if isinstance(handler_reply, Mapping) else handler_reply
    try:
        outcome = HandlerOutcome.model_validate(candidate)
    except ValidationError as invalid:
        problems = "; ".join(f"{'.'.join(map(str, item['loc'])) or 'outcome'}: {item['type']}" for item in invalid.errors())
        raise ContractError(f"handler returned an invalid outcome ({problems})") from invalid
    if outcome.action is HandlerAction.FULFIL and None in (
        outcome.order_id, outcome.payment_id, outcome.amount_paise, outcome.currency
    ):
        raise ContractError("a fulfil outcome must name order_id, payment_id, amount_paise and currency")
    return outcome


def _fulfil(
    attempt: _Attempt, outcome: HandlerOutcome, event: ingestion.PaymentEvent, *, stale_after_ms: int | None
) -> WorkerStep:
    # _validate_outcome guarantees these are set for a fulfil request.
    order_id, payment_id = str(outcome.order_id), str(outcome.payment_id)
    redirected: list[str] = []
    if order_id != event.payment.order_id:
        redirected.append(HandlerMismatch.ORDER_NOT_IN_EVENT.value)
    if payment_id != event.payment.id:
        redirected.append(HandlerMismatch.PAYMENT_NOT_IN_EVENT.value)
    if redirected:
        return _stop_for_review(attempt, redirected)

    run_id = attempt.row.run_id
    existing = storage.get_fulfilment(attempt.conn, run_id=run_id, order_id=order_id)
    if existing is not None and existing.payment_id == payment_id:
        return _settle(attempt, WorkerResult.ALREADY_FULFILLED)
    if existing is not None:
        return _stop_for_review(attempt, [HandlerMismatch.FULFILLED_BY_OTHER_PAYMENT.value])

    projection = state.load_order_projection(
        attempt.conn, run_id, order_id, attempt.now_ms, stale_after_ms=stale_after_ms
    )
    reasons = [reason.value for reason in projection.exclusions if reason in WORKER_BLOCKING_EXCLUSIONS]
    reasons.extend(_disagreements_with_order(outcome, projection))
    if reasons:
        return _stop_for_review(attempt, reasons)

    storage.insert_fulfilment(
        attempt.conn,
        storage.FulfilmentRecord(
            run_id=run_id, order_id=order_id, payment_id=payment_id, amount_paise=outcome.amount_paise,
            currency=outcome.currency, fulfilled_at_ms=attempt.now_ms, via="worker",
        ),
    )
    storage.mark_order_fulfilled(attempt.conn, run_id=run_id, order_id=order_id)
    return _settle(attempt, WorkerResult.FULFILLED)


def _disagreements_with_order(outcome: HandlerOutcome, projection: state.OrderProjection) -> list[str]:
    """The handler's own claims, checked against the order and its single capture."""
    order = projection.order
    if order is None:
        return []  # already stopped by UNKNOWN_ORDER
    found: list[str] = []
    captured_id = projection.single_captured_payment_id
    if captured_id is not None and outcome.payment_id != captured_id:
        found.append(HandlerMismatch.PAYMENT_NOT_THE_CAPTURE.value)
    if outcome.amount_paise != order.amount_paise:
        found.append(HandlerMismatch.AMOUNT_NOT_ORDER_AMOUNT.value)
    if outcome.currency != order.currency:
        found.append(HandlerMismatch.CURRENCY_NOT_ORDER_CURRENCY.value)
    return found


def _settle(attempt: _Attempt, result: WorkerResult) -> WorkerStep:
    """The row is done: fulfilled now, fulfilled before by the same payment, or no business effect."""
    attempts = attempt.row.attempts + 1
    _write_attempt(attempt, result)
    storage.record_inbox_processing(
        attempt.conn, run_id=attempt.row.run_id, inbox_id=attempt.row.inbox_id, status=InboxStatus.PROCESSED,
        attempts=attempts, last_error=None, processed_at_ms=attempt.now_ms,
    )
    return WorkerStep(attempt.row.inbox_id, attempt.row.event_id, result, InboxStatus.PROCESSED, attempts)


def _stop_for_review(attempt: _Attempt, reasons: Sequence[str]) -> WorkerStep:
    attempts = attempt.row.attempts + 1
    detail = ", ".join(reasons)
    _write_attempt(attempt, WorkerResult.MANUAL_REVIEW, detail=detail)
    storage.record_inbox_processing(
        attempt.conn, run_id=attempt.row.run_id, inbox_id=attempt.row.inbox_id, status=InboxStatus.MANUAL_REVIEW,
        attempts=attempts, last_error=f"manual review: {detail}", processed_at_ms=None,
    )
    return WorkerStep(
        attempt.row.inbox_id, attempt.row.event_id, WorkerResult.MANUAL_REVIEW, InboxStatus.MANUAL_REVIEW,
        attempts, review_reasons=tuple(reasons),
    )


def _record_failure(
    attempt: _Attempt, *, max_attempts: int, category: str | None, stack_trace: str | None, detail: str | None
) -> WorkerStep:
    """Bounded retry: pending again after the scheduled wait until ``max_attempts``, then failed.

    A failed row waits for approved reprocessing.
    """
    attempts = attempt.row.attempts + 1
    status = InboxStatus.FAILED if attempts >= max_attempts else InboxStatus.PENDING
    _write_attempt(attempt, WorkerResult.FAILED, category=category, stack_trace=stack_trace, detail=detail)
    storage.record_inbox_processing(
        attempt.conn, run_id=attempt.row.run_id, inbox_id=attempt.row.inbox_id, status=status, attempts=attempts,
        last_error=_summary_line(stack_trace) or detail or category or "handler failed without a description",
        processed_at_ms=None,
        next_attempt_at_ms=_retry_at(attempt, attempts) if status is InboxStatus.PENDING else None,
    )
    return WorkerStep(
        attempt.row.inbox_id, attempt.row.event_id, WorkerResult.FAILED, status, attempts,
        exception_category=category,
    )


def _write_attempt(
    attempt: _Attempt,
    result: WorkerResult,
    *,
    category: str | None = None,
    stack_trace: str | None = None,
    detail: str | None = None,
) -> None:
    storage.insert_worker_attempt(
        attempt.conn, run_id=attempt.row.run_id, inbox_id=attempt.row.inbox_id,
        worker_version=attempt.worker_version, started_at_ms=attempt.now_ms, finished_at_ms=attempt.now_ms,
        result=result, exception_category=category, stack_trace=stack_trace, detail=detail,
    )


def _retry_at(attempt: _Attempt, attempts_so_far: int) -> int | None:
    """When the next attempt is due; None (due at once) under an empty schedule."""
    schedule = attempt.retry_backoff_ms
    if not schedule:
        return None
    return attempt.now_ms + schedule[min(attempts_so_far, len(schedule)) - 1]


def _summary_line(stack_trace: str | None) -> str | None:
    """The last non-empty line of a traceback, which names the exception."""
    if not stack_trace:
        return None
    lines = [line.strip() for line in stack_trace.strip().splitlines() if line.strip()]
    return lines[-1][:500] if lines else None
