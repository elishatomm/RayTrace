"""The owner's model chain (Q46): hosted settings checked at start, auto resolved once, and the source the API shows.

Stub servers on the container's loopback stand in for Ollama and for an OpenAI-compatible provider; the container has
no network. The last test scans everything this process wrote or answered for the API key.
"""
from __future__ import annotations

import io
import json
import logging
import time
from collections.abc import Callable
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from stub_servers import (
    HOSTED_KEY,
    HOSTED_MODEL,
    MIXED_CASE_KEY,
    OLLAMA_DIGEST,
    Answer,
    Seen,
    completion,
    hosted_env,
    json_answer,
    key_parts_in,
    ollama_listing,
    stub_server,
)
from test_diagnose_incident import FakeModel

from recovery_workbench import api, cli, diagnosis, model_source, packet, selective, settings, storage
from recovery_workbench.contracts import EvidencePacket, ModelMode
from recovery_workbench.settings import Secret
from recovery_workbench.simulator import runtime

TOKEN = "test-operator-token-not-a-secret"
OPERATOR = "op_model_chain_tester"
STATE_CHANGE = {api.REQUEST_HEADER: "1"}
SEED = 20811  # the model chain's seeds are 20801-20900
CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/0000aaaa1111bbbb2222cccc3333dddd/ai/v1"
CLOUDFLARE_MODEL = "@cf/openai/gpt-oss-20b"
NO_OLLAMA = {"WORKBENCH_OLLAMA_URL": "http://127.0.0.1:9"}  # nothing listens on the discard port in the container


# --------------------------------------------------------------------------- settings, checked at start


@pytest.mark.parametrize("url", [
    CLOUDFLARE_URL, "https://provider.invalid/v1", "https://provider.invalid:8443/openai/v1/",
    "http://127.0.0.1:8080/v1", "http://localhost:1234/v1", "http://[::1]:8080/v1",
])
def test_an_https_url_or_plain_http_to_loopback_is_accepted_with_its_path(url: str) -> None:
    hosted = settings.load_settings({"WORKBENCH_HOSTED_URL": url}).hosted
    assert hosted.url == url
    assert not hosted.configured and hosted.missing() == ("WORKBENCH_HOSTED_MODEL", "WORKBENCH_HOSTED_API_KEY")


@pytest.mark.parametrize(("url", "problem", "shown"), [
    ("http://provider.invalid/v1", "plain http is allowed only to a loopback host", "'http://provider.invalid/v1'"),
    ("https://user:pa55word@provider.invalid/v1", "must not carry a user name or password",
     "'https://<hidden>@provider.invalid/v1'"),
    ("https://provider.invalid/v1?key=abc123", "must not carry a query", "'https://provider.invalid/v1?<hidden>'"),
    ("https://provider.invalid/v1#frag", "must not carry a query or a fragment", "'https://provider.invalid/v1#<hidden>'"),
    ("ftp://provider.invalid/v1", "must be an https URL with a host", "'ftp://provider.invalid/v1'"),
    ("https:///v1", "must be an https URL with a host", "'https:///v1'"),
    ("https://provider.invalid:99999/v1", "is not a well-formed URL", "'https://provider.invalid:99999/v1'"),
    ("https://provider.invalid/ v1", "must be printable ASCII with no spaces", ""),
    ("https://api.cloudflare.com/client/v4/accounts/<account-id>/ai/v1", "replace any <placeholder>", ""),
    ("https://provider.invalid/v1/chat/completions", "without /chat/completions", ""),
])
def test_a_hosted_url_that_could_leak_the_key_or_cannot_work_is_refused_with_its_secrets_masked(
    url: str, problem: str, shown: str
) -> None:
    with pytest.raises(ValueError, match="WORKBENCH_HOSTED_URL") as refused:
        settings.load_settings({"WORKBENCH_HOSTED_URL": url})
    assert problem in str(refused.value)
    assert shown in str(refused.value)
    assert "pa55word" not in str(refused.value) and "abc123" not in str(refused.value)


@pytest.mark.parametrize("model", [CLOUDFLARE_MODEL, "gpt-4o-mini", "meta-llama/Llama-3.1-8B-Instruct", "@hf/x:1.0"])
def test_hosted_model_ids_as_providers_write_them_are_accepted(model: str) -> None:
    assert settings.load_settings({"WORKBENCH_HOSTED_MODEL": model}).hosted.model == model


@pytest.mark.parametrize("model", ["has space", "quote\"d", "tab\tbed", "line\nbreak", "x" * 129, "-leading", "'q'"])
def test_a_hosted_model_id_with_spaces_controls_or_quotes_is_refused(model: str) -> None:
    with pytest.raises(ValueError, match="WORKBENCH_HOSTED_MODEL must be a model ID"):
        settings.load_settings({"WORKBENCH_HOSTED_MODEL": model})


@pytest.mark.parametrize("key", ["short", "has a space-in-it-0123", "line-break\n-0123456789", "x" * 513])
def test_a_malformed_key_is_refused_without_showing_any_of_it(key: str) -> None:
    with pytest.raises(ValueError, match="WORKBENCH_HOSTED_API_KEY must be") as refused:
        settings.load_settings({"WORKBENCH_HOSTED_API_KEY": key})
    assert f"got {len(key)} characters, not shown" in str(refused.value)
    assert key[:5] not in str(refused.value).split("got", 1)[1]


@pytest.mark.parametrize(("name", "value", "problem"), [
    ("WORKBENCH_HOSTED_MAX_CALLS_PER_DAY", "0", "must be a positive integer"),
    ("WORKBENCH_HOSTED_MAX_CALLS_PER_DAY", "fifty", "must be a positive integer"),
    ("WORKBENCH_HOSTED_RESPONSE_FORMAT", "json", "must be one of ['json_object', 'json_schema', 'json_schema_strict', "
                                                 "'none']"),
    ("WORKBENCH_HOSTED_TEMPERATURE", "2.5", "must be a number from 0 to 2, or none"),
    ("WORKBENCH_HOSTED_TEMPERATURE", "nan", "must be a number from 0 to 2, or none"),
    ("WORKBENCH_HOSTED_TEMPERATURE", "-0.1", "must be a number from 0 to 2, or none"),
    ("WORKBENCH_HOSTED_REASONING_EFFORT", "max", "must be one of ['high', 'low', 'medium', 'none']"),
])
def test_every_other_hosted_setting_is_checked_at_start(name: str, value: str, problem: str) -> None:
    with pytest.raises(ValueError, match=name) as refused:
        settings.load_settings({name: value})
    assert problem in str(refused.value)


def test_the_hosted_defaults_and_the_values_that_send_nothing() -> None:
    hosted = settings.load_settings({}).hosted
    assert (hosted.max_calls_per_day, hosted.response_format, hosted.temperature, hosted.reasoning_effort) == (
        50, "json_schema", 0.0, None)
    chosen = settings.load_settings({"WORKBENCH_HOSTED_TEMPERATURE": "none", "WORKBENCH_HOSTED_REASONING_EFFORT": "none",
                                     "WORKBENCH_HOSTED_MAX_CALLS_PER_DAY": "7", "WORKBENCH_HOSTED_URL": "",
                                     "WORKBENCH_HOSTED_RESPONSE_FORMAT": "none"}).hosted
    assert (chosen.temperature, chosen.reasoning_effort, chosen.max_calls_per_day, chosen.url) == (None, "none", 7, None)


def test_explicit_hosted_mode_refuses_to_start_without_all_three_settings() -> None:
    with pytest.raises(ValueError, match=r"WORKBENCH_MODEL_MODE=hosted needs WORKBENCH_HOSTED_MODEL, "
                                         r"WORKBENCH_HOSTED_API_KEY set"):
        settings.load_settings({"WORKBENCH_MODEL_MODE": "hosted", "WORKBENCH_HOSTED_URL": CLOUDFLARE_URL})
    full = settings.load_settings({"WORKBENCH_MODEL_MODE": "hosted", "WORKBENCH_HOSTED_URL": CLOUDFLARE_URL,
                                   "WORKBENCH_HOSTED_MODEL": CLOUDFLARE_MODEL, "WORKBENCH_HOSTED_API_KEY": HOSTED_KEY})
    assert (full.model_mode, full.hosted.host, full.hosted.configured) == ("hosted", "api.cloudflare.com", True)


def test_the_ollama_url_stays_loopback_only_whatever_the_hosted_settings_say() -> None:
    with pytest.raises(ValueError, match="WORKBENCH_OLLAMA_URL must point at a loopback host"):
        settings.load_settings({"WORKBENCH_OLLAMA_URL": "https://provider.invalid:11434",
                                **hosted_env("https://provider.invalid")})
    with pytest.raises(ValueError, match="WORKBENCH_MODEL_MODE must be one of"):
        settings.load_settings({"WORKBENCH_MODEL_MODE": "cloud"})


def test_the_key_is_a_secret_that_no_repr_str_or_format_of_the_settings_shows() -> None:
    loaded = settings.load_settings(hosted_env("https://provider.invalid"))
    assert loaded.hosted.api_key is not None and loaded.hosted.api_key.reveal() == HOSTED_KEY
    for text in (repr(loaded), str(loaded), repr(loaded.hosted), f"{loaded.hosted.api_key}", str(loaded.hosted.api_key),
                 "%s" % loaded.hosted.api_key):  # noqa: UP031 - the old formatting path, on purpose
        assert HOSTED_KEY not in text
    assert Secret("one-secret-value") == Secret("one-secret-value") != Secret("another-value")


# --------------------------------------------------------------------------- auto, resolved once at start


def test_auto_takes_the_local_model_when_ollama_answers_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    with stub_server(ollama_listing("qwen3:8b")) as ollama, stub_server(lambda _: completion("{}")) as provider:
        loaded = settings.load_settings({"WORKBENCH_MODEL_MODE": "auto", "WORKBENCH_OLLAMA_URL": ollama.url,
                                         **hosted_env(provider.url)})
        source = model_source.resolve(loaded)
    assert (source.mode, source.model_id, source.provider_host) == (ModelMode.LOCAL, "qwen3:8b", None)
    assert source.label == "local model qwen3:8b"
    assert source.chosen_by == f"auto: the local model answered at {ollama.url}"
    assert [seen.path for seen in ollama.requests] == ["/api/tags"]
    assert provider.requests == []
    assert model_source.settled(loaded, source).model_mode == "local"


def test_auto_takes_the_hosted_model_when_no_ollama_answers_and_never_calls_the_provider_to_decide() -> None:
    with stub_server(lambda _: completion("{}")) as provider:
        loaded = settings.load_settings({"WORKBENCH_MODEL_MODE": "auto", **NO_OLLAMA, **hosted_env(provider.url)})
        source = model_source.resolve(loaded)
    assert (source.mode, source.model_id, source.provider_host) == (ModelMode.HOSTED, HOSTED_MODEL, "127.0.0.1")
    assert source.label == f"hosted model {HOSTED_MODEL} via 127.0.0.1, not part of the M4 evaluation"
    assert source.chosen_by.startswith("auto: no local model at http://127.0.0.1:9 (Ollama GET /api/tags failed:")
    assert source.chosen_by.endswith("; the hosted settings are complete")
    assert provider.requests == []
    assert HOSTED_KEY not in source.start_line()


def test_auto_takes_the_rules_when_neither_can_serve_and_says_why() -> None:
    source = model_source.resolve(settings.load_settings({"WORKBENCH_MODEL_MODE": "auto", **NO_OLLAMA,
                                                          "WORKBENCH_HOSTED_URL": CLOUDFLARE_URL}))
    assert (source.mode, source.model_id, source.provider_host) == (ModelMode.RULES, None, None)
    assert source.start_line().startswith("Model source: rules baseline (no model); auto: no local model at")
    assert source.chosen_by.endswith("hosted settings incomplete (WORKBENCH_HOSTED_MODEL, WORKBENCH_HOSTED_API_KEY "
                                     "not set)")


def test_auto_passes_over_an_ollama_that_lacks_the_model() -> None:
    with stub_server(ollama_listing("llama3:8b")) as ollama:
        source = model_source.resolve(settings.load_settings({"WORKBENCH_MODEL_MODE": "auto",
                                                              "WORKBENCH_OLLAMA_URL": ollama.url}))
    assert source.mode is ModelMode.RULES
    assert "is not in Ollama's local model list" in source.chosen_by


def test_auto_probes_within_its_short_timeout() -> None:
    hung = Answer(body=b'{"models": []}', delay_s=8.0)
    with stub_server(lambda _: hung) as ollama:
        started = time.monotonic()
        source = model_source.resolve(settings.load_settings({"WORKBENCH_MODEL_MODE": "auto",
                                                              "WORKBENCH_OLLAMA_URL": ollama.url}))
    assert time.monotonic() - started < model_source.AUTO_PROBE_TIMEOUT_S + 2.0
    assert source.mode is ModelMode.RULES


def test_a_named_mode_is_never_probed() -> None:
    def must_not_run() -> str:
        raise AssertionError("only auto probes the local model")

    for mode in ("local", "recorded", "rules"):
        source = model_source.resolve(settings.load_settings({"WORKBENCH_MODEL_MODE": mode}), local_probe=must_not_run)
        assert (source.mode.value, source.chosen_by) == (mode, f"set by WORKBENCH_MODEL_MODE={mode}")


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "ALL_PROXY"])
def test_auto_never_asks_the_environments_proxy_for_the_local_model(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Security audit run 3: the Ollama client followed HTTP_PROXY and ALL_PROXY, so a proxy that answered for the
    loopback-only model made auto choose local and credit 127.0.0.1. Nothing listens on the model port here."""
    def as_ollama(seen: Seen) -> Answer:  # a proxy is asked for the whole URL, so only the path's end is matched
        listed = {"models": [{"name": "qwen3:8b", "model": "qwen3:8b", "digest": OLLAMA_DIGEST}]}
        return json_answer(200, listed) if seen.path.endswith("/api/tags") else json_answer(404, {"error": seen.path})

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        for spelled in (name, name.lower()):
            monkeypatch.delenv(spelled, raising=False)
    with stub_server(as_ollama) as proxy:
        monkeypatch.setenv(variable, proxy.url)
        monkeypatch.setenv(variable.lower(), proxy.url)
        source = model_source.resolve(settings.load_settings({"WORKBENCH_MODEL_MODE": "auto", **NO_OLLAMA,
                                                              **hosted_env("https://provider.invalid")}))
    assert source.mode is ModelMode.HOSTED  # the local model did not answer, so auto passed on to hosted
    assert source.chosen_by.startswith("auto: no local model at http://127.0.0.1:9 (")
    assert proxy.requests == []


# --------------------------------------------------------------------------- the API


def make_app(tmp_path: Path, env: dict[str, str], *, probe: Callable[[], str] | None = None,
             factory: Callable[[], FakeModel] | None = None, ui_dir: Path | None = None) -> TestClient:
    app = api.create_app(
        api.ApiConfig(data_dir=tmp_path / "data", operator_token=TOKEN, allowed_hosts=frozenset({"testserver"}),
                      allowed_origins=frozenset({"http://testserver"}), ui_dir=ui_dir, model_probe=probe,
                      model_client_factory=factory),
        settings.load_settings({"WORKBENCH_RUN_TIMEOUT_SECONDS": "20", **env}),
    )
    return TestClient(app)


def log_in(client: TestClient) -> None:
    assert client.post("/api/session", json={"token": TOKEN, "operator_id": OPERATOR}, headers=STATE_CHANGE).status_code == 200


def create_run(client: TestClient, seed: int = SEED) -> str:
    response = client.post("/api/runs", json={"scenario": "worker_backlog", "seed": seed}, headers=STATE_CHANGE)
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def start(client: TestClient, run_id: str, **body: object) -> dict:
    response = client.post(f"/api/runs/{run_id}/diagnoses", json=body, headers=STATE_CHANGE)
    assert response.status_code == 202, response.text
    return response.json()


def finished(client: TestClient, job: dict) -> dict:
    deadline = time.monotonic() + 30
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, job
        time.sleep(0.05)
        job = client.get(f"/api/runs/{job['run_id']}/diagnoses/{job['job_id']}").json()
    return job


def run_packet(client: TestClient, run_id: str) -> EvidencePacket:
    with closing(storage.connect(client.app.state.workbench.database)) as conn:
        run = storage.require_run(conn, run_id)
        return packet.build_packet(conn, run_id=run_id, incident_id=storage.list_incidents(conn, run_id=run_id)[-1]
                                   .incident_id, cutoff_ms=run.clock_ms, deployed=runtime.active_merchant(
                                       conn, run_id=run_id, at_ms=run.clock_ms), persist=False)


def test_the_page_reads_the_rules_source_from_the_authenticated_scenarios_route(tmp_path: Path) -> None:
    with make_app(tmp_path, {"WORKBENCH_MODEL_MODE": "rules"}) as client:
        assert client.get("/api/scenarios").status_code == 403  # the source is behind the session
        log_in(client)
        listed = client.get("/api/scenarios").json()
        health = client.get("/health").json()
    rules = {"mode": "rules", "model_id": None, "provider_host": None, "label": "rules baseline (no model)"}
    assert listed["model_source"] == health["model_source"] == rules
    assert listed["model_modes"] == ["local", "recorded", "rules"]  # hosted is not offered without its settings
    assert listed["execution_mode"] == health["execution_mode"] == "rules"


def test_a_hosted_server_offers_hosted_and_its_health_never_calls_the_provider(tmp_path: Path) -> None:
    with stub_server(lambda _: completion("{}")) as provider, \
            make_app(tmp_path, {"WORKBENCH_MODEL_MODE": "hosted", **hosted_env(provider.url)}) as client:
        for _ in range(3):
            health = client.get("/health").json()
        log_in(client)
        listed = client.get("/api/scenarios").json()
    assert provider.requests == []  # /health needs no session: a call from it would spend the budget for anyone
    assert health["model"] == {"id": HOSTED_MODEL, "thinking": False, "checked": False, "available": None,
                               "digest": None}
    assert listed["model_modes"] == ["local", "recorded", "rules", "hosted"]
    assert listed["model_source"] == health["model_source"] == {
        "mode": "hosted", "model_id": HOSTED_MODEL, "provider_host": "127.0.0.1",
        "label": f"hosted model {HOSTED_MODEL} via 127.0.0.1, not part of the M4 evaluation"}


def test_an_auto_server_resolves_once_at_start_and_keeps_its_source(tmp_path: Path) -> None:
    calls: list[str] = []

    def probe() -> str:
        calls.append("probe")
        raise diagnosis.ModelCallError("connection refused")

    with stub_server(lambda _: completion("{}")) as provider, \
            make_app(tmp_path, {"WORKBENCH_MODEL_MODE": "auto", **hosted_env(provider.url)}, probe=probe) as client:
        log_in(client)
        first = client.get("/api/scenarios").json()["model_source"]
        client.get("/health")
        again = client.get("/api/scenarios").json()["model_source"]
        execution_mode = client.get("/health").json()["execution_mode"]
    assert calls == ["probe"]  # once, at start: /health does not probe a hosted source either
    assert first == again and first["mode"] == "hosted" and execution_mode == "hosted"


def test_a_hosted_request_on_a_server_without_hosted_settings_is_refused_clearly(tmp_path: Path) -> None:
    with make_app(tmp_path, {"WORKBENCH_MODEL_MODE": "rules"}) as client:
        log_in(client)
        run_id = create_run(client)
        response = client.post(f"/api/runs/{run_id}/diagnoses", json={"mode": "hosted"}, headers=STATE_CHANGE)
        listed = client.get(f"/api/runs/{run_id}/diagnoses").json()
    assert response.status_code == 422
    assert response.json() == {"error": "ContractError", "detail": (
        "hosted mode is not configured on this server: it needs WORKBENCH_HOSTED_URL, WORKBENCH_HOSTED_MODEL and "
        "WORKBENCH_HOSTED_API_KEY at start. Choose local, recorded or rules")}
    assert listed == []


def test_a_hosted_diagnosis_through_the_api_is_labelled_hosted_with_its_host(tmp_path: Path) -> None:
    holder: dict[str, str] = {}
    with stub_server(lambda _: completion(holder["content"])) as provider, \
            make_app(tmp_path, {"WORKBENCH_MODEL_MODE": "rules", **hosted_env(provider.url)}) as client:
        log_in(client)
        run_id = create_run(client)
        holder["content"] = json.dumps(selective.rules_v3(run_packet(client, run_id)).model_dump(mode="json"))
        job = finished(client, start(client, run_id, mode="hosted"))
        listed = client.get(f"/api/runs/{run_id}/diagnoses").json()
        run = client.get(f"/api/runs/{run_id}").json()
    found = job["diagnosis"]
    assert (job["status"], job["mode"], found["mode"], found["status"]) == ("done", "hosted", "hosted", "validated")
    assert (found["model_id"], found["provider_host"], found["model_digest"], found["thinking"]) == (
        HOSTED_MODEL, "127.0.0.1", None, False)
    assert found["mode_label"] == f"HOSTED: {HOSTED_MODEL} via 127.0.0.1, not part of the M4 evaluation"
    assert listed == [found]
    assert run["model_mode"] == "rules"  # the run keeps the server's mode; the request named its own
    assert len(provider.requests) == 1


def test_a_key_echoed_in_finish_reason_or_content_encoding_is_neither_stored_nor_answered(tmp_path: Path) -> None:
    """Security audit run 3: finish_reason and the Content-Encoding value reached a diagnosis's reasons without the key
    scrub other provider text gets, so the stored row and the API's diagnosis documents held the key."""
    finishing = json.loads(completion("").body)
    finishing["choices"][0]["finish_reason"] = MIXED_CASE_KEY
    script = [Answer(body=json.dumps(finishing).encode()), Answer(body=json.dumps(finishing).encode()),
              Answer(body=b"{}", headers={"Content-Type": "application/json", "Content-Encoding": MIXED_CASE_KEY})]

    def respond(_: Seen) -> Answer:
        return script.pop(0)

    with stub_server(respond) as provider:
        env = {"WORKBENCH_MODEL_MODE": "hosted", **hosted_env(provider.url, WORKBENCH_HOSTED_API_KEY=MIXED_CASE_KEY)}
        with make_app(tmp_path, env) as client:
            log_in(client)
            run_id = create_run(client, seed=21004)
            jobs = [finished(client, start(client, run_id)) for _ in range(2)]  # the first one's repair is call 2
            listed = client.get(f"/api/runs/{run_id}/diagnoses").text
            documents = [client.get(f"/api/runs/{run_id}/diagnoses/{job['job_id']}").text for job in jobs]
            database = client.app.state.workbench.database
    with closing(storage.connect(database)) as conn:
        rows = [tuple(row) for row in conn.execute("SELECT * FROM diagnoses WHERE run_id = ?", (run_id,))]
    assert [job["diagnosis"]["reasons"] for job in jobs] == [
        [f"call {index}: the reply has no JSON content (done_reason=None); the token budget may have run out"
         for index in (1, 2)],
        ["call 1: the hosted model sent a '<api key>'-encoded reply although none was asked for; it was not read"],
    ]
    assert len(provider.requests) == 3
    assert len(rows) == 2
    for text in (listed, *documents, repr(rows)):
        assert key_parts_in(text, MIXED_CASE_KEY) == []


def test_a_hosted_reply_nested_too_deep_leaves_the_job_done_and_the_diagnosis_needing_review(tmp_path: Path) -> None:
    """Security audit run 3: RecursionError escaped the JSON readers, so the job failed 'unexpectedly', with a traceback
    on the console and no diagnosis stored. It is now one more reply that needs review."""
    deep = Answer(body=b"[" * 50_000 + b"]" * 50_000)
    with stub_server(lambda _: deep) as provider, \
            make_app(tmp_path, {"WORKBENCH_MODEL_MODE": "hosted", **hosted_env(provider.url)}) as client:
        log_in(client)
        run_id = create_run(client, seed=21003)
        job = finished(client, start(client, run_id))
        listed = client.get(f"/api/runs/{run_id}/diagnoses").json()
    assert (job["status"], job["error"]) == ("done", None)
    assert (job["diagnosis"]["status"], job["diagnosis"]["reasons"]) == (
        "needs_review", ["call 1: the hosted model's reply nests JSON too deeply to read; it was not used"])
    assert listed == [job["diagnosis"]]


def test_on_a_hosted_server_runs_are_seeded_and_diagnosed_hosted(tmp_path: Path) -> None:
    with stub_server(lambda _: json_answer(503, {"error": "busy"})) as provider, \
            make_app(tmp_path, {"WORKBENCH_MODEL_MODE": "hosted", **hosted_env(provider.url)}) as client:
        log_in(client)
        run_id = create_run(client)
        job = finished(client, start(client, run_id))
        run = client.get(f"/api/runs/{run_id}").json()
    assert run["model_mode"] == "hosted"  # stored since migration 8
    assert (job["mode"], job["diagnosis"]["status"]) == ("hosted", "needs_review")
    assert job["diagnosis"]["reasons"] == [
        'call 1: the hosted model answered HTTP 503: {"error": "busy"}']  # needs review, never rules in its place


def test_the_rules_instead_after_a_model_that_was_unavailable_is_a_new_rules_diagnosis_of_the_same_incident(
    tmp_path: Path
) -> None:
    down = lambda: FakeModel(digest_error=diagnosis.ModelCallError("connection refused"))  # noqa: E731
    with make_app(tmp_path, {"WORKBENCH_MODEL_MODE": "local"}, factory=down) as client:
        log_in(client)
        run_id = create_run(client)
        failed = finished(client, start(client, run_id))["diagnosis"]
        rules = finished(client, start(client, run_id, mode="rules", incident_id=failed["incident_id"]))["diagnosis"]
        listed = client.get(f"/api/runs/{run_id}/diagnoses").json()
    assert (failed["mode"], failed["status"]) == ("local", "needs_review")
    assert failed["reasons"] == ["the model is unavailable: connection refused"]
    assert (rules["mode"], rules["status"], rules["incident_id"]) == ("rules", "validated", failed["incident_id"])
    assert [item["diagnosis_id"] for item in listed] == [failed["diagnosis_id"], rules["diagnosis_id"]]


def test_main_prints_the_source_it_resolved_once_and_never_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[object] = []
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **_: started.append(app))
    with stub_server(lambda _: completion("{}")) as provider:
        for name, value in {"WORKBENCH_MODEL_MODE": "auto", **NO_OLLAMA, **hosted_env(provider.url)}.items():
            monkeypatch.setenv(name, value)
        printed = io.StringIO()
        with redirect_stdout(printed):
            assert api.main(["--data-dir", str(tmp_path / "data"), "--port", "8765"]) == 0
    lines = printed.getvalue().splitlines()
    assert lines[0].endswith("; execution mode auto")
    assert lines[1].startswith(f"Model source: hosted model {HOSTED_MODEL} via 127.0.0.1, not part of the M4 "
                               "evaluation; auto: no local model at http://127.0.0.1:9 (")
    assert lines[2].startswith("Operator token for this server session (shown once): ")
    assert HOSTED_KEY not in printed.getvalue()
    assert provider.requests == [] and len(started) == 1
    assert started[0].state.workbench.settings.model_mode == "hosted"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- the CLI


def cli_run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def test_cli_commands_resolve_auto_once_at_start_and_print_the_source_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {"WORKBENCH_MODEL_MODE": "auto", **NO_OLLAMA}.items():
        monkeypatch.setenv(name, value)
    data = ("--data-dir", str(tmp_path / "data"))
    code, out, err = cli_run("seed", "--scenario", "worker_backlog", "--seed", str(SEED), *data)
    assert code == 0, err
    assert err.startswith("Model source: rules baseline (no model); auto: no local model at http://127.0.0.1:9")
    seeded = json.loads(out)  # stdout stays the command's own JSON
    with closing(storage.connect(tmp_path / "data" / cli.DATABASE_FILE)) as conn:
        assert storage.require_run(conn, seeded["run_id"]).model_mode is ModelMode.RULES
    code, out, err = cli_run("diagnose", "--run-id", seeded["run_id"], "--mode", "auto", *data)
    assert code == 0, err
    assert (json.loads(out)["mode"], err.count("Model source: rules baseline")) == ("rules", 1)


def test_cli_hosted_without_its_settings_is_refused_before_anything_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKBENCH_HOSTED_URL", CLOUDFLARE_URL)
    code, out, err = cli_run("demo", "--scenario", "worker_backlog", "--seed", str(SEED), "--model-mode", "hosted",
                             "--data-dir", str(tmp_path / "data"))
    assert (code, out) == (cli.EXIT_USAGE_ERROR, "")
    assert json.loads(err) == {"error": "ContractError", "detail": (
        "hosted mode needs WORKBENCH_HOSTED_MODEL, WORKBENCH_HOSTED_API_KEY set; set all three, or choose local, "
        "recorded, rules or auto")}
    assert not (tmp_path / "data").exists()  # refused before the data directory, let alone a seed


def test_cli_diagnose_in_hosted_mode_prints_a_hosted_document_and_never_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = ("--data-dir", str(tmp_path / "data"))
    code, out, err = cli_run("seed", "--scenario", "worker_backlog", "--seed", str(SEED), *data)
    assert code == 0, err
    run_id = json.loads(out)["run_id"]
    with closing(storage.connect(tmp_path / "data" / cli.DATABASE_FILE)) as conn:
        run = storage.require_run(conn, run_id)
        built = packet.build_packet(conn, run_id=run_id, incident_id=storage.list_incidents(conn, run_id=run_id)[-1]
                                    .incident_id, cutoff_ms=run.clock_ms,
                                    deployed=runtime.active_merchant(conn, run_id=run_id, at_ms=run.clock_ms),
                                    persist=False)
    content = json.dumps(selective.rules_v3(built).model_dump(mode="json"))
    with stub_server(lambda _: completion(content)) as provider:
        for name, value in hosted_env(provider.url).items():
            monkeypatch.setenv(name, value)
        code, out, err = cli_run("diagnose", "--run-id", run_id, "--mode", "hosted", *data)
    assert code == 0, err
    document = json.loads(out)
    assert (document["mode"], document["status"], document["provider_host"]) == ("hosted", "validated", "127.0.0.1")
    assert err.startswith(f"Model source: hosted model {HOSTED_MODEL} via 127.0.0.1")
    assert HOSTED_KEY not in out + err


# --------------------------------------------------------------------------- the key, scanned for everywhere


def test_the_api_key_appears_in_no_log_line_exception_stored_row_api_response_or_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider that quotes the key whole and in part, a validated answer, a rate limit and a used-up budget: the key
    is sent, and found in nothing this process logged, raised, stored, answered or printed."""
    caplog.set_level(logging.DEBUG)  # every logger at every level, httpx's and httpcore's request traces included
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<!doctype html><title>RayTrace</title>", encoding="utf-8")
    holder: dict[str, str] = {}
    script: list[Answer] = []

    def respond(_: Seen) -> Answer:
        return script.pop(0)

    texts: list[str] = []
    with stub_server(respond) as provider:
        env = {"WORKBENCH_MODEL_MODE": "hosted", "WORKBENCH_HOSTED_MAX_CALLS_PER_DAY": "4", **hosted_env(provider.url)}
        with make_app(tmp_path, env, ui_dir=ui_dir) as client:
            log_in(client)
            run_id = create_run(client)
            holder["content"] = json.dumps(selective.rules_v3(run_packet(client, run_id)).model_dump(mode="json"))
            script.extend([
                json_answer(401, {"error": {"message": f"Incorrect API key provided: {HOSTED_KEY}"}}),
                completion(holder["content"]),
                json_answer(429, {"error": f"rate limited for key {HOSTED_KEY[:16]}"}, **{"Retry-After": "30"}),
                completion("not json"),
                completion(holder["content"]),  # for the CLI below, whose budget is its own
            ])
            jobs = [finished(client, start(client, run_id)) for _ in range(5)]  # the fifth is over the budget
            refused = client.post(f"/api/runs/{run_id}/diagnoses", json={"mode": "cloud"}, headers=STATE_CHANGE)
            for path in ("/", "/health", "/api/session", "/api/scenarios", "/api/runs", f"/api/runs/{run_id}",
                         f"/api/runs/{run_id}/incidents", f"/api/runs/{run_id}/diagnoses", f"/api/runs/{run_id}/report",
                         *(f"/api/runs/{run_id}/diagnoses/{job['job_id']}" for job in jobs)):
                response = client.get(path)
                assert response.status_code == 200, path
                texts.append(response.text)
            texts.extend([refused.text, *(json.dumps(job) for job in jobs)])
            database = client.app.state.workbench.database
            texts.append(repr(client.app.state.workbench.settings))
        with closing(storage.connect(database)) as conn:
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                texts.extend(repr(tuple(row)) for row in conn.execute(f'SELECT * FROM "{table}"'))  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query -- names from sqlite_master
            texts.extend(str(sql) for (sql,) in conn.execute("SELECT sql FROM sqlite_master"))
        for name, value in {"WORKBENCH_DATA_DIR": str(tmp_path / "cli"), **env}.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(api.uvicorn, "run", lambda app, **_: None)
        printed = io.StringIO()
        with redirect_stdout(printed), redirect_stderr(printed):
            api.main(["--data-dir", str(tmp_path / "main"), "--port", "8766"])
            cli.main(["diagnose", "--run-id", run_id, "--mode", "hosted", "--data-dir", str(tmp_path / "data")])
        texts.append(printed.getvalue())
        with pytest.raises(ValueError) as malformed:
            settings.load_settings({"WORKBENCH_HOSTED_API_KEY": HOSTED_KEY + " trailing"})
        texts.append(str(malformed.value))
    texts.extend(record.getMessage() for record in caplog.records)
    texts.extend(str(record.exc_text) for record in caplog.records if record.exc_text)

    statuses = [(job["diagnosis"]["status"], job["diagnosis"]["reasons"][:1]) for job in jobs]
    assert [status for status, _ in statuses] == ["needs_review", "validated", "needs_review", "needs_review",
                                                  "needs_review"]
    assert "Incorrect API key provided: <api key>" in statuses[0][1][0]
    assert "daily call budget is used up" in statuses[4][1][0]
    assert all(seen.headers["authorization"] == f"Bearer {HOSTED_KEY}" for seen in provider.requests)  # it was sent
    assert len(provider.requests) == 5  # four from the API, until its budget ran out, and one from the CLI's own
    assert any("HTTP Request: POST" in record.getMessage() for record in caplog.records)  # the logs were captured
    leaked = [text[:160] for text in texts if HOSTED_KEY in text or HOSTED_KEY[:16] in text]
    assert leaked == []
