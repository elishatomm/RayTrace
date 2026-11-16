"""Work that does not grow with what earlier webhook deliveries stored (security audit findings 11-13, owner Q40).

The webhook route needs no session and the fixture secrets are published, so any local process can store evidence in
a run, within the route's cap of 600 deliveries a minute, and nothing deletes it. Three reads grew with everything such
a sender had stored:

11. Numbering a delivery attempt counted the earlier attempts of its group. For an unverifiable delivery that count
    read every unverifiable attempt of the integration, inside the receipt's write transaction and, through the API,
    under the clock lock. An attempt is now numbered one past its group's highest number, read with one seek on
    migration 7's indexes.
12. Classifying the order queue scanned every payment and every held conflict of the run once per unknown order (an
    order ID that evidence names and the run does not hold). They are now grouped once per classification.
13. Projecting one order (each plan candidate, each execution check, each worker step) read every conflicting delivery
    of the run, bodies included, and filtered them in Python. It now matches the order's subjects in SQL, one indexed
    column per statement, and reads IDs only.

Work is counted, never timed: SQLite VM instructions through a progress handler, iterations over the run's payments
and conflicts, and query plans. Each fix is also compared with the code it replaced, on the same data: the old reads
are kept here as references. Every record is synthetic.
"""
from __future__ import annotations

import dataclasses
import itertools
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing
from pathlib import Path

import pytest
from conftest import (
    AMOUNT,
    INTEGRATION_A,
    INTEGRATION_B,
    MERCHANT_A,
    MERCHANT_B,
    ORDER_1,
    ORDER_2,
    PAY_1,
    PAY_2,
    RUN_A,
    RUN_B,
    SECRET_A,
    SECRET_B,
    START_MS,
    SeededRun,
    add_order,
    count_rows,
    deliver,
    observation,
    payment_event,
    seed_run,
    store_observations,
)
from fastapi.testclient import TestClient
from test_api_webhooks import RUN_ID, log_in, make_app, seed_unit_run, signed, webhook_path
from test_plans import NOW, STALE_AFTER_MS, build_batch
from test_plans import order_id as batch_order
from test_plans import pay_id as batch_payment

from recovery_workbench import api, ingestion, metrics, order_cases, state, storage
from recovery_workbench.contracts import PaymentObservation, PaymentStatus, ReceiveOutcome, sha256_hex
from recovery_workbench.simulator import signing

MINUTE = 60_000
CAPTURED, AUTHORIZED = PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED
ACCEPTED, DUPLICATE, CONFLICTING = ReceiveOutcome.ACCEPTED, ReceiveOutcome.DUPLICATE, ReceiveOutcome.CONFLICTING_ID
REJECTED_SIGNATURE = ReceiveOutcome.REJECTED_SIGNATURE


def vm_steps[Result](conn: sqlite3.Connection, action: Callable[[], Result]) -> tuple[int, Result]:
    """How many SQLite VM instructions ``action`` runs on ``conn``: the progress handler is called after every one."""
    steps = 0

    def count() -> int:
        nonlocal steps
        steps += 1
        return 0  # anything else interrupts the statement

    conn.set_progress_handler(count, 1)
    try:
        result = action()
    finally:
        conn.set_progress_handler(None, 1)
    return steps, result


def statements_run(conn: sqlite3.Connection, action: Callable[[], object]) -> list[str]:
    """Every statement ``action`` runs on ``conn``, with its bound values filled in (SQLite's trace callback)."""
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        action()
    finally:
        conn.set_trace_callback(None)
    return statements


def query_plan(conn: sqlite3.Connection, statement: str) -> list[str]:
    rows = conn.execute("EXPLAIN QUERY PLAN " + statement)  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- explains the workbench's own statement, captured by SQLite's trace callback; no outside input reaches it
    return [str(row["detail"]) for row in rows]


# --------------------------------------------------------------------------- 11: numbering a delivery attempt

EARLIER_SMALL, EARLIER_LARGE = 10, 2_000  # attempts already in the group when the next one is numbered
RECEIVED_AT = START_MS + 2_000
SIGNED_EVENT = "evt_numbered"


@dataclasses.dataclass(frozen=True)
class Group:
    """Attempts that share one number sequence, as a sender can grow them."""

    name: str
    event_id: str | None  # None: unverifiable, numbered per body hash
    same_body: bool  # every attempt carries one body; otherwise each carries a new one


GROUPS = (
    Group("unsigned, a new body each time", None, same_body=False),
    Group("unsigned, one body repeated", None, same_body=True),
    Group("signed, one event ID repeated", SIGNED_EVENT, same_body=True),
)


def delivery_into(run: SeededRun, group: Group, number: int) -> tuple[dict[str, str], bytes]:
    """The group's next request; ``number`` makes the body new when the group's bodies differ."""
    if group.event_id is not None:
        return signing.signed_delivery(payment_event(), event_id=group.event_id, secret_ref=run.secret_ref)
    return {}, (b'{"probe": "unsigned"}' if group.same_body else f'{{"probe": {number}}}'.encode())


def record_earlier_attempts(conn: sqlite3.Connection, run: SeededRun, group: Group, numbers: Iterator[int],
                            count: int) -> None:
    """``count`` more attempts in the group, each numbered by ``storage.insert_delivery_attempt`` as ingestion does."""
    outcome = REJECTED_SIGNATURE if group.event_id is None else DUPLICATE
    with storage.write_transaction(conn):
        for number in itertools.islice(numbers, count):
            _, body = delivery_into(run, group, number)
            storage.insert_delivery_attempt(
                conn, run_id=run.run_id, integration_id=run.integration_id, event_id=group.event_id,
                http_status=ingestion.HTTP_STATUS_BY_OUTCOME[outcome], outcome=outcome, latency_ms=0,
                received_at_ms=RECEIVED_AT, body_sha256=sha256_hex(body),
            )


def numbered_the_old_way(attempts: Sequence[storage.DeliveryAttemptRecord]) -> list[int]:
    """What the count gave each attempt before Q40: one more than the earlier attempts of its run and integration with
    its event ID, or, when unverifiable, with its body hash."""
    seen: Counter[tuple[str, str, str | None, str | None]] = Counter()
    numbers = []
    for attempt in sorted(attempts, key=lambda item: item.id):
        body = attempt.body_sha256 if attempt.event_id is None else None
        group = (attempt.run_id, attempt.integration_id, attempt.event_id, body)
        seen[group] += 1
        numbers.append(seen[group])
    return numbers


@pytest.mark.parametrize("group", GROUPS, ids=lambda group: group.name)
def test_numbering_an_attempt_costs_the_same_however_many_attempts_came_before_it(
    conn: sqlite3.Connection, run_a: SeededRun, group: Group
) -> None:
    numbers = itertools.count()

    def receive() -> ingestion.DeliveryResponse:
        headers, body = delivery_into(run_a, group, next(numbers))
        return ingestion.receive_webhook(conn, run_id=RUN_A, integration_id=INTEGRATION_A, headers=headers,
                                         raw_body=body, received_at_ms=RECEIVED_AT)

    def measured() -> tuple[int, ReceiveOutcome, int]:
        steps, response = vm_steps(conn, receive)
        [*_, recorded] = storage.list_delivery_attempts(conn, run_id=RUN_A)
        return steps, response.outcome, recorded.attempt_no

    # The first request prepares every statement; for the signed group it stores the event, so each later one is a
    # duplicate. Then the group, and the integration's unverifiable attempts, hold EARLIER_SMALL and EARLIER_LARGE.
    assert receive().outcome is (REJECTED_SIGNATURE if group.event_id is None else ACCEPTED)
    record_earlier_attempts(conn, run_a, group, numbers, EARLIER_SMALL - 1)
    small_steps, small_outcome, small_number = measured()
    record_earlier_attempts(conn, run_a, group, numbers, EARLIER_LARGE - EARLIER_SMALL - 1)
    large_steps, large_outcome, large_number = measured()

    assert small_outcome is large_outcome is (REJECTED_SIGNATURE if group.event_id is None else DUPLICATE)
    stored = storage.list_delivery_attempts(conn, run_id=RUN_A)
    assert [attempt.attempt_no for attempt in stored] == numbered_the_old_way(stored)
    numbered = (small_number, large_number)
    assert numbered == ((EARLIER_SMALL + 1, EARLIER_LARGE + 1) if group.same_body else (1, 1))
    assert large_steps == small_steps, (
        f"{group.name}: numbering took {small_steps} VM steps after {EARLIER_SMALL} earlier attempts and "
        f"{large_steps} after {EARLIER_LARGE}"
    )


def test_attempts_are_still_numbered_1_2_3_within_each_event_id_or_unverifiable_body(
    conn: sqlite3.Connection, run_a: SeededRun, run_b: SeededRun
) -> None:
    capture = signing.signed_delivery(payment_event(), event_id=SIGNED_EVENT, secret_ref=SECRET_A)
    changed = signing.signed_delivery(payment_event(amount_paise=AMOUNT - 1), event_id=SIGNED_EVENT,
                                      secret_ref=SECRET_A)
    other = signing.signed_delivery(payment_event(status=AUTHORIZED), event_id="evt_other", secret_ref=SECRET_A)
    unsigned_a, unsigned_b = ({}, b'{"probe": "a"}'), ({}, b'{"probe": "b"}')
    sends = (  # (run, integration, delivery, outcome, the number the count gave)
        (run_a, INTEGRATION_A, unsigned_a, REJECTED_SIGNATURE, 1),
        (run_a, INTEGRATION_A, capture, ACCEPTED, 1),
        (run_a, INTEGRATION_A, unsigned_a, REJECTED_SIGNATURE, 2),
        (run_a, INTEGRATION_A, capture, DUPLICATE, 2),
        (run_a, INTEGRATION_A, unsigned_b, REJECTED_SIGNATURE, 1),
        (run_a, INTEGRATION_A, changed, CONFLICTING, 3),
        (run_a, "int_SYNTHETIC_UNKNOWN", unsigned_a, ReceiveOutcome.UNKNOWN_INTEGRATION, 1),
        (run_a, INTEGRATION_A, other, ACCEPTED, 1),
        (run_b, INTEGRATION_B, unsigned_a, REJECTED_SIGNATURE, 1),
        (run_a, INTEGRATION_A, unsigned_a, REJECTED_SIGNATURE, 3),
        (run_a, INTEGRATION_A, capture, DUPLICATE, 4),
    )
    for at, (run, integration_id, (headers, body), outcome, _) in enumerate(sends, start=RECEIVED_AT):
        response = ingestion.receive_webhook(conn, run_id=run.run_id, integration_id=integration_id,
                                             headers=headers, raw_body=body, received_at_ms=at)
        assert response.outcome is outcome
    stored = [*storage.list_delivery_attempts(conn, run_id=RUN_A), *storage.list_delivery_attempts(conn, run_id=RUN_B)]
    by_order_sent = sorted(stored, key=lambda attempt: attempt.received_at_ms)
    assert [attempt.attempt_no for attempt in by_order_sent] == [number for *_, number in sends]
    assert [attempt.attempt_no for attempt in sorted(stored, key=lambda item: item.id)] == numbered_the_old_way(stored)


# --------------------------------------------------------------------------- migration 7 on an existing database

MIGRATION_7_INDEXES = {  # name -> (table, columns, partial)
    "delivery_attempts_by_event_attempt": (
        "delivery_attempts", ("run_id", "integration_id", "event_id", "attempt_no"), False),
    "delivery_attempts_unverified_by_body": (
        "delivery_attempts", ("run_id", "integration_id", "body_sha256", "attempt_no"), True),
    "conflicting_deliveries_by_order_received": (
        "conflicting_deliveries", ("run_id", "order_id", "received_at_ms"), False),
    "conflicting_deliveries_by_original_order_received": (
        "conflicting_deliveries", ("run_id", "original_order_id", "received_at_ms"), False),
    "conflicting_deliveries_by_payment_received": (
        "conflicting_deliveries", ("run_id", "payment_id", "received_at_ms"), False),
    "conflicting_deliveries_by_original_payment_received": (
        "conflicting_deliveries", ("run_id", "original_payment_id", "received_at_ms"), False),
}
# Migration 7 only adds indexes, so dropping them and its version row leaves a database as migration 6 left it.
REVERT_MIGRATION_7 = (
    "DROP INDEX IF EXISTS delivery_attempts_by_event_attempt",
    "DROP INDEX IF EXISTS delivery_attempts_unverified_by_body",
    "DROP INDEX IF EXISTS conflicting_deliveries_by_order_received",
    "DROP INDEX IF EXISTS conflicting_deliveries_by_original_order_received",
    "DROP INDEX IF EXISTS conflicting_deliveries_by_payment_received",
    "DROP INDEX IF EXISTS conflicting_deliveries_by_original_payment_received",
    "DELETE FROM schema_migrations WHERE version = 7",
)


def index_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def index_shape(conn: sqlite3.Connection, name: str) -> tuple[str, tuple[str, ...], bool]:
    """The index's table, its columns in order, and whether it is partial; it is never unique here."""
    table = conn.execute("SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone()[0]
    columns = tuple(row[0] for row in conn.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)))
    unique, partial = conn.execute(
        'SELECT "unique", partial FROM pragma_index_list(?) WHERE name = ?', (table, name)).fetchone()
    assert unique == 0, name
    return table, columns, bool(partial)


def test_migration_seven_indexes_a_database_that_already_holds_runs_and_numbering_carries_on(db_path: Path) -> None:
    tables = ("delivery_attempts", "conflicting_deliveries", "webhook_inbox", "payment_observations")
    capture_a, capture_b = (signing.signed_delivery(payment_event(), event_id=SIGNED_EVENT, secret_ref=secret)
                            for secret in (SECRET_A, SECRET_B))
    changed_a = signing.signed_delivery(payment_event(amount_paise=AMOUNT - 1), event_id=SIGNED_EVENT,
                                        secret_ref=SECRET_A)
    unsigned = ({}, b'{"probe": "before migration 7"}')
    received = itertools.count(RECEIVED_AT)

    def send(conn: sqlite3.Connection, run: SeededRun, delivery: tuple[dict[str, str], bytes]) -> ReceiveOutcome:
        headers, body = delivery
        return ingestion.receive_webhook(conn, run_id=run.run_id, integration_id=run.integration_id, headers=headers,
                                         raw_body=body, received_at_ms=next(received)).outcome

    def row_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
        return {(table, run_id): count_rows(conn, table, run_id) for table in tables for run_id in (RUN_A, RUN_B)}

    with closing(storage.connect(db_path)) as conn:
        # The database as the previous version left it: migrations 1 to 6 (the private helper takes a prefix) ...
        assert storage._apply_migrations(conn, storage.MIGRATIONS[:6]) == (1, 2, 3, 4, 5, 6)
        run_a = seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)
        run_b = seed_run(conn, RUN_B, MERCHANT_B, INTEGRATION_B, SECRET_B)
        # ... with attempts numbered 1 and 2 in each group, and a conflict held.
        for run, capture in ((run_a, capture_a), (run_b, capture_b)):
            outcomes = [send(conn, run, delivery) for delivery in (unsigned, capture, unsigned, capture)]
            assert outcomes == [REJECTED_SIGNATURE, ACCEPTED, REJECTED_SIGNATURE, DUPLICATE]
        assert send(conn, run_a, changed_a) is CONFLICTING
        rows_before = row_counts(conn)
        attempts_before = storage.list_delivery_attempts(conn, run_id=RUN_A)
        assert not index_names(conn) & set(MIGRATION_7_INDEXES)
    with closing(storage.connect(db_path)) as conn:  # the upgraded application opens it
        assert storage.migrate(conn) == (7, 8)  # 8: the hosted model mode (Q46)
        assert storage.migrate(conn) == ()
        assert [tuple(row) for row in conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")] == [
            (migration.version, migration.name) for migration in storage.MIGRATIONS]
        assert row_counts(conn) == rows_before
        assert storage.list_delivery_attempts(conn, run_id=RUN_A) == attempts_before
        assert {name: index_shape(conn, name) for name in MIGRATION_7_INDEXES} == MIGRATION_7_INDEXES
        # Numbering carries on from the rows the count numbered, each group with one seek on its own index.
        statements = statements_run(conn, lambda: [send(conn, run_a, unsigned), send(conn, run_a, capture_a)])
        assert [attempt.attempt_no for attempt in storage.list_delivery_attempts(conn, run_id=RUN_A)] == [
            1, 1, 2, 2, 3, 3, 4]
        numbering = [" | ".join(query_plan(conn, statement)) for statement in statements
                     if statement.startswith("SELECT MAX(attempt_no) FROM delivery_attempts")]
        assert len(numbering) == 2
        assert "INDEX delivery_attempts_unverified_by_body (run_id=? AND integration_id=? AND body_sha256=?)" in (
            numbering[0])
        assert "INDEX delivery_attempts_by_event_attempt (run_id=? AND integration_id=? AND event_id=?)" in (
            numbering[1])
        attempts_after = storage.list_delivery_attempts(conn, run_id=RUN_A)
        # The revert reported to the owner; the application then applies migration 7 again on its next start.
        with storage.write_transaction(conn):
            for statement in REVERT_MIGRATION_7:
                conn.execute(statement)
        assert not index_names(conn) & set(MIGRATION_7_INDEXES)
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == [
            1, 2, 3, 4, 5, 6, 8]  # the revert touches only 7's indexes; 8 (Q46) stays applied
    with closing(storage.connect(db_path)) as conn:
        assert storage.migrate(conn) == (7,)
        assert index_names(conn) >= set(MIGRATION_7_INDEXES)
        assert storage.list_delivery_attempts(conn, run_id=RUN_A) == attempts_after
        # Half a manual revert, the version row deleted and the indexes kept, does not stop the next start.
        with storage.write_transaction(conn):
            conn.execute("DELETE FROM schema_migrations WHERE version = 7")
    with closing(storage.connect(db_path)) as conn:
        assert storage.migrate(conn) == (7,)


# --------------------------------------------------------------------------- 13: the conflicts a projection reads

AT = START_MS + 10 * MINUTE  # after every delivery of the constructed cases below
HELD = 300  # conflicting deliveries a sender holds on another order's payment
SUBJECT_COLUMNS = frozenset({"order_id", "original_order_id", "payment_id", "original_payment_id"})


def hold_conflicts(
    conn: sqlite3.Connection, run: SeededRun, event_id: str, count: int, *, start: int = 0, **subject: str
) -> list[int]:
    """``count`` signed bodies under one stored event ID, each with other bytes (an amount counted from ``start``), so
    each is held as a new conflict."""
    held = []
    for number in range(start, start + count):
        response = deliver(conn, run, event_id, amount_paise=AMOUNT + 1 + number, **subject)  # type: ignore[arg-type]
        assert response.outcome is CONFLICTING, response
        assert response.conflicting_delivery_id is not None
        held.append(response.conflicting_delivery_id)
    return held


def test_projecting_an_order_costs_the_same_however_many_conflicts_are_held_on_another_order(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a, ORDER_1, payment_ids=(PAY_1,))
    add_order(conn, run_a, ORDER_2, payment_ids=(PAY_2,))
    deliver(conn, run_a, "evt_order_1", payment_id=PAY_1, order_id=ORDER_1)
    deliver(conn, run_a, "evt_order_2", payment_id=PAY_2, order_id=ORDER_2)  # the event a sender reuses

    def project() -> state.OrderProjection:
        return state.load_order_projection(conn, RUN_A, ORDER_1, AT)

    project()  # prepares every statement
    # One conflict first: a seek into an empty index stops a few instructions sooner than one that lands on another
    # subject's entry, a constant that says nothing about growth.
    held = hold_conflicts(conn, run_a, "evt_order_2", 1, payment_id=PAY_2, order_id=ORDER_2)
    one_steps, beside_one = vm_steps(conn, project)
    held += hold_conflicts(conn, run_a, "evt_order_2", HELD - 1, start=1, payment_id=PAY_2, order_id=ORDER_2)
    many_steps, beside_many = vm_steps(conn, project)

    assert len(set(held)) == HELD
    assert beside_many == beside_one
    assert beside_one.conflicting_delivery_ids == ()
    assert many_steps == one_steps, (
        f"projecting {ORDER_1} took {one_steps} VM steps with 1 conflict held on {ORDER_2} and {many_steps} with {HELD}"
    )
    # The order the conflicts name still sees every one of them.
    assert state.load_order_projection(conn, RUN_A, ORDER_2, AT).conflicting_delivery_ids == tuple(sorted(held))


def test_each_side_of_a_conflict_is_matched_through_its_own_index(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    add_order(conn, run_a, ORDER_1, payment_ids=(PAY_1, PAY_2))
    deliver(conn, run_a, "evt_order_1", payment_id=PAY_1, order_id=ORDER_1)
    hold_conflicts(conn, run_a, "evt_order_1", 3, payment_id=PAY_2, order_id=ORDER_2)
    statements = statements_run(conn, lambda: state.load_order_projection(conn, RUN_A, ORDER_1, AT))
    lookups = [detail for statement in statements if "FROM conflicting_deliveries" in statement
               for detail in query_plan(conn, statement) if "conflicting_deliveries" in detail]
    # Each lookup reads its index alone: never a range over every conflict of the run, and never the table, whose rows
    # keep the receipt time after the held body.
    matched = [re.fullmatch(r"SEARCH conflicting_deliveries USING COVERING INDEX \w+ "
                            r"\(run_id=\? AND (\w+)=\? AND received_at_ms<\?\)", detail) for detail in lookups]
    assert all(matched), lookups
    assert {match.group(1) for match in matched if match} == SUBJECT_COLUMNS, lookups


def test_payment_ids_are_bound_in_batches_and_every_batch_is_matched(
    conn: sqlite3.Connection, run_a: SeededRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    payments = [f"pay_batched_{number}" for number in range(5)]
    add_order(conn, run_a, ORDER_1, payment_ids=payments)
    held = []
    for number, payment_id in enumerate(payments):
        # Another order's event, reused with a body naming one of order 1's payments: matched by that payment alone.
        event_id = f"evt_elsewhere_{number}"
        deliver(conn, run_a, event_id, payment_id=f"pay_elsewhere_{number}", order_id=f"ord_elsewhere_{number}")
        held += hold_conflicts(conn, run_a, event_id, 1, payment_id=payment_id, order_id=f"ord_elsewhere_{number}")
    monkeypatch.setattr(storage, "_ID_BATCH", 2)
    statements = statements_run(conn, lambda: state.load_order_projection(conn, RUN_A, ORDER_1, AT))
    projection = state.load_order_projection(conn, RUN_A, ORDER_1, AT)
    assert projection.conflicting_delivery_ids == tuple(sorted(held))
    assert len([statement for statement in statements
                if "FROM conflicting_deliveries" in statement and "payment_id IN (" in statement]) == 3


# --------------------------------------------------------------------------- the old reads, kept as references


def conflict_ids_the_old_way(
    conn: sqlite3.Connection, *, run_id: str, as_of_ms: int, order_id: str, payment_ids: Sequence[str]
) -> list[int]:
    """``storage.list_conflicting_deliveries`` with a subject, before Q40: every conflict of the run, bodies included,
    filtered here on either side of the conflict. ``state.load_order_projection`` used only the IDs."""
    rows = conn.execute(
        "SELECT * FROM conflicting_deliveries WHERE run_id = ? AND (? IS NULL OR received_at_ms <= ?)"
        " ORDER BY received_at_ms, id",
        (run_id, as_of_ms, as_of_ms),
    ).fetchall()
    wanted = frozenset(payment_ids)
    return [row["id"] for row in rows if order_id in (row["order_id"], row["original_order_id"])
            or not wanted.isdisjoint((row["payment_id"], row["original_payment_id"]))]


def order_projection_the_old_way(
    conn: sqlite3.Connection, run_id: str, order_id: str, as_of_ms: int, *, stale_after_ms: int | None = None
) -> state.OrderProjection:
    """``state.load_order_projection`` before Q40: the same reads, with the conflicts read the old way."""
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        order = storage.get_order(conn, run_id=run_id, order_id=order_id)
        order_attempts = storage.list_payment_attempts(conn, run_id=run_id, order_id=order_id, as_of_ms=as_of_ms)
        naming_order = storage.list_observations(conn, run_id=run_id, as_of_ms=as_of_ms, order_id=order_id)
        payment_ids = sorted({a.payment_id for a in order_attempts} | {o.payment_id for o in naming_order})
        observations = storage.list_observations(conn, run_id=run_id, as_of_ms=as_of_ms, payment_ids=payment_ids)
        attempts = storage.get_payment_attempts(conn, run_id=run_id, payment_ids=payment_ids, as_of_ms=as_of_ms)
        conflict_ids = conflict_ids_the_old_way(conn, run_id=run_id, as_of_ms=as_of_ms, order_id=order_id,
                                                payment_ids=payment_ids)
        fulfilment = storage.get_fulfilment(conn, run_id=run_id, order_id=order_id)
    by_payment: defaultdict[str, list[PaymentObservation]] = defaultdict(list)
    for item in observations:
        by_payment[item.payment_id].append(item)
    payments = tuple(state.project_payment(by_payment[payment_id], as_of_ms, stale_after_ms, payment_id=payment_id)
                     for payment_id in payment_ids)
    return state.project_order(
        run_id=run_id, order_id=order_id, as_of_ms=as_of_ms, order=order, attempts=attempts, payments=payments,
        conflicting_delivery_ids=conflict_ids,
        fulfilment=fulfilment if fulfilment is not None and fulfilment.fulfilled_at_ms <= as_of_ms else None,
    )


def unknown_order_projection_the_old_way(context: order_cases._RunContext, order_id: str) -> state.OrderProjection:
    """``order_cases._unknown_order_projection`` before Q40: a scan of every payment and every conflict per order."""
    view = context.view
    payments = [payment for payment in view.payments.values()
                if any(item.order_id == order_id for item in payment.history)]
    payment_ids = {str(payment.payment_id) for payment in payments}
    conflict_ids = sorted(
        conflict.id for conflict in context.conflicts.values()
        if order_id in (conflict.order_id, conflict.original_order_id)
        or not payment_ids.isdisjoint((conflict.payment_id, conflict.original_payment_id))
    )
    return order_cases._project(context, order_id, None, payments, conflict_ids, view.fulfilments.get(order_id))


# --------------------------------------------------------------------------- 12: the order queue's unknown orders

UNKNOWN_SMALL, UNKNOWN_LARGE = 10, 40  # unknown orders a sender has named
EVIDENCE_AT = NOW - 5 * MINUTE  # visible at NOW, the batch's planning time
LATER = NOW + 20 * MINUTE  # when the evidence received after NOW is visible too
SHARED, BY_KNOWN_PAYMENT, HELD_ORIGINAL, HELD_PAYMENT, ELSEWHERE, UNKNOWN_STATUS, FUTURE_ENVELOPE, AFTER_T = (
    "ord_unknown_shared", "ord_unknown_named_by_known_payment", "ord_unknown_original_side",
    "ord_unknown_original_payment", "ord_unknown_elsewhere", "ord_unknown_status_unknown",
    "ord_unknown_future_envelope", "ord_unknown_after_t",
)


def name_fresh_unknown_orders(conn: sqlite3.Connection, run: SeededRun, numbers: range) -> None:
    """One signed authorisation per number, naming an order and payment the run does not hold; every fifth event ID is
    then reused with other bytes, so a held conflict names that order too."""
    for number in numbers:
        event_id = f"evt_fresh_{number:04d}"
        fields: dict[str, object] = {"payment_id": f"pay_fresh_{number:04d}", "order_id": f"ord_fresh_{number:04d}",
                                     "status": AUTHORIZED, "occurred_at_ms": EVIDENCE_AT + number}
        assert deliver(conn, run, event_id, **fields).outcome is ACCEPTED  # type: ignore[arg-type]
        if number % 5 == 0:
            changed = {**fields, "amount_paise": AMOUNT + 1}
            assert deliver(conn, run, event_id, **changed).outcome is CONFLICTING  # type: ignore[arg-type]


def name_unknown_orders_every_way(conn: sqlite3.Connection, run: SeededRun) -> dict[str, int]:
    """Evidence naming orders the run does not hold, in every shape the old scan matched; returns the held conflicts."""

    def send(event_id: str, payment_id: str, order_id: str, *, at: int, status: PaymentStatus = CAPTURED,
             amount: int = AMOUNT, received_at: int | None = None,
             expected: ReceiveOutcome = ACCEPTED) -> ingestion.DeliveryResponse:
        response = deliver(conn, run, event_id, received_at_ms=received_at, payment_id=payment_id, order_id=order_id,
                           status=status, occurred_at_ms=at, amount_paise=amount)
        assert response.outcome is expected, (event_id, response)
        return response

    def held_by(response: ingestion.DeliveryResponse) -> int:
        assert response.conflicting_delivery_id is not None
        return response.conflicting_delivery_id

    held: dict[str, int] = {}
    # Two payments name one unknown order.
    send("evt_unknown_a", "pay_unknown_a", SHARED, at=EVIDENCE_AT, status=AUTHORIZED)
    send("evt_unknown_b", "pay_unknown_b", SHARED, at=EVIDENCE_AT + 1)
    # A payment of the batch's order 1 also names an unknown order.
    send("evt_unknown_c", batch_payment(1), BY_KNOWN_PAYMENT, at=EVIDENCE_AT + 2, amount=11_000)
    # A reused event ID: the original names one unknown order, the new body another.
    send("evt_unknown_d", "pay_unknown_d", HELD_ORIGINAL, at=EVIDENCE_AT + 3, status=AUTHORIZED)
    held["order and original order"] = held_by(
        send("evt_unknown_d", "pay_unknown_e", SHARED, at=EVIDENCE_AT + 4, expected=CONFLICTING))
    # The batch's order 6 event, reused with a body naming a payment of an unknown order: matched by payment alone.
    held["payment"] = held_by(
        send("evt_capture_6", "pay_unknown_b", batch_order(6), at=EVIDENCE_AT + 5, expected=CONFLICTING))
    # One payment named under two unknown orders; the second's event is reused, so the first matches by original
    # payment alone.
    send("evt_unknown_f", "pay_unknown_f", HELD_PAYMENT, at=EVIDENCE_AT + 6, status=AUTHORIZED)
    send("evt_unknown_g", "pay_unknown_f", ELSEWHERE, at=EVIDENCE_AT + 7, status=AUTHORIZED)
    held["original payment"] = held_by(
        send("evt_unknown_g", "pay_unknown_h", "ord_unknown_claimed", at=EVIDENCE_AT + 8, status=AUTHORIZED,
             expected=CONFLICTING))
    # A snapshot of unknown status: its payment's visible history names the order, its order_ids do not. A conflict
    # reaches the order through that payment.
    store_observations(conn, [observation(PaymentStatus.UNKNOWN, EVIDENCE_AT + 9, EVIDENCE_AT + 9,
                                          observation_id="obs_unknown_status", payment_id="pay_unknown_i",
                                          order_id=UNKNOWN_STATUS)])
    held["unknown-status payment"] = held_by(
        send("evt_unknown_a", "pay_unknown_i", "ord_unknown_claimed", at=EVIDENCE_AT + 10, status=AUTHORIZED,
             expected=CONFLICTING))
    # Received by NOW with an envelope time after it: the inbox row names the order, no visible observation does.
    send("evt_unknown_j", "pay_unknown_j", FUTURE_ENVELOPE, at=NOW + 10 * MINUTE, received_at=EVIDENCE_AT + 11)
    held["future envelope"] = held_by(
        send("evt_unknown_j", "pay_unknown_j", FUTURE_ENVELOPE, at=NOW + 10 * MINUTE, amount=AMOUNT + 1,
             received_at=EVIDENCE_AT + 12, expected=CONFLICTING))
    # Received after NOW: visible at LATER only.
    send("evt_unknown_k", "pay_unknown_k", AFTER_T, at=NOW + MINUTE)
    held["after NOW"] = held_by(
        send("evt_unknown_b", "pay_unknown_b", SHARED, at=EVIDENCE_AT + 1, amount=AMOUNT + 1, received_at=NOW + MINUTE,
             expected=CONFLICTING))
    return held


class CountedMapping[Key, Value](Mapping[Key, Value]):
    """A mapping that counts the entries its iterations yield: one full pass over n entries adds n to ``visits``."""

    def __init__(self, entries: Mapping[Key, Value], visits: Counter[str], kind: str) -> None:
        self._entries, self._visits, self._kind = entries, visits, kind

    def __getitem__(self, key: Key) -> Value:
        return self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Key]:
        for key in self._entries:
            self._visits[self._kind] += 1
            yield key


class CountedSequence[Item](Sequence[Item]):
    """A sequence that counts the items its iterations yield, as ``CountedMapping`` does."""

    def __init__(self, items: Sequence[Item], visits: Counter[str], kind: str) -> None:
        self._items, self._visits, self._kind = items, visits, kind

    def __getitem__(self, index: int) -> Item:  # type: ignore[override]  # never sliced here
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Item]:
        for item in self._items:
            self._visits[self._kind] += 1
            yield item


def visits_while_classifying(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> tuple[Counter[str], order_cases.OrderQueue]:
    """The queue at NOW, counting each visit to a payment of the run's view and to a held conflict, wherever the
    classification iterates them: the view's payments, the conflicts read from storage, and the context's map."""
    visits: Counter[str] = Counter()
    load_run_view, list_conflicts, run_context = (
        metrics.load_run_view, storage.list_conflicting_deliveries, order_cases._RunContext)

    def counted_view(*args: object, **kwargs: object) -> metrics.RunView:
        view = load_run_view(*args, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(view, payments=CountedMapping(view.payments, visits, "payments"))

    def counted_conflicts(*args: object, **kwargs: object) -> Sequence[storage.ConflictingDeliveryRecord]:
        return CountedSequence(list_conflicts(*args, **kwargs), visits, "conflicts")  # type: ignore[arg-type]

    def counted_context(**fields: object) -> order_cases._RunContext:
        conflicts = CountedMapping(fields["conflicts"], visits, "conflicts")  # type: ignore[arg-type]
        return run_context(**{**fields, "conflicts": conflicts})  # type: ignore[arg-type]

    with monkeypatch.context() as patched:
        patched.setattr(metrics, "load_run_view", counted_view)
        patched.setattr(storage, "list_conflicting_deliveries", counted_conflicts)
        patched.setattr(order_cases, "_RunContext", counted_context)
        queue = order_cases.classify_orders(conn, run_id=RUN_A, as_of_ms=NOW, stale_after_ms=STALE_AFTER_MS)
    return visits, queue


def test_the_queue_visits_each_payment_and_held_conflict_as_often_however_many_unknown_orders_there_are(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = build_batch(conn)
    passes = {}
    for start, stop in ((0, UNKNOWN_SMALL), (UNKNOWN_SMALL, UNKNOWN_LARGE)):
        name_fresh_unknown_orders(conn, batch.run, range(start, stop))
        visits, queue = visits_while_classifying(conn, monkeypatch)
        conflicts = len(storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=NOW))
        assert queue.coverage.unknown_orders == stop + 1  # and the batch's own event for an unknown order
        passes[stop] = (visits["payments"] / queue.coverage.payments, visits["conflicts"] / conflicts)
    assert passes[UNKNOWN_LARGE] == passes[UNKNOWN_SMALL], (
        f"passes over (payments, held conflicts): {passes[UNKNOWN_SMALL]} with {UNKNOWN_SMALL} unknown orders, "
        f"{passes[UNKNOWN_LARGE]} with {UNKNOWN_LARGE}"
    )


def test_the_queue_equals_the_queue_classified_with_the_old_scan_per_unknown_order(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = build_batch(conn)
    held = name_unknown_orders_every_way(conn, batch.run)
    for at in (NOW, LATER):
        queue = order_cases.classify_orders(conn, run_id=RUN_A, as_of_ms=at, stale_after_ms=STALE_AFTER_MS)
        with monkeypatch.context() as old:
            old.setattr(order_cases, "_unknown_order_projection", unknown_order_projection_the_old_way)
            reference = order_cases.classify_orders(conn, run_id=RUN_A, as_of_ms=at, stale_after_ms=STALE_AFTER_MS)
        assert queue == reference, at
    # The evidence reaches each unknown order the way its comment says.
    at_now = {case.order_id: case for case in
              order_cases.classify_orders(conn, run_id=RUN_A, as_of_ms=NOW, stale_after_ms=STALE_AFTER_MS).cases}
    assert {order_id: list(at_now[order_id].evidence.conflicting_delivery_ids) for order_id in (
        SHARED, BY_KNOWN_PAYMENT, HELD_ORIGINAL, HELD_PAYMENT, ELSEWHERE, UNKNOWN_STATUS, FUTURE_ENVELOPE)} == {
        SHARED: sorted([held["order and original order"], held["payment"], held["unknown-status payment"]]),
        BY_KNOWN_PAYMENT: [],
        HELD_ORIGINAL: [held["order and original order"]],
        HELD_PAYMENT: [held["original payment"]],
        ELSEWHERE: [held["original payment"]],
        UNKNOWN_STATUS: [held["unknown-status payment"]],
        FUTURE_ENVELOPE: [held["future envelope"]],
    }
    assert not any(at_now[order_id].order_known for order_id in (SHARED, UNKNOWN_STATUS, FUTURE_ENVELOPE))
    assert at_now[FUTURE_ENVELOPE].evidence.payment_ids == ()
    assert AFTER_T not in at_now


def test_every_order_projection_equals_the_projection_read_the_old_way(conn: sqlite3.Connection) -> None:
    batch = build_batch(conn)
    name_unknown_orders_every_way(conn, batch.run)
    for at in (NOW, LATER):
        queue = order_cases.classify_orders(conn, run_id=RUN_A, as_of_ms=at, stale_after_ms=STALE_AFTER_MS)
        for order_id in [*(case.order_id for case in queue.cases), "ord_named_by_nothing"]:
            projection = state.load_order_projection(conn, RUN_A, order_id, at, stale_after_ms=STALE_AFTER_MS)
            reference = order_projection_the_old_way(conn, RUN_A, order_id, at, stale_after_ms=STALE_AFTER_MS)
            assert projection == reference, (order_id, at)
        unknown = [case for case in queue.cases if not case.order_known]
        assert len(unknown) >= 8
        for case in unknown:  # the queue's in-memory projection agrees with the projection read from storage
            projected = state.load_order_projection(conn, RUN_A, case.order_id, at, stale_after_ms=STALE_AFTER_MS)
            assert case.evidence.conflicting_delivery_ids == projected.conflicting_delivery_ids, case.order_id


def test_the_queue_and_case_routes_answer_as_they_did_with_the_old_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path / "data")
    seed_unit_run(app)
    sends = (  # (delivery, the answer)
        (signed("evt_api_1", payment_id="pay_api_1", order_id="ord_api_1", status=AUTHORIZED), "accepted"),
        (signed("evt_api_1", payment_id="pay_api_1", order_id="ord_api_1", status=AUTHORIZED), "duplicate"),
        (signed("evt_api_1", payment_id="pay_api_1", order_id="ord_api_1", status=AUTHORIZED), "duplicate"),
        (signed("evt_api_2", payment_id="pay_api_2", order_id="ord_api_1"), "accepted"),
        (signed("evt_api_2", payment_id="pay_api_3", order_id="ord_api_3"), "conflicting_id"),
        (signed("evt_api_4", payment_id=PAY_1, order_id="ord_api_4"), "accepted"),
        (({}, b'{"probe": "unsigned"}'), "rejected_signature"),
        (({}, b'{"probe": "unsigned"}'), "rejected_signature"),
    )

    def documents(client: TestClient) -> dict[str, dict]:
        path = f"/api/runs/{RUN_ID}/orders"
        queue = client.get(f"{path}?limit={api.MAX_QUEUE_PAGE_LIMIT}").json()
        cases = {row["order_id"]: client.get(f"{path}/{row['order_id']}").json() for row in queue["orders"]}
        return {"queue": queue, **cases}

    try:
        with TestClient(app) as client:
            answers = [client.post(webhook_path(RUN_ID), content=body, headers=headers).json()["outcome"]
                       for (headers, body), _ in sends]
            assert answers == [answer for _, answer in sends]
            log_in(client)
            now = documents(client)
            with monkeypatch.context() as old:
                old.setattr(order_cases, "_unknown_order_projection", unknown_order_projection_the_old_way)
                assert documents(client) == now
    finally:
        app.state.workbench.shutdown()
    assert set(now) >= {"ord_api_1", "ord_api_4", ORDER_1}
    assert not now["ord_api_1"]["case"]["order_known"]
    # Attempt numbers reach the case's timeline as the count gave them: 1, 2, 3 per event ID.
    attempts = [(entry["facts"]["event_id"], entry["facts"]["attempt_no"]) for entry in now["ord_api_1"]["timeline"]
                if entry["kind"] == "delivery_attempt"]
    assert attempts == [("evt_api_1", 1), ("evt_api_1", 2), ("evt_api_1", 3), ("evt_api_2", 1), ("evt_api_2", 2)]
    assert len(now["ord_api_1"]["case"]["evidence"]["conflicting_delivery_ids"]) == 1
