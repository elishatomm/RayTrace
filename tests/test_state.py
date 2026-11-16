"""Payment and order projection: evidence accumulation, as-of visibility and exclusion reasons."""
from __future__ import annotations

import itertools

import pytest

from conftest import (
    AMOUNT,
    ORDER_1,
    ORDER_2,
    PAY_1,
    PAY_2,
    RUN_A,
    START_MS,
    add_order,
    deliver,
    observation,
    store_observations,
)
from recovery_workbench import state, storage
from recovery_workbench.contracts import ContractError, ExclusionReason, PaymentStatus, ScopeError

CAPTURED = PaymentStatus.CAPTURED
FAILED = PaymentStatus.FAILED
AUTHORIZED = PaymentStatus.AUTHORIZED
CREATED = PaymentStatus.CREATED
Reason = state.ConflictReason


def semantics(projection: state.PaymentProjection) -> tuple[object, ...]:
    """Everything that carries meaning; history order is presentation only."""
    return (projection.status, projection.conflict_reasons, projection.late_authorization, projection.stale,
            projection.amount_paise, projection.currency, projection.order_ids)


def test_nothing_visible_is_unknown_never_failed():
    assert state.project_payment([], 100, payment_id=PAY_1).status is PaymentStatus.UNKNOWN
    known_later = [observation(FAILED, 10, 500)]
    assert state.project_payment(known_later, 100).status is PaymentStatus.UNKNOWN
    occurs_later = [observation(CAPTURED, 150, 90)]  # known at 90 but claims to occur at 150
    projection = state.project_payment(occurs_later, 100)
    assert projection.status is PaymentStatus.UNKNOWN
    assert projection.history == ()


@pytest.mark.parametrize("other_status", [CAPTURED, AUTHORIZED, PaymentStatus.REFUNDED])
@pytest.mark.parametrize("failed_id_sorts_first", [True, False])
def test_same_time_contradiction_is_a_conflict_whatever_the_ids(other_status, failed_id_sorts_first):
    failed_id, other_id = ("evt_a", "evt_b") if failed_id_sorts_first else ("evt_b", "evt_a")
    events = [
        observation(FAILED, 10, 11, observation_id=failed_id),
        observation(other_status, 10, 12, observation_id=other_id,
                    amount_refunded_paise=AMOUNT if other_status is PaymentStatus.REFUNDED else 0),
    ]
    results = {semantics(state.project_payment(list(order), 20)) for order in itertools.permutations(events)}
    assert len(results) == 1
    projection = state.project_payment(events, 20)
    assert Reason.SAME_TIME_CONTRADICTION in projection.conflict_reasons
    assert projection.needs_reconciliation
    assert projection.status is other_status  # success evidence is kept, not overwritten by the failure
    assert not projection.late_authorization


def test_an_old_event_cannot_undo_a_capture():
    events = [observation(CAPTURED, 3, 3), observation(AUTHORIZED, 2, 10), observation(CREATED, 1, 12)]
    for as_of in (3, 10, 12, 50):
        projection = state.project_payment(events, as_of)
        assert projection.status is CAPTURED
        assert not projection.needs_reconciliation
    assert len(state.project_payment(events, 50).history) == 3  # the historical events are retained


def test_a_later_non_capture_observation_never_erases_capture():
    assert state.project_payment([observation(CAPTURED, 3, 3), observation(CREATED, 5, 5)], 9).status is CAPTURED


def test_a_failure_after_success_is_a_conflict_not_an_overwrite():
    projection = state.project_payment([observation(AUTHORIZED, 3, 3), observation(FAILED, 4, 4)], 9)
    assert projection.status is AUTHORIZED
    assert projection.conflict_reasons == (Reason.FAILURE_AFTER_SUCCESS,)


def test_a_failure_followed_by_capture_is_a_flagged_late_authorisation():
    projection = state.project_payment([observation(FAILED, 1, 2), observation(CAPTURED, 5, 6)], 9)
    assert projection.status is CAPTURED
    assert projection.late_authorization
    assert not projection.needs_reconciliation


def test_disagreeing_amount_currency_or_order_is_a_conflict():
    amount = state.project_payment([observation(AUTHORIZED, 1, 1), observation(CAPTURED, 2, 2, amount_paise=AMOUNT + 1)], 9)
    assert amount.conflict_reasons == (Reason.AMOUNT_DISAGREEMENT,)
    assert amount.amount_paise is None
    currency = state.project_payment([observation(AUTHORIZED, 1, 1), observation(CAPTURED, 2, 2, currency="USD")], 9)
    assert currency.conflict_reasons == (Reason.CURRENCY_DISAGREEMENT,)
    order = state.project_payment([observation(AUTHORIZED, 1, 1), observation(CAPTURED, 2, 2, order_id=ORDER_2)], 9)
    assert order.conflict_reasons == (Reason.ORDER_DISAGREEMENT,)


def test_a_refund_is_reported_as_refunded_with_capture_evidence():
    projection = state.project_payment(
        [observation(CAPTURED, 1, 1), observation(PaymentStatus.REFUNDED, 5, 5, amount_refunded_paise=AMOUNT)], 9
    )
    assert projection.status is PaymentStatus.REFUNDED
    assert projection.has_capture_evidence
    assert projection.amount_refunded_paise == AMOUNT


def test_staleness_is_flagged_but_never_changes_the_status():
    events = [observation(CAPTURED, 100, 100)]
    assert state.project_payment(events, 300, stale_after_ms=150).stale
    assert state.project_payment(events, 300, stale_after_ms=150).status is CAPTURED
    assert not state.project_payment(events, 300, stale_after_ms=200).stale
    assert not state.project_payment(events, 300).stale
    missing = state.project_payment([], 300, stale_after_ms=150, payment_id=PAY_1)
    assert (missing.status, missing.stale) == (PaymentStatus.UNKNOWN, True)


def test_unknown_status_observations_carry_no_evidence():
    projection = state.project_payment([observation(CREATED, 1, 1), observation(PaymentStatus.UNKNOWN, 5, 5)], 9)
    assert projection.status is CREATED
    assert projection.last_known_at_ms == 1


def test_project_payment_takes_one_payments_observations():
    two_payments = [observation(CAPTURED, 1, 1), observation(CAPTURED, 1, 1, payment_id=PAY_2)]
    with pytest.raises(ContractError):
        state.project_payment(two_payments, 9)


# --------------------------------------------------------------------------- order projection (DB-backed)

T = START_MS + 10_000


def test_a_single_clean_capture_is_recovery_eligible(conn, run_a):
    add_order(conn, run_a)
    store_observations(conn, [observation(CAPTURED, START_MS + 1, START_MS + 2)])
    projection = state.load_order_projection(conn, RUN_A, ORDER_1, T)
    assert projection.exclusions == ()
    assert projection.single_captured_payment_id == PAY_1
    assert projection.is_recovery_eligible


def test_a_missing_snapshot_remains_unknown(conn, run_a):
    add_order(conn, run_a)
    projection = state.load_order_projection(conn, RUN_A, ORDER_1, T, stale_after_ms=60_000)
    [payment] = projection.payments
    assert payment.status is PaymentStatus.UNKNOWN
    assert set(projection.exclusions) == {
        ExclusionReason.NOT_CAPTURED, ExclusionReason.UNRESOLVED_ATTEMPT, ExclusionReason.STALE_SNAPSHOT
    }
    assert not projection.is_recovery_eligible


def _order_with_capture(conn, run, **order_options) -> None:
    add_order(conn, run, **order_options)
    store_observations(conn, [observation(CAPTURED, START_MS + 1, START_MS + 2)])


def test_multiple_captures_are_excluded(conn, run_a):
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    store_observations(conn, [observation(CAPTURED, START_MS + 1, START_MS + 2),
                              observation(CAPTURED, START_MS + 3, START_MS + 4, payment_id=PAY_2)])
    assert state.load_order_projection(conn, RUN_A, ORDER_1, T).exclusions == (ExclusionReason.MULTIPLE_CAPTURES,)


def test_a_capture_seen_only_in_observations_still_counts(conn, run_a):
    add_order(conn, run_a)  # attempt row for PAY_1 only
    store_observations(conn, [observation(CAPTURED, START_MS + 1, START_MS + 2),
                              observation(CAPTURED, START_MS + 3, START_MS + 4, payment_id=PAY_2)])
    projection = state.load_order_projection(conn, RUN_A, ORDER_1, T)
    assert ExclusionReason.MULTIPLE_CAPTURES in projection.exclusions
    assert {payment.payment_id for payment in projection.payments} == {PAY_1, PAY_2}


def test_an_unresolved_other_attempt_is_excluded(conn, run_a):
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    store_observations(conn, [observation(CAPTURED, START_MS + 1, START_MS + 2),
                              observation(AUTHORIZED, START_MS + 3, START_MS + 4, payment_id=PAY_2)])
    assert state.load_order_projection(conn, RUN_A, ORDER_1, T).exclusions == (ExclusionReason.UNRESOLVED_ATTEMPT,)


def test_a_failed_other_attempt_is_resolved(conn, run_a):
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    store_observations(conn, [observation(FAILED, START_MS + 1, START_MS + 2, payment_id=PAY_2),
                              observation(CAPTURED, START_MS + 3, START_MS + 4)])
    assert state.load_order_projection(conn, RUN_A, ORDER_1, T).is_recovery_eligible


def test_cancellation_counts_from_its_time(conn, run_a):
    _order_with_capture(conn, run_a, cancelled_at_ms=START_MS + 5_000)
    assert state.load_order_projection(conn, RUN_A, ORDER_1, START_MS + 4_999).exclusions == ()
    assert state.load_order_projection(conn, RUN_A, ORDER_1, T).exclusions == (ExclusionReason.CANCELLED,)


def test_refunds_are_excluded_from_the_order_flag_or_the_payment(conn, run_a):
    _order_with_capture(conn, run_a, refunded=True)
    assert state.load_order_projection(conn, RUN_A, ORDER_1, T).exclusions == (ExclusionReason.REFUNDED,)
    add_order(conn, run_a, ORDER_2, payment_ids=(PAY_2,))
    store_observations(conn, [observation(PaymentStatus.REFUNDED, START_MS + 1, START_MS + 2, payment_id=PAY_2,
                                          order_id=ORDER_2, amount_refunded_paise=AMOUNT)])
    assert state.load_order_projection(conn, RUN_A, ORDER_2, T).exclusions == (ExclusionReason.REFUNDED,)


@pytest.mark.parametrize(
    ("order_amount", "order_currency", "expected"),
    [(AMOUNT + 1, "INR", ExclusionReason.AMOUNT_MISMATCH), (AMOUNT, "USD", ExclusionReason.CURRENCY_MISMATCH)],
)
def test_amount_or_currency_unlike_the_order_is_excluded(conn, run_a, order_amount, order_currency, expected):
    _order_with_capture(conn, run_a, amount_paise=order_amount, currency=order_currency)
    assert state.load_order_projection(conn, RUN_A, ORDER_1, T).exclusions == (expected,)


def test_a_conflicting_delivery_touching_the_payment_is_excluded(conn, run_a):
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    assert state.load_order_projection(conn, RUN_A, ORDER_1, T).exclusions == ()
    deliver(conn, run_a, "evt_SYNTHETIC_0001", amount_paise=AMOUNT + 5, received_at_ms=START_MS + 3_000)
    assert state.load_order_projection(conn, RUN_A, ORDER_1, START_MS + 2_999).exclusions == ()
    projection = state.load_order_projection(conn, RUN_A, ORDER_1, T)
    assert projection.exclusions == (ExclusionReason.CONFLICTING_EVIDENCE,)
    assert len(projection.conflicting_delivery_ids) == 1


def test_a_payment_whose_attempt_names_another_order_is_conflicting(conn, run_a):
    add_order(conn, run_a)  # the merchant created PAY_1 for ORDER_1
    add_order(conn, run_a, ORDER_2, payment_ids=())
    store_observations(conn, [observation(CAPTURED, START_MS + 1, START_MS + 2, order_id=ORDER_2)])
    for order_id in (ORDER_1, ORDER_2):
        projection = state.load_order_projection(conn, RUN_A, order_id, T)
        assert ExclusionReason.CONFLICTING_EVIDENCE in projection.exclusions, order_id


@pytest.mark.parametrize("captured_first", [True, False])
@pytest.mark.parametrize("failed_id_sorts_first", [True, False])
def test_same_time_contradictory_webhooks_block_the_order(conn, run_a, captured_first, failed_id_sorts_first):
    add_order(conn, run_a)
    failed_id, captured_id = ("evt_a", "evt_b") if failed_id_sorts_first else ("evt_b", "evt_a")
    deliveries = [(captured_id, CAPTURED), (failed_id, FAILED)]
    for event_id, status in deliveries if captured_first else reversed(deliveries):
        deliver(conn, run_a, event_id, status=status, occurred_at_ms=START_MS + 1_500)
    projection = state.load_order_projection(conn, RUN_A, ORDER_1, T)
    assert projection.exclusions == (ExclusionReason.CONFLICTING_EVIDENCE,)
    assert projection.payments[0].conflict_reasons == (Reason.SAME_TIME_CONTRADICTION,)


def test_fulfilment_is_seen_as_of_its_time(conn, run_a):
    _order_with_capture(conn, run_a)
    with storage.write_transaction(conn):
        storage.insert_fulfilment(conn, storage.FulfilmentRecord(
            run_id=RUN_A, order_id=ORDER_1, payment_id=PAY_1, amount_paise=AMOUNT, currency="INR",
            fulfilled_at_ms=START_MS + 5_000, via="worker"))
    assert state.load_order_projection(conn, RUN_A, ORDER_1, START_MS + 4_999).exclusions == ()
    assert state.load_order_projection(conn, RUN_A, ORDER_1, T).exclusions == (ExclusionReason.ALREADY_FULFILLED,)


def test_capture_known_after_the_cutoff_is_invisible(conn, run_a):
    add_order(conn, run_a)
    store_observations(conn, [observation(CAPTURED, START_MS + 1, START_MS + 6_000)])
    assert ExclusionReason.NOT_CAPTURED in state.load_order_projection(conn, RUN_A, ORDER_1, START_MS + 5_999).exclusions
    assert state.load_order_projection(conn, RUN_A, ORDER_1, START_MS + 6_000).is_recovery_eligible


def test_another_runs_order_is_unknown_and_an_unknown_run_is_a_scope_error(conn, run_a, run_b):
    _order_with_capture(conn, run_a)
    foreign = state.load_order_projection(conn, run_b.run_id, ORDER_1, T)
    assert foreign.exclusions == (ExclusionReason.UNKNOWN_ORDER,)
    assert foreign.payments == ()
    with pytest.raises(ScopeError):
        state.load_order_projection(conn, "run_missing", ORDER_1, T)
