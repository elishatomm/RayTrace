"""A validly signed webhook body resent under a new event ID, with its option (a) applied.

The receiver's HMAC covers the raw body only (``ingestion._signature_problem``). The event ID, which is the inbox's
deduplication key, travels in an unsigned header (``signing.EVENT_ID_HEADER``). So anyone who holds one signed delivery
can send its bytes again, with its signature unchanged, under an event ID of their choosing. Before option (a) the
receiver accepted that as a new event. It caused no second fulfilment and changed no payment's state, but it refreshed
the payment's evidence time, because freshness is measured by when the workbench received evidence (``known_at_ms``),
not by when the provider says it happened (``occurred_at_ms``, signed in the body). A held order could become
plannable that way.

Option (a) is applied: once the signature verifies, a body that the run's integration already holds in its inbox
under another event ID is a duplicate. It is acknowledged (200), so a sender stops retrying, and the response points
at the row that holds the body. It adds no inbox row and no observation, and does not reserve its event ID. Its
delivery attempt is recorded like every other, and its ``detail`` names the event it repeats. The same event ID keeps
today's rules: the same bytes are a duplicate, other bytes a conflict. Migration 4 indexes inbox bodies for the lookup.

The first seven tests are Agent Y's ``test_today_...`` characterisation tests, converted to pin the new behaviour:
the same cases in the same order, renamed for what they now show. The receiver, worker and projection cases are built
by hand with the core helpers. The held-order and planner effects use the world of the orchestrator's probes
(worker_backlog, the demo seed 101), seeded twice: one copy takes the resends, the other only the same clock step. The
tests after them cover the receiver's new branch, the same-ID cases and the migration.

The last ones cover Q34. A body held in ``conflicting_deliveries`` (an event ID reused with other bytes) is a duplicate
too when it arrives under a fresh event ID; before, it was accepted and processed, past the review that holds it.
Migration 5 indexes held bodies for that lookup.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import (
    INTEGRATION_A,
    INTEGRATION_B,
    MERCHANT_A,
    MERCHANT_B,
    ORDER_1,
    PAY_1,
    PAY_2,
    RUN_A,
    RUN_B,
    SECRET_A,
    SECRET_B,
    START_MS,
    FailingControlConnection,
    FakeHandler,
    SeededRun,
    add_order,
    count_rows,
    payment_event,
    seed_run,
)
from test_order_cases import planner_disagreements
from test_webhook_conformance import PLAN_TTL_MS, STALE_AFTER_MS, validated_fix

from recovery_workbench import ingestion, order_cases, plans, state, storage, worker
from recovery_workbench.contracts import (
    CaseState,
    ExclusionReason,
    NextStep,
    PaymentObservation,
    PaymentStatus,
    ReceiveOutcome,
    WorkerResult,
    sha256_hex,
)
from recovery_workbench.simulator import driver, merchant, runtime, scenarios, signing
from recovery_workbench.simulator.world import World, generate_world

MINUTE = 60_000
STALE_SNAPSHOT = ExclusionReason.STALE_SNAPSHOT.value
BODY_INDEX = "webhook_inbox_by_body"
HELD_BODY_INDEX = "conflicting_deliveries_by_body"

Delivery = tuple[dict[str, str], bytes]  # (headers, raw body), exactly as sent


def signed(run: SeededRun, event_id: str, **event_fields: object) -> Delivery:
    return signing.signed_delivery(payment_event(**event_fields), event_id=event_id, secret_ref=run.secret_ref)


def send(conn: sqlite3.Connection, run: SeededRun, delivery: Delivery, *, at: int) -> ingestion.DeliveryResponse:
    headers, raw_body = delivery
    return ingestion.receive_webhook(conn, run_id=run.run_id, integration_id=run.integration_id, headers=headers,
                                     raw_body=raw_body, received_at_ms=at)


def resend(
    conn: sqlite3.Connection, run: SeededRun, delivery: Delivery, event_id: str, *, at: int
) -> ingestion.DeliveryResponse:
    """The delivery's exact bytes and signature header again; only the event ID header is replaced."""
    headers, raw_body = delivery
    return send(conn, run, ({**headers, signing.EVENT_ID_HEADER: event_id}, raw_body), at=at)


def repeats(row: storage.InboxRecord | ingestion.DeliveryResponse, event_id: str) -> str:
    """The delivery attempt's detail for a resend of the body that ``row`` stored as ``event_id``."""
    return f"body already received as event {event_id} (inbox row {row.inbox_id})"


def holds(conflict: storage.ConflictingDeliveryRecord) -> str:
    """The delivery attempt's detail for a body resent under a fresh event ID while ``conflict`` holds it."""
    return f"body already held as conflicting delivery {conflict.id} of event {conflict.event_id}"


def attempts(conn: sqlite3.Connection, run_id: str) -> list[tuple[str | None, int, ReceiveOutcome, int, str | None]]:
    return [(row.event_id, row.attempt_no, row.outcome, row.http_status, row.detail)
            for row in storage.list_delivery_attempts(conn, run_id=run_id)]


# --------------------------------------------------------------------------- the receiver, the worker, one payment


def test_a_signed_body_resent_under_a_new_event_id_is_a_duplicate_of_the_stored_event(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a)
    capture = signed(run_a, "evt_original")
    original = send(conn, run_a, capture, at=START_MS + 2_000)
    repeated = send(conn, run_a, capture, at=START_MS + 3_000)
    resent = resend(conn, run_a, capture, "evt_resent", at=START_MS + 4_000)
    # The same request twice is one event, because the inbox deduplicates by event ID ...
    assert (original.outcome, repeated.outcome, repeated.inbox_id) == (
        ReceiveOutcome.ACCEPTED, ReceiveOutcome.DUPLICATE, original.inbox_id)
    # ... and the same bytes and signature under a new ID are that event again: acknowledged, pointing at its row.
    assert (resent.outcome, resent.http_status, resent.event_id, resent.inbox_id) == (
        ReceiveOutcome.DUPLICATE, 200, "evt_resent", original.inbox_id)
    body_sha256 = sha256_hex(capture[1])
    inbox = storage.list_inbox_rows(conn, run_id=run_a.run_id)
    assert [(row.inbox_id, row.event_id, row.body_sha256) for row in inbox] == [
        (original.inbox_id, "evt_original", body_sha256)]
    observations = storage.list_observations(conn, run_id=run_a.run_id, as_of_ms=None)
    occurred_at = START_MS + 1_000  # payment_event's default, signed in the body
    assert [(item.source_ref, item.occurred_at_ms, item.known_at_ms) for item in observations] == [
        ("evt_original", occurred_at, START_MS + 2_000)]
    # Every delivery is still recorded, under the event ID it carried; the resend's row names the event it repeats.
    assert attempts(conn, run_a.run_id) == [
        ("evt_original", 1, ReceiveOutcome.ACCEPTED, 200, None),
        ("evt_original", 2, ReceiveOutcome.DUPLICATE, 200, None),
        ("evt_resent", 1, ReceiveOutcome.DUPLICATE, 200, repeats(original, "evt_original")),
    ]
    assert {row.body_sha256 for row in storage.list_delivery_attempts(conn, run_id=run_a.run_id)} == {body_sha256}


def test_a_resent_capture_of_a_fulfilled_order_adds_no_work_and_no_second_fulfilment(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a)
    capture = signed(run_a, "evt_captured")
    received = send(conn, run_a, capture, at=START_MS + 2_000)
    [processed] = worker.run_worker(conn, run_id=run_a.run_id, handler=FakeHandler(), now_ms=START_MS + 3_000)
    fulfilments = storage.list_fulfilments(conn, run_id=run_a.run_id)
    resent = resend(conn, run_a, capture, "evt_captured_resent", at=START_MS + 4_000)
    assert (processed.result, resent.outcome, resent.inbox_id) == (
        WorkerResult.FULFILLED, ReceiveOutcome.DUPLICATE, received.inbox_id)
    # Nothing new reached the inbox, so the worker has nothing to settle, and the order keeps its one fulfilment.
    assert worker.run_worker(conn, run_id=run_a.run_id, handler=FakeHandler(), now_ms=START_MS + 5_000) == ()
    assert len(fulfilments) == 1
    assert storage.list_fulfilments(conn, run_id=run_a.run_id) == fulfilments
    case = order_cases.classify_orders(conn, run_id=run_a.run_id, as_of_ms=START_MS + 5_000,
                                       stale_after_ms=STALE_AFTER_MS).cases[0]
    assert (case.order_id, case.state, case.coverage.captured_events_received) == (ORDER_1, CaseState.COMPLETE, 1)


def test_a_resent_old_failure_of_a_captured_payment_changes_nothing_not_even_its_evidence_time(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """The payment failed, then authorised late and was captured; its old failure arrives again under a new ID."""
    add_order(conn, run_a)
    failure = signed(run_a, "evt_failed", status=PaymentStatus.FAILED, occurred_at_ms=START_MS + 1_000)
    stored = send(conn, run_a, failure, at=START_MS + 1_050)
    captured_at = START_MS + 10 * MINUTE
    send(conn, run_a, signed(run_a, "evt_captured", occurred_at_ms=captured_at), at=captured_at + 50)
    at = START_MS + 20 * MINUTE

    def projected() -> state.OrderProjection:
        return state.load_order_projection(conn, run_a.run_id, ORDER_1, at, stale_after_ms=STALE_AFTER_MS)

    before = projected()
    resent = resend(conn, run_a, failure, "evt_failed_resent", at=at)
    after = projected()
    assert (resent.outcome, resent.inbox_id) == (ReceiveOutcome.DUPLICATE, stored.inbox_id)
    [payment_before] = before.payments
    assert (payment_before.status, payment_before.late_authorization, payment_before.conflict_reasons) == (
        PaymentStatus.CAPTURED, True, ())
    # Status, conflicts, amounts, exclusions and the time the payment was last heard of are all unchanged: the
    # resend added no observation, so the capture is still the newest word on the payment.
    assert after == before
    assert after.exclusions == ()
    assert payment_before.last_known_at_ms == captured_at + 50


def test_a_resend_does_not_refresh_the_evidence_time_of_the_payment_it_names(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """A failed attempt the provider has said nothing new about for over the threshold, beside a fresh capture."""
    failed_attempt, captured_attempt = PAY_1, PAY_2
    resend_at = START_MS + 2 * STALE_AFTER_MS
    add_order(conn, run_a, payment_ids=(failed_attempt, captured_attempt))
    failure = signed(run_a, "evt_failed", status=PaymentStatus.FAILED, payment_id=failed_attempt,
                     occurred_at_ms=START_MS + 1_000)
    capture = signed(run_a, "evt_captured", payment_id=captured_attempt, occurred_at_ms=resend_at - STALE_AFTER_MS // 2)
    send(conn, run_a, failure, at=START_MS + 1_050)
    send(conn, run_a, capture, at=resend_at - STALE_AFTER_MS // 2 + 50)

    def projected() -> state.OrderProjection:
        return state.load_order_projection(conn, run_a.run_id, ORDER_1, resend_at, stale_after_ms=STALE_AFTER_MS)

    held = projected()  # also the control: the same replay time, without any resend
    capture_resent = resend(conn, run_a, capture, "evt_captured_resent", at=resend_at)
    after_capture_resend = projected()
    failure_resent = resend(conn, run_a, failure, "evt_failed_resent", at=resend_at)
    after_failure_resend = projected()
    assert [(item.outcome, item.http_status) for item in (capture_resent, failure_resent)] == [
        (ReceiveOutcome.DUPLICATE, 200)] * 2
    assert held.exclusions == (ExclusionReason.STALE_SNAPSHOT,)
    # Neither resend is new evidence: the failed attempt's own old body, sent again, leaves it stale and the order held.
    assert after_capture_resend == held
    assert after_failure_resend == held
    assert not after_failure_resend.is_recovery_eligible
    stale = next(payment for payment in after_failure_resend.payments if payment.payment_id == failed_attempt)
    assert (stale.status, stale.stale, stale.last_known_at_ms) == (PaymentStatus.FAILED, True, START_MS + 1_050)
    assert [(item.source_ref, item.occurred_at_ms, item.known_at_ms) for item in stale.history] == [
        ("evt_failed", START_MS + 1_000, START_MS + 1_050)]


# --------------------------------------------------------------------------- the held orders of a seeded world

BACKLOG_SEED = 101  # the demo seed, and the world of the orchestrator's probes
HELD_ORDERS = 5  # in that world at its cutoff, the orders held by stale_snapshot alone


@dataclass(frozen=True)
class BacklogResend:
    """worker_backlog seeded twice and stepped one millisecond past its cutoff; only the first copy got resends."""

    resend_at: int
    held: tuple[order_cases.OrderCase, ...]  # at the cutoff: the orders whose only blocker is stale_snapshot
    resent_rows: tuple[storage.InboxRecord, ...]  # per held order, the inbox row of its stale payment
    responses: tuple[ingestion.DeliveryResponse, ...]
    resent_observations: tuple[PaymentObservation, ...]  # what the resends added
    resent_attempts: tuple[storage.DeliveryAttemptRecord, ...]  # the resends' own delivery attempts
    resent_inbox: tuple[storage.InboxRecord, ...]  # after the resends and the worker, in each copy
    control_inbox: tuple[storage.InboxRecord, ...]
    worker_steps: tuple[worker.WorkerStep, ...]  # the deployed worker at the resend time, in the resent copy
    control_worker_steps: tuple[worker.WorkerStep, ...]
    resent: order_cases.OrderQueue
    control: order_cases.OrderQueue
    resent_plan: plans.PlanPreview
    control_plan: plans.PlanPreview
    resent_fulfilments: tuple[storage.FulfilmentRecord, ...]
    control_fulfilments: tuple[storage.FulfilmentRecord, ...]


def classify(conn: sqlite3.Connection, run_id: str, at: int) -> order_cases.OrderQueue:
    return order_cases.classify_orders(conn, run_id=run_id, as_of_ms=at, stale_after_ms=STALE_AFTER_MS)


def stale_payment_row(conn: sqlite3.Connection, run_id: str, case: order_cases.OrderCase) -> storage.InboxRecord:
    """The inbox row of the order's one stale payment; there must be exactly one of each."""
    [stale_payment] = [payment.payment_id for payment in case.payments if payment.stale]
    [row] = [row for row in storage.list_inbox_rows(conn, run_id=run_id)
             if ingestion.parse_payment_event(row.raw_body).payment.id == stale_payment]
    return row


def resend_stored(
    conn: sqlite3.Connection, run_id: str, row: storage.InboxRecord, event_id: str, *, at: int
) -> ingestion.DeliveryResponse:
    """A stored body sent again under ``event_id``, with the signature the provider sent it with.

    The inbox keeps bodies, not headers, so the test recomputes the signature with the fixture secret. The HMAC is
    deterministic, so that is the header the provider sent: a real replayer holding the original delivery has it
    already and needs no secret.
    """
    headers = {signing.EVENT_ID_HEADER: event_id,
               signing.SIGNATURE_HEADER: signing.sign_body(row.raw_body, merchant.PROVIDER_SIGNING_REF)}
    return ingestion.receive_webhook(conn, run_id=run_id, integration_id=row.integration_id, headers=headers,
                                     raw_body=row.raw_body, received_at_ms=at)


def run_deployed_worker(conn: sqlite3.Connection, run_id: str, at: int) -> tuple[worker.WorkerStep, ...]:
    return worker.run_worker(conn, run_id=run_id, handler=runtime.active_merchant(conn, run_id=run_id, at_ms=at),
                             now_ms=at, retry_backoff_ms=worker.DEFAULT_RETRY_BACKOFF_MS)


def reprocess_plan(conn: sqlite3.Connection, world: World, at: int) -> plans.PlanPreview:
    """A real reprocess plan at ``at``, after a rules diagnosis and a passing validation of the repairing fix."""
    deployed = runtime.active_merchant(conn, run_id=world.run_id, at_ms=at)
    validation = validated_fix(conn, world, deployed, at)
    return plans.build_reprocess_plan(conn, run_id=world.run_id, fix_validation_id=validation.validation_id,
                                      deployed=deployed, now_ms=at, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS)


@pytest.fixture(scope="module")
def backlog(tmp_path_factory: pytest.TempPathFactory) -> BacklogResend:
    world = generate_world(scenarios.WORKER_BACKLOG, BACKLOG_SEED)
    directory = tmp_path_factory.mktemp("resend_backlog")
    with (closing(storage.connect(directory / "resent.sqlite")) as conn,
          closing(storage.connect(directory / "control.sqlite")) as control):
        for database in (conn, control):
            driver.seed_world(database, world, oracle_dir=None)
        held = tuple(case for case in classify(conn, world.run_id, world.cutoff_ms).cases
                     if case.blockers == (STALE_SNAPSHOT,))
        resend_at = world.cutoff_ms + 1  # the probe's one clock step; the control takes the same step
        for database in (conn, control):
            with storage.write_transaction(database):
                storage.advance_clock(database, world.run_id, resend_at)
        rows = tuple(stale_payment_row(conn, world.run_id, case) for case in held)
        responses = tuple(resend_stored(conn, world.run_id, row, f"evt_resend_{index}", at=resend_at)
                          for index, row in enumerate(rows))
        worker_steps = run_deployed_worker(conn, world.run_id, resend_at)
        control_worker_steps = run_deployed_worker(control, world.run_id, resend_at)
        resent, control_queue = classify(conn, world.run_id, resend_at), classify(control, world.run_id, resend_at)
        return BacklogResend(
            resend_at=resend_at,
            held=held,
            resent_rows=rows,
            responses=responses,
            resent_observations=tuple(
                item for item in storage.list_observations(conn, run_id=world.run_id, as_of_ms=None)
                if item.source_ref.startswith("evt_resend_")),
            resent_attempts=tuple(item for item in storage.list_delivery_attempts(conn, run_id=world.run_id)
                                  if (item.event_id or "").startswith("evt_resend_")),
            resent_inbox=storage.list_inbox_rows(conn, run_id=world.run_id),
            control_inbox=storage.list_inbox_rows(control, run_id=world.run_id),
            worker_steps=worker_steps,
            control_worker_steps=control_worker_steps,
            resent=resent,
            control=control_queue,
            resent_plan=reprocess_plan(conn, world, resend_at),
            control_plan=reprocess_plan(control, world, resend_at),
            resent_fulfilments=storage.list_fulfilments(conn, run_id=world.run_id),
            control_fulfilments=storage.list_fulfilments(control, run_id=world.run_id),
        )


def test_resending_each_stale_failed_attempts_own_body_leaves_the_held_orders_held(backlog: BacklogResend) -> None:
    held = [case.order_id for case in backlog.held]
    assert len(held) == HELD_ORDERS
    for case in backlog.held:
        # Each holds a capture whose event the faulty deployed handler could not process, beside an earlier failed
        # attempt the provider has said nothing new about for longer than the threshold.
        assert (case.state, case.next_step) == (CaseState.PAYMENT_STATE_UNRESOLVED, NextStep.REFRESH_THEN_REEVALUATE)
        assert sorted((payment.status, payment.stale) for payment in case.payments) == [
            (PaymentStatus.CAPTURED, False), (PaymentStatus.FAILED, True)]
        assert case.capture_processing.unsettled_inbox_ids
    assert [row.event_type for row in backlog.resent_rows] == ["payment.failed"] * HELD_ORDERS
    # Each resend is acknowledged as a duplicate of the stale payment's own stored event.
    assert [(response.outcome, response.http_status, response.inbox_id) for response in backlog.responses] == [
        (ReceiveOutcome.DUPLICATE, 200, row.inbox_id) for row in backlog.resent_rows]
    # The queue is the control's: every case, the five held ones included, and every total.
    assert backlog.resent.cases == backlog.control.cases
    assert backlog.resent.totals == backlog.control.totals
    resent = {case.order_id: case for case in backlog.resent.cases}
    for order_id in held:
        assert (resent[order_id].state, resent[order_id].blockers) == (
            CaseState.PAYMENT_STATE_UNRESOLVED, (STALE_SNAPSHOT,))
    # What the queue read differs by the five delivery attempts alone, none of which has an inbox row of its own.
    read, control_read = backlog.resent.coverage.model_dump(), backlog.control.coverage.model_dump()
    assert {name: read[name] - control_read[name] for name in read if read[name] != control_read[name]} == {
        "delivery_attempts": HELD_ORDERS, "deliveries_without_inbox_row": HELD_ORDERS}


def test_the_resends_add_no_inbox_row_no_observation_and_no_worker_step(backlog: BacklogResend) -> None:
    resent = {payment.payment_id: payment for case in backlog.resent.cases for payment in case.payments}
    control = {payment.payment_id: payment for case in backlog.control.cases for payment in case.payments}
    # Every payment is the control's, freshness included, so the five resent payments are still failed and stale.
    assert resent == control
    for row in backlog.resent_rows:
        payment_id = ingestion.parse_payment_event(row.raw_body).payment.id
        assert (resent[payment_id].status, resent[payment_id].stale) == (PaymentStatus.FAILED, True)
    assert backlog.resent_observations == ()
    assert backlog.resent_inbox == backlog.control_inbox
    assert backlog.resent_fulfilments == backlog.control_fulfilments
    # Nothing new is due, so the deployed worker (payload@2, which raises on every event) has nothing to do either way.
    assert (backlog.worker_steps, backlog.control_worker_steps) == ((), ())
    # Each resend is still recorded, under its own event ID, as a duplicate naming the stored event it repeats.
    assert [(item.event_id, item.attempt_no, item.outcome, item.http_status, item.body_sha256, item.detail)
            for item in backlog.resent_attempts] == [
        (f"evt_resend_{index}", 1, ReceiveOutcome.DUPLICATE, 200, row.body_sha256, repeats(row, row.event_id))
        for index, row in enumerate(backlog.resent_rows)]


def test_a_reprocess_plan_built_after_the_resends_is_the_control_plan(backlog: BacklogResend) -> None:
    held = {case.order_id for case in backlog.held}
    for preview in (backlog.resent_plan, backlog.control_plan):
        assert not held & {target.id for target in preview.targets}
        held_by_plan = {item.order_id: item.reasons for item in preview.excluded if item.order_id in held}
        assert held_by_plan == dict.fromkeys(held, (STALE_SNAPSHOT,))
    # Targets, value, exclusions, superseded events and dry runs are all the control's; only the plan record differs.
    assert backlog.resent_plan.model_dump(exclude={"plan"}) == backlog.control_plan.model_dump(exclude={"plan"})
    assert (backlog.control_plan.target_count, backlog.resent_plan.target_count) == (98, 98)
    # Phase 11's cross-check: in each copy the queue and the planner agree.
    assert planner_disagreements(backlog.resent, backlog.resent_plan) == {}
    assert planner_disagreements(backlog.control, backlog.control_plan) == {}


# --------------------------------------------------------------------------- the receiver's new branch


def test_every_resend_is_a_duplicate_of_the_original_row_however_often_and_under_however_many_ids(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a)
    capture = signed(run_a, "evt_original")
    original = send(conn, run_a, capture, at=START_MS + 2_000)
    resends = [resend(conn, run_a, capture, event_id, at=START_MS + 3_000 + step)
               for step, event_id in enumerate(("evt_x", "evt_y", "evt_x"))]
    assert [(item.outcome, item.event_id, item.inbox_id) for item in resends] == [
        (ReceiveOutcome.DUPLICATE, "evt_x", original.inbox_id), (ReceiveOutcome.DUPLICATE, "evt_y", original.inbox_id),
        (ReceiveOutcome.DUPLICATE, "evt_x", original.inbox_id)]
    # attempt_no still counts the attempts of each event ID, as for every delivery.
    assert attempts(conn, run_a.run_id)[1:] == [
        ("evt_x", 1, ReceiveOutcome.DUPLICATE, 200, repeats(original, "evt_original")),
        ("evt_y", 1, ReceiveOutcome.DUPLICATE, 200, repeats(original, "evt_original")),
        ("evt_x", 2, ReceiveOutcome.DUPLICATE, 200, repeats(original, "evt_original")),
    ]
    assert (count_rows(conn, "webhook_inbox", RUN_A), count_rows(conn, "payment_observations", RUN_A)) == (1, 1)


def test_concurrent_copies_of_one_body_under_different_event_ids_store_it_once(
    conn: sqlite3.Connection, run_a: SeededRun, db_path: Path
) -> None:
    """The lookup runs in the receipt's write transaction, so concurrent first deliveries cannot all miss it."""
    add_order(conn, run_a)
    capture = signed(run_a, "evt_copy_00")

    def receive_once(number: int) -> ReceiveOutcome:
        with closing(storage.connect(db_path)) as own:
            return resend(own, run_a, capture, f"evt_copy_{number:02d}", at=START_MS + 2_000).outcome

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(receive_once, range(16)))
    assert (outcomes.count(ReceiveOutcome.ACCEPTED), outcomes.count(ReceiveOutcome.DUPLICATE)) == (1, 15)
    assert (count_rows(conn, "webhook_inbox", RUN_A), count_rows(conn, "payment_observations", RUN_A)) == (1, 1)


def test_a_resend_does_not_reserve_its_event_id_for_the_event_that_later_carries_it(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """Before option (a), a body resent under a later event's ID took that ID, and the event became a conflict."""
    add_order(conn, run_a)
    failure = signed(run_a, "evt_failed", status=PaymentStatus.FAILED)
    stored = send(conn, run_a, failure, at=START_MS + 2_000)
    squatter = resend(conn, run_a, failure, "evt_next", at=START_MS + 3_000)
    capture = signed(run_a, "evt_next", occurred_at_ms=START_MS + 4_000)
    genuine = send(conn, run_a, capture, at=START_MS + 4_050)
    assert (squatter.outcome, squatter.inbox_id) == (ReceiveOutcome.DUPLICATE, stored.inbox_id)
    assert (genuine.outcome, genuine.event_id) == (ReceiveOutcome.ACCEPTED, "evt_next")
    assert [(row.event_id, row.body_sha256) for row in storage.list_inbox_rows(conn, run_id=RUN_A)] == [
        ("evt_failed", sha256_hex(failure[1])), ("evt_next", sha256_hex(capture[1]))]
    assert storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=None) == ()
    assert attempts(conn, RUN_A)[1:] == [
        ("evt_next", 1, ReceiveOutcome.DUPLICATE, 200, repeats(stored, "evt_failed")),
        ("evt_next", 2, ReceiveOutcome.ACCEPTED, 200, None),
    ]


def test_only_the_same_run_and_integration_can_hold_the_body_a_resend_repeats(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    # Another integration of the same run, and another run whose integration has the same ID as run A's.
    second = SeededRun(RUN_A, MERCHANT_A, "int_SYNTHETIC_SECOND", SECRET_B)
    with storage.write_transaction(conn):
        storage.add_integration(conn, storage.IntegrationRecord(
            run_id=RUN_A, integration_id=second.integration_id, secret_ref=second.secret_ref, max_body_bytes=65_536))
    other_run = seed_run(conn, RUN_B, MERCHANT_B, INTEGRATION_A, SECRET_B)
    for run in (run_a, other_run):
        add_order(conn, run)
    first = send(conn, run_a, signed(run_a, "evt_first"), at=START_MS + 2_000)
    # The same bytes, each signed with its receiver's secret, under new event IDs: new to that integration or run.
    in_other_integration = send(conn, second, signed(second, "evt_second"), at=START_MS + 3_000)
    in_other_run = send(conn, other_run, signed(other_run, "evt_third"), at=START_MS + 3_000)
    assert signed(run_a, "x")[1] == signed(second, "x")[1] == signed(other_run, "x")[1]
    assert [item.outcome for item in (first, in_other_integration, in_other_run)] == [ReceiveOutcome.ACCEPTED] * 3
    assert [(row.integration_id, row.event_id) for row in storage.list_inbox_rows(conn, run_id=RUN_A)] == [
        (INTEGRATION_A, "evt_first"), ("int_SYNTHETIC_SECOND", "evt_second")]
    assert [(row.integration_id, row.event_id) for row in storage.list_inbox_rows(conn, run_id=RUN_B)] == [
        (INTEGRATION_A, "evt_third")]


def test_verification_still_comes_first_so_an_unverified_copy_of_a_stored_body_is_rejected(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a)
    headers, raw_body = signed(run_a, "evt_original")
    send(conn, run_a, (headers, raw_body), at=START_MS + 2_000)
    forged = {signing.EVENT_ID_HEADER: "evt_forged", signing.SIGNATURE_HEADER: "0" * 64}
    unlabelled = {signing.SIGNATURE_HEADER: headers[signing.SIGNATURE_HEADER]}
    malformed = {**headers, signing.EVENT_ID_HEADER: "evt resent"}
    answers = [send(conn, run_a, (sent, raw_body), at=START_MS + 3_000) for sent in (forged, unlabelled, malformed)]
    assert [(item.outcome, item.http_status, item.inbox_id) for item in answers] == [
        (ReceiveOutcome.REJECTED_SIGNATURE, 401, None), (ReceiveOutcome.REJECTED_SCHEMA, 400, None),
        (ReceiveOutcome.REJECTED_SCHEMA, 400, None)]
    assert count_rows(conn, "webhook_inbox", RUN_A) == 1


def test_a_resend_whose_receipt_cannot_commit_is_never_acknowledged(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a)
    capture = signed(run_a, "evt_original")
    original = send(conn, run_a, capture, at=START_MS + 2_000)
    failing = FailingControlConnection(conn, fail_on="COMMIT")
    with pytest.raises(ingestion.ReceiptNotDurableError, match="respond 503"):
        resend(failing, run_a, capture, "evt_resent", at=START_MS + 3_000)  # type: ignore[arg-type]
    assert count_rows(conn, "delivery_attempts", RUN_A) == 1  # the resend's attempt row rolled back with its receipt
    retried = resend(conn, run_a, capture, "evt_resent", at=START_MS + 4_000)
    assert (retried.outcome, retried.inbox_id) == (ReceiveOutcome.DUPLICATE, original.inbox_id)


# --------------------------------------------------------------------------- the same event ID, unchanged


def test_the_same_event_id_keeps_its_rules_before_any_body_lookup(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    """Same bytes: a duplicate. Other bytes: a conflict, held once per body, even if another event holds those bytes."""
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    first = signed(run_a, "evt_first")
    other = signed(run_a, "evt_other", payment_id=PAY_2)
    stored_first = send(conn, run_a, first, at=START_MS + 2_000)
    stored_other = send(conn, run_a, other, at=START_MS + 2_100)
    again = send(conn, run_a, first, at=START_MS + 3_000)
    # evt_first with the bytes evt_other already holds: the event ID's rule decides, so it is a conflict.
    reused = [resend(conn, run_a, other, "evt_first", at=START_MS + 4_000 + step) for step in range(2)]
    assert (again.outcome, again.inbox_id) == (ReceiveOutcome.DUPLICATE, stored_first.inbox_id)
    assert [(item.outcome, item.http_status, item.inbox_id) for item in reused] == [
        (ReceiveOutcome.CONFLICTING_ID, 200, stored_first.inbox_id)] * 2
    [held] = storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=None)
    assert (held.id, held.event_id, held.body_sha256, held.original_sha256, held.differing_fields) == (
        reused[0].conflicting_delivery_id, "evt_first", sha256_hex(other[1]), sha256_hex(first[1]), ("payment_id",))
    assert reused[1].conflicting_delivery_id == held.id
    assert [row.inbox_id for row in storage.list_inbox_rows(conn, run_id=RUN_A)] == [
        stored_first.inbox_id, stored_other.inbox_id]
    assert [detail for *_, detail in attempts(conn, RUN_A)] == [None] * 5


# --------------------------------------------------------------------------- a body held as a conflicting delivery


def test_a_held_conflicting_body_resent_under_a_fresh_event_id_is_a_duplicate_and_stays_held(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a)
    original = send(conn, run_a, signed(run_a, "evt_first"), at=START_MS + 2_000)
    # evt_first again, now saying the payment failed: held for review, never processed.
    conflicting = signed(run_a, "evt_first", status=PaymentStatus.FAILED)
    held = send(conn, run_a, conflicting, at=START_MS + 3_000)
    resent = resend(conn, run_a, conflicting, "evt_fresh", at=START_MS + 4_000)
    assert (held.outcome, held.inbox_id) == (ReceiveOutcome.CONFLICTING_ID, original.inbox_id)
    # Acknowledged as a duplicate of the held delivery. No inbox row holds these bytes, so none is pointed at.
    assert (resent.outcome, resent.http_status, resent.event_id, resent.inbox_id, resent.conflicting_delivery_id) == (
        ReceiveOutcome.DUPLICATE, 200, "evt_fresh", None, held.conflicting_delivery_id)
    [conflict] = storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=None)
    assert (conflict.id, conflict.event_id, conflict.review_status) == (
        held.conflicting_delivery_id, "evt_first", "pending_review")
    assert [row.event_id for row in storage.list_inbox_rows(conn, run_id=RUN_A)] == ["evt_first"]
    assert [item.source_ref for item in storage.list_observations(conn, run_id=RUN_A, as_of_ms=None)] == ["evt_first"]
    assert attempts(conn, RUN_A) == [
        ("evt_first", 1, ReceiveOutcome.ACCEPTED, 200, None),
        ("evt_first", 2, ReceiveOutcome.CONFLICTING_ID, 200, None),
        ("evt_fresh", 1, ReceiveOutcome.DUPLICATE, 200, holds(conflict)),
    ]
    # The held bytes never reach the worker, whose only row stops for review because of the conflict; and the fresh
    # event ID stays free for the event that later carries it.
    steps = worker.run_worker(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=START_MS + 5_000)
    assert [(step.event_id, step.result) for step in steps] == [("evt_first", WorkerResult.MANUAL_REVIEW)]
    genuine = send(conn, run_a, signed(run_a, "evt_fresh", occurred_at_ms=START_MS + 6_000), at=START_MS + 6_050)
    assert (genuine.outcome, genuine.event_id) == (ReceiveOutcome.ACCEPTED, "evt_fresh")


def test_the_same_event_id_keeps_its_conflict_rules_before_the_held_body_lookup(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """A held body under the ID it reused is that conflict again; under another stored event's ID, a conflict there."""
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    first = send(conn, run_a, signed(run_a, "evt_first"), at=START_MS + 2_000)
    other = send(conn, run_a, signed(run_a, "evt_other", payment_id=PAY_2), at=START_MS + 2_100)
    conflicting = signed(run_a, "evt_first", status=PaymentStatus.FAILED)
    held = send(conn, run_a, conflicting, at=START_MS + 3_000)
    again = send(conn, run_a, conflicting, at=START_MS + 4_000)
    elsewhere = resend(conn, run_a, conflicting, "evt_other", at=START_MS + 5_000)
    assert [(item.outcome, item.http_status, item.inbox_id) for item in (held, again, elsewhere)] == [
        (ReceiveOutcome.CONFLICTING_ID, 200, first.inbox_id), (ReceiveOutcome.CONFLICTING_ID, 200, first.inbox_id),
        (ReceiveOutcome.CONFLICTING_ID, 200, other.inbox_id)]
    # Held once per event ID and body: the repeat points at its held row, and the other event ID holds its own.
    assert again.conflicting_delivery_id == held.conflicting_delivery_id
    assert [(item.id, item.event_id, item.body_sha256)
            for item in storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=None)] == [
        (held.conflicting_delivery_id, "evt_first", sha256_hex(conflicting[1])),
        (elsewhere.conflicting_delivery_id, "evt_other", sha256_hex(conflicting[1]))]
    assert [row.event_id for row in storage.list_inbox_rows(conn, run_id=RUN_A)] == ["evt_first", "evt_other"]
    assert [detail for *_, detail in attempts(conn, RUN_A)] == [None] * 5


def test_only_the_same_run_and_integration_can_hold_the_body_a_fresh_event_id_repeats(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    # Another integration of the same run, and another run whose integration has the same ID as run A's.
    second = SeededRun(RUN_A, MERCHANT_A, "int_SYNTHETIC_SECOND", SECRET_B)
    with storage.write_transaction(conn):
        storage.add_integration(conn, storage.IntegrationRecord(
            run_id=RUN_A, integration_id=second.integration_id, secret_ref=second.secret_ref, max_body_bytes=65_536))
    other_run = seed_run(conn, RUN_B, MERCHANT_B, INTEGRATION_A, SECRET_B)
    for run in (run_a, other_run):
        add_order(conn, run)
    send(conn, run_a, signed(run_a, "evt_first"), at=START_MS + 2_000)
    held = send(conn, run_a, signed(run_a, "evt_first", status=PaymentStatus.FAILED), at=START_MS + 3_000)
    # The held bytes under a fresh event ID, each signed with its receiver's secret: a duplicate only where held.
    fresh = [send(conn, run, signed(run, "evt_fresh", status=PaymentStatus.FAILED), at=START_MS + 4_000)
             for run in (run_a, second, other_run)]
    assert [(item.outcome, item.conflicting_delivery_id) for item in fresh] == [
        (ReceiveOutcome.DUPLICATE, held.conflicting_delivery_id), (ReceiveOutcome.ACCEPTED, None),
        (ReceiveOutcome.ACCEPTED, None)]
    assert [(row.integration_id, row.event_id) for row in storage.list_inbox_rows(conn, run_id=RUN_A)] == [
        (INTEGRATION_A, "evt_first"), ("int_SYNTHETIC_SECOND", "evt_fresh")]
    assert [(row.integration_id, row.event_id) for row in storage.list_inbox_rows(conn, run_id=RUN_B)] == [
        (INTEGRATION_A, "evt_fresh")]


# --------------------------------------------------------------------------- migrations 4 and 5 on an existing database


def body_lookup_plan(
    conn: sqlite3.Connection, lookup: Callable[..., object], run: SeededRun, body_sha256: str
) -> str:
    """SQLite's query plan for the very statement one of the receiver's body lookups runs."""
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        lookup(conn, run_id=run.run_id, integration_id=run.integration_id, body_sha256=body_sha256)
    finally:
        conn.set_trace_callback(None)
    [statement] = statements
    return " | ".join(str(row["detail"]) for row in conn.execute("EXPLAIN QUERY PLAN " + statement))  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- explains the receiver's own statement, captured by SQLite's trace callback; no outside input reaches it


def test_migration_four_indexes_inbox_bodies_in_a_database_that_already_holds_runs(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = ("webhook_inbox", "payment_observations", "delivery_attempts")

    def row_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
        return {(table, run_id): count_rows(conn, table, run_id) for table in tables for run_id in (RUN_A, RUN_B)}

    with closing(storage.connect(db_path)) as conn:
        # The database as the previous version left it: migrations 1 to 3 (the private helper takes a prefix) ...
        assert storage._apply_migrations(conn, storage.MIGRATIONS[:3]) == (1, 2, 3)
        run_a = seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)
        run_b = seed_run(conn, RUN_B, MERCHANT_B, INTEGRATION_B, SECRET_B)
        # ... with the receiver as it was: no body lookup, so run A holds one body under two event IDs.
        with monkeypatch.context() as previous_receiver:
            previous_receiver.setattr(storage, "find_inbox_row_by_body", lambda *_, **__: None)
            for run in (run_a, run_b):
                add_order(conn, run)
            capture_a, capture_b = signed(run_a, "evt_first"), signed(run_b, "evt_first")
            first = send(conn, run_a, capture_a, at=START_MS + 2_000)
            second = resend(conn, run_a, capture_a, "evt_second", at=START_MS + 3_000)
            send(conn, run_b, capture_b, at=START_MS + 2_000)
        assert (first.outcome, second.outcome) == (ReceiveOutcome.ACCEPTED, ReceiveOutcome.ACCEPTED)
        rows_before = row_counts(conn)
        assert BODY_INDEX not in {row["name"] for row in conn.execute("PRAGMA index_list(webhook_inbox)")}
    with closing(storage.connect(db_path)) as conn:  # the upgraded application opens it
        assert storage.migrate(conn) == (4, 5, 6, 7, 8)  # 6: the provider's delivery log (Q29 (b)); 7: Q40's indexes;
        # 8: the hosted model mode (Q46)
        assert storage.migrate(conn) == ()
        assert [tuple(row) for row in conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")] == [
            (migration.version, migration.name) for migration in storage.MIGRATIONS]
        assert row_counts(conn) == rows_before
        [index] = [row for row in conn.execute("PRAGMA index_list(webhook_inbox)") if row["name"] == BODY_INDEX]
        assert index["unique"] == 0  # rows from before the migration may hold one body under several event IDs
        assert [row["name"] for row in conn.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                                                    (BODY_INDEX,))] == [
            "run_id", "integration_id", "body_sha256"]
        assert BODY_INDEX in body_lookup_plan(conn, storage.find_inbox_row_by_body, run_a, sha256_hex(capture_a[1]))
        # A body stored before the migration is recognised, as a duplicate of the earliest row that holds it.
        third = resend(conn, run_a, capture_a, "evt_third", at=START_MS + 4_000)
        other_run = resend(conn, run_b, capture_b, "evt_second", at=START_MS + 4_000)
        assert (third.outcome, third.inbox_id) == (ReceiveOutcome.DUPLICATE, first.inbox_id)
        assert other_run.outcome is ReceiveOutcome.DUPLICATE
        assert (count_rows(conn, "webhook_inbox", RUN_A), count_rows(conn, "webhook_inbox", RUN_B)) == (2, 1)


def test_migration_five_indexes_held_bodies_in_a_database_that_already_holds_runs(db_path: Path) -> None:
    tables = ("webhook_inbox", "conflicting_deliveries", "payment_observations", "delivery_attempts")

    def row_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
        return {(table, run_id): count_rows(conn, table, run_id) for table in tables for run_id in (RUN_A, RUN_B)}

    with closing(storage.connect(db_path)) as conn:
        # The database as the previous version left it: migrations 1 to 4 (the private helper takes a prefix) ...
        assert storage._apply_migrations(conn, storage.MIGRATIONS[:4]) == (1, 2, 3, 4)
        run_a = seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)
        run_b = seed_run(conn, RUN_B, MERCHANT_B, INTEGRATION_B, SECRET_B)
        add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
        add_order(conn, run_b)
        send(conn, run_a, signed(run_a, "evt_first"), at=START_MS + 2_000)
        send(conn, run_a, signed(run_a, "evt_other", payment_id=PAY_2), at=START_MS + 2_100)
        send(conn, run_b, signed(run_b, "evt_first"), at=START_MS + 2_000)
        # ... with conflicts held: in run A one body reused two stored event IDs, so it is held twice.
        conflicting_a = signed(run_a, "evt_first", status=PaymentStatus.FAILED)
        conflicting_b = signed(run_b, "evt_first", status=PaymentStatus.FAILED)
        held = [send(conn, run_a, conflicting_a, at=START_MS + 3_000),
                resend(conn, run_a, conflicting_a, "evt_other", at=START_MS + 3_100),
                send(conn, run_b, conflicting_b, at=START_MS + 3_000)]
        assert [item.outcome for item in held] == [ReceiveOutcome.CONFLICTING_ID] * 3
        rows_before = row_counts(conn)
        assert HELD_BODY_INDEX not in {row["name"] for row in conn.execute("PRAGMA index_list(conflicting_deliveries)")}
    with closing(storage.connect(db_path)) as conn:  # the upgraded application opens it
        assert storage.migrate(conn) == (5, 6, 7, 8)  # 6: the provider's delivery log (Q29 (b)); 7: Q40's indexes;
        # 8: the hosted model mode (Q46)
        assert storage.migrate(conn) == ()
        assert [tuple(row) for row in conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")] == [
            (migration.version, migration.name) for migration in storage.MIGRATIONS]
        assert row_counts(conn) == rows_before
        [index] = [row for row in conn.execute("PRAGMA index_list(conflicting_deliveries)")
                   if row["name"] == HELD_BODY_INDEX]
        assert index["unique"] == 0  # one body can be held under several event IDs
        assert [row["name"] for row in conn.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                                                    (HELD_BODY_INDEX,))] == [
            "run_id", "integration_id", "body_sha256"]
        assert HELD_BODY_INDEX in body_lookup_plan(conn, storage.find_conflicting_delivery_by_body, run_a,
                                                   sha256_hex(conflicting_a[1]))
        # A body held before the migration is recognised, as a duplicate of the earliest delivery that holds it.
        fresh = [resend(conn, run, body, "evt_fresh", at=START_MS + 4_000)
                 for run, body in ((run_a, conflicting_a), (run_b, conflicting_b))]
        assert [(item.outcome, item.conflicting_delivery_id) for item in fresh] == [
            (ReceiveOutcome.DUPLICATE, held[0].conflicting_delivery_id),
            (ReceiveOutcome.DUPLICATE, held[2].conflicting_delivery_id)]
        assert (count_rows(conn, "webhook_inbox", RUN_A), count_rows(conn, "webhook_inbox", RUN_B)) == (2, 1)
