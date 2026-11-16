#!/usr/bin/env python3
"""Records the production UI and the real API for the static recorded demo (scripts/demo/build-demo.sh runs it).

It runs inside the UI walkthrough image with no network, after record.sh has started the real API in rules mode on an
empty data directory. Headless Chromium drives the production UI through the README's three flows, with the steps
of the demo walkthrough, so the values match the docs:

  * Flow A, worker_backlog seed 101: diagnose; validate payload@5; build the plan; refresh the stale-only orders and
    re-plan; approve; execute; execute again; then one recovered order's case and its receipt;
  * Flow B, method_degradation seed 101: diagnose; escalate;
  * Flow C, late_authorization seed 101: diagnose; refresh provider state (read-only); escalate.

A script added to every page before the UI loads wraps window.fetch and keeps each request the page makes and each
answer it gets: the method, path and query, the body of a state-changing request, and the answer's status, content type
and text. It keeps no header, and never the body of the login request, which carries the operator token.

Every answer is filed under the run it concerns and the stage of that run's flow it was given at: a stage is the server
state between two state-changing requests. At each stage the recorder chooses every Show filter option, reads the first
rows of each under each sort, opens the case of each of those orders (and of the orders the docs name), and exports the
report, so the replay can answer all of these at every point of each flow.

It writes the site's data to <out>/site (demo-data/ and receipts/) and a summary to <out>/recording-summary.json, then
scans everything under <out> for the operator token and the session cookie, and deletes the site data and fails if
either is found.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import BrowserContext, Locator, Page, Response, expect, sync_playwright

BASE_URL = "http://127.0.0.1:8000"
OPERATOR_ID = "op_demo_operator"
TOKEN_LINE = re.compile(r"Operator token for this server session \(shown once\): (\S+)")
RUN_PATH = re.compile(r"^/api/runs/(?P<run_id>[^/]+)(?P<rest>/.*)?$")
RECEIPT_PATH = re.compile(r"^/api/runs/(?P<run_id>[^/]+)/actions/(?P<action_id>[^/]+)/receipt$")
GLOBAL_GET_PATHS = frozenset({"/health", "/api/session", "/api/scenarios", "/api/fixes", "/api/runs"})
SECURITY_HEADER_NAMES = ("content-security-policy", "x-content-type-options", "x-frame-options", "referrer-policy")
FORMAT = "recovery-workbench-recorded-demo/1"
OPEN_RUN = "Open run"
EXPECT_TIMEOUT_MS = 30_000
SEED_TIMEOUT_MS = 300_000  # seeding a whole world through the UI
QUIET_MS = 120  # how long the page must stay idle to count as settled
VIEWPORT = {"width": 1440, "height": 900}
FIRST_BY_ORDER = 5  # cases recorded: the first rows each Show filter lists in its default order...
FIRST_BY_SORT = 3  # ...and the first rows under the Amount and Age sorts (largest first)
REPLAY_CLOCK_A = 1_793_881_800_000  # 2026-11-05 12:30:00Z, the worker_backlog world's cutoff
OPERATOR_STEP_MS = 60_000

# The page's fetch, wrapped before any page script runs. It records answers only; it never changes one.
CAPTURE_SCRIPT = """
(() => {
  if (Object.prototype.hasOwnProperty.call(window, "__demoCapture")) {
    return;
  }
  const exchanges = [];
  const counters = { started: 0, inFlight: 0 };
  Object.defineProperty(window, "__demoCapture", { value: { exchanges, counters } });
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    if (typeof input !== "string") {
      exchanges.push({ unsupported: Object.prototype.toString.call(input) });
      return originalFetch(input, init);
    }
    const url = new URL(input, window.location.href);
    const method = (init.method ?? "GET").toUpperCase();
    const isLogin = method === "POST" && url.pathname === "/api/session";
    const entry = {
      method,
      path: url.pathname,
      query: url.search,
      request: isLogin || typeof init.body !== "string" ? null : init.body,
      sent: performance.now(),
    };
    counters.started += 1;
    counters.inFlight += 1;
    try {
      const response = await originalFetch(input, init);
      entry.received = performance.now();
      entry.status = response.status;
      entry.type = response.headers.get("content-type");
      entry.body = await response.clone().text();
      exchanges.push(entry);
      return response;
    } catch (error) {
      entry.received = performance.now();
      entry.failure = String(error);
      exchanges.push(entry);
      throw error;
    } finally {
      counters.inFlight -= 1;
    }
  };
})();
"""

ROW_IDS_SCRIPT = """
(panel, limit) => [...panel.querySelectorAll(":scope > div.scroll > table > tbody > tr > td:first-child > button")]
  .slice(0, limit === null ? undefined : limit)
  .map((button) => button.textContent)
"""


class RecordingError(RuntimeError):
    """The recording cannot be trusted: the page or the API did something the recorder did not expect."""


@dataclass(frozen=True)
class FlowPlan:
    letter: str
    scenario: str
    seed: int
    title: str
    guide: str  # what the visitor does, in the README's words
    named_orders: tuple[str, ...]  # orders the README or the demo walkthrough names: their cases are recorded too


FLOW_A = FlowPlan(
    "A", "worker_backlog", 101, "the payments succeeded and our worker failed",
    "Diagnose the latest incident. In Recovery, choose the corrected handler payload@5 and validate it, build the "
    "reprocessing plan, refresh the stale-only orders and re-plan, approve, execute, and execute again (the replay). "
    "Then set Show to Complete, sort by Amount and open order_SYNTHETIC_547a954d_0099: its case and its receipt.",
    tuple(f"order_SYNTHETIC_547a954d_{number}" for number in (
        "0049", "0060", "0065", "0081", "0093", "0094", "0095", "0099", "0121", "0123", "0128", "0150")),
)
FLOW_B = FlowPlan(
    "B", "method_degradation", 101, "one payment method is failing at the provider",
    "Diagnose the latest incident, then escalate with the evidence needed. Nothing is reprocessed.",
    (),
)
FLOW_C = FlowPlan(
    "C", "late_authorization", 101, "ambiguous payment state",
    "Diagnose the latest incident, refresh provider state (read-only), then escalate.",
    ("order_SYNTHETIC_920abafa_0341", "order_SYNTHETIC_920abafa_0351"),
)
FLOWS = (FLOW_A, FLOW_B, FLOW_C)


# --------------------------------------------------------------------------- what the page asked and was answered


@dataclass(frozen=True)
class Answer:
    status: int
    type: str | None
    body: str

    def as_json(self) -> dict:
        return {"status": self.status, "type": self.type, "body": self.body}


@dataclass(frozen=True)
class Exchange:
    method: str
    path: str
    query: str
    request: str | None
    answer: Answer
    sent: float
    received: float

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}{self.query}"

    def data(self):
        return json.loads(self.answer.body)


def to_exchange(item: dict) -> Exchange:
    if "unsupported" in item:
        raise RecordingError(f"the page called fetch with {item['unsupported']}, which the recorder does not capture")
    if "failure" in item:
        raise RecordingError(f"{item['method']} {item['path']}{item['query']} failed in the browser: {item['failure']}")
    answer = Answer(status=item["status"], type=item["type"], body=item["body"])
    return Exchange(item["method"], item["path"], item["query"], item["request"], answer, item["sent"], item["received"])


@dataclass
class Transition:
    label: str
    exchange: Exchange


@dataclass
class RunTape:
    """One run's flow: the answers given at each stage, and the state-changing requests between stages."""

    run_id: str
    flow: FlowPlan
    stage_names: list[str] = field(default_factory=lambda: ["opened"])
    stages: list[dict[str, list[Answer]]] = field(default_factory=lambda: [{}])
    transitions: list[Transition] = field(default_factory=list)
    expected: list[tuple[str, str]] = field(default_factory=list)  # (label, stage name) of the next state changes
    covered: list[int] = field(default_factory=list)  # stages whose coverage pass ran
    case_orders: list[str] = field(default_factory=list)  # cases recorded, in the order they were first chosen
    receipt_reads: dict[str, Answer] = field(default_factory=dict)  # the page's own receipt reads, by key

    @property
    def stage(self) -> int:
        return len(self.transitions)


class Tape:
    """Every exchange the page made, filed under a phase of the session or under one stage of one run's flow."""

    def __init__(self) -> None:
        self.phase = "setup"
        self.global_answers: dict[str, dict[str, list[Answer]]] = {}
        self.session: dict[str, Answer] = {}
        self.runs: dict[str, RunTape] = {}
        self.creating: list[FlowPlan] = []
        self.latest: dict[str, Exchange] = {}  # the latest exchange per method and path, for the recorder's checks

    def expect(self, run_id: str, steps: list[tuple[str, str]]) -> None:
        run = self.runs[run_id]
        if run.expected:
            raise RecordingError(f"{run_id} still expects {run.expected} before {steps}")
        run.expected = list(steps)

    def add(self, raw: list[dict]) -> None:
        exchanges = sorted((to_exchange(item) for item in raw), key=lambda item: item.sent)
        base = {run_id: run.stage for run_id, run in self.runs.items()}  # each run's stage before this batch
        changes: dict[str, list[Exchange]] = {}  # each run's state changes in this batch
        for exchange in exchanges:
            self._take(exchange, base, changes)
        for exchange in exchanges:
            match = RUN_PATH.match(exchange.path)
            if exchange.method == "GET" and match is not None:
                self._file_read(self._run(match["run_id"]), exchange, base[match["run_id"]], changes)

    def _take(self, exchange: Exchange, base: dict[str, int], changes: dict[str, list[Exchange]]) -> None:
        """Files an exchange that is not a run's read: a session-wide read, a login or logout, a new run, a state change."""
        if exchange.answer.status >= 400:
            raise RecordingError(f"{exchange.key} answered {exchange.answer.status}: {exchange.answer.body[:300]}")
        self.latest[f"{exchange.method} {exchange.path}"] = exchange
        match = RUN_PATH.match(exchange.path)
        if exchange.method == "GET" and exchange.path in GLOBAL_GET_PATHS:
            answers = self.global_answers.setdefault(self.phase, {}).setdefault(exchange.key, [])
            if exchange.answer not in answers:
                answers.append(exchange.answer)
        elif exchange.path == "/api/session":
            self._session(exchange)
        elif exchange.method == "POST" and exchange.path == "/api/runs":
            base[self._created(exchange)] = 0
        elif match is None:
            raise RecordingError(f"the page requested {exchange.key}, which the recorder does not know")
        elif exchange.method != "GET":
            run = self._run(match["run_id"])
            self._transition(run, exchange)
            changes.setdefault(run.run_id, []).append(exchange)

    def _file_read(self, run: RunTape, exchange: Exchange, base: int, changes: dict[str, list[Exchange]]) -> None:
        """Files a run's read under the stage it saw: one stage further for each state change answered before it."""
        mine = changes.get(run.run_id, [])
        if any(change.sent < exchange.received and exchange.sent < change.received for change in mine):
            raise RecordingError(f"{exchange.key} overlapped a state change of {run.run_id}: its stage is ambiguous")
        self._file(run, base + sum(1 for change in mine if change.received <= exchange.sent), exchange)

    def _run(self, run_id: str) -> RunTape:
        if run_id not in self.runs:
            raise RecordingError(f"the page requested run {run_id}, which the recorder did not create")
        return self.runs[run_id]

    def _session(self, exchange: Exchange) -> None:
        if exchange.method == "POST":
            if exchange.request is not None:
                raise RecordingError("the login request's body was captured; it must never be")
            self.session["login"] = exchange.answer
        elif exchange.method == "DELETE":
            self.session["logout"] = exchange.answer
        else:
            raise RecordingError(f"unexpected session request {exchange.key}")

    def _created(self, exchange: Exchange) -> str:
        if not self.creating:
            raise RecordingError("a run was created that the recorder did not ask for")
        flow = self.creating.pop(0)
        summary = exchange.data()
        if (summary["scenario"], summary["seed"]) != (flow.scenario, flow.seed):
            raise RecordingError(f"created {summary['scenario']} seed {summary['seed']}, expected {flow.scenario} {flow.seed}")
        self.runs[summary["run_id"]] = RunTape(summary["run_id"], flow)
        return summary["run_id"]

    def _transition(self, run: RunTape, exchange: Exchange) -> None:
        if not run.expected:
            raise RecordingError(f"unexpected state change {exchange.key} on {run.run_id}")
        label, stage_name = run.expected.pop(0)
        run.transitions.append(Transition(label, exchange))
        run.stage_names.append(stage_name)
        run.stages.append({})

    def _file(self, run: RunTape, stage: int, exchange: Exchange) -> None:
        receipt = RECEIPT_PATH.match(exchange.path)
        if receipt is not None:  # receipts are written as files of their own, not into a stage
            run.receipt_reads[exchange.key] = exchange.answer
            return
        answers = run.stages[stage].setdefault(exchange.key, [])
        if exchange.answer not in answers:
            answers.append(exchange.answer)


# --------------------------------------------------------------------------- driving the page


@dataclass
class ReceiptFile:
    run_id: str
    action_id: str
    json_bytes: bytes
    json_type: str
    markdown_bytes: bytes
    markdown_type: str
    from_stage: int


class Driver:
    def __init__(self, page: Page, context: BrowserContext, tape: Tape) -> None:
        self.page = page
        self.context = context
        self.tape = tape

    # ------------------------------------------------------------ waiting

    def counters(self) -> tuple[int, int]:
        started, in_flight = self.page.evaluate(
            "() => [window.__demoCapture.counters.started, window.__demoCapture.counters.inFlight]")
        return started, in_flight

    def settle(self, timeout_ms: int = EXPECT_TIMEOUT_MS) -> None:
        """Waits until no request is in flight, none starts for QUIET_MS and the busy line is gone."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            started, in_flight = self.counters()
            if in_flight == 0 and self.page.locator("p.busy").count() == 0:
                self.page.wait_for_timeout(QUIET_MS)
                if self.counters() == (started, 0) and self.page.locator("p.busy").count() == 0:
                    return
            else:
                self.page.wait_for_timeout(25)
        raise RecordingError(f"the page did not settle within {timeout_ms} ms")

    def drain(self) -> None:
        self.tape.add(self.page.evaluate("() => window.__demoCapture.exchanges.splice(0)"))

    def no_error(self, where: str) -> None:
        shown = self.page.locator("p.banner.error").all_inner_texts()
        if shown:
            raise RecordingError(f"{where}: the page shows an error: {shown}")

    def act(self, action: Callable[[], None], where: str, *, timeout_ms: int = EXPECT_TIMEOUT_MS) -> None:
        """Does something that sends at least one request, then waits for the page to settle and files the answers."""
        started, _ = self.counters()
        action()
        self.page.wait_for_function("(count) => window.__demoCapture.counters.started > count", arg=started,
                                    timeout=timeout_ms)
        self.settle(timeout_ms)
        self.drain()
        self.no_error(where)

    # ------------------------------------------------------------ session and runs

    def log_in(self, token: str) -> None:
        expect(self.page.get_by_role("heading", name="Operator login")).to_be_visible()
        self.page.get_by_label("Operator token").fill(token)
        self.page.get_by_label("Operator ID (op_...)").fill(OPERATOR_ID)
        self.act(lambda: self.page.get_by_role("button", name="Start session").click(), "log in")
        expect(self.page.locator("header.topbar .operator")).to_contain_text(f"Operator {OPERATOR_ID}")

    def create_run(self, flow: FlowPlan) -> None:
        self.tape.creating.append(flow)
        self.page.get_by_label("Scenario").select_option(flow.scenario)
        self.page.get_by_role("textbox", name="Seed", exact=True).fill(str(flow.seed))  # Open run's options say "seed"
        self.act(lambda: self.page.get_by_role("button", name="Create synthetic run").click(),
                 f"create {flow.scenario}", timeout_ms=SEED_TIMEOUT_MS)
        if self.tape.creating:
            raise RecordingError(f"creating {flow.scenario} seed {flow.seed} sent no create request")

    def open_run(self, run: RunTape) -> None:
        self.act(lambda: self.page.get_by_label(OPEN_RUN).select_option(run.run_id), f"open {run.run_id}")
        expect(self.page.locator("header.topbar .topbar-facts")).to_contain_text(f"Run: {run.run_id}")
        expect(self.show()).to_be_visible()

    # ------------------------------------------------------------ the queue and the case view

    def queue(self) -> Locator:
        return self.page.locator("main section.panel.queue")

    def show(self) -> Locator:
        return self.queue().locator(":scope > div.row select")

    def show_options(self) -> list[tuple[str, str]]:
        return [tuple(item) for item in self.show().evaluate(
            "(select) => [...select.options].map((option) => [option.value, option.textContent])")]

    def row_ids(self, limit: int | None = None) -> list[str]:
        return self.queue().evaluate(ROW_IDS_SCRIPT, limit)

    def choose_filter(self, key: str) -> None:
        if self.show().input_value() == key:
            return
        self.act(lambda: self.show().select_option(key), f"Show {key}")

    def sort_by(self, label: str, direction: str) -> None:
        header = self.queue().locator(":scope > div.scroll > table > thead th", has=self.page.locator(
            "button", has_text=label))
        header.locator("button").click()
        expect(header).to_have_attribute("aria-sort", direction)

    def check_rows(self, run: RunTape, label: str) -> None:
        latest = self.tape.latest[f"GET /api/runs/{run.run_id}/orders"].data()
        shown = self.row_ids()
        listed = [row["order_id"] for row in latest["orders"]]
        count = int(re.search(r"\((\d+)\)$", label).group(1))
        if shown != listed or latest["matching_orders"] != count:
            raise RecordingError(f"Show {label!r}: the table lists {len(shown)} rows, the answer {len(listed)} "
                                 f"({latest['matching_orders']} matching)")

    def first_rows(self) -> list[str]:
        """The first rows the current filter lists in its default order, then by Amount and by Age, largest first."""
        firsts = self.row_ids(FIRST_BY_ORDER)
        self.sort_by("Amount", "descending")
        firsts += self.row_ids(FIRST_BY_SORT)
        self.sort_by("Age", "descending")
        firsts += self.row_ids(FIRST_BY_SORT)
        self.sort_by("Order", "ascending")
        return firsts

    def open_case(self, order_id: str) -> Locator:
        button = self.queue().locator(
            f':scope > div.scroll > table > tbody > tr > td:first-child > button:text-is("{order_id}")')
        self.act(button.click, f"open the case of {order_id}")
        detail = self.queue().locator(":scope > div.case-detail")
        expect(detail.locator(":scope > h3")).to_contain_text(f"Case {order_id}")
        expect(detail).to_contain_text("As of replay time")
        return detail

    def close_case(self) -> None:
        detail = self.queue().locator(":scope > div.case-detail")
        if detail.count():
            detail.get_by_role("button", name="Close").click()
            expect(detail).to_have_count(0)

    def export_report(self) -> None:
        button = self.page.locator("section.panel.report").get_by_role("button", name="Export report (JSON)")
        with self.page.expect_download():
            self.act(button.click, "export the report")

    def cover_stage(self, run: RunTape) -> None:
        """Every Show filter, the cases of the first orders each lists, and the report, at the run's current stage."""
        stage = run.stage
        self.close_case()
        firsts: list[str] = []
        options = self.show_options()
        for key, label in options:
            self.choose_filter(key)
            self.check_rows(run, label)
            firsts += self.first_rows()
        self.choose_filter("all")
        listed = set(self.row_ids())
        missing = [order_id for order_id in run.flow.named_orders if order_id not in listed]
        if missing:
            raise RecordingError(f"{run.run_id} does not list the orders the docs name: {missing}")
        for order_id in [*run.flow.named_orders, *firsts]:
            if order_id not in run.case_orders:
                run.case_orders.append(order_id)
        for order_id in run.case_orders:
            self.open_case(order_id)
        self.close_case()
        self.export_report()
        run.covered.append(stage)
        print(f"[record] {run.flow.letter} stage {stage} ({run.stage_names[stage]}): {len(options)} filters, "
              f"{len(run.case_orders)} cases, report", flush=True)

    # ------------------------------------------------------------ operator steps

    def recovery(self) -> Locator:
        return self.page.locator("section.panel.recovery")

    def step(self, run: RunTape, steps: list[tuple[str, str]], press: Callable[[], None],
             done: Callable[[], None]) -> None:
        self.tape.expect(run.run_id, steps)
        self.act(press, steps[0][0])
        done()
        self.settle()
        self.drain()
        self.no_error(steps[-1][0])
        if run.expected:
            raise RecordingError(f"{run.run_id}: {run.expected} did not happen")

    def diagnose(self, run: RunTape) -> None:
        panel = self.page.locator("section.panel.diagnosis")
        mode = panel.get_by_label("Mode").input_value()
        if mode != "rules":
            raise RecordingError(f"the Diagnosis panel's mode is {mode!r}, not the rules baseline")
        self.step(run, [("diagnose the latest incident with the rules baseline", "diagnosed")],
                  panel.get_by_role("button", name="Diagnose the latest incident").click,
                  lambda: expect(panel.locator("p.job-line strong.status")).to_have_text("done"))

    def validate_fix(self, run: RunTape, fix_id: str) -> None:
        self.recovery().get_by_label("Corrected handler").select_option(fix_id)
        self.step(run, [(f"validate the corrected handler {fix_id}", "fix validated")],
                  self.recovery().get_by_role("button", name="Validate this corrected handler").click,
                  lambda: expect(self.recovery().locator("p.verdict")).to_contain_text(f"{fix_id}: reproduced"))

    def build_plan(self, run: RunTape) -> None:
        self.step(run, [("build the reprocessing plan", "plan built")],
                  self.recovery().get_by_role("button", name="Build reprocessing plan").click,
                  lambda: expect(self.recovery().locator("div.plan > h3")).to_contain_text("Plan plan_"))

    def refresh_and_replan(self, run: RunTape) -> None:
        self.step(run, [("refresh the stale-only orders' provider state (read-only)", "refreshed"),
                        ("build the plan again", "re-planned")],
                  self.recovery().get_by_role("button", name="Refresh provider state (read-only) and re-plan").click,
                  lambda: expect(self.recovery().locator("div.plan")).to_contain_text("Replaces draft"))

    def approve(self, run: RunTape) -> None:
        self.step(run, [("approve the plan", "approved")],
                  self.recovery().get_by_role("button", name=re.compile(r"^Approve plan ")).click,
                  lambda: expect(self.recovery().locator("p.approved")).to_contain_text("Approved by"))

    def execute(self, run: RunTape, *, again: bool) -> None:
        name = "Execute again (shows the replay)" if again else "Execute"
        label = ("execute the plan again (the replay)", "executed again") if again else ("execute the approved plan", "executed")
        self.step(run, [label], self.recovery().get_by_role("button", name=name, exact=True).click,
                  lambda: expect(self.recovery().locator("ul.executions > li")).to_have_count(2 if again else 1))
        receipt_step = self.page.locator("section.rail ol.rail-steps > li").nth(5)
        expect(receipt_step).to_have_class(re.compile(r"\bstep-done\b"))  # the rail read the receipt for its hash

    def escalate(self, run: RunTape) -> None:
        self.step(run, [("escalate with the evidence needed", "escalated")],
                  self.recovery().get_by_role("button", name="Escalate with the evidence needed (no reprocessing)").click,
                  lambda: expect(self.recovery()).to_contain_text("Escalated as action"))

    def refresh_open_payments(self, run: RunTape) -> None:
        button = self.recovery().get_by_role(
            "button", name=re.compile(r"^Refresh provider state for \d+ open or late-authorised payments"))
        self.step(run, [("refresh provider state (read-only)", "refreshed")], button.click,
                  lambda: expect(self.recovery()).to_contain_text("Refreshed "))

    # ------------------------------------------------------------ the receipt of one recovered order

    def receipt_tab(self, link: Locator) -> tuple[bytes, str]:
        navigations: list[Response] = []

        def remember(response: Response) -> None:
            if response.request.is_navigation_request() and "/receipt" in response.url:
                navigations.append(response)

        self.context.on("response", remember)
        try:
            with self.context.expect_page() as opened:
                link.click()
            tab = opened.value
            tab.wait_for_load_state("load")
        finally:
            self.context.remove_listener("response", remember)
        response = next((item for item in navigations if item.url == tab.url), None)
        if response is None or response.status != 200:
            raise RecordingError(f"the receipt tab {tab.url} did not load with 200")
        body = response.body()
        content_type = response.headers.get("content-type", "")
        tab.close()
        return body, content_type

    def recovered_order_receipt(self, run: RunTape) -> tuple[str, ReceiptFile]:
        self.choose_filter("completed")
        self.sort_by("Amount", "descending")
        first = self.row_ids(1)[0]
        detail = self.open_case(first)
        json_link = detail.get_by_role("link", name="Receipt (JSON)")
        markdown_link = detail.get_by_role("link", name="Receipt (Markdown)")
        action = re.search(r"/actions/([^/]+)/receipt$", json_link.get_attribute("href") or "")
        if action is None:
            raise RecordingError(f"the case of {first} links no receipt")
        json_bytes, json_type = self.receipt_tab(json_link)
        markdown_bytes, markdown_type = self.receipt_tab(markdown_link)
        self.close_case()
        self.sort_by("Order", "ascending")
        self.choose_filter("all")
        action_id = action.group(1)
        read = run.receipt_reads.get(f"GET /api/runs/{run.run_id}/actions/{action_id}/receipt")
        if read is None or read.body.encode("utf-8") != json_bytes:
            raise RecordingError("the receipt the rail read differs from the receipt the case links to")
        from_stage = next(index + 1 for index, item in enumerate(run.transitions)
                          if item.exchange.path.endswith("/execute"))
        return first, ReceiptFile(run.run_id, action_id, json_bytes, json_type, markdown_bytes, markdown_type,
                                  from_stage)


# --------------------------------------------------------------------------- the documented values


@dataclass
class Checks:
    results: list[dict] = field(default_factory=list)

    def equal(self, name: str, actual: object, expected: object) -> None:
        self.results.append({"check": name, "expected": expected, "actual": actual, "passed": actual == expected})

    def starts(self, name: str, actual: str, prefix: str) -> None:
        self.results.append({"check": name, "expected": f"{prefix}...", "actual": actual,
                             "passed": actual.startswith(prefix)})

    @property
    def failed(self) -> list[dict]:
        return [item for item in self.results if not item["passed"]]


def stage_answer(run: RunTape, stage: int, key: str) -> dict:
    answers = run.stages[stage].get(key)
    if not answers:
        raise RecordingError(f"{run.run_id} stage {stage} holds no answer to {key}")
    return json.loads(answers[-1].body)


def done_job(run: RunTape) -> dict:
    """The diagnosis job's answer once it was done: the page polls it at the stage the diagnosis started."""
    polls = (json.loads(answer.body) for key, answers in run.stages[1].items() if "/diagnoses/job" in key
             for answer in answers)
    return next(job for job in polls if job["status"] == "done")


def transition_answer(run: RunTape, label: str) -> dict:
    return next(item.exchange.data() for item in run.transitions if item.label == label)


def documented_checks(tape: Tape, recovered_order: str, receipt: ReceiptFile) -> Checks:
    """The values the README and the demo walkthrough give for these steps, against what was recorded."""
    checks = Checks()
    runs = {run.flow.letter: run for run in tape.runs.values()}
    a, b, c = runs["A"], runs["B"], runs["C"]
    checks.equal("Flow A run", a.run_id, "run_53966a08d4")
    checks.equal("Flow B run", b.run_id, "run_a59cd5d713")
    queue = stage_answer(a, 0, f"GET /api/runs/{a.run_id}/orders?limit=5000")
    totals = queue["totals"]
    checks.equal("A opened: replay time", queue["as_of_ms"], REPLAY_CLOCK_A)
    checks.equal("A opened: captured, processing incomplete (orders, paise)",
                 [totals["captured_unprocessed_orders"], totals["captured_unprocessed_value_paise"]], [98, 11_959_200])
    checks.equal("A opened: still awaiting (orders, paise, unsummed)",
                 [totals["unresolved_orders"], totals["unresolved_value_paise"], totals["unresolved_unasserted_orders"]],
                 [114, 13_738_500, 4])
    checks.equal("A opened: all orders", queue["matching_orders"], 168)
    job = done_job(a)
    output = job["diagnosis"]["output"]
    checks.equal("A diagnosis", [output["hypothesis"], output["faulting_change_id"], output["proposed_operation"]],
                 ["merchant_worker_regression", "deploy_9b85880366", "reprocess_inbox"])
    checks.equal("A diagnosis packet", job["diagnosis"]["packet_hash"][:16], "8b47f5c942e67560")
    validation = transition_answer(a, "validate the corrected handler payload@5")
    checks.equal("A validation", [validation["passed"], validation["deployed_version"]], [True, "deploy_e1b540bacd"])
    first_plan = transition_answer(a, "build the reprocessing plan")
    checks.equal("A first plan (targets, paise, candidates)",
                 [first_plan["target_count"], first_plan["target_value_paise"], first_plan["candidate_orders"]],
                 [98, 11_959_200, 114])
    refreshed = transition_answer(a, "refresh the stale-only orders' provider state (read-only)")
    checks.equal("A refresh (payments, replay time)", [len(refreshed["refreshed"]), refreshed["now_ms"]],
                 [10, REPLAY_CLOCK_A + 3 * OPERATOR_STEP_MS])
    plan = transition_answer(a, "build the plan again")
    checks.equal("A second plan (targets, paise)", [plan["target_count"], plan["target_value_paise"]], [103, 12_502_700])
    checks.starts("A second plan hash", plan["plan_hash"], "9db6c8c60bee4995")
    approval = transition_answer(a, "approve the plan")
    checks.equal("A approval expires", approval["expires_at_ms"], 1_793_883_000_000)
    executed = transition_answer(a, "execute the approved plan")
    again = transition_answer(a, "execute the plan again (the replay)")
    checks.equal("A execute (status, replayed, fulfilled, restored, new capture)",
                 [executed["status"], executed["replayed"], len(executed["fulfilled_order_ids"]),
                  executed["restored_processing_value_paise"], executed["additional_captured_paise"]],
                 ["succeeded", False, 103, 12_502_700, 0])
    checks.equal("A execute again (replayed, same action)", [again["replayed"], again["action_id"]],
                 [True, executed["action_id"]])
    receipt_hash = json.loads(receipt.json_bytes)["receipt_hash"]
    checks.starts("A receipt hash", receipt_hash, "eb9d6f30f04d9ebc")
    final = stage_answer(a, 8, f"GET /api/runs/{a.run_id}/orders?limit=5000")
    complete = next(item["orders"] for item in final["totals"]["by_state"] if item["state"] == "complete")
    checks.equal("A after: replay time", final["as_of_ms"], REPLAY_CLOCK_A + 7 * OPERATOR_STEP_MS)
    checks.equal("A after: captured (orders, paise), awaiting (orders, paise), complete",
                 [final["totals"]["captured_unprocessed_orders"], final["totals"]["captured_unprocessed_value_paise"],
                  final["totals"]["unresolved_orders"], final["totals"]["unresolved_value_paise"], complete],
                 [0, 0, 11, 1_235_800, 148])
    checks.equal("A after: Complete sorted by Amount, first row", recovered_order, "order_SYNTHETIC_547a954d_0099")
    queue_b = stage_answer(b, 0, f"GET /api/runs/{b.run_id}/orders?limit=5000")
    by_state_b = {item["state"]: item["orders"] for item in queue_b["totals"]["by_state"] if item["orders"]}
    checks.equal("B opened: replay time", queue_b["as_of_ms"], REPLAY_CLOCK_A + 90 * OPERATOR_STEP_MS)
    checks.equal("B opened: captured and awaiting (orders, paise)",
                 [queue_b["totals"]["captured_unprocessed_orders"], queue_b["totals"]["captured_unprocessed_value_paise"],
                  queue_b["totals"]["unresolved_orders"], queue_b["totals"]["unresolved_value_paise"]], [0, 0, 0, 0])
    checks.equal("B opened: orders by state", [queue_b["matching_orders"], by_state_b],
                 [1200, {"complete": 1082, "payment_failed": 118}])
    output_b = done_job(b)["diagnosis"]["output"]
    checks.equal("B diagnosis", [output_b["hypothesis"], output_b["affected_method"], output_b["proposed_operation"]],
                 ["provider_method_degradation", "upi", "escalate"])
    escalation_b = transition_answer(b, "escalate with the evidence needed")
    checks.equal("B escalation (subjects, evidence needed)",
                 [escalation_b["subject_ids"], escalation_b["evidence_needed"]], [["upi"], ["compare_method_cohorts"]])
    output_c = done_job(c)["diagnosis"]["output"]
    checks.equal("C diagnosis", [output_c["hypothesis"], output_c["proposed_operation"]],
                 ["ambiguous_payment_state", "refresh_provider_state"])
    refreshed_c = transition_answer(c, "refresh provider state (read-only)")
    statuses_c: dict[str, int] = {}
    for status in refreshed_c["statuses"].values():
        statuses_c[status] = statuses_c.get(status, 0) + 1
    checks.equal("C refresh (payments, replay time, statuses)",
                 [len(refreshed_c["refreshed"]), refreshed_c["now_ms"], statuses_c],
                 [76, 1_793_880_060_000, {"authorized": 3, "captured": 10, "created": 33, "failed": 30}])
    for run, stage, wanted in (
        (a, 3, [5, 5, 1, 5, [f"order_SYNTHETIC_547a954d_{number}" for number in ("0093", "0099", "0128", "0150")]]),
        (b, 1, [10, 10, 10, 10, []]),
        (c, 2, [10, 8, 8, 10, ["order_SYNTHETIC_920abafa_0341", "order_SYNTHETIC_920abafa_0351"]]),
    ):
        late = stage_answer(run, stage, f"GET /api/runs/{run.run_id}/payment-state")["late_authorisations"]
        checks.equal(f"{run.flow.letter} late authorisations (payments, captured, fulfilled orders, orders, not fulfilled)",
                     [late["payments"], late["captured"], late["fulfilled_orders"], late["orders"],
                      [order["order_id"] for order in late["unfulfilled"]]], wanted)
    return checks


# --------------------------------------------------------------------------- the files the site serves


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")


def clock_of(run: RunTape, stage: int) -> int | None:
    answers = run.stages[stage].get(f"GET /api/runs/{run.run_id}")
    return None if not answers else json.loads(answers[-1].body)["replay_clock_ms"]


def write_site_data(tape: Tape, receipts: list[ReceiptFile], site: Path) -> dict:
    """demo-data/: one file per stage of each run (named by its content hash) and the manifest; receipts/: the files."""
    data_dir = site / "demo-data"
    data_dir.mkdir(parents=True)
    runs = []
    for run in tape.runs.values():
        stored: dict[str, list[tuple[int, list[Answer]]]] = {}
        stages = []
        for index, answers in enumerate(run.stages):
            entries: dict[str, dict] = {}
            for key in sorted(answers):
                same = next((stage for stage, earlier in stored.get(key, []) if earlier == answers[key]), None)
                if same is None:
                    entries[key] = {"answers": [answer.as_json() for answer in answers[key]]}
                    stored.setdefault(key, []).append((index, answers[key]))
                else:
                    entries[key] = {"same_as_stage": same}
            content = canonical({"format": FORMAT, "run_id": run.run_id, "stage": index, "answers": entries})
            name = f"{run.run_id}-{index}-{hashlib.sha256(content).hexdigest()[:12]}.json"
            (data_dir / name).write_bytes(content)
            stages.append({"name": run.stage_names[index], "clock_ms": clock_of(run, index), "file": f"demo-data/{name}",
                           "covered": index in run.covered})
        transitions = [{
            "label": item.label,
            "method": item.exchange.method,
            "path": item.exchange.path,
            "query": item.exchange.query,
            "request": None if item.exchange.request is None else json.loads(item.exchange.request),
            "answer": item.exchange.answer.as_json(),
        } for item in run.transitions]
        run_receipts = []
        for receipt in (item for item in receipts if item.run_id == run.run_id):
            folder = site / "receipts" / run.run_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{receipt.action_id}.json").write_bytes(receipt.json_bytes)
            (folder / f"{receipt.action_id}.md").write_bytes(receipt.markdown_bytes)
            run_receipts.append({
                "action_id": receipt.action_id, "from_stage": receipt.from_stage,
                "json": f"receipts/{run.run_id}/{receipt.action_id}.json", "json_type": receipt.json_type,
                "markdown": f"receipts/{run.run_id}/{receipt.action_id}.md", "markdown_type": receipt.markdown_type,
                "receipt_hash": json.loads(receipt.json_bytes)["receipt_hash"],
            })
        runs.append({
            "run_id": run.run_id, "flow": run.flow.letter, "title": run.flow.title, "guide": run.flow.guide,
            "scenario": run.flow.scenario, "seed": run.flow.seed, "stages": stages, "transitions": transitions,
            "receipts": run_receipts, "case_orders": run.case_orders,
        })
    start = tape.global_answers.get("start", {})
    health = json.loads(start["GET /health"][-1].body)
    manifest = {
        "format": FORMAT,
        "recorded_with": {key: health[key] for key in ("app", "version", "environment", "execution_mode", "versions")},
        "global": {key: [answer.as_json() for answer in answers] for key, answers in sorted(start.items())},
        "session": {name: answer.as_json() for name, answer in sorted(tape.session.items())},
        "runs": runs,
    }
    (data_dir / "manifest.json").write_bytes(canonical(manifest))
    return manifest


def scan_for_secrets(root: Path, secrets: dict[str, str]) -> list[str]:
    found = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        data = path.read_bytes()
        found += [f"{path.relative_to(root)} contains the {name}" for name, value in secrets.items()
                  if value and value.encode("utf-8") in data]
    return found


def site_sizes(site: Path) -> dict:
    files = {str(path.relative_to(site)): path.stat().st_size for path in site.rglob("*") if path.is_file()}
    return {"files": len(files), "bytes": sum(files.values()), "largest": max(files.items(), key=lambda item: item[1])}


# --------------------------------------------------------------------------- the session


def read_token(log_path: Path) -> str:
    for line in log_path.read_text(encoding="utf-8").splitlines():
        match = TOKEN_LINE.search(line)
        if match:
            return match.group(1)
    raise RecordingError(f"no operator token line in {log_path.name}: the API did not print one at start")


def record(token: str, out: Path) -> int:
    tape = Tape()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT, accept_downloads=True)
        context.set_default_timeout(EXPECT_TIMEOUT_MS)
        context.add_init_script(script=CAPTURE_SCRIPT)
        page = context.new_page()
        driver = Driver(page, context, tape)

        document = page.goto(f"{BASE_URL}/")
        headers = document.all_headers()
        security_headers = {name: headers.get(name) for name in SECURITY_HEADER_NAMES}
        driver.settle()
        driver.drain()
        driver.log_in(token)
        for flow in FLOWS:
            driver.create_run(flow)

        # The demo starts here: logged in, the three runs listed, none of them opened yet.
        tape.phase = "start"
        page.reload()
        expect(page.get_by_label(OPEN_RUN)).to_be_visible()
        driver.settle()
        driver.drain()
        listed = page.get_by_label(OPEN_RUN).locator("option").all_inner_texts()
        if len(listed) != 1 + len(FLOWS):
            raise RecordingError(f"Open run lists {listed}, expected the three runs")

        tape.phase = "flows"
        runs = {run.flow.letter: run for run in tape.runs.values()}
        a, b, c = runs["A"], runs["B"], runs["C"]
        driver.open_run(a)
        driver.cover_stage(a)
        driver.diagnose(a)
        driver.cover_stage(a)
        driver.validate_fix(a, "payload@5")
        driver.cover_stage(a)
        driver.build_plan(a)
        driver.cover_stage(a)
        driver.refresh_and_replan(a)  # the refresh's stage is passed through: the page plans again at once
        driver.cover_stage(a)
        driver.approve(a)
        driver.cover_stage(a)
        driver.execute(a, again=False)
        driver.cover_stage(a)
        driver.execute(a, again=True)
        driver.cover_stage(a)
        recovered_order, receipt = driver.recovered_order_receipt(a)

        driver.open_run(b)
        driver.cover_stage(b)
        driver.diagnose(b)
        driver.cover_stage(b)
        driver.escalate(b)
        driver.cover_stage(b)

        driver.open_run(c)
        driver.cover_stage(c)
        driver.diagnose(c)
        driver.cover_stage(c)
        driver.refresh_open_payments(c)
        driver.cover_stage(c)
        driver.escalate(c)
        driver.cover_stage(c)

        tape.phase = "session"
        driver.act(page.locator("header.topbar").get_by_role("button", name="Log out").click, "log out")
        driver.log_in(token)
        cookies = {f"{item['name']} cookie": item["value"] for item in context.cookies()}
        context.close()
        browser.close()

    checks = documented_checks(tape, recovered_order, receipt)
    site = out / "site"
    if site.exists():
        shutil.rmtree(site)
    manifest = write_site_data(tape, [receipt], site)
    summary = {
        "format": FORMAT,
        "security_headers": security_headers,
        "runs": [{
            "run_id": run["run_id"], "flow": run["flow"], "scenario": run["scenario"], "seed": run["seed"],
            "stages": [{"name": stage["name"], "clock_ms": stage["clock_ms"], "covered": stage["covered"]}
                       for stage in run["stages"]],
            "steps": [item["label"] for item in run["transitions"]],
            "case_orders": len(run["case_orders"]),
            "receipts": [{key: item[key] for key in ("action_id", "receipt_hash", "json_type", "markdown_type")}
                         for item in run["receipts"]],
        } for run in manifest["runs"]],
        "documented_values": checks.results,
        "site_data": site_sizes(site),
    }
    (out / "recording-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    leaks = scan_for_secrets(out, {"operator token": token, **cookies})
    if leaks:
        shutil.rmtree(site)
        (out / "recording-summary.json").unlink()
        print("[record] FAILED: secrets in the recording; it was deleted:\n  " + "\n  ".join(leaks), file=sys.stderr)
        return 3
    print(f"[record] scanned {sum(1 for item in out.rglob('*') if item.is_file())} files for the operator token "
          f"and {len(cookies)} cookie value(s): none found", flush=True)
    if checks.failed:
        print("[record] FAILED: recorded values differ from the docs:\n  "
              + "\n  ".join(json.dumps(item) for item in checks.failed), file=sys.stderr)
        return 4
    print(f"[record] {len(checks.results)} documented values match; site data {summary['site_data']['bytes']} bytes "
          f"in {summary['site_data']['files']} files", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the UI and API for the static recorded demo.")
    parser.add_argument("--server-log", type=Path, required=True, help="the API's log, for its operator token")
    parser.add_argument("--out", type=Path, required=True, help="where the site data and the summary go")
    args = parser.parse_args()
    try:
        return record(read_token(args.server_log), args.out)
    except RecordingError as problem:
        print(f"[record] FAILED: {problem}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
