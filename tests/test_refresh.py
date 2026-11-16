"""Read-only provider refresh: scope, time discipline and the simulated provider adapter."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from conftest import AMOUNT, ORDER_1, PAY_1, PAY_2, RUN_A, START_MS, add_order, business_state, deliver
from recovery_workbench import refresh, storage
from recovery_workbench.contracts import ContractError, ObservationSource, PaymentStatus, ScopeError
from recovery_workbench.simulator import scenarios
from recovery_workbench.simulator.provider import SimulatedProviderAdapter, load_provider_ledger, write_provider_records
from recovery_workbench.simulator.world import generate_world

NOW = START_MS + 60_000


class FixedAdapter:
    """Answers with prepared states, to exercise how refresh treats an adapter's answers."""

    def __init__(self, states: Mapping[str, refresh.ProviderPaymentState | None]) -> None:
        self.states = dict(states)
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def snapshot_as_of(self, payment_ids: Sequence[str], as_of_ms: int) -> dict[str, refresh.ProviderPaymentState | None]:
        self.calls.append((tuple(payment_ids), as_of_ms))
        return self.states


def captured_state(payment_id: str = PAY_1, observed_at_ms: int = NOW) -> refresh.ProviderPaymentState:
    return refresh.ProviderPaymentState(
        payment_id=payment_id, order_id=ORDER_1, method="upi", status=PaymentStatus.CAPTURED, amount_paise=AMOUNT,
        currency="INR", observed_at_ms=observed_at_ms,
    )


def test_a_refresh_records_snapshot_evidence_known_at_now(conn, run_a):
    add_order(conn, run_a)
    before = business_state(conn, RUN_A)
    adapter = FixedAdapter({PAY_1: captured_state()})
    summary = refresh.refresh_provider_state(conn, run_id=RUN_A, payment_ids=[PAY_1, PAY_1], adapter=adapter, now_ms=NOW)
    assert (summary.refreshed, summary.statuses, adapter.calls) == ((PAY_1,), {PAY_1: "captured"}, [((PAY_1,), NOW)])
    [observation] = storage.list_observations(conn, run_id=RUN_A, as_of_ms=None)
    assert (observation.source, observation.known_at_ms, observation.source_ref) == (ObservationSource.SNAPSHOT, NOW, f"refresh_{NOW}")
    assert storage.require_run(conn, RUN_A).clock_ms == NOW
    after = business_state(conn, RUN_A)
    assert {k: v for k, v in after.items() if k != "observations"} == {k: v for k, v in before.items() if k != "observations"}


def test_a_second_refresh_at_the_same_time_records_nothing_new(conn, run_a):
    add_order(conn, run_a)
    adapter = FixedAdapter({PAY_1: captured_state()})
    refresh.refresh_provider_state(conn, run_id=RUN_A, payment_ids=[PAY_1], adapter=adapter, now_ms=NOW)
    again = refresh.refresh_provider_state(conn, run_id=RUN_A, payment_ids=[PAY_1], adapter=adapter, now_ms=NOW)
    assert (again.refreshed, again.already_recorded) == ((), (PAY_1,))
    assert len(storage.list_observations(conn, run_id=RUN_A, as_of_ms=None)) == 1


def test_payments_outside_the_run_are_refused(conn, run_a, run_b):
    add_order(conn, run_a)
    add_order(conn, run_b, payment_ids=(PAY_2,))
    adapter = FixedAdapter({})
    for payment_ids in ([PAY_2], [PAY_1, "pay_never_seen"]):
        with pytest.raises(ScopeError, match="not payments of run"):
            refresh.refresh_provider_state(conn, run_id=RUN_A, payment_ids=payment_ids, adapter=adapter, now_ms=NOW)
    assert adapter.calls == []  # refused before any provider call


def test_a_refresh_cannot_go_back_in_replay_time(conn, run_a):
    add_order(conn, run_a)
    with storage.write_transaction(conn):
        storage.advance_clock(conn, RUN_A, NOW)
    adapter = FixedAdapter({})
    with pytest.raises(ContractError, match="before run"):
        refresh.refresh_provider_state(conn, run_id=RUN_A, payment_ids=[PAY_1], adapter=adapter, now_ms=NOW - 1)


@pytest.mark.parametrize(
    "answer",
    [
        {PAY_1: captured_state(observed_at_ms=NOW + 1)},  # future state
        {PAY_1: captured_state(), "pay_not_asked": None},  # unrequested payment
        {PAY_1: captured_state(payment_id=PAY_2)},  # answers for a different payment
    ],
)
def test_an_adapter_that_breaks_its_contract_changes_nothing(conn, run_a, answer):
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    adapter = FixedAdapter(answer)
    with pytest.raises(ContractError):
        refresh.refresh_provider_state(conn, run_id=RUN_A, payment_ids=[PAY_1], adapter=adapter, now_ms=NOW)
    assert storage.list_observations(conn, run_id=RUN_A, as_of_ms=None) == ()
    assert storage.require_run(conn, RUN_A).clock_ms == START_MS


def test_payments_the_provider_does_not_know_yet_are_reported_not_invented(conn, run_a):
    add_order(conn, run_a)
    summary = refresh.refresh_provider_state(conn, run_id=RUN_A, payment_ids=[PAY_1],
                                             adapter=FixedAdapter({PAY_1: None}), now_ms=NOW)
    assert (summary.refreshed, summary.unknown_at_provider) == ((), (PAY_1,))
    assert storage.list_observations(conn, run_id=RUN_A, as_of_ms=None) == ()


def test_observed_payments_without_an_attempt_row_belong_to_the_run(conn, run_a):
    deliver(conn, run_a, "evt_SYNTHETIC_0001")  # an observation, but no attempt row
    summary = refresh.refresh_provider_state(conn, run_id=RUN_A, payment_ids=[PAY_1],
                                             adapter=FixedAdapter({PAY_1: captured_state()}), now_ms=NOW)
    assert summary.refreshed == (PAY_1,)


def test_the_simulated_adapter_reads_unlabelled_records_and_never_returns_future_state(tmp_path: Path):
    world = generate_world(scenarios.LATE_AUTHORIZATION, 3)
    path = write_provider_records(world.run_id, world.attempts, tmp_path / "provider")
    raw = path.read_text(encoding="utf-8")
    for label in ("kind", "family", "fault", "oracle", "expected", "variant", "late_authorized", "stale"):
        assert f'"{label}' not in raw, label
    ledger = load_provider_ledger(path)
    adapter = SimulatedProviderAdapter(ledger)
    for attempt in world.attempts[:120]:
        for probe in (attempt.created_at_ms - 1, *(t.occurred_at_ms - 1 for t in attempt.transitions), world.cutoff_ms):
            state = adapter.snapshot_as_of([attempt.payment_id], probe)[attempt.payment_id]
            visible = [t for t in attempt.transitions if t.occurred_at_ms <= probe]
            if probe < attempt.created_at_ms:
                assert state is None
                continue
            assert state is not None
            assert state.observed_at_ms == probe
            assert state.status is (visible[-1].status if visible else PaymentStatus.CREATED)
    assert adapter.snapshot_as_of(["pay_unknown"], world.cutoff_ms) == {"pay_unknown": None}
    assert json.loads(raw)["run_id"] == world.run_id
