"""Owner decision Q26: the read-only refresh also re-checks recently failed payments, and runs before a provider replay.

A late authorisation whose webhooks were all lost projects as a failure, so its order reads ``payment_failed / none``
and nothing asks the provider again. Seed 19156 of the D3
world with a rejecting receiver and no innocent deploy holds one such order.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import (
    INTEGRATION_A,
    MERCHANT_A,
    RUN_A,
    SECRET_A,
    START_MS,
    SeededRun,
    add_order,
    observation,
    seed_run,
    store_observations,
)
from test_api import create_run, diagnose, get, log_in, make_client, post, queue_at_clock

from recovery_workbench import api, cli, metrics, order_cases, settings, storage, tools
from recovery_workbench.contracts import ModelMode, Operation, PaymentStatus
from recovery_workbench.simulator import driver, families, scenarios, signing
from recovery_workbench.simulator.provider import AttemptTruth
from recovery_workbench.simulator.scenarios import ScenarioSpec
from recovery_workbench.simulator.world import World, generate_world

HIDDEN_CAPTURE_SEED = 19156
HIDDEN_CAPTURE_PARAMS = {"fault": "receiver@2", "harmless_position": "none"}  # D3, as dev
DEMO_NAME = "d3_hidden_capture"  # served to cli.demo only by these tests
RECENT_FAILURES_KEPT = 150  # the owner's cap, after at most 50 open or late-authorised payments
MINUTE = 60_000
HOUR = 60 * MINUTE
NOW = START_MS + 8 * HOUR  # the replay clock of the constructed runs; their look-back starts 6 hours earlier
CREATED, AUTHORIZED, CAPTURED, FAILED = (PaymentStatus.CREATED, PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED,
                                         PaymentStatus.FAILED)


# --------------------------------------------------------------------------- a capture whose webhooks were all lost


@dataclass(frozen=True)
class HiddenCapture:
    spec: ScenarioSpec
    world: World
    payment_id: str
    order_id: str


def authorised_after_failing(attempt: AttemptTruth, cutoff_ms: int) -> bool:
    known = [item for item in attempt.transitions if item.occurred_at_ms <= cutoff_ms]
    failed = [item.occurred_at_ms for item in known if item.status is FAILED]
    succeeded = [item.occurred_at_ms for item in known if item.status in (AUTHORIZED, CAPTURED)]
    return bool(failed) and any(at > min(failed) for at in succeeded)


@pytest.fixture(scope="module")
def hidden_capture(tmp_path_factory: pytest.TempPathFactory) -> HiddenCapture:
    """The world's one payment the provider authorised after a failure and the workbench still projects failed."""
    runs = [run for run in families.load_manifest().runs if run.family == "D3" and run.params == HIDDEN_CAPTURE_PARAMS]
    spec = families.spec_for(runs[0].model_copy(update={"split": "dev"}))
    world = generate_world(spec, HIDDEN_CAPTURE_SEED)
    with closing(storage.connect(tmp_path_factory.mktemp("hidden") / "seeded.sqlite")) as conn:
        driver.seed_world(conn, world, oracle_dir=None)
        view = metrics.load_run_view(conn, world.run_id, world.cutoff_ms,
                                     stale_after_ms=metrics.DEFAULT_WINDOWS.stale_after_ms)
    [attempt] = [attempt for attempt in world.attempts if authorised_after_failing(attempt, world.cutoff_ms)
                 and view.payments[attempt.payment_id].status is FAILED]
    return HiddenCapture(spec, world, attempt.payment_id, attempt.order_id)


def case_state(conn: sqlite3.Connection, run_id: str, order_id: str) -> tuple[str, str]:
    queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=storage.require_run(conn, run_id).clock_ms,
                                        stale_after_ms=settings.load_settings().stale_after_seconds * 1000)
    case = next(item for item in queue.cases if item.order_id == order_id)
    return case.state.value, case.next_step.value


def test_the_cli_demo_refreshes_before_the_replay_and_the_hidden_capture_becomes_a_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hidden_capture: HiddenCapture
) -> None:
    """No shipped demo scenario proposes a replay, so this one serves the D3 world under its own name."""
    shipped = scenarios.demo_scenario
    monkeypatch.setattr(scenarios, "demo_scenario",
                        lambda name: hidden_capture.spec if name == DEMO_NAME else shipped(name))
    run_id = hidden_capture.world.run_id
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lines: list[str] = []
    assert cli.demo(scenario=DEMO_NAME, seed=HIDDEN_CAPTURE_SEED, mode=ModelMode.RULES, data_dir=data_dir,
                    recordings_dir=data_dir / cli.RECORDINGS_DIRECTORY, write=lines.append) == 0
    titles = [line.split("] ", 1)[1] for line in lines if line.startswith("[")]
    assert titles[titles.index("refresh (read-only provider snapshot)") + 1].startswith("prepare provider replay")
    report = json.loads((data_dir / cli.REPORTS_DIRECTORY / f"{run_id}.json").read_text(encoding="utf-8"))
    [replay] = [plan for plan in report["plans"] if plan["operation"] == Operation.PREPARE_PROVIDER_REPLAY.value]
    assert hidden_capture.payment_id in {target["id"] for target in replay["targets"]}
    with closing(storage.connect(data_dir / cli.DATABASE_FILE)) as conn:
        after = case_state(conn, run_id, hidden_capture.order_id)
    assert after == ("notification_not_accepted", "prepare_provider_replay")


def test_the_api_replay_rechecks_first_and_the_hidden_capture_becomes_a_target(
    tmp_path: Path, hidden_capture: HiddenCapture
) -> None:
    run_id, order_id = hidden_capture.world.run_id, hidden_capture.order_id
    with make_client(tmp_path) as client:
        log_in(client)
        workbench = client.app.state.workbench
        with closing(storage.connect(workbench.database)) as conn:
            driver.seed_world(conn, hidden_capture.world, oracle_dir=workbench.config.data_dir / cli.ORACLE_DIRECTORY)
        _, before = queue_at_clock(client, run_id)
        found = diagnose(client, run_id)["diagnosis"]
        replay = post(client, f"/api/runs/{run_id}/plans",
                      {"operation": "prepare_provider_replay", "diagnosis_id": found["diagnosis_id"]})
        _, after = queue_at_clock(client, run_id)
    assert (before[order_id]["state"], before[order_id]["next_step"]) == ("payment_failed", "none")
    assert replay["refresh_skipped_because"] is None
    assert hidden_capture.payment_id in replay["refresh"]["refreshed"]
    assert replay["plan"]["created_at_ms"] == replay["refresh"]["now_ms"] + cli.OPERATOR_STEP_MS
    assert hidden_capture.payment_id in {target["id"] for target in replay["targets"]}
    assert (after[order_id]["state"], after[order_id]["next_step"]) == ("notification_not_accepted",
                                                                        "prepare_provider_replay")


def test_without_the_runs_provider_records_the_api_replay_says_it_did_not_recheck(
    tmp_path: Path, hidden_capture: HiddenCapture
) -> None:
    run_id = hidden_capture.world.run_id
    with make_client(tmp_path) as client:
        log_in(client)
        with closing(storage.connect(client.app.state.workbench.database)) as conn:
            driver.seed_world(conn, hidden_capture.world, oracle_dir=None)  # no provider records are written
        found = diagnose(client, run_id)["diagnosis"]
        replay = post(client, f"/api/runs/{run_id}/plans",
                      {"operation": "prepare_provider_replay", "diagnosis_id": found["diagnosis_id"]})
    assert replay["refresh"] is None
    assert "no provider records" in replay["refresh_skipped_because"]
    assert hidden_capture.payment_id not in {target["id"] for target in replay["targets"]}


# --------------------------------------------------------------------------- which payments a refresh re-checks


def payment(number: int) -> str:
    return signing.synthetic_id("pay", 5000 + number)


def add_payment(conn: sqlite3.Connection, run: SeededRun, number: int,
                *seen: tuple[PaymentStatus, int] | tuple[PaymentStatus, int, int]) -> str:
    """One order with one payment, seen by snapshots as (status, occurred at[, known at]), by default known at once."""
    payment_id, order_id = payment(number), signing.synthetic_id("order", 5000 + number)
    add_order(conn, run, order_id, payment_ids=(payment_id,))
    store_observations(conn, [
        observation(status, at, known[0] if known else at, observation_id=f"obs_{payment_id}_{index}",
                    payment_id=payment_id, order_id=order_id)
        for index, (status, at, *known) in enumerate(seen)
    ])
    return payment_id


def move_clock_to_now(conn: sqlite3.Connection) -> None:
    with storage.write_transaction(conn):
        storage.advance_clock(conn, RUN_A, NOW)


def test_recently_failed_payments_follow_todays_selection_newest_first(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    created = add_payment(conn, run_a, 1, (CREATED, NOW - HOUR))
    authorised = add_payment(conn, run_a, 2, (AUTHORIZED, NOW - HOUR))
    late = add_payment(conn, run_a, 3, (FAILED, NOW - 2 * HOUR), (AUTHORIZED, NOW - 2 * HOUR + 10 * MINUTE))
    never_seen = add_payment(conn, run_a, 4)  # an attempt with no observation: unknown, so open
    newest = add_payment(conn, run_a, 5, (FAILED, NOW - MINUTE))
    tied_first = add_payment(conn, run_a, 6, (FAILED, NOW - 2 * HOUR))
    tied_second = add_payment(conn, run_a, 7, (FAILED, NOW - 2 * HOUR))
    at_window_start = add_payment(conn, run_a, 8, (FAILED, NOW - tools.LOOKBACK_MS))
    add_payment(conn, run_a, 9, (FAILED, NOW - tools.LOOKBACK_MS - 1))  # just before the window
    add_payment(conn, run_a, 10, (FAILED, NOW - 7 * HOUR, NOW - MINUTE))  # learned late: the failure's time counts
    add_payment(conn, run_a, 11, (FAILED, NOW - 7 * HOUR), (FAILED, NOW - MINUTE))  # its first failure counts
    add_payment(conn, run_a, 12, (CAPTURED, NOW - HOUR))  # settled: no refresh can change it
    move_clock_to_now(conn)
    assert cli.refresh_candidates(conn, RUN_A) == (
        *sorted([created, authorised, late, never_seen]), newest, tied_first, tied_second, at_window_start)


def test_each_part_keeps_its_cap_so_one_refresh_request_fits_the_apis_limit(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    open_payments = [add_payment(conn, run_a, number, (CREATED, NOW - HOUR)) for number in range(1, 56)]
    failed = [add_payment(conn, run_a, 100 + number, (FAILED, NOW - number * MINUTE)) for number in range(1, 161)]
    move_clock_to_now(conn)
    candidates = cli.refresh_candidates(conn, RUN_A)
    assert candidates == (*sorted(open_payments)[:50], *failed[:RECENT_FAILURES_KEPT])  # failed: newest first
    assert len(candidates) == api.MAX_REFRESH_PAYMENTS


def test_the_api_offers_recent_failures_up_to_the_cap_and_refreshes_them_in_one_request(tmp_path: Path) -> None:
    """The seed-101 method-degradation world has more recent failures than the cap."""
    with make_client(tmp_path) as client:
        log_in(client)
        run_id = create_run(client, "method_degradation", 101)
        candidates = get(client, f"/api/runs/{run_id}/payment-state")["refresh_candidates"]
        with closing(storage.connect(client.app.state.workbench.database)) as conn:
            clock_ms = storage.require_run(conn, run_id).clock_ms
            view = metrics.load_run_view(conn, run_id, clock_ms, stale_after_ms=metrics.DEFAULT_WINDOWS.stale_after_ms)
        refreshed = post(client, f"/api/runs/{run_id}/refresh", {"payment_ids": candidates})
    failed = [payment_id for payment_id, projected in view.payments.items() if projected.status is FAILED]
    assert len(failed) > RECENT_FAILURES_KEPT
    assert sum(view.payments[payment_id].status is FAILED for payment_id in candidates) == RECENT_FAILURES_KEPT
    assert len(candidates) <= api.MAX_REFRESH_PAYMENTS
    assert sorted(refreshed["refreshed"]) == sorted(candidates)


def test_with_nothing_to_recheck_the_replay_path_calls_no_provider_and_says_why(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        workbench = client.app.state.workbench
        with closing(storage.connect(workbench.database)) as conn:
            run = seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)
            add_payment(conn, run, 1, (CAPTURED, START_MS))  # settled by the run's clock; no provider records exist
            rechecked = api._recheck_before_replay(conn, workbench, RUN_A)
            clock_ms = storage.require_run(conn, RUN_A).clock_ms
    assert rechecked == {"refresh": None, "refresh_skipped_because": cli.NOTHING_TO_REFRESH}
    assert clock_ms == START_MS  # no refresh, so no operator step
