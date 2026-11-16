"""Storage: pragmas, migrations, transactions, constraints and run scope."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import (
    AMOUNT,
    INTEGRATION_A,
    MERCHANT_B,
    ORDER_1,
    ORDER_2,
    PAY_1,
    PAY_2,
    RUN_A,
    START_MS,
    FailingControlConnection,
    FakeHandler,
    add_order,
    count_rows,
    deliver,
    observation,
    store_observations,
)
from recovery_workbench import storage, worker
from recovery_workbench.contracts import (
    ActionStatus,
    ContractError,
    DiagnosisStatus,
    EvidenceKind,
    InboxStatus,
    ModelMode,
    ObservationSource,
    Operation,
    PaymentStatus,
    PlanStatus,
    ReceiveOutcome,
    ScopeError,
    WorkerResult,
)

DIGEST = "a" * 64


def test_connect_applies_pragmas_to_a_file_database(db_path: Path):
    with closing(storage.connect(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == storage.BUSY_TIMEOUT_MS  # set by connect(timeout=)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert conn.isolation_level is None


def test_connect_supports_an_in_memory_database():
    with closing(storage.connect(":memory:")) as conn:
        assert storage.migrate(conn) == tuple(migration.version for migration in storage.MIGRATIONS)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == storage.BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert set(storage.TABLES) <= names


def test_migrate_is_idempotent_and_recorded(conn: sqlite3.Connection):
    assert storage.migrate(conn) == ()
    rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [tuple(row) for row in rows] == [(migration.version, migration.name) for migration in storage.MIGRATIONS]
    for table in storage.TABLES:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert "run_id" in columns, table


def test_concurrent_migrations_apply_each_version_once(db_path: Path):
    def migrate_once(_: int) -> tuple[int, ...]:
        with closing(storage.connect(db_path)) as own:
            return storage.migrate(own)

    with ThreadPoolExecutor(max_workers=8) as pool:
        applied = list(pool.map(migrate_once, range(8)))
    assert sorted(version for versions in applied for version in versions) == [m.version for m in storage.MIGRATIONS]


def _has_migration_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'").fetchone() is not None


@pytest.mark.parametrize("versions", [(1, 1), (2, 1), (1, 3, 2)])
def test_a_migration_list_whose_versions_repeat_or_go_backwards_is_refused_before_anything_is_applied(
    db_path: Path, versions: tuple[int, ...]
):
    # schema_migrations keys on the version alone, so a reused number would be taken as applied and skipped.
    probes = [storage.Migration(version, f"probe_{position}", ()) for position, version in enumerate(versions)]
    with closing(storage.connect(db_path)) as conn:
        with pytest.raises(storage.StorageError, match="unique and strictly increasing"):
            storage._apply_migrations(conn, probes)
        assert not _has_migration_table(conn)


def test_the_simulator_track_cannot_reuse_an_application_version(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Both tracks are recorded in the one schema_migrations table, so they are checked as one list.
    reused = storage.MIGRATIONS[-1].version
    monkeypatch.setattr(storage, "SIMULATOR_MIGRATIONS", (storage.Migration(reused, "simulator_probe", ()),))
    with closing(storage.connect(db_path)) as conn:
        with pytest.raises(storage.StorageError, match="unique and strictly increasing"):
            storage.migrate_simulator(conn)
        assert not _has_migration_table(conn)


def test_the_body_indexes_are_built_again_without_error_after_half_a_manual_revert(db_path: Path):
    """The revert's DELETE ran and its DROP INDEX did not: the versions are gone, the indexes are still there."""
    with closing(storage.connect(db_path)) as conn:
        storage.migrate(conn)
        with storage.write_transaction(conn):
            conn.execute("DELETE FROM schema_migrations WHERE version IN (4, 5)")
    with closing(storage.connect(db_path)) as conn:  # the application opens it again
        assert storage.migrate(conn) == (4, 5)
        rows = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        assert [tuple(row) for row in rows] == [(migration.version, migration.name) for migration in storage.MIGRATIONS]
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"webhook_inbox_by_body", "conflicting_deliveries_by_body"} <= indexes


# --------------------------------------------------------------------------- the WAL switch under contention


@dataclass
class FakeClock:
    """A monotonic clock that moves only when the code under test sleeps, so a retry costs no real time."""

    now: float = 1_000.0
    pauses: list[float] = field(default_factory=list)
    on_pause: Callable[[], None] = lambda: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds
        self.on_pause()


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(storage, "_monotonic", clock.monotonic)
    monkeypatch.setattr(storage, "_sleep", clock.sleep)
    return clock


@contextmanager
def write_lock_on_a_rollback_mode_file(path: Path) -> Iterator[sqlite3.Connection]:
    """Another connection holding the write lock of a file still in rollback mode, as while it opens a new database.

    SQLite then refuses the WAL switch at once with SQLITE_BUSY, without waiting out the busy timeout.
    """
    with closing(sqlite3.connect(path, isolation_level=None)) as holder:
        holder.execute("CREATE TABLE opened_elsewhere (x INTEGER)")
        holder.execute("BEGIN IMMEDIATE")
        yield holder


class ScriptedWalSwitch:
    """Stands in for a connection whose WAL switch raises each of ``failures`` in turn, then succeeds."""

    def __init__(self, *failures: sqlite3.Error) -> None:
        self.failures = list(failures)
        self.attempts = 0

    def execute(self, sql: str) -> ScriptedWalSwitch:
        assert sql == "PRAGMA journal_mode = WAL"
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return self

    def fetchone(self) -> tuple[str]:
        return ("wal",)


def sqlite_error(message: str, code: int | None) -> sqlite3.OperationalError:
    """An OperationalError as the sqlite3 module raises it, SQLite's extended result code in ``sqlite_errorcode``."""
    error = sqlite3.OperationalError(message)
    if code is not None:
        error.sqlite_errorcode = code  # type: ignore[attr-defined]
    return error


def test_connect_retries_the_wal_switch_until_the_other_connection_lets_go(db_path: Path, fake_clock: FakeClock):
    with write_lock_on_a_rollback_mode_file(db_path) as holder:
        def release_the_lock() -> None:
            holder.execute("COMMIT")

        fake_clock.on_pause = release_the_lock
        with closing(storage.connect(db_path)) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert fake_clock.pauses == [storage._WAL_SWITCH_PAUSE_S]  # refused once, while the lock was held


def test_connect_gives_up_with_a_storage_error_once_the_busy_budget_is_spent(db_path: Path, fake_clock: FakeClock):
    with write_lock_on_a_rollback_mode_file(db_path) as holder:
        with pytest.raises(storage.StorageError) as raised:
            storage.connect(db_path)
        assert holder.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # the file was left as it was
    message = str(raised.value)
    assert str(db_path) in message  # which file
    assert f"other connections held the lock it needs for the whole {storage.BUSY_TIMEOUT_MS} ms budget" in message
    assert "Retry once the other processes using this database have finished opening it" in message  # what to do
    refusal = raised.value.__cause__
    assert isinstance(refusal, sqlite3.OperationalError)
    assert refusal.sqlite_errorcode == sqlite3.SQLITE_BUSY  # SQLite's own refusal, kept as the cause
    assert sum(fake_clock.pauses) == pytest.approx(storage.BUSY_TIMEOUT_MS / 1000)
    assert max(fake_clock.pauses) <= storage._WAL_SWITCH_PAUSE_S


def test_connect_raises_a_file_that_is_not_a_database_unchanged_and_without_retrying(
    db_path: Path, fake_clock: FakeClock,
):
    db_path.write_bytes(b"not an SQLite database " * 64)

    with pytest.raises(sqlite3.DatabaseError, match="file is not a database") as raised:
        storage.connect(db_path)

    assert type(raised.value) is sqlite3.DatabaseError
    assert raised.value.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    assert fake_clock.pauses == []


@pytest.mark.parametrize("refusal", [
    sqlite_error("database is locked", sqlite3.SQLITE_BUSY_SNAPSHOT),  # an extended busy code
    sqlite_error("database table is locked", sqlite3.SQLITE_LOCKED),
])
def test_the_wal_switch_retries_busy_and_locked_refusals(refusal: sqlite3.OperationalError, fake_clock: FakeClock):
    switch = ScriptedWalSwitch(refusal)

    assert storage._switch_to_wal(switch, "scripted.sqlite") == "wal"  # type: ignore[arg-type]
    assert (switch.attempts, fake_clock.pauses) == (2, [storage._WAL_SWITCH_PAUSE_S])


@pytest.mark.parametrize("failure", [
    sqlite_error("disk I/O error", sqlite3.SQLITE_IOERR),
    sqlite_error("database is locked", None),  # no SQLite code: the code decides, never the message
])
def test_the_wal_switch_reraises_any_other_error_unchanged(failure: sqlite3.OperationalError, fake_clock: FakeClock):
    switch = ScriptedWalSwitch(failure)

    with pytest.raises(sqlite3.OperationalError) as raised:
        storage._switch_to_wal(switch, "scripted.sqlite")  # type: ignore[arg-type]

    assert raised.value is failure
    assert (switch.attempts, fake_clock.pauses) == (1, [])


@pytest.mark.parametrize(
    ("table", "column", "enum"),
    [
        ("runs", "model_mode", ModelMode),
        ("payment_observations", "status", PaymentStatus),
        ("payment_observations", "source", ObservationSource),
        ("webhook_inbox", "processing_status", InboxStatus),
        ("delivery_attempts", "outcome", ReceiveOutcome),
        ("worker_attempts", "result", WorkerResult),
        ("evidence", "kind", EvidenceKind),
        ("diagnoses", "mode", ModelMode),
        ("diagnoses", "status", DiagnosisStatus),
        ("plans", "operation", Operation),
        ("plans", "status", PlanStatus),
        ("actions", "status", ActionStatus),
    ],
)
def test_check_constraints_list_exactly_the_contract_enum_values(
    conn: sqlite3.Connection, table: str, column: str, enum: type[StrEnum]
):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    match = re.search(rf"CHECK \({column} IN \(([^)]*)\)\)", sql)
    assert match is not None, f"{table}.{column} has no IN check"
    allowed = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert allowed == {member.value for member in enum}


def test_every_guide_table_accepts_a_valid_row(conn: sqlite3.Connection, run_a):
    """Exercises every FK and CHECK definition, including the tables phase 2 will use."""
    add_order(conn, run_a)
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    deliver(conn, run_a, "evt_SYNTHETIC_0001", amount_paise=AMOUNT + 1)  # held as a conflicting delivery
    worker.process_next(conn, run_id=RUN_A, handler=FakeHandler(failure=KeyError("payment")), now_ms=START_MS + 5_000)
    with storage.write_transaction(conn):
        storage.insert_deployment(conn, storage.DeploymentRecord(
            run_id=RUN_A, deploy_id="deploy_SYNTHETIC_0001", service_id="merchant_worker", kind="code",
            deployed_at_ms=START_MS, known_at_ms=START_MS, summary="Read nested payment fields",
            files_changed=("merchant_app/payload.py",), diff_text="- old\n+ new",
        ))
        storage.insert_merchant_log(conn, storage.MerchantLogRecord(
            run_id=RUN_A, log_id="log_1", service_id="merchant_worker", level="error", message="worker retrying",
            occurred_at_ms=START_MS, known_at_ms=START_MS,
        ))
        conn.execute("INSERT INTO evidence VALUES (?, 'ev_1', 'worker_error', 'worker_attempts', '1', ?, ?, '{}')",
                     (RUN_A, START_MS, DIGEST))
        conn.execute(
            "INSERT INTO incidents (run_id, incident_id, \"trigger\", cohort, window_start_ms, window_end_ms,"
            " evidence_cutoff_ms, state, measured_impact) VALUES (?, 'inc_1', 'processing_gap', '{}', ?, ?, ?, 'open', '{}')",
            (RUN_A, START_MS, START_MS + 1, START_MS + 1),
        )
        conn.execute("INSERT INTO diagnoses (run_id, diagnosis_id, incident_id, mode, packet_hash, status,"
                     " created_wall_ms) VALUES (?, 'diag_1', 'inc_1', 'rules', ?, 'validated', 0)", (RUN_A, DIGEST))
        conn.execute("INSERT INTO fix_validations VALUES (?, 'val_1', 'diag_1', 'payload@5', 'fixture_capture',"
                     " '{}', '{}', 1, ?, 'operator_1')", (RUN_A, START_MS))
        conn.execute("INSERT INTO plans VALUES (?, 'plan_1', 'inc_1', 'diag_1', 'val_1', 'reprocess_inbox',"
                     " '[\"order_SYNTHETIC_0001\"]', ?, 'ev1', 'policy-v1', ?, ?, 'approved', ?)",
                     (RUN_A, AMOUNT, DIGEST, START_MS + 60_000, START_MS))
        conn.execute("INSERT INTO approvals VALUES (?, 'appr_1', 'plan_1', ?, 'operator_1', ?, ?)",
                     (RUN_A, DIGEST, START_MS, START_MS + 60_000))
        conn.execute("INSERT INTO actions (run_id, action_id, operation_key, plan_id, status, \"before\", \"after\","
                     " executed_at_ms) VALUES (?, 'act_1', 'op_1', 'plan_1', 'succeeded', '{}', '{}', ?)",
                     (RUN_A, START_MS + 1))
        storage.insert_fulfilment(conn, storage.FulfilmentRecord(
            run_id=RUN_A, order_id=ORDER_1, payment_id=PAY_1, amount_paise=AMOUNT, currency="INR",
            fulfilled_at_ms=START_MS + 2, via="reprocess", action_id="act_1",
        ))
        storage.insert_plan_audit(conn, storage.PlanAuditRecord(
            run_id=RUN_A, plan_id="plan_1", deployed_version="deploy_SYNTHETIC_0001", corrected_version="payload@5",
            stale_after_ms=60_000, candidates=(), dry_runs=(), superseded_inbox_ids={},
        ))
        storage.insert_action_audit(conn, storage.ActionAuditRecord(
            run_id=RUN_A, action_id="act_1", application_version="0.1.0", stale_after_ms=60_000, checks=(),
            evidence={}, orders=(), postconditions={}, amounts={},
        ))
        storage.insert_provider_delivery_attempt(
            conn, run_id=RUN_A, integration_id=INTEGRATION_A, provider_attempt_id="evt_SYNTHETIC_0001:1",
            event_id="evt_SYNTHETIC_0001", event_type="payment.captured", payment_id=PAY_1, attempted_at_ms=START_MS,
            provider_http_status=None, triggered_by="automatic", known_at_ms=START_MS + 3, source_ref="delivery_log_1",
        )
    for table in storage.TABLES:
        assert count_rows(conn, table, RUN_A) >= 1, table
    # An approval binds to the plan's hash: any other hash has no matching plan row.
    with pytest.raises(sqlite3.IntegrityError), storage.write_transaction(conn):
        conn.execute("INSERT INTO approvals VALUES (?, 'appr_2', 'plan_1', ?, 'operator_1', ?, ?)",
                     (RUN_A, "b" * 64, START_MS, START_MS + 60_000))


def test_write_transaction_commits_on_success_and_rolls_back_on_error(conn, run_a, open_connection):
    add_order_row = storage.OrderRecord(
        run_id=RUN_A, order_id=ORDER_1, merchant_id=run_a.merchant_id, created_at_ms=START_MS,
        amount_paise=AMOUNT, currency="INR",
    )
    abandon = RuntimeError("abandon")
    with pytest.raises(RuntimeError, match="abandon"), storage.write_transaction(conn):
        storage.insert_order(conn, add_order_row)
        raise abandon
    assert not conn.in_transaction
    other = open_connection()
    assert storage.get_order(other, run_id=RUN_A, order_id=ORDER_1) is None
    add_order(conn, run_a)
    assert storage.get_order(other, run_id=RUN_A, order_id=ORDER_1) is not None


def test_a_real_commit_failure_rolls_back_and_raises(conn, run_a):
    """A deferred foreign-key violation makes SQLite's COMMIT itself fail."""

    def insert_an_orphan_attempt() -> None:
        with storage.write_transaction(conn):
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute("INSERT INTO payment_attempts VALUES (?, 'pay_orphan', 'order_missing', 'upi', ?)",
                         (RUN_A, START_MS))

    with pytest.raises(sqlite3.IntegrityError):
        insert_an_orphan_attempt()  # fails at COMMIT, when the deferred check runs
    assert not conn.in_transaction
    assert count_rows(conn, "payment_attempts", RUN_A) == 0


def test_a_rollback_failure_surfaces_both_errors_and_commits_nothing(conn, run_a, open_connection):
    failing = FailingControlConnection(conn, fail_on="ROLLBACK")
    add_order_row = storage.OrderRecord(
        run_id=RUN_A, order_id=ORDER_1, merchant_id=run_a.merchant_id, created_at_ms=START_MS,
        amount_paise=AMOUNT, currency="INR",
    )
    body_failure = ValueError("body failed")
    with (pytest.raises(storage.StorageError, match="ROLLBACK failed after ValueError") as raised,
          storage.write_transaction(failing)):  # type: ignore[arg-type]
        storage.insert_order(failing, add_order_row)  # type: ignore[arg-type]
        raise body_failure
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert isinstance(raised.value.__cause__.__context__, ValueError)
    other = open_connection()
    assert storage.get_order(other, run_id=RUN_A, order_id=ORDER_1) is None
    conn.close()  # closing discards the transaction the failed ROLLBACK left open
    add_order(other, run_a)
    assert storage.get_order(other, run_id=RUN_A, order_id=ORDER_1) is not None


@pytest.mark.parametrize("transaction", [storage.write_transaction, storage.read_transaction])
def test_a_transaction_ends_on_normal_exit_and_on_error(conn, transaction):
    with transaction(conn):
        assert conn.in_transaction
    assert not conn.in_transaction  # committed
    failure = ValueError("body failed")
    with pytest.raises(ValueError, match="body failed"), transaction(conn):
        raise failure
    assert not conn.in_transaction  # rolled back


def test_a_consumer_that_stops_early_rolls_back_the_write_it_was_inside(conn, run_a, open_connection):
    """Closing a generator suspended inside the block raises GeneratorExit there: a rollback, never a commit."""
    order = storage.OrderRecord(run_id=RUN_A, order_id=ORDER_1, merchant_id=run_a.merchant_id, created_at_ms=START_MS,
                                amount_paise=AMOUNT, currency="INR")

    def insert_then_wait():
        with storage.write_transaction(conn):
            storage.insert_order(conn, order)
            yield

    consumer = insert_then_wait()
    next(consumer)
    assert conn.in_transaction
    consumer.close()
    assert not conn.in_transaction
    assert storage.get_order(open_connection(), run_id=RUN_A, order_id=ORDER_1) is None


def test_a_consumer_that_stops_early_ends_the_read_snapshot_it_was_inside(conn, run_a):
    def read_then_wait():
        with storage.read_transaction(conn):
            yield storage.require_run(conn, RUN_A)

    consumer = read_then_wait()
    next(consumer)
    assert conn.in_transaction
    consumer.close()
    assert not conn.in_transaction


@pytest.mark.parametrize("transaction", [storage.write_transaction, storage.read_transaction])
def test_an_entered_transaction_closed_without_exit_rolls_back(conn, transaction):
    """What garbage collection does to an entered manager that is never exited: GeneratorExit at its own yield."""
    manager = transaction(conn)
    manager.__enter__()
    assert conn.in_transaction
    manager.gen.close()  # type: ignore[attr-defined]
    assert not conn.in_transaction


def test_write_helpers_refuse_to_run_outside_a_transaction(conn, run_a):
    order = storage.OrderRecord(
        run_id=RUN_A, order_id=ORDER_1, merchant_id=run_a.merchant_id, created_at_ms=START_MS, amount_paise=AMOUNT,
        currency="INR",
    )
    with pytest.raises(storage.StorageError, match="write_transaction"):
        storage.insert_order(conn, order)
    with pytest.raises(storage.StorageError, match="write_transaction"):
        storage.advance_clock(conn, RUN_A, START_MS + 1)


def test_run_lookup_enforces_existence_and_merchant(conn, run_a):
    assert storage.require_run(conn, RUN_A).merchant_id == run_a.merchant_id
    assert storage.get_run(conn, "run_missing") is None
    with pytest.raises(ScopeError):
        storage.require_run(conn, "run_missing")
    with pytest.raises(ScopeError):
        storage.require_run(conn, RUN_A, merchant_id=MERCHANT_B)


def test_replay_clock_only_moves_forward(conn, run_a):
    with storage.write_transaction(conn):
        assert storage.advance_clock(conn, RUN_A, START_MS + 10) == START_MS + 10
        assert storage.advance_clock(conn, RUN_A, START_MS + 10) == START_MS + 10
    with pytest.raises(ContractError, match="refusing to move it back"), storage.write_transaction(conn):
        storage.advance_clock(conn, RUN_A, START_MS + 9)
    with pytest.raises(ContractError), storage.write_transaction(conn):
        storage.advance_clock(conn, RUN_A, True)  # type: ignore[arg-type]
    with pytest.raises(ScopeError), storage.write_transaction(conn):
        storage.advance_clock(conn, "run_missing", START_MS)
    with pytest.raises(sqlite3.IntegrityError, match="backwards"), storage.write_transaction(conn):
        conn.execute("UPDATE runs SET clock_ms = ? WHERE run_id = ?", (START_MS, RUN_A))
    assert storage.require_run(conn, RUN_A).clock_ms == START_MS + 10


def test_runs_are_synthetic_only(conn):
    with pytest.raises(ValidationError):
        storage.RunRecord(run_id="run_live", seed=1, scenario="x", split="unit", merchant_id="m", environment="live",
                          generator_version="v", clock_ms=0, model_mode=ModelMode.RULES)  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError), storage.write_transaction(conn):
        conn.execute("INSERT INTO runs VALUES ('run_live', 1, 'x', 'unit', 'm', 'live', 'v', 0, 'rules', 0)")


def test_money_is_positive_integer_paise(conn, run_a):
    for bad_amount in (0, -5, "12000", 1200.0):
        with pytest.raises(ValidationError):
            storage.OrderRecord(run_id=RUN_A, order_id=ORDER_1, merchant_id=run_a.merchant_id, created_at_ms=START_MS,
                                amount_paise=bad_amount, currency="INR")  # type: ignore[arg-type]
    for bad_amount in (0, 1200.5):
        with pytest.raises(sqlite3.IntegrityError), storage.write_transaction(conn):
            conn.execute("INSERT INTO orders (run_id, order_id, merchant_id, created_at_ms, amount_paise, currency,"
                         " allowed_methods) VALUES (?, 'o_bad', ?, 0, ?, 'INR', '[]')",
                         (RUN_A, run_a.merchant_id, bad_amount))


def test_records_are_scoped_to_their_run_and_merchant(conn, run_a, run_b):
    add_order(conn, run_a)
    store_observations(conn, [observation(PaymentStatus.CAPTURED, START_MS, START_MS)])
    assert storage.get_order(conn, run_id=run_b.run_id, order_id=ORDER_1) is None
    with pytest.raises(ScopeError):
        storage.require_order(conn, run_id=run_b.run_id, order_id=ORDER_1)
    assert storage.list_observations(conn, run_id=run_b.run_id, as_of_ms=None) == ()
    other_merchant_order = storage.OrderRecord(
        run_id=RUN_A, order_id="order_other_merchant", merchant_id=MERCHANT_B, created_at_ms=START_MS,
        amount_paise=AMOUNT, currency="INR",
    )
    with pytest.raises(ScopeError), storage.write_transaction(conn):
        storage.insert_order(conn, other_merchant_order)
    cross_run_attempt = storage.PaymentAttemptRecord(
        run_id=run_b.run_id, payment_id="pay_cross", order_id=ORDER_1, method="upi", created_at_ms=START_MS
    )
    with pytest.raises(ScopeError), storage.write_transaction(conn):
        storage.insert_payment_attempt(conn, cross_run_attempt)
    with pytest.raises(ScopeError), storage.write_transaction(conn):
        storage.mark_order_fulfilled(conn, run_id=run_b.run_id, order_id=ORDER_1)


def test_observations_are_append_only(conn, run_a):
    store_observations(conn, [observation(PaymentStatus.CAPTURED, START_MS, START_MS)])
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), storage.write_transaction(conn):
        conn.execute("UPDATE payment_observations SET status = 'failed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), storage.write_transaction(conn):
        conn.execute("DELETE FROM payment_observations")


def test_observation_queries_apply_the_as_of_cutoff(conn, run_a):
    store_observations(conn, [
        observation(PaymentStatus.CREATED, START_MS, START_MS + 1),
        observation(PaymentStatus.CAPTURED, START_MS + 5, START_MS + 50),  # known late
        observation(PaymentStatus.FAILED, START_MS + 40, START_MS + 30),  # claims a future occurrence at T=30
    ])
    visible = storage.list_observations(conn, run_id=RUN_A, as_of_ms=START_MS + 30, order_id=ORDER_1)
    assert [item.status for item in visible] == [PaymentStatus.CREATED]
    everything = storage.list_observations(conn, run_id=RUN_A, as_of_ms=None, payment_ids=[PAY_1])
    assert len(everything) == 3


def test_one_final_fulfilment_per_order(conn, run_a):
    add_order(conn, run_a)
    fulfilment = storage.FulfilmentRecord(run_id=RUN_A, order_id=ORDER_1, payment_id=PAY_1, amount_paise=AMOUNT,
                                          currency="INR", fulfilled_at_ms=START_MS, via="worker")
    with storage.write_transaction(conn):
        storage.insert_fulfilment(conn, fulfilment)
        storage.mark_order_fulfilled(conn, run_id=RUN_A, order_id=ORDER_1)
    second_payment = fulfilment.model_copy(update={"payment_id": "pay_other"})
    with pytest.raises(sqlite3.IntegrityError), storage.write_transaction(conn):
        storage.insert_fulfilment(conn, second_payment)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), storage.write_transaction(conn):
        conn.execute("DELETE FROM fulfilments")
    with pytest.raises(sqlite3.IntegrityError, match="cannot become unfulfilled"), storage.write_transaction(conn):
        conn.execute("UPDATE orders SET fulfilment_state = 'unfulfilled'")
    with pytest.raises(sqlite3.IntegrityError), storage.write_transaction(conn):  # reprocess needs an action
        conn.execute("INSERT INTO fulfilments VALUES (?, 'o2', 'p2', 1, 'INR', 0, 'reprocess', NULL)", (RUN_A,))


def test_conflicting_deliveries_match_either_side_of_the_conflict(conn, run_a):
    deliver(conn, run_a, "evt_SYNTHETIC_0001")  # the original names PAY_1 and ORDER_1
    held_id = deliver(conn, run_a, "evt_SYNTHETIC_0001", payment_id=PAY_2, order_id=ORDER_2,
                      received_at_ms=START_MS + 3_000).conflicting_delivery_id

    def listed(as_of_ms: int | None) -> list[tuple[str, str, str, str]]:
        return [(c.payment_id, c.order_id, c.original_payment_id, c.original_order_id)
                for c in storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=as_of_ms)]

    def touching(order_id: str, payment_ids: list[str], as_of_ms: int = START_MS + 3_000) -> list[int]:
        return list(storage.conflicting_delivery_ids(conn, run_id=RUN_A, as_of_ms=as_of_ms, order_id=order_id,
                                                     payment_ids=payment_ids))

    held = [(PAY_2, ORDER_2, PAY_1, ORDER_1)]
    assert listed(None) == held
    assert listed(START_MS + 2_999) == []
    assert listed(START_MS + 3_000) == held
    assert touching(ORDER_1, []) == [held_id]  # the original's order
    assert touching(ORDER_2, []) == [held_id]  # the reused-ID body's order
    assert touching("order_other", [PAY_1]) == [held_id]  # the original's payment
    assert touching("order_other", ["pay_other", PAY_2]) == [held_id]  # either subject is enough
    assert touching(ORDER_1, [PAY_1, PAY_2]) == [held_id]  # matched on every side, listed once
    assert touching("order_other", []) == []
    assert touching("order_other", ["pay_other"]) == []
    assert touching(ORDER_1, [], as_of_ms=START_MS + 2_999) == []


def test_deployments_and_logs_respect_the_cutoff(conn, run_a):
    with storage.write_transaction(conn):
        for number, known_at in ((1, START_MS), (2, START_MS + 100)):
            storage.insert_deployment(conn, storage.DeploymentRecord(
                run_id=RUN_A, deploy_id=f"deploy_{number}", service_id="merchant_worker", kind="code",
                deployed_at_ms=START_MS, known_at_ms=known_at, summary="Tidy parsing", files_changed=("a.py",),
                diff_text="+x",
            ))
            storage.insert_merchant_log(conn, storage.MerchantLogRecord(
                run_id=RUN_A, log_id=f"log_{number}", service_id="merchant_worker", level="info", message="ok",
                occurred_at_ms=START_MS, known_at_ms=known_at,
            ))
    assert [d.deploy_id for d in storage.list_deployments(conn, run_id=RUN_A, as_of_ms=START_MS + 50)] == ["deploy_1"]
    assert [log.log_id for log in storage.list_merchant_logs(conn, run_id=RUN_A, as_of_ms=START_MS + 50)] == ["log_1"]
    assert len(storage.list_deployments(conn, run_id=RUN_A, as_of_ms=START_MS + 100)) == 2


def test_migration_two_adds_the_retry_schedule_to_inbox_rows(conn, run_a):
    # 3: the receipt audits (phase 11); 4 and 5: the indexes for the receiver's body lookups (Q32 option a, Q34);
    # 6: the provider's delivery log (Q29 (b)); 7: the attempt-number and conflict-subject indexes (Q40);
    # 8: the hosted model mode, a rebuild of runs and diagnoses (Q46)
    assert [migration.version for migration in storage.MIGRATIONS] == [1, 2, 3, 4, 5, 6, 7, 8]
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(webhook_inbox)")}
    assert "next_attempt_at_ms" in columns
    deliver(conn, run_a, "evt_SYNTHETIC_0001")
    [row] = storage.list_inbox_rows(conn, run_id=RUN_A)
    assert row.next_attempt_at_ms is None
    with pytest.raises(sqlite3.IntegrityError), storage.write_transaction(conn):
        conn.execute("UPDATE webhook_inbox SET next_attempt_at_ms = -1 WHERE run_id = ?", (RUN_A,))
