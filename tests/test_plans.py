"""Recovery plans: candidates, exclusions, dry runs, the SPEC §10 plan hash, replay instructions, escalation.

``build_batch`` creates the PLAN M2 acceptance batch with storage, ingestion and the worker plus a
merchant composition carrying the payload@2 fault; test_executor.py reuses it.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import (
    INTEGRATION_A,
    MERCHANT_A,
    RUN_A,
    START_MS,
    SeededRun,
    add_order,
    deliver,
    observation,
    seed_run,
    store_observations,
)
from test_tools import seed

from recovery_workbench import cli, diagnosis_store, fixes, order_cases, plans, policy, settings, state, storage, worker
from recovery_workbench.contracts import (
    CheckType,
    ContractError,
    DiagnosisOutput,
    ExclusionReason,
    InboxStatus,
    Operation,
    PaymentObservation,
    PaymentStatus,
    PlanStatus,
    ReceiveOutcome,
    ScopeError,
    canonical_json,
    sha256_hex,
)
from recovery_workbench.order_cases import CaseState, NextStep
from recovery_workbench.simulator import driver, families, merchant, signing
from recovery_workbench.simulator.world import generate_world

MINUTE = 60_000
T_BASELINE = START_MS + 5_000  # the baseline worker ran up to here
T_FAULT = START_MS + 10 * MINUTE  # payload@2 deployed
NOW = START_MS + 40 * MINUTE  # planning time
STALE_AFTER_MS = 35 * MINUTE
PLAN_TTL_MS = 15 * MINUTE
OPERATOR = "op_unit_operator"
CORRECT_FIX = "payload@5"
WRONG_FIX = "config@5"  # corrects a different module, so the payload@2 fault survives it
INCIDENT = "inc_unit_0001"
UNKNOWN_ORDER = signing.synthetic_id("order", 999)


def order_id(number: int) -> str:
    return signing.synthetic_id("order", number)


def pay_id(number: int, suffix: str = "") -> str:
    return f"{signing.synthetic_id('pay', number)}{suffix}"


def add_incident(conn: sqlite3.Connection, run_id: str, incident_id: str = INCIDENT) -> str:
    """The incidents table belongs to Agent A's phase-2 module; this inserts the minimum row a diagnosis needs."""
    with storage.write_transaction(conn):
        conn.execute(
            'INSERT INTO incidents (run_id, incident_id, "trigger", cohort, window_start_ms, window_end_ms,'
            " evidence_cutoff_ms, state, measured_impact) VALUES (?, ?, 'order_completion_drop', '{}', ?, ?, ?, 'open', '{}')",
            (run_id, incident_id, START_MS, NOW, NOW),
        )
    return incident_id


def worker_regression_output() -> DiagnosisOutput:
    return DiagnosisOutput(
        hypothesis="merchant_worker_regression",
        faulting_change_id="dep_payload_accessor",
        affected_method="none",
        evidence_level="supported",
        supporting_evidence_ids=("ev_worker_errors",),
        contradicting_evidence_ids=(),
        missing_checks=(),
        next_check="validate_corrected_handler",
        proposed_operation="reprocess_inbox",
        rationale="Captured payments fail in the parser after the payload deployment.",
    )


def add_diagnosis(conn: sqlite3.Connection, run_id: str, incident_id: str = INCIDENT) -> str:
    stored = diagnosis_store.record_rules_diagnosis(
        conn, run_id=run_id, incident_id=incident_id, packet_hash="0" * 64, output=worker_regression_output(),
        rules_version="rules-unit",
    )
    return stored.diagnosis_id


@dataclass
class Batch:
    run: SeededRun
    deployed: merchant.DeployedMerchant
    diagnosis_id: str
    eligible: dict[str, int]  # order ID -> amount in paise
    expected_exclusions: dict[str, set[str]]
    processed_before_fault: list[str]
    superseded_order: str
    duplicate_delivery_orders: list[str] = field(default_factory=list)


def _deliver_capture(
    conn: sqlite3.Connection, run: SeededRun, event_id: str, *, order: str, payment: str, at: int,
    amount: int, currency: str = "INR", status: PaymentStatus = PaymentStatus.CAPTURED, received_at: int | None = None,
    expected: ReceiveOutcome = ReceiveOutcome.ACCEPTED,
) -> None:
    response = deliver(
        conn, run, event_id, status=status, payment_id=payment, order_id=order, occurred_at_ms=at,
        amount_paise=amount, currency=currency, received_at_ms=received_at,
    )
    assert response.outcome is expected, response


def build_batch(conn: sqlite3.Connection) -> Batch:
    """56 orders plus one event for an unknown order, after a payload@2 deployment broke the worker.

    41 orders are eligible, including order 50, whose payment failed and was captured later: since
    policy-v3 (owner decision Q8) a late authorisation alone excludes nothing. Three orders were
    processed before the fault and have nothing pending. Order 55's late payment was authorised but
    never captured, so it has no capture event and is never a candidate. The rest each carry one
    reason they must not be reprocessed; orders 54 and 56 are late authorisations that stay out
    for another reason (a second capture, and an uncaptured late payment beside a captured retry).
    """
    run = seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, "fixture_secret_a")
    baseline = merchant.compose(version="dep_baseline")
    faulty = merchant.compose({"payload": 2}, version="dep_payload_accessor")

    # Processed before the fault: three orders, plus order 44 whose capture is re-sent later.
    for number in (41, 42, 43, 44):
        add_order(conn, run, order_id(number), payment_ids=(pay_id(number),), amount_paise=100_000 + number)
        _deliver_capture(conn, run, f"evt_early_{number}", order=order_id(number), payment=pay_id(number),
                         at=START_MS + number * 10, amount=100_000 + number)
    early_steps = worker.run_worker(conn, run_id=run.run_id, handler=baseline, now_ms=T_BASELINE)
    assert {step.result.value for step in early_steps} == {"fulfilled"}

    # A stale capture: received early but the worker did not run again until after the fault.
    add_order(conn, run, order_id(51), payment_ids=(pay_id(51),), amount_paise=51_000)
    _deliver_capture(conn, run, "evt_stale_51", order=order_id(51), payment=pay_id(51), at=T_BASELINE + 1_000, amount=51_000)

    eligible: dict[str, int] = {}
    for number in range(1, 41):
        amount = 10_000 + number * 1_000
        add_order(conn, run, order_id(number), payment_ids=(pay_id(number),), amount_paise=amount)
        _deliver_capture(conn, run, f"evt_capture_{number}", order=order_id(number), payment=pay_id(number),
                         at=T_FAULT + number * 10_000, amount=amount)
        eligible[order_id(number)] = amount
    duplicated = [order_id(1), order_id(2), order_id(3)]
    for number in (1, 2, 3):  # byte-identical redelivery: one logical event, so still one target
        _deliver_capture(conn, run, f"evt_capture_{number}", order=order_id(number), payment=pay_id(number),
                         at=T_FAULT + number * 10_000, amount=10_000 + number * 1_000, expected=ReceiveOutcome.DUPLICATE)
    # The provider resent order 4's capture byte for byte under a new event ID, and the receiver from before Q32 (a),
    # which had no body lookup, stored it as a second inbox row. Every database it wrote can hold such a pair: the
    # same bytes received at the same time, told apart by inbox ID alone. The batch keeps one, stored that way.
    with pytest.MonkeyPatch.context() as previous_receiver:
        previous_receiver.setattr(storage, "find_inbox_row_by_body", lambda *_, **__: None)
        _deliver_capture(conn, run, "evt_capture_4_resent", order=order_id(4), payment=pay_id(4),
                         at=T_FAULT + 4 * 10_000, amount=14_000)

    # Already fulfilled before the fault; the provider re-issues that old capture under a new ID after the fault,
    # 1 ms later, so new bytes (the same bytes would now be a duplicate), and it now fails in the worker.
    _deliver_capture(conn, run, "evt_resent_44", order=order_id(44), payment=pay_id(44), at=START_MS + 441,
                     amount=100_044, received_at=T_FAULT + 30_000)
    add_order(conn, run, order_id(45), payment_ids=(pay_id(45),), amount_paise=45_000, cancelled_at_ms=T_FAULT + 5 * MINUTE)
    _deliver_capture(conn, run, "evt_capture_45", order=order_id(45), payment=pay_id(45), at=T_FAULT + 1_000, amount=45_000)
    add_order(conn, run, order_id(46), payment_ids=(pay_id(46, "A"), pay_id(46, "B")), amount_paise=46_000)
    _deliver_capture(conn, run, "evt_capture_46", order=order_id(46), payment=pay_id(46, "A"), at=T_FAULT + 2_000, amount=46_000)
    _deliver_capture(conn, run, "evt_auth_46b", order=order_id(46), payment=pay_id(46, "B"), at=T_FAULT + 2_500,
                     amount=46_000, status=PaymentStatus.AUTHORIZED)
    add_order(conn, run, order_id(47), payment_ids=(pay_id(47, "A"), pay_id(47, "B")), amount_paise=47_000)
    _deliver_capture(conn, run, "evt_capture_47a", order=order_id(47), payment=pay_id(47, "A"), at=T_FAULT + 3_000, amount=47_000)
    _deliver_capture(conn, run, "evt_capture_47b", order=order_id(47), payment=pay_id(47, "B"), at=T_FAULT + 3_500, amount=47_000)
    add_order(conn, run, order_id(48), payment_ids=(pay_id(48),), amount_paise=48_000)
    _deliver_capture(conn, run, "evt_capture_48", order=order_id(48), payment=pay_id(48), at=T_FAULT + 4_000, amount=47_900)
    add_order(conn, run, order_id(49), payment_ids=(pay_id(49),), amount_paise=49_000)
    _deliver_capture(conn, run, "evt_capture_49", order=order_id(49), payment=pay_id(49), at=T_FAULT + 5_000, amount=49_000)
    conflicting = deliver(conn, run, "evt_capture_49", status=PaymentStatus.CAPTURED, payment_id=pay_id(49),
                          order_id=order_id(49), occurred_at_ms=T_FAULT + 5_000, amount_paise=4_900)
    assert conflicting.outcome.value == "conflicting_id"
    add_order(conn, run, order_id(50), payment_ids=(pay_id(50),), amount_paise=50_000)
    _deliver_capture(conn, run, "evt_failed_50", order=order_id(50), payment=pay_id(50), at=T_FAULT + 1 * MINUTE,
                     amount=50_000, status=PaymentStatus.FAILED)
    _deliver_capture(conn, run, "evt_capture_50", order=order_id(50), payment=pay_id(50), at=T_FAULT + 6 * MINUTE, amount=50_000)
    eligible[order_id(50)] = 50_000
    # Told the first payment failed, the customer paid again on the same order; the first one was
    # then captured as well. Two captures: a person must refund one, so nothing is reprocessed.
    add_order(conn, run, order_id(54), payment_ids=(pay_id(54, "A"), pay_id(54, "B")), amount_paise=54_000)
    _deliver_capture(conn, run, "evt_failed_54a", order=order_id(54), payment=pay_id(54, "A"), at=T_FAULT + 7 * MINUTE,
                     amount=54_000, status=PaymentStatus.FAILED)
    _deliver_capture(conn, run, "evt_capture_54b", order=order_id(54), payment=pay_id(54, "B"), at=T_FAULT + 8 * MINUTE,
                     amount=54_000)
    _deliver_capture(conn, run, "evt_capture_54a", order=order_id(54), payment=pay_id(54, "A"), at=T_FAULT + 9 * MINUTE,
                     amount=54_000)
    # A failure followed by an authorisation that was never captured: no capture event at all.
    add_order(conn, run, order_id(55), payment_ids=(pay_id(55),), amount_paise=55_000)
    _deliver_capture(conn, run, "evt_failed_55", order=order_id(55), payment=pay_id(55), at=T_FAULT + 7 * MINUTE,
                     amount=55_000, status=PaymentStatus.FAILED)
    _deliver_capture(conn, run, "evt_auth_55", order=order_id(55), payment=pay_id(55), at=T_FAULT + 9 * MINUTE,
                     amount=55_000, status=PaymentStatus.AUTHORIZED)
    # The same uncaptured late authorisation beside a captured retry: the open attempt keeps the order out.
    add_order(conn, run, order_id(56), payment_ids=(pay_id(56, "A"), pay_id(56, "B")), amount_paise=56_000)
    _deliver_capture(conn, run, "evt_failed_56a", order=order_id(56), payment=pay_id(56, "A"), at=T_FAULT + 7 * MINUTE,
                     amount=56_000, status=PaymentStatus.FAILED)
    _deliver_capture(conn, run, "evt_capture_56b", order=order_id(56), payment=pay_id(56, "B"), at=T_FAULT + 8 * MINUTE,
                     amount=56_000)
    _deliver_capture(conn, run, "evt_auth_56a", order=order_id(56), payment=pay_id(56, "A"), at=T_FAULT + 9 * MINUTE,
                     amount=56_000, status=PaymentStatus.AUTHORIZED)
    add_order(conn, run, order_id(52), payment_ids=(pay_id(52),), amount_paise=52_000, refunded=True)
    _deliver_capture(conn, run, "evt_capture_52", order=order_id(52), payment=pay_id(52), at=T_FAULT + 7_000, amount=52_000)
    add_order(conn, run, order_id(53), payment_ids=(pay_id(53),), amount_paise=53_000)
    _deliver_capture(conn, run, "evt_capture_53", order=order_id(53), payment=pay_id(53), at=T_FAULT + 8_000,
                     amount=53_000, currency="USD")
    _deliver_capture(conn, run, "evt_capture_unknown", order=UNKNOWN_ORDER, payment=pay_id(999), at=T_FAULT + 9_000, amount=9_900)

    steps = worker.run_worker(conn, run_id=run.run_id, handler=faulty, now_ms=NOW)
    assert steps
    assert all(step.result.value == "failed" for step in steps)
    assert not [row for row in storage.list_inbox_rows(conn, run_id=run.run_id) if row.processing_status is InboxStatus.PENDING]

    add_incident(conn, run.run_id)
    return Batch(
        run=run,
        deployed=faulty,
        diagnosis_id=add_diagnosis(conn, run.run_id),
        eligible=eligible,
        expected_exclusions={
            order_id(44): {ExclusionReason.ALREADY_FULFILLED.value},
            order_id(45): {ExclusionReason.CANCELLED.value},
            order_id(46): {ExclusionReason.UNRESOLVED_ATTEMPT.value},
            order_id(47): {ExclusionReason.MULTIPLE_CAPTURES.value},
            order_id(48): {ExclusionReason.AMOUNT_MISMATCH.value},
            order_id(49): {ExclusionReason.CONFLICTING_EVIDENCE.value},
            order_id(51): {ExclusionReason.STALE_SNAPSHOT.value},
            order_id(52): {ExclusionReason.REFUNDED.value},
            order_id(53): {ExclusionReason.CURRENCY_MISMATCH.value},
            order_id(54): {ExclusionReason.MULTIPLE_CAPTURES.value},
            order_id(56): {ExclusionReason.UNRESOLVED_ATTEMPT.value},
            UNKNOWN_ORDER: {ExclusionReason.UNKNOWN_ORDER.value},
        },
        processed_before_fault=[order_id(41), order_id(42), order_id(43)],
        superseded_order=order_id(4),
        duplicate_delivery_orders=duplicated,
    )


def validate(conn: sqlite3.Connection, batch: Batch, fix_id: str = CORRECT_FIX) -> fixes.FixValidation:
    return fixes.validate_fix(
        conn, run_id=batch.run.run_id, deployed=batch.deployed, fix_id=fix_id, diagnosis_id=batch.diagnosis_id,
        operator_id=OPERATOR, now_ms=NOW,
    )


def build_plan(conn: sqlite3.Connection, batch: Batch, validation: fixes.FixValidation) -> plans.PlanPreview:
    return plans.build_reprocess_plan(
        conn, run_id=batch.run.run_id, fix_validation_id=validation.validation_id, deployed=batch.deployed,
        now_ms=NOW, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS,
    )


@pytest.fixture
def batch(conn: sqlite3.Connection) -> Batch:
    return build_batch(conn)


# --------------------------------------------------------------------------- the batch plan


def test_batch_has_at_least_fifty_orders(conn: sqlite3.Connection, batch: Batch) -> None:
    assert len(storage.list_orders(conn, run_id=batch.run.run_id)) >= 50


def test_plan_targets_exactly_the_eligible_orders_with_their_value(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    assert {target.id for target in preview.targets} == set(batch.eligible)
    assert preview.target_count == 41
    assert preview.target_value_paise == sum(batch.eligible.values())
    assert all(target.amount_paise == batch.eligible[target.id] for target in preview.targets)
    assert preview.plan.status is PlanStatus.DRAFT
    assert preview.plan.operation is Operation.REPROCESS_INBOX


def test_every_other_candidate_is_excluded_for_its_own_reason(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    excluded = {item.order_id: set(item.reasons) for item in preview.excluded}
    assert excluded == batch.expected_exclusions
    for reason, orders in preview.exclusions_by_reason.items():
        assert all(reason in batch.expected_exclusions[order] for order in orders)
    assert preview.candidate_orders == len(batch.eligible) + len(batch.expected_exclusions)


def _late_authorised(conn: sqlite3.Connection, batch: Batch, number: int) -> state.OrderProjection:
    projection = state.load_order_projection(conn, batch.run.run_id, order_id(number), NOW, stale_after_ms=STALE_AFTER_MS)
    assert any(payment.late_authorization for payment in projection.payments), "the batch should make it late"
    return projection


def test_a_late_authorised_capture_is_a_target(conn: sqlite3.Connection, batch: Batch) -> None:
    """Policy-v3: the payment failed first, but its capture is real and the order's only one."""
    preview = build_plan(conn, batch, validate(conn, batch))
    projection = _late_authorised(conn, batch, 50)
    assert projection.exclusions == ()
    assert projection.single_captured_payment_id == pay_id(50)
    target = next(target for target in preview.targets if target.id == order_id(50))
    assert (target.payment_id, target.amount_paise) == (pay_id(50), 50_000)
    assert "late_authorization" not in preview.exclusions_by_reason


def test_a_late_authorised_order_with_a_second_capture_stays_out(conn: sqlite3.Connection, batch: Batch) -> None:
    """The guard for a customer who paid again on the same order after being told it failed."""
    preview = build_plan(conn, batch, validate(conn, batch))
    assert ExclusionReason.MULTIPLE_CAPTURES in _late_authorised(conn, batch, 54).exclusions
    excluded = {item.order_id: set(item.reasons) for item in preview.excluded}
    assert excluded[order_id(54)] == {ExclusionReason.MULTIPLE_CAPTURES.value}
    assert order_id(54) not in {target.id for target in preview.targets}


def test_an_authorised_but_uncaptured_late_payment_is_never_planned(conn: sqlite3.Connection, batch: Batch) -> None:
    """Planning only fulfils money already captured, so new capture stays 0."""
    preview = build_plan(conn, batch, validate(conn, batch))
    planned = {target.id for target in preview.targets}
    excluded = {item.order_id: set(item.reasons) for item in preview.excluded}
    assert ExclusionReason.NOT_CAPTURED in _late_authorised(conn, batch, 55).exclusions
    assert order_id(55) not in planned
    assert order_id(55) not in excluded  # no capture event: never a candidate
    assert ExclusionReason.UNRESOLVED_ATTEMPT in _late_authorised(conn, batch, 56).exclusions
    assert order_id(56) not in planned
    assert excluded[order_id(56)] == {ExclusionReason.UNRESOLVED_ATTEMPT.value}


def test_orders_processed_before_the_fault_are_not_candidates(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    mentioned = {target.id for target in preview.targets} | {item.order_id for item in preview.excluded}
    assert not mentioned & set(batch.processed_before_fault)


def test_duplicate_deliveries_never_add_targets(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    target_orders = [target.id for target in preview.targets]
    assert len(target_orders) == len(set(target_orders))
    for duplicated in batch.duplicate_delivery_orders:
        assert target_orders.count(duplicated) == 1
    # Order 4's two rows are the legacy pair: the same bytes received at the same time, so the inbox ID decides.
    original, resent = [row for row in storage.list_inbox_rows(conn, run_id=batch.run.run_id)
                        if row.event_id in ("evt_capture_4", "evt_capture_4_resent")]
    assert (resent.body_sha256, resent.received_at_ms) == (original.body_sha256, original.received_at_ms)
    assert preview.superseded_inbox_ids == {batch.superseded_order: (resent.inbox_id,)}
    chosen = next(target for target in preview.targets if target.id == batch.superseded_order)
    assert chosen.event_id == "evt_capture_4"  # earliest received wins, then the lower inbox ID: deterministic


def test_every_target_passed_the_dry_run_and_nothing_was_written(conn: sqlite3.Connection, batch: Batch) -> None:
    before = (len(storage.list_fulfilments(conn, run_id=batch.run.run_id)),
              [row.processing_status for row in storage.list_inbox_rows(conn, run_id=batch.run.run_id)])
    preview = build_plan(conn, batch, validate(conn, batch))
    assert {run.order_id for run in preview.dry_runs if run.passed} == set(batch.eligible)
    assert all(run.outcome and run.outcome["action"] == "fulfil" for run in preview.dry_runs)
    after = (len(storage.list_fulfilments(conn, run_id=batch.run.run_id)),
             [row.processing_status for row in storage.list_inbox_rows(conn, run_id=batch.run.run_id)])
    assert after == before


def test_preview_lists_the_preconditions_rechecked_at_execution(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    assert preview.preconditions == tuple(policy.RECHECK_DESCRIPTIONS.values())
    assert preview.deployed_version == batch.deployed.version != preview.corrected_version


def test_stored_plan_round_trips_and_hashes_its_own_fields(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    stored = plans.require_plan(conn, run_id=batch.run.run_id, plan_id=preview.plan.plan_id)
    assert stored == preview.plan
    assert stored.plan_hash == policy.compute_plan_hash(
        operation=Operation.REPROCESS_INBOX, target_ids=stored.target_ids, evidence_version=stored.evidence_version,
        policy_version=stored.policy_version, run_id=stored.run_id, expires_at_ms=stored.expires_at_ms,
    )
    assert stored.expires_at_ms == NOW + PLAN_TTL_MS


def test_a_wrong_fix_fails_validation_and_blocks_planning(conn: sqlite3.Connection, batch: Batch) -> None:
    validation = validate(conn, batch, WRONG_FIX)
    assert not validation.passed
    with pytest.raises(plans.PlanRefusedError, match="did not pass"):
        build_plan(conn, batch, validation)
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_a_fix_validated_against_other_deployed_code_cannot_plan(conn: sqlite3.Connection, batch: Batch) -> None:
    validation = validate(conn, batch)
    redeployed = merchant.compose({"payload": 2, "notifications": 2}, version="dep_later_notifications")
    with pytest.raises(plans.PlanRefusedError, match="validated against"):
        plans.build_reprocess_plan(
            conn, run_id=batch.run.run_id, fix_validation_id=validation.validation_id, deployed=redeployed,
            now_ms=NOW, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS,
        )


def test_a_validation_from_another_run_is_out_of_scope(conn: sqlite3.Connection, batch: Batch) -> None:
    validation = validate(conn, batch)
    seed_run(conn, "run_unit_other", MERCHANT_A, "int_unit_other", "fixture_secret_a")
    with pytest.raises(ScopeError):
        plans.build_reprocess_plan(
            conn, run_id="run_unit_other", fix_validation_id=validation.validation_id, deployed=batch.deployed,
            now_ms=NOW, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS,
        )


def test_planning_refuses_replay_time_behind_the_run_clock(conn: sqlite3.Connection, batch: Batch) -> None:
    validation = validate(conn, batch)
    with storage.write_transaction(conn):
        storage.advance_clock(conn, batch.run.run_id, NOW + MINUTE)
    with pytest.raises(ContractError, match="cannot go back"):
        build_plan(conn, batch, validation)


def test_a_healthy_deployed_handler_reproduces_nothing_and_its_plan_has_no_targets(tmp_path: Path) -> None:
    """The operator's gates after a wrong worker-regression diagnosis on a run whose worker is healthy.

    The CLI demo reached them until val-5, which now refuses that diagnosis first when the packet holds no
    worker_error item. They still matter for a wrong worker regression that val-5 lets through, so the
    diagnosis is stored directly, as a rules answer never validated against a packet.
    """
    conn, summary = seed(tmp_path, "method_degradation")
    try:
        run_id = summary.run_id
        diagnosis_id = add_diagnosis(conn, run_id, summary.incident_ids[0])
        validation = cli.validate_fix(conn, run_id=run_id, fix_id=CORRECT_FIX, diagnosis_id=diagnosis_id,
                                      operator=OPERATOR)
        fulfilled = storage.list_fulfilments(conn, run_id=run_id)
        preview = cli.build_plan(conn, run_id=run_id, fix_validation_id=validation.validation_id)
        fulfilled_after = storage.list_fulfilments(conn, run_id=run_id)
    finally:
        conn.close()
    assert (validation.reproduced, validation.deployed_failures, validation.passed) == (False, (), True)
    assert (preview.target_count, preview.target_value_paise, preview.targets) == (0, 0, ())
    assert fulfilled_after == fulfilled


# --------------------------------------------------------------------------- plan hash canonicalisation (SPEC §10)

HASH_INPUTS = {
    "operation": Operation.REPROCESS_INBOX,
    "target_ids": ["order_B", "order_A"],
    "evidence_version": "e" * 64,
    "policy_version": "policy-v2",
    "run_id": "run_hash",
    "expires_at_ms": 1_794_390_000_000,
}


def test_plan_hash_is_sha256_of_the_canonical_spec_fields() -> None:
    expected_bytes = (
        b'{"evidence_version":"' + b"e" * 64 + b'","expires_at_ms":1794390000000,"operation":"reprocess_inbox",'
        b'"policy_version":"policy-v2","run_id":"run_hash","target_ids":["order_A","order_B"]}'
    )
    assert canonical_json(
        {**{key: value for key, value in HASH_INPUTS.items() if key != "operation"}, "operation": "reprocess_inbox",
         "target_ids": ["order_A", "order_B"]}
    ) == expected_bytes
    assert policy.compute_plan_hash(**HASH_INPUTS) == sha256_hex(expected_bytes)


def test_plan_hash_ignores_target_listing_order() -> None:
    reordered = {**HASH_INPUTS, "target_ids": list(reversed(HASH_INPUTS["target_ids"]))}
    assert policy.compute_plan_hash(**reordered) == policy.compute_plan_hash(**HASH_INPUTS)


@pytest.mark.parametrize(
    ("field_name", "changed"),
    [
        ("operation", Operation.ESCALATE),
        ("target_ids", ["order_A", "order_C"]),
        ("evidence_version", "f" * 64),
        ("policy_version", "policy-v3"),
        ("run_id", "run_other"),
        ("expires_at_ms", 1_794_390_000_001),
    ],
)
def test_every_hashed_field_changes_the_plan_hash(field_name: str, changed: object) -> None:
    assert policy.compute_plan_hash(**{**HASH_INPUTS, field_name: changed}) != policy.compute_plan_hash(**HASH_INPUTS)


def test_repeated_target_ids_are_refused() -> None:
    with pytest.raises(ContractError, match="unique"):
        policy.compute_plan_hash(**{**HASH_INPUTS, "target_ids": ["order_A", "order_A"]})


# --------------------------------------------------------------------------- replay instructions (D3) and escalation


def test_replay_lists_provider_captures_that_never_reached_the_inbox(conn: sqlite3.Connection, batch: Batch) -> None:
    from conftest import observation, store_observations

    # The provider snapshot shows two captures whose webhooks were rejected before durable receipt.
    for number in (61, 62):
        add_order(conn, batch.run, order_id(number), payment_ids=(pay_id(number),), amount_paise=61_000)
    store_observations(conn, [
        observation(PaymentStatus.CAPTURED, T_FAULT + 1_000, NOW - MINUTE, payment_id=pay_id(61), order_id=order_id(61),
                    amount_paise=61_000),
        observation(PaymentStatus.CAPTURED, T_FAULT + 2_000, NOW - MINUTE, payment_id=pay_id(62), order_id=order_id(62),
                    amount_paise=61_000),
    ])
    business_before = (
        conn.execute("SELECT COUNT(*) FROM fulfilments").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM webhook_inbox").fetchone()[0],
        [order.model_dump() for order in storage.list_orders(conn, run_id=batch.run.run_id)],
    )
    instructions = plans.prepare_provider_replay(
        conn, run_id=batch.run.run_id, incident_id=INCIDENT, diagnosis_id=batch.diagnosis_id, now_ms=NOW,
        ttl_ms=PLAN_TTL_MS, stale_after_ms=settings.load_settings().stale_after_seconds * 1_000,
    )
    assert [target.id for target in instructions.targets] == [pay_id(61), pay_id(62)]
    assert instructions.total_value_paise == 122_000
    assert instructions.provider_calls_made == 0
    assert sum(pay_id(61) in line for line in instructions.instructions) == 1
    assert instructions.plan.operation is Operation.PREPARE_PROVIDER_REPLAY
    assert instructions.plan.status is PlanStatus.DRAFT
    business_after = (
        conn.execute("SELECT COUNT(*) FROM fulfilments").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM webhook_inbox").fetchone()[0],
        [order.model_dump() for order in storage.list_orders(conn, run_id=batch.run.run_id)],
    )
    assert business_after == business_before


# Blockers first, as for reprocessing: a capture the provider shows but no webhook carried is held, never a target,
# while its order carries any exclusion the reprocess planner applies. Each case adds one blocking condition.

REPLAY_ORDER = order_id(70)
REPLAY_AMOUNT = 70_000
CAPTURE = pay_id(70)  # its captured event never reached the inbox
OTHER = pay_id(70, "B")  # a second payment attempt on the same order
FRESH_AT = NOW - MINUTE
STALE_AT = START_MS + 1_000  # older than STALE_AFTER_MS at NOW
TWO_ATTEMPTS: dict[str, object] = {"payment_ids": (CAPTURE, OTHER)}


def provider_says(
    payment: str = CAPTURE, status: PaymentStatus = PaymentStatus.CAPTURED, *, at: int = FRESH_AT,
    order: str = REPLAY_ORDER, amount: int = REPLAY_AMOUNT, currency: str = "INR",
) -> PaymentObservation:
    """A provider snapshot of one payment; in these cases no webhook carries anything."""
    return observation(status, at, at, observation_id=f"obs_replay_{payment}_{status.value}_{at}", payment_id=payment,
                       order_id=order, amount_paise=amount, currency=currency)


def held_for(*reasons: str, payment: str = CAPTURE, order: str = REPLAY_ORDER) -> tuple[str, str, tuple[str, ...]]:
    return payment, order, reasons


@dataclass(frozen=True)
class ReplayCase:
    observations: tuple[PaymentObservation, ...]
    held: frozenset[tuple[str, str, tuple[str, ...]]]  # (payment, order, reasons)
    order: dict[str, object] | None = field(default_factory=dict)  # add_order options; None: no order record
    fulfilled: bool = False
    targets: tuple[str, ...] = ()


REPLAY_CASES: dict[str, ReplayCase] = {
    "nothing_blocks": ReplayCase((provider_says(),), frozenset(), targets=(CAPTURE,)),
    "amount_mismatch": ReplayCase((provider_says(amount=REPLAY_AMOUNT - 100),), frozenset({held_for("amount_mismatch")})),
    "currency_mismatch": ReplayCase((provider_says(currency="USD"),), frozenset({held_for("currency_mismatch")})),
    "unknown_order": ReplayCase(
        (provider_says(order=order_id(71)),), frozenset({held_for("unknown_order", order=order_id(71))}), order=None),
    "multiple_captures": ReplayCase(
        (provider_says(), provider_says(OTHER)),
        frozenset({held_for("multiple_captures"), held_for("multiple_captures", payment=OTHER)}), order=TWO_ATTEMPTS),
    "cancelled": ReplayCase((provider_says(),), frozenset({held_for("cancelled")}), order={"cancelled_at_ms": FRESH_AT}),
    "refunded": ReplayCase((provider_says(),), frozenset({held_for("refunded")}), order={"refunded": True}),
    # Agent C's D3 finding: the capture is fresh, but an older failed attempt's provider state is stale.
    "stale_older_attempt": ReplayCase(
        (provider_says(OTHER, PaymentStatus.FAILED, at=STALE_AT), provider_says()), frozenset({held_for("stale_snapshot")}),
        order=TWO_ATTEMPTS),
    "stale_capture": ReplayCase((provider_says(at=STALE_AT),), frozenset({held_for("stale_snapshot")})),
    "never_observed_attempt": ReplayCase(
        (provider_says(),), frozenset({held_for("unresolved_attempt", "stale_snapshot")}), order=TWO_ATTEMPTS),
    "unresolved_extra_attempt": ReplayCase(
        (provider_says(), provider_says(OTHER, PaymentStatus.AUTHORIZED)), frozenset({held_for("unresolved_attempt")}),
        order=TWO_ATTEMPTS),
    "conflicting_evidence": ReplayCase(
        (provider_says(), provider_says(status=PaymentStatus.FAILED, at=FRESH_AT + 1)),
        frozenset({held_for("conflicting_evidence")})),
    "already_fulfilled": ReplayCase((provider_says(),), frozenset({held_for("already_fulfilled")}), fulfilled=True),
}


def replay_at_now(conn: sqlite3.Connection, run_id: str = RUN_A) -> plans.ReplayInstructions:
    return plans.prepare_provider_replay(conn, run_id=run_id, incident_id=INCIDENT, diagnosis_id=None, now_ms=NOW,
                                         ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS)


@pytest.mark.parametrize("name", sorted(REPLAY_CASES))
def test_replay_holds_every_capture_whose_order_the_reprocess_planner_would_exclude(
    conn: sqlite3.Connection, run_a: SeededRun, name: str
) -> None:
    case = REPLAY_CASES[name]
    if case.order is not None:
        add_order(conn, run_a, REPLAY_ORDER, **{"payment_ids": (CAPTURE,), "amount_paise": REPLAY_AMOUNT, **case.order})
    store_observations(conn, case.observations)
    if case.fulfilled:
        with storage.write_transaction(conn):
            storage.insert_fulfilment(conn, storage.FulfilmentRecord(
                run_id=RUN_A, order_id=REPLAY_ORDER, payment_id=CAPTURE, amount_paise=REPLAY_AMOUNT, currency="INR",
                fulfilled_at_ms=FRESH_AT, via="worker",
            ))
    add_incident(conn, RUN_A)
    instructions = replay_at_now(conn)
    found = {(item.payment_id, item.order_id, item.reasons) for item in instructions.held_for_reconciliation}
    assert found == case.held
    assert [target.id for target in instructions.targets] == list(case.targets)
    assert instructions.total_value_paise == REPLAY_AMOUNT * len(case.targets)
    for payment_id, held_order, reasons in found:
        # Exactly the order's exclusions as the reprocess planner reads them for a candidate (policy.assess_target).
        projection = state.load_order_projection(conn, RUN_A, held_order, NOW, stale_after_ms=STALE_AFTER_MS)
        assert reasons == tuple(reason.value for reason in projection.exclusions)
        [line] = [line for line in instructions.instructions if line.startswith(f"Do not resend {payment_id} ")]
        assert all(reason in line for reason in reasons)
        assert not [line for line in instructions.instructions if line.startswith(f"Resend payment.captured for {payment_id} ")]


def test_replay_judges_staleness_by_the_threshold_it_is_given(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    """The stale older attempt holds the capture at the planner's threshold, and not at a longer one."""
    add_order(conn, run_a, REPLAY_ORDER, payment_ids=(CAPTURE, OTHER), amount_paise=REPLAY_AMOUNT)
    store_observations(conn, [provider_says(OTHER, PaymentStatus.FAILED, at=STALE_AT), provider_says()])
    add_incident(conn, RUN_A)
    held = replay_at_now(conn).held_for_reconciliation
    assert [(item.payment_id, item.reasons) for item in held] == [(CAPTURE, ("stale_snapshot",))]
    longer = plans.prepare_provider_replay(conn, run_id=RUN_A, incident_id=INCIDENT, diagnosis_id=None, now_ms=NOW,
                                           ttl_ms=PLAN_TTL_MS, stale_after_ms=NOW - START_MS)
    assert ([target.id for target in longer.targets], longer.held_for_reconciliation) == ([CAPTURE], ())
    with pytest.raises(ContractError, match="stale_after_ms"):
        plans.prepare_provider_replay(conn, run_id=RUN_A, incident_id=INCIDENT, diagnosis_id=None, now_ms=NOW,
                                      ttl_ms=PLAN_TTL_MS, stale_after_ms=0)


# (evidence version, plan hash) of the plan below, measured on b47a1c3, before the instructions said what to do when
# the dashboard refuses a resend. The plan hash binds the operation, the targets, the evidence and policy versions, the
# run and the expiry, never the instruction text.
REFUSED_RESEND_PLAN = (
    "d2155578a21c44119bbbc5b4f9889471cdfa7ba0b6488f4343e3ff080ccf63db",
    "b5fffc94a3656060b2455f8eef31fa14724cc88a7cedd66bf23766ceba076c40",
)


def test_the_replay_instructions_send_a_refused_resend_to_escalation_and_leave_the_plan_hash_alone(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """Owner decision Q29 (a): the provider's dashboard refuses to resend an event it recorded as answered 2xx."""
    add_order(conn, run_a, REPLAY_ORDER, payment_ids=(CAPTURE,), amount_paise=REPLAY_AMOUNT)
    store_observations(conn, [provider_says()])
    add_incident(conn, RUN_A)
    instructions = plans.prepare_provider_replay(
        conn, run_id=RUN_A, incident_id=INCIDENT, diagnosis_id=None, now_ms=NOW, ttl_ms=PLAN_TTL_MS,
        stale_after_ms=STALE_AFTER_MS, plan_id="plan_q29_refused_resend",
    )
    plan = instructions.plan
    assert [target.id for target in instructions.targets] == [CAPTURE]
    version_and_hash = (plan.evidence_version, plan.plan_hash)
    assert version_and_hash == REFUSED_RESEND_PLAN
    assert plans.require_plan(conn, run_id=RUN_A, plan_id=plan.plan_id) == plan  # the stored row holds no text either
    # Before the first payment line, the operator reads what a refusal means: escalate, never retry or plan it again.
    lines = instructions.instructions
    [refusal] = [index for index, line in enumerate(lines) if "already recorded a 2xx answer" in line]
    assert refusal < next(index for index, line in enumerate(lines) if line.startswith("Resend payment.captured for "))
    assert "escalate the order" in lines[refusal]
    assert "do not try again or prepare the replay again" in lines[refusal]


# Agent C's sweep world for this D3 parameter set (seeds 14101-14200 are phase 11's). On it the replay plan used to
# target two captures whose orders the queue holds for a provider refresh; 2 of the 5 such captures in the sweep.
D3_SWEEP_SEED = 14120
D3_PARAMS: dict[str, object] = {"fault": "receiver@2", "harmless": "handler@2", "harmless_position": "after"}


def test_replay_holds_the_d3_captures_whose_older_attempt_is_stale(tmp_path: Path) -> None:
    run = next(run for run in families.load_manifest().runs if run.family == "D3" and run.params == D3_PARAMS)
    world = generate_world(families.spec_for(run.model_copy(update={"split": "dev"})), D3_SWEEP_SEED)
    stale_after_ms = settings.load_settings().stale_after_seconds * 1_000
    with closing(storage.connect(tmp_path / "d3.sqlite")) as conn:
        driver.seed_world(conn, world, oracle_dir=None)
        now = storage.require_run(conn, world.run_id).clock_ms
        add_incident(conn, world.run_id)
        replay = plans.prepare_provider_replay(conn, run_id=world.run_id, incident_id=INCIDENT, diagnosis_id=None,
                                               now_ms=now, ttl_ms=PLAN_TTL_MS, stale_after_ms=stale_after_ms)
        queue = order_cases.classify_orders(conn, run_id=world.run_id, as_of_ms=now, stale_after_ms=stale_after_ms)
    cases = {case.order_id: case for case in queue.cases}
    assert [item.reasons for item in replay.held_for_reconciliation] == [("stale_snapshot",)] * 2
    for item in replay.held_for_reconciliation:
        case = cases[item.order_id]
        assert (case.state, case.next_step) == (CaseState.PAYMENT_STATE_UNRESOLVED, NextStep.REFRESH_THEN_REEVALUATE)
        assert case.blockers == ("stale_snapshot", "capture_event_not_received")
        [capture] = [payment for payment in case.payments if payment.status is PaymentStatus.CAPTURED]
        # What made it a target before: a capture seen only by snapshot, fresh, whose own observations agree.
        assert (capture.payment_id, capture.webhook_observations, capture.conflict_reasons, capture.stale) == (
            item.payment_id, 0, (), False)
        # What holds it now: an older attempt on the same order that failed, and whose provider state is stale.
        older = [payment for payment in case.payments if payment.payment_id != capture.payment_id]
        assert older
        assert all(payment.status is PaymentStatus.FAILED and payment.stale for payment in older)
        assert all(payment.last_known_at_ms < capture.last_known_at_ms for payment in older)
    assert {target.order_id for target in replay.targets} == {
        case.order_id for case in queue.cases if case.state is CaseState.NOTIFICATION_NOT_ACCEPTED}
    assert len(replay.targets) == 62


def test_the_cli_and_api_replays_pass_the_reprocess_planners_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each passes the threshold its own reprocess planner uses. The API's is its server's settings object (owner
    decision Q31), which keeps its default here; the CLI's is WORKBENCH_STALE_AFTER_SECONDS, read at call time."""
    from test_api import create_run, diagnose, log_in, make_client, post

    monkeypatch.setenv("WORKBENCH_STALE_AFTER_SECONDS", "1234")
    passed: list[tuple[str, object]] = []
    planner, replay = plans.build_reprocess_plan, plans.prepare_provider_replay

    def planning(*args: object, **kwargs: object) -> plans.PlanPreview:
        passed.append(("reprocess plan", kwargs["stale_after_ms"]))
        return planner(*args, **kwargs)  # type: ignore[arg-type]

    def replaying(*args: object, **kwargs: object) -> plans.ReplayInstructions:
        passed.append(("replay", kwargs["stale_after_ms"]))
        return replay(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(plans, "build_reprocess_plan", planning)
    monkeypatch.setattr(plans, "prepare_provider_replay", replaying)
    with make_client(tmp_path) as client:
        log_in(client)
        run_id = create_run(client)
        found = diagnose(client, run_id)["diagnosis"]
        validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate", {"diagnosis_id": found["diagnosis_id"]})
        post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        post(client, f"/api/runs/{run_id}/plans", {"operation": "prepare_provider_replay",
                                                   "diagnosis_id": found["diagnosis_id"]})
        workbench = client.app.state.workbench
    with closing(storage.connect(workbench.database)) as conn:
        stored = diagnosis_store.get_diagnosis(conn, run_id=run_id, diagnosis_id=found["diagnosis_id"])
        assert stored is not None
        printed: list[str] = []
        cli._demo_replay(cli._Narrator(printed.append), conn, run_id, stored)
    assert passed == [("reprocess plan", 3_600_000), ("replay", 3_600_000), ("replay", 1_234_000)]
    assert workbench.settings.stale_after_seconds * 1_000 == 3_600_000
    assert printed[0].startswith("[1] prepare provider replay")


@pytest.mark.parametrize(("server_seconds", "environment_seconds"), [
    pytest.param(3_600, 864_000, id="server-hour-env-ten-days"),
    pytest.param(864_000, 3_600, id="server-ten-days-env-hour"),
])
def test_the_api_replay_targets_and_holds_captures_as_the_api_queue_judges_their_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_seconds: int, environment_seconds: int
) -> None:
    """Owner decision Q31: the replay takes its threshold from the server's settings object, as the queue does.

    The server's threshold and WORKBENCH_STALE_AFTER_SECONDS differ. On the D3 sweep world above, an hour holds two
    captures for stale provider state, and ten days holds none.
    """
    from test_api import diagnose, log_in, make_client, post, queue_at_clock

    monkeypatch.setenv("WORKBENCH_STALE_AFTER_SECONDS", str(environment_seconds))
    run = next(run for run in families.load_manifest().runs if run.family == "D3" and run.params == D3_PARAMS)
    world = generate_world(families.spec_for(run.model_copy(update={"split": "dev"})), D3_SWEEP_SEED)
    with make_client(tmp_path, stale_after_seconds=server_seconds) as client:
        log_in(client)
        with closing(storage.connect(client.app.state.workbench.database)) as conn:
            driver.seed_world(conn, world, oracle_dir=None)
        found = diagnose(client, world.run_id)["diagnosis"]
        replay = post(client, f"/api/runs/{world.run_id}/plans",
                      {"operation": "prepare_provider_replay", "diagnosis_id": found["diagnosis_id"]})
        queue, rows = queue_at_clock(client, world.run_id)  # the replay took the last operator step: the queue's time
        database = client.app.state.workbench.database
    targets = {target["order_id"] for target in replay["targets"]}
    assert targets == {order_id for order_id, row in rows.items() if row["state"] == "notification_not_accepted"}
    for held in replay["held_for_reconciliation"]:
        assert rows[held["order_id"]]["state"] != "notification_not_accepted"
        assert set(held["reasons"]) <= set(rows[held["order_id"]]["blockers"])
    # The environment's threshold would send other captures to replay, so the agreement is not chance.
    with closing(storage.connect(database)) as conn:
        by_environment = order_cases.classify_orders(conn, run_id=world.run_id, as_of_ms=queue["as_of_ms"],
                                                     stale_after_ms=environment_seconds * 1000)
    assert {case.order_id for case in by_environment.cases if case.state is CaseState.NOTIFICATION_NOT_ACCEPTED} != (
        targets)


def test_escalation_records_the_exception_and_the_evidence_needed(conn: sqlite3.Connection, batch: Batch) -> None:
    record = plans.escalate(
        conn, run_id=batch.run.run_id, incident_id=INCIDENT, diagnosis_id=batch.diagnosis_id, operator_id=OPERATOR,
        summary="Late authorisation on a payment the customer was told had failed",
        evidence_needed=[CheckType.FETCH_PROVIDER_SNAPSHOT, CheckType.INSPECT_ORDER_JOURNEY],
        subject_ids=[order_id(50)], now_ms=NOW,
    )
    action = conn.execute("SELECT * FROM actions WHERE action_id = ?", (record.action_id,)).fetchone()
    assert action["status"] == "succeeded"
    assert json.loads(action["after"])["evidence_needed"] == ["fetch_provider_snapshot", "inspect_order_journey"]
    assert record.plan.operation is Operation.ESCALATE
    assert record.plan.target_ids == (order_id(50),)
    with pytest.raises(ScopeError):
        plans.escalate(
            conn, run_id=batch.run.run_id, incident_id=INCIDENT, diagnosis_id=None, operator_id="qwen3:4b",
            summary="model tries to escalate", evidence_needed=[], subject_ids=[], now_ms=NOW,
        )


# --------------------------------------------------------------------------- dry runs


class StandInHandler:
    """A corrected handler stand-in whose reply each test chooses; it never touches the database."""

    def __init__(self, reply: object = None, *, raises: BaseException | None = None) -> None:
        self.reply, self.raises, self.version = reply, raises, "rev_stand_in"

    def handle(self, event: dict[str, object]) -> object:
        if self.raises is not None:
            raise self.raises
        return self.reply

    def describe_failure(self, exc: BaseException) -> object:
        from recovery_workbench.contracts import FailureReport

        return FailureReport(exception_category=None, stack_trace=f"{type(exc).__name__}: {exc}\n")


def first_target(conn: sqlite3.Connection, batch: Batch) -> tuple[storage.InboxRecord, policy.TargetAssessment]:
    preview = build_plan(conn, batch, validate(conn, batch))
    target = preview.targets[0]
    row = storage.get_inbox_row(conn, run_id=batch.run.run_id, inbox_id=target.inbox_id)
    assessment = policy.assess_target(conn, run_id=batch.run.run_id, inbox_row=row, now_ms=NOW, stale_after_ms=STALE_AFTER_MS)
    return row, assessment


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (StandInHandler(raises=KeyError("id")), policy.PlanExclusion.DRY_RUN_RAISED),
        (StandInHandler({"action": "ignore"}), policy.PlanExclusion.DRY_RUN_NO_FULFILMENT),
        (StandInHandler({"action": "fulfil"}), policy.PlanExclusion.DRY_RUN_MISMATCH),
        (StandInHandler("not a mapping"), policy.PlanExclusion.DRY_RUN_MISMATCH),
    ],
)
def test_a_dry_run_that_does_not_fulfil_the_exact_target_excludes_it(
    conn: sqlite3.Connection, batch: Batch, handler: StandInHandler, reason: policy.PlanExclusion
) -> None:
    row, assessment = first_target(conn, batch)
    result = plans.dry_run_target(handler, row, assessment)  # type: ignore[arg-type]
    assert not result.passed
    assert result.reason == reason.value


def test_a_dry_run_must_match_every_field_of_the_target(conn: sqlite3.Connection, batch: Batch) -> None:
    row, assessment = first_target(conn, batch)
    exact = {"action": "fulfil", "order_id": assessment.order_id, "payment_id": assessment.payment_id,
             "amount_paise": assessment.amount_paise, "currency": assessment.currency}
    assert plans.dry_run_target(StandInHandler(exact), row, assessment).passed  # type: ignore[arg-type]
    for field_name, other in (("order_id", "order_SYNTHETIC_0777"), ("payment_id", "pay_SYNTHETIC_0777"),
                              ("amount_paise", assessment.amount_paise + 1), ("currency", "USD")):
        result = plans.dry_run_target(StandInHandler({**exact, field_name: other}), row, assessment)  # type: ignore[arg-type]
        assert result.reason == policy.PlanExclusion.DRY_RUN_MISMATCH.value
        assert field_name in str(result.detail)
