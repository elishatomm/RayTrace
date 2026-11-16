"""The 23 design-spike tests (experiments/feasibility_spike.py) ported to the real modules.

Each keeps the spike test's number and intent. Two spike simplifications are fixed,
not ported: same-time ties are a conflict whatever the event IDs (tests 09-12 plus
test_state.py), and refunds are reported apart from captures (test 22).
"""
from __future__ import annotations

import itertools
import json
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
    SECRET_A,
    START_MS,
    FailingControlConnection,
    FakeHandler,
    SeededRun,
    add_order,
    count_rows,
    deliver,
    fulfil_reply,
    observation,
    payment_event,
)
from recovery_workbench import ingestion, metrics, policy, state, storage, tools, worker
from recovery_workbench.contracts import (
    ContractError,
    PaymentStatus,
    ReceiveOutcome,
    WorkerResult,
)
from recovery_workbench.simulator import signing

CAPTURED = PaymentStatus.CAPTURED


def _signed(envelope_bytes: bytes, event_id: str = "evt_SYNTHETIC_0001") -> dict[str, str]:
    return {
        signing.EVENT_ID_HEADER: event_id,
        signing.SIGNATURE_HEADER: signing.sign_body(envelope_bytes, SECRET_A),
    }


def _receive(conn, run: SeededRun, headers: dict[str, str], raw_body: bytes) -> ingestion.DeliveryResponse:
    return ingestion.receive_webhook(
        conn, run_id=run.run_id, integration_id=run.integration_id, headers=headers, raw_body=raw_body,
        received_at_ms=START_MS + 2_000,
    )


def test_01_valid_raw_signature(conn, run_a):
    """Spike test_01_valid_raw_signature: a body signed over its exact bytes is accepted."""
    response = deliver(conn, run_a, "evt_SYNTHETIC_0001")
    assert response.outcome is ReceiveOutcome.ACCEPTED
    assert response.http_status == 200


def test_02_mutated_raw_body_rejected(conn, run_a):
    """Spike test_02_mutated_raw_body_rejected: re-serialised JSON with the original signature fails."""
    raw_body = signing.serialise_event(payment_event())
    changed = json.dumps(json.loads(raw_body)).encode()  # same JSON value, different bytes
    assert changed != raw_body
    response = _receive(conn, run_a, _signed(raw_body), changed)
    assert response.outcome is ReceiveOutcome.REJECTED_SIGNATURE
    assert response.http_status == 401


def test_03_invalid_signature_has_no_db_effect(conn, run_a):
    """Spike test_03_invalid_signature_has_no_db_effect: a bad signature stores no event."""
    raw_body = signing.serialise_event(payment_event())
    headers = {signing.EVENT_ID_HEADER: "evt_SYNTHETIC_0001", signing.SIGNATURE_HEADER: "bad"}
    _receive(conn, run_a, headers, raw_body)
    assert count_rows(conn, "webhook_inbox", run_a.run_id) == 0
    assert count_rows(conn, "payment_observations", run_a.run_id) == 0


def test_04_malformed_signed_payload_rejected(conn, run_a):
    """Spike test_04_malformed_signed_payload_rejected: a signed body outside the subset is a 400."""
    raw_body = b'{"not_a_payment": 1}'
    response = _receive(conn, run_a, _signed(raw_body), raw_body)
    assert response.outcome is ReceiveOutcome.REJECTED_SCHEMA
    assert response.http_status == 400


def test_05_concurrent_duplicate_receipts(conn, run_a, db_path: Path):
    """Spike test_05_concurrent_duplicate_receipts: 32 concurrent copies give one acceptance."""
    headers, raw_body = signing.signed_delivery(payment_event(), event_id="evt_SYNTHETIC_0001", secret_ref=SECRET_A)

    def receive_once(_: int) -> ReceiveOutcome:
        with closing(storage.connect(db_path)) as own:
            return _receive(own, run_a, headers, raw_body).outcome

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(receive_once, range(32)))
    assert outcomes.count(ReceiveOutcome.ACCEPTED) == 1
    assert outcomes.count(ReceiveOutcome.DUPLICATE) == 31


def test_06_conflicting_event_id(conn, run_a):
    """Spike test_06_conflicting_event_id: a reused event ID with different bytes is flagged."""
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    response = deliver(conn, run_a, "evt_SYNTHETIC_0001", payment_id=PAY_2)
    assert response.outcome is ReceiveOutcome.CONFLICTING_ID


def test_07_different_events_one_business_effect(conn, run_a):
    """Spike test_07_different_events_one_business_effect: two events, one fulfilment."""
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    deliver(conn, run_a, "evt_SYNTHETIC_0002", occurred_at_ms=START_MS + 1_500)
    steps = worker.run_worker(conn, run_id=run_a.run_id, handler=FakeHandler(), now_ms=START_MS + 5_000)
    assert [step.result for step in steps] == [WorkerResult.FULFILLED, WorkerResult.ALREADY_FULFILLED]
    assert count_rows(conn, "fulfilments", run_a.run_id) == 1


def test_08_concurrent_fulfilment(conn, run_a, db_path: Path):
    """Spike test_08_concurrent_fulfilment: 32 concurrent worker steps fulfil the order once."""
    add_order(conn, run_a)
    for number in range(32):
        deliver(conn, run_a, f"evt_SYNTHETIC_{number:04d}", occurred_at_ms=START_MS + 1_000 + number)
    def work(_: int) -> WorkerResult | None:
        with closing(storage.connect(db_path)) as own:
            step = worker.process_next(own, run_id=run_a.run_id, handler=FakeHandler(), now_ms=START_MS + 9_000)
            return None if step is None else step.result

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(32)))
    assert results.count(WorkerResult.FULFILLED) == 1
    assert results.count(WorkerResult.ALREADY_FULFILLED) == 31
    assert count_rows(conn, "fulfilments", run_a.run_id) == 1


def test_09_state_order_permutations():
    """Spike test_09_state_order_permutations: arrival order never changes the projection."""
    events = [
        observation(PaymentStatus.CREATED, 1, 1),
        observation(PaymentStatus.AUTHORIZED, 2, 4),
        observation(CAPTURED, 3, 3),
    ]
    projections = {state.project_payment(list(order), 5) for order in itertools.permutations(events)}
    assert len(projections) == 1
    assert projections.pop().status is CAPTURED


def test_10_late_authorization():
    """Spike test_10_late_authorization: failed may later become authorised, and is flagged."""
    events = [observation(PaymentStatus.FAILED, 10, 11), observation(PaymentStatus.AUTHORIZED, 20, 21)]
    assert state.project_payment(events, 15).status is PaymentStatus.FAILED
    later = state.project_payment(events, 22)
    assert later.status is PaymentStatus.AUTHORIZED
    assert later.late_authorization


def test_11_as_of_excludes_future_knowledge():
    """Spike test_11_as_of_excludes_future_knowledge: evidence known after T is invisible at T."""
    events = [observation(PaymentStatus.CREATED, 1, 1), observation(CAPTURED, 5, 50)]
    assert state.project_payment(events, 20).status is PaymentStatus.CREATED


def test_12_contradiction_requests_reconciliation():
    """Spike test_12_contradiction_requests_reconciliation: failure after capture keeps capture, flags conflict."""
    projection = state.project_payment([observation(CAPTURED, 3, 3), observation(PaymentStatus.FAILED, 4, 4)], 5)
    assert projection.status is CAPTURED
    assert projection.needs_reconciliation


def test_13_authorized_does_not_fulfil(conn, run_a):
    """Spike test_13_authorized_does_not_fulfil: a handler cannot fulfil an authorised-only payment."""
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001", status=PaymentStatus.AUTHORIZED)
    greedy = FakeHandler(reply=fulfil_reply)  # asks to fulfil whatever the status
    step = worker.process_next(conn, run_id=run_a.run_id, handler=greedy, now_ms=START_MS + 5_000)
    assert step is not None
    assert step.result is WorkerResult.MANUAL_REVIEW
    assert "not_captured" in step.review_reasons
    assert count_rows(conn, "fulfilments", run_a.run_id) == 0


def test_14_amount_or_currency_mismatch(conn, run_a):
    """Spike test_14_amount_or_currency_mismatch: capture amount or currency unlike the order stops fulfilment."""
    add_order(conn, run_a, ORDER_1, payment_ids=(PAY_1,), amount_paise=10_000)
    add_order(conn, run_a, ORDER_2, payment_ids=(PAY_2,), amount_paise=10_000)
    deliver(conn, run_a, "evt_SYNTHETIC_0001", amount_paise=100)
    deliver(conn, run_a, "evt_SYNTHETIC_0002", payment_id=PAY_2, order_id=ORDER_2, amount_paise=10_000, currency="USD")
    steps = worker.run_worker(conn, run_id=run_a.run_id, handler=FakeHandler(), now_ms=START_MS + 5_000)
    assert [step.result for step in steps] == [WorkerResult.MANUAL_REVIEW, WorkerResult.MANUAL_REVIEW]
    assert "amount_mismatch" in steps[0].review_reasons
    assert "currency_mismatch" in steps[1].review_reasons
    assert count_rows(conn, "fulfilments", run_a.run_id) == 0


def test_15_multiple_captures_review(conn, run_a):
    """Spike test_15_multiple_captures_review: two captures for one order go to manual review."""
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    deliver(conn, run_a, "evt_SYNTHETIC_0002", payment_id=PAY_2, occurred_at_ms=START_MS + 1_200)
    steps = worker.run_worker(conn, run_id=run_a.run_id, handler=FakeHandler(), now_ms=START_MS + 5_000)
    assert [step.result for step in steps] == [WorkerResult.MANUAL_REVIEW, WorkerResult.MANUAL_REVIEW]
    assert all("multiple_captures" in step.review_reasons for step in steps)
    assert count_rows(conn, "fulfilments", run_a.run_id) == 0


def test_16_run_isolation(conn, run_a, run_b):
    """Spike test_16_run_isolation: the same event ID and body are independent in two runs."""
    assert deliver(conn, run_a, "evt_SYNTHETIC_0001").outcome is ReceiveOutcome.ACCEPTED
    assert deliver(conn, run_b, "evt_SYNTHETIC_0001").outcome is ReceiveOutcome.ACCEPTED


def test_17_transaction_failure_rolls_back(conn, run_a):
    """Spike test_17_transaction_failure_rolls_back: a failed commit stores nothing; a retry is accepted."""
    failing = FailingControlConnection(conn, fail_on="COMMIT")
    with pytest.raises(ingestion.ReceiptNotDurableError):
        deliver(failing, run_a, "evt_SYNTHETIC_0001")  # type: ignore[arg-type]
    assert count_rows(conn, "webhook_inbox", run_a.run_id) == 0
    assert deliver(conn, run_a, "evt_SYNTHETIC_0001").outcome is ReceiveOutcome.ACCEPTED


def test_18_approval_rechecks_state():
    """Spike test_18_approval_rechecks_state: every precondition is rechecked against current state."""
    plan = policy.PlanFacts(plan_hash="p1", run_id="r", evidence_version="2")
    approval = policy.ApprovalFacts(approver=policy.ApproverKind.HUMAN, plan_hash="p1", run_id="r", expires_at_ms=200)
    current = policy.CurrentFacts(
        run_id="r", evidence_version="2", snapshot_age_ms=2_000, status=CAPTURED, captures_for_order=1,
        worker_healthy=True, cancelled=False, refunded=False,
    )
    assert policy.failed_preconditions(plan, approval, current, 100) == ()
    changes = {
        "evidence_version": "3", "snapshot_age_ms": 31_000, "status": PaymentStatus.FAILED, "captures_for_order": 2,
        "worker_healthy": False, "cancelled": True, "refunded": True, "run_id": "another",
    }
    for field, value in changes.items():
        changed = policy.CurrentFacts(**{**current.__dict__, field: value})
        assert policy.failed_preconditions(plan, approval, changed, 100), field
    model_approval = policy.ApprovalFacts(policy.ApproverKind.MODEL, "p1", "r", 200)
    assert policy.FailedPrecondition.APPROVER_NOT_HUMAN in policy.failed_preconditions(plan, model_approval, current, 100)
    other_hash = policy.ApprovalFacts(policy.ApproverKind.HUMAN, "changed", "r", 200)
    assert policy.FailedPrecondition.PLAN_HASH_MISMATCH in policy.failed_preconditions(plan, other_hash, current, 100)
    assert policy.failed_preconditions(plan, approval, current, 200) == (policy.FailedPrecondition.APPROVAL_EXPIRED,)


def test_19_low_sample_suppression():
    """Spike test_19_low_sample_suppression: a tiny current cohort never alerts."""
    assert not metrics.is_degraded(950, 1000, 2, 3)


def test_20_large_observed_drop_detected():
    """Spike test_20_large_observed_drop_detected: a large drop on enough attempts alerts."""
    assert metrics.is_degraded(950, 1000, 25, 100)


def test_21_mix_shift_not_within_cohort_degradation():
    """Spike test_21_mix_shift_not_within_cohort_degradation: per-cohort ratios fixed means no cohort alert."""
    # Overall ratio drops 0.904 -> 0.676 while both component cohorts stay fixed.
    assert metrics.is_degraded(904, 1000, 676, 1000)
    assert not metrics.is_degraded(784, 800, 196, 200)
    assert not metrics.is_degraded(120, 200, 480, 800)


def test_22_restored_processing_is_not_new_capture(conn, run_a):
    """Spike test_22_restored_processing_is_not_new_capture: processing adds zero capture; refunds stay separate."""
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    before = metrics.capture_ledger(state.load_order_projection(conn, run_a.run_id, ORDER_1, START_MS + 5_000).payments)

    steps = worker.run_worker(conn, run_id=run_a.run_id, handler=FakeHandler(), now_ms=START_MS + 6_000)
    assert [step.result for step in steps] == [WorkerResult.FULFILLED]
    # The capture re-issued under a new ID, 1 ms later: new bytes, so a second event (the same bytes are a duplicate).
    redelivery = deliver(conn, run_a, "evt_SYNTHETIC_0002", occurred_at_ms=START_MS + 1_001)
    assert redelivery.outcome is ReceiveOutcome.ACCEPTED
    after_projection = state.load_order_projection(conn, run_a.run_id, ORDER_1, START_MS + 7_000)
    after = metrics.capture_ledger(after_projection.payments + after_projection.payments)
    assert metrics.capture_change(before, after).additional_captured_paise == 0
    restored = metrics.restored_processing_value(
        [metrics.ProcessedOrder(ORDER_1, AMOUNT, capture_known_at_ms=START_MS + 1_050)], intervention_at_ms=START_MS + 6_000
    )
    assert restored == AMOUNT

    refunded_payment = state.project_payment(
        [observation(PaymentStatus.REFUNDED, 8_000, 8_000, payment_id=PAY_2, amount_paise=30_000,
                     amount_refunded_paise=1_000)],
        9_000,
    )
    change = metrics.capture_change(before, metrics.capture_ledger([*after_projection.payments, refunded_payment]))
    assert (change.additional_captured_paise, change.additional_refunded_paise) == (30_000, 1_000)


def test_23_unknown_fields_do_not_change_policy():
    """Spike test_23_unknown_fields_do_not_change_policy: unknown tools and extra arguments are refused."""
    call = tools.validate_tool_call("get_order_journey", {"order_id": "order_SYNTHETIC_0001"})
    assert call.arguments == {"order_id": "order_SYNTHETIC_0001"}
    for name, arguments in [
        ("refund_all", {}),
        ("get_order_journey", {"order_id": "order_SYNTHETIC_0001", "run_id": "another"}),
        ("get_order_journey", {"order_id": "order_SYNTHETIC_0001", "approval": "human"}),
        ("get_order_journey", {}),
    ]:
        with pytest.raises(ContractError):
            tools.validate_tool_call(name, arguments)


# --------------------------------------------------------------------------- beyond the spike


def test_a_decline_of_exactly_ten_points_meets_the_threshold():
    """Exact fractions: 0.95 - 0.85 in floats is 0.0999..., which would wrongly miss the threshold."""
    assert 0.95 - 0.85 < 0.10
    assert metrics.is_degraded(950, 1000, 425, 500)  # 95% -> 85% on enough attempts
    with pytest.raises(ContractError):
        metrics.is_degraded(5, 4, 1, 50)


def test_tool_arguments_must_be_short_identifiers():
    for value in ("../../etc/passwd", "1; DROP TABLE orders", "http://127.0.0.1:9", "", 42):
        with pytest.raises(ContractError):
            tools.validate_tool_call("get_order_journey", {"order_id": value})


def test_the_capture_ledger_never_nets_refunds_or_counts_conflicts():
    clean = state.project_payment(
        [observation(PaymentStatus.REFUNDED, 1, 1, payment_id=PAY_1, amount_refunded_paise=1_000)], 9
    )
    disputed = state.project_payment(
        [observation(CAPTURED, 1, 1, payment_id=PAY_2), observation(CAPTURED, 2, 2, payment_id=PAY_2, amount_paise=5)], 9
    )
    ledger = metrics.capture_ledger([clean, disputed])
    assert (ledger.captured_paise, ledger.refunded_paise) == (AMOUNT, 1_000)
    assert ledger.unreconciled_payment_ids == (PAY_2,)
