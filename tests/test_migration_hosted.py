"""Migration 8 (Q46): hosted joins the mode lists of runs.model_mode and diagnoses.mode, by a rebuild of both tables.

The database is made as the unchanged code made it: migrations 1 to 7 and the simulator's two, the exact list before
this change, then a seeded world taken through the CLI demo's recovery and a diagnosis in every other mode, so every
table that references a run or a diagnosis holds rows. After the rebuild every row, index, trigger and foreign key is
as it was, every report and receipt is byte for byte as it was, and the new mode can be stored, only with its host.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import INTEGRATION_A, MERCHANT_A, RUN_A, SECRET_A, seed_run
from test_diagnose_incident import FakeModel

from recovery_workbench import cli, diagnosis_store, packet, report, selective, settings, storage
from recovery_workbench.contracts import DiagnosisStatus, ModelMode
from recovery_workbench.diagnosis import ModelCallError
from recovery_workbench.simulator import runtime

SEED = 20821  # the model chain's seeds are 20801-20900
EXPORTED_AT_MS = 1_794_700_000_000
BEFORE = (1, 2, 3, 4, 5, 6, 7, 1001, 1002)  # what the unchanged code applies to a data directory
REBUILT = ("runs", "diagnoses")
MODES_BEFORE = "('local', 'recorded', 'rules')"
MODES_AFTER = "('local', 'recorded', 'rules', 'hosted')"
HOST_CHECK = "CHECK ((mode = 'hosted') = (json_extract(output, '$.provider_host') IS NOT NULL))"


def snapshot(conn: sqlite3.Connection) -> dict[str, object]:
    """Everything a rebuild could lose: every row of every table, and every index, trigger and foreign key."""
    tables = [str(name) for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    schema = {(kind, name): sql for kind, name, sql in conn.execute("SELECT type, name, sql FROM sqlite_master")}
    return {
        "rows": {table: sorted(repr(tuple(row)) for row in conn.execute(f'SELECT * FROM "{table}"'))  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query -- names from sqlite_master
                 for table in tables if table != "schema_migrations"},
        "indexes": {name: sql for (kind, name), sql in schema.items() if kind == "index"},
        "triggers": {name: sql for (kind, name), sql in schema.items() if kind == "trigger"},
        "foreign_keys": {table: [tuple(row) for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')]
                         for table in tables},
        "tables": {name: sql for (kind, name), sql in schema.items() if kind == "table"},
    }


def versions(conn: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version"))


def exported(conn: sqlite3.Connection, run_id: str, action_id: str) -> dict[str, bytes]:
    """The run's report, as the CLI writes it at a fixed export time, and the action's receipt in both forms."""
    with pytest.MonkeyPatch.context() as frozen:
        frozen.setattr(storage, "wall_clock_ms", lambda: EXPORTED_AT_MS)
        written = json.dumps(report.build_report(conn, run_id=run_id), indent=2, sort_keys=True).encode()
    receipt = report.build_receipt(conn, run_id=run_id, action_id=action_id)
    return {"report": written, "receipt": report.receipt_bytes(receipt),
            "markdown": report.render_receipt_markdown(receipt).encode()}


@dataclass(frozen=True)
class OldDatabase:
    path: Path
    run_id: str
    action_id: str
    before: dict[str, object]
    exports: dict[str, bytes]


def old_database(directory: Path) -> OldDatabase:
    """A data directory as the unchanged code left it, with rows in every table that references runs or diagnoses."""
    data_dir = directory / "data"
    data_dir.mkdir()  # as run_demo does before it calls demo
    with pytest.MonkeyPatch.context() as unchanged:
        unchanged.setattr(storage, "MIGRATIONS", storage.MIGRATIONS[:7])  # the application list before this change
        assert cli.demo(scenario="worker_backlog", seed=SEED, mode=ModelMode.RULES, data_dir=data_dir,
                        recordings_dir=directory / "recordings", write=lambda _: None) == 0
        with closing(storage.connect(data_dir / cli.DATABASE_FILE)) as conn:
            assert versions(conn) == BEFORE
            [run_id] = [str(row[0]) for row in conn.execute("SELECT run_id FROM runs")]
            [rules] = diagnosis_store.list_diagnoses(conn, run_id=run_id)
            clock = storage.require_run(conn, run_id).clock_ms
            deployed = runtime.active_merchant(conn, run_id=run_id, at_ms=clock)
            configured = settings.load_settings({"WORKBENCH_MODEL": FakeModel.model})
            answer = selective.rules_v3(packet.build_packet(conn, run_id=run_id, incident_id=rules.incident_id,
                                                            cutoff_ms=clock, deployed=deployed, persist=False))
            stored = [diagnosis_store.diagnose_incident(conn, run_id=run_id, incident_id=rules.incident_id, mode=mode,
                                                        settings=configured, deployed=deployed, client=client)
                      for mode, client in ((ModelMode.LOCAL, FakeModel(answer.model_dump(mode="json"))),
                                           (ModelMode.RECORDED, None),
                                           (ModelMode.LOCAL, FakeModel(digest_error=ModelCallError("refused"))))]
            assert [(item.mode, item.status) for item in stored] == [
                (ModelMode.LOCAL, DiagnosisStatus.VALIDATED), (ModelMode.RECORDED, DiagnosisStatus.VALIDATED),
                (ModelMode.LOCAL, DiagnosisStatus.NEEDS_REVIEW)]
            unavailable = stored[-1]
            cli.escalate_diagnosis(conn, run_id=run_id, diagnosis_id=unavailable.diagnosis_id, operator="op_migrator")
            [action_id] = [str(row[0]) for row in conn.execute(
                "SELECT action_id FROM actions WHERE run_id = ? AND status = 'succeeded' AND action_id IN "
                "(SELECT action_id FROM action_audits)", (run_id,))]
            assert {row[0] for row in conn.execute("SELECT mode FROM diagnoses")} == {"rules", "local", "recorded"}
            before = snapshot(conn)
            exports = exported(conn, run_id, action_id)
    for table in ("diagnoses", "fix_validations", "plans", "approvals", "actions", "fulfilments", "plan_audits",
                  "action_audits", "integrations", "orders", "evidence", "incidents", "sim_merchant_profiles"):
        assert before["rows"][table], table  # type: ignore[index]  # every child of runs and diagnoses has rows
    return OldDatabase(data_dir / cli.DATABASE_FILE, run_id, action_id, before, exports)


@pytest.fixture(scope="module")
def migrated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[OldDatabase, sqlite3.Connection, tuple]]:
    old = old_database(tmp_path_factory.mktemp("migration_8"))
    conn = storage.connect(old.path)
    try:
        yield old, conn, storage.migrate_simulator(conn)  # what the API does at start; the CLI applies migrate()
    finally:
        conn.close()


def test_the_unchanged_codes_database_takes_migration_eight_once(migrated) -> None:
    _, conn, applied = migrated
    assert applied == (8,)
    assert storage.migrate_simulator(conn) == storage.migrate(conn) == ()
    assert versions(conn) == (1, 2, 3, 4, 5, 6, 7, 8, 1001, 1002)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # back on for the connection that migrated


def test_every_row_index_trigger_and_foreign_key_survives_the_rebuild(migrated) -> None:
    old, conn, _ = migrated
    after = snapshot(conn)
    assert after["rows"] == old.before["rows"]
    assert after["indexes"] == old.before["indexes"]  # the primary keys' own indexes included, under their old names
    assert after["triggers"] == old.before["triggers"]  # runs_clock_monotonic, with the same text
    assert after["foreign_keys"] == old.before["foreign_keys"]  # in both directions: into and out of the rebuilt two
    tables_after, tables_before = after["tables"], old.before["tables"]
    assert {name: sql for name, sql in tables_after.items() if name not in REBUILT} == {  # type: ignore[union-attr]
        name: sql for name, sql in tables_before.items() if name not in REBUILT}  # type: ignore[union-attr]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_only_the_mode_lists_and_the_host_check_change_in_the_two_rebuilt_tables(migrated) -> None:
    old, conn, _ = migrated
    after = snapshot(conn)["tables"]
    for table in REBUILT:
        rebuilt = after[table].replace(f'CREATE TABLE "{table}"', f"CREATE TABLE {table}")  # type: ignore[index]
        assert MODES_AFTER in rebuilt and MODES_BEFORE not in rebuilt
        expected = old.before["tables"][table].replace(MODES_BEFORE, MODES_AFTER)  # type: ignore[index]
        if table == "diagnoses":
            rebuilt = rebuilt.replace(f",\n        {HOST_CHECK}", "")
        assert rebuilt == expected, table


def test_reports_and_receipts_of_existing_runs_are_byte_for_byte_as_before(migrated) -> None:
    old, conn, _ = migrated
    assert exported(conn, old.run_id, old.action_id) == old.exports
    stored_envelopes = {row[0] for row in conn.execute("SELECT output FROM diagnoses")}
    assert not any("provider_host" in envelope or "token_usage" in envelope for envelope in stored_envelopes)


def test_the_rebuilt_tables_still_enforce_their_trigger_and_foreign_keys(migrated) -> None:
    old, conn, _ = migrated
    with pytest.raises(sqlite3.IntegrityError, match="cannot move backwards"), storage.write_transaction(conn):
        conn.execute("UPDATE runs SET clock_ms = clock_ms - 1 WHERE run_id = ?", (old.run_id,))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), storage.write_transaction(conn):
        conn.execute("DELETE FROM runs WHERE run_id = ?", (old.run_id,))  # its children still reference it
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), storage.write_transaction(conn):
        conn.execute("DELETE FROM diagnoses WHERE run_id = ?", (old.run_id,))  # fix validations and plans name them
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), storage.write_transaction(conn):
        conn.execute("INSERT INTO diagnoses (run_id, diagnosis_id, incident_id, mode, packet_hash, status, output,"
                     " created_wall_ms) VALUES (?, 'diag_orphan', 'inc_nowhere', 'rules', ?, 'needs_review', '{}', 1)",
                     (old.run_id, "a" * 64))
    assert snapshot(conn)["rows"] == old.before["rows"]  # every refused write rolled back


@pytest.fixture
def small_old(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A database at migration 7 holding one run and its integration, as conftest's helpers seed it."""
    with closing(storage.connect(db_path)) as conn:
        assert storage._apply_migrations(conn, storage.MIGRATIONS[:7]) == (1, 2, 3, 4, 5, 6, 7)
        seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)
        yield conn


def test_after_the_migration_hosted_is_stored_only_with_its_provider_host(small_old: sqlite3.Connection) -> None:
    conn = small_old
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), storage.write_transaction(conn):  # before: refused
        conn.execute("UPDATE runs SET model_mode = 'hosted' WHERE run_id = ?", (RUN_A,))
    assert storage.migrate(conn) == (8,)
    with storage.write_transaction(conn):
        conn.execute("UPDATE runs SET model_mode = 'hosted' WHERE run_id = ?", (RUN_A,))
    assert storage.require_run(conn, RUN_A).model_mode is ModelMode.HOSTED
    with storage.write_transaction(conn):
        conn.execute("INSERT INTO incidents (run_id, incident_id, \"trigger\", cohort, window_start_ms, window_end_ms,"
                     " evidence_cutoff_ms, state, measured_impact) VALUES (?, 'inc_1', 't', '{}', 1, 2, 2, 'open', '{}')",
                     (RUN_A,))
    insert = ("INSERT INTO diagnoses (run_id, diagnosis_id, incident_id, mode, model_id, thinking, prompt_version,"
              " packet_hash, status, output, error, created_wall_ms) VALUES (?, ?, 'inc_1', ?, 'm', 0, 'diag-p5', ?,"
              " 'needs_review', ?, 'why', 1)")
    hosted_envelope = json.dumps({"provider_host": "provider.invalid"})
    with storage.write_transaction(conn):
        conn.execute(insert, (RUN_A, "diag_hosted", "hosted", "a" * 64, hosted_envelope))
    for mode, envelope in (("hosted", "{}"), ("local", hosted_envelope)):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"), storage.write_transaction(conn):
            conn.execute(insert, (RUN_A, f"diag_{mode}_refused", mode, "a" * 64, envelope))


def test_a_fresh_database_and_a_migrated_one_end_with_the_same_schema(migrated, tmp_path: Path) -> None:
    _, conn, _ = migrated

    def schema(of: sqlite3.Connection) -> set[tuple[str, str, str, str | None]]:
        return {tuple(row) for row in of.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")}  # type: ignore[misc]

    with closing(storage.connect(tmp_path / "fresh.sqlite")) as fresh:
        storage.migrate_simulator(fresh)
        assert schema(fresh) == schema(conn)
        assert re.search(r"CHECK \(model_mode IN \('local', 'recorded', 'rules', 'hosted'\)\)",
                         fresh.execute("SELECT sql FROM sqlite_master WHERE name = 'runs'").fetchone()[0])


def test_a_rebuild_that_would_leave_a_broken_reference_changes_nothing(small_old: sqlite3.Connection) -> None:
    conn = small_old
    conn.execute("PRAGMA foreign_keys = OFF")  # an orphan, as a damaged file might hold one
    with storage.write_transaction(conn):
        conn.execute("INSERT INTO incidents (run_id, incident_id, \"trigger\", cohort, window_start_ms, window_end_ms,"
                     " evidence_cutoff_ms, state, measured_impact) VALUES ('run_gone', 'inc_1', 't', '{}', 1, 2, 2,"
                     " 'open', '{}')")
    conn.execute("PRAGMA foreign_keys = ON")
    runs_before = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'runs'").fetchone()[0]
    with pytest.raises(storage.StorageError, match=r"migration 8 \(hosted_model_mode\) would leave 1 rows"):
        storage.migrate(conn)
    assert versions(conn) == (1, 2, 3, 4, 5, 6, 7)
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name = 'runs'").fetchone()[0] == runs_before
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_v8'").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
