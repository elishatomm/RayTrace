"""SQLite schema, migrations, transactions and run-scoped queries.

Every table carries ``run_id``; every query helper takes ``run_id`` and filters on
it, so one run can never read or change another run's records.

Write helpers never open a transaction themselves. They refuse to run outside
``write_transaction`` so callers can compose several writes into one atomic unit
(for example inbox row + observation + delivery attempt in ingestion).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from recovery_workbench.contracts import (
    ContractError,
    CurrencyCode,
    EpochMs,
    EvidenceKind,
    InboxStatus,
    ModelMode,
    Paise,
    PaymentObservation,
    ReceiveOutcome,
    ScopeError,
    StrictModel,
    WorkerResult,
)

BUSY_TIMEOUT_MS = 5000
MEMORY_DATABASE = ":memory:"
# Between attempts at the WAL switch while another connection holds the lock it needs (see _switch_to_wal).
_WAL_SWITCH_PAUSE_S = 0.005
# Primary result codes; error.sqlite_errorcode is the extended code, whose low byte is the primary one.
_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
# The WAL switch's clock and sleep. Tests replace them to show a retry without real waiting.
_monotonic: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


class StorageError(RuntimeError):
    """The database cannot be configured or used the way the workbench requires."""


def wall_clock_ms() -> int:
    """Wall-clock time for ``*_wall_ms`` columns; never used as replay time."""
    return time.time_ns() // 1_000_000


# --------------------------------------------------------------------------- connections


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a connection with the workbench pragmas; ``":memory:"`` is supported.

    ``isolation_level=None`` disables the sqlite3 module's implicit transactions;
    every write goes through ``write_transaction`` instead. Raises ``StorageError``
    if other connections keep a new database file locked for ``BUSY_TIMEOUT_MS``.
    """
    target = os.fspath(path)
    conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn, target)
    except BaseException:
        conn.close()
        raise
    return conn


def _apply_pragmas(conn: sqlite3.Connection, target: str) -> None:
    # sqlite3.connect(timeout=...) has already set busy_timeout to BUSY_TIMEOUT_MS for the
    # lock waits SQLite retries itself; the WAL switch needs its own retry (_switch_to_wal).
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise StorageError("SQLite refused PRAGMA foreign_keys=ON; run and order references would go unenforced")
    if target != MEMORY_DATABASE:
        mode = _switch_to_wal(conn, target)
        if mode != "wal":
            raise StorageError(f"could not enable WAL journal mode (got {mode!r}); keep the database on a local disk")
    # FULL: a commit is on disk before ingestion acknowledges a webhook. NORMAL in WAL
    # mode can lose the latest commits on power loss, which would break "ack after durable receipt".
    conn.execute("PRAGMA synchronous = FULL")


def _switch_to_wal(conn: sqlite3.Connection, target: str) -> str:
    """``PRAGMA journal_mode = WAL``, retried while other connections hold the lock it needs; returns the mode.

    busy_timeout does not cover this statement. On a file still in rollback mode, the switch upgrades
    its read lock to a write lock, and SQLite refuses that upgrade at once (SQLITE_BUSY) rather than
    wait, because waiting could deadlock. Connections that open the same new file together hit this.
    So busy and locked refusals are retried here, until BUSY_TIMEOUT_MS has passed on a monotonic
    clock; an attempt that SQLite itself makes wait can end up to one busy_timeout after that.
    """
    deadline = _monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            return str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError as switch_error:
            if not _is_busy(switch_error):
                raise
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise StorageError(
                    f"could not switch {target} to WAL journal mode: other connections held the lock it needs for "
                    f"the whole {BUSY_TIMEOUT_MS} ms budget. Retry once the other processes using this database "
                    "have finished opening it; if it keeps failing, stop the process that holds it in a long "
                    "transaction"
                ) from switch_error
            _sleep(min(_WAL_SWITCH_PAUSE_S, remaining))


def _is_busy(error: sqlite3.Error) -> bool:
    """SQLITE_BUSY or SQLITE_LOCKED, extended codes included; an error without an SQLite code is neither."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in _BUSY_CODES


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; on any error ``ROLLBACK`` and re-raise.

    IMMEDIATE takes the write lock up front, so a check made inside the block cannot
    be invalidated by another writer before the block's own writes commit.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException as body_error:  # GeneratorExit included: a block closed early rolls back
        _roll_back(conn, cause=body_error)
        raise
    else:
        _commit(conn)


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One consistent snapshot for several reads; reuses the caller's transaction if open."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN DEFERRED")
    try:
        yield conn
    except BaseException as read_error:  # GeneratorExit included: a block closed early rolls back
        _roll_back(conn, cause=read_error)
        raise
    else:
        conn.execute("COMMIT")


def _commit(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("COMMIT")
    except BaseException as commit_error:
        _roll_back(conn, cause=commit_error)
        raise


def _roll_back(conn: sqlite3.Connection, *, cause: BaseException) -> None:
    if not conn.in_transaction:
        return  # SQLite already rolled back (some commit failures do); nothing is left to undo.
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error as rollback_error:
        raise StorageError(
            f"ROLLBACK failed after {type(cause).__name__}: {cause}. Nothing was committed; "
            "close this connection so SQLite discards the open transaction"
        ) from rollback_error


def _require_write_transaction(conn: sqlite3.Connection, operation: str) -> None:
    if not conn.in_transaction:
        raise StorageError(f"{operation} must run inside storage.write_transaction so it commits with related writes")


# --------------------------------------------------------------------------- schema


def _append_only(table: str, *, if_not_exists: bool = False) -> tuple[str, str]:
    create = "CREATE TRIGGER IF NOT EXISTS" if if_not_exists else "CREATE TRIGGER"
    return (
        f"{create} {table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
        f"{create} {table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
    )


# The replay clock's guard on runs. Migration 1 creates it, and migration 8 creates it again after rebuilding runs, from
# this one text, so the trigger a rebuilt database holds is the one it held before.
_RUNS_CLOCK_MONOTONIC = """CREATE TRIGGER runs_clock_monotonic BEFORE UPDATE OF clock_ms ON runs
        WHEN NEW.clock_ms < OLD.clock_ms
        BEGIN SELECT RAISE(ABORT, 'the replay clock cannot move backwards'); END"""

_INITIAL_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        seed INTEGER NOT NULL,
        scenario TEXT NOT NULL,
        split TEXT NOT NULL CHECK (split IN ('dev', 'test', 'demo', 'unit')),
        merchant_id TEXT NOT NULL,
        environment TEXT NOT NULL CHECK (environment = 'synthetic'),
        generator_version TEXT NOT NULL,
        clock_ms INTEGER NOT NULL CHECK (clock_ms >= 0),
        model_mode TEXT NOT NULL CHECK (model_mode IN ('local', 'recorded', 'rules')),
        created_wall_ms INTEGER NOT NULL CHECK (created_wall_ms >= 0)
    ) STRICT""",
    _RUNS_CLOCK_MONOTONIC,
    """CREATE TABLE integrations (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        integration_id TEXT NOT NULL,
        secret_ref TEXT NOT NULL,
        max_body_bytes INTEGER NOT NULL CHECK (max_body_bytes > 0),
        PRIMARY KEY (run_id, integration_id)
    ) STRICT""",
    """CREATE TABLE orders (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        order_id TEXT NOT NULL,
        merchant_id TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
        amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
        allowed_methods TEXT NOT NULL CHECK (json_valid(allowed_methods) AND json_type(allowed_methods) = 'array'),
        cancelled_at_ms INTEGER CHECK (cancelled_at_ms IS NULL OR cancelled_at_ms >= 0),
        refunded INTEGER NOT NULL DEFAULT 0 CHECK (refunded IN (0, 1)),
        consent_state TEXT NOT NULL DEFAULT 'unknown' CHECK (consent_state IN ('unknown', 'granted', 'withdrawn')),
        consent_known_at_ms INTEGER CHECK (consent_known_at_ms IS NULL OR consent_known_at_ms >= 0),
        fulfilment_state TEXT NOT NULL DEFAULT 'unfulfilled' CHECK (fulfilment_state IN ('unfulfilled', 'fulfilled')),
        PRIMARY KEY (run_id, order_id)
    ) STRICT""",
    """CREATE TRIGGER orders_fulfilment_is_final BEFORE UPDATE OF fulfilment_state ON orders
        WHEN OLD.fulfilment_state = 'fulfilled' AND NEW.fulfilment_state <> 'fulfilled'
        BEGIN SELECT RAISE(ABORT, 'a fulfilled order cannot become unfulfilled'); END""",
    """CREATE TABLE payment_attempts (
        run_id TEXT NOT NULL,
        payment_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        method TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
        PRIMARY KEY (run_id, payment_id),
        FOREIGN KEY (run_id, order_id) REFERENCES orders(run_id, order_id)
    ) STRICT""",
    "CREATE INDEX payment_attempts_by_order ON payment_attempts(run_id, order_id)",
    """CREATE TABLE payment_observations (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        observation_id TEXT NOT NULL,
        payment_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('unknown', 'created', 'authorized', 'captured', 'failed', 'refunded')),
        amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        method TEXT,
        source TEXT NOT NULL CHECK (source IN ('webhook', 'snapshot')),
        source_ref TEXT NOT NULL,
        occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
        known_at_ms INTEGER NOT NULL CHECK (known_at_ms >= 0),
        amount_refunded_paise INTEGER NOT NULL DEFAULT 0 CHECK (amount_refunded_paise >= 0),
        error_code TEXT,
        error_source TEXT,
        error_step TEXT,
        error_reason TEXT,
        PRIMARY KEY (run_id, observation_id)
    ) STRICT""",
    "CREATE INDEX payment_observations_by_payment ON payment_observations(run_id, payment_id)",
    "CREATE INDEX payment_observations_by_order ON payment_observations(run_id, order_id)",
    *_append_only("payment_observations"),
    """CREATE TABLE webhook_inbox (
        inbox_id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        integration_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        raw_body BLOB NOT NULL,
        body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
        signature_result TEXT NOT NULL CHECK (signature_result = 'verified'),
        received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
        processing_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (processing_status IN ('pending', 'processed', 'failed', 'manual_review')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        last_error TEXT,
        processed_at_ms INTEGER,
        UNIQUE (run_id, integration_id, event_id),
        UNIQUE (run_id, inbox_id),
        FOREIGN KEY (run_id, integration_id) REFERENCES integrations(run_id, integration_id)
    ) STRICT""",
    "CREATE INDEX webhook_inbox_pending ON webhook_inbox(run_id, processing_status, received_at_ms, inbox_id)",
    """CREATE TABLE conflicting_deliveries (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        integration_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        raw_body BLOB NOT NULL,
        body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
        original_sha256 TEXT NOT NULL CHECK (length(original_sha256) = 64 AND original_sha256 <> body_sha256),
        differing_fields TEXT NOT NULL CHECK (json_valid(differing_fields) AND json_type(differing_fields) = 'array'),
        payment_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        original_payment_id TEXT NOT NULL,
        original_order_id TEXT NOT NULL,
        review_status TEXT NOT NULL DEFAULT 'pending_review' CHECK (review_status IN ('pending_review', 'reviewed')),
        received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
        UNIQUE (run_id, integration_id, event_id, body_sha256),
        FOREIGN KEY (run_id, integration_id, event_id) REFERENCES webhook_inbox(run_id, integration_id, event_id)
    ) STRICT""",
    "CREATE INDEX conflicting_deliveries_by_payment ON conflicting_deliveries(run_id, payment_id)",
    "CREATE INDEX conflicting_deliveries_by_original_payment ON conflicting_deliveries(run_id, original_payment_id)",
    """CREATE TABLE delivery_attempts (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        integration_id TEXT NOT NULL,
        event_id TEXT,
        attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
        http_status INTEGER NOT NULL CHECK (http_status BETWEEN 100 AND 599),
        outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'duplicate', 'conflicting_id', 'rejected_signature',
                                                 'rejected_schema', 'rejected_size', 'unknown_integration')),
        latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
        received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
        body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
        detail TEXT
    ) STRICT""",
    "CREATE INDEX delivery_attempts_by_event ON delivery_attempts(run_id, integration_id, event_id)",
    *_append_only("delivery_attempts"),
    """CREATE TABLE worker_attempts (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        inbox_id INTEGER NOT NULL,
        worker_version TEXT NOT NULL,
        started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0),
        finished_at_ms INTEGER NOT NULL CHECK (finished_at_ms >= started_at_ms),
        exception_category TEXT,
        stack_trace TEXT,
        result TEXT NOT NULL CHECK (result IN ('fulfilled', 'already_fulfilled', 'no_effect', 'failed', 'manual_review')),
        detail TEXT,
        FOREIGN KEY (run_id, inbox_id) REFERENCES webhook_inbox(run_id, inbox_id)
    ) STRICT""",
    "CREATE INDEX worker_attempts_by_inbox ON worker_attempts(run_id, inbox_id)",
    *_append_only("worker_attempts"),
    """CREATE TABLE deployments (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        deploy_id TEXT NOT NULL,
        service_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('code', 'config')),
        deployed_at_ms INTEGER NOT NULL CHECK (deployed_at_ms >= 0),
        known_at_ms INTEGER NOT NULL CHECK (known_at_ms >= 0),
        summary TEXT NOT NULL,
        files_changed TEXT NOT NULL CHECK (json_valid(files_changed) AND json_type(files_changed) = 'array'),
        diff_text TEXT NOT NULL,
        PRIMARY KEY (run_id, deploy_id)
    ) STRICT""",
    """CREATE TABLE merchant_logs (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        log_id TEXT NOT NULL,
        service_id TEXT NOT NULL,
        level TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical')),
        message TEXT NOT NULL,
        occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
        known_at_ms INTEGER NOT NULL CHECK (known_at_ms >= 0),
        PRIMARY KEY (run_id, log_id)
    ) STRICT""",
    """CREATE TABLE evidence (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        evidence_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('incident_trigger', 'metric', 'provider_state', 'delivery_health',
                                           'worker_health', 'worker_error', 'deployment', 'log', 'fixture_check',
                                           'order_journey')),
        source_table TEXT NOT NULL,
        source_record_id TEXT NOT NULL,
        known_at_ms INTEGER NOT NULL CHECK (known_at_ms >= 0),
        content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
        finding TEXT NOT NULL CHECK (json_valid(finding)),
        PRIMARY KEY (run_id, evidence_id)
    ) STRICT""",
    """CREATE TABLE incidents (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        incident_id TEXT NOT NULL,
        "trigger" TEXT NOT NULL,
        cohort TEXT NOT NULL CHECK (json_valid(cohort)),
        window_start_ms INTEGER NOT NULL CHECK (window_start_ms >= 0),
        window_end_ms INTEGER NOT NULL CHECK (window_end_ms >= window_start_ms),
        evidence_cutoff_ms INTEGER NOT NULL CHECK (evidence_cutoff_ms >= 0),
        state TEXT NOT NULL,
        measured_impact TEXT NOT NULL CHECK (json_valid(measured_impact)),
        PRIMARY KEY (run_id, incident_id)
    ) STRICT""",
    """CREATE TABLE diagnoses (
        run_id TEXT NOT NULL,
        diagnosis_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('local', 'recorded', 'rules')),
        model_id TEXT,
        model_digest TEXT,
        thinking INTEGER CHECK (thinking IS NULL OR thinking IN (0, 1)),
        prompt_version TEXT,
        packet_hash TEXT NOT NULL CHECK (length(packet_hash) = 64),
        status TEXT NOT NULL CHECK (status IN ('validated', 'needs_review')),
        output TEXT CHECK (output IS NULL OR json_valid(output)),
        error TEXT,
        elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
        created_wall_ms INTEGER NOT NULL CHECK (created_wall_ms >= 0),
        PRIMARY KEY (run_id, diagnosis_id),
        FOREIGN KEY (run_id, incident_id) REFERENCES incidents(run_id, incident_id)
    ) STRICT""",
    """CREATE TABLE fix_validations (
        run_id TEXT NOT NULL,
        validation_id TEXT NOT NULL,
        diagnosis_id TEXT NOT NULL,
        fix_id TEXT NOT NULL,
        fixture_test_id TEXT NOT NULL,
        deployed_result TEXT NOT NULL CHECK (json_valid(deployed_result)),
        corrected_result TEXT NOT NULL CHECK (json_valid(corrected_result)),
        passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
        validated_at_ms INTEGER NOT NULL CHECK (validated_at_ms >= 0),
        operator_id TEXT NOT NULL,
        PRIMARY KEY (run_id, validation_id),
        FOREIGN KEY (run_id, diagnosis_id) REFERENCES diagnoses(run_id, diagnosis_id)
    ) STRICT""",
    """CREATE TABLE plans (
        run_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        diagnosis_id TEXT,
        fix_validation_id TEXT,
        operation TEXT NOT NULL CHECK (operation IN ('reprocess_inbox', 'prepare_provider_replay', 'escalate',
                                                     'refresh_provider_state', 'no_action')),
        targets TEXT NOT NULL CHECK (json_valid(targets) AND json_type(targets) = 'array'),
        target_value_paise INTEGER NOT NULL CHECK (target_value_paise >= 0),
        evidence_version TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        plan_hash TEXT NOT NULL UNIQUE CHECK (length(plan_hash) = 64),
        expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms >= 0),
        status TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'executed', 'invalidated', 'expired')),
        created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
        PRIMARY KEY (run_id, plan_id),
        UNIQUE (run_id, plan_id, plan_hash),
        FOREIGN KEY (run_id, incident_id) REFERENCES incidents(run_id, incident_id),
        FOREIGN KEY (run_id, diagnosis_id) REFERENCES diagnoses(run_id, diagnosis_id),
        FOREIGN KEY (run_id, fix_validation_id) REFERENCES fix_validations(run_id, validation_id)
    ) STRICT""",
    """CREATE TABLE approvals (
        run_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        operator_id TEXT NOT NULL,
        approved_at_ms INTEGER NOT NULL CHECK (approved_at_ms >= 0),
        expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms > approved_at_ms),
        PRIMARY KEY (run_id, approval_id),
        UNIQUE (run_id, plan_id),
        FOREIGN KEY (run_id, plan_id, plan_hash) REFERENCES plans(run_id, plan_id, plan_hash)
    ) STRICT""",
    """CREATE TABLE actions (
        run_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        operation_key TEXT NOT NULL UNIQUE,
        plan_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('succeeded', 'rejected', 'failed', 'unknown_outcome')),
        "before" TEXT CHECK ("before" IS NULL OR json_valid("before")),
        "after" TEXT CHECK ("after" IS NULL OR json_valid("after")),
        error TEXT,
        executed_at_ms INTEGER NOT NULL CHECK (executed_at_ms >= 0),
        PRIMARY KEY (run_id, action_id),
        FOREIGN KEY (run_id, plan_id) REFERENCES plans(run_id, plan_id)
    ) STRICT""",
    """CREATE TABLE fulfilments (
        run_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        payment_id TEXT NOT NULL,
        amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        fulfilled_at_ms INTEGER NOT NULL CHECK (fulfilled_at_ms >= 0),
        via TEXT NOT NULL CHECK (via IN ('worker', 'reprocess')),
        action_id TEXT,
        PRIMARY KEY (run_id, order_id),
        CHECK ((via = 'reprocess') = (action_id IS NOT NULL)),
        FOREIGN KEY (run_id, order_id) REFERENCES orders(run_id, order_id),
        FOREIGN KEY (run_id, action_id) REFERENCES actions(run_id, action_id)
    ) STRICT""",
    *_append_only("fulfilments"),
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    # A rebuild drops and recreates a table that other tables reference. SQLite allows that only with foreign keys off
    # for the connection, so _apply_migrations switches them off around the migration and checks every reference before
    # it commits.
    rebuilds_tables: bool = False


# The recovery receipt (phase 11). A plan row and an action row keep what was decided, not why each order was
# left out at planning or which checks passed at execution; these two append-only tables keep that, written in
# the same transaction as the plan or the action, so a receipt can be regenerated from the database alone.
_RECEIPT_AUDITS: tuple[str, ...] = (
    """CREATE TABLE plan_audits (
        run_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        deployed_version TEXT NOT NULL,
        corrected_version TEXT NOT NULL,
        stale_after_ms INTEGER NOT NULL CHECK (stale_after_ms > 0),
        candidates TEXT NOT NULL CHECK (json_valid(candidates) AND json_type(candidates) = 'array'),
        dry_runs TEXT NOT NULL CHECK (json_valid(dry_runs) AND json_type(dry_runs) = 'array'),
        superseded_inbox_ids TEXT NOT NULL
            CHECK (json_valid(superseded_inbox_ids) AND json_type(superseded_inbox_ids) = 'object'),
        PRIMARY KEY (run_id, plan_id),
        FOREIGN KEY (run_id, plan_id) REFERENCES plans(run_id, plan_id)
    ) STRICT""",
    *_append_only("plan_audits"),
    """CREATE TABLE action_audits (
        run_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        application_version TEXT NOT NULL,
        stale_after_ms INTEGER NOT NULL CHECK (stale_after_ms > 0),
        checks TEXT NOT NULL CHECK (json_valid(checks) AND json_type(checks) = 'array'),
        evidence TEXT NOT NULL CHECK (json_valid(evidence) AND json_type(evidence) = 'object'),
        orders TEXT NOT NULL CHECK (json_valid(orders) AND json_type(orders) = 'array'),
        postconditions TEXT NOT NULL CHECK (json_valid(postconditions) AND json_type(postconditions) = 'object'),
        amounts TEXT NOT NULL CHECK (json_valid(amounts) AND json_type(amounts) = 'object'),
        PRIMARY KEY (run_id, action_id),
        FOREIGN KEY (run_id, action_id) REFERENCES actions(run_id, action_id)
    ) STRICT""",
    *_append_only("action_audits"),
)

# The provider's webhook delivery log (owner decision Q29 (b)), as a read-only refresh read it: one row per delivery
# attempt the provider recorded, with the HTTP status it saw (NULL: it recorded a timeout). It is the provider's view;
# delivery_attempts is the receiver's own record, and the two differ exactly when a receiver answered 2xx and lost the
# event before storing it. Every statement is IF NOT EXISTS, so half a manual revert does not stop the next start.
_PROVIDER_DELIVERY_LOG: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS provider_delivery_attempts (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        integration_id TEXT NOT NULL,
        provider_attempt_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payment_id TEXT NOT NULL,
        attempted_at_ms INTEGER NOT NULL CHECK (attempted_at_ms >= 0),
        provider_http_status INTEGER
            CHECK (provider_http_status IS NULL OR provider_http_status BETWEEN 100 AND 599),
        triggered_by TEXT NOT NULL CHECK (triggered_by IN ('automatic', 'manual')),
        known_at_ms INTEGER NOT NULL CHECK (known_at_ms >= attempted_at_ms),
        source_ref TEXT NOT NULL,
        UNIQUE (run_id, integration_id, provider_attempt_id),
        FOREIGN KEY (run_id, integration_id) REFERENCES integrations(run_id, integration_id)
    ) STRICT""",
    "CREATE INDEX IF NOT EXISTS provider_delivery_attempts_by_payment"
    " ON provider_delivery_attempts(run_id, payment_id)",
    *_append_only("provider_delivery_attempts", if_not_exists=True),
)

# Q46 (the owner's model chain): the hosted model mode. The mode lists in runs.model_mode and diagnoses.mode are CHECK
# constraints, which SQLite cannot alter, so both tables are rebuilt by SQLite's documented procedure: create the new
# table, copy every row, drop the old table, rename the new one. Foreign keys are off for the connection meanwhile
# (Migration.rebuilds_tables), so no reference is touched: a foreign key names a table, and the rebuilt table takes the
# name again. Nothing else changes: the same columns in the same order, every other constraint as it was, runs' one
# trigger created again from the same text, and no index besides each primary key, then as now. diagnoses gains one
# CHECK: a row's envelope names its provider host exactly when the row is hosted.
_HOSTED_MODE: tuple[str, ...] = (
    """CREATE TABLE runs_v8 (
        run_id TEXT PRIMARY KEY,
        seed INTEGER NOT NULL,
        scenario TEXT NOT NULL,
        split TEXT NOT NULL CHECK (split IN ('dev', 'test', 'demo', 'unit')),
        merchant_id TEXT NOT NULL,
        environment TEXT NOT NULL CHECK (environment = 'synthetic'),
        generator_version TEXT NOT NULL,
        clock_ms INTEGER NOT NULL CHECK (clock_ms >= 0),
        model_mode TEXT NOT NULL CHECK (model_mode IN ('local', 'recorded', 'rules', 'hosted')),
        created_wall_ms INTEGER NOT NULL CHECK (created_wall_ms >= 0)
    ) STRICT""",
    "INSERT INTO runs_v8 (run_id, seed, scenario, split, merchant_id, environment, generator_version, clock_ms,"
    " model_mode, created_wall_ms) SELECT run_id, seed, scenario, split, merchant_id, environment, generator_version,"
    " clock_ms, model_mode, created_wall_ms FROM runs",
    "DROP TABLE runs",
    "ALTER TABLE runs_v8 RENAME TO runs",
    _RUNS_CLOCK_MONOTONIC,
    """CREATE TABLE diagnoses_v8 (
        run_id TEXT NOT NULL,
        diagnosis_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('local', 'recorded', 'rules', 'hosted')),
        model_id TEXT,
        model_digest TEXT,
        thinking INTEGER CHECK (thinking IS NULL OR thinking IN (0, 1)),
        prompt_version TEXT,
        packet_hash TEXT NOT NULL CHECK (length(packet_hash) = 64),
        status TEXT NOT NULL CHECK (status IN ('validated', 'needs_review')),
        output TEXT CHECK (output IS NULL OR json_valid(output)),
        error TEXT,
        elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
        created_wall_ms INTEGER NOT NULL CHECK (created_wall_ms >= 0),
        PRIMARY KEY (run_id, diagnosis_id),
        FOREIGN KEY (run_id, incident_id) REFERENCES incidents(run_id, incident_id),
        CHECK ((mode = 'hosted') = (json_extract(output, '$.provider_host') IS NOT NULL))
    ) STRICT""",
    "INSERT INTO diagnoses_v8 (run_id, diagnosis_id, incident_id, mode, model_id, model_digest, thinking,"
    " prompt_version, packet_hash, status, output, error, elapsed_ms, created_wall_ms) SELECT run_id, diagnosis_id,"
    " incident_id, mode, model_id, model_digest, thinking, prompt_version, packet_hash, status, output, error,"
    " elapsed_ms, created_wall_ms FROM diagnoses",
    "DROP TABLE diagnoses",
    "ALTER TABLE diagnoses_v8 RENAME TO diagnoses",
)

# Ordered; each applied once and recorded in schema_migrations. Never change what an applied
# migration builds: add a new one, because existing databases will not re-run it.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_schema", _INITIAL_SCHEMA),
    # Retry backoff: a failed row waits until next_attempt_at_ms instead of being retried at once.
    Migration(2, "inbox_retry_schedule", (
        "ALTER TABLE webhook_inbox ADD COLUMN next_attempt_at_ms INTEGER"
        " CHECK (next_attempt_at_ms IS NULL OR next_attempt_at_ms >= 0)",
    )),
    Migration(3, "recovery_receipt_audits", _RECEIPT_AUDITS),
    # Q32 option (a): every verified delivery with a new event ID looks its body up by hash inside the receipt's
    # write transaction. Without this index that lookup reads every inbox row of the integration under the write lock.
    # Not unique: rows received before this migration may already hold one body under several event IDs. IF NOT
    # EXISTS, here and in 5: a database that kept an index but lost its version row (half a manual revert) still opens.
    Migration(4, "inbox_body_lookup", (
        "CREATE INDEX IF NOT EXISTS webhook_inbox_by_body ON webhook_inbox(run_id, integration_id, body_sha256)",
    )),
    # Q34: a body that no inbox row holds is then looked up among the deliveries held as conflicts, in the same
    # transaction. Not unique: one body can be held under several event IDs, each reused with other bytes.
    Migration(5, "conflict_body_lookup", (
        "CREATE INDEX IF NOT EXISTS conflicting_deliveries_by_body"
        " ON conflicting_deliveries(run_id, integration_id, body_sha256)",
    )),
    # Q29 (b): the provider's delivery log, read by a refresh. Not in the oracle's observable dataset: a world's seeding
    # never writes it, and leaving it out keeps every world's observable hash.
    Migration(6, "provider_delivery_log", _PROVIDER_DELIVERY_LOG),
    # Q40 (security audit findings 11 and 13): reads that grew with what any local webhook sender can store. The first
    # two number a delivery attempt with one seek, MAX(attempt_no) + 1, where a count read every earlier attempt under
    # the receipt's write lock; the partial one holds only unverifiable attempts, which are numbered per body. The other
    # four find the conflicts naming an order or its payments, where an order projection read every conflict of the
    # run. Each ends in received_at_ms, so the as-of lookup reads the index alone: the receipt time is stored after the
    # held body, and reading it from the table pages through that body. IF NOT EXISTS, as in 4 and 5.
    Migration(7, "attempt_and_conflict_lookups", (
        "CREATE INDEX IF NOT EXISTS delivery_attempts_by_event_attempt"
        " ON delivery_attempts(run_id, integration_id, event_id, attempt_no)",
        "CREATE INDEX IF NOT EXISTS delivery_attempts_unverified_by_body"
        " ON delivery_attempts(run_id, integration_id, body_sha256, attempt_no) WHERE event_id IS NULL",
        "CREATE INDEX IF NOT EXISTS conflicting_deliveries_by_order_received"
        " ON conflicting_deliveries(run_id, order_id, received_at_ms)",
        "CREATE INDEX IF NOT EXISTS conflicting_deliveries_by_original_order_received"
        " ON conflicting_deliveries(run_id, original_order_id, received_at_ms)",
        "CREATE INDEX IF NOT EXISTS conflicting_deliveries_by_payment_received"
        " ON conflicting_deliveries(run_id, payment_id, received_at_ms)",
        "CREATE INDEX IF NOT EXISTS conflicting_deliveries_by_original_payment_received"
        " ON conflicting_deliveries(run_id, original_payment_id, received_at_ms)",
    )),
    Migration(8, "hosted_model_mode", _HOSTED_MODE, rebuilds_tables=True),
)

TABLES: tuple[str, ...] = (
    "runs", "integrations", "orders", "payment_attempts", "payment_observations", "webhook_inbox",
    "conflicting_deliveries", "delivery_attempts", "worker_attempts", "deployments", "merchant_logs",
    "evidence", "incidents", "diagnoses", "fix_validations", "plans", "approvals", "actions", "fulfilments",
    "plan_audits", "action_audits", "provider_delivery_attempts",
)


def migrate(conn: sqlite3.Connection) -> tuple[int, ...]:
    """Apply pending application migrations in order; returns the versions applied by this call.

    Each migration runs in its own write transaction and re-checks
    ``schema_migrations`` under the write lock, so concurrent callers apply it once.
    """
    return _apply_migrations(conn, MIGRATIONS)


def _apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> tuple[int, ...]:
    _require_increasing_versions(migrations)
    with write_transaction(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_wall_ms INTEGER NOT NULL) STRICT"
        )
    newly_applied: list[int] = []
    for migration in migrations:
        rebuild: AbstractContextManager[None] = _foreign_keys_off(conn) if migration.rebuilds_tables else nullcontext()
        with rebuild, write_transaction(conn):
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (migration.version,)
            ).fetchone()
            if already is not None:
                continue
            for statement in migration.statements:
                conn.execute(statement)
            if migration.rebuilds_tables:
                _require_resolved_references(conn, migration)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_wall_ms) VALUES (?, ?, ?)",
                (migration.version, migration.name, wall_clock_ms()),
            )
        newly_applied.append(migration.version)
    return tuple(newly_applied)


@contextmanager
def _foreign_keys_off(conn: sqlite3.Connection) -> Iterator[None]:
    """Foreign keys off for this connection only, around one table rebuild; other connections keep enforcing them.

    SQLite ignores the pragma inside a transaction, so it is switched before the migration's transaction begins and
    back on once it has ended, committed or rolled back. Both switches are read back, as connect() reads its own.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise StorageError("SQLite kept foreign keys on, so no table can be rebuilt; migrate outside any transaction")
    try:
        yield
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StorageError("SQLite refused PRAGMA foreign_keys=ON after a table rebuild; close this connection")


def _require_resolved_references(conn: sqlite3.Connection, migration: Migration) -> None:
    """Refuse to commit a rebuild after which any row's foreign key no longer resolves; the rollback undoes it all."""
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise StorageError(
            f"migration {migration.version} ({migration.name}) would leave {len(broken)} rows whose foreign keys do "
            f"not resolve, the first {tuple(broken[0])}; nothing was changed. Check the database with PRAGMA "
            "foreign_key_check before starting again"
        )


def _require_increasing_versions(migrations: Sequence[Migration]) -> None:
    """Refuse, before anything is applied, a list whose versions repeat or go backwards.

    ``schema_migrations`` keys on the version alone, so a reused number would be taken as applied and skipped.
    """
    versions = [migration.version for migration in migrations]
    if any(later <= earlier for earlier, later in pairwise(versions)):
        raise StorageError(
            f"migration versions must be unique and strictly increasing, got {versions}; "
            "give each new migration the next unused number"
        )


# --------------------------------------------------------------------------- records

RecordId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")]
Split = Literal["dev", "test", "demo", "unit"]
FulfilmentVia = Literal["worker", "reprocess"]


class RunRecord(StrictModel):
    run_id: RecordId
    seed: Annotated[int, Field(strict=True)]
    scenario: str
    split: Split
    merchant_id: RecordId
    environment: Literal["synthetic"] = "synthetic"
    generator_version: str
    clock_ms: EpochMs
    model_mode: ModelMode
    created_wall_ms: EpochMs = Field(default_factory=wall_clock_ms)


class IntegrationRecord(StrictModel):
    run_id: RecordId
    integration_id: RecordId
    secret_ref: RecordId  # name of a fixture secret held in code, never the secret itself
    max_body_bytes: Annotated[int, Field(strict=True, gt=0)]


class OrderRecord(StrictModel):
    run_id: RecordId
    order_id: RecordId
    merchant_id: RecordId
    created_at_ms: EpochMs
    amount_paise: Paise
    currency: CurrencyCode
    allowed_methods: tuple[str, ...] = ()
    cancelled_at_ms: EpochMs | None = None
    refunded: bool = False
    consent_state: Literal["unknown", "granted", "withdrawn"] = "unknown"
    consent_known_at_ms: EpochMs | None = None
    fulfilment_state: Literal["unfulfilled", "fulfilled"] = "unfulfilled"


class PaymentAttemptRecord(StrictModel):
    run_id: RecordId
    payment_id: RecordId
    order_id: RecordId
    method: str
    created_at_ms: EpochMs


class DeploymentRecord(StrictModel):
    run_id: RecordId
    deploy_id: RecordId
    service_id: RecordId
    kind: Literal["code", "config"]
    deployed_at_ms: EpochMs
    known_at_ms: EpochMs
    summary: str
    files_changed: tuple[str, ...]
    diff_text: str


class MerchantLogRecord(StrictModel):
    run_id: RecordId
    log_id: RecordId
    service_id: RecordId
    level: Literal["debug", "info", "warning", "error", "critical"]
    message: str  # untrusted free text
    occurred_at_ms: EpochMs
    known_at_ms: EpochMs


class InboxRecord(StrictModel):
    inbox_id: int
    run_id: str
    integration_id: str
    event_id: str
    event_type: str
    raw_body: bytes
    body_sha256: str
    signature_result: Literal["verified"]
    received_at_ms: EpochMs
    processing_status: InboxStatus
    attempts: int
    last_error: str | None
    processed_at_ms: EpochMs | None
    next_attempt_at_ms: EpochMs | None = None  # a pending row is not retried before this replay time


class ConflictingDeliveryRecord(StrictModel):
    id: int
    run_id: str
    integration_id: str
    event_id: str
    raw_body: bytes
    body_sha256: str
    original_sha256: str
    differing_fields: tuple[str, ...]
    payment_id: str
    order_id: str
    original_payment_id: str
    original_order_id: str
    review_status: Literal["pending_review", "reviewed"]
    received_at_ms: EpochMs


class DeliveryAttemptRecord(StrictModel):
    id: int
    run_id: str
    integration_id: str
    event_id: str | None  # None when the delivery could not be verified
    attempt_no: int
    http_status: int
    outcome: ReceiveOutcome
    latency_ms: int
    received_at_ms: EpochMs
    body_sha256: str
    # Why a delivery was rejected, or what a body resent under a new event ID repeats: a stored event or a delivery
    # held as a conflict. Workbench text, never payload content.
    detail: str | None


HttpStatus = Annotated[int, Field(strict=True, ge=100, le=599)]


class ProviderDeliveryAttemptRecord(StrictModel):
    """One webhook delivery attempt as the provider's delivery log records it, read by a refresh (migration 6).

    The provider's view of a delivery, not the receiver's: ``delivery_attempts`` holds what the receiver recorded.
    """

    id: int
    run_id: RecordId
    integration_id: RecordId  # the integration the provider delivered to
    provider_attempt_id: RecordId  # the provider's own ID for the attempt
    event_id: RecordId
    event_type: str
    payment_id: RecordId
    attempted_at_ms: EpochMs
    provider_http_status: HttpStatus | None  # None: the provider saw no answer and recorded a timeout
    triggered_by: Literal["automatic", "manual"]  # the provider's own schedule, or a resend from its dashboard
    known_at_ms: EpochMs  # the refresh that first read it
    source_ref: str

    @property
    def acknowledged(self) -> bool:
        """Whether the provider saw a 2xx answer, after which it neither retries nor resends the event."""
        return self.provider_http_status is not None and 200 <= self.provider_http_status < 300


class WorkerAttemptRecord(StrictModel):
    id: int
    run_id: str
    inbox_id: int
    worker_version: str
    started_at_ms: EpochMs
    finished_at_ms: EpochMs
    exception_category: str | None
    stack_trace: str | None
    result: WorkerResult
    detail: str | None


class FulfilmentRecord(StrictModel):
    run_id: RecordId
    order_id: RecordId
    payment_id: RecordId
    amount_paise: Paise
    currency: CurrencyCode
    fulfilled_at_ms: EpochMs
    via: FulfilmentVia
    action_id: RecordId | None = None


class PlanAuditRecord(StrictModel):
    """How planning judged every candidate order of one reprocess plan (migration 3).

    ``plans.py`` writes it with the plan and gives the JSON fields their shape; a receipt reads it back.
    """

    run_id: RecordId
    plan_id: RecordId
    deployed_version: str
    corrected_version: str
    stale_after_ms: Annotated[int, Field(strict=True, gt=0)]
    candidates: tuple[dict[str, JsonValue], ...]
    dry_runs: tuple[dict[str, JsonValue], ...]
    superseded_inbox_ids: dict[str, tuple[int, ...]]


class ActionAuditRecord(StrictModel):
    """What one execution checked, read and changed (migration 3).

    ``executor.py`` writes it in the transaction that records the action and gives the JSON fields their shape.
    """

    run_id: RecordId
    action_id: RecordId
    application_version: str
    stale_after_ms: Annotated[int, Field(strict=True, gt=0)]
    checks: tuple[dict[str, JsonValue], ...]
    evidence: dict[str, JsonValue]
    orders: tuple[dict[str, JsonValue], ...]
    postconditions: dict[str, JsonValue]
    amounts: dict[str, JsonValue]


def _columns(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


# --------------------------------------------------------------------------- runs


def create_run(conn: sqlite3.Connection, run: RunRecord) -> RunRecord:
    _require_write_transaction(conn, "create_run")
    conn.execute(
        "INSERT INTO runs (run_id, seed, scenario, split, merchant_id, environment, generator_version, clock_ms,"
        " model_mode, created_wall_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run.run_id, run.seed, run.scenario, run.split, run.merchant_id, run.environment,
            run.generator_version, run.clock_ms, run.model_mode.value, run.created_wall_ms,
        ),
    )
    return run


def get_run(conn: sqlite3.Connection, run_id: str) -> RunRecord | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else RunRecord.model_validate(_columns(row))


def require_run(conn: sqlite3.Connection, run_id: str, *, merchant_id: str | None = None) -> RunRecord:
    """The run, or ``ScopeError`` if it does not exist or belongs to another merchant."""
    run = get_run(conn, run_id)
    if run is None:
        raise ScopeError(f"run {run_id!r} does not exist; create it before reading or writing its records")
    if merchant_id is not None and run.merchant_id != merchant_id:
        raise ScopeError(f"run {run_id!r} belongs to merchant {run.merchant_id!r}, not {merchant_id!r}")
    return run


def advance_clock(conn: sqlite3.Connection, run_id: str, to_ms: int) -> int:
    """Move the run's replay clock forward (or leave it); refuses to move it back."""
    _require_write_transaction(conn, "advance_clock")
    if isinstance(to_ms, bool) or not isinstance(to_ms, int) or to_ms < 0:
        raise ContractError(f"replay clock target must be a non-negative integer of milliseconds, got {to_ms!r}")
    run = require_run(conn, run_id)
    if to_ms < run.clock_ms:
        raise ContractError(
            f"replay clock of run {run_id!r} is at {run.clock_ms} ms; refusing to move it back to {to_ms} ms"
        )
    conn.execute("UPDATE runs SET clock_ms = ? WHERE run_id = ?", (to_ms, run_id))
    return to_ms


def set_run_model_mode(conn: sqlite3.Connection, run_id: str, model_mode: ModelMode) -> None:
    """Record ``model_mode`` as the mode the run was seeded in.

    Only public mode's demo runs need it: they are seeded when the image is built and copied in at every start
    (public_template), and each takes the mode of the server that copies it, as a run that server seeded would.
    """
    _require_write_transaction(conn, "set_run_model_mode")
    require_run(conn, run_id)
    conn.execute("UPDATE runs SET model_mode = ? WHERE run_id = ?", (model_mode.value, run_id))


# --------------------------------------------------------------------------- integrations


def add_integration(conn: sqlite3.Connection, integration: IntegrationRecord) -> IntegrationRecord:
    _require_write_transaction(conn, "add_integration")
    require_run(conn, integration.run_id)
    conn.execute(
        "INSERT INTO integrations (run_id, integration_id, secret_ref, max_body_bytes) VALUES (?, ?, ?, ?)",
        (integration.run_id, integration.integration_id, integration.secret_ref, integration.max_body_bytes),
    )
    return integration


def get_integration(conn: sqlite3.Connection, *, run_id: str, integration_id: str) -> IntegrationRecord | None:
    row = conn.execute(
        "SELECT * FROM integrations WHERE run_id = ? AND integration_id = ?", (run_id, integration_id)
    ).fetchone()
    return None if row is None else IntegrationRecord.model_validate(_columns(row))


# --------------------------------------------------------------------------- orders and attempts


def _order_from_row(row: sqlite3.Row) -> OrderRecord:
    columns = _columns(row)
    columns["allowed_methods"] = tuple(json.loads(str(columns["allowed_methods"])))
    return OrderRecord.model_validate(columns)


def insert_order(conn: sqlite3.Connection, order: OrderRecord) -> OrderRecord:
    """Insert an order of the run's own merchant; another merchant's order is a ``ScopeError``."""
    _require_write_transaction(conn, "insert_order")
    require_run(conn, order.run_id, merchant_id=order.merchant_id)
    conn.execute(
        "INSERT INTO orders (run_id, order_id, merchant_id, created_at_ms, amount_paise, currency, allowed_methods,"
        " cancelled_at_ms, refunded, consent_state, consent_known_at_ms, fulfilment_state)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            order.run_id, order.order_id, order.merchant_id, order.created_at_ms, order.amount_paise,
            order.currency, json.dumps(list(order.allowed_methods)), order.cancelled_at_ms, int(order.refunded),
            order.consent_state, order.consent_known_at_ms, order.fulfilment_state,
        ),
    )
    return order


def get_order(conn: sqlite3.Connection, *, run_id: str, order_id: str) -> OrderRecord | None:
    row = conn.execute("SELECT * FROM orders WHERE run_id = ? AND order_id = ?", (run_id, order_id)).fetchone()
    return None if row is None else _order_from_row(row)


def require_order(conn: sqlite3.Connection, *, run_id: str, order_id: str) -> OrderRecord:
    order = get_order(conn, run_id=run_id, order_id=order_id)
    if order is None:
        raise ScopeError(f"order {order_id!r} is not an order of run {run_id!r}")
    return order


def list_orders(conn: sqlite3.Connection, *, run_id: str) -> tuple[OrderRecord, ...]:
    rows = conn.execute("SELECT * FROM orders WHERE run_id = ? ORDER BY order_id", (run_id,)).fetchall()
    return tuple(_order_from_row(row) for row in rows)


def mark_order_fulfilled(conn: sqlite3.Connection, *, run_id: str, order_id: str) -> None:
    _require_write_transaction(conn, "mark_order_fulfilled")
    cursor = conn.execute(
        "UPDATE orders SET fulfilment_state = 'fulfilled' WHERE run_id = ? AND order_id = ?", (run_id, order_id)
    )
    if cursor.rowcount != 1:
        raise ScopeError(f"order {order_id!r} is not an order of run {run_id!r}; nothing was marked fulfilled")


def insert_payment_attempt(conn: sqlite3.Connection, attempt: PaymentAttemptRecord) -> PaymentAttemptRecord:
    """Record a distinct payment attempt; delivery retries are never new attempts."""
    _require_write_transaction(conn, "insert_payment_attempt")
    require_order(conn, run_id=attempt.run_id, order_id=attempt.order_id)
    conn.execute(
        "INSERT INTO payment_attempts (run_id, payment_id, order_id, method, created_at_ms) VALUES (?, ?, ?, ?, ?)",
        (attempt.run_id, attempt.payment_id, attempt.order_id, attempt.method, attempt.created_at_ms),
    )
    return attempt


def list_payment_attempts(
    conn: sqlite3.Connection, *, run_id: str, order_id: str, as_of_ms: int | None
) -> tuple[PaymentAttemptRecord, ...]:
    """Attempts for one order; with ``as_of_ms``, only attempts created by then."""
    rows = conn.execute(
        "SELECT * FROM payment_attempts WHERE run_id = ? AND order_id = ? AND (? IS NULL OR created_at_ms <= ?)"
        " ORDER BY created_at_ms, payment_id",
        (run_id, order_id, as_of_ms, as_of_ms),
    ).fetchall()
    return tuple(PaymentAttemptRecord.model_validate(_columns(row)) for row in rows)


def get_payment_attempts(
    conn: sqlite3.Connection, *, run_id: str, payment_ids: Sequence[str], as_of_ms: int | None
) -> tuple[PaymentAttemptRecord, ...]:
    """Attempt rows for specific payments, whichever order they name."""
    if not payment_ids:
        return ()
    rows = conn.execute(
        f"SELECT * FROM payment_attempts WHERE run_id = ? AND payment_id IN ({_placeholders(len(payment_ids))})"
        " AND (? IS NULL OR created_at_ms <= ?) ORDER BY created_at_ms, payment_id",
        (run_id, *payment_ids, as_of_ms, as_of_ms),
    ).fetchall()
    return tuple(PaymentAttemptRecord.model_validate(_columns(row)) for row in rows)


# --------------------------------------------------------------------------- observations

_OBSERVATION_COLUMNS: tuple[str, ...] = (
    "run_id", "observation_id", "payment_id", "order_id", "status", "amount_paise", "currency", "method",
    "source", "source_ref", "occurred_at_ms", "known_at_ms", "amount_refunded_paise", "error_code",
    "error_source", "error_step", "error_reason",
)


def insert_observation(conn: sqlite3.Connection, observation: PaymentObservation) -> PaymentObservation:
    """Append one provider observation. The table rejects updates and deletes."""
    _require_write_transaction(conn, "insert_observation")
    require_run(conn, observation.run_id)
    values = observation.model_dump(mode="json")
    conn.execute(
        f"INSERT INTO payment_observations ({', '.join(_OBSERVATION_COLUMNS)})"
        f" VALUES ({_placeholders(len(_OBSERVATION_COLUMNS))})",
        tuple(values[column] for column in _OBSERVATION_COLUMNS),
    )
    return observation


def list_observations(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    as_of_ms: int | None,
    order_id: str | None = None,
    payment_ids: Sequence[str] = (),
) -> tuple[PaymentObservation, ...]:
    """Observations visible at ``as_of_ms``: known by then and not occurring after it.

    With ``order_id`` and/or ``payment_ids``, only observations naming that order or
    one of those payments. ``as_of_ms=None`` means no cutoff (audit use only).
    """
    clauses = ["run_id = ?"]
    params: list[object] = [run_id]
    if as_of_ms is not None:
        clauses.append("known_at_ms <= ? AND occurred_at_ms <= ?")
        params.extend((as_of_ms, as_of_ms))
    subject: list[str] = []
    if order_id is not None:
        subject.append("order_id = ?")
        params.append(order_id)
    if payment_ids:
        subject.append(f"payment_id IN ({_placeholders(len(payment_ids))})")
        params.extend(payment_ids)
    if subject:
        clauses.append(f"({' OR '.join(subject)})")
    rows = conn.execute(
        f"SELECT * FROM payment_observations WHERE {' AND '.join(clauses)}"
        " ORDER BY occurred_at_ms, known_at_ms, observation_id",
        params,
    ).fetchall()
    return tuple(PaymentObservation.model_validate(_columns(row)) for row in rows)


# --------------------------------------------------------------------------- deployments and logs


def insert_deployment(conn: sqlite3.Connection, deployment: DeploymentRecord) -> DeploymentRecord:
    _require_write_transaction(conn, "insert_deployment")
    require_run(conn, deployment.run_id)
    conn.execute(
        "INSERT INTO deployments (run_id, deploy_id, service_id, kind, deployed_at_ms, known_at_ms, summary,"
        " files_changed, diff_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            deployment.run_id, deployment.deploy_id, deployment.service_id, deployment.kind,
            deployment.deployed_at_ms, deployment.known_at_ms, deployment.summary,
            json.dumps(list(deployment.files_changed)), deployment.diff_text,
        ),
    )
    return deployment


def list_deployments(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int, service_id: str | None = None
) -> tuple[DeploymentRecord, ...]:
    """Deployments known and deployed by ``as_of_ms``, oldest first."""
    rows = conn.execute(
        "SELECT * FROM deployments WHERE run_id = ? AND known_at_ms <= ? AND deployed_at_ms <= ?"
        " AND (? IS NULL OR service_id = ?) ORDER BY deployed_at_ms, deploy_id",
        (run_id, as_of_ms, as_of_ms, service_id, service_id),
    ).fetchall()
    deployments = []
    for row in rows:
        columns = _columns(row)
        columns["files_changed"] = tuple(json.loads(str(columns["files_changed"])))
        deployments.append(DeploymentRecord.model_validate(columns))
    return tuple(deployments)


def insert_merchant_log(conn: sqlite3.Connection, log: MerchantLogRecord) -> MerchantLogRecord:
    _require_write_transaction(conn, "insert_merchant_log")
    require_run(conn, log.run_id)
    conn.execute(
        "INSERT INTO merchant_logs (run_id, log_id, service_id, level, message, occurred_at_ms, known_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (log.run_id, log.log_id, log.service_id, log.level, log.message, log.occurred_at_ms, log.known_at_ms),
    )
    return log


def list_merchant_logs(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int, service_id: str | None = None
) -> tuple[MerchantLogRecord, ...]:
    """Log lines known and occurring by ``as_of_ms``, oldest first."""
    rows = conn.execute(
        "SELECT * FROM merchant_logs WHERE run_id = ? AND known_at_ms <= ? AND occurred_at_ms <= ?"
        " AND (? IS NULL OR service_id = ?) ORDER BY occurred_at_ms, log_id",
        (run_id, as_of_ms, as_of_ms, service_id, service_id),
    ).fetchall()
    return tuple(MerchantLogRecord.model_validate(_columns(row)) for row in rows)


# --------------------------------------------------------------------------- webhook inbox


def insert_inbox_row(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    integration_id: str,
    event_id: str,
    event_type: str,
    raw_body: bytes,
    body_sha256: str,
    received_at_ms: int,
) -> int:
    """Insert a verified event as pending work; the unique key makes a second insert fail."""
    _require_write_transaction(conn, "insert_inbox_row")
    cursor = conn.execute(
        "INSERT INTO webhook_inbox (run_id, integration_id, event_id, event_type, raw_body, body_sha256,"
        " signature_result, received_at_ms) VALUES (?, ?, ?, ?, ?, ?, 'verified', ?)",
        (run_id, integration_id, event_id, event_type, raw_body, body_sha256, received_at_ms),
    )
    if cursor.lastrowid is None:
        raise StorageError(f"SQLite returned no inbox_id for event {event_id!r} of run {run_id!r}")
    return cursor.lastrowid


def find_inbox_row(
    conn: sqlite3.Connection, *, run_id: str, integration_id: str, event_id: str
) -> InboxRecord | None:
    row = conn.execute(
        "SELECT * FROM webhook_inbox WHERE run_id = ? AND integration_id = ? AND event_id = ?",
        (run_id, integration_id, event_id),
    ).fetchone()
    return None if row is None else InboxRecord.model_validate(_columns(row))


def find_inbox_row_by_body(
    conn: sqlite3.Connection, *, run_id: str, integration_id: str, body_sha256: str
) -> InboxRecord | None:
    """The integration's earliest inbox row holding exactly these bytes, whatever its event ID."""
    row = conn.execute(
        "SELECT * FROM webhook_inbox WHERE run_id = ? AND integration_id = ? AND body_sha256 = ?"
        " ORDER BY inbox_id LIMIT 1",
        (run_id, integration_id, body_sha256),
    ).fetchone()
    return None if row is None else InboxRecord.model_validate(_columns(row))


def get_inbox_row(conn: sqlite3.Connection, *, run_id: str, inbox_id: int) -> InboxRecord | None:
    row = conn.execute(
        "SELECT * FROM webhook_inbox WHERE run_id = ? AND inbox_id = ?", (run_id, inbox_id)
    ).fetchone()
    return None if row is None else InboxRecord.model_validate(_columns(row))


def list_inbox_rows(conn: sqlite3.Connection, *, run_id: str) -> tuple[InboxRecord, ...]:
    rows = conn.execute("SELECT * FROM webhook_inbox WHERE run_id = ? ORDER BY inbox_id", (run_id,)).fetchall()
    return tuple(InboxRecord.model_validate(_columns(row)) for row in rows)


def next_pending_inbox_row(conn: sqlite3.Connection, *, run_id: str, received_by_ms: int) -> InboxRecord | None:
    """Oldest pending row that is due at ``received_by_ms`` (replay time), or None.

    Due means received by then and, for a row waiting to be retried, past its
    ``next_attempt_at_ms``.
    """
    row = conn.execute(
        "SELECT * FROM webhook_inbox WHERE run_id = ? AND processing_status = 'pending' AND received_at_ms <= ?"
        " AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms <= ?) ORDER BY received_at_ms, inbox_id LIMIT 1",
        (run_id, received_by_ms, received_by_ms),
    ).fetchone()
    return None if row is None else InboxRecord.model_validate(_columns(row))


def record_inbox_processing(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    inbox_id: int,
    status: InboxStatus,
    attempts: int,
    last_error: str | None,
    processed_at_ms: int | None,
    next_attempt_at_ms: int | None = None,
) -> None:
    """Record a processing outcome; ``next_attempt_at_ms`` schedules a retry of a pending row."""
    _require_write_transaction(conn, "record_inbox_processing")
    if next_attempt_at_ms is not None and status is not InboxStatus.PENDING:
        raise StorageError(f"only a pending inbox row can wait for a retry, not a {status.value!r} one")
    cursor = conn.execute(
        "UPDATE webhook_inbox SET processing_status = ?, attempts = ?, last_error = ?, processed_at_ms = ?,"
        " next_attempt_at_ms = ? WHERE run_id = ? AND inbox_id = ?",
        (status.value, attempts, last_error, processed_at_ms, next_attempt_at_ms, run_id, inbox_id),
    )
    if cursor.rowcount != 1:
        raise ScopeError(f"inbox row {inbox_id} is not part of run {run_id!r}; nothing was updated")


# --------------------------------------------------------------------------- conflicting deliveries


def _conflict_from_row(row: sqlite3.Row) -> ConflictingDeliveryRecord:
    columns = _columns(row)
    columns["differing_fields"] = tuple(json.loads(str(columns["differing_fields"])))
    return ConflictingDeliveryRecord.model_validate(columns)


def insert_conflicting_delivery(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    integration_id: str,
    event_id: str,
    raw_body: bytes,
    body_sha256: str,
    original_sha256: str,
    differing_fields: Sequence[str],
    payment_id: str,
    order_id: str,
    original_payment_id: str,
    original_order_id: str,
    received_at_ms: int,
) -> int:
    """Hold a reused event ID with different bytes for review; it is never processed automatically."""
    _require_write_transaction(conn, "insert_conflicting_delivery")
    cursor = conn.execute(
        "INSERT INTO conflicting_deliveries (run_id, integration_id, event_id, raw_body, body_sha256,"
        " original_sha256, differing_fields, payment_id, order_id, original_payment_id, original_order_id,"
        " received_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, integration_id, event_id, raw_body, body_sha256, original_sha256,
            json.dumps(list(differing_fields)), payment_id, order_id, original_payment_id, original_order_id,
            received_at_ms,
        ),
    )
    if cursor.lastrowid is None:
        raise StorageError(f"SQLite returned no id for the conflicting delivery of event {event_id!r}")
    return cursor.lastrowid


def find_conflicting_delivery(
    conn: sqlite3.Connection, *, run_id: str, integration_id: str, event_id: str, body_sha256: str
) -> ConflictingDeliveryRecord | None:
    row = conn.execute(
        "SELECT * FROM conflicting_deliveries WHERE run_id = ? AND integration_id = ? AND event_id = ?"
        " AND body_sha256 = ?",
        (run_id, integration_id, event_id, body_sha256),
    ).fetchone()
    return None if row is None else _conflict_from_row(row)


def find_conflicting_delivery_by_body(
    conn: sqlite3.Connection, *, run_id: str, integration_id: str, body_sha256: str
) -> ConflictingDeliveryRecord | None:
    """The integration's earliest held conflicting delivery of exactly these bytes, whatever event ID it reused."""
    row = conn.execute(
        "SELECT * FROM conflicting_deliveries WHERE run_id = ? AND integration_id = ? AND body_sha256 = ?"
        " ORDER BY id LIMIT 1",
        (run_id, integration_id, body_sha256),
    ).fetchone()
    return None if row is None else _conflict_from_row(row)


def list_conflicting_deliveries(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int | None
) -> tuple[ConflictingDeliveryRecord, ...]:
    """Every conflicting delivery of the run received by ``as_of_ms``, bodies included; all of them when None."""
    rows = conn.execute(
        "SELECT * FROM conflicting_deliveries WHERE run_id = ? AND (? IS NULL OR received_at_ms <= ?)"
        " ORDER BY received_at_ms, id",
        (run_id, as_of_ms, as_of_ms),
    ).fetchall()
    return tuple(_conflict_from_row(row) for row in rows)


def conflicting_delivery_ids(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int, order_id: str, payment_ids: Sequence[str]
) -> tuple[int, ...]:
    """IDs of the conflicting deliveries received by ``as_of_ms`` that touch the order or one of the payments.

    A conflict touches both what the original event said and what the reused-ID body
    claims, so both sides are matched. Every order projection asks here, and a signed sender
    can hold one conflict per request, so the match runs in SQL on migration 7's indexes and
    reads IDs only: never another order's conflicts, and no held body. Each column has its own
    SELECT, because SQLite answers an OR of them with a range over every conflict of the run.
    Payment IDs are bound in batches of ``_ID_BATCH``, since signed deliveries can name one
    order from any number of payments.
    """
    found = {row[0] for row in conn.execute(
        "SELECT id FROM conflicting_deliveries WHERE run_id = ? AND order_id = ? AND received_at_ms <= ?"
        " UNION ALL SELECT id FROM conflicting_deliveries WHERE run_id = ? AND original_order_id = ?"
        " AND received_at_ms <= ?",
        (run_id, order_id, as_of_ms, run_id, order_id, as_of_ms),
    )}
    wanted = sorted(set(payment_ids))
    for start in range(0, len(wanted), _ID_BATCH):
        batch = wanted[start:start + _ID_BATCH]
        marks = _placeholders(len(batch))
        found.update(row[0] for row in conn.execute(
            f"SELECT id FROM conflicting_deliveries WHERE run_id = ? AND payment_id IN ({marks})"
            " AND received_at_ms <= ?"
            f" UNION ALL SELECT id FROM conflicting_deliveries WHERE run_id = ? AND original_payment_id IN ({marks})"
            " AND received_at_ms <= ?",
            (run_id, *batch, as_of_ms, run_id, *batch, as_of_ms),
        ))
    return tuple(sorted(found))


# --------------------------------------------------------------------------- delivery and worker attempts


def insert_delivery_attempt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    integration_id: str,
    event_id: str | None,
    http_status: int,
    outcome: ReceiveOutcome,
    latency_ms: int,
    received_at_ms: int,
    body_sha256: str,
    detail: str | None = None,
) -> DeliveryAttemptRecord:
    """Record one delivery attempt as diagnostic metadata.

    ``attempt_no`` numbers the attempts of the same event ID; when the event ID is
    unverifiable (None), the unverifiable attempts with the same body hash. It is one past
    the group's highest number, one seek on migration 7's indexes, where a count read every
    earlier attempt under the receipt's write lock, and any local sender can add unverifiable
    ones. The highest is the count: this is the append-only table's only writer, and it
    numbers each group 1, 2, 3 inside the write transaction.
    """
    _require_write_transaction(conn, "insert_delivery_attempt")
    if event_id is None:
        highest = conn.execute(
            "SELECT MAX(attempt_no) FROM delivery_attempts WHERE run_id = ? AND integration_id = ? AND event_id IS NULL"
            " AND body_sha256 = ?",
            (run_id, integration_id, body_sha256),
        ).fetchone()[0]
    else:
        highest = conn.execute(
            "SELECT MAX(attempt_no) FROM delivery_attempts WHERE run_id = ? AND integration_id = ? AND event_id = ?",
            (run_id, integration_id, event_id),
        ).fetchone()[0]
    attempt_no = int(highest or 0) + 1  # NULL: the group has no attempt yet
    cursor = conn.execute(
        "INSERT INTO delivery_attempts (run_id, integration_id, event_id, attempt_no, http_status, outcome,"
        " latency_ms, received_at_ms, body_sha256, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, integration_id, event_id, attempt_no, http_status, outcome.value, latency_ms, received_at_ms,
         body_sha256, detail),
    )
    if cursor.lastrowid is None:
        raise StorageError(f"SQLite returned no id for a delivery attempt of run {run_id!r}")
    return DeliveryAttemptRecord(
        id=cursor.lastrowid, run_id=run_id, integration_id=integration_id, event_id=event_id,
        attempt_no=attempt_no, http_status=http_status, outcome=outcome, latency_ms=latency_ms,
        received_at_ms=received_at_ms, body_sha256=body_sha256, detail=detail,
    )


def list_delivery_attempts(
    conn: sqlite3.Connection, *, run_id: str, integration_id: str | None = None
) -> tuple[DeliveryAttemptRecord, ...]:
    rows = conn.execute(
        "SELECT * FROM delivery_attempts WHERE run_id = ? AND (? IS NULL OR integration_id = ?) ORDER BY id",
        (run_id, integration_id, integration_id),
    ).fetchall()
    return tuple(DeliveryAttemptRecord.model_validate(_columns(row)) for row in rows)


# --------------------------------------------------------------------------- the provider's delivery log (migration 6)

_PROVIDER_DELIVERY_COLUMNS: tuple[str, ...] = (
    "run_id", "integration_id", "provider_attempt_id", "event_id", "event_type", "payment_id", "attempted_at_ms",
    "provider_http_status", "triggered_by", "known_at_ms", "source_ref",
)


def insert_provider_delivery_attempt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    integration_id: str,
    provider_attempt_id: str,
    event_id: str,
    event_type: str,
    payment_id: str,
    attempted_at_ms: int,
    provider_http_status: int | None,
    triggered_by: Literal["automatic", "manual"],
    known_at_ms: int,
    source_ref: str,
) -> ProviderDeliveryAttemptRecord:
    """Append one attempt from the provider's delivery log. The table rejects updates and deletes."""
    _require_write_transaction(conn, "insert_provider_delivery_attempt")
    values = (run_id, integration_id, provider_attempt_id, event_id, event_type, payment_id, attempted_at_ms,
              provider_http_status, triggered_by, known_at_ms, source_ref)
    cursor = conn.execute(
        f"INSERT INTO provider_delivery_attempts ({', '.join(_PROVIDER_DELIVERY_COLUMNS)})"
        f" VALUES ({_placeholders(len(_PROVIDER_DELIVERY_COLUMNS))})",
        values,
    )
    if cursor.lastrowid is None:
        raise StorageError(f"SQLite returned no id for a provider delivery attempt of run {run_id!r}")
    return ProviderDeliveryAttemptRecord.model_validate(
        {"id": cursor.lastrowid, **dict(zip(_PROVIDER_DELIVERY_COLUMNS, values, strict=True))}
    )


def list_provider_delivery_attempts(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int | None, payment_ids: Sequence[str] = ()
) -> tuple[ProviderDeliveryAttemptRecord, ...]:
    """Attempts visible at ``as_of_ms``: read by a refresh by then and made by then; all when None (audit use only).

    With ``payment_ids``, only the attempts of those payments' events. Oldest attempt first.
    """
    clauses = ["run_id = ?"]
    params: list[object] = [run_id]
    if as_of_ms is not None:
        clauses.append("known_at_ms <= ? AND attempted_at_ms <= ?")
        params.extend((as_of_ms, as_of_ms))
    if payment_ids:
        clauses.append(f"payment_id IN ({_placeholders(len(payment_ids))})")
        params.extend(payment_ids)
    rows = conn.execute(
        f"SELECT * FROM provider_delivery_attempts WHERE {' AND '.join(clauses)}"
        " ORDER BY attempted_at_ms, integration_id, provider_attempt_id",
        params,
    ).fetchall()
    return tuple(ProviderDeliveryAttemptRecord.model_validate(_columns(row)) for row in rows)


def acknowledged_unstored_payment_ids(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int, event_type: str
) -> frozenset[str]:
    """Payments with an ``event_type`` event the provider's delivery log shows answered 2xx and no inbox row holds.

    Both as of ``as_of_ms``: attempts made and read by then, inbox rows received by then. The receiver acknowledged
    such an event and lost it before its commit, and the provider will not send it again. The order queue and the
    replay plan both ask here, so they agree on which captures it covers.
    """
    rows = conn.execute(
        "SELECT DISTINCT p.payment_id FROM provider_delivery_attempts p"
        " WHERE p.run_id = ? AND p.known_at_ms <= ? AND p.attempted_at_ms <= ? AND p.event_type = ?"
        " AND p.provider_http_status BETWEEN 200 AND 299"
        " AND NOT EXISTS (SELECT 1 FROM webhook_inbox i WHERE i.run_id = p.run_id"
        " AND i.integration_id = p.integration_id AND i.event_id = p.event_id AND i.received_at_ms <= ?)",
        (run_id, as_of_ms, as_of_ms, event_type, as_of_ms),
    ).fetchall()
    return frozenset(row[0] for row in rows)


_ID_BATCH = 500  # IDs per IN (...) list, well inside SQLite's limit on bound parameters


def find_provider_delivery_attempts(
    conn: sqlite3.Connection, *, run_id: str, provider_attempt_ids: Sequence[str]
) -> dict[tuple[str, str], ProviderDeliveryAttemptRecord]:
    """Stored attempts with these provider attempt IDs, keyed by (integration ID, provider attempt ID)."""
    wanted = sorted(set(provider_attempt_ids))
    found: dict[tuple[str, str], ProviderDeliveryAttemptRecord] = {}
    for start in range(0, len(wanted), _ID_BATCH):
        batch = wanted[start:start + _ID_BATCH]
        rows = conn.execute(
            f"SELECT * FROM provider_delivery_attempts WHERE run_id = ?"
            f" AND provider_attempt_id IN ({_placeholders(len(batch))})",
            (run_id, *batch),
        ).fetchall()
        for row in rows:
            record = ProviderDeliveryAttemptRecord.model_validate(_columns(row))
            found[(record.integration_id, record.provider_attempt_id)] = record
    return found


def insert_worker_attempt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    inbox_id: int,
    worker_version: str,
    started_at_ms: int,
    finished_at_ms: int,
    result: WorkerResult,
    exception_category: str | None = None,
    stack_trace: str | None = None,
    detail: str | None = None,
) -> int:
    _require_write_transaction(conn, "insert_worker_attempt")
    cursor = conn.execute(
        "INSERT INTO worker_attempts (run_id, inbox_id, worker_version, started_at_ms, finished_at_ms,"
        " exception_category, stack_trace, result, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, inbox_id, worker_version, started_at_ms, finished_at_ms, exception_category, stack_trace,
         result.value, detail),
    )
    if cursor.lastrowid is None:
        raise StorageError(f"SQLite returned no id for a worker attempt on inbox row {inbox_id}")
    return cursor.lastrowid


def list_worker_attempts(
    conn: sqlite3.Connection, *, run_id: str, inbox_id: int | None = None
) -> tuple[WorkerAttemptRecord, ...]:
    rows = conn.execute(
        "SELECT * FROM worker_attempts WHERE run_id = ? AND (? IS NULL OR inbox_id = ?) ORDER BY id",
        (run_id, inbox_id, inbox_id),
    ).fetchall()
    return tuple(WorkerAttemptRecord.model_validate(_columns(row)) for row in rows)


# --------------------------------------------------------------------------- fulfilments


def insert_fulfilment(conn: sqlite3.Connection, fulfilment: FulfilmentRecord) -> FulfilmentRecord:
    """Insert the order's one fulfilment; the primary key rejects a second one."""
    _require_write_transaction(conn, "insert_fulfilment")
    require_order(conn, run_id=fulfilment.run_id, order_id=fulfilment.order_id)
    conn.execute(
        "INSERT INTO fulfilments (run_id, order_id, payment_id, amount_paise, currency, fulfilled_at_ms, via,"
        " action_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fulfilment.run_id, fulfilment.order_id, fulfilment.payment_id, fulfilment.amount_paise,
            fulfilment.currency, fulfilment.fulfilled_at_ms, fulfilment.via, fulfilment.action_id,
        ),
    )
    return fulfilment


def get_fulfilment(conn: sqlite3.Connection, *, run_id: str, order_id: str) -> FulfilmentRecord | None:
    row = conn.execute(
        "SELECT * FROM fulfilments WHERE run_id = ? AND order_id = ?", (run_id, order_id)
    ).fetchone()
    return None if row is None else FulfilmentRecord.model_validate(_columns(row))


def list_fulfilments(conn: sqlite3.Connection, *, run_id: str) -> tuple[FulfilmentRecord, ...]:
    rows = conn.execute("SELECT * FROM fulfilments WHERE run_id = ? ORDER BY order_id", (run_id,)).fetchall()
    return tuple(FulfilmentRecord.model_validate(_columns(row)) for row in rows)


# --------------------------------------------------------------------------- receipt audits (migration 3)

_PLAN_AUDIT_JSON = ("candidates", "dry_runs", "superseded_inbox_ids")
_ACTION_AUDIT_JSON = ("checks", "evidence", "orders", "postconditions", "amounts")


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _with_json_columns(row: sqlite3.Row, names: Sequence[str]) -> dict[str, object]:
    columns = _columns(row)
    for name in names:
        columns[name] = json.loads(str(columns[name]))
    return columns


def insert_plan_audit(conn: sqlite3.Connection, audit: PlanAuditRecord) -> PlanAuditRecord:
    """Record how planning judged each candidate, in the transaction that inserts the plan."""
    _require_write_transaction(conn, "insert_plan_audit")
    conn.execute(
        "INSERT INTO plan_audits (run_id, plan_id, deployed_version, corrected_version, stale_after_ms, candidates,"
        " dry_runs, superseded_inbox_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            audit.run_id, audit.plan_id, audit.deployed_version, audit.corrected_version, audit.stale_after_ms,
            _json_text(list(audit.candidates)), _json_text(list(audit.dry_runs)),
            _json_text({order_id: list(ids) for order_id, ids in audit.superseded_inbox_ids.items()}),
        ),
    )
    return audit


def get_plan_audit(conn: sqlite3.Connection, *, run_id: str, plan_id: str) -> PlanAuditRecord | None:
    """None for a plan built before migration 3, which recorded no audit."""
    row = conn.execute("SELECT * FROM plan_audits WHERE run_id = ? AND plan_id = ?", (run_id, plan_id)).fetchone()
    return None if row is None else PlanAuditRecord.model_validate(_with_json_columns(row, _PLAN_AUDIT_JSON))


def insert_action_audit(conn: sqlite3.Connection, audit: ActionAuditRecord) -> ActionAuditRecord:
    """Record what an execution checked, read and changed, in the transaction that records its action."""
    _require_write_transaction(conn, "insert_action_audit")
    conn.execute(
        "INSERT INTO action_audits (run_id, action_id, application_version, stale_after_ms, checks, evidence, orders,"
        " postconditions, amounts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            audit.run_id, audit.action_id, audit.application_version, audit.stale_after_ms,
            _json_text(list(audit.checks)), _json_text(audit.evidence), _json_text(list(audit.orders)),
            _json_text(audit.postconditions), _json_text(audit.amounts),
        ),
    )
    return audit


def get_action_audit(conn: sqlite3.Connection, *, run_id: str, action_id: str) -> ActionAuditRecord | None:
    """None for an action recorded before migration 3, or by an operation that keeps no audit (an escalation)."""
    row = conn.execute(
        "SELECT * FROM action_audits WHERE run_id = ? AND action_id = ?", (run_id, action_id)
    ).fetchone()
    return None if row is None else ActionAuditRecord.model_validate(_with_json_columns(row, _ACTION_AUDIT_JSON))


# --------------------------------------------------------------------------- simulator private state (phase 2)

# Private simulator state: which merchant code is live in each run, and from when the simulated
# provider signs with which secret. Only simulator/runtime.py reads these tables; read tools, the
# evidence packet and the model never do. The "sim_" prefix makes every use easy to find with grep.
PRIVATE_SIMULATOR_TABLES: tuple[str, ...] = ("sim_merchant_releases", "sim_merchant_profiles", "sim_provider_signing")

_SIMULATOR_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE sim_merchant_releases (
        run_id TEXT NOT NULL,
        deploy_id TEXT NOT NULL,
        module TEXT NOT NULL CHECK (module IN ('config', 'payload', 'orders', 'handler', 'receiver', 'checkout',
                                               'notifications')),
        variant INTEGER NOT NULL CHECK (variant >= 1),
        PRIMARY KEY (run_id, deploy_id),
        -- Variants are whole files: a second deploy of a module would silently revert the first.
        UNIQUE (run_id, module),
        FOREIGN KEY (run_id, deploy_id) REFERENCES deployments(run_id, deploy_id)
    ) STRICT""",
    """CREATE TABLE sim_merchant_profiles (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
        emits_categories INTEGER NOT NULL CHECK (emits_categories IN (0, 1))
    ) STRICT""",
)

# A separate ordered track, recorded in schema_migrations like the application track.
# Only generator-created databases carry simulator state, so the application schema
# (``migrate``) never contains it. Its versions follow the application track's, and both
# are applied and checked as one list, so one number can never serve both tracks.
SIMULATOR_MIGRATIONS: tuple[Migration, ...] = (
    Migration(1001, "simulator_private_state", _SIMULATOR_SCHEMA),
    # A provider secret rotation (scenarios.SecretRotation): from when the simulated provider signs new events with
    # another secret. A run without a row signs with merchant.PROVIDER_SIGNING_REF throughout.
    Migration(1002, "simulator_provider_signing", (
        """CREATE TABLE sim_provider_signing (
            run_id TEXT NOT NULL REFERENCES runs(run_id),
            from_ms INTEGER NOT NULL CHECK (from_ms >= 0),
            secret_ref TEXT NOT NULL,
            PRIMARY KEY (run_id, from_ms)
        ) STRICT""",
    )),
)


def migrate_simulator(conn: sqlite3.Connection) -> tuple[int, ...]:
    """Application migrations plus the simulator's private tables; for generator databases."""
    return _apply_migrations(conn, (*MIGRATIONS, *SIMULATOR_MIGRATIONS))


class MerchantRelease(StrictModel):
    run_id: str
    deploy_id: str
    module: str
    variant: Annotated[int, Field(strict=True, ge=1)]
    deployed_at_ms: EpochMs


def insert_merchant_release(conn: sqlite3.Connection, *, run_id: str, deploy_id: str, module: str, variant: int) -> None:
    _require_write_transaction(conn, "insert_merchant_release")
    conn.execute(
        "INSERT INTO sim_merchant_releases (run_id, deploy_id, module, variant) VALUES (?, ?, ?, ?)",
        (run_id, deploy_id, module, variant),
    )


def list_merchant_releases(
    conn: sqlite3.Connection, *, run_id: str, at_ms: int | None
) -> tuple[MerchantRelease, ...]:
    """Releases deployed at or before ``at_ms`` (all when None), oldest first."""
    rows = conn.execute(
        "SELECT r.run_id, r.deploy_id, r.module, r.variant, d.deployed_at_ms FROM sim_merchant_releases r"
        " JOIN deployments d ON d.run_id = r.run_id AND d.deploy_id = r.deploy_id"
        " WHERE r.run_id = ? AND (? IS NULL OR d.deployed_at_ms <= ?) ORDER BY d.deployed_at_ms, r.deploy_id",
        (run_id, at_ms, at_ms),
    ).fetchall()
    return tuple(MerchantRelease.model_validate(_columns(row)) for row in rows)


def set_merchant_profile(conn: sqlite3.Connection, *, run_id: str, emits_categories: bool) -> None:
    _require_write_transaction(conn, "set_merchant_profile")
    require_run(conn, run_id)
    conn.execute(
        "INSERT INTO sim_merchant_profiles (run_id, emits_categories) VALUES (?, ?)", (run_id, int(emits_categories))
    )


def get_merchant_profile(conn: sqlite3.Connection, *, run_id: str) -> bool | None:
    row = conn.execute("SELECT emits_categories FROM sim_merchant_profiles WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else bool(row[0])


def record_provider_rotation(conn: sqlite3.Connection, *, run_id: str, secret_ref: str, from_ms: int) -> None:
    """From ``from_ms`` on, the simulated provider signs new events with ``secret_ref`` (a fixture secret's name)."""
    _require_write_transaction(conn, "record_provider_rotation")
    require_run(conn, run_id)
    conn.execute(
        "INSERT INTO sim_provider_signing (run_id, from_ms, secret_ref) VALUES (?, ?, ?)", (run_id, from_ms, secret_ref)
    )


def provider_secret_at(conn: sqlite3.Connection, *, run_id: str, at_ms: int) -> str | None:
    """The secret of the latest provider rotation at or before ``at_ms``; None when none was recorded by then."""
    row = conn.execute(
        "SELECT secret_ref FROM sim_provider_signing WHERE run_id = ? AND from_ms <= ? ORDER BY from_ms DESC LIMIT 1",
        (run_id, at_ms),
    ).fetchone()
    return None if row is None else str(row[0])


# --------------------------------------------------------------------------- phase 2 helpers


def list_integrations(conn: sqlite3.Connection, *, run_id: str) -> tuple[IntegrationRecord, ...]:
    rows = conn.execute("SELECT * FROM integrations WHERE run_id = ? ORDER BY integration_id", (run_id,)).fetchall()
    return tuple(IntegrationRecord.model_validate(_columns(row)) for row in rows)


def update_integration_secret_ref(
    conn: sqlite3.Connection, *, run_id: str, integration_id: str, secret_ref: str
) -> None:
    """Point an integration at another fixture secret (a deployed config change does this)."""
    _require_write_transaction(conn, "update_integration_secret_ref")
    cursor = conn.execute(
        "UPDATE integrations SET secret_ref = ? WHERE run_id = ? AND integration_id = ?",
        (secret_ref, run_id, integration_id),
    )
    if cursor.rowcount != 1:
        raise ScopeError(f"integration {integration_id!r} is not part of run {run_id!r}; nothing was updated")


def cancel_order(conn: sqlite3.Connection, *, run_id: str, order_id: str, cancelled_at_ms: int) -> None:
    """Record a cancellation when it happens, so no row ever carries a future cancellation."""
    _require_write_transaction(conn, "cancel_order")
    cursor = conn.execute(
        "UPDATE orders SET cancelled_at_ms = ? WHERE run_id = ? AND order_id = ? AND cancelled_at_ms IS NULL",
        (cancelled_at_ms, run_id, order_id),
    )
    if cursor.rowcount != 1:
        raise ScopeError(f"order {order_id!r} of run {run_id!r} does not exist or is already cancelled")


def mark_order_refunded(conn: sqlite3.Connection, *, run_id: str, order_id: str) -> None:
    _require_write_transaction(conn, "mark_order_refunded")
    cursor = conn.execute("UPDATE orders SET refunded = 1 WHERE run_id = ? AND order_id = ?", (run_id, order_id))
    if cursor.rowcount != 1:
        raise ScopeError(f"order {order_id!r} is not an order of run {run_id!r}; nothing was marked refunded")


def list_run_payment_attempts(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int | None
) -> tuple[PaymentAttemptRecord, ...]:
    """Every attempt of the run created by ``as_of_ms`` (all when None)."""
    rows = conn.execute(
        "SELECT * FROM payment_attempts WHERE run_id = ? AND (? IS NULL OR created_at_ms <= ?)"
        " ORDER BY created_at_ms, payment_id",
        (run_id, as_of_ms, as_of_ms),
    ).fetchall()
    return tuple(PaymentAttemptRecord.model_validate(_columns(row)) for row in rows)


def observed_statuses(conn: sqlite3.Connection, *, run_id: str, as_of_ms: int) -> dict[str, frozenset[str]]:
    """For each payment, the set of statuses visible at ``as_of_ms`` (cheap reconciler input)."""
    rows = conn.execute(
        "SELECT payment_id, status FROM payment_observations WHERE run_id = ? AND known_at_ms <= ?"
        " AND occurred_at_ms <= ? GROUP BY payment_id, status",
        (run_id, as_of_ms, as_of_ms),
    ).fetchall()
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row["payment_id"], set()).add(row["status"])
    return {payment_id: frozenset(statuses) for payment_id, statuses in found.items()}


def latest_deployment(conn: sqlite3.Connection, *, run_id: str) -> DeploymentRecord | None:
    deployments = list_deployments(conn, run_id=run_id, as_of_ms=2**62)
    return deployments[-1] if deployments else None


# --------------------------------------------------------------------------- incidents and evidence


class IncidentRecord(StrictModel):
    run_id: RecordId
    incident_id: RecordId
    trigger: str
    cohort: dict[str, JsonValue]
    window_start_ms: EpochMs
    window_end_ms: EpochMs
    evidence_cutoff_ms: EpochMs
    state: Literal["open", "closed"]
    measured_impact: dict[str, JsonValue]


def insert_incident(conn: sqlite3.Connection, incident: IncidentRecord) -> IncidentRecord:
    _require_write_transaction(conn, "insert_incident")
    require_run(conn, incident.run_id)
    conn.execute(
        'INSERT INTO incidents (run_id, incident_id, "trigger", cohort, window_start_ms, window_end_ms,'
        " evidence_cutoff_ms, state, measured_impact) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            incident.run_id, incident.incident_id, incident.trigger, json.dumps(incident.cohort, sort_keys=True),
            incident.window_start_ms, incident.window_end_ms, incident.evidence_cutoff_ms, incident.state,
            json.dumps(incident.measured_impact, sort_keys=True),
        ),
    )
    return incident


def list_incidents(conn: sqlite3.Connection, *, run_id: str, as_of_ms: int | None = None) -> tuple[IncidentRecord, ...]:
    """Incidents opened (evidence cutoff) at or before ``as_of_ms``, oldest first."""
    rows = conn.execute(
        "SELECT * FROM incidents WHERE run_id = ? AND (? IS NULL OR evidence_cutoff_ms <= ?)"
        " ORDER BY evidence_cutoff_ms, incident_id",
        (run_id, as_of_ms, as_of_ms),
    ).fetchall()
    incidents = []
    for row in rows:
        columns = _columns(row)
        columns["cohort"] = json.loads(str(columns["cohort"]))
        columns["measured_impact"] = json.loads(str(columns["measured_impact"]))
        incidents.append(IncidentRecord.model_validate(columns))
    return tuple(incidents)


class EvidenceRecord(StrictModel):
    run_id: RecordId
    evidence_id: Annotated[str, Field(pattern=r"^ev_[a-z0-9_]{1,60}$")]
    kind: EvidenceKind
    source_table: str
    source_record_id: str
    known_at_ms: EpochMs
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    finding: dict[str, JsonValue]


def insert_evidence(conn: sqlite3.Connection, evidence: EvidenceRecord) -> EvidenceRecord:
    _require_write_transaction(conn, "insert_evidence")
    require_run(conn, evidence.run_id)
    conn.execute(
        "INSERT INTO evidence (run_id, evidence_id, kind, source_table, source_record_id, known_at_ms, content_hash,"
        " finding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence.run_id, evidence.evidence_id, evidence.kind.value, evidence.source_table,
            evidence.source_record_id, evidence.known_at_ms, evidence.content_hash,
            json.dumps(evidence.finding, sort_keys=True),
        ),
    )
    return evidence


def list_evidence(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    as_of_ms: int | None = None,
    kind: EvidenceKind | None = None,
    source_record_id: str | None = None,
) -> tuple[EvidenceRecord, ...]:
    rows = conn.execute(
        "SELECT * FROM evidence WHERE run_id = ? AND (? IS NULL OR known_at_ms <= ?) AND (? IS NULL OR kind = ?)"
        " AND (? IS NULL OR source_record_id = ?) ORDER BY known_at_ms, evidence_id",
        (run_id, as_of_ms, as_of_ms, None if kind is None else kind.value, None if kind is None else kind.value,
         source_record_id, source_record_id),
    ).fetchall()
    records = []
    for row in rows:
        columns = _columns(row)
        columns["finding"] = json.loads(str(columns["finding"]))
        records.append(EvidenceRecord.model_validate(columns))
    return tuple(records)


# --------------------------------------------------------------------------- phase 3 helpers


def run_payment_ids(conn: sqlite3.Connection, *, run_id: str) -> frozenset[str]:
    """Every payment the run knows: attempt rows plus payments named by observations."""
    rows = conn.execute(
        "SELECT payment_id FROM payment_attempts WHERE run_id = ?"
        " UNION SELECT payment_id FROM payment_observations WHERE run_id = ?",
        (run_id, run_id),
    ).fetchall()
    return frozenset(row[0] for row in rows)


def existing_observation_ids(
    conn: sqlite3.Connection, *, run_id: str, observation_ids: Sequence[str]
) -> frozenset[str]:
    if not observation_ids:
        return frozenset()
    rows = conn.execute(
        f"SELECT observation_id FROM payment_observations WHERE run_id = ?"
        f" AND observation_id IN ({_placeholders(len(observation_ids))})",
        (run_id, *observation_ids),
    ).fetchall()
    return frozenset(row[0] for row in rows)
