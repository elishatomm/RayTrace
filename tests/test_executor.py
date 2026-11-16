"""Approval and execution (PLAN M2 acceptance): exact effects once, rechecks inside the transaction."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from test_plans import (
    MINUTE,
    NOW,
    OPERATOR,
    STALE_AFTER_MS,
    T_FAULT,
    Batch,
    build_batch,
    build_plan,
    order_id,
    pay_id,
    validate,
)

from recovery_workbench import executor, metrics, plans, policy, report, storage
from recovery_workbench.contracts import ActionStatus, ContractError, InboxStatus, PlanStatus, ScopeError, StaleStateError
from recovery_workbench.simulator import merchant

APPROVED_AT = NOW + MINUTE
EXECUTED_AT = NOW + 2 * MINUTE
APPROVAL_TTL_MS = 10 * MINUTE


@pytest.fixture
def batch(conn: sqlite3.Connection) -> Batch:
    return build_batch(conn)


def approved_plan(conn: sqlite3.Connection, batch: Batch) -> plans.PlanPreview:
    preview = build_plan(conn, batch, validate(conn, batch))
    executor.approve_plan(
        conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
        operator_id=OPERATOR, now_ms=APPROVED_AT, ttl_ms=APPROVAL_TTL_MS,
    )
    return preview


def execute(
    conn: sqlite3.Connection, batch: Batch, plan_id: str, *, now_ms: int = EXECUTED_AT,
    deployed: merchant.DeployedMerchant | None = None,
) -> executor.ExecutionResult:
    return executor.execute_plan(
        conn, run_id=batch.run.run_id, plan_id=plan_id, deployed=deployed or batch.deployed, now_ms=now_ms,
        stale_after_ms=STALE_AFTER_MS,
    )


def rejection(conn: sqlite3.Connection, batch: Batch, plan_id: str, **kwargs: object) -> executor.ExecutionResult:
    with pytest.raises(executor.PlanRejectedError) as caught:
        execute(conn, batch, plan_id, **kwargs)  # type: ignore[arg-type]
    assert isinstance(caught.value, StaleStateError)  # the API maps this to 409
    return caught.value.result


def codes(result: executor.ExecutionResult) -> set[str]:
    return {reason.split(":", 1)[0] for reason in result.reasons}


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # table names are literals in this file


# --------------------------------------------------------------------------- the happy path


def test_eligible_orders_are_repaired_once_and_the_rest_are_untouched(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    before = {order.order_id: order.fulfilment_state for order in storage.list_orders(conn, run_id=batch.run.run_id)}
    result = execute(conn, batch, preview.plan.plan_id)

    assert result.status is ActionStatus.SUCCEEDED
    assert not result.replayed
    assert set(result.fulfilled_order_ids) == set(batch.eligible)
    fulfilments = {item.order_id: item for item in storage.list_fulfilments(conn, run_id=batch.run.run_id)}
    for order, amount in batch.eligible.items():
        assert fulfilments[order].via == "reprocess"
        assert fulfilments[order].action_id == result.action_id
        assert fulfilments[order].amount_paise == amount
    after = {order.order_id: order.fulfilment_state for order in storage.list_orders(conn, run_id=batch.run.run_id)}
    changed = {order for order in after if after[order] != before[order]}
    assert changed == set(batch.eligible)
    targets = {target.inbox_id for target in preview.targets}
    for row in storage.list_inbox_rows(conn, run_id=batch.run.run_id):
        if row.inbox_id in targets:
            assert row.processing_status is InboxStatus.PROCESSED
            assert row.processed_at_ms == EXECUTED_AT
    superseded = preview.superseded_inbox_ids[batch.superseded_order]
    for inbox_id in superseded:  # the other event of an order is not part of the approved rows
        assert storage.get_inbox_row(conn, run_id=batch.run.run_id, inbox_id=inbox_id).processing_status is InboxStatus.FAILED
    assert plans.require_plan(conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id).status is PlanStatus.EXECUTED


def test_restored_value_is_the_eligible_sum_and_new_capture_is_zero(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    result = execute(conn, batch, preview.plan.plan_id)
    assert result.restored_processing_value_paise == sum(batch.eligible.values()) == preview.target_value_paise
    assert result.additional_captured_paise == 0
    action = executor.list_actions(conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id)[0]
    assert action.after["restored_processing_value_paise"] == result.restored_processing_value_paise
    assert action.after["captured_paise"] == action.before["captured_paise"]  # measured, not assumed
    assert "revenue" not in json.dumps(action.after)


def test_a_repeated_execute_returns_the_same_action_and_changes_nothing(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    first = execute(conn, batch, preview.plan.plan_id)
    fulfilments = count(conn, "fulfilments")
    second = execute(conn, batch, preview.plan.plan_id, now_ms=EXECUTED_AT + MINUTE)
    assert second.replayed
    assert second.action_id == first.action_id
    assert second.fulfilled_order_ids == first.fulfilled_order_ids
    assert second.restored_processing_value_paise == first.restored_processing_value_paise
    assert count(conn, "fulfilments") == fulfilments
    assert count(conn, "actions") == 1


def test_a_late_authorised_capture_is_fulfilled_once_with_no_new_capture(conn: sqlite3.Connection, batch: Batch) -> None:
    """Policy-v3 (owner decision Q8): order 50's payment failed first and was captured later."""
    late = order_id(50)
    preview = approved_plan(conn, batch)
    assert late in preview.plan.target_ids
    ledger_before = executor._capture_ledger(conn, run_id=batch.run.run_id, as_of_ms=EXECUTED_AT)
    first = execute(conn, batch, preview.plan.plan_id)
    assert first.status is ActionStatus.SUCCEEDED
    assert late in first.fulfilled_order_ids
    assert first.restored_processing_value_paise == preview.target_value_paise == sum(batch.eligible.values())
    assert batch.eligible[late] == 50_000
    assert first.additional_captured_paise == 0
    ledger_after = executor._capture_ledger(conn, run_id=batch.run.run_id, as_of_ms=EXECUTED_AT)
    assert ledger_after.captured_paise == ledger_before.captured_paise  # nobody was charged again
    [fulfilment] = [item for item in storage.list_fulfilments(conn, run_id=batch.run.run_id) if item.order_id == late]
    assert (fulfilment.payment_id, fulfilment.amount_paise, fulfilment.via) == (pay_id(50), 50_000, "reprocess")
    again = execute(conn, batch, preview.plan.plan_id, now_ms=EXECUTED_AT + MINUTE)
    assert again.replayed
    assert again.action_id == first.action_id
    assert again.additional_captured_paise == 0
    assert [item for item in storage.list_fulfilments(conn, run_id=batch.run.run_id) if item.order_id == late] == [fulfilment]
    assert count(conn, "actions") == 1


def test_a_double_click_from_two_connections_executes_once(
    conn: sqlite3.Connection, batch: Batch, db_path: Path
) -> None:
    preview = approved_plan(conn, batch)
    results: list[executor.ExecutionResult] = []
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def click() -> None:
        own = storage.connect(db_path)
        try:
            start.wait()
            results.append(
                executor.execute_plan(
                    own, run_id=batch.run.run_id, plan_id=preview.plan.plan_id,
                    deployed=merchant.compose({"payload": 2}, version=batch.deployed.version), now_ms=EXECUTED_AT,
                    stale_after_ms=STALE_AFTER_MS,
                )
            )
        except BaseException as error:  # surfaced by the assertion below, never ignored
            errors.append(error)
        finally:
            own.close()

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert sorted(result.replayed for result in results) == [False, True]
    assert len({result.action_id for result in results}) == 1
    assert count(conn, "actions") == 1
    assert count(conn, "fulfilments") == len(batch.eligible) + 4


# --------------------------------------------------------------------------- invalidation


def test_a_cancellation_after_approval_invalidates_the_plan(
    conn: sqlite3.Connection, batch: Batch, open_connection: Callable[[], sqlite3.Connection]
) -> None:
    preview = approved_plan(conn, batch)
    second = open_connection()
    with storage.write_transaction(second):  # the shop cancels one target order from another connection
        second.execute(
            "UPDATE orders SET cancelled_at_ms = ? WHERE run_id = ? AND order_id = ?",
            (APPROVED_AT + 1_000, batch.run.run_id, order_id(7)),
        )
    fulfilments = count(conn, "fulfilments")
    result = rejection(conn, batch, preview.plan.plan_id)
    assert result.status is ActionStatus.REJECTED
    assert {policy.RecheckFailure.TARGET_INELIGIBLE.value, policy.RecheckFailure.EVIDENCE_CHANGED.value} <= codes(result)
    assert any(order_id(7) in reason and "cancelled" in reason for reason in result.reasons)
    assert count(conn, "fulfilments") == fulfilments  # not even the 39 unaffected orders
    assert plans.require_plan(conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id).status is PlanStatus.INVALIDATED
    action = executor.list_actions(conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id)[0]
    assert action.status is ActionStatus.REJECTED
    assert action.after is None
    again = rejection(conn, batch, preview.plan.plan_id)  # a retry finds the same rejected action
    assert again.replayed
    assert again.action_id == result.action_id
    assert count(conn, "actions") == 1


def test_a_worker_effect_after_approval_invalidates_the_plan(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    target = preview.targets[0]
    with storage.write_transaction(conn):  # the order is fulfilled some other way before execution
        storage.insert_fulfilment(conn, storage.FulfilmentRecord(
            run_id=batch.run.run_id, order_id=target.id, payment_id=target.payment_id, amount_paise=target.amount_paise,
            currency=target.currency, fulfilled_at_ms=APPROVED_AT + 1_000, via="worker",
        ))
        storage.mark_order_fulfilled(conn, run_id=batch.run.run_id, order_id=target.id)
    result = rejection(conn, batch, preview.plan.plan_id)
    assert any(target.id in reason and "already_fulfilled" in reason for reason in result.reasons)


def test_an_expired_approval_is_rejected(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    late = APPROVED_AT + APPROVAL_TTL_MS  # the approval expires exactly now
    result = rejection(conn, batch, preview.plan.plan_id, now_ms=late)
    assert policy.RecheckFailure.APPROVAL_EXPIRED.value in codes(result)


def test_an_expired_approval_cannot_be_revived_with_an_older_time(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    with storage.write_transaction(conn):
        storage.advance_clock(conn, batch.run.run_id, APPROVED_AT + APPROVAL_TTL_MS + 1)
    with pytest.raises(ContractError, match="cannot go back"):
        execute(conn, batch, preview.plan.plan_id, now_ms=EXECUTED_AT)
    assert count(conn, "actions") == 0


def test_approval_refuses_a_hash_the_operator_did_not_review(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    with pytest.raises(StaleStateError, match="review it again"):
        executor.approve_plan(
            conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id, plan_hash="0" * 64, operator_id=OPERATOR,
            now_ms=APPROVED_AT, ttl_ms=APPROVAL_TTL_MS,
        )
    assert executor.get_approval(conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id) is None
    assert plans.require_plan(conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id).status is PlanStatus.DRAFT


def test_a_plan_altered_after_approval_no_longer_matches_its_hash(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    targets = json.loads(conn.execute("SELECT targets FROM plans WHERE plan_id = ?", (preview.plan.plan_id,)).fetchone()[0])
    with storage.write_transaction(conn):  # someone drops a target from the approved plan
        conn.execute("UPDATE plans SET targets = ? WHERE plan_id = ?", (json.dumps(targets[1:]), preview.plan.plan_id))
    result = rejection(conn, batch, preview.plan.plan_id)
    assert policy.RecheckFailure.PLAN_HASH_MISMATCH.value in codes(result)
    assert count(conn, "fulfilments") == 4  # only the orders the worker fulfilled before the fault


ALTERED_TARGET = order_id(7)  # 17,000 paise INR, paid by pay_7, whose capture arrived as evt_capture_7
STORED_FIELDS = ("event_id", "payment_id", "amount_paise", "currency")


def alter_stored_target(conn: sqlite3.Connection, plan_id: str, field: str, value: object) -> None:
    """One field of ALTERED_TARGET changed in the stored plan after approval; the plan hash binds only the order IDs."""
    targets = json.loads(conn.execute("SELECT targets FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()[0])
    altered = [{**target, field: value} if target["id"] == ALTERED_TARGET else target for target in targets]
    with storage.write_transaction(conn):
        conn.execute("UPDATE plans SET targets = ? WHERE plan_id = ?", (json.dumps(altered), plan_id))


def business_state(
    conn: sqlite3.Connection, batch: Batch
) -> tuple[tuple[storage.FulfilmentRecord, ...], tuple[storage.InboxRecord, ...], tuple[storage.OrderRecord, ...]]:
    run_id = batch.run.run_id
    return (storage.list_fulfilments(conn, run_id=run_id), storage.list_inbox_rows(conn, run_id=run_id),
            storage.list_orders(conn, run_id=run_id))


@pytest.mark.parametrize(("field", "stored", "detail"), [
    ("event_id", "evt_capture_8", "plan stores event_id 'evt_capture_8', its event has 'evt_capture_7'"),
    ("payment_id", pay_id(8), f"plan stores payment_id {pay_id(8)!r}, its event has {pay_id(7)!r}"),
    ("amount_paise", 34_000, "plan stores amount_paise 34000, its event has 17000"),
    ("currency", "USD", "plan stores currency 'USD', its event has 'INR'"),
], ids=list(STORED_FIELDS))
def test_a_target_field_altered_after_approval_refuses_the_plan_on_that_target_alone_and_changes_nothing(
    conn: sqlite3.Connection, batch: Batch, field: str, stored: object, detail: str
) -> None:
    """The plan hash still matches and the executor fulfils from the stored fields, so the recheck compares them with
    each target's event: target_ineligible fails for order 7 alone, with the stored and the event's value."""
    preview = approved_plan(conn, batch)
    alter_stored_target(conn, preview.plan.plan_id, field, stored)
    before = business_state(conn, batch)
    result = rejection(conn, batch, preview.plan.plan_id)
    assert result.reasons == (f"target_ineligible: {ALTERED_TARGET}: {detail}",)
    assert business_state(conn, batch) == before  # no fulfilment, inbox row or order changed
    assert plans.require_plan(conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id).status is PlanStatus.INVALIDATED
    receipt = report.build_receipt(conn, run_id=batch.run.run_id, action_id=result.action_id)
    checks = receipt["preconditions"]["execution"]["checks"]
    assert {check["code"]: (check["result"], check["details"]) for check in checks if check["result"] != "passed"} == {
        "target_ineligible": ("failed", [f"{ALTERED_TARGET}: {detail}"]), executor.RERUN_CHECK: ("not_run", [])}
    assert (receipt["value"]["restored_processing_value_paise"], receipt["execution"]["results"]["completed"]) == (0, 0)


def test_a_stored_target_rewritten_with_its_own_values_still_executes_as_before(
    conn: sqlite3.Connection, batch: Batch
) -> None:
    """The control: the same rewrites, each field to the value it held. Nothing is refused, and every target is
    fulfilled with the payment, amount and currency of its event."""
    preview = approved_plan(conn, batch)
    target = next(item for item in preview.targets if item.id == ALTERED_TARGET)
    for field in STORED_FIELDS:
        alter_stored_target(conn, preview.plan.plan_id, field, getattr(target, field))
    result = execute(conn, batch, preview.plan.plan_id)
    assert (result.status, set(result.fulfilled_order_ids)) == (ActionStatus.SUCCEEDED, set(batch.eligible))
    assert result.restored_processing_value_paise == preview.target_value_paise == sum(batch.eligible.values())
    fulfilled = {item.order_id: (item.payment_id, item.amount_paise, item.currency)
                 for item in storage.list_fulfilments(conn, run_id=batch.run.run_id) if item.action_id == result.action_id}
    assert fulfilled == {item.id: (item.payment_id, item.amount_paise, item.currency) for item in preview.targets}
    audit = executor.get_action_audit(conn, run_id=batch.run.run_id, action_id=result.action_id)
    assert audit is not None
    assert [(check.result, check.details) for check in audit.checks if check.code == "target_ineligible"] == [
        ("passed", ())]


def test_only_a_human_operator_can_approve(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    for impostor in ("qwen3:4b", "model", "agent_reviewer", "OP_shouting"):
        with pytest.raises(ScopeError, match="not a human operator"):
            executor.approve_plan(
                conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                operator_id=impostor, now_ms=APPROVED_AT, ttl_ms=APPROVAL_TTL_MS,
            )


def test_a_non_human_approval_smuggled_into_the_table_is_rejected(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    with storage.write_transaction(conn):  # bypasses approve_plan entirely
        conn.execute(
            "INSERT INTO approvals (run_id, approval_id, plan_id, plan_hash, operator_id, approved_at_ms, expires_at_ms)"
            " VALUES (?, 'appr_smuggled', ?, ?, 'qwen3:4b', ?, ?)",
            (batch.run.run_id, preview.plan.plan_id, preview.plan.plan_hash, APPROVED_AT, APPROVED_AT + APPROVAL_TTL_MS),
        )
        conn.execute("UPDATE plans SET status = 'approved' WHERE plan_id = ?", (preview.plan.plan_id,))
    result = rejection(conn, batch, preview.plan.plan_id)
    assert codes(result) == {policy.RecheckFailure.APPROVER_NOT_HUMAN.value}


def test_executing_without_approval_is_rejected(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    result = rejection(conn, batch, preview.plan.plan_id)
    assert {policy.RecheckFailure.APPROVAL_MISSING.value, policy.RecheckFailure.PLAN_NOT_APPROVED.value} <= codes(result)


def test_a_changed_deployment_since_validation_is_rejected(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    redeployed = merchant.compose({"payload": 2, "notifications": 2}, version="dep_later_notifications")
    result = rejection(conn, batch, preview.plan.plan_id, deployed=redeployed)
    assert codes(result) == {policy.RecheckFailure.DEPLOYED_VERSION_CHANGED.value}


def test_a_capture_that_goes_stale_before_execution_is_rejected(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    first_capture_known_at = T_FAULT + 10_000 + 50  # order 1's capture: occurred, then received 50 ms later
    stale_now = first_capture_known_at + STALE_AFTER_MS + 1
    assert stale_now < APPROVED_AT + APPROVAL_TTL_MS < preview.plan.expires_at_ms  # approval and plan still valid
    result = rejection(conn, batch, preview.plan.plan_id, now_ms=stale_now)
    stale = [reason for reason in result.reasons if "stale_snapshot" in reason]
    assert stale
    assert all(order_id(1) in reason for reason in stale)


def test_another_runs_plan_is_out_of_scope_and_records_nothing(conn: sqlite3.Connection, batch: Batch) -> None:
    from conftest import MERCHANT_A, seed_run

    preview = approved_plan(conn, batch)
    seed_run(conn, "run_unit_other", MERCHANT_A, "int_unit_other", "fixture_secret_a")
    with pytest.raises(ScopeError):
        executor.execute_plan(
            conn, run_id="run_unit_other", plan_id=preview.plan.plan_id, deployed=batch.deployed, now_ms=EXECUTED_AT,
            stale_after_ms=STALE_AFTER_MS,
        )
    assert count(conn, "actions") == 0


def test_capture_accounting_is_untouched_by_reprocessing(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    before = executor._capture_ledger(conn, run_id=batch.run.run_id, as_of_ms=EXECUTED_AT)
    execute(conn, batch, preview.plan.plan_id)
    after = executor._capture_ledger(conn, run_id=batch.run.run_id, as_of_ms=EXECUTED_AT)
    assert metrics.capture_change(before, after).additional_captured_paise == 0
    assert after.captured_paise == before.captured_paise


# --------------------------------------------------------------------------- the action audit's checks


def intercept_recheck(
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[policy.RecheckOutcome], policy.RecheckOutcome] = lambda outcome: outcome,
) -> list[policy.RecheckOutcome]:
    """What ``recheck_reprocess`` reports to the executor, after ``change``: one entry per call."""
    real = policy.recheck_reprocess
    reported: list[policy.RecheckOutcome] = []

    def reporting(*args: object, **kwargs: object) -> policy.RecheckOutcome:
        outcome = change(real(*args, **kwargs))  # type: ignore[arg-type]
        reported.append(outcome)
        return outcome

    monkeypatch.setattr(policy, "recheck_reprocess", reporting)
    return reported


def as_reported(outcome: policy.RecheckOutcome) -> dict[str, tuple[str, tuple[str, ...]]]:
    return {check.code.value: ("passed" if check.passed else "failed", check.details) for check in outcome.checks}


def as_recorded(
    conn: sqlite3.Connection, batch: Batch, result: executor.ExecutionResult
) -> dict[str, tuple[str, tuple[str, ...]]]:
    audit = executor.get_action_audit(conn, run_id=batch.run.run_id, action_id=result.action_id)
    assert audit is not None
    return {check.code: (check.result, check.details) for check in audit.checks}


def test_without_an_approval_the_audit_records_the_checks_never_evaluated_as_not_run(
    conn: sqlite3.Connection, batch: Batch, monkeypatch: pytest.MonkeyPatch
) -> None:
    reported = intercept_recheck(monkeypatch)
    preview = build_plan(conn, batch, validate(conn, batch))
    result = rejection(conn, batch, preview.plan.plan_id)
    (outcome,) = reported
    never_evaluated = ("approver_not_human", "approval_hash_mismatch", "approval_expired")
    assert set(never_evaluated).isdisjoint(as_reported(outcome))
    recorded = as_recorded(conn, batch, result)
    assert list(recorded) == [*(code.value for code in policy.RECHECK_DESCRIPTIONS), executor.RERUN_CHECK]
    assert recorded == {**as_reported(outcome), **dict.fromkeys(never_evaluated, ("not_run", ())),
                        executor.RERUN_CHECK: ("not_run", ())}


def test_the_audit_records_each_check_as_the_recheck_reported_it_and_infers_none(
    conn: sqlite3.Connection, batch: Batch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check missing from the report stays not_run even on a plan that executes: no pass is derived from the
    absence of a failure."""
    left_out = policy.RecheckFailure.EVIDENCE_CHANGED
    reported = intercept_recheck(monkeypatch, lambda outcome: policy.RecheckOutcome(
        failures=outcome.failures, checks=tuple(check for check in outcome.checks if check.code is not left_out),
        assessments=outcome.assessments))
    preview = approved_plan(conn, batch)
    result = execute(conn, batch, preview.plan.plan_id)
    assert result.status is ActionStatus.SUCCEEDED
    (outcome,) = reported
    assert as_recorded(conn, batch, result) == {**as_reported(outcome), left_out.value: ("not_run", ()),
                                                executor.RERUN_CHECK: ("passed", ())}
