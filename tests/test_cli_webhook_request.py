"""``webhook-request``: one signed, provider-shaped delivery for an existing payment, checked by the real receiver.

The helper only reads the database and sends nothing; these tests hand its body and headers to
``ingestion.receive_webhook`` in a throwaway database. ``tests/test_api_webhooks.py`` sends them over HTTP.
"""
from __future__ import annotations

import json
import shlex
import socket
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pytest
from conftest import MERCHANT_A, PAY_1, add_order, seed_run

from recovery_workbench import cli, ingestion, storage
from recovery_workbench.contracts import ReceiveOutcome
from recovery_workbench.simulator import driver, merchant, scenarios, signing
from recovery_workbench.simulator.world import INTEGRATION_ID, generate_world

NO_OVERRIDES: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class Seeded:
    data_dir: Path
    run_id: str
    payment: storage.PaymentAttemptRecord
    order: storage.OrderRecord

    @property
    def database(self) -> Path:
        return self.data_dir / cli.DATABASE_FILE


@dataclass(frozen=True)
class Printed:
    headers: dict[str, str]
    curl: list[str]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Seeded:
    """A generated demo run; the tests read it and add only their own deliveries."""
    data_dir = tmp_path_factory.mktemp("webhook_request")
    with closing(storage.connect(data_dir / cli.DATABASE_FILE)) as conn:
        summary = driver.seed_world(conn, generate_world(scenarios.demo_scenario("worker_backlog"), 101),
                                    oracle_dir=None)
        payment = storage.list_run_payment_attempts(conn, run_id=summary.run_id, as_of_ms=None)[0]
        order = storage.require_order(conn, run_id=summary.run_id, order_id=payment.order_id)
    return Seeded(data_dir, summary.run_id, payment, order)


def helper(seeded: Seeded, output: Path, overrides: Mapping[str, str] = NO_OVERRIDES) -> list[str]:
    options = {"--data-dir": str(seeded.data_dir), "--run-id": seeded.run_id, "--payment-id": seeded.payment.payment_id,
               "--status": "captured", "--event-id": "evt_cli_0001", "--output": str(output), **overrides}
    return ["webhook-request", *(word for option in options.items() for word in option)]


def printed(capsys: pytest.CaptureFixture[str]) -> Printed:
    lines = capsys.readouterr().out.splitlines()
    headers = dict(line.split(": ", 1) for line in lines if line.startswith("X-Webhook-"))
    [command] = [line for line in lines if line.startswith("curl ")]
    return Printed(headers, shlex.split(command))


def receive(conn: sqlite3.Connection, run_id: str, headers: Mapping[str, str], raw_body: bytes) -> ReceiveOutcome:
    clock_ms = storage.require_run(conn, run_id).clock_ms
    return ingestion.receive_webhook(conn, run_id=run_id, integration_id=INTEGRATION_ID, headers=headers,
                                     raw_body=raw_body, received_at_ms=clock_ms).outcome


def database_state(
    database: Path, run_id: str
) -> tuple[dict[str, int], storage.RunRecord, tuple[storage.IntegrationRecord, ...]]:
    with closing(storage.connect(database)) as conn:
        return (driver.run_counts(conn, run_id), storage.require_run(conn, run_id),
                storage.list_integrations(conn, run_id=run_id))


# --------------------------------------------------------------------------- the request it builds


def test_the_printed_request_gets_each_documented_outcome_from_the_real_receiver(
    seeded: Seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    first, reused = tmp_path / "captured.json", tmp_path / "failed.json"
    assert cli.main(helper(seeded, first, {"--event-id": "evt_cli_walk_0001"})) == 0
    sent = printed(capsys)
    assert cli.main(helper(seeded, reused, {"--event-id": "evt_cli_walk_0001", "--status": "failed"})) == 0
    reused_id = printed(capsys)
    body = first.read_bytes()
    with closing(storage.connect(seeded.database)) as conn:
        outcomes = [
            receive(conn, seeded.run_id, sent.headers, body),  # send once
            receive(conn, seeded.run_id, sent.headers, body),  # send twice
            receive(conn, seeded.run_id, sent.headers, body[:-1] + bytes([body[-1] ^ 1])),  # one byte edited
            receive(conn, seeded.run_id, reused_id.headers, reused.read_bytes()),  # same event ID, other status
        ]
        [held] = [item for item in storage.list_conflicting_deliveries(conn, run_id=seeded.run_id, as_of_ms=None)
                  if item.event_id == "evt_cli_walk_0001"]
    assert outcomes == [ReceiveOutcome.ACCEPTED, ReceiveOutcome.DUPLICATE, ReceiveOutcome.REJECTED_SIGNATURE,
                        ReceiveOutcome.CONFLICTING_ID]
    assert held.differing_fields == ("status",)


@pytest.mark.parametrize("status", cli.WEBHOOK_STATUSES)
def test_the_event_carries_the_payment_its_order_amount_and_the_run_clock(
    seeded: Seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str], status: str
):
    output = tmp_path / "body.json"
    assert cli.main(helper(seeded, output, {"--status": status, "--event-id": f"evt_cli_{status}"})) == 0
    sent = printed(capsys)
    body = output.read_bytes()
    event = ingestion.parse_payment_event(body)  # the receiver's own parser accepts every status the helper offers
    with closing(storage.connect(seeded.database)) as conn:
        clock_ms = storage.require_run(conn, seeded.run_id).clock_ms
    assert event.payment.id == seeded.payment.payment_id
    assert event.payment.order_id == seeded.order.order_id
    assert event.payment.amount == seeded.order.amount_paise
    assert event.payment.currency == seeded.order.currency
    assert event.payment.method == seeded.payment.method
    assert event.payment.status == status
    assert event.occurred_at_ms == clock_ms
    assert sent.headers == {signing.EVENT_ID_HEADER: f"evt_cli_{status}",
                            signing.SIGNATURE_HEADER: signing.sign_body(body, merchant.PROVIDER_SIGNING_REF)}


def test_the_curl_command_posts_the_file_unchanged_to_the_runs_webhook_route(
    seeded: Seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    output = tmp_path / "body.json"
    assert cli.main(helper(seeded, output, {"--port": "8123"})) == 0
    sent = printed(capsys)
    words = sent.curl
    headers = [words[index + 1] for index, word in enumerate(words) if word == "--header"]
    assert words[0] == "curl"
    assert words[words.index("--data-binary") + 1] == f"@{output.resolve()}"  # --data-binary keeps every byte
    assert headers == ["Content-Type: application/json", *(f"{name}: {value}" for name, value in sent.headers.items())]
    assert words[-1] == f"http://127.0.0.1:8123/api/runs/{seeded.run_id}/webhooks/{INTEGRATION_ID}"


def test_the_signature_is_the_providers_whatever_secret_the_receiver_verifies_with(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with closing(storage.connect(data_dir / cli.DATABASE_FILE)) as conn:
        storage.migrate(conn)
        add_order(conn, seed_run(conn, "run_unit_rotated", MERCHANT_A, INTEGRATION_ID, "fixture_secret_b"))
    arguments = ["webhook-request", "--data-dir", str(data_dir), "--run-id", "run_unit_rotated", "--payment-id", PAY_1,
                 "--status", "captured", "--event-id", "evt_cli_rotated_0001", "--output", str(tmp_path / "body.json")]
    assert cli.main(arguments) == 0
    sent = printed(capsys)
    body = (tmp_path / "body.json").read_bytes()
    assert sent.headers[signing.SIGNATURE_HEADER] == signing.sign_body(body, merchant.PROVIDER_SIGNING_REF)
    with closing(storage.connect(data_dir / cli.DATABASE_FILE)) as conn:
        # This receiver verifies with another secret, so it refuses the helper's request as it would the provider's.
        assert receive(conn, "run_unit_rotated", sent.headers, body) is ReceiveOutcome.REJECTED_SIGNATURE


# --------------------------------------------------------------------------- what it refuses and what it never does


@pytest.mark.parametrize(("option", "unknown"), [
    ("--run-id", "run_does_not_exist"),
    ("--payment-id", "pay_NOT_IN_THIS_RUN"),
    ("--integration-id", "int_SYNTHETIC_OTHER"),
], ids=["run", "payment", "integration"])
def test_an_unknown_run_payment_or_integration_is_a_clear_error_and_writes_nothing(
    seeded: Seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str], option: str, unknown: str
):
    output = tmp_path / "body.json"
    assert cli.main(helper(seeded, output, {option: unknown})) == cli.EXIT_USAGE_ERROR
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "ScopeError"
    assert unknown in error["detail"]
    assert not output.exists()


def test_an_unknown_payment_error_names_payments_that_exist(
    seeded: Seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert cli.main(helper(seeded, tmp_path / "body.json", {"--payment-id": "pay_NOT_IN_THIS_RUN"})) == cli.EXIT_USAGE_ERROR
    assert seeded.payment.payment_id in json.loads(capsys.readouterr().err)["detail"]


def test_an_event_id_the_receiver_would_refuse_is_refused_before_anything_is_written(
    seeded: Seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    output = tmp_path / "body.json"
    arguments = helper(seeded, output, {"--event-id": "evt with spaces"})
    with pytest.raises(SystemExit) as exited:
        cli.main(arguments)
    assert exited.value.code == 2
    assert "not an event ID the receiver accepts" in capsys.readouterr().err
    assert not output.exists()


def test_the_helper_only_reads_the_database_and_opens_no_network_connection(
    seeded: Seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    def no_network(*_: object, **__: object) -> None:
        raise AssertionError("webhook-request must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    before = database_state(seeded.database, seeded.run_id)
    assert cli.main(helper(seeded, tmp_path / "body.json", {"--event-id": "evt_cli_read_0001"})) == 0
    assert printed(capsys).headers[signing.EVENT_ID_HEADER] == "evt_cli_read_0001"
    assert database_state(seeded.database, seeded.run_id) == before


def test_the_help_says_how_to_get_each_outcome(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exited:
        cli.main(["webhook-request", "--help"])
    assert exited.value.code == 0
    text = capsys.readouterr().out
    for phrase in ("accepted", "duplicate", "change one byte", "401", "same --event-id", "conflicting",
                   "never processed", "sends nothing", "provider's secret", "another secret"):
        assert phrase in text
