"""A reprocessing target's own amount and currency are its order's, or it is not a target (policy.assess_target).

The plan, the dry run, the execution rerun and the fulfilment all take the target event's amount and currency. The
order is judged on a projection that holds the event's own observation only once it is visible: a signed capture
whose envelope time lies after its receipt is stored with an observation dated in the future, which every as-of read
hides until then. With a genuine capture at the order's amount visible beside it, nothing else disagrees.
"""
from __future__ import annotations

import sqlite3

import pytest
from conftest import (
    INTEGRATION_A,
    MERCHANT_A,
    ORDER_1,
    ORDER_2,
    PAY_1,
    PAY_2,
    RUN_A,
    SECRET_A,
    START_MS,
    SeededRun,
    add_order,
    deliver,
    seed_run,
)
from test_order_cases import planner_disagreements
from test_plans import CORRECT_FIX, INCIDENT, MINUTE, OPERATOR, PLAN_TTL_MS, STALE_AFTER_MS, add_diagnosis

from recovery_workbench import executor, fixes, order_cases, plans, policy, state, storage, worker
from recovery_workbench.contracts import ActionStatus, ReceiveOutcome
from recovery_workbench.order_cases import CaseState
from recovery_workbench.simulator import merchant

FUTURE = START_MS + 365 * 24 * 60 * MINUTE  # an envelope time after every replay time below
WORKER_AT = START_MS + 5 * MINUTE
PLAN_AT = START_MS + 10 * MINUTE
APPROVE_AT = START_MS + 11 * MINUTE
EXECUTE_AT = START_MS + 12 * MINUTE
APPROVAL_TTL_MS = 10 * MINUTE
ORDER_AMOUNT = 30_000
CONTROL_AMOUNT = 20_000
FAULTY = merchant.compose({"payload": 2}, version="dep_payload_accessor")


def forged_capture_run(
    conn: sqlite3.Connection, *, amount: int, currency: str = "INR", occurred_at_ms: int = FUTURE
) -> SeededRun:
    """Order 1 (30,000 paise INR) with two signed captures of its one payment, and order 2, a plain control.

    Order 1's first capture received carries ``amount``, ``currency`` and the envelope time ``occurred_at_ms``; the
    second is the genuine one, at the order's amount. Order 2's one capture is at its own amount. payload@2 then fails
    every capture, so each waits for an approved reprocess.
    """
    run = seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)
    add_order(conn, run, ORDER_1, payment_ids=(PAY_1,), amount_paise=ORDER_AMOUNT)
    add_order(conn, run, ORDER_2, payment_ids=(PAY_2,), amount_paise=CONTROL_AMOUNT)
    responses = [
        deliver(conn, run, "evt_forged", payment_id=PAY_1, order_id=ORDER_1, amount_paise=amount, currency=currency,
                occurred_at_ms=occurred_at_ms, received_at_ms=START_MS + 1_000),
        deliver(conn, run, "evt_genuine", payment_id=PAY_1, order_id=ORDER_1, amount_paise=ORDER_AMOUNT,
                occurred_at_ms=START_MS + 1_500, received_at_ms=START_MS + 2_000),
        deliver(conn, run, "evt_control", payment_id=PAY_2, order_id=ORDER_2, amount_paise=CONTROL_AMOUNT,
                occurred_at_ms=START_MS + 3_000),
    ]
    assert [response.outcome for response in responses] == [ReceiveOutcome.ACCEPTED] * 3
    steps = worker.run_worker(conn, run_id=run.run_id, handler=FAULTY, now_ms=WORKER_AT)
    assert {step.result.value for step in steps} == {"failed"}
    return run


def plan(conn: sqlite3.Connection, run: SeededRun) -> plans.PlanPreview:
    """The incident, the worker-regression diagnosis and the payload@5 validation, then the reprocess plan."""
    with storage.write_transaction(conn):
        storage.insert_incident(conn, storage.IncidentRecord(
            run_id=run.run_id, incident_id=INCIDENT, trigger="order_completion_drop", cohort={},
            window_start_ms=START_MS, window_end_ms=PLAN_AT, evidence_cutoff_ms=PLAN_AT, state="open",
            measured_impact={},
        ))
    validation = fixes.validate_fix(
        conn, run_id=run.run_id, deployed=FAULTY, fix_id=CORRECT_FIX, diagnosis_id=add_diagnosis(conn, run.run_id),
        operator_id=OPERATOR, now_ms=PLAN_AT,
    )
    assert validation.passed
    return plans.build_reprocess_plan(
        conn, run_id=run.run_id, fix_validation_id=validation.validation_id, deployed=FAULTY, now_ms=PLAN_AT,
        ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS,
    )


def approve(conn: sqlite3.Connection, run: SeededRun, preview: plans.PlanPreview) -> None:
    executor.approve_plan(
        conn, run_id=run.run_id, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash, operator_id=OPERATOR,
        now_ms=APPROVE_AT, ttl_ms=APPROVAL_TTL_MS,
    )


def execute(conn: sqlite3.Connection, run: SeededRun, preview: plans.PlanPreview) -> executor.ExecutionResult:
    return executor.execute_plan(
        conn, run_id=run.run_id, plan_id=preview.plan.plan_id, deployed=FAULTY, now_ms=EXECUTE_AT,
        stale_after_ms=STALE_AFTER_MS,
    )


def fulfilments_unlike_their_order(conn: sqlite3.Connection, run_id: str) -> dict[str, dict[str, tuple[int, str]]]:
    """Every fulfilment whose amount or currency is not its order's, beside the order's."""
    orders = {item.order_id: (item.amount_paise, item.currency) for item in storage.list_orders(conn, run_id=run_id)}
    return {
        item.order_id: {"fulfilled": (item.amount_paise, item.currency), "order": orders[item.order_id]}
        for item in storage.list_fulfilments(conn, run_id=run_id)
        if (item.amount_paise, item.currency) != orders[item.order_id]
    }


@pytest.mark.parametrize("forged_amount", [1, 45_000])
def test_an_executed_reprocess_never_fulfils_an_order_at_an_amount_that_is_not_its_own(
    conn: sqlite3.Connection, forged_amount: int
) -> None:
    """The order's visible evidence is clean (the genuine capture), so only the target event's own amount can tell."""
    run = forged_capture_run(conn, amount=forged_amount)
    preview = plan(conn, run)
    approve(conn, run, preview)
    result = execute(conn, run, preview)
    assert result.status is ActionStatus.SUCCEEDED
    assert fulfilments_unlike_their_order(conn, run.run_id) == {}
    assert [(item.order_id, item.reasons) for item in preview.excluded] == [(ORDER_1, ("amount_mismatch",))]
    assert (result.fulfilled_order_ids, result.restored_processing_value_paise) == ((ORDER_2,), CONTROL_AMOUNT)
    assert storage.get_fulfilment(conn, run_id=run.run_id, order_id=ORDER_1) is None


def test_a_target_is_judged_on_its_own_amount_while_its_observation_is_not_visible(conn: sqlite3.Connection) -> None:
    run = forged_capture_run(conn, amount=1)
    [forged] = [row for row in storage.list_inbox_rows(conn, run_id=run.run_id) if row.event_id == "evt_forged"]
    projection = state.load_order_projection(conn, run.run_id, ORDER_1, PLAN_AT, stale_after_ms=STALE_AFTER_MS)
    assert projection.exclusions == ()
    assert "evt_forged" not in {item.source_ref for payment in projection.payments for item in payment.history}
    assessment = policy.assess_target(
        conn, run_id=run.run_id, inbox_row=forged, now_ms=PLAN_AT, stale_after_ms=STALE_AFTER_MS
    )
    assert (assessment.amount_paise, assessment.reasons) == (1, ("amount_mismatch",))


def test_a_target_in_another_currency_is_refused_by_the_policy_not_left_to_the_merchant_handler(
    conn: sqlite3.Connection,
) -> None:
    """Without the check, the only stop was the corrected handler refusing anything but INR (its dry run raised)."""
    run = forged_capture_run(conn, amount=ORDER_AMOUNT, currency="USD")
    preview = plan(conn, run)
    assert {item.order_id: item.reasons for item in preview.excluded} == {ORDER_1: ("currency_mismatch",)}
    assert [dry_run.order_id for dry_run in preview.dry_runs] == [ORDER_2]


def test_a_plan_built_without_the_check_is_refused_by_the_recheck_at_execution(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy version is unchanged, so a plan stored before the check can still be approved; the recheck inside the
    execution transaction judges its target again and rejects the whole plan."""
    run = forged_capture_run(conn, amount=1)
    judged = policy.assess_target
    with monkeypatch.context() as before_the_check:
        before_the_check.setattr(policy, "assess_target",
                                 lambda *args, **kwargs: judged(*args, **kwargs).model_copy(update={"reasons": ()}))
        preview = plan(conn, run)
    assert set(preview.plan.target_ids) == {ORDER_1, ORDER_2}
    approve(conn, run, preview)
    with pytest.raises(executor.PlanRejectedError) as caught:
        execute(conn, run, preview)
    assert caught.value.result.reasons == (f"target_ineligible: {ORDER_1}: amount_mismatch",)
    assert storage.list_fulfilments(conn, run_id=run.run_id) == ()


def test_the_queue_holds_the_order_for_the_reason_the_planner_excludes_it(conn: sqlite3.Connection) -> None:
    """order_cases mirrors the planner's checks on the event it would reprocess, so the two still agree."""
    run = forged_capture_run(conn, amount=1)
    preview = plan(conn, run)
    queue = order_cases.classify_orders(conn, run_id=run.run_id, as_of_ms=PLAN_AT, stale_after_ms=STALE_AFTER_MS)
    assert planner_disagreements(queue, preview) == {}
    [case] = [case for case in queue.cases if case.order_id == ORDER_1]
    assert (case.state, case.blockers) == (CaseState.PAYMENT_ORDER_MISMATCH, ("amount_mismatch",))


# --------------------------------------------------------------------------- controls


def test_a_visible_capture_at_another_amount_is_excluded_exactly_as_before(conn: sqlite3.Connection) -> None:
    """Dated before its receipt, the event's observation is in the projection, which already reports the mismatch."""
    run = forged_capture_run(conn, amount=1, occurred_at_ms=START_MS + 500)
    preview = plan(conn, run)
    assert [(item.order_id, item.reasons) for item in preview.excluded] == [
        (ORDER_1, ("amount_mismatch", "conflicting_evidence"))
    ]
    assert [target.id for target in preview.targets] == [ORDER_2]


def test_a_capture_dated_after_its_receipt_at_its_orders_amount_is_still_a_target(conn: sqlite3.Connection) -> None:
    """Only the amount and currency are bound: an envelope time after the receipt alone excludes nothing, as before."""
    run = forged_capture_run(conn, amount=ORDER_AMOUNT)
    preview = plan(conn, run)
    assert preview.excluded == ()
    assert {(target.id, target.event_id, target.amount_paise) for target in preview.targets} == {
        (ORDER_1, "evt_forged", ORDER_AMOUNT), (ORDER_2, "evt_control", CONTROL_AMOUNT),
    }
    approve(conn, run, preview)
    result = execute(conn, run, preview)
    assert set(result.fulfilled_order_ids) == {ORDER_1, ORDER_2}
    assert result.restored_processing_value_paise == ORDER_AMOUNT + CONTROL_AMOUNT
    assert fulfilments_unlike_their_order(conn, run.run_id) == {}
