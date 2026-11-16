"""Bounds that hold before anything parses a request body (two low-severity findings of the security audit).

1. Every route but the webhook route refuses a body over ``api.MAX_REQUEST_BODY_BYTES`` with 413 before the route
   reads it. FastAPI reads and decodes a route's whole JSON body before it runs any dependency, so the bound comes
   first: before the session check, before login, and before a malformed login body is echoed back in a 422. A
   declared length over the limit is refused before any route runs, so before the first byte is read, and a route
   that never reads its body does not run either; a body sent without one is stopped once it passes the limit. The
   webhook route keeps its own capped read, so an oversized delivery is still recorded.
2. A deployed merchant receiver prepares a webhook body for verification only when the delivered bytes are within the
   integration's size limit and the nesting limit. receiver@2 re-serialises what it reads with indentation, so a deep
   body used to grow about 900-fold under the clock lock. Any other body reaches ingestion as delivered, and is
   refused and recorded exactly as it is behind the baseline receiver.

The helpers come from the webhook route's tests, and the steps to an approved plan from the API's. Every body is
synthetic, and the only network connections are on 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from conftest import payment_event
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_api import create_run, diagnose, post
from test_api_webhooks import (
    CLOCK_MS,
    EVENT_ID,
    OPERATOR,
    OTHER_STATE_CHANGING_ROUTES,
    PATH_PARAMETERS,
    RUN_ID,
    SIGNATURE,
    TOKEN,
    TOO_DEEP,
    UI_HEADER,
    WEBHOOK,
    database,
    delivery_attempts,
    log_in,
    make_app,
    nested_event,
    read_response,
    request_head,
    seed_unit_run,
    served_on_loopback,
)
from uvicorn.protocols.http.h11_impl import H11Protocol

from recovery_workbench import api, executor, ingestion, plans, storage
from recovery_workbench.contracts import PlanStatus, ReceiveOutcome
from recovery_workbench.simulator import merchant, runtime, signing
from recovery_workbench.simulator.world import INTEGRATION_ID

# api.MAX_REQUEST_BODY_BYTES, stated here so the tests pin the server's limit by what it does at this size.
LIMIT = 64 * 1024
PIECE_BYTES = 4_096  # a body sent without a declared length reaches the app in pieces of this size
JSON_STATE_CHANGE = {**UI_HEADER, "content-type": "application/json"}  # what the UI sends with every state change
INTEGRATION_LIMIT = 65_536  # the unit run's integration limit (conftest.seed_run's default); checked below


# --------------------------------------------------------------------------- helpers


def json_array(size: int) -> bytes:
    """Valid JSON of exactly ``size`` bytes that no request model accepts: an array of empty objects."""
    body = b"[" + b",".join([b"{}"] * ((size - 1) // 3)) + b"]"
    return body + b" " * (size - len(body))  # JSON allows whitespace after the document


def sent_in_pieces(
    app: FastAPI, method: str, path: str, headers: dict[str, str] | list[tuple[str, str]], body: bytes
) -> tuple[httpx.Response, int]:
    """Send ``body`` in pieces the app pulls one at a time; also return how many bytes it pulled.

    Unless ``headers`` declares a content-length, the body goes chunked, with no length at all. A list of pairs can
    repeat a header, and the app receives every copy.
    """
    pulled = 0

    async def pieces() -> AsyncIterator[bytes]:
        nonlocal pulled
        for start in range(0, len(body), PIECE_BYTES):
            piece = body[start:start + PIECE_BYTES]
            pulled += len(piece)
            yield piece

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.request(method, path, content=pieces(), headers=headers)

    return asyncio.run(send()), pulled


def session_cookie(client: TestClient) -> dict[str, str]:
    """The header that carries ``client``'s operator session on a request sent outside it, as by ``sent_in_pieces``."""
    return {"cookie": f"{api.SESSION_COOKIE}={client.cookies[api.SESSION_COOKIE]}"}


def approved_plan(client: TestClient) -> tuple[str, dict]:
    """Flow A through the API to an approved reprocess plan, by the steps the API's tests take; the run ID and plan."""
    run_id = create_run(client)
    found = diagnose(client, run_id)["diagnosis"]
    validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate", {"diagnosis_id": found["diagnosis_id"]})
    plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
    post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
    return run_id, plan


def recovery_records(app: FastAPI, run_id: str, plan_id: str) -> dict[str, object]:
    """What executing the plan changes: its status, its actions, the run's fulfilments and the run's replay clock."""
    with closing(storage.connect(database(app))) as conn:
        return {"plan_status": plans.require_plan(conn, run_id=run_id, plan_id=plan_id).status,
                "actions": executor.list_actions(conn, run_id=run_id, plan_id=plan_id),
                "fulfilments": storage.list_fulfilments(conn, run_id=run_id),
                "replay_clock_ms": storage.require_run(conn, run_id).clock_ms}


def padded_event(event_id: str, size: int) -> tuple[dict[str, str], bytes]:
    """A provider-shaped event of exactly ``size`` bytes, signed as the provider signs; an ignored field pads it."""
    opening = signing.serialise_event(payment_event())[:-1] + b',"x_padding":"'
    body = opening + b"x" * (size - len(opening) - 2) + b'"}'
    return {EVENT_ID: event_id, SIGNATURE: signing.sign_body(body, merchant.PROVIDER_SIGNING_REF)}, body


def amplifying_body() -> bytes:
    """The audit's shape: 900 nested arrays around 1,148 zeros. 4,095 bytes, which receiver@2 grows about 900-fold."""
    return b"[" * 900 + b",".join([b"0"] * 1_148) + b"]" * 900


def deploy_receiver_2(app: FastAPI) -> None:
    """receiver@2, live before the run's clock: it re-serialises every body with indent=2 before verification."""
    with closing(storage.connect(database(app))) as conn:
        runtime.record_deployment(conn, run_id=RUN_ID, deploy_id="deploy_bounds_0001", variant="receiver@2",
                                  deployed_at_ms=CLOCK_MS - 1_000, summary="Store webhook bodies in one stable layout")


@dataclass
class ReceiverCalls:
    """What deployed receivers were given to prepare for verification, and what they made of it."""

    delivered: list[bytes] = field(default_factory=list)
    prepared: list[bytes] = field(default_factory=list)


def record_receiver_calls(monkeypatch: pytest.MonkeyPatch) -> ReceiverCalls:
    """From now on, record every body a deployed receiver prepares; the receiver itself runs unchanged."""
    calls = ReceiverCalls()
    prepare = merchant.DeployedMerchant.body_for_verification

    def recorded(deployed: merchant.DeployedMerchant, raw_body: bytes) -> bytes:
        calls.delivered.append(raw_body)  # before the call, so a receiver that raises is recorded too
        prepared = prepare(deployed, raw_body)
        calls.prepared.append(prepared)
        return prepared

    monkeypatch.setattr(merchant.DeployedMerchant, "body_for_verification", recorded)
    return calls


def received(app: FastAPI, path: str, headers: dict[str, str], body: bytes) -> tuple[int, object, list[object]]:
    """The answer to one delivery and the delivery records it left, without row IDs and latencies."""
    with TestClient(app) as provider:
        response = provider.post(path, content=body, headers=headers)
    records = [attempt.model_dump(mode="json", exclude={"id", "latency_ms"}) for attempt in delivery_attempts(app)]
    return response.status_code, response.json(), records


@pytest.fixture
def app(tmp_path: Path) -> Iterator[FastAPI]:
    application = make_app(tmp_path / "data")
    seed_unit_run(application)
    yield application
    application.state.workbench.shutdown()


@pytest.fixture
def logins(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The operator ID of every call that reached ``Workbench.login``."""
    called: list[str] = []
    login = api.Workbench.login

    def recorded(workbench: api.Workbench, token: str, operator_id: str,
                 **browser: str | None) -> tuple[str, api.OperatorSession]:
        called.append(operator_id)
        return login(workbench, token, operator_id, **browser)  # the session the browser presented, when the route says

    monkeypatch.setattr(api.Workbench, "login", recorded)
    return called


@pytest.fixture
def decoded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The path of every request whose body was decoded as JSON, as FastAPI decodes a route's body."""
    paths: list[str] = []
    decode = Request.json

    async def recorded(request: Request) -> object:
        paths.append(request.url.path)
        return await decode(request)

    monkeypatch.setattr(Request, "json", recorded)
    return paths


@pytest.fixture
def session_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The session ID of every call to ``Workbench.session``: each session check, and GET /api/session's own lookup."""
    looked_up: list[str | None] = []
    lookup = api.Workbench.session

    def recorded(workbench: api.Workbench, session_id: str | None) -> api.OperatorSession | None:
        looked_up.append(session_id)
        return lookup(workbench, session_id)

    monkeypatch.setattr(api.Workbench, "session", recorded)
    return looked_up


# --------------------------------------------------------------------------- the request body limit


@pytest.mark.parametrize(("method", "template"), OTHER_STATE_CHANGING_ROUTES,
                         ids=[f"{method} {template}" for method, template in OTHER_STATE_CHANGING_ROUTES])
def test_a_declared_length_over_the_limit_is_413_before_any_byte_is_read_on_every_route_but_the_webhook_route(
    app: FastAPI, logins: list[str], decoded: list[str], method: str, template: str
):
    body = json_array(2 * LIMIT)  # without a session: /api/session used to echo all of it back in a 422
    response, pulled = sent_in_pieces(app, method, template.format(**PATH_PARAMETERS),
                                      {**JSON_STATE_CHANGE, "content-length": str(len(body))}, body)
    assert response.status_code == 413
    assert response.text == "Content Too Large"  # the whole answer: nothing of the body comes back
    assert pulled == 0  # refused before the route read a byte
    assert decoded == []
    assert logins == []


# Each declares 2 * LIMIT, the length of the body sent with it; two of them also declare 2. Behind uvicorn's h11, which
# api.main runs, only the first reaches the app, and uvicorn answers the others 400 itself. Starlette's limiter reads
# only the first content-length, with int(), so it takes the last one's sign, underscore and spaces as well.
DECLARED_OVER_THE_LIMIT = [
    pytest.param([("content-length", str(2 * LIMIT))], id="one-length"),
    pytest.param([("content-length", str(2 * LIMIT)), ("content-length", "2")], id="the-larger-of-two-first"),
    pytest.param([("content-length", "2"), ("content-length", str(2 * LIMIT))], id="the-larger-of-two-second"),
    pytest.param([("content-length", f" +{2 * LIMIT:_} ")], id="written-as-int-reads-it"),
]


@pytest.mark.parametrize("declared", DECLARED_OVER_THE_LIMIT)
def test_with_a_session_a_logout_that_declares_a_length_over_the_limit_is_413_and_the_session_still_works(
    app: FastAPI, declared: list[tuple[str, str]]
):
    """Logout never reads its body, so behind Starlette's limiter alone it ran whatever length was declared."""
    with TestClient(app) as client:
        log_in(client)
        response, pulled = sent_in_pieces(app, "DELETE", "/api/session",
                                          [*JSON_STATE_CHANGE.items(), *session_cookie(client).items(), *declared],
                                          json_array(2 * LIMIT))
        session = client.get("/api/session").json()
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"  # the answer Starlette's limiter gives
    assert pulled == 0
    assert session == {"operator_id": OPERATOR}  # the logout never ran


def test_with_a_session_executing_an_approved_plan_while_declaring_a_length_over_the_limit_is_413_and_executes_nothing(
    app: FastAPI
):
    body = json_array(2 * LIMIT)
    with TestClient(app) as client:
        log_in(client)
        run_id, plan = approved_plan(client)
        execute = f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute"
        declared = {**JSON_STATE_CHANGE, **session_cookie(client), "content-length": str(len(body))}
        before = recovery_records(app, run_id, plan["plan_id"])
        response, pulled = sent_in_pieces(app, "POST", execute, declared, body)
        after = recovery_records(app, run_id, plan["plan_id"])
        executed = post(client, execute)  # as the UI sends it
    assert (response.status_code, response.text, pulled) == (413, "Content Too Large", 0)
    assert (before["plan_status"], before["actions"]) == (PlanStatus.APPROVED, ())
    assert after == before  # no action, no fulfilment, and neither the plan nor the replay clock moved
    assert (executed["status"], executed["replayed"]) == ("succeeded", False)  # executed now, for the first time


def test_with_a_session_a_read_that_declares_a_length_over_the_limit_is_413_before_its_handler_runs(
    app: FastAPI, session_lookups: list[str | None]
):
    """GET /api/session's handler is one session lookup and nothing else, so no lookup means it never ran."""
    body = json_array(2 * LIMIT)
    with TestClient(app) as client:
        log_in(client)
        response, pulled = sent_in_pieces(app, "GET", "/api/session",
                                          {**session_cookie(client), "content-length": str(len(body))}, body)
    assert (response.status_code, response.text, pulled) == (413, "Content Too Large", 0)
    assert session_lookups == []


@pytest.mark.parametrize("declared", ["lots", "0x20000", f"{8 * LIMIT}, {8 * LIMIT}"],
                         ids=["words", "hexadecimal", "a-folded-pair"])
def test_a_content_length_that_int_cannot_read_declares_nothing_and_the_body_is_stopped_once_it_passes_the_limit(
    app: FastAPI, logins: list[str], decoded: list[str], declared: str
):
    """As for Starlette's limiter. Behind uvicorn's h11 none of these reaches the app as written: uvicorn answers the
    first two 400, and h11 merges the folded pair into one length, which is refused before any route runs."""
    response, pulled = sent_in_pieces(app, "POST", "/api/session", {**JSON_STATE_CHANGE, "content-length": declared},
                                      json_array(8 * LIMIT))
    assert response.status_code == 413
    assert response.json() == {"detail": "Content Too Large"}  # the answer to a body sent without a length
    assert LIMIT < pulled <= LIMIT + PIECE_BYTES
    assert decoded == []
    assert logins == []


@pytest.mark.parametrize("path", ["/api/session", f"/api/runs/{RUN_ID}/advance"], ids=["login", "advance"])
def test_over_a_real_loopback_server_a_body_announced_over_the_limit_is_413_before_it_is_sent(app: FastAPI, path: str):
    """Served as ``api.main`` serves it: the answer comes while the sender still holds back all but three bytes."""
    head = request_head(path, UI_HEADER, 8 * LIMIT)
    with served_on_loopback(app, H11Protocol) as port, \
            closing(socket.create_connection(("127.0.0.1", port), timeout=10)) as client:
        client.sendall(head + b"[{}")
        status_line, headers, body = read_response(client)
    assert status_line.startswith("HTTP/1.1 413 ")
    assert body == b"Content Too Large"
    assert headers["content-security-policy"] == api.SECURITY_HEADERS["Content-Security-Policy"]  # the guard ran first


@pytest.mark.parametrize("path", ["/api/session", f"/api/runs/{RUN_ID}/advance"], ids=["login", "advance"])
def test_a_body_sent_without_a_length_is_stopped_once_it_passes_the_limit(
    app: FastAPI, logins: list[str], decoded: list[str], path: str
):
    response, pulled = sent_in_pieces(app, "POST", path, JSON_STATE_CHANGE, json_array(8 * LIMIT))
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json() == {"detail": "Content Too Large"}
    assert LIMIT < pulled <= LIMIT + PIECE_BYTES  # it stopped at the piece that took the body past the limit
    assert decoded == []
    assert logins == []


def test_a_login_body_one_byte_over_the_limit_never_reaches_login_and_one_at_the_limit_logs_in(
    app: FastAPI, logins: list[str]
):
    def padded_login(size: int) -> bytes:
        compact = json.dumps({"token": TOKEN, "operator_id": OPERATOR}).encode()
        return compact + b" " * (size - len(compact))  # well-formed: JSON allows whitespace after the document

    with TestClient(app) as client:
        over_the_limit = client.post("/api/session", content=padded_login(LIMIT + 1), headers=JSON_STATE_CHANGE)
        at_the_limit = client.post("/api/session", content=padded_login(LIMIT), headers=JSON_STATE_CHANGE)
    assert (over_the_limit.status_code, at_the_limit.status_code) == (413, 200)
    assert "set-cookie" not in over_the_limit.headers
    assert logins == [OPERATOR]  # only the body at the limit reached login


def test_the_largest_legitimate_body_is_well_under_the_limit_and_reaches_its_route(app: FastAPI):
    """RefreshRequest is the largest request model: MAX_REFRESH_PAYMENTS IDs of 128 characters, written compactly
    as the UI's JSON.stringify writes them."""
    payment_ids = [f"pay_{index:03d}".ljust(128, "x") for index in range(api.MAX_REFRESH_PAYMENTS)]
    largest = json.dumps({"payment_ids": payment_ids}, separators=(",", ":")).encode()
    assert 2 * len(largest) < LIMIT
    with TestClient(app) as client:
        log_in(client)
        response = client.post(f"/api/runs/{RUN_ID}/refresh", content=largest, headers=JSON_STATE_CHANGE)
    # Only the route raises this: the hand-made run has no provider records. So the body was read, decoded, validated
    # and authorised in full.
    assert (response.status_code, response.json()["error"]) == (422, "MissingServerData")


def test_a_webhook_delivery_over_the_request_limit_still_reaches_its_route_and_is_recorded(app: FastAPI):
    headers, body = padded_event("evt_bounds_0001", max(LIMIT, INTEGRATION_LIMIT) + 1)
    with TestClient(app) as provider:
        response = provider.post(WEBHOOK, content=body, headers=headers)
    assert (response.status_code, response.json()) == (413, {"outcome": "rejected_size"})
    [attempt] = delivery_attempts(app)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SIZE
    assert attempt.detail == f"body of {len(body)} bytes exceeds the integration limit"
    assert attempt.body_sha256 == hashlib.sha256(body).hexdigest()


# --------------------------------------------------------------------------- what the deployed receiver may read


OUTSIDE_THE_LIMITS = [
    pytest.param(WEBHOOK, *padded_event("evt_bounds_0002", INTEGRATION_LIMIT + 1), id="over-the-integration-limit"),
    pytest.param(WEBHOOK, *nested_event("evt_bounds_0003", ingestion.MAX_JSON_NESTING), id="one-level-too-deep"),
    pytest.param(WEBHOOK, *nested_event("evt_bounds_0004", TOO_DEEP), id="too-deep-for-the-parser"),
    pytest.param(WEBHOOK, {}, amplifying_body(), id="unsigned-and-amplifying"),
    # No integration, so no limit to be within: ingestion refuses it before any check the receiver's output could pass.
    pytest.param(f"/api/runs/{RUN_ID}/webhooks/int_SYNTHETIC_UNKNOWN", *padded_event("evt_bounds_0005", 2_000),
                 id="unknown-integration"),
]


@pytest.mark.parametrize(("path", "headers", "body"), OUTSIDE_THE_LIMITS)
def test_with_receiver_2_live_a_body_outside_the_limits_never_reaches_it_and_is_recorded_as_behind_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, headers: dict[str, str], body: bytes
):
    baseline = make_app(tmp_path / "baseline")
    seed_unit_run(baseline)
    deployed = make_app(tmp_path / "receiver_2")
    seed_unit_run(deployed)
    deploy_receiver_2(deployed)
    expected = received(baseline, path, headers, body)
    calls = record_receiver_calls(monkeypatch)
    observed = received(deployed, path, headers, body)
    assert calls.delivered == []  # receiver@2 never read these bytes
    assert observed == expected  # the same answer and the same record as behind the baseline receiver
    for application in (baseline, deployed):
        application.state.workbench.shutdown()


@pytest.mark.parametrize(("headers", "body", "outcome"), [
    pytest.param(*padded_event("evt_bounds_0006", INTEGRATION_LIMIT), ReceiveOutcome.REJECTED_SIZE,
                 id="size-at-the-integration-limit"),
    pytest.param(*nested_event("evt_bounds_0007", ingestion.MAX_JSON_NESTING - 1), ReceiveOutcome.REJECTED_SIGNATURE,
                 id="nesting-at-the-limit"),
])
def test_with_receiver_2_live_a_body_at_the_limits_is_still_prepared_by_it_as_before(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], body: bytes, outcome: ReceiveOutcome
):
    with closing(storage.connect(database(app))) as conn:
        integration = storage.get_integration(conn, run_id=RUN_ID, integration_id=INTEGRATION_ID)
    assert integration is not None
    assert integration.max_body_bytes == INTEGRATION_LIMIT
    deploy_receiver_2(app)
    calls = record_receiver_calls(monkeypatch)
    with TestClient(app) as provider:
        response = provider.post(WEBHOOK, content=body, headers=headers)
    [attempt] = delivery_attempts(app)
    assert calls.delivered == [body]
    [prepared] = calls.prepared
    assert prepared != body  # receiver@2 re-serialised it, so the provider's signature no longer matches
    assert attempt.body_sha256 == hashlib.sha256(prepared).hexdigest()  # ingestion checked what receiver@2 made of it
    assert (attempt.outcome, response.status_code) == (outcome, ingestion.HTTP_STATUS_BY_OUTCOME[outcome])
