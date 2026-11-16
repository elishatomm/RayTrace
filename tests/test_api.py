"""The HTTP API (SPEC §11) through FastAPI's TestClient: rules mode or scripted fakes, never Ollama."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_diagnose_incident import FAKE_DIGEST, FakeModel, answer

from recovery_workbench import (
    api,
    cli,
    diagnosis,
    diagnosis_store,
    order_cases,
    packet,
    plans,
    selective,
    settings,
    storage,
)
from recovery_workbench import rules as rules_v2
from recovery_workbench.contracts import EvidencePacket
from recovery_workbench.simulator import runtime

TOKEN = "test-operator-token-not-a-secret"
OPERATOR = "op_api_tester"
STATE_CHANGE = {api.REQUEST_HEADER: "1"}
CROSS_SITE = {"origin": "http://evil.example", "sec-fetch-site": "cross-site"}


def make_client(
    tmp_path: Path, *, mode: str = "rules", factory: Callable[[], FakeModel] | None = None,
    probe: Callable[[], str] | None = None, ui_dir: Path | None = None, stale_after_seconds: int | None = None,
    plan_ttl_seconds: int | None = None, approval_ttl_seconds: int | None = None,
) -> TestClient:
    injected = {"WORKBENCH_MODEL_MODE": mode}
    for name, seconds in (("WORKBENCH_STALE_AFTER_SECONDS", stale_after_seconds),
                          ("WORKBENCH_PLAN_TTL_SECONDS", plan_ttl_seconds),
                          ("WORKBENCH_APPROVAL_TTL_SECONDS", approval_ttl_seconds)):
        if seconds is not None:
            injected[name] = str(seconds)
    app = api.create_app(
        api.ApiConfig(data_dir=tmp_path / "data", operator_token=TOKEN, allowed_hosts=frozenset({"testserver"}),
                      allowed_origins=frozenset({"http://testserver"}), ui_dir=ui_dir, model_client_factory=factory,
                      model_probe=probe),
        settings.load_settings(injected),
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        log_in(test_client)
        yield test_client


def log_in(test_client: TestClient, operator: str = OPERATOR) -> None:
    response = test_client.post("/api/session", json={"token": TOKEN, "operator_id": operator}, headers=STATE_CHANGE)
    assert response.status_code == 200, response.text


def post(test_client: TestClient, path: str, body: object | None = None, expect: int = 200) -> dict:
    response = test_client.post(path, json=body if body is not None else {}, headers=STATE_CHANGE)
    assert response.status_code == expect, response.text
    return response.json()


def get(test_client: TestClient, path: str, expect: int = 200) -> dict:
    response = test_client.get(path)
    assert response.status_code == expect, response.text
    return response.json()


def create_run(test_client: TestClient, scenario: str = "worker_backlog", seed: int = 101) -> str:
    return post(test_client, "/api/runs", {"scenario": scenario, "seed": seed}, expect=201)["run_id"]


def diagnose(test_client: TestClient, run_id: str, **body: object) -> dict:
    job = post(test_client, f"/api/runs/{run_id}/diagnoses", body, expect=202)
    deadline = time.monotonic() + 30
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, job
        time.sleep(0.05)
        job = get(test_client, f"/api/runs/{run_id}/diagnoses/{job['job_id']}")
    return job


# --------------------------------------------------------------------------- Flow A


def test_flow_a_through_the_api(client: TestClient) -> None:
    run_id = create_run(client)
    run = get(client, f"/api/runs/{run_id}")
    assert run["seed"] == 101
    assert run["incident_ids"]
    assert "oracle" not in str(run)
    observed = get(client, f"/api/runs/{run_id}/incidents")
    assert observed["incidents"]
    assert observed["merchant"]["completion"]["denominator"] > 0
    assert {"pending_captured_value", "plan_value", "provider_authorisation"} <= set(observed["definitions"])

    job = diagnose(client, run_id)
    assert job["status"] == "done", job
    found = job["diagnosis"]
    assert found["mode"] == "rules"
    assert found["status"] == "validated"
    assert found["mode_label"].startswith("rules baseline")
    assert found["output"]["proposed_operation"] == "reprocess_inbox"
    visible = {item["evidence_id"] for item in job["packet"]["items"]}
    assert set(found["output"]["supporting_evidence_ids"]) <= visible
    assert job["evidence_cutoff_ms"] == run["replay_clock_ms"]

    assert "payload@5" in {item["fix_id"] for item in get(client, "/api/fixes")}
    validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate", {"diagnosis_id": found["diagnosis_id"]})
    assert validation["reproduced"]
    assert validation["passed"]
    assert validation["operator_id"] == OPERATOR

    draft = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
    assert draft["stale_only_orders"]
    assert draft["target_count"] == len(draft["targets"]) > 0
    stale_payments = [payment for item in draft["stale_only_orders"] for payment in item["payment_ids"]]
    refreshed = post(client, f"/api/runs/{run_id}/refresh", {"payment_ids": stale_payments})
    assert sorted(refreshed["refreshed"]) == sorted(stale_payments)
    plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
    stale_orders = {item["order_id"] for item in draft["stale_only_orders"]}
    assert {target["id"] for target in plan["targets"]} == {target["id"] for target in draft["targets"]} | stale_orders

    approval = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
    assert approval["operator_id"] == OPERATOR
    assert approval["plan_hash"] == plan["plan_hash"]
    first = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
    assert first["status"] == "succeeded"
    assert len(first["fulfilled_order_ids"]) == plan["target_count"]
    assert first["restored_processing_value_paise"] == plan["target_value_paise"]
    assert first["additional_captured_paise"] == 0

    exported = get(client, f"/api/runs/{run_id}/report")
    assert exported["outcome"]["orders_fulfilled_by_reprocessing"] == plan["target_count"]
    assert exported["outcome"]["additional_captured_paise"] == 0
    assert exported["metadata"]["seed"] == 101


def test_a_second_execute_is_an_idempotent_replay(client: TestClient) -> None:
    run_id = create_run(client)
    found = diagnose(client, run_id)["diagnosis"]
    validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate", {"diagnosis_id": found["diagnosis_id"]})
    plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
    post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
    first = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
    again = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
    assert again["replayed"]
    assert again["action_id"] == first["action_id"]
    assert not first["replayed"]
    assert again["fulfilled_order_ids"] == first["fulfilled_order_ids"]
    with closing(storage.connect(client.app.state.workbench.database)) as conn:
        reprocessed = [item for item in storage.list_fulfilments(conn, run_id=run_id) if item.via == "reprocess"]
    assert len(reprocessed) == plan["target_count"]  # no order fulfilled twice


# --------------------------------------------------------------------------- error mapping


def test_stale_and_conflicting_requests_are_409(client: TestClient) -> None:
    run_id = create_run(client)
    assert post(client, "/api/runs", {"scenario": "worker_backlog", "seed": 101}, expect=409)["run_id"] == run_id
    found = diagnose(client, run_id)["diagnosis"]
    validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate", {"diagnosis_id": found["diagnosis_id"]})
    plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
    refused = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": "0" * 64}, expect=409)
    assert refused["error"] == "StaleStateError"
    # Time moves past the plan's expiry: approving the reviewed hash is now stale too.
    post(client, f"/api/runs/{run_id}/advance", {"by_ms": settings.load_settings().plan_ttl_seconds * 1000 + 60_000})
    assert post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]},
                expect=409)["error"] == "StaleStateError"


def test_malformed_contracts_are_422(client: TestClient) -> None:
    run_id = create_run(client)
    assert post(client, "/api/runs", {"scenario": "no_such_scenario", "seed": 1}, expect=422)["error"] == "ValueError"
    post(client, "/api/runs", {"scenario": "worker_backlog", "seed": 1, "extra": True}, expect=422)
    post(client, "/api/runs", {"scenario": "worker_backlog", "seed": -1}, expect=422)
    post(client, f"/api/runs/{run_id}/plans", {"operation": "reprocess_inbox"}, expect=422)
    post(client, f"/api/runs/{run_id}/advance", {"by_ms": 0}, expect=422)
    post(client, f"/api/runs/{run_id}/plans/plan_x/approve", {"plan_hash": "not-a-hash"}, expect=422)
    found = diagnose(client, run_id)["diagnosis"]
    unknown_fix = post(client, f"/api/runs/{run_id}/fixes/payload@99/validate", {"diagnosis_id": found["diagnosis_id"]},
                       expect=422)
    assert "payload@99" in unknown_fix["detail"]


def test_scope_failures_are_403(client: TestClient) -> None:
    assert get(client, "/api/runs/run_does_not_exist/incidents", expect=403)["error"] == "ScopeError"
    run_id = create_run(client)
    other = create_run(client, "late_authorization", 101)
    with closing(storage.connect(client.app.state.workbench.database)) as conn:
        foreign_payment = storage.list_run_payment_attempts(conn, run_id=other, as_of_ms=None)[0].payment_id
    post(client, f"/api/runs/{run_id}/refresh", {"payment_ids": [foreign_payment]}, expect=403)
    post(client, f"/api/runs/{run_id}/plans/plan_of_nobody/execute", expect=403)
    get(client, f"/api/runs/{run_id}/diagnoses/job_of_nobody", expect=403)


def test_error_details_name_no_server_paths(client: TestClient, tmp_path: Path) -> None:
    run_id = create_run(client)
    for records in (tmp_path / "data" / "provider").glob("*.json"):
        records.unlink()  # the run's provider records disappear from the server
    with closing(storage.connect(client.app.state.workbench.database)) as conn:
        payment = storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=None)[0].payment_id
    refused = post(client, f"/api/runs/{run_id}/refresh", {"payment_ids": [payment]}, expect=422)
    assert refused["error"] == "MissingServerData"
    assert str(tmp_path) not in refused["detail"]


# --------------------------------------------------------------------------- session and cross-origin protection


def test_every_api_route_needs_an_operator_session(tmp_path: Path) -> None:
    with make_client(tmp_path) as anonymous:
        assert get(anonymous, "/api/runs", expect=403)["error"] == "ScopeError"
        post(anonymous, "/api/runs", {"scenario": "worker_backlog", "seed": 101}, expect=403)
        refused = anonymous.post("/api/session", json={"token": "wrong", "operator_id": OPERATOR}, headers=STATE_CHANGE)
        assert refused.status_code == 403
        assert "set-cookie" not in refused.headers
        anonymous.cookies.set(api.SESSION_COOKIE, "forged-session-id")
        get(anonymous, "/api/runs", expect=403)
        assert get(anonymous, "/api/session")["operator_id"] is None
        assert get(anonymous, "/health")["environment"] == "synthetic"  # health is open; it holds no run data


def test_the_session_cookie_is_http_only_and_strict_and_the_token_is_never_echoed(tmp_path: Path) -> None:
    with make_client(tmp_path) as test_client:
        response = test_client.post("/api/session", json={"token": TOKEN, "operator_id": OPERATOR}, headers=STATE_CHANGE)
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert TOKEN not in response.text
        assert TOKEN not in test_client.get("/health").text
        assert TOKEN not in test_client.get("/api/session").text
        logout = test_client.delete("/api/session", headers=STATE_CHANGE)
        assert logout.status_code == 200
        get(test_client, "/api/runs", expect=403)


def test_a_model_cannot_become_an_operator(tmp_path: Path) -> None:
    with make_client(tmp_path) as test_client:
        response = test_client.post("/api/session", json={"token": TOKEN, "operator_id": "qwen3:8b"}, headers=STATE_CHANGE)
        assert response.status_code == 422
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize("headers", [
    {},  # no X-Workbench-Request: what a cross-site HTML form would send
    {**STATE_CHANGE, "origin": "http://evil.example"},
    {**STATE_CHANGE, "origin": "null"},
    {**STATE_CHANGE, "sec-fetch-site": "cross-site"},
])
def test_cross_origin_state_changes_are_refused_even_with_a_session(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/api/runs", json={"scenario": "worker_backlog", "seed": 7}, headers=headers)
    assert response.status_code == 403, response.text
    assert get(client, "/api/runs") == []  # nothing was created


def test_a_foreign_host_header_is_refused_for_reads_too(client: TestClient) -> None:
    response = client.get("/api/runs", headers={"host": "attacker.example"})
    assert response.status_code == 403
    assert client.get("/health", headers={"host": "attacker.example"}).status_code == 403


@pytest.mark.parametrize("method", ["PROPFIND", "TRACE", "FROBNICATE"])
def test_every_method_but_get_head_and_options_faces_the_origin_and_ui_header_checks(
    client: TestClient, method: str
) -> None:
    """The guard names the methods it lets through, so a route added later with any other method fails closed."""
    without_header = client.request(method, "/api/runs")
    assert without_header.status_code == 403
    assert api.REQUEST_HEADER in without_header.json()["detail"]
    for hostile in ({"origin": CROSS_SITE["origin"]}, {"sec-fetch-site": CROSS_SITE["sec-fetch-site"]}):
        assert client.request(method, "/api/runs", headers={**STATE_CHANGE, **hostile}).status_code == 403
    assert client.request(method, "/api/runs", headers=STATE_CHANGE).status_code == 405  # past the guard: no such route


@pytest.mark.parametrize(("method", "status"), [("GET", 200), ("HEAD", 405), ("OPTIONS", 405)])
def test_get_head_and_options_pass_the_guard_on_the_host_check_alone(
    client: TestClient, method: str, status: int
) -> None:
    """As before: the router answers them whatever Origin and Sec-Fetch-Site say, and without the UI header."""
    assert client.request(method, "/api/runs", headers=CROSS_SITE).status_code == status
    assert client.request(method, "/api/runs", headers={"host": "attacker.example"}).status_code == 403


@pytest.mark.parametrize("dots", [".", "..", "..."])
def test_a_path_parameter_made_only_of_dots_is_refused_on_every_route(client: TestClient, dots: str) -> None:
    """A browser reads a path segment of . or .. as "this directory" or "its parent", so the UI could not open a record
    with such an ID. Each path parameter refuses any ID made only of dots before its route runs. The dots go
    percent-encoded, which the server decodes, because the test client would resolve them as a browser does."""
    encoded = "%2E" * len(dots)
    refused: dict[str, bool] = {}
    for route in client.app.routes:
        if not isinstance(route, APIRoute):
            continue
        for name in route.param_convertors:
            path = route.path.format(**{**dict.fromkeys(route.param_convertors, "x"), name: encoded})
            for method in route.methods:
                response = client.request(method, path, headers=STATE_CHANGE, json=None if method == "GET" else {})
                errors = response.json()["detail"] if response.status_code == 422 else []
                located = isinstance(errors, list) and ["path", name] in [error["loc"] for error in errors]
                refused[f"{method} {route.path} {name}"] = located
    assert refused
    assert refused == dict.fromkeys(refused, True)


def test_failed_logins_are_rate_limited(tmp_path: Path) -> None:
    with make_client(tmp_path) as test_client:
        codes = [test_client.post("/api/session", json={"token": f"guess-{index}", "operator_id": OPERATOR},
                                  headers=STATE_CHANGE).status_code for index in range(api.MAX_FAILED_LOGINS_PER_MINUTE + 1)]
        assert codes[:-1] == [403] * api.MAX_FAILED_LOGINS_PER_MINUTE
        assert codes[-1] == 429
        assert test_client.post("/api/session", json={"token": TOKEN, "operator_id": OPERATOR},
                                headers=STATE_CHANGE).status_code == 200  # the window holds back wrong tokens only


# --------------------------------------------------------------------------- diagnosis jobs, modes and health


def test_a_model_failure_is_a_visible_needs_review_with_the_evidence_kept(tmp_path: Path) -> None:
    with make_client(tmp_path, mode="local", factory=lambda: FakeModel("not json", "{still not")) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        job = diagnose(test_client, run_id)
        assert job["status"] == "done"
        assert job["diagnosis"]["status"] == "needs_review"
        assert job["diagnosis"]["output"] is None
        assert job["diagnosis"]["reasons"]
        assert job["packet"]["items"]
        assert job["diagnosis"]["mode_label"] == "live local model fake-model:1b"


def test_a_recorded_replay_is_labelled_as_recorded(tmp_path: Path) -> None:
    holder: dict[str, FakeModel] = {}
    with make_client(tmp_path, mode="local", factory=lambda: holder["model"]) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        database = test_client.app.state.workbench.database
        with closing(storage.connect(database)) as conn:
            run = storage.require_run(conn, run_id)
            built = packet.build_packet(
                conn, run_id=run_id, incident_id=storage.list_incidents(conn, run_id=run_id)[-1].incident_id,
                cutoff_ms=run.clock_ms, deployed=runtime.active_merchant(conn, run_id=run_id, at_ms=run.clock_ms),
                persist=False,
            )
        holder["model"] = FakeModel(answer(built))
        live = diagnose(test_client, run_id)["diagnosis"]
        assert live["status"] == "validated"
        assert live["model_digest"] == FAKE_DIGEST
        replay = diagnose(test_client, run_id, mode="recorded")["diagnosis"]
        assert replay["mode"] == "recorded"
        assert replay["mode_label"].startswith("RECORDED replay")
        assert replay["output"] == live["output"]


def test_rules_mode_answers_rules_v3_and_lists_an_earlier_rules_v2_diagnosis_with_its_version(client: TestClient) -> None:
    """Q28: the API's rules mode answers rules-v3; a diagnosis stored before the switch is still shown as rules-v2."""
    run_id = create_run(client)
    with closing(storage.connect(client.app.state.workbench.database)) as conn:
        run = storage.require_run(conn, run_id)
        incident_id = storage.list_incidents(conn, run_id=run_id)[-1].incident_id
        built = packet.build_packet(conn, run_id=run_id, incident_id=incident_id, cutoff_ms=run.clock_ms,
                                    deployed=runtime.active_merchant(conn, run_id=run_id, at_ms=run.clock_ms))
        earlier = diagnosis_store.record_rules_diagnosis(  # as rules mode stored it before Q28: rules-v2's answer
            conn, run_id=run_id, incident_id=incident_id, packet_hash=built.packet_hash(),
            output=rules_v2.diagnose(built), rules_version=rules_v2.RULES_VERSION,
        )
    job = diagnose(client, run_id)
    assert job["status"] == "done", job
    found = job["diagnosis"]
    assert (found["rules_version"], found["mode_label"]) == ("rules-v3", "rules baseline rules-v3 (no model)")
    assert found["output"] == selective.rules_v3(EvidencePacket.model_validate(job["packet"])).model_dump(mode="json")
    listed = {item["diagnosis_id"]: (item["rules_version"], item["mode_label"])
              for item in get(client, f"/api/runs/{run_id}/diagnoses")}
    assert listed == {earlier.diagnosis_id: ("rules-v2", "rules baseline rules-v2 (no model)"),
                      found["diagnosis_id"]: ("rules-v3", "rules baseline rules-v3 (no model)")}


def test_one_inference_at_a_time(tmp_path: Path) -> None:
    with make_client(tmp_path, mode="local", factory=lambda: FakeModel("not json", "nor this", delay_s=0.4)) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        first = post(test_client, f"/api/runs/{run_id}/diagnoses", {}, expect=202)
        busy = post(test_client, f"/api/runs/{run_id}/diagnoses", {}, expect=409)
        assert busy["job_id"] == first["job_id"]


class HeldModel(FakeModel):
    """A scripted model that answers only once the test lets it, so its job stays live meanwhile."""

    def __init__(self, release: threading.Event, *answers: object) -> None:
        super().__init__(*answers)
        self._release = release

    def chat(self, request: diagnosis.ChatRequest, *, timeout_s: float) -> diagnosis.ChatReply:
        assert self._release.wait(timeout=20), "the test never released the model"
        return super().chat(request, timeout_s=timeout_s)


def test_the_server_keeps_every_live_job_and_only_its_newest_finished_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security audit run 3: every finished job stayed in memory, its packet with it, for the server's whole life. Now
    the oldest finished job goes first once more than MAX_FINISHED_JOBS have finished, and its ID answers 404; an ID the
    server never issued still answers 403, as before."""
    monkeypatch.setattr(api, "MAX_FINISHED_JOBS", 2, raising=False)  # not raising: the code before had no bound to set
    release = threading.Event()
    with make_client(tmp_path, factory=lambda: HeldModel(release, "not json", "nor this")) as test_client:
        log_in(test_client)
        run_id = create_run(test_client, seed=21002)
        oldest, middle = (diagnose(test_client, run_id) for _ in range(2))
        live = post(test_client, f"/api/runs/{run_id}/diagnoses", {"mode": "local"}, expect=202)
        for job in (oldest, middle, live):  # two finished and one live: the live one counts against no limit
            assert get(test_client, f"/api/runs/{run_id}/diagnoses/{job['job_id']}")["job_id"] == job["job_id"]
        release.set()
        assert diagnose_finished(test_client, live)["status"] == "done"  # three finished now, one too many
        dropped = test_client.get(f"/api/runs/{run_id}/diagnoses/{oldest['job_id']}")
        kept = [get(test_client, f"/api/runs/{run_id}/diagnoses/{job['job_id']}")["status"] for job in (middle, live)]
        forged = oldest["job_id"][:-1] + ("0" if oldest["job_id"][-1] != "0" else "1")  # its tag no longer matches
        never_issued = [test_client.get(f"/api/runs/{run_id}/diagnoses/{job_id}").status_code
                        for job_id in ("job_of_nobody", forged)]
        listed = {item["diagnosis_id"] for item in get(test_client, f"/api/runs/{run_id}/diagnoses")}
    assert dropped.status_code == 404
    assert dropped.json() == {"error": "JobNotKeptError", "detail": (
        f"diagnosis job {oldest['job_id']!r} has finished and is no longer kept: this server keeps its newest 2 "
        "finished jobs; the run's diagnoses list holds its result")}
    assert kept == ["done", "done"]
    assert never_issued == [403, 403]
    assert oldest["diagnosis"]["diagnosis_id"] in listed  # its diagnosis is stored; only the job document went


def diagnose_finished(test_client: TestClient, job: dict) -> dict:
    """Poll a started job until it is no longer queued or running."""
    deadline = time.monotonic() + 30
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, job
        time.sleep(0.05)
        job = get(test_client, f"/api/runs/{job['run_id']}/diagnoses/{job['job_id']}")
    return job


def test_escalation_takes_the_operator_from_the_session(client: TestClient) -> None:
    run_id = create_run(client, "method_degradation", 101)
    found = diagnose(client, run_id)["diagnosis"]
    assert found["output"]["hypothesis"] == "provider_method_degradation"
    assert found["output"]["affected_method"] != "none"
    record = post(client, f"/api/runs/{run_id}/plans", {"operation": "escalate", "diagnosis_id": found["diagnosis_id"],
                                                        "operator_id": "op_someone_else"}, expect=422)
    assert "operator_id" in str(record)  # the body cannot name an operator
    record = post(client, f"/api/runs/{run_id}/plans", {"operation": "escalate", "diagnosis_id": found["diagnosis_id"]})
    assert record["plan"]["operation"] == "escalate"
    assert record["operator_id"] == OPERATOR
    assert record["evidence_needed"]


HEALTH_MODEL_FIELDS = {"id", "thinking", "checked", "available", "digest"}  # in every mode, probed or not


def test_health_reports_mode_model_and_versions(tmp_path: Path) -> None:
    with make_client(tmp_path, mode="local", probe=lambda: "sha256:" + "a" * 64) as local:
        model = get(local, "/health")["model"]
        assert model["checked"]
        assert model["available"]
        assert model["digest"].startswith("sha256:")
        assert set(model) == HEALTH_MODEL_FIELDS

    def unreachable() -> str:
        raise diagnosis.ModelCallError("connection refused")

    with make_client(tmp_path / "second", mode="local", probe=unreachable) as down:
        report = get(down, "/health")
        assert report["model"]["available"] is False
        assert report["execution_mode"] == "local"
        assert report["versions"]["validator"] == "val-7"
        assert report["versions"]["prompt"] == "diag-p5"
    with make_client(tmp_path / "third", mode="rules") as rules:
        model = get(rules, "/health")["model"]
        assert model["checked"] is False
        assert set(model) == HEALTH_MODEL_FIELDS


def test_health_logs_the_model_probes_error_instead_of_answering_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """/health answers without a session, and a failed probe's message can quote the model endpoint's own reply.

    The message goes to the server console instead: escaped, so the reply cannot forge a log line, and as a warning,
    because uvicorn configures only its own loggers and Python's last-resort handler prints this module's from WARNING.
    """
    reply = "Ollama GET /api/tags returned HTTP 502: upstream says\nINFO:     forged line\x1b[2J"

    def failing() -> str:
        raise diagnosis.ModelCallError(reply)

    with caplog.at_level(logging.WARNING, logger=api.__name__), \
            make_client(tmp_path, mode="local", probe=failing) as test_client:
        response = test_client.get("/health")
    model = response.json()["model"]
    assert (model["checked"], model["available"], model["digest"]) == (True, False, None)
    assert set(model) == HEALTH_MODEL_FIELDS
    assert "upstream says" not in response.text
    [record] = [record for record in caplog.records if record.name == api.__name__]
    assert record.levelno == logging.WARNING
    assert "upstream says" in record.getMessage()
    assert "\n" not in record.getMessage()
    assert "\x1b" not in record.getMessage()


def test_the_built_ui_is_served_with_a_strict_content_security_policy(tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>workbench</title>", encoding="utf-8")
    with make_client(tmp_path, ui_dir=ui) as test_client:
        page = test_client.get("/")
        assert page.status_code == 200
        assert "workbench" in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY"


def test_flow_c_through_the_api_refreshes_states_facts_and_escalates(client: TestClient) -> None:
    run_id = create_run(client, "late_authorization", 101)
    found = diagnose(client, run_id)["diagnosis"]
    assert found["output"]["hypothesis"] == "ambiguous_payment_state"
    assert found["output"]["proposed_operation"] == "refresh_provider_state"
    state = get(client, f"/api/runs/{run_id}/payment-state")
    facts = state["late_authorisations"]
    assert facts["payments"] > 0
    assert facts["fulfilled_orders"] + len(facts["unfulfilled"]) == facts["orders"]
    assert "held_back" not in facts  # policy-v3: a late authorisation alone holds nothing back
    with closing(storage.connect(client.app.state.workbench.database)) as conn:
        fulfilled = {item.order_id for item in storage.list_fulfilments(conn, run_id=run_id)}
    assert not {order["order_id"] for order in facts["unfulfilled"]} & fulfilled
    assert all("late_authorization" not in order["exclusions"] for order in facts["unfulfilled"])
    refreshed = post(client, f"/api/runs/{run_id}/refresh", {"payment_ids": state["refresh_candidates"]})
    assert refreshed["refreshed"]
    assert not refreshed["unknown_at_provider"]
    record = post(client, f"/api/runs/{run_id}/plans", {"operation": "escalate", "diagnosis_id": found["diagnosis_id"]})
    assert record["operation"] == "escalate"
    assert record["evidence_needed"]
    with closing(storage.connect(client.app.state.workbench.database)) as conn:
        assert not [item for item in storage.list_fulfilments(conn, run_id=run_id) if item.via == "reprocess"]


# --------------------------------------------------------------------------- one freshness threshold (Q31)

# Each test injects a threshold into the server's settings object and sets WORKBENCH_STALE_AFTER_SECONDS to another.
# On the README demo world an hour holds five orders whose only exclusion is stale provider state; ten days holds none.
HOUR_S = 3_600
TEN_DAYS_S = 864_000
SERVER_AND_ENVIRONMENT = [pytest.param(HOUR_S, TEN_DAYS_S, id="server-hour-env-ten-days"),
                          pytest.param(TEN_DAYS_S, HOUR_S, id="server-ten-days-env-hour")]


def queue_at_clock(test_client: TestClient, run_id: str) -> tuple[dict, dict[str, dict]]:
    """The order queue at the run's replay clock, and its rows by order ID."""
    document = get(test_client, f"/api/runs/{run_id}/orders?limit={api.MAX_QUEUE_PAGE_LIMIT}")
    assert document["next_after"] is None
    return document, {row["order_id"]: row for row in document["orders"]}


def stale_orders(reasons_by_order: dict[str, tuple[str, ...]]) -> set[str]:
    """The orders whose blockers or exclusions include stale provider state."""
    return {order_id for order_id, reasons in reasons_by_order.items() if "stale_snapshot" in reasons}


@pytest.mark.parametrize(("server_seconds", "environment_seconds"), SERVER_AND_ENVIRONMENT)
def test_the_queue_and_the_reprocess_planner_judge_staleness_by_the_servers_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_seconds: int, environment_seconds: int
) -> None:
    """An order the queue holds as stale is not a plan target, and every order it finds fresh and clean is one."""
    monkeypatch.setenv("WORKBENCH_STALE_AFTER_SECONDS", str(environment_seconds))
    with make_client(tmp_path, stale_after_seconds=server_seconds) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        found = diagnose(test_client, run_id)["diagnosis"]
        validation = post(test_client, f"/api/runs/{run_id}/fixes/payload@5/validate",
                          {"diagnosis_id": found["diagnosis_id"]})
        plan = post(test_client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        queue, rows = queue_at_clock(test_client, run_id)  # planning took the last operator step: the plan's time
        database = test_client.app.state.workbench.database
    assert queue["stale_after_ms"] == server_seconds * 1000
    targets = {target["id"] for target in plan["targets"]}
    held_stale = stale_orders({order_id: tuple(row["blockers"]) for order_id, row in rows.items()})
    plannable = {order_id for order_id, row in rows.items()
                 if row["state"] == "captured_processing_incomplete" and not row["blockers"]}
    assert targets
    assert not held_stale & targets
    assert plannable == targets
    # The environment's threshold judges some of the plan's candidates the other way, so the agreement is not chance.
    with closing(storage.connect(database)) as conn:
        by_environment = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=queue["as_of_ms"],
                                                     stale_after_ms=environment_seconds * 1000)
    candidates = targets | {item["order_id"] for item in plan["excluded"]}
    assert (held_stale ^ stale_orders({case.order_id: case.blockers for case in by_environment.cases})) & candidates


def test_the_executors_recheck_uses_the_servers_threshold_after_the_environment_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planned and approved while the environment agrees with the server, executed once it says one second."""
    monkeypatch.setenv("WORKBENCH_STALE_AFTER_SECONDS", str(HOUR_S))
    with make_client(tmp_path, stale_after_seconds=HOUR_S) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        found = diagnose(test_client, run_id)["diagnosis"]
        validation = post(test_client, f"/api/runs/{run_id}/fixes/payload@5/validate",
                          {"diagnosis_id": found["diagnosis_id"]})
        plan = post(test_client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        post(test_client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
        monkeypatch.setenv("WORKBENCH_STALE_AFTER_SECONDS", "1")  # every target's provider state is older than that
        executed = post(test_client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
        receipt = get(test_client, f"/api/runs/{run_id}/actions/{executed['action_id']}/receipt")
    assert executed["status"] == "succeeded"
    assert sorted(executed["fulfilled_order_ids"]) == sorted(target["id"] for target in plan["targets"])
    assert receipt["evidence"]["stale_after_ms"] == HOUR_S * 1000  # the threshold the recheck was given


@pytest.mark.parametrize(("server_seconds", "environment_seconds"), [
    pytest.param(HOUR_S, 1, id="server-hour-env-one-second"), pytest.param(1, HOUR_S, id="server-one-second-env-hour"),
])
def test_the_late_authorisation_facts_judge_staleness_by_the_servers_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_seconds: int, environment_seconds: int
) -> None:
    """Each unfulfilled late-authorised order is stale in the payment-state facts exactly when the queue holds it so.

    On the README demo world an hour leaves none of them stale, and one second makes three of the four stale.
    """
    monkeypatch.setenv("WORKBENCH_STALE_AFTER_SECONDS", str(environment_seconds))
    with make_client(tmp_path, stale_after_seconds=server_seconds) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        unfulfilled = get(test_client, f"/api/runs/{run_id}/payment-state")["late_authorisations"]["unfulfilled"]
        queue, rows = queue_at_clock(test_client, run_id)
        database = test_client.app.state.workbench.database
    exclusions = {order["order_id"]: tuple(order["exclusions"]) for order in unfulfilled}
    assert exclusions
    assert all(set(found) <= set(rows[order_id]["blockers"]) for order_id, found in exclusions.items())
    queue_blockers = {order_id: tuple(rows[order_id]["blockers"]) for order_id in exclusions}
    assert stale_orders(exclusions) == stale_orders(queue_blockers)
    # The environment's threshold judges some of these orders the other way, so the agreement is not chance.
    with closing(storage.connect(database)) as conn:
        by_environment = plans.late_authorised_orders(conn, run_id=run_id, now_ms=queue["as_of_ms"],
                                                      stale_after_ms=environment_seconds * 1000)
    assert stale_orders({order.order_id: order.exclusions for order in by_environment if not order.fulfilled}) != (
        stale_orders(exclusions))


def test_a_run_seeded_through_the_api_gets_an_oracle_judged_by_the_servers_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oracle labels eligibility with the planner's threshold (settings.Settings), so the API's is the server's."""
    monkeypatch.setenv("WORKBENCH_STALE_AFTER_SECONDS", str(TEN_DAYS_S))
    with make_client(tmp_path, stale_after_seconds=HOUR_S) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        oracle_dir = test_client.app.state.workbench.config.data_dir / cli.ORACLE_DIRECTORY
    oracle = json.loads((oracle_dir / f"{run_id}.json").read_text(encoding="utf-8"))
    assert oracle["eligibility"]["stale_after_ms"] == HOUR_S * 1000


# --------------------------------------------------------------------------- one source for expiry times (Q35)


def test_a_reprocess_plan_and_its_approval_expire_by_the_servers_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server holds 20 and 10 minutes; the environment says 40 and 20."""
    monkeypatch.setenv("WORKBENCH_PLAN_TTL_SECONDS", "2400")
    monkeypatch.setenv("WORKBENCH_APPROVAL_TTL_SECONDS", "1200")
    with make_client(tmp_path, plan_ttl_seconds=1200, approval_ttl_seconds=600) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        found = diagnose(test_client, run_id)["diagnosis"]
        validation = post(test_client, f"/api/runs/{run_id}/fixes/payload@5/validate",
                          {"diagnosis_id": found["diagnosis_id"]})
        plan = post(test_client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        planned_at = get(test_client, f"/api/runs/{run_id}")["replay_clock_ms"]  # the plan's own operator step
        approval = post(test_client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve",
                        {"plan_hash": plan["plan_hash"]})
        approved_at = get(test_client, f"/api/runs/{run_id}")["replay_clock_ms"]
    assert plan["expires_at_ms"] - planned_at == 1200 * 1000
    assert approval["expires_at_ms"] - approved_at == 600 * 1000


def test_after_start_no_api_route_reads_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """settings.load_settings refuses every recovery timing here, so any route that read the environment would fail.

    Before owner decision Q35 the plan and approve routes still read their expiry times from it on every request.
    """
    for name in ("WORKBENCH_STALE_AFTER_SECONDS", "WORKBENCH_PLAN_TTL_SECONDS", "WORKBENCH_APPROVAL_TTL_SECONDS"):
        monkeypatch.setenv(name, "0")
    with make_client(tmp_path) as test_client:
        log_in(test_client)
        run_id = create_run(test_client)
        get(test_client, f"/api/runs/{run_id}/payment-state")
        found = diagnose(test_client, run_id)["diagnosis"]
        validation = post(test_client, f"/api/runs/{run_id}/fixes/payload@5/validate",
                          {"diagnosis_id": found["diagnosis_id"]})
        plan = post(test_client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        post(test_client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
        executed = post(test_client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
        post(test_client, f"/api/runs/{run_id}/plans", {"operation": "prepare_provider_replay",
                                                         "diagnosis_id": found["diagnosis_id"]})
        get(test_client, f"/api/runs/{run_id}/orders")
        get(test_client, f"/api/runs/{run_id}/actions/{executed['action_id']}/receipt")
        get(test_client, f"/api/runs/{run_id}/report")
    assert executed["status"] == "succeeded"


# --------------------------------------------------------------------------- every GET route only reads


def stored_state(workbench: api.Workbench) -> dict[str, tuple[int, str]]:
    """A row count and SHA-256 per table, ``sqlite_master`` standing for the schema, and a size and SHA-256 per file
    in the data directory but the database's own; each key names its table or file."""
    state: dict[str, tuple[int, str]] = {}
    with closing(storage.connect(workbench.database)) as conn, storage.read_transaction(conn):
        tables = [str(row["name"]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in ["sqlite_master", *tables]:
            rows = sorted(repr(tuple(row)) for row in conn.execute(f'SELECT * FROM "{table}"'))  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- a table name cannot be a bound parameter, and these names come from sqlite_master, the workbench's own schema: no outside input reaches it
            state[f"table {table}"] = (len(rows), hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest())
    data_dir = workbench.config.data_dir
    for path in data_dir.rglob("*"):
        if path.is_file() and not path.name.startswith(cli.DATABASE_FILE):  # the database's rows are read above
            content = path.read_bytes()
            state[f"file {path.relative_to(data_dir)}"] = (len(content), hashlib.sha256(content).hexdigest())
    return state


def changed(before: dict[str, tuple[int, str]], after: dict[str, tuple[int, str]]) -> list[str]:
    """The tables and files that differ, or that only one side has."""
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def test_every_get_route_leaves_the_database_and_the_data_directory_unchanged(client: TestClient) -> None:
    """The guard checks Origin and the UI header on every method but GET, HEAD and OPTIONS. A page on another port of
    this host is same-site, so the operator's browser sends that page's GETs with the SameSite=Strict cookie.

    Every GET route the app declares runs here with a session, its path filled from a run with a finished diagnosis
    job and an executed reprocess action, so a GET route added later runs too, or fails here for want of a value.
    """
    run_id = create_run(client)
    job = diagnose(client, run_id)
    validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate",
                      {"diagnosis_id": job["diagnosis"]["diagnosis_id"]})
    plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
    post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
    executed = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
    values = {"run_id": run_id, "job_id": job["job_id"], "action_id": executed["action_id"],
              "order_id": executed["fulfilled_order_ids"][0]}  # a case with a receipt
    workbench = client.app.state.workbench
    effects: dict[str, tuple[int, list[str]]] = {}  # each route's status and the tables and files it changed
    state = stored_state(workbench)
    for route in client.app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        unfilled = sorted(set(route.param_convertors) - set(values))
        assert not unfilled, f"GET {route.path}: give this test a value for {unfilled} from the seeded run"
        status = client.get(route.path.format(**values)).status_code
        before, state = state, stored_state(workbench)
        effects[route.path] = (status, changed(before, state))
    assert effects
    assert effects == dict.fromkeys(effects, (200, []))
    assert get(client, "/api/session")["operator_id"] == OPERATOR  # and no GET ended the session


# --------------------------------------------------------------------------- the entry point


def test_main_serves_on_loopback_without_trusting_proxy_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvicorn's default trusts X-Forwarded-For and X-Forwarded-Proto from 127.0.0.1 and ::1, so any local program
    could set a request's client address and scheme. Nothing reads either today; a later check must not trust them."""
    served: dict[str, object] = {}

    def run(app: object, **options: object) -> None:
        served.update(options, app=app)

    monkeypatch.setattr(api.uvicorn, "run", run)
    assert api.main(["--data-dir", str(tmp_path / "data"), "--port", "8123"]) == 0
    assert isinstance(served["app"], FastAPI)
    assert (served["host"], served["port"]) == (api.BIND_HOST, 8123)
    assert served["proxy_headers"] is False
