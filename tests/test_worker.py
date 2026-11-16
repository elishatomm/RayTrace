"""Worker: one transaction per inbox row, bounded retry, re-checks before effect, crash safety."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from conftest import (
    AMOUNT,
    ORDER_1,
    ORDER_2,
    PAY_1,
    PAY_2,
    RUN_A,
    START_MS,
    FailingControlConnection,
    FakeHandler,
    add_order,
    count_rows,
    deliver,
    fulfil_reply,
)
from recovery_workbench import storage, worker
from recovery_workbench.contracts import (
    ContractError,
    InboxStatus,
    PaymentStatus,
    ReceiveOutcome,
    ScopeError,
    WorkerResult,
)

NOW = START_MS + 10_000


class WorkerKilled(BaseException):
    """Stands in for the process dying mid-transaction (not an ordinary handler exception)."""


def only_row(conn: sqlite3.Connection, run_id: str = RUN_A) -> storage.InboxRecord:
    [row] = storage.list_inbox_rows(conn, run_id=run_id)
    return row


def test_a_captured_event_is_fulfilled_with_a_complete_record(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    handler = FakeHandler(version="deploy_SYNTHETIC_0042")
    step = worker.process_next(conn, run_id=RUN_A, handler=handler, now_ms=NOW)
    assert step is not None
    assert (step.result, step.inbox_status, step.attempts) == (WorkerResult.FULFILLED, InboxStatus.PROCESSED, 1)
    [fulfilment] = storage.list_fulfilments(conn, run_id=RUN_A)
    assert (fulfilment.payment_id, fulfilment.amount_paise, fulfilment.currency) == (PAY_1, AMOUNT, "INR")
    assert (fulfilment.via, fulfilment.action_id, fulfilment.fulfilled_at_ms) == ("worker", None, NOW)
    assert storage.require_order(conn, run_id=RUN_A, order_id=ORDER_1).fulfilment_state == "fulfilled"
    row = only_row(conn)
    assert (row.processing_status, row.processed_at_ms, row.last_error) == (InboxStatus.PROCESSED, NOW, None)
    [attempt] = storage.list_worker_attempts(conn, run_id=RUN_A)
    assert (attempt.worker_version, attempt.result) == ("deploy_SYNTHETIC_0042", WorkerResult.FULFILLED)
    assert worker.process_next(conn, run_id=RUN_A, handler=handler, now_ms=NOW) is None


def test_an_event_without_business_effect_is_processed_as_no_effect(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001", status=PaymentStatus.FAILED)
    step = worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)
    assert step is not None
    assert (step.result, step.inbox_status) == (WorkerResult.NO_EFFECT, InboxStatus.PROCESSED)
    assert count_rows(conn, "fulfilments", RUN_A) == 0


def test_handler_failures_are_recorded_and_retried_a_bounded_number_of_times(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    broken = FakeHandler(version="deploy_SYNTHETIC_0007", failure=KeyError("payment"))
    steps = worker.run_worker(conn, run_id=RUN_A, handler=broken, now_ms=NOW, max_attempts=3)
    assert [(s.result, s.inbox_status, s.attempts) for s in steps] == [
        (WorkerResult.FAILED, InboxStatus.PENDING, 1),
        (WorkerResult.FAILED, InboxStatus.PENDING, 2),
        (WorkerResult.FAILED, InboxStatus.FAILED, 3),
    ]
    attempts = storage.list_worker_attempts(conn, run_id=RUN_A)
    assert {(a.worker_version, a.exception_category, a.result) for a in attempts} == {
        ("deploy_SYNTHETIC_0007", "KeyError", WorkerResult.FAILED)
    }
    assert all("merchant_app/payload.py" in (a.stack_trace or "") for a in attempts)
    assert only_row(conn).last_error == "KeyError: 'payment'"
    assert worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW) is None  # failed waits
    assert count_rows(conn, "fulfilments", RUN_A) == 0


INVALID_REPLIES: dict[str, object] = {
    "not_a_mapping": None,
    "unknown_action": {"action": "refund"},
    "fulfil_without_fields": {"action": "fulfil"},
    "amount_as_string": {"action": "fulfil", "order_id": ORDER_1, "payment_id": PAY_1, "amount_paise": "120000",
                         "currency": "INR"},
    "extra_field": {"action": "ignore", "approval": "granted"},
}


@pytest.mark.parametrize("case", sorted(INVALID_REPLIES))
def test_an_invalid_handler_outcome_is_a_recorded_handler_failure(conn, run_a, case: str):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    handler = FakeHandler(reply=lambda _entity: INVALID_REPLIES[case])
    step = worker.process_next(conn, run_id=RUN_A, handler=handler, now_ms=NOW)
    assert step is not None
    assert (step.result, step.exception_category) == (WorkerResult.FAILED, worker.HANDLER_CONTRACT_VIOLATION)
    [attempt] = storage.list_worker_attempts(conn, run_id=RUN_A)
    assert attempt.stack_trace is None
    assert attempt.detail
    assert count_rows(conn, "fulfilments", RUN_A) == 0


def test_different_events_for_one_captured_order_fulfil_it_once(conn, run_a, monkeypatch: pytest.MonkeyPatch):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001", status=PaymentStatus.AUTHORIZED, occurred_at_ms=START_MS + 500)
    deliver(conn, run_a, "evt_SYNTHETIC_0002", occurred_at_ms=START_MS + 1_000)
    # The same capture's bytes under a new event ID, stored by the receiver from before Q32 (a), which had no body
    # lookup: a pair every database it wrote can hold. The two rows share every time, so the worker's projection
    # of the order, made when it fulfils, sees two identical captures.
    with monkeypatch.context() as previous_receiver:
        previous_receiver.setattr(storage, "find_inbox_row_by_body", lambda *_, **__: None)
        deliver(conn, run_a, "evt_SYNTHETIC_0003", occurred_at_ms=START_MS + 1_000)
    capture, resent = storage.list_inbox_rows(conn, run_id=RUN_A)[1:]
    assert (resent.body_sha256, resent.received_at_ms) == (capture.body_sha256, capture.received_at_ms)
    steps = worker.run_worker(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)
    assert [step.result for step in steps] == [
        WorkerResult.NO_EFFECT, WorkerResult.FULFILLED, WorkerResult.ALREADY_FULFILLED
    ]
    assert count_rows(conn, "fulfilments", RUN_A) == 1


def test_an_order_fulfilled_by_another_payment_goes_to_manual_review(conn, run_a):
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    worker.run_worker(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)
    deliver(conn, run_a, "evt_SYNTHETIC_0002", payment_id=PAY_2, occurred_at_ms=NOW + 1_000)
    step = worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW + 5_000)
    assert step is not None
    assert step.result is WorkerResult.MANUAL_REVIEW
    assert step.review_reasons == (worker.HandlerMismatch.FULFILLED_BY_OTHER_PAYMENT.value,)
    assert [f.payment_id for f in storage.list_fulfilments(conn, run_id=RUN_A)] == [PAY_1]


def _with_reply(change: Callable[[dict[str, object]], dict[str, object]]) -> FakeHandler:
    return FakeHandler(reply=lambda entity: change(fulfil_reply(entity)))


def _seed_conflicting_redelivery(conn, run) -> None:
    deliver(conn, run, "evt_SYNTHETIC_0001", amount_paise=AMOUNT + 1)


STOPS: dict[str, tuple[dict[str, object], Callable[..., None] | None, FakeHandler, str]] = {
    # case: (add_order options, extra setup, handler, expected review reason)
    "cancelled": ({"cancelled_at_ms": START_MS + 100}, None, FakeHandler(), "cancelled"),
    "refunded_order": ({"refunded": True}, None, FakeHandler(), "refunded"),
    "provider_amount_differs": ({"amount_paise": AMOUNT - 1}, None, FakeHandler(), "amount_mismatch"),
    "provider_currency_differs": ({"currency": "USD"}, None, FakeHandler(), "currency_mismatch"),
    "conflicting_redelivery": ({}, _seed_conflicting_redelivery, FakeHandler(), "conflicting_evidence"),
    "handler_amount_wrong": ({}, None, _with_reply(lambda r: {**r, "amount_paise": AMOUNT // 100}),
                             "amount_not_order_amount"),
    "handler_currency_wrong": ({}, None, _with_reply(lambda r: {**r, "currency": "USD"}), "currency_not_order_currency"),
    "handler_redirects_order": ({}, None, _with_reply(lambda r: {**r, "order_id": ORDER_2}), "order_not_in_event"),
    "handler_names_other_payment": ({}, None, _with_reply(lambda r: {**r, "payment_id": PAY_2}),
                                    "payment_not_in_event"),
}


@pytest.mark.parametrize("case", sorted(STOPS))
def test_invariant_checks_stop_automatic_fulfilment(conn, run_a, case: str):
    order_options, setup, handler, expected_reason = STOPS[case]
    add_order(conn, run_a, **order_options)  # type: ignore[arg-type]
    add_order(conn, run_a, ORDER_2, payment_ids=(PAY_2,))
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    if setup is not None:
        setup(conn, run_a)
    step = worker.process_next(conn, run_id=RUN_A, handler=handler, now_ms=NOW)
    assert step is not None
    assert (step.result, step.inbox_status) == (WorkerResult.MANUAL_REVIEW, InboxStatus.MANUAL_REVIEW)
    assert expected_reason in step.review_reasons
    assert (only_row(conn).last_error or "").startswith("manual review:")
    assert count_rows(conn, "fulfilments", RUN_A) == 0
    assert {o.fulfilment_state for o in storage.list_orders(conn, run_id=RUN_A)} == {"unfulfilled"}


def test_an_event_for_an_order_this_run_does_not_have_stops_for_review(conn, run_a):
    deliver(conn, run_a, "evt_SYNTHETIC_0001", order_id="order_SYNTHETIC_0404")
    step = worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)
    assert step is not None
    assert "unknown_order" in step.review_reasons


def test_a_conflicting_redelivery_is_never_processed_automatically(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    assert deliver(conn, run_a, "evt_SYNTHETIC_0001", status=PaymentStatus.FAILED).outcome is ReceiveOutcome.CONFLICTING_ID
    steps = worker.run_worker(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)
    assert [step.result for step in steps] == [WorkerResult.MANUAL_REVIEW]  # only the original inbox row exists
    [held] = storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=None)
    assert held.review_status == "pending_review"
    assert count_rows(conn, "fulfilments", RUN_A) == 0


def test_the_worker_only_sees_rows_received_by_its_replay_time(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001", received_at_ms=START_MS + 5_000)
    assert worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=START_MS + 4_999) is None
    step = worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=START_MS + 5_000)
    assert step is not None
    assert step.result is WorkerResult.FULFILLED


def test_the_worker_is_scoped_to_its_run(conn, run_a, run_b):
    add_order(conn, run_a)
    add_order(conn, run_a, ORDER_2, payment_ids=(PAY_2,))
    add_order(conn, run_b)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    deliver(conn, run_b, "evt_SYNTHETIC_0001")
    deliver(conn, run_b, "evt_SYNTHETIC_0002", payment_id=PAY_2, order_id=ORDER_2)  # ORDER_2 exists only in run A
    steps = worker.run_worker(conn, run_id=run_b.run_id, handler=FakeHandler(), now_ms=NOW)
    assert [step.result for step in steps] == [WorkerResult.FULFILLED, WorkerResult.MANUAL_REVIEW]
    assert "unknown_order" in steps[1].review_reasons
    assert count_rows(conn, "fulfilments", RUN_A) == 0
    assert {row.processing_status for row in storage.list_inbox_rows(conn, run_id=RUN_A)} == {InboxStatus.PENDING}
    assert storage.require_order(conn, run_id=RUN_A, order_id=ORDER_2).fulfilment_state == "unfulfilled"
    handler = FakeHandler()
    with pytest.raises(ScopeError):
        worker.process_next(conn, run_id="run_missing", handler=handler, now_ms=NOW)


def test_a_worker_killed_before_commit_leaves_the_work_pending(conn, run_a, open_connection):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    killed = FakeHandler(failure=WorkerKilled())
    with pytest.raises(WorkerKilled):
        worker.process_next(conn, run_id=RUN_A, handler=killed, now_ms=NOW)
    row = only_row(conn)
    assert (row.processing_status, row.attempts) == (InboxStatus.PENDING, 0)
    assert count_rows(conn, "worker_attempts", RUN_A) == 0
    restarted = open_connection()
    steps = worker.run_worker(restarted, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)
    assert [step.result for step in steps] == [WorkerResult.FULFILLED]
    assert count_rows(conn, "fulfilments", RUN_A) == 1


def test_a_failed_worker_commit_rolls_back_every_effect(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    failing = FailingControlConnection(conn, fail_on="COMMIT")
    handler = FakeHandler()
    with pytest.raises(sqlite3.OperationalError, match="injected COMMIT failure"):
        worker.process_next(failing, run_id=RUN_A, handler=handler, now_ms=NOW)  # type: ignore[arg-type]
    assert (only_row(conn).processing_status, count_rows(conn, "worker_attempts", RUN_A)) == (InboxStatus.PENDING, 0)
    assert count_rows(conn, "fulfilments", RUN_A) == 0
    assert storage.require_order(conn, run_id=RUN_A, order_id=ORDER_1).fulfilment_state == "unfulfilled"
    step = worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)
    assert step is not None
    assert step.result is WorkerResult.FULFILLED


def test_a_worker_stopped_after_commit_resumes_and_duplicates_are_harmless(conn, run_a, open_connection):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)  # committed, then the worker "dies"
    restarted = open_connection()
    assert worker.run_worker(restarted, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW) == ()
    assert deliver(restarted, run_a, "evt_SYNTHETIC_0001").outcome is ReceiveOutcome.DUPLICATE  # lost ack retried
    # The provider re-issues the capture under a new ID, 1 ms later: new bytes (the same bytes would be a duplicate).
    deliver(restarted, run_a, "evt_SYNTHETIC_0002", occurred_at_ms=START_MS + 1_001)
    steps = worker.run_worker(restarted, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW + 1_000)
    assert [step.result for step in steps] == [WorkerResult.ALREADY_FULFILLED]
    assert count_rows(conn, "fulfilments", RUN_A) == 1


def test_concurrent_workers_never_fulfil_an_order_twice(conn, run_a, db_path: Path):
    add_order(conn, run_a)
    add_order(conn, run_a, ORDER_2, payment_ids=(PAY_2,))
    for number in range(16):
        deliver(conn, run_a, f"evt_SYNTHETIC_1{number:03d}", occurred_at_ms=START_MS + 1_000 + number)
        deliver(conn, run_a, f"evt_SYNTHETIC_2{number:03d}", payment_id=PAY_2, order_id=ORDER_2,
                occurred_at_ms=START_MS + 1_000 + number)
    start = threading.Barrier(32)

    def work(_: int) -> WorkerResult | None:
        with closing(storage.connect(db_path)) as own:
            start.wait(timeout=30)
            step = worker.process_next(own, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW)
            return None if step is None else step.result

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(work, range(32)))
    assert results.count(WorkerResult.FULFILLED) == 2
    assert results.count(WorkerResult.ALREADY_FULFILLED) == 30
    assert sorted(f.order_id for f in storage.list_fulfilments(conn, run_id=RUN_A)) == [ORDER_1, ORDER_2]


class _UndescribableHandler(FakeHandler):
    def describe_failure(self, exc: BaseException):  # type: ignore[override]
        raise RuntimeError("renderer unavailable")


def test_a_handler_that_cannot_describe_its_failure_fails_loudly_and_keeps_the_row(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    handler = _UndescribableHandler(failure=KeyError("x"))
    with pytest.raises(RuntimeError, match="renderer unavailable"):
        worker.process_next(conn, run_id=RUN_A, handler=handler, now_ms=NOW)
    assert (only_row(conn).processing_status, only_row(conn).attempts) == (InboxStatus.PENDING, 0)


@pytest.mark.parametrize(("now_ms", "max_attempts"), [(-1, 3), (True, 3), (NOW, 0)])
def test_worker_arguments_are_validated(conn, run_a, now_ms, max_attempts):
    handler = FakeHandler()
    with pytest.raises(ContractError):
        worker.process_next(conn, run_id=RUN_A, handler=handler, now_ms=now_ms, max_attempts=max_attempts)


# --------------------------------------------------------------------------- retry backoff (phase 3)

MINUTE = 60_000


def test_failures_are_retried_on_the_backoff_schedule_then_marked_failed(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    broken = FakeHandler(failure=KeyError("payment"))

    def step_at(now_ms: int) -> worker.WorkerStep | None:
        return worker.process_next(conn, run_id=RUN_A, handler=broken, now_ms=now_ms,
                                   retry_backoff_ms=worker.DEFAULT_RETRY_BACKOFF_MS)

    first = step_at(NOW)
    assert first is not None
    assert (first.inbox_status, first.attempts, only_row(conn).next_attempt_at_ms) == (InboxStatus.PENDING, 1, NOW + MINUTE)
    assert step_at(NOW + MINUTE - 1) is None  # not due yet
    second = step_at(NOW + MINUTE)
    assert second is not None
    assert (second.attempts, only_row(conn).next_attempt_at_ms) == (2, NOW + 6 * MINUTE)
    assert step_at(NOW + 6 * MINUTE - 1) is None
    third = step_at(NOW + 6 * MINUTE)
    assert third is not None
    assert (third.inbox_status, third.attempts, only_row(conn).next_attempt_at_ms) == (InboxStatus.FAILED, 3, None)
    started = [a.started_at_ms for a in storage.list_worker_attempts(conn, run_id=RUN_A)]
    assert started == [NOW, NOW + MINUTE, NOW + 6 * MINUTE]  # spread over time, not one instant


class _FailsForPayment(FakeHandler):
    """Raises for one payment's events and behaves normally for every other event."""

    def __init__(self, payment_id: str) -> None:
        super().__init__()
        self._payment_id = payment_id

    def handle(self, event):  # type: ignore[override]
        if event["payload"]["payment"]["entity"]["id"] == self._payment_id:
            raise KeyError("id")
        return super().handle(event)


def test_a_row_waiting_for_its_retry_does_not_block_later_rows(conn, run_a):
    add_order(conn, run_a)
    add_order(conn, run_a, ORDER_2, payment_ids=(PAY_2,))
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    deliver(conn, run_a, "evt_SYNTHETIC_0002", payment_id=PAY_2, order_id=ORDER_2, occurred_at_ms=START_MS + 2_000)
    steps = worker.run_worker(conn, run_id=RUN_A, handler=_FailsForPayment(PAY_1), now_ms=NOW,
                              retry_backoff_ms=worker.DEFAULT_RETRY_BACKOFF_MS)
    assert [(s.event_id, s.result) for s in steps] == [
        ("evt_SYNTHETIC_0001", WorkerResult.FAILED), ("evt_SYNTHETIC_0002", WorkerResult.FULFILLED)
    ]
    retry = worker.run_worker(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=NOW + MINUTE,
                              retry_backoff_ms=worker.DEFAULT_RETRY_BACKOFF_MS)
    assert [(s.event_id, s.result) for s in retry] == [("evt_SYNTHETIC_0001", WorkerResult.FULFILLED)]
    assert {row.next_attempt_at_ms for row in storage.list_inbox_rows(conn, run_id=RUN_A)} == {None}


def test_an_empty_schedule_keeps_retrying_at_once(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    steps = worker.run_worker(conn, run_id=RUN_A, handler=FakeHandler(failure=KeyError("x")), now_ms=NOW,
                              retry_backoff_ms=worker.IMMEDIATE_RETRY)
    assert [s.attempts for s in steps] == [1, 2, 3]


@pytest.mark.parametrize("schedule", [(-1,), (1.5,), (True,)])
def test_retry_schedules_are_validated(conn, run_a, schedule):
    handler = FakeHandler()
    with pytest.raises(ContractError):
        worker.process_next(conn, run_id=RUN_A, handler=handler, now_ms=NOW, retry_backoff_ms=schedule)


def test_only_pending_rows_can_wait_for_a_retry(conn, run_a):
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    row = only_row(conn)
    with pytest.raises(storage.StorageError, match="only a pending"), storage.write_transaction(conn):
        storage.record_inbox_processing(conn, run_id=RUN_A, inbox_id=row.inbox_id, status=InboxStatus.FAILED,
                                        attempts=1, last_error="x", processed_at_ms=None, next_attempt_at_ms=NOW)
