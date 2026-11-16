"""The hosted model (owner answer to Q46) against a stub OpenAI-compatible server on the container's loopback.

What the client sends, a validated hosted diagnosis and its labels in the store, the report and the receipt, every
failure path (each leaves the diagnosis needing review with an escaped reason that never holds the key), the daily
call budget, redirects, the reply cap and the environment's proxies. No real provider is called, and the test
container has no network.
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
import ssl
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from stub_servers import (
    HOSTED_KEY,
    HOSTED_MODEL,
    MIXED_CASE_KEY,
    Answer,
    closed_port_url,
    completion,
    hosted_env,
    json_answer,
    key_parts_in,
    stub_server,
)
from test_tools import seed

from recovery_workbench import cli, diagnosis, diagnosis_store, hosted_model, packet, report, selective, settings, storage
from recovery_workbench.contracts import ContractError, DiagnosisStatus, EvidencePacket, ModelMode
from recovery_workbench.diagnosis import ChatMessage, ChatRequest
from recovery_workbench.settings import Secret
from recovery_workbench.simulator import runtime

SEED = 20801  # the model chain's seeds are 20801-20900
RECEIPT_SEED = 20802
LOOPBACK = "127.0.0.1"
HOSTED_LABEL = f"HOSTED: {HOSTED_MODEL} via {LOOPBACK}, not part of the M4 evaluation"
OPERATOR = "op_hosted_tester"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sqlite3.Connection, str, str]]:
    conn, summary = seed(tmp_path_factory.mktemp("hosted_world"), "worker_backlog", SEED)
    assert summary.incident_ids
    try:
        yield conn, summary.run_id, summary.incident_ids[-1]
    finally:
        conn.close()


def packet_now(conn: sqlite3.Connection, run_id: str, incident_id: str) -> EvidencePacket:
    clock = storage.require_run(conn, run_id).clock_ms
    return packet.build_packet(conn, run_id=run_id, incident_id=incident_id, cutoff_ms=clock,
                               deployed=runtime.active_merchant(conn, run_id=run_id, at_ms=clock), persist=False)


def valid_content(conn: sqlite3.Connection, run_id: str, incident_id: str) -> str:
    """An answer the validator accepts for the packet a diagnosis would build now: the rules baseline's own."""
    return json.dumps(selective.rules_v3(packet_now(conn, run_id, incident_id)).model_dump(mode="json"))


def make_client(base_url: str, *, budget: hosted_model.HostedCallBudget | None = None, key: str = HOSTED_KEY,
                **options: object) -> hosted_model.HostedClient:
    return hosted_model.HostedClient(base_url=f"{base_url}/v1", model=HOSTED_MODEL, api_key=Secret(key),
                                     budget=budget or hosted_model.HostedCallBudget(50), **options)  # type: ignore[arg-type]


def configured(base_url: str, *, timeout_s: int = 20) -> settings.Settings:
    return settings.load_settings({"WORKBENCH_RUN_TIMEOUT_SECONDS": str(timeout_s), **hosted_env(base_url)})


def diagnose_hosted(conn: sqlite3.Connection, run_id: str, incident_id: str, client: hosted_model.HostedClient, *,
                    timeout_s: int = 20, recordings: Path | None = None) -> diagnosis_store.StoredDiagnosis:
    clock = storage.require_run(conn, run_id).clock_ms
    return diagnosis_store.diagnose_incident(
        conn, run_id=run_id, incident_id=incident_id, mode=ModelMode.HOSTED,
        settings=configured("http://127.0.0.1:9", timeout_s=timeout_s),
        deployed=runtime.active_merchant(conn, run_id=run_id, at_ms=clock), hosted=client, recordings_dir=recordings,
    )


def request() -> ChatRequest:
    return ChatRequest(messages=(ChatMessage(role="user", content="Answer with one JSON object."),),
                       response_schema={"type": "object"}, think=True)


# --------------------------------------------------------------------------- a validated hosted diagnosis


def test_a_hosted_diagnosis_is_validated_stored_and_labelled_hosted_never_local(world, tmp_path: Path) -> None:
    conn, run_id, incident_id = world
    content = valid_content(conn, run_id, incident_id)
    recordings = tmp_path / "recordings"
    with stub_server(lambda _: completion(content)) as provider, make_client(provider.url) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client, recordings=recordings)
    assert (stored.mode, stored.status) == (ModelMode.HOSTED, DiagnosisStatus.VALIDATED)
    assert (stored.model_id, stored.provider_host, stored.model_digest, stored.thinking) == (
        HOSTED_MODEL, LOOPBACK, None, False)
    assert (stored.prompt_version, stored.validator_version) == (diagnosis.PROMPT_VERSION, diagnosis.VALIDATOR_VERSION)
    assert stored.mode_label == HOSTED_LABEL
    assert "local" not in stored.mode_label
    assert stored.output == selective.rules_v3(packet_now(conn, run_id, incident_id))
    assert [item.model_dump() for item in stored.token_usage] == [
        {"call_index": 0, "prompt_tokens": 1200, "completion_tokens": 150}]
    assert diagnosis_store.get_diagnosis(conn, run_id=run_id, diagnosis_id=stored.diagnosis_id) == stored
    row = conn.execute("SELECT mode, model_id, model_digest, thinking, output FROM diagnoses WHERE run_id = ? AND "
                       "diagnosis_id = ?", (run_id, stored.diagnosis_id)).fetchone()
    assert (row["mode"], row["model_id"], row["model_digest"], row["thinking"]) == ("hosted", HOSTED_MODEL, None, 0)
    envelope = json.loads(row["output"])
    assert (envelope["provider_host"], envelope["token_usage"][0]["completion_tokens"]) == (LOOPBACK, 150)
    assert "STUB-REASONING-TEXT" not in row["output"]  # a reasoning field is never read, so never stored
    assert not recordings.exists()  # recorded mode replays local outputs only, so a hosted one is never recorded
    with pytest.raises(ContractError, match="no recorded local output"):  # and is not replayed from the store either
        diagnosis_store.diagnose_incident(
            conn, run_id=run_id, incident_id=incident_id, mode=ModelMode.RECORDED, settings=configured(provider.url),
            deployed=runtime.active_merchant(conn, run_id=run_id, at_ms=storage.require_run(conn, run_id).clock_ms))


def test_the_request_is_the_local_models_prompt_and_schema_with_the_key_to_the_configured_host(world) -> None:
    conn, run_id, incident_id = world
    content = valid_content(conn, run_id, incident_id)
    built = packet_now(conn, run_id, incident_id)
    with stub_server(lambda _: completion(content)) as provider, make_client(provider.url) as client:
        diagnose_hosted(conn, run_id, incident_id, client)
    [sent] = provider.requests
    assert (sent.method, sent.path) == ("POST", "/v1/chat/completions")
    assert sent.headers["authorization"] == f"Bearer {HOSTED_KEY}"
    assert sent.headers["accept-encoding"] == "identity"
    body = sent.json()
    assert body == {
        "model": HOSTED_MODEL,
        "messages": [message.model_dump() for message in diagnosis.initial_messages(built)],  # diag-p5, unchanged
        "stream": False,
        "temperature": 0.0,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "diagnosis", "schema": diagnosis.build_response_schema(built)}},
    }


@pytest.mark.parametrize(("response_format", "expected"), [
    ("json_schema_strict", {"type": "json_schema", "json_schema": {"name": "diagnosis", "schema": {"type": "object"},
                                                                   "strict": True}}),
    ("json_schema", {"type": "json_schema", "json_schema": {"name": "diagnosis", "schema": {"type": "object"}}}),
    ("json_object", {"type": "json_object"}),
    ("none", None),
])
def test_the_request_asks_for_json_as_the_format_setting_says(response_format: str, expected: object) -> None:
    body = make_client("https://provider.invalid", response_format=response_format).request_body(request())
    assert body.get("response_format") == expected
    assert "think" not in body  # Ollama's switch has no counterpart in this protocol


def test_temperature_and_reasoning_effort_are_sent_only_as_the_settings_say() -> None:
    defaults = make_client("https://provider.invalid").request_body(request())
    assert (defaults["temperature"], "reasoning_effort" in defaults) == (0.0, False)
    left_out = settings.load_settings({**hosted_env("https://provider.invalid"), "WORKBENCH_HOSTED_TEMPERATURE": "none",
                                       "WORKBENCH_HOSTED_REASONING_EFFORT": "low",
                                       "WORKBENCH_HOSTED_RESPONSE_FORMAT": "json_object"})
    with hosted_model.client_from_settings(left_out, hosted_model.HostedCallBudget(1)) as client:
        body = client.request_body(request())
    assert "temperature" not in body
    assert (body["reasoning_effort"], body["response_format"]) == ("low", {"type": "json_object"})


def test_the_diagnosis_schema_meets_the_strict_structured_output_rules(world) -> None:
    """Strict modes (Groq, Anthropic's layer) need every property required and additionalProperties false on every
    object. The schema is never changed per provider, so this guards it."""
    conn, run_id, incident_id = world
    schema = diagnosis.build_response_schema(packet_now(conn, run_id, incident_id))

    def objects(node: object) -> Iterator[dict]:
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for value in node.values():
                yield from objects(value)
        elif isinstance(node, list):
            for value in node:
                yield from objects(value)

    found = list(objects(schema))
    assert found
    for node in found:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


# --------------------------------------------------------------------------- failures: each needs review, with why


def raw(status: int, body: bytes, content_type: str = "text/plain; charset=utf-8", **headers: str) -> Answer:
    return Answer(status=status, body=body, headers={"Content-Type": content_type, **headers})


FAILURES = {
    "server error, with control characters": (
        raw(500, b"upstream overloaded \x1b[2J\nINFO:     forged line"),
        r"call 1: the hosted model answered HTTP 500: upstream overloaded \\u001b\[2J INFO: forged line"),
    "rate limited": (
        json_answer(429, {"error": {"message": "Rate limit reached for requests"}}, **{"Retry-After": "30"}),
        r"HTTP 429 \(Retry-After: 30\): .*Rate limit reached for requests"),
    "JSON mode not met, as an error status": (
        json_answer(400, {"errors": [{"message": "JSON Mode couldn't be met", "code": 5025}], "success": False}),
        r"HTTP 400: .*JSON Mode couldn't be met"),
    "JSON mode not met, with status 200": (
        json_answer(200, {"errors": [{"message": "JSON Mode couldn't be met"}], "success": False}),
        r"reply has no choices: .*JSON Mode couldn't be met"),
    "a body that is not JSON": (raw(200, b"<html>gateway</html>", "application/json"), r"is not strict JSON"),
    "NaN": (raw(200, b'{"choices": [], "temperature": NaN}', "application/json"), r"not strict JSON: NaN is not JSON"),
    "a key twice": (raw(200, b'{"choices": [], "choices": []}', "application/json"), r"names a key twice"),
    "not application/json": (raw(200, b"<html>login</html>", "text/html"), r"reply is text/html, not application/json"),
    "a refusal": (
        json_answer(200, {"choices": [{"message": {"role": "assistant", "content": None, "refusal": "I can't."}}]}),
        r"refused to answer: I can't\."),
    "no content": (json_answer(200, {"choices": [{"message": {"role": "assistant"}}]}), r"has no message content"),
    "compressed": (raw(200, gzip.compress(b"{}"), "application/json", **{"Content-Encoding": "gzip"}),
                   r"'gzip'-encoded reply although none was asked for"),
}


@pytest.mark.parametrize("case", list(FAILURES))
def test_a_provider_failure_leaves_the_diagnosis_needing_review_with_an_escaped_reason(world, case: str) -> None:
    conn, run_id, incident_id = world
    answer, reason = FAILURES[case]
    with stub_server(lambda _: answer) as provider, make_client(provider.url) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert (stored.mode, stored.status, stored.output) == (ModelMode.HOSTED, DiagnosisStatus.NEEDS_REVIEW, None)
    assert stored.mode_label == HOSTED_LABEL  # labelled hosted even when it failed: never local, never rules
    [why] = stored.reasons  # a provider failure ends the run: no repair is sent after it
    assert re.search(reason, why), why
    assert "\x1b" not in why and "\n" not in why
    assert HOSTED_KEY not in why
    assert len(provider.requests) == 1


def test_an_answer_that_is_not_json_gets_its_one_repair_and_then_needs_review(world) -> None:
    conn, run_id, incident_id = world
    with stub_server(lambda _: completion("this is not json")) as provider, make_client(provider.url) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert [reason.split(":")[0] for reason in stored.reasons] == ["call 1", "call 2"]
    assert all("the reply is not valid JSON" in reason for reason in stored.reasons)
    assert len(provider.requests) == len(stored.token_usage) == 2
    second = provider.requests[1].json()["messages"]
    assert [message["role"] for message in second[-2:]] == ["assistant", "user"]  # the validator's own repair turn


def test_a_provider_that_does_not_answer_in_time_leaves_the_diagnosis_needing_review(world) -> None:
    conn, run_id, incident_id = world
    slow = Answer(body=completion("{}").body, delay_s=6.0)
    started = time.monotonic()
    with stub_server(lambda _: slow) as provider, make_client(provider.url) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client, timeout_s=1)
    assert time.monotonic() - started < 5.0  # the deadline held; the stub was still asleep
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert re.search(r"did not answer within|no reply within the remaining", stored.reasons[0]), stored.reasons


def test_an_unreachable_provider_leaves_the_diagnosis_needing_review(world) -> None:
    conn, run_id, incident_id = world
    with make_client(closed_port_url()) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert "call 1: the hosted model call failed: ConnectError" in stored.reasons[0]


@pytest.mark.parametrize(("echo", "shown"), [
    (HOSTED_KEY, "Incorrect API key provided: <api key>"),
    (HOSTED_KEY[:20], "<the reply quotes part of the API key, so it is not shown>"),
])
def test_a_provider_that_quotes_the_key_never_puts_it_in_the_reason(world, echo: str, shown: str) -> None:
    conn, run_id, incident_id = world
    answer = json_answer(401, {"error": {"message": f"Incorrect API key provided: {echo}"}})
    with stub_server(lambda _: answer) as provider, make_client(provider.url) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert shown in stored.reasons[0]
    assert HOSTED_KEY[:12] not in stored.reasons[0]
    assert provider.requests[0].headers["authorization"] == f"Bearer {HOSTED_KEY}"  # sent, so the scrub is real


def finishing_with(reason: str, content: str = "") -> Answer:
    """A completion whose finish_reason is ``reason``; with no content, the diagnosis quotes it in its reasons."""
    payload = json.loads(completion(content).body)
    payload["choices"][0]["finish_reason"] = reason
    return Answer(body=json.dumps(payload).encode())


KEY_ECHOES = {  # where a provider puts the key, and what every reason shows instead
    "finish_reason": (lambda key: finishing_with(key), "(done_reason=None)"),
    "Content-Encoding": (
        lambda key: Answer(body=b"{}", headers={"Content-Type": "application/json", "Content-Encoding": key}),
        "sent a '<api key>'-encoded reply"),
    "Content-Encoding, in part": (
        lambda key: Answer(body=b"{}", headers={"Content-Type": "application/json", "Content-Encoding": key[:20]}),
        "sent a '<the reply quotes part of the API key, so it is not shown>'-encoded reply"),
    "Content-Type": (lambda key: Answer(body=b"{}", headers={"Content-Type": f"{key}; charset=utf-8"}),
                     "reply is <api key>, not application/json"),
}


@pytest.mark.parametrize("field", list(KEY_ECHOES))
def test_a_key_the_provider_echoes_in_any_reply_field_reaches_no_reason(world, field: str) -> None:
    """Security audit run 3: finish_reason and the Content-Encoding value skipped the key scrub, and the media type
    was lowercased before it, so a key with capitals in it slipped past. Every reason now holds no part of the key."""
    conn, run_id, incident_id = world
    echo, shown = KEY_ECHOES[field]
    answer = echo(MIXED_CASE_KEY)
    with stub_server(lambda _: answer) as provider, make_client(provider.url, key=MIXED_CASE_KEY) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert stored.reasons
    assert all(shown in reason for reason in stored.reasons), stored.reasons
    assert key_parts_in(" ".join(stored.reasons), MIXED_CASE_KEY) == []
    assert provider.requests[0].headers["authorization"] == f"Bearer {MIXED_CASE_KEY}"  # sent, so the scrub is real


def test_only_the_documented_finish_reasons_are_kept() -> None:
    """The allowlist itself: a documented value is kept as sent, anything else becomes None."""
    cases = (("stop", "stop"), ("length", "length"), ("content_filter", "content_filter"), ("tool_calls", "tool_calls"),
             ("STOP", None), ("eos", None), (["stop"], None), (None, None))
    with make_client("https://provider.invalid") as client:
        for sent, kept in cases:
            payload = json.loads(completion("{}").body)
            payload["choices"][0]["finish_reason"] = sent
            assert client._reply("application/json", json.dumps(payload).encode()).done_reason == kept, sent


DEEP = 50_000  # levels: 100,000 bytes of brackets, under the reply cap and past what Python's JSON reader takes


@pytest.mark.parametrize(("answer", "reason", "calls"), [
    pytest.param(Answer(body=b"[" * DEEP + b"]" * DEEP),
                 "call 1: the hosted model's reply nests JSON too deeply to read; it was not used", 1, id="the-reply"),
    pytest.param(completion("[" * DEEP + "]" * DEEP),
                 "call 1: the reply nests JSON too deeply to read; answer with one flat JSON object", 2,
                 id="its-answer"),
])
def test_a_reply_nested_too_deep_for_the_json_reader_needs_review(
    world, answer: Answer, reason: str, calls: int
) -> None:
    """Security audit run 3: the readers caught ValueError or JSONDecodeError only, and RecursionError is neither, so
    such a reply ended the diagnosis with an unexpected error and stored nothing. A reply that cannot be read ends the
    run at once, as any provider failure does; an answer that cannot be read gets its one repair first."""
    conn, run_id, incident_id = world
    with stub_server(lambda _: answer) as provider, make_client(provider.url) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert (stored.status, stored.output) == (DiagnosisStatus.NEEDS_REVIEW, None)
    assert stored.reasons[0] == reason
    assert len(stored.reasons) == calls
    assert len(provider.requests) == calls


# --------------------------------------------------------------------------- the daily budget


def test_the_daily_budget_counts_every_call_repairs_included_and_sends_nothing_over_it(world) -> None:
    conn, run_id, incident_id = world
    now = [datetime(2026, 11, 15, 23, 50, tzinfo=UTC).timestamp()]
    budget = hosted_model.HostedCallBudget(3, clock=lambda: now[0])
    with stub_server(lambda _: completion("not json")) as provider, make_client(provider.url, budget=budget) as client:
        first = diagnose_hosted(conn, run_id, incident_id, client)
        assert (len(provider.requests), budget.used_today()) == (2, 2)  # the call and its repair
        second = diagnose_hosted(conn, run_id, incident_id, client)
        assert len(provider.requests) == 3  # its repair was over the budget, so it was not sent
        third = diagnose_hosted(conn, run_id, incident_id, client)
        assert len(provider.requests) == 3  # nothing was sent at all
        now[0] += 15 * 60  # 00:05 UTC: a new day
        fourth = diagnose_hosted(conn, run_id, incident_id, client)
        assert len(provider.requests) == 5
    assert all(stored.status is DiagnosisStatus.NEEDS_REVIEW for stored in (first, second, third, fourth))
    assert second.reasons[-1].startswith("call 2: the hosted model's daily call budget is used up: 3 calls on "
                                         "2026-11-15 (UTC)")
    assert third.reasons == (third.reasons[0],) and "no call was sent" in third.reasons[0]
    assert third.reasons[0].startswith("call 1: the hosted model's daily call budget is used up")
    assert [len(stored.token_usage) for stored in (first, second, third, fourth)] == [2, 2, 1, 2]


def test_a_budget_needs_at_least_one_call() -> None:
    with pytest.raises(ValueError, match="at least 1 call"):
        hosted_model.HostedCallBudget(0)


# --------------------------------------------------------------------------- the transport's limits


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_not_followed_so_the_key_reaches_no_other_host(world, status: int) -> None:
    conn, run_id, incident_id = world
    with stub_server(lambda _: completion("{}")) as elsewhere:
        moved = Answer(status=status, headers={"Location": f"{elsewhere.url}/v1/chat/completions"})
        with stub_server(lambda _: moved) as provider, make_client(provider.url) as client:
            stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert f"answered HTTP {status}, a redirect, which is not followed" in stored.reasons[0]
    assert elsewhere.requests == []


@pytest.mark.parametrize(("chunked", "reason"), [
    (False, "reply declares {size} bytes, over the 4096-byte limit; it was not read"),
    (True, "reply passed the 4096-byte limit; the rest was not read"),  # no length declared: stopped at the cap
])
def test_a_reply_over_the_size_cap_is_not_read(world, chunked: bool, reason: str) -> None:
    conn, run_id, incident_id = world
    big = completion("x" * 10_000)
    reason = reason.format(size=len(big.body))
    answer = Answer(body=big.body, chunked=chunked)
    with stub_server(lambda _: answer) as provider, make_client(provider.url, max_response_bytes=4096) as client:
        stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert reason in stored.reasons[0]
    assert hosted_model.MAX_RESPONSE_BYTES == 256 * 1024


def test_the_environments_proxy_settings_are_ignored_so_the_key_goes_to_the_configured_host_only(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, run_id, incident_id = world
    content = valid_content(conn, run_id, incident_id)
    with stub_server(lambda _: completion("{}")) as proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(name, proxy.url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        with stub_server(lambda _: completion(content)) as provider, make_client(provider.url) as client:
            stored = diagnose_hosted(conn, run_id, incident_id, client)
    assert stored.status is DiagnosisStatus.VALIDATED
    assert proxy.requests == []
    assert len(provider.requests) == 1


def test_tls_is_verified() -> None:
    # httpx 0.28.1, pinned by uv.lock, keeps the connection pool's SSL context here.
    with make_client("https://provider.invalid") as client:
        context = client._http._transport._pool._ssl_context  # type: ignore[attr-defined]
    assert (context.verify_mode, context.check_hostname) == (ssl.CERT_REQUIRED, True)


# --------------------------------------------------------------------------- refusals before any call


def test_the_client_refuses_what_the_settings_refuse() -> None:
    budget = hosted_model.HostedCallBudget(1)
    with pytest.raises(ContractError, match="plain http is allowed only to a loopback host"):
        hosted_model.HostedClient(base_url="http://provider.invalid/v1", model=HOSTED_MODEL, api_key=Secret(HOSTED_KEY),
                                  budget=budget)
    with pytest.raises(ContractError, match="API key must be") as refused:
        hosted_model.HostedClient(base_url="https://provider.invalid/v1", model=HOSTED_MODEL,
                                  api_key=Secret("has a space in it"), budget=budget)
    assert "space in it" not in str(refused.value)
    with pytest.raises(ContractError, match="not a hosted model ID"):
        hosted_model.HostedClient(base_url="https://provider.invalid/v1", model="bad model", api_key=Secret(HOSTED_KEY),
                                  budget=budget)
    with pytest.raises(ContractError, match="hosted mode is not configured on this server: WORKBENCH_HOSTED_API_KEY"):
        hosted_model.client_from_settings(
            settings.load_settings({"WORKBENCH_HOSTED_URL": "https://provider.invalid/v1",
                                    "WORKBENCH_HOSTED_MODEL": HOSTED_MODEL}), budget)
    with closing(sqlite3.connect(":memory:")) as conn, pytest.raises(ContractError, match="needs a hosted model client"):
        diagnosis_store.diagnose_incident(
            conn, run_id="run_none", incident_id="inc_none", mode=ModelMode.HOSTED, settings=settings.load_settings({}),
            deployed=None)  # type: ignore[arg-type]  # refused before the packet is built


# --------------------------------------------------------------------------- the report and the receipt


def test_a_recovery_from_a_hosted_diagnosis_reports_and_receipts_it_as_hosted(tmp_path: Path) -> None:
    conn, summary = seed(tmp_path, "worker_backlog", RECEIPT_SEED)
    run_id, incident_id = summary.run_id, summary.incident_ids[-1]
    try:
        content = valid_content(conn, run_id, incident_id)
        with stub_server(lambda _: completion(content)) as provider, make_client(provider.url) as client:
            hosted = diagnose_hosted(conn, run_id, incident_id, client)
        assert hosted.status is DiagnosisStatus.VALIDATED
        assert hosted.output is not None and hosted.output.proposed_operation.value == "reprocess_inbox"
        validation = next(
            item for item in (cli.validate_fix(conn, run_id=run_id, fix_id=option.fix_id,
                                               diagnosis_id=hosted.diagnosis_id, operator=OPERATOR)
                              for option in cli._candidate_fixes(conn, run_id, hosted.output.faulting_change_id))
            if item.passed)
        preview = cli.build_plan(conn, run_id=run_id, fix_validation_id=validation.validation_id)
        cli.approve_plan(conn, run_id=run_id, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                         operator=OPERATOR)
        executed = cli.execute_plan(conn, run_id=run_id, plan_id=preview.plan.plan_id)
        exported = report.build_report(conn, run_id=run_id)
        receipt = report.build_receipt(conn, run_id=run_id, action_id=executed.action_id)
    finally:
        conn.close()
    [entry] = [item for item in exported["diagnoses"] if item["diagnosis_id"] == hosted.diagnosis_id]
    assert (entry["mode"], entry["mode_label"], entry["provider_host"]) == ("hosted", HOSTED_LABEL, LOOPBACK)
    assert (entry["model_id"], entry["model_digest"], entry["thinking"]) == (HOSTED_MODEL, None, False)
    assert "token_usage" not in entry  # kept in the stored envelope, not exported
    proposal = receipt["proposal"]
    assert (proposal["mode"], proposal["provider_host"], proposal["mode_label"]) == ("hosted", LOOPBACK, HOSTED_LABEL)
    assert (receipt["versions"]["model"], receipt["versions"]["model_digest"]) == (HOSTED_MODEL, None)
    assert receipt["receipt_hash"] == report.receipt_hash_of(receipt)  # the hosted fields are hashed with the rest
    markdown = report.render_receipt_markdown(receipt).splitlines()
    assert "- Mode: hosted; status: validated" in markdown
    assert "- Source: HOSTED: test\\-model\\-1 via 127\\.0\\.0\\.1, not part of the M4 evaluation" in markdown
    assert "- model digest: not reported by a hosted provider" in markdown
