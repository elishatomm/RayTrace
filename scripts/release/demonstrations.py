"""The release demonstrations A, B and C, run through the workbench's own entry points; prints one JSON document.

Run it in the test image, which has no network and an in-memory /tmp:

    docker run --rm --init --pull never --network none --read-only --tmpfs /tmp:size=2g \\
        --entrypoint python <test image> scripts/release/demonstrations.py

* A, a successful recovery: the CLI's separate commands (README, How to run), in the order the README's Flow A demo
  runs them, on worker_backlog seed 101; then the API, on the same data directory, for the queue and the receipt.
* B, an approval that becomes stale: the HTTP API as the page calls it. B1 is the demo walkthrough's stale-approval beat
  (worker_backlog seed 102, approve, replay clock +15 min, execute), then a new plan as the way forward. B2 approves
  worker_backlog seed 101; the simulated provider's refund of one target then arrives through the webhook route
  (docs/RUNNING.md, section 8), before execute.
* C, an unresolved payment state: the API on late_authorization seed 101 (README Flow C). One order whose payment is
  authorised and not captured, before and after the read-only refresh the page offers, and the README's
  late-authorised order after it.

Rules mode only. WORKBENCH_OLLAMA_URL points at a loopback listener that counts every request, so a model call
would be measured, not assumed. Every directory is new and under /tmp; nothing printed names a path. Each step is
timed once on the wall clock (n=1); CLI steps include starting Python, API steps are one local HTTP round trip.
"""
from __future__ import annotations

import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar

import httpx

import recovery_workbench
from recovery_workbench import diagnosis, policy, report
from recovery_workbench.cli import DATABASE_FILE

OPERATOR = "op_demo_operator"  # the README's and the recording script's operator
FIX = "payload@5"
INTEGRATION = "int_SYNTHETIC_MAIN"
STATE_CHANGE = {"x-workbench-request": "1"}  # the header the page sends with every state change
FIFTEEN_MINUTES_MS = 15 * 60_000
REFUND_EVENT_ID = "evt_release_refund_0001"
README_LATE_AUTHORISED_ORDER = "order_SYNTHETIC_920abafa_0341"  # README "The three flows", Flow C
UNCERTAIN_STATES = ("payment_state_unresolved", "capture_not_confirmed")  # both unresolved: refresh, or hold
DIAGNOSE_STEP = "diagnose (rules)"
T = TypeVar("T")
Json = dict[str, Any]


# --------------------------------------------------------------------------- measuring


class ModelCanary(BaseHTTPRequestHandler):
    """Answers 503 to everything and records the request: any model call or probe would land here."""

    requests: list[str] = []

    def _record(self) -> None:
        ModelCanary.requests.append(f"{self.command} {self.path}")
        self.send_response(503)
        self.end_headers()

    do_GET = do_POST = _record

    def log_message(self, *_: object) -> None:
        return


class Steps:
    """Wall-clock time of each step of one demonstration, measured once."""

    def __init__(self) -> None:
        self.timings: list[Json] = []

    def __call__(self, name: str, action: Callable[[], T]) -> T:
        started = time.perf_counter()
        value = action()
        self.timings.append({"step": name, "wall_ms": round((time.perf_counter() - started) * 1000, 1)})
        return value

    def document(self) -> Json:
        return {"steps": self.timings, "total": round(sum(item["wall_ms"] for item in self.timings), 1)}


def select(database: Path, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    """A read of the workbench's database; the connection refuses every write."""
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("PRAGMA query_only = ON")
        return conn.execute(sql, parameters).fetchall()


def effects(database: Path, run_id: str) -> Json:
    """The run's protected local effects (fulfilment rows, one per order at most) and its recorded actions."""
    by_via = dict(select(database, "SELECT via, COUNT(*) FROM fulfilments WHERE run_id = ? GROUP BY via", (run_id,)))
    [(actions,)] = select(database, "SELECT COUNT(*) FROM actions WHERE run_id = ?", (run_id,))
    return {"fulfilments": sum(by_via.values()), "fulfilments_via_reprocess": by_via.get("reprocess", 0),
            "actions": actions}


def committed_by_action(database: Path, run_id: str, action_id: str) -> list[Json]:
    rows = select(database, "SELECT currency, COUNT(*), COUNT(DISTINCT order_id), SUM(amount_paise) FROM fulfilments"
                            " WHERE run_id = ? AND action_id = ? GROUP BY currency", (run_id, action_id))
    return [{"currency": currency, "fulfilment_rows": count, "distinct_orders": orders, "amount_paise": paise}
            for currency, count, orders, paise in rows]


def diagnoses_by_mode(database: Path) -> dict[str, Json]:
    rows = select(database, "SELECT mode, COUNT(*), SUM(model_id IS NOT NULL) FROM diagnoses GROUP BY mode")
    return {mode: {"diagnoses": total, "with_a_model_id": with_model} for mode, total, with_model in rows}


# --------------------------------------------------------------------------- the two entry points

ENVIRONMENT: dict[str, str] = {}


def cli(*args: str) -> str:
    """``python -m recovery_workbench.cli``; its standard output, or an error naming the command and its stderr."""
    done = subprocess.run([sys.executable, "-m", "recovery_workbench.cli", *args], env=ENVIRONMENT,
                          capture_output=True, text=True, timeout=600, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"cli {args[0]} exited {done.returncode}: {done.stderr.strip()[-1500:]}")
    return done.stdout


def cli_json(*args: str) -> Json:
    return json.loads(cli(*args))


def free_port() -> int:
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Server:
    """``python -m recovery_workbench.api`` on a loopback port, with one operator session."""

    def __init__(self, data_dir: Path, log: Path) -> None:
        self.port = free_port()
        self._log = log.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "recovery_workbench.api", "--data-dir", str(data_dir), "--port", str(self.port)],
            env=ENVIRONMENT, stdout=self._log, stderr=subprocess.STDOUT)
        token, self.start_lines = self._printed_token(log)
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=120, trust_env=False)
        self._wait_for_health()
        self.post("/api/session", {"token": token, "operator_id": OPERATOR})

    def _printed_token(self, log: Path) -> tuple[str, list[str]]:
        marker = "Operator token for this server session (shown once): "
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            lines = log.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines):
                if line.startswith(marker):
                    return line.removeprefix(marker), lines[:number]  # the lines before it; never the token
            if self.process.poll() is not None:
                raise RuntimeError(f"the API exited {self.process.returncode} before it printed its token")
            time.sleep(0.05)
        raise RuntimeError("the API printed no operator token within 60 s")

    def _wait_for_health(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if self.http.get("/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        raise RuntimeError("the API did not answer /health within 60 s")

    def get(self, path: str, expect: int = 200) -> Any:
        return checked(self.http.get(path), expect).json()

    def post(self, path: str, body: Json | None = None, expect: int = 200) -> Any:
        return checked(self.http.post(path, json=body or {}, headers=STATE_CHANGE), expect).json()

    def create_run(self, scenario: str, seed: int, timed: Steps) -> str:
        return timed("create run", lambda: self.post("/api/runs", {"scenario": scenario, "seed": seed},
                                                      expect=201)["run_id"])

    def diagnose(self, run_id: str) -> Json:
        job = self.post(f"/api/runs/{run_id}/diagnoses", expect=202)
        deadline = time.monotonic() + 120
        while job["status"] in ("queued", "running"):
            if time.monotonic() > deadline:
                raise RuntimeError(f"diagnosis job {job['job_id']} still {job['status']} after 120 s")
            time.sleep(0.05)
            job = self.get(f"/api/runs/{run_id}/diagnoses/{job['job_id']}")
        if job["status"] != "done":
            raise RuntimeError(f"diagnosis job {job['job_id']} ended {job['status']}: {job.get('error')}")
        return job["diagnosis"]

    def approved_plan(self, run_id: str, timed: Steps) -> tuple[Json, Json, Json]:
        """Diagnose in rules mode, validate payload@5, build the reprocess plan and approve its exact hash."""
        found = timed(DIAGNOSE_STEP, lambda: self.diagnose(run_id))
        validation = timed("validate-fix", lambda: self.post(f"/api/runs/{run_id}/fixes/{FIX}/validate",
                                                             {"diagnosis_id": found["diagnosis_id"]}))
        plan = timed("plan", lambda: self.post(f"/api/runs/{run_id}/plans",
                                               {"fix_validation_id": validation["validation_id"]}))
        approval = timed("approve", lambda: self.post(f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve",
                                                      {"plan_hash": plan["plan_hash"]}))
        return validation, plan, approval

    def close(self) -> None:
        self.http.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=20)
        self._log.close()


def checked(response: httpx.Response, expect: int) -> httpx.Response:
    if response.status_code != expect:
        raise RuntimeError(f"{response.request.method} {response.request.url.path} answered {response.status_code}, "
                           f"expected {expect}: {response.text[:1500]}")
    return response


# --------------------------------------------------------------------------- summaries


def plan_summary(plan: Json) -> Json:
    return {"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"], "operation": plan["operation"],
            "target_count": plan["target_count"], "target_value_paise": plan["target_value_paise"],
            "expires_at_ms": plan["expires_at_ms"],
            "exclusions_by_reason": {reason: len(orders) for reason, orders in plan["exclusions_by_reason"].items()}}


def approval_summary(approval: Json) -> Json:
    return {key: approval[key] for key in ("approval_id", "operator_id", "plan_hash", "approved_at_ms", "expires_at_ms")}


def execution_summary(result: Json) -> Json:
    return {key: result[key] for key in ("action_id", "status", "replayed", "reasons", "restored_processing_value_paise",
                                         "additional_captured_paise", "executed_at_ms")} | {
        "fulfilled_orders": len(result["fulfilled_order_ids"])}


def refusal_receipt(server: Server, run_id: str, action_id: str) -> Json:
    receipt = server.get(f"/api/runs/{run_id}/actions/{action_id}/receipt")
    return {"receipt_hash": receipt["receipt_hash"], "status": receipt["execution"]["status"],
            "results": receipt["execution"]["results"],
            "failed_checks": {item["code"]: item["details"] for item in receipt["preconditions"]["execution"]["checks"]
                              if item["result"] == "failed"},
            "restored_processing_value_paise": receipt["value"]["restored_processing_value_paise"],
            "additional_captured_paise": receipt["value"]["additional_captured_paise"]}


def case_summary(document: Json) -> Json:
    return {**{key: document["summary"][key] for key in ("state", "next_step", "blockers")},
            "state_condition": document["state_condition"], "fulfilled": document["case"]["fulfilment"] is not None,
            "payments": [{key: payment[key] for key in ("payment_id", "status", "late_authorization")}
                         for payment in document["case"]["payments"]]}


def uncertain_totals(queue: Json) -> Json:
    return {"by_state": {item["state"]: item["orders"] for item in queue["totals"]["by_state"]
                         if item["state"] in UNCERTAIN_STATES},
            "unresolved_orders": queue["totals"]["unresolved_orders"]}


# --------------------------------------------------------------------------- A: a successful recovery


def demonstration_a(base: Path) -> tuple[Json, list[str]]:
    timed = Steps()
    data = ("--data-dir", str(base / "a"))
    database = base / "a" / DATABASE_FILE
    seeded = timed("seed", lambda: cli_json("seed", "--scenario", "worker_backlog", "--seed", "101", *data))
    run_id = seeded["run_id"]
    at_seed = effects(database, run_id)
    found = timed(DIAGNOSE_STEP, lambda: cli_json("diagnose", "--run-id", run_id, "--mode", "rules", *data))
    validation = timed("validate-fix", lambda: cli_json("validate-fix", "--run-id", run_id, "--fix-id", FIX,
                                                        "--diagnosis-id", found["diagnosis_id"], "--operator",
                                                        OPERATOR, *data))
    draft = timed("plan", lambda: cli_json("plan", "--run-id", run_id, "--fix-validation-id",
                                           validation["validation_id"], *data))
    reasons: dict[str, set[str]] = {}
    for reason, orders in draft["exclusions_by_reason"].items():
        for order in orders:
            reasons.setdefault(order, set()).add(reason)
    stale_only = sorted(order for order, held_for in reasons.items() if held_for == {"stale_snapshot"})
    [(clock_ms,)] = select(database, "SELECT clock_ms FROM runs WHERE run_id = ?", (run_id,))
    payments = [payment for order, payment in select(
        database, "SELECT order_id, payment_id FROM payment_attempts WHERE run_id = ? AND created_at_ms <= ?"
                  " ORDER BY created_at_ms, payment_id", (run_id, clock_ms)) if order in stale_only]
    refreshed = timed("refresh the stale-only orders' payments (read-only)", lambda: cli_json(
        "refresh", "--run-id", run_id, *[word for payment in payments for word in ("--payment-id", payment)], *data))
    plan = timed("plan again", lambda: cli_json("plan", "--run-id", run_id, "--fix-validation-id",
                                                validation["validation_id"], *data))
    approval = timed("approve", lambda: cli_json("approve", "--run-id", run_id, "--plan-id", plan["plan_id"],
                                                 "--plan-hash", plan["plan_hash"], "--operator", OPERATOR, *data))
    before = effects(database, run_id)
    executed = timed("execute", lambda: cli_json("execute", "--run-id", run_id, "--plan-id", plan["plan_id"], *data))
    after = effects(database, run_id)
    again = timed("execute again", lambda: cli_json("execute", "--run-id", run_id, "--plan-id", plan["plan_id"], *data))
    after_retry = effects(database, run_id)
    exports = [timed(f"receipt export {number}", lambda number=number: cli_json(
        "receipt", "--run-id", run_id, "--action-id", executed["action_id"], "--output-dir",
        str(base / f"receipts_{number}"), *data)) for number in (1, 2)]
    files = [(Path(item["json"]).read_bytes(), Path(item["markdown"]).read_bytes()) for item in exports]
    receipt = json.loads(files[0][0])
    after_receipts = effects(database, run_id)

    server = Server(base / "a", base / "server_a.log")  # the page's view of the same data directory
    try:
        cutoff = seeded["cutoff_ms"]
        example = plan["targets"][0]["id"]
        queue_then = timed("API: queue as of the seed's cutoff", lambda: server.get(
            f"/api/runs/{run_id}/orders?as_of_ms={cutoff}&limit=1"))
        case_then = timed("API: example case as of the cutoff", lambda: server.get(
            f"/api/runs/{run_id}/orders/{example}?as_of_ms={cutoff}"))
        route = f"/api/runs/{run_id}/actions/{executed['action_id']}/receipt"
        served = timed("API: receipt JSON and Markdown", lambda: (
            server.http.get(route).content, server.http.get(f"{route}?format=markdown").content))
        queue_now = server.get(f"/api/runs/{run_id}/orders?limit=1")
        case_now = server.get(f"/api/runs/{run_id}/orders/{example}")
        outcome = server.get(f"/api/runs/{run_id}/report")["outcome"]
        start_lines = server.start_lines
    finally:
        server.close()
    evidence = case_then["case"]["evidence"]
    document = {
        "entry_points": "python -m recovery_workbench.cli (seed, diagnose, validate-fix, plan, refresh, plan, approve, "
                        "execute, execute, receipt, receipt); then python -m recovery_workbench.api on the same data "
                        "directory for the queue, the receipt route and the report",
        "run_id": run_id, "scenario": "worker_backlog", "seed": 101, "seed_cutoff_ms": seeded["cutoff_ms"],
        "diagnosis": {key: found[key] for key in ("diagnosis_id", "mode_label", "status", "rules_version")}
        | {key: found["output"][key] for key in ("hypothesis", "faulting_change_id", "proposed_operation")},
        "fix_validation": {key: validation[key] for key in ("validation_id", "fix_id", "deployed_version", "reproduced",
                                                            "passed")},
        "draft_plan": plan_summary(draft), "stale_only_orders": stale_only,
        "refresh": {"payments": len(payments), "refreshed": len(refreshed["refreshed"]),
                    "unknown_at_provider": len(refreshed["unknown_at_provider"]), "now_ms": refreshed["now_ms"]},
        "plan": plan_summary(plan),
        "target_order_ids": [target["id"] for target in plan["targets"]],
        "approval": approval,
        "queue_before_recovery": {
            "as_of_ms": cutoff, "captured_unprocessed_orders": queue_then["totals"]["captured_unprocessed_orders"],
            "captured_unprocessed_value_paise": queue_then["totals"]["captured_unprocessed_value_paise"],
            "example_order": {"order_id": example, "amount_paise": case_then["summary"]["amount_paise"],
                              "currency": case_then["summary"]["currency"], "state": case_then["summary"]["state"],
                              "next_step": case_then["summary"]["next_step"], "blockers": case_then["summary"]["blockers"],
                              "evidence_links": {name: len(evidence[name]) for name in (
                                  "payment_ids", "observation_ids", "inbox_ids", "worker_attempt_ids",
                                  "delivery_attempt_ids")}}},
        "protected_local_effects": {"after_seed": at_seed, "after_preparing_and_approving": before,
                                    "after_execute": after, "after_execute_again": after_retry,
                                    "after_two_receipt_exports": after_receipts,
                                    "preparing_and_approving_changed_no_order": at_seed == before},
        "execution": execution_summary(executed),
        "execute_again": execution_summary(again) | {"same_action": again["action_id"] == executed["action_id"]},
        "receipt": {
            "receipt_version": receipt["receipt_version"], "receipt_hash": receipt["receipt_hash"],
            "newly_completed_orders": receipt["execution"]["results"]["completed"],
            "results": receipt["execution"]["results"],
            "restored_processing_value_paise_by_currency": {
                receipt["value"]["currency"]: receipt["value"]["restored_processing_value_paise"]},
            "new_captures_caused_by_recovery": {
                "additional_captured_paise": receipt["value"]["additional_captured_paise"],
                "new_captured_payment_ids": receipt["postconditions"]["new_captured_payment_ids"]},
            "duplicate_fulfilments": receipt["postconditions"]["duplicate_fulfilments"],
            "unresolved_orders": receipt["value"]["unresolved_orders"],
            "unresolved_value_paise": receipt["value"]["unresolved_value_paise"],
            "committed_fulfilments_of_this_action": committed_by_action(database, run_id, executed["action_id"]),
            "two_exports_same_hash": exports[0]["receipt_hash"] == exports[1]["receipt_hash"] == receipt["receipt_hash"],
            "two_exports_byte_identical": files[0] == files[1],
            "api_route_bytes_equal_cli_export": served == files[0],
            "report_outcome": {key: outcome[key] for key in (
                "reprocess_actions_succeeded", "orders_fulfilled_by_reprocessing", "restored_processing_value_paise",
                "additional_captured_paise")},
        },
        "queue_after_recovery": {
            "captured_unprocessed_orders": queue_now["totals"]["captured_unprocessed_orders"],
            "captured_unprocessed_value_paise": queue_now["totals"]["captured_unprocessed_value_paise"],
            "example_order": {"state": case_now["summary"]["state"], "next_step": case_now["summary"]["next_step"],
                              "receipts": [{key: item[key] for key in ("action_id", "status", "order_result")}
                                           for item in case_now["receipts"]]}},
        "wall_clock_ms": timed.document(),
    }
    return document, start_lines


# --------------------------------------------------------------------------- B: an approval that becomes stale


def demonstration_b1(server: Server, database: Path) -> Json:
    """The demo walkthrough's stale-approval beat: approve, advance the replay clock 15 minutes, execute; then plan again."""
    timed = Steps()
    run_id = server.create_run("worker_backlog", 102, timed)
    validation, plan, approval = server.approved_plan(run_id, timed)
    execute = f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute"
    before = effects(database, run_id)
    clock = server.get(f"/api/runs/{run_id}")["replay_clock_ms"]
    advanced = timed("advance replay clock +15 min", lambda: server.post(f"/api/runs/{run_id}/advance",
                                                                          {"by_ms": FIFTEEN_MINUTES_MS}))
    refused = timed("execute", lambda: server.post(execute, expect=409))
    after = effects(database, run_id)
    retried = timed("execute again", lambda: server.post(execute, expect=409))
    approve_again = timed("approve the same plan again", lambda: server.post(
        f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]}, expect=409))
    replanned = timed("plan again on current evidence", lambda: server.post(
        f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]}))
    after_replan = effects(database, run_id)
    return {
        "run_id": run_id, "scenario": "worker_backlog", "seed": 102, "plan": plan_summary(plan),
        "approval": approval_summary(approval),
        "replay_clock_ms": {"before_advance": clock, "after_advance": advanced["replay_clock_ms"]},
        "execute": {"http_status": 409, "error": refused["error"], **execution_summary(refused["result"])},
        "receipt_of_the_refusal": refusal_receipt(server, run_id, refused["result"]["action_id"]),
        "protected_local_effects": {"before": before, "after": after, "after_new_plan": after_replan,
                                    "unchanged": before["fulfilments"] == after["fulfilments"]
                                    == after_replan["fulfilments"]},
        "execute_again": {"http_status": 409, "replayed": retried["result"]["replayed"],
                          "same_action": retried["result"]["action_id"] == refused["result"]["action_id"]},
        "approve_the_same_plan_again": {"http_status": 409, "error": approve_again["error"],
                                        "detail": approve_again["detail"]},
        "new_plan_needing_its_own_approval": plan_summary(replanned),
        "wall_clock_ms": timed.document(),
    }


def demonstration_b2(server: Server, base: Path, database: Path) -> Json:
    """A refund of one approved target, delivered by the simulated provider between approval and execution."""
    timed = Steps()
    run_id = server.create_run("worker_backlog", 101, timed)
    _, plan, approval = server.approved_plan(run_id, timed)
    target = plan["targets"][0]
    body_file = base / "refund_body.json"
    printed = timed("cli webhook-request (signs; reads only)", lambda: cli(
        "webhook-request", "--data-dir", str(base / "api"), "--run-id", run_id, "--payment-id", target["payment_id"],
        "--status", "refunded", "--event-id", REFUND_EVENT_ID, "--output", str(body_file), "--port", str(server.port)))
    signed = dict(line.split(": ", 1) for line in printed.splitlines() if line.startswith("X-Webhook-"))
    before = effects(database, run_id)
    delivered = timed("POST the signed refund to the webhook route", lambda: checked(server.http.post(
        f"/api/runs/{run_id}/webhooks/{INTEGRATION}", content=body_file.read_bytes(),
        headers={"Content-Type": "application/json", **signed}), 200).json())
    refused = timed("execute", lambda: server.post(f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute", expect=409))
    after = effects(database, run_id)
    case = server.get(f"/api/runs/{run_id}/orders/{target['id']}")
    return {
        "run_id": run_id, "scenario": "worker_backlog", "seed": 101, "plan": plan_summary(plan),
        "approval": approval_summary(approval),
        "business_change_after_approval": {
            "kind": "refund of one approved target, signed as the simulated provider signs (cli webhook-request)",
            "order_id": target["id"], "payment_id": target["payment_id"], "amount_paise": target["amount_paise"],
            "event_id": REFUND_EVENT_ID, "webhook_answer": delivered},
        "execute": {"http_status": 409, "error": refused["error"], **execution_summary(refused["result"])},
        "receipt_of_the_refusal": refusal_receipt(server, run_id, refused["result"]["action_id"]),
        "protected_local_effects": {"before": before, "after": after,
                                    "unchanged": before["fulfilments"] == after["fulfilments"]},
        "refunded_order_in_the_queue": {key: case["summary"][key] for key in ("state", "next_step", "blockers")},
        "wall_clock_ms": timed.document(),
    }


# --------------------------------------------------------------------------- C: payment state unresolved


def demonstration_c(server: Server, database: Path) -> Json:
    timed = Steps()
    run_id = server.create_run("late_authorization", 101, timed)
    orders = f"/api/runs/{run_id}/orders"
    queue = timed("queue: uncertain payment states", lambda: server.get(
        f"{orders}?{'&'.join(f'state={name}' for name in UNCERTAIN_STATES)}&limit=5000"))
    order = queue["orders"][0]["order_id"]
    case = timed("case before the refresh", lambda: server.get(f"{orders}/{order}"))
    found = timed(DIAGNOSE_STEP, lambda: server.diagnose(run_id))
    candidates = server.get(f"/api/runs/{run_id}/payment-state")["refresh_candidates"]  # what the page refreshes
    before = effects(database, run_id)
    refreshed = timed("refresh (read-only provider snapshot)", lambda: server.post(
        f"/api/runs/{run_id}/refresh", {"payment_ids": candidates}))
    case_after = timed("case after the refresh", lambda: server.get(f"{orders}/{order}"))
    queue_after = server.get(f"{orders}?limit=5000")
    late = server.get(f"{orders}/{README_LATE_AUTHORISED_ORDER}")
    after_refresh = effects(database, run_id)
    escalation = timed("escalate", lambda: server.post(f"/api/runs/{run_id}/plans",
                                                       {"operation": "escalate", "diagnosis_id": found["diagnosis_id"]}))
    after_escalation = effects(database, run_id)
    unresolved = {item["state"] for item in queue_after["states"] if item["unresolved"]}
    return {
        "run_id": run_id, "scenario": "late_authorization", "seed": 101,
        "unresolved_before_the_refresh": uncertain_totals(queue),
        "order_id": order, "case_before_the_refresh": case_summary(case),
        "diagnosis": {key: found[key] for key in ("diagnosis_id", "mode_label", "status")}
        | {key: found["output"][key] for key in ("hypothesis", "proposed_operation", "next_check")},
        "refresh": {"payments_requested": len(candidates), "includes_the_order": all(
            payment["payment_id"] in candidates for payment in case["case"]["payments"]),
                    "refreshed": len(refreshed["refreshed"]), "unknown_at_provider": len(refreshed["unknown_at_provider"]),
                    "now_ms": refreshed["now_ms"]},
        "case_after_the_refresh": case_summary(case_after),
        "still_counted_unresolved": case_after["summary"]["state"] in unresolved,
        "readme_late_authorised_order_after_the_refresh": {"order_id": README_LATE_AUTHORISED_ORDER,
                                                            **case_summary(late),
                                                            "counted_unresolved": late["summary"]["state"] in unresolved},
        "unresolved_after_the_refresh": uncertain_totals(queue_after),
        "escalation": {"action_id": escalation["action_id"], "operation": escalation["operation"],
                       "evidence_needed": escalation["evidence_needed"]},
        "protected_local_effects": {"before_refresh": before, "after_refresh": after_refresh,
                                    "after_escalation": after_escalation,
                                    "no_new_fulfilment": before["fulfilments"] == after_refresh["fulfilments"]
                                    == after_escalation["fulfilments"]},
        "wall_clock_ms": timed.document(),
    }


# --------------------------------------------------------------------------- the document


def main() -> int:
    canary = ThreadingHTTPServer(("127.0.0.1", 0), ModelCanary)
    threading.Thread(target=canary.serve_forever, daemon=True).start()
    ENVIRONMENT.update({name: value for name, value in os.environ.items() if not name.startswith("WORKBENCH_")})
    ENVIRONMENT.update({"WORKBENCH_MODEL_MODE": "rules",
                        "WORKBENCH_OLLAMA_URL": f"http://127.0.0.1:{canary.server_address[1]}"})
    base = Path(tempfile.mkdtemp(prefix="rw-release-demo-", dir="/tmp"))  # new, private and empty
    a, a_start = demonstration_a(base)
    server = Server(base / "api", base / "server_api.log")
    try:
        api_database = base / "api" / DATABASE_FILE
        b1 = demonstration_b1(server, api_database)
        b2 = demonstration_b2(server, base, api_database)
        c = demonstration_c(server, api_database)
        api_start = server.start_lines
    finally:
        server.close()
    canary.shutdown()
    by_mode = {"A (cli data directory)": diagnoses_by_mode(base / "a" / DATABASE_FILE),
               "B and C (api data directory)": diagnoses_by_mode(base / "api" / DATABASE_FILE)}
    evaluated = sum(item["diagnoses"] for modes in by_mode.values() for item in modes.values())
    with_model = sum(item["with_a_model_id"] for modes in by_mode.values() for item in modes.values())
    document = {
        "document": "RayTrace release demonstrations (synthetic sandbox)",
        "conditions": {
            "model_mode": "rules (WORKBENCH_MODEL_MODE=rules for every command and server)",
            "data_directory": "new and empty, under /tmp in the container; paths are not printed",
            "timing": "wall clock per step, measured once (n=1) in one container; CLI steps include starting Python, "
                      "API steps are one HTTP round trip to the local server; not a latency benchmark",
        },
        "versions": {"application": recovery_workbench.__version__, "python": platform.python_version(),
                     "sqlite": sqlite3.sqlite_version, "policy": policy.POLICY_VERSION,
                     "validator": diagnosis.VALIDATOR_VERSION, "receipt": report.RECEIPT_VERSION},
        "server_start_lines": {"A": a_start, "B_and_C": api_start},
        "A_successful_recovery": a,
        "B1_approval_expired_on_the_replay_clock": b1,
        "B2_refund_after_approval": b2,
        "C_payment_state_unresolved": c,
        "model_invocations": {
            "requests_at_the_model_url": len(ModelCanary.requests), "request_lines": ModelCanary.requests,
            "diagnoses_by_mode": by_mode, "cases_evaluated": evaluated, "cases_with_a_model_call": with_model,
            "rate": f"{with_model}/{evaluated}",
        },
    }
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
