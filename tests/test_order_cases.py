"""The per-order recovery queue (order_cases.py): one state per order, every blocker, planner agreement, as-of reads.

Constructed cases are written with the helpers test_state.py and test_plans.py use. The planner comparison, the
replay comparison and the per-family state counts also run on fresh worlds of every manifest parameter set and
every demo scenario, seeded from 14101 (seeds 14101-14200 are reserved for phase 11).
"""
from __future__ import annotations

import ast
import dataclasses
import itertools
import json
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path

import pytest
from conftest import (
    AMOUNT,
    ORDER_1,
    PAY_1,
    PAY_2,
    RUN_A,
    START_MS,
    FakeHandler,
    SeededRun,
    add_order,
    deliver,
    fulfil_reply,
    observation,
    store_observations,
)
from test_plans import NOW as BATCH_NOW
from test_plans import STALE_AFTER_MS as BATCH_STALE_AFTER_MS
from test_plans import UNKNOWN_ORDER, build_batch, build_plan, validate, worker_regression_output
from test_plans import order_id as batch_order
from test_tools import TableReads

from recovery_workbench import diagnosis_store, fixes, order_cases, plans, policy, refresh, settings, storage, worker
from recovery_workbench.contracts import ContractError, PaymentStatus, ScopeError
from recovery_workbench.order_cases import AmountStatus, CaseState, OrderCase, OrderQueue
from recovery_workbench.simulator import driver, families, merchant, runtime, scenarios
from recovery_workbench.simulator.scenarios import ScenarioSpec
from recovery_workbench.simulator.world import generate_world

MINUTE = 60_000
HOUR = 60 * MINUTE
T = START_MS + 2 * HOUR  # when constructed cases are classified
STALE_AFTER = HOUR
FRESH = T - 5 * MINUTE  # evidence this recent is fresh at T
OLD = START_MS + 1_000  # evidence this old is stale at T
SHORT = AMOUNT - 100  # a capture short of the order's amount

CAPTURED, FAILED, AUTHORIZED, CREATED, REFUNDED = (
    PaymentStatus.CAPTURED, PaymentStatus.FAILED, PaymentStatus.AUTHORIZED, PaymentStatus.CREATED, PaymentStatus.REFUNDED,
)
# One letter per state keeps the precedence tables readable; the order is the precedence order.
M, P, B, U, A, C, X, K, N, I, F, E = (  # noqa: E741
    CaseState.PAYMENT_ORDER_MISMATCH, CaseState.MULTIPLE_CAPTURES, CaseState.BUSINESS_STATE_CONFLICT,
    CaseState.PAYMENT_STATE_UNRESOLVED, CaseState.CAPTURE_NOT_CONFIRMED, CaseState.COMPLETE,
    CaseState.CAPTURED_PROCESSING_CLOSED, CaseState.NOTIFICATION_ACKNOWLEDGED_NOT_STORED,
    CaseState.NOTIFICATION_NOT_ACCEPTED, CaseState.CAPTURED_PROCESSING_INCOMPLETE, CaseState.PAYMENT_FAILED,
    CaseState.INSUFFICIENT_EVIDENCE,
)

HEALTHY = FakeHandler()
FAILING = FakeHandler(failure=KeyError("id"))
IGNORING = FakeHandler(reply=lambda entity: {"action": "ignore"})
WRONG_AMOUNT = FakeHandler(reply=lambda entity: {**fulfil_reply(entity), "amount_paise": int(entity["amount"]) + 1})


def classify(conn: sqlite3.Connection, at: int = T, *, run_id: str = RUN_A, stale_after_ms: int = STALE_AFTER) -> OrderQueue:
    return order_cases.classify_orders(conn, run_id=run_id, as_of_ms=at, stale_after_ms=stale_after_ms)


def case_of(queue: OrderQueue, order: str) -> OrderCase:
    [case] = [case for case in queue.cases if case.order_id == order]
    return case


def facts_of(case: OrderCase) -> order_cases.CaseFacts:
    """The state's inputs, re-derived from the record alone: a case carries what explains its state."""
    return order_cases.CaseFacts(
        blockers=frozenset(case.blockers),
        has_capture=any(payment.status in (CAPTURED, REFUNDED) for payment in case.payments),
        fulfilled=case.fulfilment is not None,
        payment_statuses=tuple(payment.status for payment in case.payments),
        stale_open_payment=bool(case.freshness.stale_open_payment_ids),
        unsettled_capture_event=bool(case.capture_processing.unsettled_inbox_ids),
    )


RULES = {rule.state: rule for rule in order_cases.STATE_PRECEDENCE}
PRECEDENCE = [rule.state for rule in order_cases.STATE_PRECEDENCE]


# --------------------------------------------------------------------------- constructed single-order stories


class Story:
    """One order's evidence, written with the helpers test_state.py and test_plans.py use."""

    def __init__(
        self, conn: sqlite3.Connection, run: SeededRun, *, payments: tuple[str, ...] = (PAY_1,), known: bool = True,
        amount: int = AMOUNT, currency: str = "INR", created_at: int = START_MS, cancelled_at: int | None = None,
        refunded: bool = False,
    ) -> None:
        self.conn, self.run = conn, run
        self._numbers = itertools.count(1)
        self.last_event: str | None = None
        if known:
            add_order(conn, run, ORDER_1, payment_ids=payments, amount_paise=amount, currency=currency,
                      cancelled_at_ms=cancelled_at, refunded=refunded, created_at_ms=created_at)

    def webhook(
        self, payment_id: str = PAY_1, status: PaymentStatus = CAPTURED, *, at: int = FRESH, amount: int = AMOUNT,
        currency: str = "INR", event_id: str | None = None, received_at: int | None = None,
    ) -> None:
        event = event_id or f"evt_case_{next(self._numbers):03d}"
        response = deliver(self.conn, self.run, event, received_at_ms=received_at, status=status, payment_id=payment_id,
                           order_id=ORDER_1, occurred_at_ms=at, amount_paise=amount, currency=currency)
        assert response.outcome.value in ("accepted", "conflicting_id"), response
        self.last_event = event

    def redeliver(self, status: PaymentStatus = CAPTURED, *, amount: int) -> None:
        """The last event's ID again with a different body, which ingestion holds as a conflicting delivery."""
        assert self.last_event is not None
        self.webhook(PAY_1, status, amount=amount, event_id=self.last_event)

    def snapshot(
        self, payment_id: str = PAY_1, status: PaymentStatus = CAPTURED, *, at: int = FRESH, amount: int = AMOUNT,
        refunded: int = 0,
    ) -> None:
        store_observations(self.conn, [observation(
            status, at, at, observation_id=f"obs_case_{next(self._numbers):03d}", payment_id=payment_id,
            amount_paise=amount, amount_refunded_paise=refunded,
        )])

    def work(self, handler: FakeHandler, *, at: int = FRESH + MINUTE) -> None:
        assert worker.run_worker(self.conn, run_id=self.run.run_id, handler=handler, now_ms=at)

    def fulfil(self, payment_id: str = PAY_1, *, at: int = FRESH + MINUTE) -> None:
        with storage.write_transaction(self.conn):
            storage.insert_fulfilment(self.conn, storage.FulfilmentRecord(
                run_id=self.run.run_id, order_id=ORDER_1, payment_id=payment_id, amount_paise=AMOUNT, currency="INR",
                fulfilled_at_ms=at, via="worker",
            ))

    def acknowledge(self, payment_id: str = PAY_1, *, at: int = FRESH) -> None:
        """A refresh at T reads the provider's delivery log: it answered 2xx to the payment's payment.captured event,
        which no inbox row holds. Moves the clock to T, so it comes last in a scene."""
        event = f"evt_case_ack_{next(self._numbers):03d}"
        answered = refresh.ProviderDeliveryAttempt(
            attempt_id=f"{event}:1", integration_id=self.run.integration_id, event_id=event,
            event_type=policy.CAPTURE_EVENT_TYPE, payment_id=payment_id, attempted_at_ms=at, http_status=200,
            triggered_by="automatic",
        )
        refresh.record_delivery_log(self.conn, run_id=self.run.run_id, payment_ids=[payment_id],
                                    delivery_log=AnsweringLog([answered]), now_ms=T)

    def case(self, at: int = T) -> OrderCase:
        return case_of(classify(self.conn, at), ORDER_1)


class AnsweringLog:
    """A provider delivery log that answers with prepared attempts, never one made after the read."""

    def __init__(self, attempts: list[refresh.ProviderDeliveryAttempt]) -> None:
        self.attempts = attempts

    def deliveries_as_of(self, payment_ids: Sequence[str], as_of_ms: int) -> dict[str, list[refresh.ProviderDeliveryAttempt]]:
        return {payment_id: [item for item in self.attempts
                             if item.payment_id == payment_id and item.attempted_at_ms <= as_of_ms]
                for payment_id in payment_ids}


Step = Callable[[Story], None]


def hook(payment_id: str = PAY_1, status: PaymentStatus = CAPTURED, **options: object) -> Step:
    return lambda story: story.webhook(payment_id, status, **options)  # type: ignore[arg-type]


def snap(payment_id: str = PAY_1, status: PaymentStatus = CAPTURED, **options: object) -> Step:
    return lambda story: story.snapshot(payment_id, status, **options)  # type: ignore[arg-type]


def work(handler: FakeHandler, **options: object) -> Step:
    return lambda story: story.work(handler, **options)  # type: ignore[arg-type]


def fulfil(**options: object) -> Step:
    return lambda story: story.fulfil(**options)  # type: ignore[arg-type]


def redeliver(status: PaymentStatus = CAPTURED, *, amount: int) -> Step:
    return lambda story: story.redeliver(status, amount=amount)


def ack(payment_id: str = PAY_1) -> Step:
    return lambda story: story.acknowledge(payment_id)


@dataclasses.dataclass(frozen=True)
class Scene:
    order: Mapping[str, object] = dataclasses.field(default_factory=dict)
    steps: tuple[Step, ...] = ()

    def build(self, conn: sqlite3.Connection, run: SeededRun) -> OrderCase:
        story = Story(conn, run, **self.order)  # type: ignore[arg-type]
        for step in self.steps:
            step(story)
        return story.case()


TWO = {"payments": (PAY_1, PAY_2)}
CANCELLED = {"cancelled_at": FRESH}
UNKNOWN = {"known": False}

# name -> (state, blockers, scene): every state, and every way into it.
WITNESSES: dict[str, tuple[CaseState, set[str], Scene]] = {
    "fulfilled_by_a_healthy_worker": (C, {"already_fulfilled"}, Scene(steps=(hook(), work(HEALTHY)))),
    "captured_and_failing_in_the_worker": (I, set(), Scene(steps=(hook(), work(FAILING)))),
    "captured_and_not_yet_attempted": (I, set(), Scene(steps=(hook(),))),
    "capture_seen_only_by_a_snapshot": (N, {"capture_event_not_received"}, Scene(steps=(snap(),))),
    "capture_the_receiver_acknowledged_and_never_stored": (
        K, {"capture_event_not_received", "capture_event_acknowledged_not_stored"}, Scene(steps=(snap(), ack()))),
    "capture_event_ignored_by_the_handler": (
        X, {"capture_event_settled_without_fulfilment"}, Scene(steps=(hook(), work(IGNORING)))),
    "capture_event_held_for_review": (
        X, {"capture_event_settled_without_fulfilment"}, Scene(steps=(hook(), work(WRONG_AMOUNT)))),
    "stale_capture": (U, {"stale_snapshot"}, Scene(steps=(hook(at=OLD), work(FAILING, at=OLD + MINUTE)))),
    "same_time_contradiction": (U, {"conflicting_evidence"}, Scene(steps=(hook(status=FAILED), hook()))),
    "conflicting_redelivery": (U, {"conflicting_evidence"}, Scene(steps=(hook(), redeliver(amount=AMOUNT + 5)))),
    "unresolved_extra_attempt": (U, {"unresolved_attempt"}, Scene(TWO, (hook(), snap(PAY_2, AUTHORIZED)))),
    "captured_event_for_another_payment": (
        U, {"target_not_the_capture"}, Scene(steps=(snap(), hook(PAY_2, at=T + MINUTE, received_at=FRESH)))),
    "stale_created_payment": (
        U, {"unresolved_attempt", "not_captured", "stale_snapshot"}, Scene(steps=(snap(status=CREATED, at=OLD),))),
    "stale_authorised_payment": (
        U, {"unresolved_attempt", "not_captured", "stale_snapshot"}, Scene(steps=(snap(status=AUTHORIZED, at=OLD),))),
    "unobserved_old_attempt": (U, {"unresolved_attempt", "not_captured", "stale_snapshot"}, Scene()),
    "authorised_not_captured": (A, {"unresolved_attempt", "not_captured"}, Scene(steps=(hook(status=AUTHORIZED),))),
    "failed": (F, {"not_captured"}, Scene(steps=(hook(status=FAILED),))),
    "failed_long_ago": (F, {"not_captured", "stale_snapshot"}, Scene(steps=(hook(status=FAILED, at=OLD),))),
    "no_payment_attempt": (E, {"not_captured"}, Scene({"payments": ()})),
    "young_unobserved_attempt": (
        E, {"unresolved_attempt", "not_captured", "stale_snapshot"}, Scene({"created_at": T - MINUTE})),
    "created_and_fresh": (E, {"unresolved_attempt", "not_captured"}, Scene(steps=(snap(status=CREATED),))),
    "two_captures": (P, {"multiple_captures"}, Scene(TWO, (hook(), hook(PAY_2)))),
    "captured_then_cancelled": (B, {"cancelled"}, Scene(CANCELLED, (hook(),))),
    "captured_order_flagged_refunded": (B, {"refunded"}, Scene({"refunded": True}, (hook(),))),
    "refund_observed": (
        B, {"refunded", "capture_event_not_received"}, Scene(steps=(snap(status=REFUNDED, refunded=AMOUNT),))),
    "fulfilled_without_capture": (
        B, {"already_fulfilled", "unresolved_attempt", "not_captured"}, Scene(steps=(hook(status=AUTHORIZED), fulfil()))),
    "capture_short_of_the_order": (M, {"amount_mismatch"}, Scene(steps=(hook(amount=SHORT),))),
    "capture_in_another_currency": (M, {"currency_mismatch"}, Scene(steps=(hook(currency="USD"),))),
    "capture_for_an_unknown_order": (M, {"unknown_order"}, Scene(UNKNOWN, (hook(),))),
}


@pytest.mark.parametrize("name", sorted(WITNESSES))
def test_every_state_and_every_way_into_it(conn: sqlite3.Connection, run_a: SeededRun, name: str) -> None:
    expected_state, expected_blockers, scene = WITNESSES[name]
    case = scene.build(conn, run_a)
    assert (case.state, set(case.blockers)) == (expected_state, expected_blockers)
    assert case.next_step is order_cases.NEXT_STEP[expected_state]
    assert list(case.blockers) == sorted(case.blockers, key=order_cases.BLOCKER_ORDER.index)
    assert order_cases.choose_state(facts_of(case)) is case.state


def test_the_witnesses_reach_every_state() -> None:
    assert {state for state, _, _ in WITNESSES.values()} == set(CaseState)


@pytest.mark.parametrize(("name", "expected"), [
    ("captured_and_failing_in_the_worker", AmountStatus.ASSERTED),
    ("same_time_contradiction", AmountStatus.ASSERTED),  # the observations disagree on status, not on money
    ("capture_short_of_the_order", AmountStatus.CONFLICTING),
    ("capture_in_another_currency", AmountStatus.CONFLICTING),
    ("conflicting_redelivery", AmountStatus.CONFLICTING),  # the held body claims another amount
    ("capture_for_an_unknown_order", AmountStatus.UNKNOWN),
])
def test_amounts_say_whether_they_may_enter_a_total(
    conn: sqlite3.Connection, run_a: SeededRun, name: str, expected: AmountStatus
) -> None:
    case = WITNESSES[name][2].build(conn, run_a)
    assert case.amount_status is expected
    assert (case.amount_paise is None) == (expected is AmountStatus.UNKNOWN)


def test_a_consistent_order_in_another_currency_is_counted_not_summed(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    story = Story(conn, run_a, currency="USD")
    story.webhook(currency="USD")
    case = story.case()
    assert (case.state, case.amount_status, case.blockers) == (I, AmountStatus.OTHER_CURRENCY, ())
    totals = classify(conn).totals
    assert (totals.currency, totals.captured_unprocessed_orders, totals.captured_unprocessed_value_paise) == ("INR", 1, 0)
    assert next(total for total in totals.by_state if total.state is I).unasserted_orders == 1


# --------------------------------------------------------------------------- precedence


def test_the_documented_precedence_order() -> None:
    assert PRECEDENCE == [M, P, B, U, A, C, X, K, N, I, F, E] == list(CaseState)
    assert order_cases.UNRESOLVED_STATES == set(CaseState) - {C, F, E}
    assert {order_cases.NEXT_STEP[state] for state in (M, P, B, X, K)} == {order_cases.NextStep.MANUAL_REVIEW}


# (earlier, later) -> a scene where both conditions hold and no earlier one does.
PAIRS: dict[tuple[CaseState, CaseState], Scene] = {
    (M, P): Scene(TWO, (hook(amount=SHORT), hook(PAY_2))),
    (M, B): Scene(CANCELLED, (hook(amount=SHORT),)),
    (M, U): Scene(steps=(hook(amount=SHORT, at=OLD),)),
    (M, A): Scene(UNKNOWN, (hook(status=AUTHORIZED),)),
    (M, C): Scene(steps=(hook(amount=SHORT), fulfil())),
    (M, X): Scene(steps=(hook(amount=SHORT), work(HEALTHY))),  # the worker holds it for the mismatch
    (M, K): Scene(steps=(snap(amount=SHORT), ack())),
    (M, N): Scene(steps=(snap(amount=SHORT),)),
    (M, I): Scene(steps=(hook(amount=SHORT), work(FAILING))),
    (M, F): Scene(UNKNOWN, (hook(status=FAILED),)),
    (M, E): Scene(UNKNOWN, (snap(status=CREATED),)),
    (P, B): Scene({**TWO, **CANCELLED}, (hook(), hook(PAY_2))),
    (P, U): Scene(TWO, (hook(at=OLD), hook(PAY_2))),
    (P, C): Scene(TWO, (hook(), hook(PAY_2), fulfil())),
    (P, X): Scene(TWO, (hook(), hook(PAY_2), work(HEALTHY))),
    (P, K): Scene(TWO, (snap(), snap(PAY_2), ack())),
    (P, N): Scene(TWO, (snap(), snap(PAY_2))),
    (P, I): Scene(TWO, (hook(), hook(PAY_2), work(FAILING))),
    (B, U): Scene(CANCELLED, (hook(at=OLD),)),
    (B, A): Scene(steps=(hook(status=AUTHORIZED), fulfil())),
    (B, C): Scene(CANCELLED, (hook(), fulfil())),
    (B, X): Scene(CANCELLED, (hook(), work(HEALTHY))),  # the worker holds it for the cancellation
    (B, K): Scene(CANCELLED, (snap(), ack())),
    (B, N): Scene(CANCELLED, (snap(),)),
    (B, I): Scene(CANCELLED, (hook(), work(FAILING))),
    (B, F): Scene(steps=(hook(status=FAILED), fulfil())),
    (B, E): Scene({"payments": ()}, (fulfil(),)),
    (U, A): Scene(steps=(hook(status=AUTHORIZED), redeliver(AUTHORIZED, amount=AMOUNT + 5))),
    (U, C): Scene(TWO, (hook(), work(HEALTHY), snap(PAY_2, AUTHORIZED))),
    (U, X): Scene(steps=(hook(status=FAILED), hook(), work(HEALTHY))),  # held for the contradiction, as in D4
    (U, K): Scene(steps=(snap(at=OLD), ack())),
    (U, N): Scene(steps=(snap(at=OLD),)),
    (U, I): Scene(steps=(hook(at=OLD), work(FAILING, at=OLD + MINUTE))),
    (U, F): Scene(steps=(hook(status=FAILED), redeliver(FAILED, amount=AMOUNT + 5))),
    (U, E): Scene(TWO, (snap(status=CREATED), snap(PAY_2, CREATED, at=OLD))),
    (A, E): Scene(steps=(hook(status=AUTHORIZED),)),
    (C, K): Scene(steps=(snap(), fulfil(), ack())),
    (C, N): Scene(steps=(snap(), fulfil())),
    (C, I): Scene(steps=(hook(), work(HEALTHY), hook(at=FRESH + 2 * MINUTE))),  # a resent capture, not yet processed
    (K, N): Scene(steps=(snap(), ack())),  # an acknowledged capture is also one whose notification never got in
    (F, E): Scene(steps=(hook(status=FAILED),)),
}

# (earlier, later) pairs whose conditions never hold together without an even earlier state, and why.
NEEDS_CAPTURE = "the earlier state needs capture evidence and the later one needs none, or the reverse"
FULFILLED_WITHOUT_CAPTURE = "a fulfilled order with no capture evidence is business_state_conflict, which comes first"
EXCLUSIVE: dict[tuple[CaseState, CaseState], str] = {
    (P, A): NEEDS_CAPTURE, (P, F): NEEDS_CAPTURE, (P, E): NEEDS_CAPTURE,
    (A, X): NEEDS_CAPTURE, (A, K): NEEDS_CAPTURE, (A, N): NEEDS_CAPTURE, (A, I): NEEDS_CAPTURE,
    (X, F): NEEDS_CAPTURE, (X, E): NEEDS_CAPTURE, (K, F): NEEDS_CAPTURE, (K, E): NEEDS_CAPTURE,
    (N, F): NEEDS_CAPTURE, (N, E): NEEDS_CAPTURE, (I, F): NEEDS_CAPTURE, (I, E): NEEDS_CAPTURE,
    (A, F): "one needs an authorised payment, the other every payment failed",
    (A, C): FULFILLED_WITHOUT_CAPTURE, (C, F): FULFILLED_WITHOUT_CAPTURE, (C, E): FULFILLED_WITHOUT_CAPTURE,
    (C, X): "a closed captured event is a blocker of unfulfilled orders only",
    (X, K): "one needs settled captured events, the other none at all",
    (X, N): "one needs settled captured events, the other none at all",
    (X, I): "one needs every captured event settled, the other one unsettled",
    (K, I): "one needs no captured event, the other an unsettled one",
    (N, I): "one needs no captured event, the other an unsettled one",
}


def test_every_pair_of_states_is_tested_or_shown_exclusive() -> None:
    ordered = {(first, second) for index, first in enumerate(PRECEDENCE) for second in PRECEDENCE[index + 1:]}
    assert set(PAIRS) | set(EXCLUSIVE) == ordered
    assert not set(PAIRS) & set(EXCLUSIVE)
    assert (len(PAIRS), len(EXCLUSIVE)) == (41, 25)


@pytest.mark.parametrize(("earlier", "later"), list(PAIRS), ids=[f"{a.value}-over-{b.value}" for a, b in PAIRS])
def test_the_earlier_state_wins_when_both_conditions_hold(
    conn: sqlite3.Connection, run_a: SeededRun, earlier: CaseState, later: CaseState
) -> None:
    case = PAIRS[(earlier, later)].build(conn, run_a)
    facts = facts_of(case)
    assert RULES[earlier].applies(facts)
    assert RULES[later].applies(facts)
    assert [state for state in PRECEDENCE[:PRECEDENCE.index(earlier)] if RULES[state].applies(facts)] == []
    assert case.state is earlier


def test_a_captured_order_with_a_blocker_no_rule_names_is_refused() -> None:
    facts = order_cases.CaseFacts(blockers=frozenset({"a_future_exclusion"}), has_capture=True, fulfilled=False,
                                  payment_statuses=(CAPTURED,), stale_open_payment=False, unsettled_capture_event=True)
    with pytest.raises(ContractError, match="add a precedence rule"):
        order_cases.choose_state(facts)


# --------------------------------------------------------------------------- agreement with the planner


DRY_RUN_REASONS = frozenset({
    policy.PlanExclusion.DRY_RUN_RAISED.value, policy.PlanExclusion.DRY_RUN_NO_FULFILMENT.value,
    policy.PlanExclusion.DRY_RUN_MISMATCH.value,
})


def planner_disagreements(queue: OrderQueue, preview: plans.PlanPreview) -> dict[str, str]:
    """Every target is captured_processing_incomplete with no blocker; every exclusion is among the blockers.

    The one exclusion the queue cannot see is a failed dry run of the corrected handler: it is a property of the
    fix, not of the evidence. Such an order must otherwise be eligible, and it is counted, not hidden.
    """
    cases = {case.order_id: case for case in queue.cases}
    found: dict[str, str] = {}
    for target in preview.targets:
        case = cases.get(target.id)
        if case is None or case.state is not I or case.blockers:
            found[target.id] = f"planned, but the queue says {None if case is None else (case.state.value, case.blockers)}"
    dry_run_excluded = set()
    for item in preview.excluded:
        case = cases.get(item.order_id)
        if case is None:
            found[item.order_id] = "excluded by the planner, missing from the queue"
            continue
        if DRY_RUN_REASONS & set(item.reasons):
            dry_run_excluded.add(item.order_id)
            if case.state is not I or case.blockers:
                found[item.order_id] = f"dry run {item.reasons}, and the queue says {case.state.value} {case.blockers}"
            continue
        missing = set(item.reasons) - set(case.blockers)
        if missing:
            found[item.order_id] = f"excluded for {sorted(missing)}, which the queue's blockers {case.blockers} lack"
    eligible = {order_id for order_id, case in cases.items() if case.state is I}
    for order_id in sorted(eligible ^ ({target.id for target in preview.targets} | dry_run_excluded)):
        found.setdefault(order_id, "eligible in only one of the queue and the plan")
    return found


def replay_disagreements(queue: OrderQueue, replay: plans.ReplayInstructions) -> dict[str, str]:
    """Each notification_not_accepted order's capture is a replay target, and each replay target's order is
    notification_not_accepted; each held capture's order is held by the queue too, for reasons among its blockers."""
    cases = {case.order_id: case for case in queue.cases}
    targets = {target.id: target for target in replay.targets}
    found: dict[str, str] = {}
    for case in cases.values():
        if case.state is not N:
            continue
        captured = [payment.payment_id for payment in case.payments if payment.status is CAPTURED]
        if len(captured) != 1 or captured[0] not in targets:
            found[case.order_id] = f"notification not accepted, but its capture {captured} is not a replay target"
    for target in replay.targets:
        case = cases.get(target.order_id)
        if case is None or case.state is not N:
            found[target.order_id] = f"replay target {target.id}, but the queue says {case}"
    for held in replay.held_for_reconciliation:
        case = cases.get(held.order_id)
        if case is None or case.state is N or not set(held.reasons) <= set(case.blockers):
            found[held.payment_id] = f"held for {held.reasons}, but the queue says {case}"
    return found


def test_the_queue_agrees_with_the_planner_on_the_constructed_batch(conn: sqlite3.Connection) -> None:
    batch = build_batch(conn)
    preview = build_plan(conn, batch, validate(conn, batch))
    queue = classify(conn, BATCH_NOW, run_id=batch.run.run_id, stale_after_ms=BATCH_STALE_AFTER_MS)
    assert planner_disagreements(queue, preview) == {}
    states = {case.order_id: case.state for case in queue.cases}
    assert {order for order, state in states.items() if state is I} == set(batch.eligible)
    others = {41: C, 42: C, 43: C, 44: C, 45: B, 46: U, 47: P, 48: M, 49: U, 51: U, 52: B, 53: M, 54: P, 55: A, 56: U}
    assert {order: state for order, state in states.items() if order not in batch.eligible} == {
        **{batch_order(number): state for number, state in others.items()}, UNKNOWN_ORDER: M}
    late = case_of(queue, batch_order(50))  # failed first, captured later: a target since policy-v3
    assert (late.state, late.blockers, [payment.late_authorization for payment in late.payments]) == (I, (), [True])


def test_the_batch_totals_keep_unknown_and_conflicting_amounts_out(conn: sqlite3.Connection) -> None:
    batch = build_batch(conn)
    preview = build_plan(conn, batch, validate(conn, batch))
    totals = classify(conn, BATCH_NOW, run_id=batch.run.run_id, stale_after_ms=BATCH_STALE_AFTER_MS).totals
    assert (totals.captured_unprocessed_orders, totals.captured_unprocessed_value_paise) == (41, preview.target_value_paise)
    by_state = {total.state: (total.orders, total.asserted_value_paise, total.unasserted_orders) for total in totals.by_state}
    assert by_state == {
        M: (3, 0, 3),  # 48 (amount), 53 (currency) and the unknown order: counted, never summed
        P: (2, 47_000 + 54_000, 0),
        B: (2, 45_000 + 52_000, 0),
        U: (4, 46_000 + 51_000 + 56_000, 1),  # 49's held redelivery claims another amount
        A: (1, 55_000, 0),
        C: (4, sum(100_000 + number for number in (41, 42, 43, 44)), 0),
        X: (0, 0, 0), K: (0, 0, 0), N: (0, 0, 0),
        I: (41, sum(batch.eligible.values()), 0),
        F: (0, 0, 0), E: (0, 0, 0),
    }
    assert totals.unresolved_orders == 41 + 3 + 2 + 2 + 4 + 1
    assert totals.unresolved_value_paise == sum(batch.eligible.values()) + 406_000
    assert totals.unresolved_unasserted_orders == 4


def test_every_order_appears_exactly_once_with_its_evidence(conn: sqlite3.Connection) -> None:
    batch = build_batch(conn)
    queue = classify(conn, BATCH_NOW, run_id=batch.run.run_id, stale_after_ms=BATCH_STALE_AFTER_MS)
    known = {order.order_id for order in storage.list_orders(conn, run_id=batch.run.run_id)}
    assert [case.order_id for case in queue.cases] == sorted(known | {UNKNOWN_ORDER})
    assert (queue.coverage.orders, queue.coverage.unknown_orders) == (len(known), 1)
    unknown = case_of(queue, UNKNOWN_ORDER)
    assert (unknown.order_known, unknown.amount_paise, unknown.amount_status) == (False, None, AmountStatus.UNKNOWN)
    resent = case_of(queue, batch_order(4)).capture_processing  # two captured events for one payment
    assert len(resent.captured_inbox_ids) == len(resent.unsettled_inbox_ids) == 2
    assert resent.target_inbox_id == resent.captured_inbox_ids[0]  # the earliest received, as the planner picks
    assert resent.worker_failures == 6
    evidence = case_of(queue, batch_order(4)).evidence
    assert set(evidence.inbox_ids) == set(resent.captured_inbox_ids)
    assert (len(evidence.worker_attempt_ids), len(evidence.delivery_attempt_ids)) == (6, 2)


# --------------------------------------------------------------------------- fresh worlds of every family

RESERVED_SEEDS = range(14101, 14201)  # reserved for phase 11 (2026-11-12); no other seed is used here
SWEEP_FIRST_SEED = 14101
TRUNCATION_FIRST_SEED = 14139  # after the 38 sweep worlds
READS_SEED = 14143  # after the 4 truncation worlds
STALE_AFTER_MS = settings.load_settings().stale_after_seconds * 1_000
PLAN_TTL_MS = 15 * MINUTE
SWEEP_OPERATOR = "op_case_sweep"


@dataclasses.dataclass(frozen=True)
class Source:
    family: str
    label: str
    spec: ScenarioSpec

    @property
    def group(self) -> str:
        """Counts are kept per manifest family, and per demo scenario, which adds hand-placed edge cases."""
        return self.label if self.label.startswith("demo ") else self.family


def sweep_sources() -> list[Source]:
    """One world per distinct manifest parameter set, as a development run, then each demo scenario."""
    distinct: dict[str, families.FamilyRun] = {}
    for run in families.load_manifest().runs:
        distinct.setdefault(f"{run.family} {run.variant} {json.dumps(run.params, sort_keys=True)}", run)
    sources = [Source(run.family, label, families.spec_for(run.model_copy(update={"split": "dev"})))
               for label, run in distinct.items()]
    sources.extend(Source(spec.family, f"demo {name}", spec) for name, spec in sorted(scenarios.DEMO_SCENARIOS.items()))
    return sources


def ensure_incident(conn: sqlite3.Connection, run_id: str, now: int) -> str:
    """A diagnosis needs an incident; a world that opened none gets a minimal one."""
    known = storage.list_incidents(conn, run_id=run_id)
    if known:
        return known[0].incident_id
    with storage.write_transaction(conn):
        storage.insert_incident(conn, storage.IncidentRecord(
            run_id=run_id, incident_id="inc_case_sweep", trigger="order_queue_check", cohort={}, window_start_ms=now,
            window_end_ms=now, evidence_cutoff_ms=now, state="open", measured_impact={},
        ))
    return "inc_case_sweep"


def passing_validation(
    conn: sqlite3.Connection, run_id: str, deployed: merchant.DeployedMerchant, diagnosis_id: str, now: int
) -> fixes.FixValidation:
    """The first allowlisted fix whose fixture tests all pass, chosen by fixture results alone."""
    for fix_id in merchant.CORRECTED_VARIANT_IDS:
        validation = fixes.validate_fix(conn, run_id=run_id, deployed=deployed, fix_id=fix_id,
                                        diagnosis_id=diagnosis_id, operator_id=SWEEP_OPERATOR, now_ms=now)
        if validation.passed:
            return validation
    raise AssertionError(f"no allowlisted fix passes its fixture tests on run {run_id}")


@dataclasses.dataclass(frozen=True)
class Swept:
    queue: OrderQueue
    preview: plans.PlanPreview
    replay: plans.ReplayInstructions
    problems: dict[str, object]


def sweep_world(conn: sqlite3.Connection, source: Source, seed: int) -> Swept:
    """Seed a world, classify it at its cutoff, then plan and prepare replay there and compare."""
    world = generate_world(source.spec, seed)
    summary = driver.seed_world(conn, world, oracle_dir=None)
    now = storage.require_run(conn, world.run_id).clock_ms
    assert now == summary.cutoff_ms
    queue = order_cases.classify_orders(conn, run_id=world.run_id, as_of_ms=now, stale_after_ms=STALE_AFTER_MS)
    incident_id = ensure_incident(conn, world.run_id, now)
    diagnosis = diagnosis_store.record_rules_diagnosis(
        conn, run_id=world.run_id, incident_id=incident_id, packet_hash="0" * 64, output=worker_regression_output(),
        rules_version="cases-sweep",
    )
    deployed = runtime.active_merchant(conn, run_id=world.run_id, at_ms=now)
    validation = passing_validation(conn, world.run_id, deployed, diagnosis.diagnosis_id, now)
    preview = plans.build_reprocess_plan(conn, run_id=world.run_id, fix_validation_id=validation.validation_id,
                                         deployed=deployed, now_ms=now, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS)
    replay = plans.prepare_provider_replay(conn, run_id=world.run_id, incident_id=incident_id,
                                           diagnosis_id=diagnosis.diagnosis_id, now_ms=now, ttl_ms=PLAN_TTL_MS,
                                           stale_after_ms=STALE_AFTER_MS)
    orders = sorted(order.order_id for order in storage.list_orders(conn, run_id=world.run_id) if order.created_at_ms <= now)
    problems: dict[str, object] = {
        "planner": planner_disagreements(queue, preview),
        "replay": replay_disagreements(queue, replay),
        "not_total": [case.order_id for case in queue.cases] != orders or queue.coverage.unknown_orders != 0,
        "state_not_rederived": [case.order_id for case in queue.cases
                                if order_cases.choose_state(facts_of(case)) is not case.state],
        "changed_by_planning": order_cases.classify_orders(
            conn, run_id=world.run_id, as_of_ms=now, stale_after_ms=STALE_AFTER_MS) != queue,
    }
    return Swept(queue, preview, replay, {key: value for key, value in problems.items() if value})


# Measured on the first run and pinned, so the report's numbers can be re-checked: worlds per group (a manifest family,
# or a demo scenario, which adds hand-placed edge cases); the states over each group's worlds; and per group the
# planner's targets, its exclusions, its dry-run exclusions and the replay targets.
SWEEP_SOURCES_BY_FAMILY: dict[str, int] = {
    "D1": 8,
    "D2": 6,
    "D3": 7,
    "D4": 3,
    "D5": 3,
    "D6": 8,
    "demo late_authorization": 1,
    "demo method_degradation": 1,
    "demo worker_backlog": 1,
}
FAMILY_STATE_COUNTS: dict[str, dict[str, int]] = {
    "D1": {"captured_processing_incomplete": 728, "complete": 411, "payment_failed": 72,
           "payment_state_unresolved": 12},
    "D2": {"capture_not_confirmed": 3, "complete": 7129, "payment_failed": 668},
    "D3": {"complete": 495, "insufficient_evidence": 5, "notification_not_accepted": 502, "payment_failed": 43,
           "payment_state_unresolved": 5},
    "D4": {"capture_not_confirmed": 2, "complete": 901, "insufficient_evidence": 67, "payment_failed": 46,
           "payment_state_unresolved": 64},
    "D5": {"capture_not_confirmed": 6, "complete": 705, "insufficient_evidence": 75, "payment_failed": 147,
           "payment_state_unresolved": 3},
    "D6": {"capture_not_confirmed": 3, "complete": 7323, "payment_failed": 974},
    "demo late_authorization": {"complete": 306, "insufficient_evidence": 32, "payment_failed": 22},
    "demo method_degradation": {"capture_not_confirmed": 3, "complete": 1088, "payment_failed": 109},
    "demo worker_backlog": {"business_state_conflict": 5, "captured_processing_incomplete": 81, "complete": 62,
                            "insufficient_evidence": 3, "multiple_captures": 2, "payment_failed": 6,
                            "payment_order_mismatch": 2, "payment_state_unresolved": 7},
}
FAMILY_PLANNER_COUNTS: dict[str, dict[str, int]] = {
    "D1": {"dry_run_excluded": 0, "excluded": 12, "replay_targets": 0, "targets": 728, "worlds": 8},
    "D2": {"dry_run_excluded": 0, "excluded": 0, "replay_targets": 0, "targets": 0, "worlds": 6},
    "D3": {"dry_run_excluded": 0, "excluded": 0, "replay_targets": 502, "targets": 0, "worlds": 7},
    "D4": {"dry_run_excluded": 0, "excluded": 0, "replay_targets": 0, "targets": 0, "worlds": 3},
    "D5": {"dry_run_excluded": 0, "excluded": 0, "replay_targets": 0, "targets": 0, "worlds": 3},
    "D6": {"dry_run_excluded": 0, "excluded": 0, "replay_targets": 0, "targets": 0, "worlds": 8},
    "demo late_authorization":
        {"dry_run_excluded": 0, "excluded": 0, "replay_targets": 0, "targets": 0, "worlds": 1},
    "demo method_degradation":
        {"dry_run_excluded": 0, "excluded": 0, "replay_targets": 0, "targets": 0, "worlds": 1},
    "demo worker_backlog":
        {"dry_run_excluded": 0, "excluded": 16, "replay_targets": 0, "targets": 81, "worlds": 1},
}


def test_the_queue_agrees_with_the_planner_on_fresh_worlds_of_every_family(tmp_path: Path) -> None:
    sources = sweep_sources()
    assert SWEEP_FIRST_SEED + len(sources) == TRUNCATION_FIRST_SEED
    assert TRUNCATION_FIRST_SEED + len(truncation_sources()) == READS_SEED
    assert {SWEEP_FIRST_SEED, READS_SEED} <= set(RESERVED_SEEDS)
    states: defaultdict[str, Counter[str]] = defaultdict(Counter)
    planner: defaultdict[str, Counter[str]] = defaultdict(Counter)
    problems: dict[str, dict[str, object]] = {}
    with closing(storage.connect(tmp_path / "sweep.sqlite")) as conn:
        for offset, source in enumerate(sources):
            seed = SWEEP_FIRST_SEED + offset
            swept = sweep_world(conn, source, seed)
            if swept.problems:
                problems[f"{seed} {source.label}"] = swept.problems
            states[source.group].update(case.state.value for case in swept.queue.cases)
            planner[source.group].update({
                "worlds": 1,
                "targets": swept.preview.target_count,
                "excluded": len(swept.preview.excluded),
                "dry_run_excluded": sum(bool(DRY_RUN_REASONS & set(item.reasons)) for item in swept.preview.excluded),
                "replay_targets": len(swept.replay.targets),
            })
    assert problems == {}
    measured = {
        "sources": dict(sorted(Counter(source.group for source in sources).items())),
        "states": {family: dict(sorted(counts.items())) for family, counts in sorted(states.items())},
        "planner": {family: dict(sorted(counts.items())) for family, counts in sorted(planner.items())},
    }
    pinned = {"sources": SWEEP_SOURCES_BY_FAMILY, "states": FAMILY_STATE_COUNTS, "planner": FAMILY_PLANNER_COUNTS}
    assert measured == pinned, json.dumps(measured, sort_keys=True)


# --------------------------------------------------------------------------- as of T


def test_records_known_after_t_never_change_the_queue_at_t(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    cut = FRESH + 2 * MINUTE
    orders = {number: batch_order(number) for number in range(1, 6)}
    payments = {number: f"pay_asof_{number}" for number in range(1, 6)}
    for number in (1, 2, 3, 4):
        add_order(conn, run_a, orders[number], payment_ids=(payments[number],))
    for number in (1, 2):
        deliver(conn, run_a, f"evt_asof_{number}", payment_id=payments[number], order_id=orders[number], occurred_at_ms=FRESH)
    store_observations(conn, [observation(AUTHORIZED, FRESH, FRESH, observation_id="obs_asof_3",
                                          payment_id=payments[3], order_id=orders[3])])
    before = classify(conn, cut)
    assert [case.state for case in before.cases] == [I, I, A, U]

    # Everything below becomes known after the cut, or occurs after it.
    worker.run_worker(conn, run_id=RUN_A, handler=HEALTHY, now_ms=cut + 1)  # fulfils orders 1 and 2
    deliver(conn, run_a, "evt_asof_2", payment_id=payments[2], order_id=orders[2], occurred_at_ms=FRESH,
            amount_paise=AMOUNT + 9, received_at_ms=cut + 2)  # a conflicting redelivery
    deliver(conn, run_a, "evt_asof_3", payment_id=payments[3], order_id=orders[3], occurred_at_ms=cut + 3)  # a capture
    store_observations(conn, [observation(CAPTURED, cut + MINUTE, cut - 1, observation_id="obs_asof_4",
                                          payment_id=payments[4], order_id=orders[4])])  # known before, occurring after
    rejected = deliver(conn, run_a, "evt_asof_5", secret_ref="fixture_secret_b", payment_id=payments[4],
                       order_id=orders[4], occurred_at_ms=cut + 4)
    assert rejected.outcome.value == "rejected_signature"
    add_order(conn, run_a, orders[5], payment_ids=(payments[5],), created_at_ms=cut + 5)
    with storage.write_transaction(conn):
        storage.cancel_order(conn, run_id=RUN_A, order_id=orders[4], cancelled_at_ms=cut + 6)
        storage.advance_clock(conn, RUN_A, cut + 7)
        storage.mark_order_refunded(conn, run_id=RUN_A, order_id=orders[2])  # undated, set once the clock passed the cut

    again = classify(conn, cut)
    assert (again.cases, again.totals, again.coverage) == (before.cases, before.totals, before.coverage)
    # The clock has passed the cut, so the undated refund column is no longer read there; nothing was flagged by then.
    assert (before.refund_flag_read, again.refund_flag_read) == (True, False)
    later = classify(conn, cut + 2 * MINUTE)
    assert [case.state for case in later.cases] == [C, B, I, B, E]
    assert later.coverage.rejected_deliveries == 1 > before.coverage.rejected_deliveries


def test_the_undated_refund_flag_is_read_only_from_the_run_clock(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    story = Story(conn, run_a, refunded=True)
    story.webhook()
    read = classify(conn)  # the clock (START_MS) is before T, so the flag was set by T
    assert (read.refund_flag_read, case_of(read, ORDER_1).state) == (True, B)
    with storage.write_transaction(conn):
        storage.advance_clock(conn, RUN_A, T + MINUTE)
    historical = classify(conn)  # the flag may since have been set after T: only dated refund evidence counts
    assert not historical.refund_flag_read
    assert (case_of(historical, ORDER_1).state, case_of(historical, ORDER_1).blockers) == (I, ())
    story.snapshot(status=REFUNDED, refunded=AMOUNT)  # a refund observation is dated, so it counts at T
    dated = story.case()
    assert (dated.state, "refunded" in dated.blockers) == (B, True)


def records_known_by(conn: sqlite3.Connection, run_id: str, at: int) -> dict[str, list[object]]:
    """Every append-only record known by ``at``, with the mutable, undated columns left out.

    Only replay-determined fields are compared: replay times, record IDs and content. A field measured on the wall
    clock while the replay runs can differ between two replays of one world, so it is left out. There is one:
    ``DeliveryAttemptRecord.latency_ms``, which ingestion times with ``time.perf_counter`` (``_elapsed_ms``), so the
    same delivery can record 0 ms in one replay and 1 ms in the other. Worker attempt times are the replay time.
    """
    return {
        "orders": [(o.order_id, o.created_at_ms, o.amount_paise, o.currency,
                    o.cancelled_at_ms if o.cancelled_at_ms is not None and o.cancelled_at_ms <= at else None)
                   for o in storage.list_orders(conn, run_id=run_id) if o.created_at_ms <= at],
        "attempts": list(storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=at)),
        "observations": list(storage.list_observations(conn, run_id=run_id, as_of_ms=at)),
        "inbox": [(row.inbox_id, row.event_id, row.event_type, row.body_sha256, row.received_at_ms)
                  for row in storage.list_inbox_rows(conn, run_id=run_id) if row.received_at_ms <= at],
        "worker": [item for item in storage.list_worker_attempts(conn, run_id=run_id) if item.finished_at_ms <= at],
        "deliveries": [item.model_dump(exclude={"latency_ms"})  # wall-clock measured; see the docstring
                       for item in storage.list_delivery_attempts(conn, run_id=run_id) if item.received_at_ms <= at],
        "fulfilments": [item for item in storage.list_fulfilments(conn, run_id=run_id) if item.fulfilled_at_ms <= at],
        "conflicts": [item.model_copy(update={"review_status": "pending_review"})
                      for item in storage.list_conflicting_deliveries(conn, run_id=run_id, as_of_ms=at)],
    }


def truncation_sources() -> list[Source]:
    """Worlds whose replay up to T does not depend on where the run ends (no reconciler pause)."""
    sources = sweep_sources()
    wanted = [
        next(source for source in sources if source.family == "D1"),
        next(source for source in sources if source.family == "D3"),
        next(source for source in sources if source.family == "D4" and "late_authorization" in source.label),
        next(source for source in sources if source.label == "demo worker_backlog"),
    ]
    assert all(source.spec.reconciler_pause is None for source in wanted)
    return wanted


def test_a_world_cut_at_t_and_the_whole_world_give_the_same_queue_at_t(tmp_path: Path) -> None:
    """The whole world holds records known after T; the cut one was replayed only up to T, as the run stood then.

    The one permitted difference is the undated refund column: the cut run's clock is T, so it is read there, and
    the whole run's clock is past T, so it is not. An order refunded by T on that flag alone differs, and only there.
    """
    flag_only: list[str] = []
    for offset, source in enumerate(truncation_sources()):
        seed = TRUNCATION_FIRST_SEED + offset
        world = generate_world(source.spec, seed)
        cut_at = world.start_ms + (world.cutoff_ms - world.start_ms) * 7 // 10
        with (closing(storage.connect(tmp_path / f"whole_{seed}.sqlite")) as whole,
              closing(storage.connect(tmp_path / f"cut_{seed}.sqlite")) as cut):
            driver.seed_world(whole, world, oracle_dir=None)
            driver.seed_world(cut, dataclasses.replace(world, cutoff_ms=cut_at), oracle_dir=None)
            assert records_known_by(whole, world.run_id, cut_at) == records_known_by(cut, world.run_id, cut_at)
            flagged = {order.order_id for order in storage.list_orders(whole, run_id=world.run_id) if order.refunded}
            from_whole = order_cases.classify_orders(whole, run_id=world.run_id, as_of_ms=cut_at,
                                                     stale_after_ms=STALE_AFTER_MS)
            from_cut = order_cases.classify_orders(cut, run_id=world.run_id, as_of_ms=cut_at, stale_after_ms=STALE_AFTER_MS)
        assert (from_whole.refund_flag_read, from_cut.refund_flag_read) == (False, True)
        assert from_whole.coverage == from_cut.coverage
        assert [case.order_id for case in from_whole.cases] == [case.order_id for case in from_cut.cases]
        assert len({case.state for case in from_cut.cases}) >= 3, source.label  # not a trivial queue
        differing = [then for later, then in zip(from_whole.cases, from_cut.cases, strict=True) if later != then]
        for then in differing:
            assert then.order_id in flagged, (source.label, then.order_id)
            assert "refunded" in then.blockers, (source.label, then.order_id)
            assert "refunded" not in case_of(from_whole, then.order_id).blockers
        flag_only.extend(then.order_id for then in differing)
        if not differing:
            assert from_whole.totals == from_cut.totals
    assert len(flag_only) <= 2  # the demo world refunds two orders in all


# --------------------------------------------------------------------------- reads, scope and no oracle


def test_the_queue_reads_only_workbench_tables_and_writes_nothing(tmp_path: Path) -> None:
    with closing(storage.connect(tmp_path / "reads.sqlite")) as conn:
        world = generate_world(scenarios.WORKER_BACKLOG, READS_SEED)
        summary = driver.seed_world(conn, world, oracle_dir=None)
        changes = conn.total_changes
        with TableReads(conn) as reads:
            queue = order_cases.classify_orders(conn, run_id=world.run_id, as_of_ms=summary.cutoff_ms,
                                                stale_after_ms=STALE_AFTER_MS)
        assert conn.total_changes == changes
    assert queue.cases
    assert reads.tables <= {
        "runs", "integrations", "orders", "payment_attempts", "payment_observations", "webhook_inbox",
        "worker_attempts", "delivery_attempts", "fulfilments", "conflicting_deliveries", "provider_delivery_attempts",
    }
    assert not any(table.startswith("sim_") for table in reads.tables)


def test_the_module_imports_nothing_from_the_oracle_families_or_evaluation() -> None:
    source = Path(order_cases.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported |= {node.module} | {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
    assert not [name for name in imported if name.startswith(("recovery_workbench.simulator", "recovery_workbench.evaluation"))]
    # Nothing it imports in turn may load them either: checked in a fresh interpreter.
    probe = "import sys, recovery_workbench.order_cases; print('\\n'.join(sorted(sys.modules)))"
    loaded = subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True).stdout.split()
    assert "recovery_workbench.order_cases" in loaded
    forbidden = ("recovery_workbench.simulator.oracle", "recovery_workbench.simulator.families",
                 "recovery_workbench.evaluation")
    assert [name for name in loaded if name.startswith(forbidden)] == []


def test_an_unknown_run_and_invalid_times_are_refused(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    with pytest.raises(ScopeError):
        classify(conn, run_id="run_missing")
    for as_of_ms, stale_after_ms in ((-1, STALE_AFTER), (True, STALE_AFTER), (T, 0), (T, "3600")):
        with pytest.raises(ContractError):
            classify(conn, as_of_ms, stale_after_ms=stale_after_ms)  # type: ignore[arg-type]

