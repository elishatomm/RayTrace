"""Release gates G01-G18 of the final sprint's acceptance pack: the focused checks the existing suite left out.

Each test names its gate. They reuse the PLAN M2 batch (``test_plans.build_batch``), the executor's helpers and the
API client of ``test_api``. Where a gate is about binding, one field changes at a time, so a refusal is shown for its
own reason and not because another check already refuses everything.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from conftest import (
    ORDER_1,
    ORDER_2,
    PAY_1,
    PAY_2,
    RUN_A,
    START_MS,
    FailingControlConnection,
    FakeHandler,
    SeededRun,
    add_order,
    count_rows,
    deliver,
    observation,
    store_observations,
)
from fastapi.testclient import TestClient
from test_api import STATE_CHANGE, create_run, diagnose, get, log_in, make_client, post
from test_executor import APPROVED_AT, EXECUTED_AT, approved_plan, codes, count, execute, rejection
from test_plans import (
    MINUTE,
    NOW,
    OPERATOR,
    STALE_AFTER_MS,
    T_FAULT,
    WRONG_FIX,
    Batch,
    build_batch,
    build_plan,
    order_id,
    pay_id,
    validate,
)

from recovery_workbench import cli, executor, order_cases, plans, policy, report, state, storage, worker
from recovery_workbench.contracts import (
    ActionStatus,
    CaseState,
    ContractError,
    NextStep,
    PaymentStatus,
    PlanStatus,
    ReceiveOutcome,
    ScopeError,
    StaleStateError,
    WorkerResult,
)

TARGET = order_id(7)  # an approved target of the batch: 17,000 paise INR, one payment, captured after the fault
TARGET_PAYMENT = pay_id(7)
TARGET_AMOUNT = 17_000
SECOND_PAYMENT = pay_id(7, "B")
CHANGED_AT = APPROVED_AT + 1_000  # between the approval and the execution
SHORT_APPROVAL_MS = MINUTE  # expires at EXECUTED_AT, while every target's evidence is still fresh


@pytest.fixture
def batch(conn: sqlite3.Connection) -> Batch:
    return build_batch(conn)


def receipt_checks(conn: sqlite3.Connection, action_id: str) -> dict[str, list[str]]:
    receipt = report.build_receipt(conn, run_id=RUN_A, action_id=action_id)
    return {check["code"]: check["details"] for check in receipt["preconditions"]["execution"]["checks"]}


def approve_briefly(conn: sqlite3.Connection, batch: Batch) -> plans.PlanPreview:
    """The batch plan, approved at APPROVED_AT for one minute of replay time."""
    preview = build_plan(conn, batch, validate(conn, batch))
    executor.approve_plan(conn, run_id=RUN_A, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                          operator_id=OPERATOR, now_ms=APPROVED_AT, ttl_ms=SHORT_APPROVAL_MS)
    return preview


# --------------------------------------------------------------------------- G01: startup and migrations

# The race failed about 3 in 1,000 such connections before fix C1 (15 and 23 of 4,800 when measured on 2026-11-12), so 1,200
# would have shown it with about 97% probability.
ROUNDS = 150
CONNECTIONS = 8


def test_g01_connections_opening_one_new_database_together_never_fail_and_apply_each_migration_once(
    tmp_path: Path,
) -> None:
    """G01: the WAL-switch race found on 2026-11-12 (fix C1), repeated after migration 8 changed the migration path.

    150 rounds; in each, 8 connections released by one barrier open the same new database file and migrate it.
    """
    expected = [migration.version for migration in storage.MIGRATIONS]
    for round_number in range(ROUNDS):
        database = tmp_path / f"round_{round_number}.sqlite"
        start = threading.Barrier(CONNECTIONS)

        def open_and_migrate(_: int, database: Path = database, start: threading.Barrier = start) -> tuple[int, ...]:
            start.wait(timeout=30)
            with closing(storage.connect(database)) as own:
                return storage.migrate(own)

        with ThreadPoolExecutor(max_workers=CONNECTIONS) as pool:
            applied = list(pool.map(open_and_migrate, range(CONNECTIONS)))
        assert sorted(version for versions in applied for version in versions) == expected, round_number


# --------------------------------------------------------------------------- G02: queue scope


def test_g02_each_runs_queue_lists_its_own_orders_once_and_no_evidence_of_another_run(
    conn: sqlite3.Connection, run_a: SeededRun, run_b: SeededRun
) -> None:
    """G02: two runs in one database hold the same order and payment IDs; only run B received a capture for them."""
    add_order(conn, run_a)
    add_order(conn, run_a, ORDER_2, payment_ids=(PAY_2,))
    add_order(conn, run_b)
    assert deliver(conn, run_b, "evt_SYNTHETIC_0001").outcome is ReceiveOutcome.ACCEPTED
    at = START_MS + 10_000
    queue_a = order_cases.classify_orders(conn, run_id=run_a.run_id, as_of_ms=at, stale_after_ms=STALE_AFTER_MS)
    queue_b = order_cases.classify_orders(conn, run_id=run_b.run_id, as_of_ms=at, stale_after_ms=STALE_AFTER_MS)
    assert [case.order_id for case in queue_a.cases] == [ORDER_1, ORDER_2]
    assert [case.order_id for case in queue_b.cases] == [ORDER_1]
    [in_b] = queue_b.cases
    in_a = next(case for case in queue_a.cases if case.order_id == ORDER_1)
    assert in_b.evidence.inbox_ids and in_b.evidence.observation_ids
    assert (in_a.evidence.inbox_ids, in_a.evidence.observation_ids, in_a.evidence.delivery_attempt_ids) == ((), (), ())
    assert not any(payment.status is PaymentStatus.CAPTURED for payment in in_a.payments)
    assert (queue_a.totals.captured_unprocessed_orders, queue_b.totals.captured_unprocessed_orders) == (0, 1)


# --------------------------------------------------------------------------- G04 and G09: every attempt, business changes


def test_g04_a_later_failed_attempt_on_the_same_order_does_not_erase_its_earlier_capture(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """G04: the order's first payment is captured; a second attempt on the order fails afterwards."""
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    store_observations(conn, [observation(PaymentStatus.CAPTURED, START_MS + 1, START_MS + 2),
                              observation(PaymentStatus.FAILED, START_MS + 3, START_MS + 4, payment_id=PAY_2)])
    projection = state.load_order_projection(conn, RUN_A, ORDER_1, START_MS + 10_000)
    assert projection.exclusions == ()
    assert projection.single_captured_payment_id == PAY_1
    assert projection.is_recovery_eligible


def _second_attempt(conn: sqlite3.Connection, batch: Batch) -> None:
    with storage.write_transaction(conn):  # the customer starts another payment on the approved target's order
        storage.insert_payment_attempt(conn, storage.PaymentAttemptRecord(
            run_id=batch.run.run_id, payment_id=SECOND_PAYMENT, order_id=TARGET, method="upi", created_at_ms=CHANGED_AT))


def _refund(conn: sqlite3.Connection, batch: Batch) -> None:
    deliver(conn, batch.run, "evt_refund_7", status=PaymentStatus.REFUNDED, payment_id=TARGET_PAYMENT, order_id=TARGET,
            amount_paise=TARGET_AMOUNT, amount_refunded_paise=TARGET_AMOUNT, occurred_at_ms=CHANGED_AT)


def _second_capture(conn: sqlite3.Connection, batch: Batch) -> None:
    _second_attempt(conn, batch)
    deliver(conn, batch.run, "evt_capture_7b", payment_id=SECOND_PAYMENT, order_id=TARGET, amount_paise=TARGET_AMOUNT,
            occurred_at_ms=CHANGED_AT)


def _new_unresolved_attempt(conn: sqlite3.Connection, batch: Batch) -> None:
    _second_attempt(conn, batch)
    deliver(conn, batch.run, "evt_authorized_7b", status=PaymentStatus.AUTHORIZED, payment_id=SECOND_PAYMENT,
            order_id=TARGET, amount_paise=TARGET_AMOUNT, occurred_at_ms=CHANGED_AT)


BUSINESS_CHANGES: dict[str, tuple[Callable[[sqlite3.Connection, Batch], None], str]] = {
    "refund": (_refund, "refunded"),
    "second_capture": (_second_capture, "multiple_captures"),
    "new_unresolved_attempt": (_new_unresolved_attempt, "unresolved_attempt"),
}


@pytest.mark.parametrize("change", sorted(BUSINESS_CHANGES))
def test_g04_g09_a_business_change_after_approval_refuses_execution_for_its_own_reason(
    conn: sqlite3.Connection, batch: Batch, change: str
) -> None:
    """G04 and G09: one change to one approved target, between approval and execution; nothing else changes."""
    preview = approved_plan(conn, batch)
    make_change, reason = BUSINESS_CHANGES[change]
    make_change(conn, batch)
    fulfilled = count(conn, "fulfilments")
    result = rejection(conn, batch, preview.plan.plan_id)
    assert codes(result) == {"target_ineligible", "evidence_changed"}
    assert receipt_checks(conn, result.action_id)["target_ineligible"] == [f"{TARGET}: {reason}"]
    assert count(conn, "fulfilments") == fulfilled
    receipt = report.build_receipt(conn, run_id=RUN_A, action_id=result.action_id)
    assert (receipt["value"]["restored_processing_value_paise"], receipt["execution"]["results"]["completed"]) == (0, 0)


# --------------------------------------------------------------------------- G06: durable storage and the fix


def test_g06_a_plan_pointed_at_a_failed_fix_validation_is_refused_at_execution(
    conn: sqlite3.Connection, batch: Batch
) -> None:
    """G06: after approval the plan names a validation of config@5, which did not pass; only that field changes."""
    preview = approved_plan(conn, batch)
    failed = validate(conn, batch, WRONG_FIX)
    assert not failed.passed
    with storage.write_transaction(conn):
        conn.execute("UPDATE plans SET fix_validation_id = ? WHERE plan_id = ?", (failed.validation_id, preview.plan.plan_id))
    fulfilled = count(conn, "fulfilments")
    result = rejection(conn, batch, preview.plan.plan_id)
    assert codes(result) == {"fix_not_validated"}
    assert count(conn, "fulfilments") == fulfilled


def test_g06_a_capture_only_the_provider_reports_is_never_a_reprocess_candidate(
    conn: sqlite3.Connection, batch: Batch
) -> None:
    """G06: no recovery from storage the workbench does not hold. Order 61's capture is in a provider snapshot only;
    no webhook carried it into the durable inbox, so only provider replay instructions can follow."""
    add_order(conn, batch.run, order_id(61), payment_ids=(pay_id(61),), amount_paise=61_000)
    store_observations(conn, [observation(PaymentStatus.CAPTURED, T_FAULT + 1_000, NOW - MINUTE, payment_id=pay_id(61),
                                          order_id=order_id(61), amount_paise=61_000)])
    preview = build_plan(conn, batch, validate(conn, batch))
    assert order_id(61) not in {target.id for target in preview.targets} | {item.order_id for item in preview.excluded}
    queue = order_cases.classify_orders(conn, run_id=RUN_A, as_of_ms=NOW, stale_after_ms=STALE_AFTER_MS)
    [case] = [case for case in queue.cases if case.order_id == order_id(61)]
    assert (case.state, case.next_step) == (CaseState.NOTIFICATION_NOT_ACCEPTED, NextStep.PREPARE_PROVIDER_REPLAY)


# --------------------------------------------------------------------------- G07: exact approval


def test_g07_a_policy_version_changed_since_approval_refuses_execution_on_that_check_alone(
    conn: sqlite3.Connection, batch: Batch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G07: the plan and its approval were made under policy-v3; the executor now runs a later policy version."""
    preview = approved_plan(conn, batch)
    monkeypatch.setattr(policy, "POLICY_VERSION", "policy-v4-release-check")
    fulfilled = count(conn, "fulfilments")
    result = rejection(conn, batch, preview.plan.plan_id)
    assert codes(result) == {"policy_version_changed"}
    assert count(conn, "fulfilments") == fulfilled


def test_g07_a_plan_whose_operation_was_altered_after_approval_is_refused_before_any_write(
    conn: sqlite3.Connection, batch: Batch
) -> None:
    """G07: the approved plan's operation is changed in storage; the executor refuses it before recording anything."""
    preview = approved_plan(conn, batch)
    with storage.write_transaction(conn):
        conn.execute("UPDATE plans SET operation = 'escalate' WHERE plan_id = ?", (preview.plan.plan_id,))
    fulfilled = count(conn, "fulfilments")
    with pytest.raises(ContractError, match="the executor only runs reprocess_inbox"):
        execute(conn, batch, preview.plan.plan_id)
    assert (count(conn, "actions"), count(conn, "fulfilments")) == (0, fulfilled)


@pytest.mark.parametrize(("field", "changed"), [
    ("amount_paise", 2 * TARGET_AMOUNT), ("currency", "USD"), ("payment_id", pay_id(8)),
])
def test_g07_a_target_field_altered_after_approval_cannot_reuse_the_approval(
    conn: sqlite3.Connection, batch: Batch, field: str, changed: object
) -> None:
    """G07: one field of one approved target (order 7, 17,000 paise INR, payment pay_7) is altered in the stored plan
    after approval, as the existing test alters the target list. The plan hash binds the order IDs only, so the
    approval may not cover the altered value, currency or payment."""
    preview = approved_plan(conn, batch)
    stored = json.loads(conn.execute("SELECT targets FROM plans WHERE plan_id = ?", (preview.plan.plan_id,)).fetchone()[0])
    altered = [{**target, field: changed} if target["id"] == TARGET else target for target in stored]
    with storage.write_transaction(conn):
        conn.execute("UPDATE plans SET targets = ? WHERE plan_id = ?", (json.dumps(altered), preview.plan.plan_id))
    fulfilled = count(conn, "fulfilments")
    result = rejection(conn, batch, preview.plan.plan_id)
    assert result.status is ActionStatus.REJECTED
    assert count(conn, "fulfilments") == fulfilled


# --------------------------------------------------------------------------- G08: expiry and reset


@pytest.mark.parametrize(("offset_ms", "expired"), [(-1, False), (0, True)])
def test_g08_an_approval_expires_at_its_own_replay_time_and_then_authorises_nothing(
    conn: sqlite3.Connection, batch: Batch, offset_ms: int, expired: bool
) -> None:
    """G08: a one-minute approval executed one millisecond before its expiry succeeds; at its expiry it is refused for
    approval_expired alone. The expired approval then authorises nothing: the plan is invalidated, the retry finds the
    recorded refusal and the plan cannot be approved again."""
    preview = approve_briefly(conn, batch)
    now_ms = APPROVED_AT + SHORT_APPROVAL_MS + offset_ms
    fulfilled = count(conn, "fulfilments")
    if not expired:
        assert execute(conn, batch, preview.plan.plan_id, now_ms=now_ms).status is ActionStatus.SUCCEEDED
        return
    result = rejection(conn, batch, preview.plan.plan_id, now_ms=now_ms)
    assert codes(result) == {"approval_expired"}
    assert plans.require_plan(conn, run_id=RUN_A, plan_id=preview.plan.plan_id).status is PlanStatus.INVALIDATED
    again = rejection(conn, batch, preview.plan.plan_id, now_ms=now_ms + MINUTE)
    assert (again.replayed, again.action_id) == (True, result.action_id)
    with pytest.raises(StaleStateError, match="only a draft plan can be approved"):
        executor.approve_plan(conn, run_id=RUN_A, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                              operator_id=OPERATOR, now_ms=now_ms + MINUTE, ttl_ms=SHORT_APPROVAL_MS)
    assert (count(conn, "fulfilments"), count(conn, "actions")) == (fulfilled, 1)


def test_g08_an_approval_made_before_a_reset_authorises_nothing_after_it(tmp_path: Path) -> None:
    """G08: the batch built again in a fresh database (a reset) gives a plan with the same hash, but no approval: the
    old approval stayed with the old run's records, and the old plan ID is not part of the new run."""
    with closing(storage.connect(tmp_path / "before_reset.sqlite")) as before:
        storage.migrate(before)
        old = approved_plan(before, build_batch(before))
    with closing(storage.connect(tmp_path / "after_reset.sqlite")) as after:
        storage.migrate(after)
        fresh = build_batch(after)
        new = build_plan(after, fresh, validate(after, fresh))
        fulfilled = count(after, "fulfilments")
        assert new.plan.plan_hash == old.plan.plan_hash
        with pytest.raises(ScopeError, match="is not part of run"):
            execute(after, fresh, old.plan.plan_id)
        refused = rejection(after, fresh, new.plan.plan_id)
        assert codes(refused) == {"approval_missing", "plan_not_approved"}
        assert count(after, "fulfilments") == fulfilled


# --------------------------------------------------------------------------- G11: reordered observations


def test_g11_an_older_notification_delivered_after_the_capture_neither_regresses_it_nor_fulfils_by_itself(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """G11: the payment's authorised notification is delivered after its captured one, out of order."""
    add_order(conn, run_a)
    assert deliver(conn, run_a, "evt_captured", occurred_at_ms=START_MS + 2_000).outcome is ReceiveOutcome.ACCEPTED
    late = deliver(conn, run_a, "evt_authorized", status=PaymentStatus.AUTHORIZED, occurred_at_ms=START_MS + 1_000,
                   received_at_ms=START_MS + 3_000)
    assert late.outcome is ReceiveOutcome.ACCEPTED
    projection = state.load_order_projection(conn, RUN_A, ORDER_1, START_MS + 5_000)
    [payment] = projection.payments
    assert (payment.status, payment.conflict_reasons, projection.exclusions) == (PaymentStatus.CAPTURED, (), ())
    steps = worker.run_worker(conn, run_id=RUN_A, handler=FakeHandler(), now_ms=START_MS + 5_000)
    assert steps[0].result is WorkerResult.FULFILLED
    assert [step.result for step in steps[1:]] in ([WorkerResult.NO_EFFECT], [WorkerResult.ALREADY_FULFILLED])
    assert count_rows(conn, "fulfilments", RUN_A) == 1


# --------------------------------------------------------------------------- G12: recovery after an uncertain response


def test_g12_a_repeat_of_a_committed_execution_gets_its_stored_outcome_where_a_first_attempt_is_revalidated(
    tmp_path: Path,
) -> None:
    """G12: two databases hold the same batch and the same one-minute approval. In the first the plan was executed
    before the approval expired; repeated after its expiry, the request returns the stored action and writes nothing.
    In the second the plan was never executed; its first attempt at that same time is revalidated and refused."""
    late = APPROVED_AT + SHORT_APPROVAL_MS + MINUTE
    with closing(storage.connect(tmp_path / "committed.sqlite")) as conn:
        storage.migrate(conn)
        batch = build_batch(conn)
        preview = approve_briefly(conn, batch)
        first = execute(conn, batch, preview.plan.plan_id, now_ms=EXECUTED_AT - 1)
        effects = (count(conn, "fulfilments"), count(conn, "actions"))
        repeated = execute(conn, batch, preview.plan.plan_id, now_ms=late)
        assert (repeated.replayed, repeated.status, repeated.action_id) == (True, ActionStatus.SUCCEEDED, first.action_id)
        assert repeated.fulfilled_order_ids == first.fulfilled_order_ids
        assert repeated.restored_processing_value_paise == first.restored_processing_value_paise == sum(
            batch.eligible.values())
        assert (count(conn, "fulfilments"), count(conn, "actions")) == effects
    with closing(storage.connect(tmp_path / "never_executed.sqlite")) as conn:
        storage.migrate(conn)
        batch = build_batch(conn)
        preview = approve_briefly(conn, batch)
        fulfilled = count(conn, "fulfilments")
        refused = rejection(conn, batch, preview.plan.plan_id, now_ms=late)
        assert (refused.replayed, codes(refused)) == (False, {"approval_expired"})
        assert count(conn, "fulfilments") == fulfilled


def reprocessed(database: Path, run_id: str) -> list[storage.FulfilmentRecord]:
    with closing(storage.connect(database)) as conn:
        return [item for item in storage.list_fulfilments(conn, run_id=run_id) if item.via == "reprocess"]


def approved_through_the_api(client: TestClient) -> tuple[str, dict, dict]:
    """Flow A on the README's demo world (worker_backlog, seed 101) through the API, up to an approved plan."""
    run_id = create_run(client)
    found = diagnose(client, run_id)["diagnosis"]
    validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate", {"diagnosis_id": found["diagnosis_id"]})
    plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
    approval = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
    return run_id, plan, approval


def test_g12_a_response_lost_after_commit_is_answered_from_the_stored_outcome_once_the_approval_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G12: the execute request commits and its answer never reaches the operator. The retry is sent after the approval
    has expired; it gets the stored outcome: the same action, no second fulfilment, the value counted once."""
    committed: list[executor.ExecutionResult] = []
    real_execute = cli.execute_plan

    def answer_lost(*args: object, **kwargs: object) -> executor.ExecutionResult:
        committed.append(real_execute(*args, **kwargs))  # type: ignore[arg-type]
        raise ConnectionResetError("the connection dropped before the answer was sent")

    with make_client(tmp_path) as client:
        log_in(client)
        run_id, plan, approval = approved_through_the_api(client)
        execute_path = f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute"
        with monkeypatch.context() as lost:
            lost.setattr(cli, "execute_plan", answer_lost)
            with pytest.raises(ConnectionResetError):
                client.post(execute_path, json={}, headers=STATE_CHANGE)
        database = Path(client.app.state.workbench.database)
        after_commit = reprocessed(database, run_id)
        clock = get(client, f"/api/runs/{run_id}")["replay_clock_ms"]
        post(client, f"/api/runs/{run_id}/advance", {"by_ms": approval["expires_at_ms"] - clock + MINUTE})
        retried = post(client, execute_path)
        receipt = get(client, f"/api/runs/{run_id}/actions/{retried['action_id']}/receipt")
    [first] = committed
    assert (first.status, first.replayed) == (ActionStatus.SUCCEEDED, False)
    assert (retried["replayed"], retried["status"], retried["action_id"]) == (True, "succeeded", first.action_id)
    assert retried["restored_processing_value_paise"] == first.restored_processing_value_paise == plan["target_value_paise"]
    assert len(after_commit) == len(reprocessed(database, run_id)) == plan["target_count"]
    assert receipt["value"]["restored_processing_value_paise"] == plan["target_value_paise"]


# --------------------------------------------------------------------------- G13: transaction failure


def test_g13_an_execution_whose_commit_fails_leaves_no_effect_no_action_and_no_receipt(
    conn: sqlite3.Connection, batch: Batch
) -> None:
    """G13: the execution's COMMIT fails and is rolled back. Nothing it wrote remains, no receipt can claim success,
    the targets stay unresolved in the queue, and the retry under the same operation key executes exactly once."""
    preview = approved_plan(conn, batch)
    fulfilled = count(conn, "fulfilments")
    failing = FailingControlConnection(conn, fail_on="COMMIT")
    with pytest.raises(sqlite3.OperationalError, match="injected COMMIT failure"):
        executor.execute_plan(failing, run_id=RUN_A, plan_id=preview.plan.plan_id,  # type: ignore[arg-type]
                              deployed=batch.deployed, now_ms=EXECUTED_AT, stale_after_ms=STALE_AFTER_MS)
    assert (count(conn, "actions"), count(conn, "action_audits"), count(conn, "fulfilments")) == (0, 0, fulfilled)
    with pytest.raises(ScopeError, match="is not part of run"):
        report.build_receipt(conn, run_id=RUN_A, action_id=plans.action_id_for(plans.operation_key(preview.plan)))
    assert plans.require_plan(conn, run_id=RUN_A, plan_id=preview.plan.plan_id).status is PlanStatus.APPROVED
    queue = order_cases.classify_orders(conn, run_id=RUN_A, as_of_ms=EXECUTED_AT, stale_after_ms=STALE_AFTER_MS)
    assert {case.state for case in queue.cases if case.order_id in batch.eligible} == {
        CaseState.CAPTURED_PROCESSING_INCOMPLETE}
    retried = execute(conn, batch, preview.plan.plan_id)
    assert (retried.status, retried.replayed) == (ActionStatus.SUCCEEDED, False)
    assert (count(conn, "actions"), count(conn, "fulfilments")) == (1, fulfilled + len(batch.eligible))


# --------------------------------------------------------------------------- G14: receipt agreement


def paise_text(paise: int) -> str:
    return f"INR {paise // 100:,}.{paise % 100:02d} ({paise} paise)"


def test_g14_the_execute_answer_receipts_report_queue_and_committed_rows_state_one_recovery_alike(
    tmp_path: Path,
) -> None:
    """G14: one recovery through the API in rules mode, and every place that states its totals: the execute answer the
    page shows, the JSON and Markdown receipts, the run report, the queue's totals and the committed fulfilments."""
    with make_client(tmp_path) as client:
        log_in(client)
        run_id, plan, _ = approved_through_the_api(client)
        before = get(client, f"/api/runs/{run_id}/orders")["totals"]
        executed = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
        route = f"/api/runs/{run_id}/actions/{executed['action_id']}/receipt"
        receipt = get(client, route)
        markdown = client.get(f"{route}?format=markdown").text
        outcome = get(client, f"/api/runs/{run_id}/report")["outcome"]
        after = get(client, f"/api/runs/{run_id}/orders")["totals"]
        database = Path(client.app.state.workbench.database)
    committed = [item for item in reprocessed(database, run_id) if item.action_id == executed["action_id"]]
    restored = sum(item.amount_paise for item in committed)
    assert {item.currency for item in committed} == {"INR"} == {receipt["value"]["currency"]}
    assert len({item.order_id for item in committed}) == len(committed) == plan["target_count"]
    assert len(executed["fulfilled_order_ids"]) == receipt["execution"]["results"]["completed"] == len(committed)
    assert outcome["orders_fulfilled_by_reprocessing"] == len(committed)
    assert restored == plan["target_value_paise"] == executed["restored_processing_value_paise"]
    assert restored == receipt["value"]["restored_processing_value_paise"] == outcome["restored_processing_value_paise"]
    assert (executed["additional_captured_paise"], receipt["value"]["additional_captured_paise"],
            outcome["additional_captured_paise"]) == (0, 0, 0)
    assert f"| Restored processing value | {paise_text(restored)} |" in markdown
    assert f"| Additional captured | {paise_text(0)} |" in markdown
    assert f"Receipt hash `{receipt['receipt_hash']}`" in markdown
    assert (before["captured_unprocessed_orders"], before["captured_unprocessed_value_paise"]) == (len(committed), restored)
    assert (after["captured_unprocessed_orders"], after["captured_unprocessed_value_paise"]) == (0, 0)
