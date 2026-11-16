"""World generator and driver: determinism, scenario properties, leakage and time discipline."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from recovery_workbench import metrics, storage
from recovery_workbench.contracts import (
    ExclusionReason,
    Hypothesis,
    ObservationSource,
    Operation,
    PaymentStatus,
    ReceiveOutcome,
    WorkerResult,
    canonical_json,
    sha256_hex,
)
from recovery_workbench.simulator import driver, scenarios
from recovery_workbench.simulator.oracle import observable_dataset_hash
from recovery_workbench.simulator.world import OrderKind, World, generate_world

BANNED_WORDS = re.compile(
    r"\b(bug|bugs|buggy|fault|faults|faulty|faulting|broken|fix|fixes|fixed|fixing|hotfix|regression|defect|"
    r"culprit|oracle|injected|harmless|innocent|root[ _]cause)\b",
    re.IGNORECASE,
)
ORACLE_LABELS = re.compile(
    r"\bD[1-6]\b|top_frame_in_changed_file|upstream_of_failing_frame|configuration_only|receiver_code|receiver_config"
    r"|\b(config|payload|orders|handler|receiver|checkout|notifications)@\d+\b|faulting_deploy|expected_operation"
)


class Seeded:
    def __init__(self, conn: sqlite3.Connection, world: World, summary: driver.SeedSummary, data_dir: Path) -> None:
        self.conn, self.world, self.summary, self.data_dir = conn, world, summary, data_dir

    @property
    def oracle(self) -> dict:
        return json.loads(Path(str(self.summary.oracle_path)).read_text(encoding="utf-8"))


def seed(directory: Path, spec: scenarios.ScenarioSpec, seed_value: int) -> Seeded:
    directory.mkdir(parents=True, exist_ok=True)
    conn = storage.connect(directory / "workbench.sqlite")
    world = generate_world(spec, seed_value)
    summary = driver.seed_world(conn, world, oracle_dir=directory / "oracle")
    return Seeded(conn, world, summary, directory)


def receiver_spec(variant: str) -> scenarios.ScenarioSpec:
    return scenarios.ScenarioSpec(
        name=f"receiver_check_{variant.replace('@', '_')}", family="D3", variant_label="receiver_check",
        expected_hypothesis=Hypothesis.RECEIVER_REJECTING_DELIVERIES,
        expected_operation=Operation.PREPARE_PROVIDER_REPLAY, split="unit", duration_minutes=120,
        orders_per_hour=60, method_mix={"upi": 0.5, "card": 0.5}, authorization_rate={"upi": 0.95, "card": 0.95},
        fault=scenarios.Deploy(variant=variant, at_fraction=0.5),
    )


@pytest.fixture(scope="module")
def backlog(tmp_path_factory: pytest.TempPathFactory):
    seeded = seed(tmp_path_factory.mktemp("backlog"), scenarios.WORKER_BACKLOG, 101)
    yield seeded
    seeded.conn.close()


@pytest.fixture(scope="module")
def late(tmp_path_factory: pytest.TempPathFactory):
    seeded = seed(tmp_path_factory.mktemp("late"), scenarios.LATE_AUTHORIZATION, 7)
    yield seeded
    seeded.conn.close()


@pytest.fixture(scope="module", params=["receiver@2", "config@3"])
def receiver_run(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    seeded = seed(tmp_path_factory.mktemp("receiver"), receiver_spec(request.param), 11)
    yield seeded
    seeded.conn.close()


# --------------------------------------------------------------------------- determinism


def test_generation_is_deterministic_and_seed_sensitive():
    first = generate_world(scenarios.WORKER_BACKLOG, 5)
    regenerated = generate_world(scenarios.WORKER_BACKLOG, 5)  # a second, independent generation from the same seed
    assert first == regenerated
    assert first != generate_world(scenarios.WORKER_BACKLOG, 6)


def test_the_same_seed_gives_the_same_observable_content_in_any_database(tmp_path: Path):
    first = seed(tmp_path / "first", scenarios.WORKER_BACKLOG, 5)
    shared = seed(tmp_path / "shared", scenarios.WORKER_BACKLOG, 6)  # a database that already holds another run
    second = driver.seed_world(shared.conn, generate_world(scenarios.WORKER_BACKLOG, 5), oracle_dir=None)
    try:
        assert first.summary.observable_sha256 == second.observable_sha256
        assert first.summary.observable_sha256 != shared.summary.observable_sha256
        assert first.summary.counts == second.counts
    finally:
        first.conn.close()
        shared.conn.close()


# --------------------------------------------------------------------------- worker_backlog (Flow A)


def test_worker_backlog_has_processing_before_the_fault_and_real_tracebacks_after(backlog: Seeded):
    run_id, conn = backlog.world.run_id, backlog.conn
    fault = backlog.world.faulting_deployment
    assert fault is not None
    assert len(storage.list_orders(conn, run_id=run_id)) >= 50
    fulfilled_at = [f.fulfilled_at_ms for f in storage.list_fulfilments(conn, run_id=run_id)]
    assert len(fulfilled_at) >= 10
    assert max(fulfilled_at) < fault.deployed_at_ms
    failures = [a for a in storage.list_worker_attempts(conn, run_id=run_id) if a.result is WorkerResult.FAILED]
    assert failures
    assert min(a.started_at_ms for a in failures) >= fault.deployed_at_ms
    assert all((a.stack_trace or "").startswith("Traceback (most recent call last):") for a in failures)
    assert all('File "merchant_app/payload.py"' in (a.stack_trace or "") for a in failures)
    pending = [order for order in backlog.oracle["orders"].values() if order["expected"] == "eligible_for_reprocessing"]
    assert len(pending) > len(fulfilled_at)  # most orders are stuck after the faulting deploy


def test_worker_backlog_contains_every_edge_case(backlog: Seeded):
    run_id, conn = backlog.world.run_id, backlog.conn
    kinds = Counter(order.kind for order in backlog.world.orders)
    for field, count in scenarios.WORKER_BACKLOG.special_orders.counts().items():
        assert kinds[OrderKind(field)] == count, field
    view = metrics.load_run_view(conn, run_id, backlog.world.cutoff_ms)
    exclusions = Counter(reason for p in view.order_projections.values() for reason in p.exclusions)
    for reason in (ExclusionReason.CANCELLED, ExclusionReason.REFUNDED, ExclusionReason.MULTIPLE_CAPTURES,
                   ExclusionReason.AMOUNT_MISMATCH, ExclusionReason.CONFLICTING_EVIDENCE,
                   ExclusionReason.UNRESOLVED_ATTEMPT, ExclusionReason.ALREADY_FULFILLED):
        assert exclusions[reason] >= 1, reason
    assert sum(p.late_authorization for p in view.payments.values()) >= 3
    outcomes = Counter(a.outcome for a in storage.list_delivery_attempts(conn, run_id=run_id))
    assert outcomes[ReceiveOutcome.DUPLICATE] >= 4
    assert outcomes[ReceiveOutcome.CONFLICTING_ID] == 2


def test_the_most_recent_deployment_is_innocent_and_appears_in_tracebacks(backlog: Seeded):
    deployments = storage.list_deployments(backlog.conn, run_id=backlog.world.run_id, as_of_ms=backlog.world.cutoff_ms)
    latest = deployments[-1]
    assert latest.deploy_id != backlog.oracle["faulting_deploy_id"]
    assert latest.deploy_id in backlog.oracle["harmless_deploy_ids"]
    versions = {a.worker_version for a in storage.list_worker_attempts(backlog.conn, run_id=backlog.world.run_id)
                if a.result is WorkerResult.FAILED}
    assert {backlog.oracle["faulting_deploy_id"], latest.deploy_id} <= versions
    assert latest.files_changed == ("merchant_app/handler.py",)


# --------------------------------------------------------------------------- receiver variants (D3)


def test_receiver_variants_reject_valid_deliveries_only_after_their_deploy(receiver_run: Seeded):
    run_id, conn = receiver_run.world.run_id, receiver_run.conn
    deployed_at = receiver_run.world.deployments[0].deployed_at_ms
    attempts = storage.list_delivery_attempts(conn, run_id=run_id)
    before = {a.outcome for a in attempts if a.received_at_ms < deployed_at}
    after = {a.outcome for a in attempts if a.received_at_ms >= deployed_at}
    assert before <= {ReceiveOutcome.ACCEPTED, ReceiveOutcome.DUPLICATE}
    assert after == {ReceiveOutcome.REJECTED_SIGNATURE}
    assert all(row.received_at_ms < deployed_at for row in storage.list_inbox_rows(conn, run_id=run_id))
    retried = Counter(a.body_sha256 for a in attempts if a.outcome is ReceiveOutcome.REJECTED_SIGNATURE)
    assert max(retried.values()) > 1  # the sender retries with backoff
    expected = Counter(order["expected"] for order in receiver_run.oracle["orders"].values())
    assert expected["capture_not_durably_received"] >= 5
    [incident] = storage.list_incidents(conn, run_id=run_id)
    assert metrics.REJECTION_SPIKE in incident.measured_impact["fired_detectors"]


# --------------------------------------------------------------------------- late authorisation (Flow C)


def test_late_authorization_run_contains_late_authorisations_and_stale_snapshots(late: Seeded):
    view = metrics.load_run_view(late.conn, late.world.run_id, late.world.cutoff_ms, stale_after_ms=30 * 60_000)
    assert sum(payment.late_authorization for payment in view.payments.values()) >= 4
    assert sum(order["late_authorized"] for order in late.oracle["orders"].values()) >= 4
    stale = [view.payments[pid] for pid in late.world.stale_payment_ids if pid in view.payments]
    assert stale
    assert any(payment.stale for payment in stale)
    snapshots = Counter(o.payment_id for o in storage.list_observations(late.conn, run_id=late.world.run_id, as_of_ms=None)
                        if o.source is ObservationSource.SNAPSHOT)
    assert all(snapshots[pid] <= 1 for pid in late.world.stale_payment_ids)


def test_snapshots_never_reveal_the_future(late: Seeded):
    ledger = late.world.ledger
    for attempt in ledger:
        probes = [attempt.created_at_ms - 1, late.world.cutoff_ms]
        probes += [t.occurred_at_ms + delta for t in attempt.transitions for delta in (-1, 0)]
        for probe in probes:
            snapshot = ledger.snapshot(attempt.payment_id, probe)
            if probe < attempt.created_at_ms:
                assert snapshot is None
                continue
            visible = [t for t in attempt.transitions if t.occurred_at_ms <= probe]
            assert snapshot is not None
            assert snapshot.status is (visible[-1].status if visible else PaymentStatus.CREATED)
    for observation in storage.list_observations(late.conn, run_id=late.world.run_id, as_of_ms=None):
        if observation.source is ObservationSource.SNAPSHOT:
            assert observation.status is ledger.attempt(observation.payment_id).status_at(observation.known_at_ms)


# --------------------------------------------------------------------------- leakage and time discipline


def _observable_text(conn: sqlite3.Connection, run_id: str) -> list[tuple[str, str]]:
    found = []
    for table in storage.TABLES:
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})") if row["type"] in ("TEXT", "BLOB")]
        for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE run_id = ?", (run_id,)):
            for column, value in zip(columns, row, strict=True):
                if value is not None:
                    found.append((f"{table}.{column}", value.decode("utf-8") if isinstance(value, bytes) else str(value)))
    return found


def test_the_database_holds_no_oracle_labels_or_fault_words(backlog: Seeded, late: Seeded, receiver_run: Seeded):
    for seeded in (backlog, late, receiver_run):
        texts = _observable_text(seeded.conn, seeded.world.run_id)
        assert texts
        assert [(where, text[:80]) for where, text in texts if BANNED_WORDS.search(text)] == []
        assert [(where, text[:80]) for where, text in texts if ORACLE_LABELS.search(text)] == []
        tables = {row[0] for row in seeded.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not any("oracle" in name for name in tables)
        oracle = seeded.oracle
        assert all(oracle["oracle_sha256"] not in text for _, text in texts)


def test_the_oracle_records_truth_and_verifiable_hashes(backlog: Seeded):
    oracle = backlog.oracle
    assert (oracle["family"], oracle["expected"]["hypothesis"]) == ("D1", "merchant_worker_regression")
    assert oracle["expected"]["faulting_change_id"] == oracle["faulting_deploy_id"] == backlog.world.faulting_deployment.deploy_id
    claimed = oracle.pop("oracle_sha256")
    assert sha256_hex(canonical_json(oracle)) == claimed
    assert oracle["observable_dataset_sha256"] == observable_dataset_hash(backlog.conn, backlog.world.run_id)
    assert len(oracle["provider_outcomes"]) == len(backlog.world.attempts)
    fulfilled = {f.order_id for f in storage.list_fulfilments(backlog.conn, run_id=backlog.world.run_id)}
    assert {order_id for order_id, o in oracle["orders"].items() if o["expected"] == "already_fulfilled"} <= fulfilled


def test_the_replay_clock_ends_at_the_cutoff_and_nothing_is_future_dated(backlog: Seeded):
    run_id, conn, cutoff = backlog.world.run_id, backlog.conn, backlog.world.cutoff_ms
    assert storage.require_run(conn, run_id).clock_ms == cutoff
    checks = {
        "SELECT MAX(known_at_ms) FROM payment_observations WHERE run_id = ?": cutoff,
        "SELECT MAX(created_at_ms) FROM orders WHERE run_id = ?": cutoff,
        "SELECT MAX(cancelled_at_ms) FROM orders WHERE run_id = ?": cutoff,
        "SELECT MAX(received_at_ms) FROM webhook_inbox WHERE run_id = ?": cutoff,
        "SELECT MAX(known_at_ms) FROM merchant_logs WHERE run_id = ?": cutoff,
        "SELECT MAX(finished_at_ms) FROM worker_attempts WHERE run_id = ?": cutoff,
    }
    for query, limit in checks.items():
        assert conn.execute(query, (run_id,)).fetchone()[0] <= limit, query


def test_scenario_specs_enforce_one_deployment_per_module():
    second_payload_deploy = {**scenarios.WORKER_BACKLOG.model_dump(),
                             "harmless": [{"variant": "payload@4", "at_fraction": 0.7}]}
    with pytest.raises(ValidationError, match="one deployment per module"):
        scenarios.WORKER_BACKLOG.model_validate(second_payload_deploy)


# --------------------------------------------------------------------------- phase 3 generator features

EDGE_SPEC = scenarios.ScenarioSpec(
    name="edge_features", family="D4", variant_label="edge_features", split="unit",
    expected_hypothesis=Hypothesis.AMBIGUOUS_PAYMENT_STATE, expected_operation=Operation.REFRESH_PROVIDER_STATE,
    duration_minutes=150, orders_per_hour=60, method_mix={"upi": 0.5, "card": 0.5},
    authorization_rate={"upi": 0.95, "card": 0.95},
    contradiction=scenarios.Episode(start_fraction=0.2, end_fraction=0.45, rate=1.0),
    abandonment=scenarios.Episode(start_fraction=0.5, end_fraction=0.65, rate=1.0),
    delivery_delay=scenarios.DeliveryDelay(start_fraction=0.7, end_fraction=1.0, delay_minutes=(20, 30)),
    reconciler_pause=scenarios.Window(start_fraction=0.7, end_fraction=1.0),
)


@pytest.fixture(scope="module")
def edge(tmp_path_factory: pytest.TempPathFactory):
    seeded = seed(tmp_path_factory.mktemp("edge"), EDGE_SPEC, 21)
    yield seeded
    seeded.conn.close()


def _fraction_ms(world: World, fraction: float) -> int:
    return world.start_ms + int(fraction * (world.cutoff_ms - world.start_ms))


def test_contradictory_reports_hold_the_orders_for_review(edge: Seeded):
    contradicted = {o.order_id for o in edge.world.orders if o.kind is OrderKind.CONTRADICTED}
    assert len(contradicted) >= 10
    view = metrics.load_run_view(edge.conn, edge.world.run_id, edge.world.cutoff_ms)
    for order_id in contradicted:
        assert ExclusionReason.CONFLICTING_EVIDENCE in view.order_projections[order_id].exclusions
        assert edge.oracle["orders"][order_id]["expected"] == "conflicting_evidence"
    held = [a for a in storage.list_worker_attempts(edge.conn, run_id=edge.world.run_id) if a.result is WorkerResult.MANUAL_REVIEW]
    assert len(held) >= len(contradicted) // 2


def test_abandoned_orders_have_no_payment_attempt(edge: Seeded):
    abandoned = {o.order_id for o in edge.world.orders if o.kind is OrderKind.ABANDONED}
    assert abandoned
    attempts = storage.list_run_payment_attempts(edge.conn, run_id=edge.world.run_id, as_of_ms=None)
    assert not abandoned & {a.order_id for a in attempts}
    assert {edge.oracle["orders"][order_id]["expected"] for order_id in abandoned} == {"not_captured"}


def test_late_deliveries_and_a_paused_reconciler_leave_an_observation_gap(edge: Seeded):
    paused_from = _fraction_ms(edge.world, 0.7)
    delayed = [d for d in edge.world.deliveries if d.copy == "original" and d.send_at_ms >= paused_from + 20 * 60_000]
    assert delayed
    snapshots = [o for o in storage.list_observations(edge.conn, run_id=edge.world.run_id, as_of_ms=None)
                 if o.source is ObservationSource.SNAPSHOT]
    assert snapshots
    assert all(o.known_at_ms < paused_from for o in snapshots)


def test_seeding_writes_unlabelled_provider_records_and_the_eligibility_policy(edge: Seeded):
    records = Path(str(edge.summary.provider_records_path))
    assert records.parent == edge.data_dir / "provider"
    assert '"kind"' not in records.read_text(encoding="utf-8")
    eligibility = edge.oracle["eligibility"]
    assert eligibility["as_of_ms"] == edge.world.cutoff_ms
    assert eligibility["late_authorization_blocks_reprocessing"] is False  # owner decision Q8
    assert eligibility["stale_after_ms"] > 0
    assert storage.require_run(edge.conn, edge.world.run_id).generator_version == driver.GENERATOR_VERSION
