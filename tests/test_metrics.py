"""Provider and merchant measures, windows and the method detector, on generated runs."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from recovery_workbench import metrics, storage
from recovery_workbench.contracts import Hypothesis, Operation, ReceiveOutcome
from recovery_workbench.simulator import driver, scenarios
from recovery_workbench.simulator.world import MINUTE_MS, World, generate_world

HIGH_VOLUME = {"orders_per_hour": 480, "duration_minutes": 300, "method_mix": {"upi": 0.4, "card": 0.3, "netbanking": 0.3}}


def detector_spec(name: str, **changes: object) -> scenarios.ScenarioSpec:
    """Enough volume that every method clears the detector minimums (baseline 200, current 50)."""
    fields = {
        "name": name, "family": "D2", "variant_label": name, "split": "unit",
        "expected_hypothesis": Hypothesis.PROVIDER_METHOD_DEGRADATION, "expected_operation": Operation.ESCALATE,
        "authorization_rate": {"upi": 0.95, "card": 0.92, "netbanking": 0.70}, **HIGH_VOLUME, **changes,
    }
    return scenarios.ScenarioSpec.model_validate(fields)


def seeded_view(directory: Path, spec: scenarios.ScenarioSpec, seed: int) -> tuple[World, metrics.RunView, object]:
    conn = storage.connect(directory / "workbench.sqlite")
    world = generate_world(spec, seed)
    driver.seed_world(conn, world, oracle_dir=directory / "oracle")
    return world, metrics.load_run_view(conn, world.run_id, world.cutoff_ms, stale_after_ms=30 * MINUTE_MS), conn


@pytest.fixture(scope="module")
def degraded(tmp_path_factory: pytest.TempPathFactory):
    spec = detector_spec("card_drop", degradation={"method": "card", "authorization_rate": 0.5, "start_fraction": 0.62})
    world, view, conn = seeded_view(tmp_path_factory.mktemp("degraded"), spec, 3)
    yield world, view
    conn.close()


@pytest.fixture(scope="module")
def mix_shift(tmp_path_factory: pytest.TempPathFactory):
    spec = detector_spec("mix_shift", mix_shift={"method_mix": {"upi": 0.2, "card": 0.2, "netbanking": 0.6},
                                                 "start_fraction": 0.62})
    world, view, conn = seeded_view(tmp_path_factory.mktemp("mix"), spec, 3)
    yield world, view
    conn.close()


@pytest.fixture(scope="module")
def backlog(tmp_path_factory: pytest.TempPathFactory):
    directory = tmp_path_factory.mktemp("backlog")
    world, view, conn = seeded_view(directory, scenarios.WORKER_BACKLOG, 101)
    oracle = json.loads((directory / "oracle" / f"{world.run_id}.json").read_text(encoding="utf-8"))
    yield world, view, conn, oracle
    conn.close()


def method_results(view: metrics.RunView) -> dict[str, metrics.DetectorResult]:
    return {r.cohort["method"]: r for r in metrics.detect_method_drop(view, metrics.DEFAULT_INCIDENT_DETECTORS)}


def test_ratios_always_carry_their_denominator():
    assert metrics.Ratio(3, 4).as_json() == {"numerator": 3, "denominator": 4, "value": 0.75}
    assert metrics.Ratio(0, 0).as_json() == {"numerator": 0, "denominator": 0, "value": None}


def test_mature_windows_end_one_horizon_before_the_evaluation_time():
    windows = metrics.MeasureWindows(horizon_ms=10, window_ms=30, baseline_ms=100)
    assert windows.current(1_000) == (960, 990)
    assert windows.previous(1_000) == (930, 960)
    assert windows.baseline(1_000) == (830, 930)


def test_the_method_detector_fires_only_for_the_degraded_method(degraded):
    _, view = degraded
    results = method_results(view)
    assert results["card"].fired
    assert results["card"].reason == "degraded_in_two_successive_windows"
    for method in ("upi", "netbanking"):  # judged on full samples, not skipped for lack of data
        assert (results[method].fired, results[method].reason) == (False, "within_baseline")


def test_a_method_mix_shift_does_not_fire_the_method_detector(mix_shift):
    world, view = mix_shift
    shifted_at = world.start_ms + int(0.62 * world.spec.duration_minutes * MINUTE_MS)
    aggregate_now = metrics.provider_measures(view, metrics.DEFAULT_WINDOWS.current(world.cutoff_ms)).authorization
    aggregate_before = metrics.provider_measures(view, (shifted_at - 120 * MINUTE_MS, shifted_at)).authorization
    assert aggregate_before.value - aggregate_now.value >= 0.04  # the aggregate really fell
    results = method_results(view)
    assert not any(result.fired for result in results.values())
    assert any(result.reason == "within_baseline" for result in results.values())


def test_provider_measures_count_distinct_attempts_with_freshness(backlog):
    world, view, _, _ = backlog
    window = metrics.DEFAULT_WINDOWS.current(world.cutoff_ms)
    measures = metrics.provider_measures(view, window, method="upi")
    expected = [a for a in view.attempts if window[0] <= a.created_at_ms < window[1] and a.method == "upi"]
    assert measures.authorization.denominator == measures.capture.denominator == len(expected) > 0
    as_json = measures.as_json()
    assert as_json["freshness_ms"] is not None
    assert as_json["freshness_ms"] >= 0
    assert set(as_json) >= {"authorization", "capture", "coverage", "unresolved", "window_start_ms", "window_end_ms"}


def test_merchant_measures_match_the_oracle_backlog(backlog):
    """The processing gap counts captured orders the worker should have processed.

    It follows the worker's own blocking rules, not the recovery plan's: that is the oracle's
    reprocessing-eligible set plus orders recovery holds back only for stale provider state,
    which the worker does not check. Late authorisation blocks neither since decision Q8.
    """
    world, view, _, oracle = backlog
    worker_eligible = {order_id for order_id, order in oracle["orders"].items()
                       if set(order["reasons"]) <= {"stale_snapshot"}}
    assert worker_eligible > {order_id for order_id, o in oracle["orders"].items() if not o["reasons"]}
    assert not any("late_authorization" in order["reasons"] for order in oracle["orders"].values())
    measures = metrics.merchant_measures(view, grace_ms=0)
    assert measures.pending_captured_orders == len(worker_eligible)
    amounts = {order.order_id: order.amount_paise for order in world.orders}
    assert measures.pending_captured_value_paise == sum(amounts[order_id] for order_id in worker_eligible)
    assert measures.completion.numerator == len(view.fulfilments)


def test_acknowledgements_are_counted_separately_from_logical_events(backlog):
    world, view, conn, _ = backlog
    measures = metrics.merchant_measures(view)
    outcomes = Counter(a.outcome for a in storage.list_delivery_attempts(conn, run_id=world.run_id))
    assert measures.logical_events == len(storage.list_inbox_rows(conn, run_id=world.run_id))
    assert measures.delivery_attempts == sum(outcomes.values()) > measures.logical_events
    assert measures.acknowledged_attempts == measures.delivery_attempts - measures.rejected_attempts
    assert measures.attempts_by_outcome[ReceiveOutcome.DUPLICATE.value] == outcomes[ReceiveOutcome.DUPLICATE]


def test_backlog_age_counts_from_the_oldest_unsettled_event(backlog):
    world, view, _, _ = backlog
    measures = metrics.merchant_measures(view)
    unsettled = [row for row in view.inbox if row.inbox_id not in view.settled]
    assert measures.backlog_rows == len(unsettled) > 0
    assert measures.oldest_backlog_age_ms == world.cutoff_ms - min(row.received_at_ms for row in unsettled)
    assert measures.oldest_backlog_age_ms > world.cutoff_ms - world.faulting_deployment.deployed_at_ms - 5 * MINUTE_MS
    assert measures.rows_with_failures == measures.backlog_rows
