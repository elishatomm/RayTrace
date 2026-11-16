"""The v1 read tools: server-injected scope, cutoff filtering, and no private or oracle reads."""
from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import INTEGRATION_B, SECRET_B, START_MS, SeededRun, deliver
from pydantic import JsonValue

from recovery_workbench import ingestion, storage, tools
from recovery_workbench.contracts import ContractError, ReceiveOutcome, ScopeError
from recovery_workbench.simulator import driver, merchant, runtime, scenarios
from recovery_workbench.simulator.world import generate_world


def seed(directory: Path, scenario: str, seed_value: int = 101) -> tuple[sqlite3.Connection, driver.SeedSummary]:
    conn = storage.connect(directory / "workbench.sqlite")
    summary = driver.seed_world(conn, generate_world(scenarios.demo_scenario(scenario), seed_value),
                                oracle_dir=directory / "oracle")
    return conn, summary


@pytest.fixture(scope="module")
def backlog(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sqlite3.Connection, driver.SeedSummary]]:
    conn, summary = seed(tmp_path_factory.mktemp("tools_backlog"), "worker_backlog")
    yield conn, summary
    conn.close()


class TableReads:
    """Records every table SQLite reads on a connection (``set_authorizer``)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.tables: set[str] = set()
        self._conn = conn

    def __enter__(self) -> TableReads:
        def authorize(action: int, first: str | None, *_: object) -> int:
            if action == sqlite3.SQLITE_READ and first:
                self.tables.add(first)
            return sqlite3.SQLITE_OK

        self._conn.set_authorizer(authorize)
        return self

    def __exit__(self, *_: object) -> None:
        self._conn.set_authorizer(None)


def test_scope_cannot_reach_beyond_the_replay_clock(backlog: tuple[sqlite3.Connection, driver.SeedSummary]) -> None:
    conn, summary = backlog
    assert tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.cutoff_ms).merchant_id
    with pytest.raises(ContractError, match="future"):
        tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.cutoff_ms + 1)
    with pytest.raises(ScopeError):
        tools.scope_for(conn, run_id="run_elsewhere", cutoff_ms=summary.cutoff_ms)


def test_no_tool_reads_a_private_simulator_table(backlog: tuple[sqlite3.Connection, driver.SeedSummary]) -> None:
    conn, summary = backlog
    scope = tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.cutoff_ms)
    deployed = runtime.active_merchant(conn, run_id=summary.run_id, at_ms=summary.cutoff_ms)  # the caller's job
    order_id = storage.list_orders(conn, run_id=summary.run_id)[0].order_id
    with TableReads(conn) as reads:
        tools.get_metrics(conn, scope, cohort="all", window="current")
        tools.get_metrics(conn, scope, cohort="upi", window="current")
        tools.get_metrics(conn, scope, cohort="processing", window="current")
        tools.get_metrics(conn, scope, cohort="provider_state", window="current")
        tools.get_order_journey(conn, scope, order_id=order_id)
        tools.get_delivery_health(conn, scope, integration_id="all")
        tools.get_worker_health(conn, scope, worker_id=tools.WORKER_ID)
        tools.get_recent_changes(conn, scope, service_id="all")
        tools.run_fixture_check(deployed, test_id=merchant.FIXTURE_TEST_IDS[0])
    assert reads.tables, "the authorizer saw no reads at all"
    assert not {table for table in reads.tables if table.startswith("sim_")}
    assert set(storage.PRIVATE_SIMULATOR_TABLES).isdisjoint(reads.tables)


def _code_names(source: str) -> set[str]:
    """Imports, identifiers, attributes and string literals of a module, without its docstrings."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body
        and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.add(node.value)
    return found


def test_tool_modules_never_name_private_tables_or_oracle_files() -> None:
    source_root = Path(tools.__file__).parent
    forbidden = ("sim_", "oracle", "runtime", "merchant_release", "merchant_profile", "PRIVATE_SIMULATOR", "variant_truth")
    for name in ("tools.py", "packet.py"):
        names = _code_names((source_root / name).read_text(encoding="utf-8"))
        assert not [item for item in names if any(word in item for word in forbidden)], name


def test_recent_changes_and_metrics_only_see_what_was_known_by_the_cutoff(
    backlog: tuple[sqlite3.Connection, driver.SeedSummary],
) -> None:
    conn, summary = backlog
    late = tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.cutoff_ms)
    early = tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.start_ms + 60 * 60_000)
    all_changes = tools.get_recent_changes(conn, late, service_id="all")
    early_changes = tools.get_recent_changes(conn, early, service_id="all")
    assert len(early_changes) < len(all_changes) == 3
    assert all(change["deployed_at_ms"] <= early.cutoff_ms for change in early_changes)
    assert tools.get_recent_changes(conn, late, service_id="order_worker") == [
        change for change in all_changes if change["service"] == "order_worker"
    ]
    early_health = tools.get_worker_health(conn, early, worker_id=tools.WORKER_ID)
    late_health = tools.get_worker_health(conn, late, worker_id=tools.WORKER_ID)
    assert early_health["health"]["inbox_failed"] <= late_health["health"]["inbox_failed"]
    assert late_health["errors"]["last_failure_ms"] <= late.cutoff_ms


def test_worker_health_is_reconstructed_as_of_the_cutoff_not_from_current_rows(
    backlog: tuple[sqlite3.Connection, driver.SeedSummary],
) -> None:
    conn, summary = backlog
    scope = tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.cutoff_ms)
    health = tools.get_worker_health(conn, scope, worker_id=tools.WORKER_ID)["health"]
    rows = [row for row in storage.list_inbox_rows(conn, run_id=summary.run_id) if row.received_at_ms <= scope.cutoff_ms]
    assert health["inbox_failed"] == sum(row.processing_status.value == "failed" for row in rows)
    assert health["retry_policy"].startswith(f"{tools.worker_max_attempts()} attempts")


def test_errors_and_scope_are_enforced(backlog: tuple[sqlite3.Connection, driver.SeedSummary]) -> None:
    conn, summary = backlog
    scope = tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.cutoff_ms)
    with pytest.raises(ContractError):
        tools.get_metrics(conn, scope, cohort="wallet", window="current")
    with pytest.raises(ContractError):
        tools.get_metrics(conn, scope, cohort="all", window="baseline")
    with pytest.raises(ScopeError):
        tools.get_order_journey(conn, scope, order_id="order_SYNTHETIC_9999")
    with pytest.raises(ScopeError):
        tools.get_delivery_health(conn, scope, integration_id="int_elsewhere")
    with pytest.raises(ContractError):
        tools.get_worker_health(conn, scope, worker_id="another_worker")
    deployed = merchant.compose()
    with pytest.raises(ContractError):
        tools.run_fixture_check(deployed, test_id="any_test")


def test_delivery_health_has_no_wall_clock_values(backlog: tuple[sqlite3.Connection, driver.SeedSummary]) -> None:
    conn, summary = backlog
    scope = tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.cutoff_ms)
    facts = tools.get_delivery_health(conn, scope, integration_id="all")
    assert not [key for key in facts if "latency" in key or "wall" in key]
    assert facts["first_rejection_ms"] is None
    assert facts["accepted_since_first_rejection"] is None


def delivery_health_at(conn: sqlite3.Connection, run: SeededRun, integration_id: str) -> dict[str, JsonValue]:
    """Delivery health a minute into the run, after the deliveries the caller made."""
    with storage.write_transaction(conn):
        storage.advance_clock(conn, run.run_id, START_MS + 60_000)
    scope = tools.scope_for(conn, run_id=run.run_id, cutoff_ms=START_MS + 60_000)
    return tools.get_delivery_health(conn, scope, integration_id=integration_id)


def test_delivery_health_of_all_integrations_ignores_a_request_to_one_the_run_does_not_configure(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """The request leaves its row, but delivery health is per integration (README section 8), as in the rejection
    detectors: an unsigned POST to any other integration path must not show the run's receiver rejecting deliveries."""
    accepted = deliver(conn, run_a, "evt_capture_1", occurred_at_ms=START_MS + 1_000)
    unsigned = ingestion.receive_webhook(conn, run_id=run_a.run_id, integration_id="int_NOT_CONFIGURED", headers={},
                                         raw_body=b"{}", received_at_ms=START_MS + 2_000)
    assert (accepted.outcome, unsigned.outcome) == (ReceiveOutcome.ACCEPTED, ReceiveOutcome.UNKNOWN_INTEGRATION)
    assert len(storage.list_delivery_attempts(conn, run_id=run_a.run_id)) == 2
    health = delivery_health_at(conn, run_a, "all")
    assert health == delivery_health_at(conn, run_a, run_a.integration_id)
    rejections = (health["delivery_attempts"], health["distinct_events_rejected"], health["first_rejection_ms"])
    assert rejections == (1, 0, None)


def test_delivery_health_of_all_integrations_covers_every_integration_the_run_configures(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    with storage.write_transaction(conn):
        storage.add_integration(conn, storage.IntegrationRecord(
            run_id=run_a.run_id, integration_id=INTEGRATION_B, secret_ref=SECRET_B, max_body_bytes=65_536,
        ))
    deliver(conn, run_a, "evt_capture_1", occurred_at_ms=START_MS + 1_000)
    refused = deliver(conn, run_a, "evt_capture_2", integration_id=INTEGRATION_B, occurred_at_ms=START_MS + 2_000)
    assert refused.outcome is ReceiveOutcome.REJECTED_SIGNATURE  # signed with integration A's secret
    health = delivery_health_at(conn, run_a, "all")
    counted = ("delivery_attempts", "accepted", "rejected_signature", "distinct_events_rejected", "first_rejection_ms",
               "accepted_since_first_rejection")
    assert {key: health[key] for key in counted} == {
        "delivery_attempts": 2, "accepted": 1, "rejected_signature": 1, "distinct_events_rejected": 1,
        "first_rejection_ms": START_MS + 2_050, "accepted_since_first_rejection": 0,
    }


def test_an_order_journey_shows_only_observations_known_by_the_cutoff(
    backlog: tuple[sqlite3.Connection, driver.SeedSummary],
) -> None:
    conn, summary = backlog
    scope = tools.scope_for(conn, run_id=summary.run_id, cutoff_ms=summary.cutoff_ms)
    view = tools.load_view(conn, scope)
    order_id = tools.sample_journey_orders(view, scope, limit=1)[0]
    journey = tools.get_order_journey(conn, scope, order_id=order_id)
    observations = [item for attempt in journey["attempts"] for item in attempt["observations"]]
    assert observations
    assert all(item["known_at_ms"] <= scope.cutoff_ms for item in observations)
    assert set(journey["projection"]) == {"status", "exclusions", "late_authorization", "conflicts"}


def test_fixture_checks_run_on_the_deployed_handler_only(backlog: tuple[sqlite3.Connection, driver.SeedSummary]) -> None:
    conn, summary = backlog
    deployed = runtime.active_merchant(conn, run_id=summary.run_id, at_ms=summary.cutoff_ms)
    result = tools.run_fixture_check(deployed, test_id="captured_payment_builds_fulfilment")
    assert result.deployed_version == deployed.version
    assert not result.passed


def test_the_phase_one_allowlist_still_refuses_injected_scope() -> None:
    with pytest.raises(ContractError, match="server injects"):
        tools.validate_tool_call("get_metrics", {"cohort": "all", "window": "current", "run_id": "run_x"})
    assert tools.validate_tool_call("run_fixture_check", {"test_id": "signed_delivery_verifies"}).name == "run_fixture_check"
