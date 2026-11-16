"""Shared fixtures and helpers for the core (inbox, state, worker) tests.

Every database here is ephemeral: a file under pytest's ``tmp_path`` or ``:memory:``.
Helpers are plain functions so tests can ``from conftest import ...`` them.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue

from recovery_workbench import ingestion, storage
from recovery_workbench.contracts import (
    FailureReport,
    ModelMode,
    ObservationSource,
    PaymentObservation,
    PaymentStatus,
)
from recovery_workbench.simulator import signing

RUN_A = "run_unit_a"
RUN_B = "run_unit_b"
MERCHANT_A = "merchant_SYNTHETIC_A"
MERCHANT_B = "merchant_SYNTHETIC_B"
INTEGRATION_A = "int_SYNTHETIC_A"
INTEGRATION_B = "int_SYNTHETIC_B"
SECRET_A = "fixture_secret_a"
SECRET_B = "fixture_secret_b"
START_MS = 1_794_387_600_000  # replay epoch for the tests; any fixed instant works
ORDER_1 = signing.synthetic_id("order", 1)
ORDER_2 = signing.synthetic_id("order", 2)
PAY_1 = signing.synthetic_id("pay", 1)
PAY_2 = signing.synthetic_id("pay", 2)
AMOUNT = 120_000  # paise


@dataclass(frozen=True)
class SeededRun:
    run_id: str
    merchant_id: str
    integration_id: str
    secret_ref: str


def seed_run(
    conn: sqlite3.Connection,
    run_id: str,
    merchant_id: str,
    integration_id: str,
    secret_ref: str,
    *,
    max_body_bytes: int = 65_536,
) -> SeededRun:
    with storage.write_transaction(conn):
        storage.create_run(
            conn,
            storage.RunRecord(
                run_id=run_id, seed=7, scenario="unit", split="unit", merchant_id=merchant_id,
                generator_version="unit-tests", clock_ms=START_MS, model_mode=ModelMode.RULES,
            ),
        )
        storage.add_integration(
            conn,
            storage.IntegrationRecord(
                run_id=run_id, integration_id=integration_id, secret_ref=secret_ref, max_body_bytes=max_body_bytes
            ),
        )
    return SeededRun(run_id, merchant_id, integration_id, secret_ref)


def add_order(
    conn: sqlite3.Connection,
    run: SeededRun,
    order_id: str = ORDER_1,
    *,
    payment_ids: Sequence[str] = (PAY_1,),
    amount_paise: int = AMOUNT,
    currency: str = "INR",
    cancelled_at_ms: int | None = None,
    refunded: bool = False,
    created_at_ms: int = START_MS,
) -> storage.OrderRecord:
    """An order of the run's merchant plus one attempt row per payment ID."""
    with storage.write_transaction(conn):
        order = storage.insert_order(
            conn,
            storage.OrderRecord(
                run_id=run.run_id, order_id=order_id, merchant_id=run.merchant_id, created_at_ms=created_at_ms,
                amount_paise=amount_paise, currency=currency, allowed_methods=("upi", "card"),
                cancelled_at_ms=cancelled_at_ms, refunded=refunded,
            ),
        )
        for payment_id in payment_ids:
            storage.insert_payment_attempt(
                conn,
                storage.PaymentAttemptRecord(
                    run_id=run.run_id, payment_id=payment_id, order_id=order_id, method="upi",
                    created_at_ms=created_at_ms,
                ),
            )
    return order


def payment_event(
    *,
    status: PaymentStatus = PaymentStatus.CAPTURED,
    payment_id: str = PAY_1,
    order_id: str = ORDER_1,
    occurred_at_ms: int = START_MS + 1_000,
    amount_paise: int = AMOUNT,
    currency: str = "INR",
    **extra: object,
) -> dict[str, JsonValue]:
    return signing.build_payment_event(
        payment_id=payment_id, order_id=order_id, amount_paise=amount_paise, currency=currency, status=status,
        occurred_at_ms=occurred_at_ms, payment_created_at_ms=START_MS, **extra,  # type: ignore[arg-type]
    )


def deliver(
    conn: sqlite3.Connection,
    run: SeededRun,
    event_id: str,
    *,
    received_at_ms: int | None = None,
    secret_ref: str | None = None,
    integration_id: str | None = None,
    **event_fields: object,
) -> ingestion.DeliveryResponse:
    """Build, sign and receive one payment event; received 50 ms after it occurred by default."""
    envelope = payment_event(**event_fields)  # type: ignore[arg-type]
    headers, raw_body = signing.signed_delivery(envelope, event_id=event_id, secret_ref=secret_ref or run.secret_ref)
    occurred_at_ms = int(event_fields.get("occurred_at_ms", START_MS + 1_000))  # type: ignore[call-overload]
    return ingestion.receive_webhook(
        conn, run_id=run.run_id, integration_id=integration_id or run.integration_id, headers=headers,
        raw_body=raw_body, received_at_ms=received_at_ms if received_at_ms is not None else occurred_at_ms + 50,
    )


def observation(
    status: PaymentStatus,
    occurred_at_ms: int,
    known_at_ms: int,
    *,
    observation_id: str | None = None,
    payment_id: str = PAY_1,
    order_id: str = ORDER_1,
    amount_paise: int = AMOUNT,
    currency: str = "INR",
    amount_refunded_paise: int = 0,
    source: ObservationSource = ObservationSource.SNAPSHOT,
    run_id: str = RUN_A,
) -> PaymentObservation:
    name = observation_id or f"obs_{status.value}_{occurred_at_ms}_{known_at_ms}"
    return PaymentObservation(
        run_id=run_id, observation_id=name, payment_id=payment_id, order_id=order_id, status=status,
        amount_paise=amount_paise, currency=currency, method="upi", source=source, source_ref=name,
        occurred_at_ms=occurred_at_ms, known_at_ms=known_at_ms, amount_refunded_paise=amount_refunded_paise,
    )


def store_observations(conn: sqlite3.Connection, observations: Sequence[PaymentObservation]) -> None:
    with storage.write_transaction(conn):
        for item in observations:
            storage.insert_observation(conn, item)


def count_rows(conn: sqlite3.Connection, table: str, run_id: str) -> int:
    if table not in storage.TABLES:
        raise ValueError(f"{table!r} is not a workbench table")
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)).fetchone()[0])


def business_state(conn: sqlite3.Connection, run_id: str) -> dict[str, object]:
    """Everything a delivery or worker step could change in business terms."""
    return {
        "inbox": count_rows(conn, "webhook_inbox", run_id),
        "observations": count_rows(conn, "payment_observations", run_id),
        "conflicts": count_rows(conn, "conflicting_deliveries", run_id),
        "fulfilments": count_rows(conn, "fulfilments", run_id),
        "orders": [order.model_dump() for order in storage.list_orders(conn, run_id=run_id)],
    }


def fulfil_reply(entity: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "action": "fulfil",
        "order_id": entity["order_id"],
        "payment_id": entity["id"],
        "amount_paise": entity["amount"],
        "currency": entity["currency"],
    }


class FakeHandler:
    """Stand-in merchant handler: fulfils captured events and ignores the rest.

    ``failure`` makes ``handle`` raise; ``reply`` replaces the decision.
    """

    def __init__(
        self,
        version: str = "deploy_SYNTHETIC_0001",
        *,
        failure: BaseException | None = None,
        reply: Callable[[Mapping[str, JsonValue]], object] | None = None,
    ) -> None:
        self._version = version
        self.failure = failure
        self.reply = reply

    @property
    def version(self) -> str:
        return self._version

    def handle(self, event: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        entity = event["payload"]["payment"]["entity"]  # type: ignore[index]
        if self.failure is not None:
            raise self.failure
        if self.reply is not None:
            return self.reply(entity)  # type: ignore[arg-type, return-value]
        if entity["status"] != "captured":  # type: ignore[index]
            return {"action": "ignore"}
        return fulfil_reply(entity)  # type: ignore[arg-type]

    def describe_failure(self, exc: BaseException) -> FailureReport:
        return FailureReport(
            exception_category=type(exc).__name__,
            stack_trace=(
                "Traceback (most recent call last):\n"
                '  File "merchant_app/payload.py", line 15, in parse_event\n'
                '    entity = event["payload"]["payment"]["entity"]\n'
                f"{type(exc).__name__}: {exc}"
            ),
        )


class FailingControlConnection:
    """Delegates to a real connection but fails one transaction-control statement (e.g. COMMIT)."""

    def __init__(self, inner: sqlite3.Connection, *, fail_on: str) -> None:
        self._inner = inner
        self._fail_on = fail_on

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        if sql.strip().upper() == self._fail_on:
            raise sqlite3.OperationalError(f"injected {self._fail_on} failure")
        return self._inner.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "workbench.sqlite"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = storage.connect(db_path)
    storage.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def open_connection(db_path: Path) -> Iterator[Callable[[], sqlite3.Connection]]:
    """Extra main-thread connections to the same file; threads must open and close their own."""
    opened: list[sqlite3.Connection] = []

    def factory() -> sqlite3.Connection:
        connection = storage.connect(db_path)
        opened.append(connection)
        return connection

    yield factory
    for connection in opened:
        connection.close()


@pytest.fixture
def run_a(conn: sqlite3.Connection) -> SeededRun:
    return seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)


@pytest.fixture
def run_b(conn: sqlite3.Connection) -> SeededRun:
    return seed_run(conn, RUN_B, MERCHANT_B, INTEGRATION_B, SECRET_B)
