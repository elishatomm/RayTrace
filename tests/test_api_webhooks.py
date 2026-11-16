"""The webhook route (SPEC §11, owner answer Q10): HTTP deliveries take the in-process path of ``driver.deliver``.

Every test builds its own app on a throwaway data directory. Most use a hand-made run whose replay
clock stands at ``CLOCK_MS``. The deployed-receiver test seeds D3 worlds from the evaluation manifest,
and the replay-time tests seed the worker_backlog demo run, which has an incident to diagnose. A
delivery that reaches the database moves the run's clock one millisecond and is received at that new
time. No test runs the worker or calls a model. The only network connections are on 127.0.0.1, where
the last section serves the app with uvicorn, as ``api.main`` does.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import socket
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from conftest import PAY_1, START_MS, add_order, count_rows, payment_event, seed_run
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from uvicorn.protocols.http.auto import AutoHTTPProtocol
from uvicorn.protocols.http.h11_impl import H11Protocol

from recovery_workbench import api, cli, ingestion, packet, settings, storage
from recovery_workbench.contracts import InboxStatus, PaymentStatus, ReceiveOutcome
from recovery_workbench.simulator import driver, families, merchant, runtime, scenarios, signing
from recovery_workbench.simulator.world import INTEGRATION_ID, generate_world

TOKEN = "test-operator-token-not-a-secret"
OPERATOR = "op_webhook_tester"
RUN_ID = "run_unit_webhooks"
MERCHANT_ID = "merchant_SYNTHETIC_A"
CLOCK_MS = START_MS + 60_000  # the run's replay clock before any delivery arrives
STEP = api.RECEIPT_CLOCK_STEP_MS
WEBHOOK = f"/api/runs/{RUN_ID}/webhooks/{INTEGRATION_ID}"
UI_HEADER = {api.REQUEST_HEADER: "1"}
EVENT_ID = signing.EVENT_ID_HEADER
SIGNATURE = signing.SIGNATURE_HEADER
OTHER_SECRET = "fixture_secret_a"  # a provisioned fixture secret the run's integration does not verify with
CEILING = ingestion.MAX_BODY_BYTES_CEILING
CHUNK_BYTES = 65_536
RETRY_AFTER = "retry-after"
SCOPE_ERROR = "ScopeError"
NOT_STORED = "DeliveryNotStoredError"
# Past the JSON parser's own nesting limit (not sys.getrecursionlimit()), yet a 50 kB body, under the size cap.
TOO_DEEP = 25_000
# The webhook route and every other route in a method the request guard checks (any but api.SAFE_METHODS), as
# declared; PATH_PARAMETERS fills the templates in.
WEBHOOK_ROUTE = ("POST", "/api/runs/{run_id}/webhooks/{integration_id}")
OTHER_STATE_CHANGING_ROUTES = (
    ("POST", "/api/session"),
    ("DELETE", "/api/session"),
    ("POST", "/api/runs"),
    ("POST", "/api/runs/{run_id}/advance"),
    ("POST", "/api/runs/{run_id}/diagnoses"),
    ("POST", "/api/runs/{run_id}/fixes/{fix_id}/validate"),
    ("POST", "/api/runs/{run_id}/plans"),
    ("POST", "/api/runs/{run_id}/plans/{plan_id}/approve"),
    ("POST", "/api/runs/{run_id}/plans/{plan_id}/execute"),
    ("POST", "/api/runs/{run_id}/refresh"),
)
PATH_PARAMETERS = {"run_id": RUN_ID, "integration_id": INTEGRATION_ID, "fix_id": "payload@5", "plan_id": "plan_none"}
# Tab, LF and CR, percent-encoded: urllib.parse drops them, so request.url.path loses them and the router does not.
CONTROL_CHARACTERS = ("%09", "%0a", "%0d")


# --------------------------------------------------------------------------- helpers


def make_app(data_dir: Path, *, max_deliveries: int = api.DEFAULT_MAX_WEBHOOK_DELIVERIES_PER_MINUTE) -> FastAPI:
    return api.create_app(
        api.ApiConfig(data_dir=data_dir, operator_token=TOKEN, allowed_hosts=frozenset({"testserver"}),
                      allowed_origins=frozenset({"http://testserver"}), ui_dir=None,
                      max_webhook_deliveries_per_minute=max_deliveries),
        settings.load_settings({"WORKBENCH_MODEL_MODE": "rules"}),
    )


def database(app: FastAPI) -> Path:
    return app.state.workbench.database


def seed_unit_run(app: FastAPI) -> None:
    """One order with one payment, the baseline merchant receiver, and the replay clock at CLOCK_MS."""
    with closing(storage.connect(database(app))) as conn:
        run = seed_run(conn, RUN_ID, MERCHANT_ID, INTEGRATION_ID, merchant.PROVIDER_SIGNING_REF)
        add_order(conn, run)
        with storage.write_transaction(conn):
            storage.set_merchant_profile(conn, run_id=RUN_ID, emits_categories=True)
            storage.advance_clock(conn, RUN_ID, CLOCK_MS)


def seed_backlog(app: FastAPI) -> str:
    """The worker_backlog demo run (seed 101), replayed to its cutoff; it has an incident to diagnose."""
    with closing(storage.connect(database(app))) as conn:
        return driver.seed_world(conn, generate_world(scenarios.WORKER_BACKLOG, 101), oracle_dir=None).run_id


def webhook_path(run_id: str) -> str:
    return f"/api/runs/{run_id}/webhooks/{INTEGRATION_ID}"


def signed(event_id: str, **fields: object) -> tuple[dict[str, str], bytes]:
    """A provider-shaped event, signed the way the simulated provider signs in ``driver.deliver``."""
    return signing.signed_delivery(payment_event(**fields), event_id=event_id,  # type: ignore[arg-type]
                                   secret_ref=merchant.PROVIDER_SIGNING_REF)


def payment_delivery(
    app: FastAPI, run_id: str, event_id: str, *, secret_ref: str = merchant.PROVIDER_SIGNING_REF
) -> tuple[dict[str, str], bytes]:
    """A captured event for the run's newest payment, occurring at the run's clock, signed with ``secret_ref``."""
    with closing(storage.connect(database(app))) as conn:
        payment = storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=None)[-1]
        order = storage.require_order(conn, run_id=run_id, order_id=payment.order_id)
        clock_ms = storage.require_run(conn, run_id).clock_ms
    envelope = signing.build_payment_event(
        payment_id=payment.payment_id, order_id=order.order_id, amount_paise=order.amount_paise,
        status=PaymentStatus.CAPTURED, occurred_at_ms=clock_ms, payment_created_at_ms=payment.created_at_ms,
        method=payment.method,
    )
    return signing.signed_delivery(envelope, event_id=event_id, secret_ref=secret_ref)


def rows(app: FastAPI, table: str, run_id: str = RUN_ID) -> int:
    with closing(storage.connect(database(app))) as conn:
        return count_rows(conn, table, run_id)


def delivery_attempts(app: FastAPI, run_id: str = RUN_ID) -> tuple[storage.DeliveryAttemptRecord, ...]:
    with closing(storage.connect(database(app))) as conn:
        return storage.list_delivery_attempts(conn, run_id=run_id)


def replay_clock(app: FastAPI, run_id: str = RUN_ID) -> int:
    with closing(storage.connect(database(app))) as conn:
        return storage.require_run(conn, run_id).clock_ms


def advance_clock(app: FastAPI, *, by_ms: int) -> None:
    with closing(storage.connect(database(app))) as conn, storage.write_transaction(conn):
        storage.advance_clock(conn, RUN_ID, storage.require_run(conn, RUN_ID).clock_ms + by_ms)


def packet_hash_at(app: FastAPI, run_id: str, cutoff_ms: int, *, incident_id: str | None = None,
                   persist: bool = False) -> str:
    """The evidence packet's hash at ``cutoff_ms``; with ``persist``, the packet store compares it with stored evidence."""
    with closing(storage.connect(database(app))) as conn:
        incident = incident_id or storage.list_incidents(conn, run_id=run_id)[-1].incident_id
        built = packet.build_packet(conn, run_id=run_id, incident_id=incident, cutoff_ms=cutoff_ms,
                                    deployed=runtime.active_merchant(conn, run_id=run_id, at_ms=cutoff_ms),
                                    persist=persist)
    return built.packet_hash()


def log_in(client: TestClient) -> None:
    response = client.post("/api/session", json={"token": TOKEN, "operator_id": OPERATOR}, headers=UI_HEADER)
    assert response.status_code == 200, response.text


def finished_job(client: TestClient, run_id: str, job: dict) -> dict:
    deadline = time.monotonic() + 30
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, job
        time.sleep(0.05)
        job = client.get(f"/api/runs/{run_id}/diagnoses/{job['job_id']}").json()
    return job


def post_streamed(
    app: FastAPI, path: str, headers: dict[str, str], chunks: Iterable[bytes]
) -> tuple[httpx.Response, int]:
    """POST a chunked body that the app pulls one chunk at a time; also return how many bytes it pulled."""
    pulled = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal pulled
        for chunk in chunks:
            pulled += len(chunk)
            yield chunk

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, content=body(), headers=headers)

    response = asyncio.run(send())
    return response, pulled


def has_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def nested_event(event_id: str, levels: int) -> tuple[dict[str, str], bytes]:
    """A signed provider-shaped event whose ignored extra field nests ``levels`` arrays; the envelope is one more."""
    body = signing.serialise_event(payment_event())[:-1] + b',"x_extra":' + b"[" * levels + b"]" * levels + b"}"
    return {EVENT_ID: event_id, SIGNATURE: signing.sign_body(body, merchant.PROVIDER_SIGNING_REF)}, body


def with_control_characters(path: str) -> list[str]:
    """``path`` with each control character inserted in the middle of each segment in turn, and at the end."""
    segments = path.split("/")
    variants: list[str] = []
    for code in CONTROL_CHARACTERS:
        for index, segment in enumerate(segments[1:], start=1):
            middle = len(segment) // 2
            changed = segment[:middle] + code + segment[middle:]
            variants.append("/".join([*segments[:index], changed, *segments[index + 1:]]))
        variants.append(path + code)
    return variants


def refused_by_the_guard(response: httpx.Response) -> bool:
    """The request guard's refusal of a state change that lacks the UI header, not the router's 404 or 422."""
    return response.status_code == 403 and api.REQUEST_HEADER in str(response.json()["detail"])


class ObservedClockLock:
    """Stands in for ``Workbench.clock_lock`` and shows which threads reach it and which get past it.

    The test holds ``held``, the lock itself, directly. A thread taking this one signals ``reached``
    immediately before it waits for ``held``, and counts itself in ``passed`` once it has it.
    """

    def __init__(self) -> None:
        self.held = threading.Lock()
        self.reached = threading.Semaphore(0)
        self._passed = 0
        self._count_lock = threading.Lock()

    def __enter__(self) -> None:
        self.reached.release()
        self.held.acquire()
        with self._count_lock:
            self._passed += 1

    def __exit__(self, *_: object) -> None:
        self.held.release()

    def locked(self) -> bool:
        return self.held.locked()

    @property
    def passed(self) -> int:
        with self._count_lock:
            return self._passed


def observe_clock_lock(app: FastAPI) -> ObservedClockLock:
    observed = ObservedClockLock()
    app.state.workbench.clock_lock = observed
    return observed


@pytest.fixture
def app(tmp_path: Path) -> Iterator[FastAPI]:
    application = make_app(tmp_path / "data")
    seed_unit_run(application)
    yield application
    application.state.workbench.shutdown()


@pytest.fixture
def provider(app: FastAPI) -> Iterator[TestClient]:
    """A client that, like a provider, never logs in and never sends the UI's X-Workbench-Request header."""
    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------- receiver outcomes


def test_a_signed_delivery_is_accepted_without_a_session_and_waits_as_pending(app: FastAPI, provider: TestClient):
    headers, body = signed("evt_http_0001")
    response = provider.post(WEBHOOK, content=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"outcome": "accepted", "event_id": "evt_http_0001"}
    assert api.REQUEST_HEADER not in response.request.headers
    assert "cookie" not in response.request.headers
    with closing(storage.connect(database(app))) as conn:
        [row] = storage.list_inbox_rows(conn, run_id=RUN_ID)
        [observation] = storage.list_observations(conn, run_id=RUN_ID, as_of_ms=None)
        attempts = storage.list_delivery_attempts(conn, run_id=RUN_ID)
        worker_attempts = storage.list_worker_attempts(conn, run_id=RUN_ID)
    assert row.raw_body == body
    assert row.processing_status is InboxStatus.PENDING
    assert row.received_at_ms == CLOCK_MS + STEP  # strictly after the clock the delivery found
    assert observation.known_at_ms == CLOCK_MS + STEP
    assert replay_clock(app) == CLOCK_MS + STEP
    assert [(attempt.outcome, attempt.http_status) for attempt in attempts] == [(ReceiveOutcome.ACCEPTED, 200)]
    assert worker_attempts == ()  # the route only receives


def test_the_same_bytes_again_are_a_duplicate_received_after_that_requests_clock(app: FastAPI, provider: TestClient):
    headers, body = signed("evt_http_0002")
    first = provider.post(WEBHOOK, content=body, headers=headers)
    advance_clock(app, by_ms=5_000)
    again = provider.post(WEBHOOK, content=body, headers=headers)
    assert first.json()["outcome"] == "accepted"
    assert again.status_code == 200
    assert again.json() == {"outcome": "duplicate", "event_id": "evt_http_0002"}
    assert rows(app, "webhook_inbox") == 1
    received = [attempt.received_at_ms for attempt in delivery_attempts(app)]
    assert received == [CLOCK_MS + STEP, CLOCK_MS + STEP + 5_000 + STEP]  # read at request time, then stepped


def test_a_reused_event_id_with_other_bytes_is_acknowledged_held_and_never_processed(
    app: FastAPI, provider: TestClient
):
    headers, body = signed("evt_http_0003")
    reused_headers, reused_body = signed("evt_http_0003", status=PaymentStatus.FAILED)
    provider.post(WEBHOOK, content=body, headers=headers)
    response = provider.post(WEBHOOK, content=reused_body, headers=reused_headers)
    assert response.status_code == 200
    assert response.json() == {"outcome": "conflicting_id", "event_id": "evt_http_0003"}
    with closing(storage.connect(database(app))) as conn:
        [held] = storage.list_conflicting_deliveries(conn, run_id=RUN_ID, as_of_ms=None)
        [row] = storage.list_inbox_rows(conn, run_id=RUN_ID)
        observations = storage.list_observations(conn, run_id=RUN_ID, as_of_ms=None)
    assert held.raw_body == reused_body
    assert held.differing_fields == ("status",)
    assert held.review_status == "pending_review"
    assert row.raw_body == body  # the inbox keeps the original
    assert row.processing_status is InboxStatus.PENDING
    assert [observation.status for observation in observations] == [PaymentStatus.CAPTURED]
    assert rows(app, "worker_attempts") == 0


def signature_headers(problem: str, headers: dict[str, str]) -> list[tuple[str, str]]:
    event, signature = (EVENT_ID, headers[EVENT_ID]), headers[SIGNATURE]
    if problem == "bad":
        return [event, (SIGNATURE, ("1" if signature[0] == "0" else "0") + signature[1:])]
    if problem == "missing":
        return [event]
    return [event, (SIGNATURE, signature), (SIGNATURE, signature)]  # repeated, even with the right value


@pytest.mark.parametrize("problem", ["bad", "missing", "repeated"])
def test_a_bad_missing_or_repeated_signature_is_401_and_leaves_only_delivery_metadata(
    app: FastAPI, provider: TestClient, problem: str
):
    headers, body = signed("evt_http_0004")
    response = provider.post(WEBHOOK, content=body, headers=signature_headers(problem, headers))
    assert response.status_code == 401
    assert response.json() == {"outcome": "rejected_signature"}
    assert rows(app, "webhook_inbox") == 0
    assert rows(app, "payment_observations") == 0
    [attempt] = delivery_attempts(app)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SIGNATURE
    assert attempt.received_at_ms == CLOCK_MS + STEP  # a rejected delivery steps the clock too


def test_a_repeated_event_id_header_counts_as_absent(app: FastAPI, provider: TestClient):
    headers, body = signed("evt_http_0005")
    repeated = [(SIGNATURE, headers[SIGNATURE]), (EVENT_ID, "evt_http_0005"), (EVENT_ID, "evt_http_0005")]
    response = provider.post(WEBHOOK, content=body, headers=repeated)
    assert response.status_code == 400
    assert response.json() == {"outcome": "rejected_schema"}
    assert rows(app, "webhook_inbox") == 0


def test_an_unknown_integration_is_404_and_recorded(app: FastAPI, provider: TestClient):
    headers, body = signed("evt_http_0006")
    response = provider.post(f"/api/runs/{RUN_ID}/webhooks/int_SYNTHETIC_OTHER", content=body, headers=headers)
    assert response.status_code == 404
    assert response.json() == {"outcome": "unknown_integration"}
    [attempt] = delivery_attempts(app)
    assert attempt.integration_id == "int_SYNTHETIC_OTHER"
    assert attempt.outcome is ReceiveOutcome.UNKNOWN_INTEGRATION
    assert attempt.received_at_ms == CLOCK_MS + STEP
    assert rows(app, "webhook_inbox") == 0


def test_an_unknown_run_is_404_worded_for_a_sender_and_records_nothing(app: FastAPI, provider: TestClient):
    headers, body = signed("evt_http_0007")
    response = provider.post(f"/api/runs/run_missing/webhooks/{INTEGRATION_ID}", content=body, headers=headers)
    assert response.status_code == 404
    assert response.json() == {
        "error": "UnknownRunError",
        "detail": "run 'run_missing' does not exist on this server; check the run ID in the delivery URL",
    }
    assert rows(app, "delivery_attempts") == 0  # there is no run to record it in
    assert replay_clock(app) == CLOCK_MS
    log_in(provider)
    elsewhere = provider.get("/api/runs/run_missing")  # an operator's route keeps its 403
    assert (elsewhere.status_code, elsewhere.json()["error"]) == (403, SCOPE_ERROR)


def test_malformed_json_is_400_and_the_parsers_reason_stays_in_the_database(app: FastAPI, provider: TestClient):
    body = b'{"entity": "event", "payload": '
    headers = {EVENT_ID: "evt_http_0008", SIGNATURE: signing.sign_body(body, merchant.PROVIDER_SIGNING_REF)}
    response = provider.post(WEBHOOK, content=body, headers=headers)
    assert response.status_code == 400
    assert response.json() == {"outcome": "rejected_schema", "event_id": "evt_http_0008"}
    [attempt] = delivery_attempts(app)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SCHEMA
    assert "JSON" in str(attempt.detail)
    assert rows(app, "webhook_inbox") == 0


@pytest.mark.parametrize(("field", "dots", "location"), [
    ("order_id", "..", "entity.order_id"),
    ("order_id", ".", "entity.order_id"),
    ("payment_id", "..", "entity.id"),
])
def test_a_signed_event_naming_an_order_or_payment_made_only_of_dots_is_400_rejected_schema(
    app: FastAPI, provider: TestClient, field: str, dots: str, location: str
):
    """The UI opens an order's case at a path ending in its ID, where a browser reads . and .. as directory steps."""
    headers, body = signed("evt_http_0022", **{field: dots})
    response = provider.post(WEBHOOK, content=body, headers=headers)
    assert (response.status_code, response.json()) == (400, {"outcome": "rejected_schema", "event_id": "evt_http_0022"})
    [attempt] = delivery_attempts(app)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SCHEMA
    assert f"{location}: string_pattern_mismatch" in str(attempt.detail)
    assert rows(app, "webhook_inbox") == 0
    assert rows(app, "payment_observations") == 0


def test_a_signed_body_nested_past_the_parsers_limit_is_400_and_recorded_like_any_schema_rejection(
    app: FastAPI, provider: TestClient
):
    headers, body = nested_event("evt_http_deep_0001", TOO_DEEP)
    with pytest.raises(RecursionError):  # the parser alone cannot read it; the receiver's nesting cap refuses it first
        json.loads(body)
    response = provider.post(WEBHOOK, content=body, headers=headers)
    assert response.status_code == 400
    assert response.json() == {"outcome": "rejected_schema", "event_id": "evt_http_deep_0001"}
    [attempt] = delivery_attempts(app)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SCHEMA
    assert attempt.received_at_ms == CLOCK_MS + STEP
    assert rows(app, "webhook_inbox") == 0


def test_over_http_nesting_at_the_limit_is_accepted_and_one_level_more_is_400_and_recorded(
    app: FastAPI, provider: TestClient
):
    limit = ingestion.MAX_JSON_NESTING
    sent = [nested_event("evt_http_nest_0001", limit - 1), nested_event("evt_http_nest_0002", limit)]
    answers = [provider.post(WEBHOOK, content=body, headers=headers) for headers, body in sent]
    assert [(answer.status_code, answer.json()) for answer in answers] == [
        (200, {"outcome": "accepted", "event_id": "evt_http_nest_0001"}),
        (400, {"outcome": "rejected_schema", "event_id": "evt_http_nest_0002"}),
    ]
    assert [(attempt.outcome, attempt.received_at_ms) for attempt in delivery_attempts(app)] == [
        (ReceiveOutcome.ACCEPTED, CLOCK_MS + STEP), (ReceiveOutcome.REJECTED_SCHEMA, CLOCK_MS + 2 * STEP),
    ]
    assert rows(app, "webhook_inbox") == 1


def test_a_streamed_body_far_over_the_ceiling_is_413_after_reading_one_byte_past_it(app: FastAPI):
    headers, _ = signed("evt_http_0009")
    offered = [b"x" * CHUNK_BYTES] * (8 * CEILING // CHUNK_BYTES)  # 8 MiB, sent chunked
    response, pulled = post_streamed(app, WEBHOOK, headers, offered)
    assert response.status_code == 413
    assert response.json() == {"outcome": "rejected_size"}
    assert response.headers["connection"] == "close"  # the rest of the body is not read on a kept connection
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert CEILING + 1 <= pulled < CEILING + 1 + CHUNK_BYTES  # it stopped at the chunk that held ceiling + 1 bytes
    [attempt] = delivery_attempts(app)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SIZE
    assert attempt.body_sha256 == hashlib.sha256(b"x" * (CEILING + 1)).hexdigest()  # exactly ceiling + 1 bytes held
    assert rows(app, "webhook_inbox") == 0


# --------------------------------------------------------------------------- the deployed receiver


def d3_run(fault: str) -> families.FamilyRun:
    return next(run for run in families.load_manifest().runs if run.family == "D3" and run.params["fault"] == fault)


@pytest.mark.parametrize(("fault", "provider_key_still_configured"), [("receiver@2", True), ("config@3", False)])
def test_a_receiver_change_that_rejects_valid_deliveries_answers_over_http_as_it_did_in_process(
    tmp_path: Path, fault: str, provider_key_still_configured: bool
):
    entry = d3_run(fault)
    world = generate_world(families.spec_for(entry), entry.seed)
    app = make_app(tmp_path / "data")
    with closing(storage.connect(database(app))) as conn:
        driver.seed_world(conn, world, oracle_dir=None)
        integration = storage.get_integration(conn, run_id=world.run_id, integration_id=INTEGRATION_ID)
        inbox_before = count_rows(conn, "webhook_inbox", world.run_id)
    changed_at = next(plan.deployed_at_ms for plan in world.deployments if plan.variant == fault)
    replayed = delivery_attempts(app, world.run_id)
    in_process = {(attempt.outcome.value, attempt.http_status) for attempt in replayed
                  if attempt.received_at_ms >= changed_at}
    assert ReceiveOutcome.ACCEPTED in {attempt.outcome for attempt in replayed if attempt.received_at_ms < changed_at}
    assert in_process == {("rejected_signature", 401)}  # what driver.deliver got once the change was live
    # receiver@2 keeps the provider's key, so only its pre-processing of the bytes can reject the signature;
    # config@3 moved the integration to another key.
    assert (integration.secret_ref == merchant.PROVIDER_SIGNING_REF) is provider_key_still_configured
    clock_ms = replay_clock(app, world.run_id)
    headers, body = payment_delivery(app, world.run_id, "evt_http_d3_0001")
    with TestClient(app) as provider:
        response = provider.post(webhook_path(world.run_id), content=body, headers=headers)
    assert (response.json()["outcome"], response.status_code) in in_process
    assert rows(app, "webhook_inbox", world.run_id) == inbox_before
    latest = delivery_attempts(app, world.run_id)[-1]
    assert latest.outcome is ReceiveOutcome.REJECTED_SIGNATURE
    assert latest.received_at_ms == clock_ms + STEP


def test_a_receiver_that_raises_before_verification_is_500_without_its_message(app: FastAPI, provider: TestClient):
    with closing(storage.connect(database(app))) as conn:
        runtime.record_deployment(conn, run_id=RUN_ID, deploy_id="deploy_b_0001", variant="receiver@2",
                                  deployed_at_ms=CLOCK_MS - 1_000, summary="Store webhook bodies in one stable layout")
    body = b"not a JSON document"
    headers = {EVENT_ID: "evt_http_0010", SIGNATURE: signing.sign_body(body, merchant.PROVIDER_SIGNING_REF)}
    response = provider.post(WEBHOOK, content=body, headers=headers)
    assert response.status_code == 500
    assert response.json()["error"] == "ReceiverFailedError"
    assert "Expecting value" not in response.text  # the merchant code's own message stays on the server
    assert rows(app, "delivery_attempts") == 0
    assert rows(app, "webhook_inbox") == 0
    assert replay_clock(app) == CLOCK_MS + STEP  # the clock stepped before the receiver ran


# --------------------------------------------------------------------------- replay time and the clock lock


def test_deliveries_land_after_the_clock_so_a_packet_already_cut_there_is_unchanged(tmp_path: Path):
    app = make_app(tmp_path / "data")
    run_id = seed_backlog(app)
    cutoff_ms = replay_clock(app, run_id)
    cut_before = packet_hash_at(app, run_id, cutoff_ms)
    rejected = payment_delivery(app, run_id, "evt_http_after_0001", secret_ref=OTHER_SECRET)
    accepted = payment_delivery(app, run_id, "evt_http_after_0002")
    with TestClient(app) as provider:
        answers = [provider.post(webhook_path(run_id), content=body, headers=headers).status_code
                   for headers, body in (rejected, accepted)]
    received = [attempt.received_at_ms for attempt in delivery_attempts(app, run_id) if attempt.received_at_ms > cutoff_ms]
    assert answers == [401, 200]
    assert received == [cutoff_ms + STEP, cutoff_ms + 2 * STEP]  # each one step after the clock it found
    assert replay_clock(app, run_id) == cutoff_ms + 2 * STEP
    assert packet_hash_at(app, run_id, cutoff_ms) == cut_before


def test_a_diagnosis_stored_before_deliveries_still_matches_a_fresh_build_of_its_evidence(tmp_path: Path):
    app = make_app(tmp_path / "data")
    run_id = seed_backlog(app)
    with TestClient(app) as client:
        log_in(client)
        started = client.post(f"/api/runs/{run_id}/diagnoses", json={}, headers=UI_HEADER)
        job = finished_job(client, run_id, started.json())
        for headers, body in (payment_delivery(app, run_id, "evt_http_after_0003", secret_ref=OTHER_SECRET),
                              payment_delivery(app, run_id, "evt_http_after_0004")):
            client.post(webhook_path(run_id), content=body, headers=headers)
    stored = job["diagnosis"]
    assert job["status"] == "done"
    assert replay_clock(app, run_id) == job["evidence_cutoff_ms"] + 2 * STEP
    # packet.persist_packet refuses a fresh build that differs from the evidence stored for incident@cutoff.
    rebuilt = packet_hash_at(app, run_id, job["evidence_cutoff_ms"], incident_id=stored["incident_id"], persist=True)
    assert rebuilt == stored["packet_hash"]


def test_the_receipt_runs_under_the_clock_lock_after_the_clock_step(
    app: FastAPI, provider: TestClient, monkeypatch: pytest.MonkeyPatch
):
    lock = app.state.workbench.clock_lock
    seen: list[tuple[bool, int, object]] = []
    receive = ingestion.receive_webhook

    def watched(conn: sqlite3.Connection, **arguments: object) -> ingestion.DeliveryResponse:
        seen.append((lock.locked(), storage.require_run(conn, RUN_ID).clock_ms, arguments["received_at_ms"]))
        return receive(conn, **arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(ingestion, "receive_webhook", watched)
    headers, body = signed("evt_http_0011")
    assert provider.post(WEBHOOK, content=body, headers=headers).status_code == 200
    assert seen == [(True, CLOCK_MS + STEP, CLOCK_MS + STEP)]
    assert not lock.locked()


def test_a_held_clock_lock_holds_back_both_a_delivery_and_a_diagnosis_cutoff(tmp_path: Path):
    app = make_app(tmp_path / "data")
    run_id = seed_backlog(app)
    clock_before = replay_clock(app, run_id)
    headers, body = payment_delivery(app, run_id, "evt_http_lock_0001")
    answers: dict[str, httpx.Response] = {}
    observed = observe_clock_lock(app)
    with TestClient(app) as client:
        log_in(client)

        def deliver() -> None:
            answers["delivery"], _ = post_streamed(app, webhook_path(run_id), headers, [body])

        def capture_cutoff() -> None:
            answers["diagnosis"] = client.post(f"/api/runs/{run_id}/diagnoses", json={}, headers=UI_HEADER)

        workers = [threading.Thread(target=deliver), threading.Thread(target=capture_cutoff)]
        with observed.held:  # as if another delivery or evidence time held it
            for worker in workers:
                worker.start()
            reached = [observed.reached.acquire(timeout=30) for _ in workers]
            passed_while_held = observed.passed
            clock_while_held = replay_clock(app, run_id)
        for worker in workers:
            worker.join(timeout=30)
        job = finished_job(client, run_id, answers["diagnosis"].json())
    [delivered] = [item for item in delivery_attempts(app, run_id) if item.event_id == "evt_http_lock_0001"]
    assert reached == [True, True]  # both requests got as far as the clock lock while it was held
    assert passed_while_held == 0  # and neither got past it, so neither could step or read the clock
    assert observed.passed == 2  # both went ahead once it was free
    assert clock_while_held == clock_before
    assert answers["delivery"].status_code == 200
    assert delivered.received_at_ms == clock_before + STEP
    # Whichever went first, the cutoff is either before the receipt or after its commit, never at a half-done one.
    assert job["evidence_cutoff_ms"] in (clock_before, clock_before + STEP)
    rebuilt = packet_hash_at(app, run_id, job["evidence_cutoff_ms"], incident_id=job["incident_id"], persist=True)
    assert rebuilt == job["diagnosis"]["packet_hash"]


def run_count(app: FastAPI) -> int:
    with closing(storage.connect(database(app))) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])


# Each fixes an evidence time from the clock. The bodies pass validation, so each request reaches its
# route; most then fail their business checks, which happens only after the lock is taken.
@pytest.mark.parametrize(("path", "body"), [
    (f"/api/runs/{RUN_ID}/advance", {"by_ms": 1_000}),
    (f"/api/runs/{RUN_ID}/fixes/payload@5/validate", {"diagnosis_id": "diag_none"}),
    (f"/api/runs/{RUN_ID}/plans", {"operation": "escalate", "diagnosis_id": "diag_none"}),
    (f"/api/runs/{RUN_ID}/plans/plan_none/approve", {"plan_hash": "0" * 64}),
    (f"/api/runs/{RUN_ID}/plans/plan_none/execute", {}),
    (f"/api/runs/{RUN_ID}/refresh", {"payment_ids": [PAY_1]}),
    ("/api/runs", {"scenario": "worker_backlog", "seed": 7}),
], ids=["advance", "validate-fix", "plan", "approve", "execute", "refresh", "seed-run"])
def test_every_other_route_that_fixes_an_evidence_time_waits_for_the_clock_lock(
    app: FastAPI, provider: TestClient, path: str, body: dict[str, object]
):
    log_in(provider)
    observed = observe_clock_lock(app)
    answers: list[int] = []

    def send() -> None:
        answers.append(provider.post(path, json=body, headers=UI_HEADER).status_code)

    worker = threading.Thread(target=send)
    with observed.held:
        worker.start()
        reached = observed.reached.acquire(timeout=30)
        passed_while_held = observed.passed
        clock_while_held = replay_clock(app)
        runs_while_held = run_count(app)
    worker.join(timeout=60)
    assert reached  # the request got as far as the clock lock while it was held
    assert passed_while_held == 0  # and no further
    assert clock_while_held == CLOCK_MS  # no operator step moved the clock past an in-flight delivery
    assert runs_while_held == 1  # no seeding replay started
    assert observed.passed == 1
    assert len(answers) == 1  # it went ahead once the lock was free


# --------------------------------------------------------------------------- durability, threads and rate limit


def test_a_storage_failure_in_the_receipt_is_503_with_retry_after_and_stores_nothing(
    app: FastAPI, provider: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def failing_disk(*_: object, **__: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    headers, body = signed("evt_http_0012")
    monkeypatch.setattr(storage, "insert_observation", failing_disk)  # inside the receipt transaction
    with caplog.at_level(logging.ERROR, logger=api.__name__):
        response = provider.post(WEBHOOK, content=body, headers=headers)
    assert response.status_code == 503
    assert int(response.headers[RETRY_AFTER]) == api.NOT_DURABLE_RETRY_AFTER_SECONDS
    assert response.json()["error"] == NOT_STORED
    assert "disk I/O error" not in response.text
    assert "evt_http_0012" not in response.text
    assert RUN_ID not in response.text
    assert "disk I/O error" in caplog.text  # the cause is on the server console
    assert rows(app, "webhook_inbox") == 0
    assert rows(app, "payment_observations") == 0
    assert rows(app, "delivery_attempts") == 0


@pytest.mark.parametrize(("failing", "clock_after"), [
    ("advance_clock", CLOCK_MS),  # the clock step itself
    ("get_run", CLOCK_MS),  # require_run's read
    ("insert_delivery_attempt", CLOCK_MS + STEP),  # recording a rejected delivery, after the step committed
], ids=["clock-step", "run-read", "rejection-record"])
def test_a_storage_failure_anywhere_on_the_path_is_503_with_retry_after(
    app: FastAPI, provider: TestClient, monkeypatch: pytest.MonkeyPatch, failing: str, clock_after: int
):
    def locked_database(*_: object, **__: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    headers, body = signed("evt_http_0013")
    with monkeypatch.context() as broken:
        broken.setattr(storage, failing, locked_database)
        response = provider.post(WEBHOOK, content=body, headers=signature_headers("bad", headers))
    assert response.status_code == 503
    assert int(response.headers[RETRY_AFTER]) == api.NOT_DURABLE_RETRY_AFTER_SECONDS
    assert response.json()["error"] == NOT_STORED
    assert "database is locked" not in response.text
    assert rows(app, "delivery_attempts") == 0
    assert replay_clock(app) == clock_after


def test_the_receipt_runs_in_a_worker_thread_not_on_the_event_loop(
    provider: TestClient, monkeypatch: pytest.MonkeyPatch
):
    on_event_loop: list[bool] = []
    receive = ingestion.receive_webhook

    def observed(conn: sqlite3.Connection, **arguments: object) -> ingestion.DeliveryResponse:
        on_event_loop.append(has_running_event_loop())
        return receive(conn, **arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(ingestion, "receive_webhook", observed)
    headers, body = signed("evt_http_0014")
    assert provider.post(WEBHOOK, content=body, headers=headers).status_code == 200
    assert on_event_loop == [False]


def test_over_the_cap_the_answer_is_429_before_the_body_is_read_or_the_database_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def database_must_stay_closed(*_: object, **__: object) -> sqlite3.Connection:
        raise AssertionError("the rate limit must be decided before the database is opened")

    app = make_app(tmp_path / "data", max_deliveries=2)
    seed_unit_run(app)
    headers, body = signed("evt_http_0015")
    first, _ = post_streamed(app, WEBHOOK, headers, [body])
    second, _ = post_streamed(app, WEBHOOK, headers, [body])
    clock_before_refusal = replay_clock(app)
    with monkeypatch.context() as offline:
        offline.setattr(storage, "connect", database_must_stay_closed)
        refused, pulled = post_streamed(app, WEBHOOK, headers, [body])
    assert [first.status_code, second.status_code, refused.status_code] == [200, 200, 429]
    assert 1 <= int(refused.headers[RETRY_AFTER]) <= 60
    assert refused.json()["error"] == "TooManyDeliveriesError"
    assert pulled == 0  # the body was never read
    assert clock_before_refusal == CLOCK_MS + 2 * STEP
    assert replay_clock(app) == clock_before_refusal  # the refused request did not step the clock
    assert rows(app, "delivery_attempts") == 2
    assert rows(app, "webhook_inbox") == 1
    app.state.workbench.shutdown()


def test_the_cap_must_admit_at_least_one_delivery(tmp_path: Path):
    loopback = frozenset({"testserver"})
    with pytest.raises(ValueError, match="at least 1"):
        api.ApiConfig(data_dir=tmp_path, operator_token=TOKEN, allowed_hosts=loopback, allowed_origins=loopback,
                      max_webhook_deliveries_per_minute=0)


@dataclass
class RateWindow:
    """A workbench whose webhook cap is three a minute, read on a clock the test sets."""

    workbench: api.Workbench
    now: float = 0.0

    def ask_at(self, seconds: float) -> int | None:
        """Ask for one delivery slot at ``seconds``: None when admitted, else the Retry-After it was given."""
        self.now = seconds
        try:
            self.workbench.admit_webhook_delivery()
        except api.TooManyDeliveriesError as refused:
            return refused.retry_after_seconds
        return None


@pytest.fixture
def rate_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RateWindow]:
    app = make_app(tmp_path / "data", max_deliveries=3)
    window = RateWindow(app.state.workbench)
    # Only api's own name for the time module is replaced, so nothing else sees the test's clock.
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: window.now))
    yield window
    app.state.workbench.shutdown()


def test_the_rate_window_frees_one_slot_as_each_admitted_delivery_turns_a_minute_old(rate_window: RateWindow):
    asked_at = (0.0, 10.0, 20.0, 30.0, 30.0, 59.75, 60.0, 60.25, 60.5, 70.25, 70.5)
    assert [rate_window.ask_at(seconds) for seconds in asked_at] == [
        None, None, None,  # three admitted
        30, 30,  # full until the one from 0 s is more than 60 s old; a refusal takes no slot
        1, 1,  # still full at exactly 60 s, and Retry-After never drops below 1
        None,  # 60.25: the one from 0 s has left, so exactly one more fits
        10,  # 60.5: full again until the one from 10 s leaves, 9.5 s later
        None, 10,  # 70.25: that one has left; 70.5: full until the one from 20 s leaves
    ]


def test_a_quiet_minute_empties_the_rate_window(rate_window: RateWindow):
    first = [rate_window.ask_at(0.0) for _ in range(4)]
    later = [rate_window.ask_at(60.25) for _ in range(4)]
    assert first == [None, None, None, 60]
    assert later == [None, None, None, 60]


def asgi_post(path: str, headers: dict[str, str], body_length: int) -> Scope:
    """The scope a server builds for a POST from 127.0.0.1 that announces a body of ``body_length`` bytes."""
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-length", str(body_length).encode()),
                    *((name.lower().encode(), value.encode()) for name, value in headers.items())],
        "client": ("127.0.0.1", 50_000), "server": ("127.0.0.1", api.DEFAULT_PORT),
    }


def test_a_sender_that_disconnects_mid_body_leaves_no_record_and_no_error_but_keeps_its_rate_slot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    app = make_app(tmp_path / "data", max_deliveries=1)
    seed_unit_run(app)
    headers, body = signed("evt_http_0019")
    arriving = iter([{"type": "http.request", "body": body[: len(body) // 2], "more_body": True},
                     {"type": "http.disconnect"}])
    sent: list[Message] = []

    async def receive() -> Message:
        return next(arriving, {"type": "http.disconnect"})

    async def send(message: Message) -> None:
        sent.append(message)

    with caplog.at_level(logging.WARNING):
        asyncio.run(app(asgi_post(WEBHOOK, headers, len(body)), receive, send))  # nothing escapes the app
    retried, _ = post_streamed(app, WEBHOOK, headers, [body])
    assert rows(app, "delivery_attempts") == 0
    assert replay_clock(app) == CLOCK_MS
    assert (sent[0]["type"], sent[0]["status"]) == ("http.response.start", 400)  # not a 500; nobody reads it
    [warning] = [record for record in caplog.records if record.name == api.__name__]
    assert warning.levelno == logging.WARNING
    assert RUN_ID in warning.getMessage()
    assert INTEGRATION_ID in warning.getMessage()
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
    assert retried.status_code == 429  # the cap is one a minute, and the disconnected request used it
    app.state.workbench.shutdown()


# --------------------------------------------------------------------------- request guard


@pytest.mark.parametrize("hostile", [
    {"origin": "http://evil.example"},
    {"sec-fetch-site": "cross-site"},
    {"host": "attacker.example"},
], ids=["cross-site-origin", "cross-site-fetch", "foreign-host"])
def test_a_browser_on_another_site_or_a_foreign_host_is_refused(
    app: FastAPI, provider: TestClient, hostile: dict[str, str]
):
    headers, body = signed("evt_http_0016")
    response = provider.post(WEBHOOK, content=body, headers={**headers, **hostile})
    assert response.status_code == 403
    assert response.json()["error"] == SCOPE_ERROR
    assert rows(app, "delivery_attempts") == 0
    assert replay_clock(app) == CLOCK_MS  # a refused request leaves the clock alone


def test_the_route_lists_above_name_every_state_changing_route(app: FastAPI):
    declared = {(method, route.path) for route in app.routes if isinstance(route, APIRoute)
                for method in route.methods - api.SAFE_METHODS}
    assert declared == {WEBHOOK_ROUTE, *OTHER_STATE_CHANGING_ROUTES}


@pytest.mark.parametrize(("method", "template"), OTHER_STATE_CHANGING_ROUTES,
                         ids=[f"{method} {template}" for method, template in OTHER_STATE_CHANGING_ROUTES])
def test_the_exemption_reaches_no_other_state_changing_route_even_with_control_characters_in_its_path(
    app: FastAPI, provider: TestClient, method: str, template: str
):
    log_in(provider)  # a session, so only the missing header can refuse these
    path = template.format(**PATH_PARAMETERS)
    paths = [path, *with_control_characters(path)]
    answers = {sent: provider.request(method, sent, json={}) for sent in paths}
    assert {sent: refused_by_the_guard(response) for sent, response in answers.items()} == dict.fromkeys(paths, True)
    assert replay_clock(app) == CLOCK_MS
    assert run_count(app) == 1
    assert provider.get("/api/session").json() == {"operator_id": OPERATOR}  # the session was not ended either


def test_control_characters_in_the_webhook_path_lose_the_exemption(app: FastAPI, provider: TestClient):
    headers, body = signed("evt_http_0018")
    paths = with_control_characters(WEBHOOK)  # urllib.parse would drop each, so request.url.path still matched
    answers = {sent: provider.post(sent, content=body, headers=headers) for sent in paths}
    assert {sent: refused_by_the_guard(response) for sent, response in answers.items()} == dict.fromkeys(paths, True)
    assert rows(app, "delivery_attempts") == 0
    assert replay_clock(app) == CLOCK_MS


@pytest.mark.parametrize(("method", "path"), [
    ("POST", f"{WEBHOOK}/extra"),
    ("POST", f"{WEBHOOK}/"),
    ("POST", f"/api/runs/{'r' * 129}/webhooks/{INTEGRATION_ID}"),
    ("POST", f"/api/runs/%2E%2E/webhooks/{INTEGRATION_ID}"),  # encoded, as the client would resolve ..
    ("POST", f"/api/runs/{RUN_ID}/webhooks/%2E"),
    ("PUT", WEBHOOK),
    ("PROPFIND", WEBHOOK),
], ids=["longer-path", "trailing-slash", "over-long-run-id", "dots-only-run-id", "dot-only-integration-id",
        "other-method", "method-no-route-serves"])
def test_only_a_post_to_the_exact_webhook_path_is_exempt_from_the_ui_header(
    app: FastAPI, provider: TestClient, method: str, path: str
):
    headers, body = signed("evt_http_0017")
    response = provider.request(method, path, content=body, headers=headers)
    assert response.status_code == 403
    assert api.REQUEST_HEADER in response.json()["detail"]
    assert rows(app, "delivery_attempts") == 0
    assert replay_clock(app) == CLOCK_MS


# --------------------------------------------------------------------------- the CLI helper's request, over HTTP


def printed_request(capsys: pytest.CaptureFixture[str]) -> tuple[str, dict[str, str], Path]:
    """The path, headers and body file of the curl command ``webhook-request`` printed."""
    [command] = [line for line in capsys.readouterr().out.splitlines() if line.startswith("curl ")]
    words = shlex.split(command)
    headers = dict(words[index + 1].split(": ", 1) for index, word in enumerate(words) if word == "--header")
    body_file = Path(words[words.index("--data-binary") + 1].removeprefix("@"))
    return urlsplit(words[-1]).path, headers, body_file


def test_the_cli_helpers_request_gets_each_outcome_its_help_promises(
    app: FastAPI, provider: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    def helper(status: str, output: Path) -> list[str]:
        return ["webhook-request", "--data-dir", str(database(app).parent), "--run-id", RUN_ID, "--payment-id", PAY_1,
                "--status", status, "--event-id", "evt_walk_0001", "--output", str(output)]

    assert cli.main(helper("captured", tmp_path / "captured.json")) == 0
    path, headers, body_file = printed_request(capsys)
    body = body_file.read_bytes()
    sent_once = provider.post(path, content=body, headers=headers)
    sent_twice = provider.post(path, content=body, headers=headers)
    one_byte_edited = provider.post(path, content=body[:-1] + bytes([body[-1] ^ 1]), headers=headers)
    assert cli.main(helper("failed", tmp_path / "failed.json")) == 0
    _, reused_headers, reused_body_file = printed_request(capsys)
    reused_id = provider.post(path, content=reused_body_file.read_bytes(), headers=reused_headers)
    answers = [(response.status_code, response.json()["outcome"])
               for response in (sent_once, sent_twice, one_byte_edited, reused_id)]
    assert path == WEBHOOK
    assert answers == [(200, "accepted"), (200, "duplicate"), (401, "rejected_signature"), (200, "conflicting_id")]
    assert rows(app, "webhook_inbox") == 1
    assert rows(app, "conflicting_deliveries") == 1


def test_after_a_receiver_secret_rotation_the_cli_helpers_request_is_refused_like_the_providers(
    app: FastAPI, provider: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    with closing(storage.connect(database(app))) as conn:  # config@3 verifies with the next key, as in D3
        runtime.record_deployment(conn, run_id=RUN_ID, deploy_id="deploy_w_0001", variant="config@3",
                                  deployed_at_ms=CLOCK_MS - 1_000, summary="Verify webhooks with the next key")
        rotated = storage.get_integration(conn, run_id=RUN_ID, integration_id=INTEGRATION_ID)
    arguments = ["webhook-request", "--data-dir", str(database(app).parent), "--run-id", RUN_ID, "--payment-id", PAY_1,
                 "--status", "captured", "--event-id", "evt_rotated_0001", "--output", str(tmp_path / "body.json")]
    assert cli.main(arguments) == 0
    path, headers, body_file = printed_request(capsys)
    from_helper = provider.post(path, content=body_file.read_bytes(), headers=headers)
    provider_headers, provider_body = signed("evt_rotated_0002")  # signed as driver.deliver signs
    from_provider = provider.post(WEBHOOK, content=provider_body, headers=provider_headers)
    assert rotated.secret_ref != merchant.PROVIDER_SIGNING_REF
    assert [(answer.status_code, answer.json()) for answer in (from_helper, from_provider)] == [
        (401, {"outcome": "rejected_signature"}), (401, {"outcome": "rejected_signature"}),
    ]
    assert [attempt.outcome for attempt in delivery_attempts(app)] == [ReceiveOutcome.REJECTED_SIGNATURE] * 2
    assert rows(app, "webhook_inbox") == 0  # nothing was accepted after the rotation


# --------------------------------------------------------------------------- a real server on 127.0.0.1


@dataclass
class SocketReads:
    """The bytes uvicorn read from the socket: in all, and by the time its answer had been sent."""

    total: int = 0
    when_answered: int | None = None


def counting(reads: SocketReads) -> type[H11Protocol]:
    """uvicorn's HTTP/1.1 protocol, the one ``api.main`` runs, counting what it reads into ``reads``."""

    class CountingH11Protocol(H11Protocol):
        def data_received(self, data: bytes) -> None:
            reads.total += len(data)
            super().data_received(data)

        def on_response_complete(self) -> None:
            reads.when_answered = reads.total
            super().on_response_complete()

    return CountingH11Protocol


@contextmanager
def served_on_loopback(
    asgi_app: Callable[[Scope, Receive, Send], Awaitable[None]], protocol: type[H11Protocol]
) -> Iterator[int]:
    """Serve ``asgi_app`` with uvicorn from a thread on 127.0.0.1, on a port the kernel picks; yields the port.

    On leaving, uvicorn shuts down gracefully, so every request it took has finished, logging included.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(asgi_app, lifespan="off", log_config=None, http=protocol))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
    assert not thread.is_alive(), "the loopback server did not stop"


def request_head(path: str, headers: dict[str, str], content_length: int) -> bytes:
    lines = [f"POST {path} HTTP/1.1", "Host: testserver", "Content-Type: application/json",
             *(f"{name}: {value}" for name, value in headers.items()), f"Content-Length: {content_length}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def read_response(client: socket.socket) -> tuple[str, dict[str, str], bytes]:
    """The status line, headers (lowercase names) and body of one response that has a Content-Length."""
    received = b""
    while b"\r\n\r\n" not in received:
        chunk = client.recv(CHUNK_BYTES)
        assert chunk, f"the server closed the connection before it answered; got {received!r}"
        received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in header_lines)}
    while len(body) < int(headers["content-length"]):
        chunk = client.recv(CHUNK_BYTES)
        assert chunk, "the server closed the connection in the middle of its answer"
        body += chunk
    return status_line, headers, body


def upload_until_refused(client: socket.socket, size: int) -> type[OSError] | None:
    """Send ``size`` more body bytes; the kind of error that stopped the upload, or None if all of it went."""
    chunk = b"x" * CHUNK_BYTES
    sent = 0
    try:
        while sent < size:
            piece = chunk[: size - sent]
            client.sendall(piece)
            sent += len(piece)
    except OSError as refused:  # the outcome under test when the server has closed the connection
        return type(refused)
    return None


def test_the_loopback_server_is_the_one_api_main_runs():
    assert AutoHTTPProtocol is H11Protocol  # uvicorn's "auto" picks h11 here, so the counting subclass is faithful


def test_over_a_real_loopback_server_the_body_cap_answers_413_and_the_server_stops_reading(tmp_path: Path):
    """The sender announces 64 MiB, sends one byte past the ceiling, reads the answer, then tries to go on.

    Pausing before the answer means the server holds no unread bytes when it closes, so the close is a
    clean one and the answer arrives whole. A sender that never pauses may instead see the connection
    reset before the answer: closing on unread data is what stops the server reading.
    """
    app = make_app(tmp_path / "data")
    seed_unit_run(app)
    reads = SocketReads()
    headers, _ = signed("evt_http_0020")
    announced = 64 * CEILING
    head = request_head(WEBHOOK, headers, announced)
    with served_on_loopback(app, counting(reads)) as port, \
            closing(socket.create_connection(("127.0.0.1", port), timeout=10)) as client:
        client.sendall(head + b"x" * (CEILING + 1))
        status_line, response_headers, body = read_response(client)
        rest_of_upload = upload_until_refused(client, announced - (CEILING + 1))
    assert status_line.startswith("HTTP/1.1 413 ")
    assert json.loads(body) == {"outcome": "rejected_size"}
    assert reads.when_answered == len(head) + CEILING + 1  # everything sent before the answer, and no more
    assert reads.total == reads.when_answered  # once it had answered, the server read nothing at all
    assert response_headers.get("connection") == "close"
    assert rest_of_upload in (ConnectionResetError, BrokenPipeError)  # the rest of the body found no reader
    [attempt] = delivery_attempts(app)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SIZE
    assert attempt.body_sha256 == hashlib.sha256(b"x" * (CEILING + 1)).hexdigest()  # exactly ceiling + 1 bytes held
    app.state.workbench.shutdown()


def test_over_a_real_loopback_server_a_sender_that_disconnects_mid_body_logs_no_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    app = make_app(tmp_path / "data")
    seed_unit_run(app)
    headers, body = signed("evt_http_0021")
    finished = threading.Event()

    async def tracked(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await app(scope, receive, send)
        finally:
            finished.set()

    with caplog.at_level(logging.WARNING), served_on_loopback(tracked, H11Protocol) as port:
        with closing(socket.create_connection(("127.0.0.1", port), timeout=10)) as client:
            client.sendall(request_head(WEBHOOK, headers, len(body)) + body[: len(body) // 2])
        assert finished.wait(timeout=10), "the request never reached the app"
        # Leaving the server waits for the request's task, so whatever uvicorn logs about it is in caplog after.
    assert [(record.name, record.getMessage()) for record in caplog.records if record.levelno >= logging.ERROR] == []
    assert [record.levelno for record in caplog.records if record.name == api.__name__] == [logging.WARNING]
    assert rows(app, "delivery_attempts") == 0
    assert replay_clock(app) == CLOCK_MS
    app.state.workbench.shutdown()
