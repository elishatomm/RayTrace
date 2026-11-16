#!/usr/bin/env python3
"""Agent U, phase 11: a headless walkthrough of the RayTrace one-page UI.

It runs inside rw-ui-walkthrough:u, a container with no network, after run.sh has:
  * seeded worker_backlog seed 101 with the CLI demo in rules mode (a diagnosis, a plan, an approval and an
    execution, so a receipt exists) and started server A on 127.0.0.1:8000 over that data;
  * started server B on 127.0.0.1:8001 over an empty data directory, where the UI itself creates the same
    world and walks Flow A, for the regression pass.

Each step saves screenshots to --out and records every check it made. ui_results.json holds them all, with
every console message, page error, failed request and HTTP error the browser saw. Nothing is planted: the
plain-text checks use the records exactly as the simulator wrote them.

Agent V, phase 11 follow-up: U's walkthrough, extended with checks for the fixes to U-1, U-2 and U-3. Every one
of U's checks is kept. Two of them asserted what the fixes change, and both carry "[V-adjusted" in their text:
  * step 8, "Enter on the row's button opens its case; focus stays on the button" recorded the U-1 defect. It now
    checks only that the case opens; [V] checks say where focus goes;
  * the case view's attribute allow-list (steps 5 and 7b) admits tabindex, but only as the heading's "-1".
Every new check starts with "[V]". Outputs are named v_* (U's were u_*); nothing else in U's flow changed.

Agent X, phase 11 follow-up X1: V's walkthrough, unchanged, followed by two new steps on server B. Step 9 presses
each control that used to disable itself while its own request ran (Create synthetic run, +1 min, +15 min, +1 h,
Diagnose the latest incident), holds that request in the browser, and checks focus, the busy state and that more
presses send nothing; then it releases the request and checks focus again. Step 9g measures, without changing
anything, what a second press does while the diagnosis start request itself is in flight. Every new check starts
with "[X]". Outputs are named x_*; every one of U's and V's checks runs exactly as before, ahead of the new steps.

Agent Q33, owner decision Q33: X's walkthrough, unchanged, followed by five new steps on server B in a browser
context of their own. They check the four approved fixes: Diagnose is busy during its own start request (fix 1); the
Recovery panel's buttons, Export report and Log out refuse a second press while their own request runs (fix 2); focus
moves to a chosen target when a control that held it goes away or is disabled after its request, and never moves when
the user had put it elsewhere (fix 3); the busy line is a persistent polite live region (fix 4). Checks starting
"[Q33]" assert a change, so each fails on ca1971e; checks starting "[Q33 guard]" assert what must not change, so they
pass on both. Outputs are named q33_*; every one of U's, V's and X's checks runs exactly as before, ahead of these.

Agent Q36 (the Q33 agent), owner decision Q36: the walkthrough is kept in the repository under scripts/ui-walkthrough/,
and run-walkthrough.sh builds and runs it; outputs are named ui_*. Three new steps on server B, in a browser context of
their own, check the Q36 changes: focus after Log out goes to the Operator token field and after a login to the
Scenario select (step 15); a running diagnosis is announced once, not on each poll (step 16); Approve's focus target is
the "Approved by" line (step 17). Checks starting "[Q36]" fail on the Q33 build; "[Q36 guard]" checks pass on both.
Two of Q33's checks carry "[Q36-adjusted": Q36 moves Approve's focus target, so they assert only what both targets share.

Agent UI-A, the owner's "guided console" redesign: Q36's walkthrough, with the checks the redesign moves and the checks
it adds. Checks whose locator the layout moved carry "[UI-A-adjusted" and assert the same values: the whole-queue totals
moved from the queue panel's div.facts to the money strip in the run header (steps 2, 3 and 7b), and the case view's
payments table sits in its own sideways-scrolling box (steps 5 and 7b). Every new check starts with "[UI-A]": the money
strip against the API's totals and the Execute answer, never 0 before an execution (steps 2 and 7b); the recovery
rail's state and proof after each real step of the UI-driven Flow A, against the API's answers (step 7b), and on the
escalation, refresh and replay paths (steps 12 and 13); and step 18, in a context of its own on server B: no sideways
page scroll at 1440, 1024 and 390 px with screenshots of each, the split view and its stacking, motion under
prefers-reduced-motion, and the rail on the needs-review and no_action paths, which no demo scenario's rules diagnosis
takes (rewritten in the browser, as step 13 does for the replay).

Agent UI-B, round 2 of the redesign: UI-A's walkthrough, with checks for five defects an independent review found.
Checks starting "[UI-B]" assert a fix, so each fails on the round-1 UI (b84b859); "[UI-B guard]" checks assert what
must not change, so they pass on both. Step 18's needs-review rail checks carry "[UI-B-adjusted": step 1 of a diagnosis
that needs review now reads "Needs review", not "Done". Step 19, in a context of its own on server B: an execution the
server rejects (plan and approval expired) reaches the rail, the Recovery card and the money strip while the error
banner stays as it was; validating or planning again after an execution keeps that execution's answer in the strip,
while the rail and the card describe the new plan only; a receipt read held until another run is open, then failed,
shows no banner there; a diagnosis poll held until another run is open, or until Log out, changes nothing.

Agent UI-C, round 3: the same class of race for two more answers. Checks starting "[UI-C]" fail on the round-2 UI
(4a1e878); "[UI-C guard]" checks pass on both. Step 20, in a context of its own on server B: a diagnosis start (POST
diagnoses) and a run reload (the run-summary read after +1 min) are each held in the browser until the operator has
opened another run, or has logged out; once released, the page stays on the run that was opened, showing nothing of
the other, and the login page shows no banner. The sweep's one chained write is checked too: a refresh-and-re-plan
whose refresh is answered after another run was opened sends no POST plans for the run that was left.

The model-chain agent, owner answer to Q46: UI-C's walkthrough, unchanged, followed by step 21 on server B in a browser
context of its own. Server B runs in rules mode, so the top bar must name that source, read from the authenticated
GET /api/scenarios and not before login; then, since this container runs no Ollama, a diagnosis asked of the local model
needs review, and the page must offer "Diagnose with the rules instead": a rules diagnosis of the same incident, with
focus handed to Diagnose when the action goes. Step 22, in another context of its own, answers /health in the browser
with {"status": "ok"} alone, as the server's public mode does: the page must still render, and show the model source
once logged in. Step 23 answers a seeding in the browser with an existing run and 200, as public mode does for a scenario
and seed that already have one: the run list must stay as it was, the run listed once. Checks starting "[MC]" fail on
the UI before the model chain; "[MC guard]" checks pass on both. Every earlier check runs exactly as before, ahead of
these.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from playwright.sync_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Locator,
    Page,
    Request,
    Response,
    Route,
    WebError,
    expect,
    sync_playwright,
)

OPERATOR_ID = "op_demo_operator"
SERVER_A = "http://127.0.0.1:8000"
SERVER_B = "http://127.0.0.1:8001"
TOKEN_LINE = re.compile(r"Operator token for this server session \(shown once\): (\S+)")
EXPECT_TIMEOUT_MS = 20_000
SLOW_TIMEOUT_MS = 180_000  # seeding a whole world through the UI
VIEWPORT = {"width": 1440, "height": 900}

# The UI's display vocabulary (frontend/src/format.ts, CaseDetail.tsx): what a cell must read for an API value.
CASE_STATE_LABELS = {
    "payment_order_mismatch": "Payment/order mismatch",
    "multiple_captures": "Multiple captures",
    "business_state_conflict": "Business-state conflict",
    "payment_state_unresolved": "Payment state unresolved",
    "capture_not_confirmed": "Capture not confirmed",
    "complete": "Complete",
    "captured_processing_closed": "Captured; processing closed",
    "notification_acknowledged_not_stored": "Notification acknowledged, not stored",
    "notification_not_accepted": "Notification not accepted",
    "captured_processing_incomplete": "Captured; processing incomplete",
    "payment_failed": "Payment failed",
    "insufficient_evidence": "Insufficient evidence",
}
NEXT_STEP_LABELS = {
    "manual_review": "Manual review",
    "refresh_then_reevaluate": "Refresh provider state, then re-evaluate",
    "hold": "Hold: no fulfilment until a capture is confirmed",
    "prepare_provider_replay": "Prepare provider replay instructions",
    "validate_fix_then_reprocess": "Validate the fix, then an approved reprocess plan",
    "none": "None",
}
AMOUNT_NOTES = {
    "conflicting": "the evidence disagrees: counted, not summed",
    "other_currency": "another currency: counted, not summed",
    "unknown": "no order record",
}
STAGE_LABELS = {"order": "Order", "provider": "Provider", "delivery": "Delivery", "inbox": "Inbox", "worker": "Worker"}
# api.TIMELINE_STAGES key order: how records known at one replay time are placed.
TIMELINE_KIND_ORDER = (
    "order_created", "payment_attempt", "provider_observation", "delivery_attempt", "inbox_event",
    "conflicting_delivery", "worker_attempt", "fulfilment", "order_cancelled",
)
QUEUE_COLUMNS = ["Order", "Amount", "Case state", "Age", "Evidence freshness", "Next step", "Blockers"]
# The brief's four worklists and the server query each must send.
MAIN_FILTERS: dict[str, dict[str, list[str]]] = {
    "captured_unprocessed": {"state": ["captured_processing_incomplete"]},
    "unresolved": {"state": ["payment_state_unresolved"]},
    "manual_review": {"next_step": ["manual_review"]},
    "completed": {"state": ["complete"]},
}
# Every element type CaseDetail.tsx and JsonText.tsx create; anything else would have come from somewhere else.
CASE_DETAIL_TAGS = frozenset({
    "h3", "h4", "button", "p", "dl", "dt", "dd", "strong", "ul", "ol", "li", "a", "table", "thead", "tbody", "tr",
    "th", "td", "span", "details", "summary", "pre", "div",
})
CASE_DETAIL_ATTRIBUTES = frozenset({"class", "type", "href", "target", "rel", "open"})
INTERACTIVE_ROLES = frozenset({
    "button", "link", "combobox", "textbox", "checkbox", "radio", "spinbutton", "listbox", "searchbox", "switch",
    "slider", "menuitem", "tab", "DisclosureTriangle",
})


# --------------------------------------------------------------------------- display formats (format.ts)


def js_round(value: float) -> int:
    """Math.round: halves round toward +infinity."""
    return math.floor(value + 0.5)


def to_fixed(value: float, digits: int) -> str:
    """Number.prototype.toFixed: the exact binary value, halves rounded up."""
    return str(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def duration(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    seconds = js_round(ms / 1000)
    if seconds < 120:
        return f"{seconds} s"
    minutes = js_round(seconds / 60)
    return f"{minutes} min" if minutes < 120 else f"{to_fixed(minutes / 60, 1)} h"


def rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    return f"{sign}INR {whole:,}.{fraction:02d}"


def parse_rupees(text: str) -> int | None:
    match = re.fullmatch(r"(-?)INR ([\d,]+)\.(\d{2})", text.strip())
    if match is None:
        return None
    paise = int(match.group(2).replace(",", "")) * 100 + int(match.group(3))
    return -paise if match.group(1) else paise


def replay_time(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms // 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S") + "Z"


def order_amount(paise: int | None, currency: str | None, status: str) -> str:
    if paise is None or currency is None:
        return "unknown (no order record)"
    shown = rupees(paise) if currency == "INR" else f"{currency} {paise} (minor units)"
    return shown if status == "asserted" else f"{shown} ({AMOUNT_NOTES[status]})"


def expected_cells(row: dict) -> list[str]:
    """The seven cells QueuePanel renders for one API row."""
    return [
        row["order_id"],
        order_amount(row["amount_paise"], row["currency"], row["amount_status"]),
        CASE_STATE_LABELS[row["state"]],
        duration(row["age_ms"]),
        "no observation" if row["freshness_ms"] is None else duration(row["freshness_ms"]),
        NEXT_STEP_LABELS[row["next_step"]],
        ", ".join(row["blockers"]) or "none",
    ]


# --------------------------------------------------------------------------- what the browser reported


@dataclass
class Recorder:
    """Every console message, page error, failed request and response, labelled with its context and step."""

    step: str = "setup"
    console: list[dict] = field(default_factory=list)
    page_errors: list[dict] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    http_errors: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)

    def attach(self, context: BrowserContext, label: str) -> None:
        context.on("console", lambda message: self._console(label, message))
        context.on("weberror", lambda problem: self._page_error(label, problem))
        context.on("requestfailed", lambda request: self._failed(label, request))
        context.on("response", lambda response: self._response(label, response))

    def _console(self, label: str, message: ConsoleMessage) -> None:
        location = message.location or {}
        self.console.append({
            "context": label, "step": self.step, "type": message.type, "text": message.text,
            "source": location.get("url"), "page": None if message.page is None else message.page.url,
        })

    def _page_error(self, label: str, problem: WebError) -> None:
        self.page_errors.append({
            "context": label, "step": self.step, "message": problem.error.message,
            "page": None if problem.page is None else problem.page.url,
        })

    def _failed(self, label: str, request: Request) -> None:
        self.failed_requests.append({
            "context": label, "step": self.step, "method": request.method, "url": request.url,
            "failure": request.failure,
        })

    def _response(self, label: str, response: Response) -> None:
        entry = {"context": label, "step": self.step, "method": response.request.method, "url": response.url,
                 "status": response.status}
        self.responses.append(entry)
        if response.status >= 400:
            self.http_errors.append(entry)


class CheckFailed(AssertionError):
    """A check the step cannot go on without."""


@dataclass
class StepRecord:
    number: str
    title: str
    status: str = "not run"
    checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    error: str | None = None

    def check(self, passed: bool, description: str) -> bool:
        self.checks.append(f"{'PASS' if passed else 'FAIL'}: {description}")
        return passed

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def failed_checks(self) -> list[str]:
        return [item for item in self.checks if item.startswith("FAIL")]


class Walkthrough:
    def __init__(self, out: Path, recorder: Recorder) -> None:
        self.out = out
        self.recorder = recorder
        self.steps: list[StepRecord] = []
        self.shared: dict[str, object] = {}

    def run(self, number: str, title: str, body: Callable[[StepRecord], None], page: Callable[[], Page | None],
            needs: tuple[str, ...] = ()) -> StepRecord:
        record = StepRecord(number, title)
        self.steps.append(record)
        self.recorder.step = number
        print(f"[step {number}] {title}", flush=True)
        missing = [name for name in needs if name not in self.shared]
        if missing:
            record.status = "blocked"
            record.error = f"blocked: needs {missing} from an earlier step that failed"
            print(f"[step {number}] {record.error}", flush=True)
            return record
        try:
            body(record)
        except Exception as problem:  # recorded with its traceback; the walkthrough goes on with the next step
            record.error = "".join(traceback.format_exception_only(problem)).strip()
            record.notes.append(traceback.format_exc(limit=10))
            self._failure_screenshot(record, page())
        record.status = "fail" if record.error or record.failed_checks else "pass"
        print(f"[step {number}] {record.status}: {len(record.checks)} checks, {len(record.failed_checks)} failed"
              + (f"; error: {record.error}" if record.error else ""), flush=True)
        for failed in record.failed_checks:
            print(f"    {failed}", flush=True)
        return record

    def _failure_screenshot(self, record: StepRecord, page: Page | None) -> None:
        if page is None or page.is_closed():
            return
        name = f"ui_fail_step{record.number}.png"
        try:
            page.screenshot(path=str(self.out / name), full_page=True)
            record.screenshots.append(name)
        except Exception as problem:  # the step has already failed; say why there is no picture of it
            record.note(f"no failure screenshot: {problem}")

    def shot(self, record: StepRecord, target: Page | Locator, name: str, *, full_page: bool = False) -> None:
        path = str(self.out / name)
        if isinstance(target, Page):
            target.screenshot(path=path, full_page=full_page)
        else:
            target.screenshot(path=path)
        record.screenshots.append(name)


# --------------------------------------------------------------------------- the API, read with the page's own session

FETCH_JS = """async ([path, accept]) => {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: accept } });
  return { status: response.status, contentType: response.headers.get("content-type"), text: await response.text() };
}"""


def fetch_in_page(page: Page, path: str, accept: str = "application/json") -> dict:
    return page.evaluate(FETCH_JS, [path, accept])


def get_json(page: Page, path: str):
    answer = fetch_in_page(page, path)
    if answer["status"] != 200:
        raise CheckFailed(f"GET {path} with the page's session answered {answer['status']}: {answer['text'][:300]}")
    return json.loads(answer["text"])


def run_path(run_id: str) -> str:
    return f"/api/runs/{quote(run_id, safe='')}"


def query_params(query: dict[str, list[str]]) -> list[tuple[str, str]]:
    """The parameters api.ts sends for a filter: the largest page, then each state and next step."""
    return [("limit", "5000"), *(("state", item) for item in query.get("state", [])),
            *(("next_step", item) for item in query.get("next_step", []))]


def queue_path(run_id: str, query: dict[str, list[str]]) -> str:
    return f"{run_path(run_id)}/orders?{urlencode(query_params(query))}"


def case_path(run_id: str, order_id: str, as_of_ms: int) -> str:
    return f"{run_path(run_id)}/orders/{quote(order_id, safe='')}?as_of_ms={as_of_ms}"


def receipt_path(run_id: str, action_id: str, *, markdown: bool = False) -> str:
    path = f"{run_path(run_id)}/actions/{quote(action_id, safe='')}/receipt"
    return f"{path}?format=markdown" if markdown else path


def describe_query(query: dict[str, list[str]]) -> str:
    return urlencode([pair for pair in query_params(query) if pair[0] != "limit"]) or "no filter"


def option_query(key: str, states: list[dict]) -> dict[str, list[str]]:
    """What each Show option means, from the brief and Agent F's report (not from the UI code)."""
    if key == "all":
        return {}
    if key in MAIN_FILTERS:
        return MAIN_FILTERS[key]
    if key == "every_unresolved":
        return {"state": [rule["state"] for rule in states if rule["unresolved"]]}
    if key.startswith("state:"):
        return {"state": [key.removeprefix("state:")]}
    raise CheckFailed(f"unexpected Show option {key!r}")


# --------------------------------------------------------------------------- the queue panel

ROWS_JS = """(panel) => [...panel.querySelectorAll(":scope > div.scroll > table > tbody > tr")]
  .map((row) => [...row.children].map((cell) => cell.textContent))"""
HEADERS_JS = """(panel) => [...panel.querySelectorAll(":scope > div.scroll > table > thead th")].map((cell) => cell.textContent)"""
SORT_JS = """([rows, key, descending]) => {
  const value = (row) => (key === "age" ? row.age_ms : row.amount_paise);
  const direction = descending ? -1 : 1;
  const compare = (left, right) => {
    if (left === null && right === null) return 0;
    if (left === null) return 1;
    if (right === null) return -1;
    return (left - right) * direction;
  };
  return [...rows].sort((left, right) => compare(value(left), value(right)) || left.order_id.localeCompare(right.order_id))
    .map((row) => row.order_id);
}"""


@dataclass
class RunView:
    run_id: str
    as_of_ms: int
    whole: dict  # the API's unfiltered queue at as_of_ms


def queue_panel(page: Page) -> Locator:
    return page.locator("main section.panel.queue")


def show_select(panel: Locator) -> Locator:
    return panel.locator(":scope > div.row select")


def queue_rows(panel: Locator) -> list[list[str]]:
    return panel.evaluate(ROWS_JS)


def sort_header(panel: Locator, label: str) -> Locator:
    return panel.locator(":scope > div.scroll > table > thead th button", has_text=label)


def wait_for_row_ids(page: Page, panel: Locator, expected_ids: list[str], what: str) -> list[list[str]]:
    deadline = time.monotonic() + EXPECT_TIMEOUT_MS / 1000
    while True:
        rows = queue_rows(panel)
        shown = [row[0] for row in rows]
        if shown == expected_ids:
            return rows
        if time.monotonic() > deadline:
            raise CheckFailed(f"{what}: the table never showed the expected {len(expected_ids)} rows "
                              f"(first {expected_ids[:3]}); it shows {len(shown)} (first {shown[:3]})")
        page.wait_for_timeout(100)


def row_mismatches(rows: list[list[str]], api_rows: list[dict]) -> list[str]:
    problems = []
    for shown, api_row in zip(rows, api_rows):
        wanted = expected_cells(api_row)
        if shown != wanted:
            problems.append(f"{api_row['order_id']}: shows {shown}, API row formats as {wanted}")
    if len(rows) != len(api_rows):
        problems.append(f"{len(rows)} rows shown, {len(api_rows)} in the API")
    return problems


def no_error_banner(record: StepRecord, page: Page, where: str) -> None:
    banners = page.locator("p.banner.error").all_inner_texts()
    record.check(not banners, f"{where}: no error banner (role=alert) is shown{'; it says ' + repr(banners) if banners else ''}")


# --------------------------------------------------------------------------- [UI-A] the run header: money strip and rail

# The rail's six steps and five states (frontend/src/recovery.ts), and the money strip's four figures (RunHeader.tsx).
RAIL_STEPS = ("1 Diagnose", "2 Validate fix", "3 Plan", "4 Approve", "5 Execute", "6 Receipt")
RAIL_STATES = frozenset({"Done", "Current", "Not yet", "Not applicable", "Failed", "Needs review"})
NOT_APPLICABLE = ["Not applicable"] * 5


def rail_states(*first: str) -> list[str]:
    """The rail's six states: the given first ones, then "Not yet" for the rest."""
    return [*first, *["Not yet"] * (len(RAIL_STEPS) - len(first))]
CAPTURED_FIGURE = "Captured, processing incomplete"
AWAITING_FIGURE = "Still awaiting an action or a hold"
RESTORED_FIGURE = "Restored processing value"
NEW_CAPTURE_FIGURE = "New capture"
NOT_EXECUTED = "nothing executed in this recovery yet"
MONEY_JS = """() => {
  const strip = document.querySelector("main .run-header .money-strip");
  if (strip === null) return null;
  const figures = {};
  const classes = {};
  for (const figure of strip.querySelectorAll(":scope > dl.figures > .figure")) {
    const label = figure.querySelector(":scope > dt").textContent;
    figures[label] = {
      value: figure.querySelector(":scope > dd.figure-value")?.textContent ?? null,
      detail: figure.querySelector(":scope > dd.figure-detail")?.textContent ?? null,
    };
    classes[label] = figure.className;
  }
  return { figures, classes, meta: strip.querySelector(".card-meta")?.textContent ?? null };
}"""
RAIL_JS = """() => {
  const rail = document.querySelector("main .run-header .rail");
  if (rail === null) return null;
  const facts = {};
  for (const fact of rail.querySelectorAll(":scope > dl.rail-facts > div")) {
    facts[fact.querySelector(":scope > dt").textContent] = fact.querySelector(":scope > dd").textContent;
  }
  const steps = [...rail.querySelectorAll(":scope > ol.rail-steps > li")].map((step) => {
    const state = step.querySelector(":scope > .step-state");
    const link = step.querySelector(":scope > .step-proof > a");
    return {
      name: step.querySelector(":scope > .step-name")?.textContent ?? null,
      className: step.className,
      state: state?.textContent ?? null,
      icon: state !== null && [...state.children].some((child) => child.tagName.toLowerCase() === "svg"
        && child.getAttribute("aria-hidden") === "true"),
      current: step.getAttribute("aria-current"),
      proof: [...step.querySelectorAll(":scope > .step-proof > .proof-line")].map((line) => line.textContent),
      link: link === null ? null : { href: link.getAttribute("href"), text: link.textContent, target: link.getAttribute("target"),
                                     rel: link.getAttribute("rel") },
    };
  });
  return { facts, steps };
}"""


def count_of(count: int, noun: str, plural: str | None = None) -> str:
    """format.ts countOf: "1 order", "2 orders"."""
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


def hash_proof(label: str, digest: str) -> str:
    """How the rail shows a hash: its label, then the first 12 hex digits and an ellipsis."""
    return f"{label} {digest.removeprefix('sha256:')[:12]}\u2026"


def money_strip(page: Page) -> dict:
    strip = page.evaluate(MONEY_JS)
    if strip is None:
        raise CheckFailed("the run header has no money strip")
    return strip


def captured_detail(totals: dict) -> str:
    """RunHeader's words: a zero says what it means."""
    count = totals["captured_unprocessed_orders"]
    return "0 orders: no captured payment is waiting on our processing" if count == 0 else count_of(count, "order")


def awaiting_detail(totals: dict) -> str:
    unasserted = totals["unresolved_unasserted_orders"]
    if totals["unresolved_orders"] == 0:
        return "0 orders: nothing is waiting"
    return count_of(totals["unresolved_orders"], "order") + (
        f"; {unasserted} of them are counted but not summed" if unasserted > 0 else "")


def check_queue_figures(record: StepRecord, page: Page, totals: dict, label: str, note: str = "") -> dict:
    """The money strip's two queue figures against the API's whole-queue totals."""
    figures = money_strip(page)["figures"]
    captured, awaiting = figures.get(CAPTURED_FIGURE, {}), figures.get(AWAITING_FIGURE, {})
    wanted_captured = {"value": rupees(totals["captured_unprocessed_value_paise"]),
                       "detail": captured_detail(totals)}
    wanted_awaiting = {"value": rupees(totals["unresolved_value_paise"]), "detail": awaiting_detail(totals)}
    record.check(captured == wanted_captured, f"{label}: totals show {CAPTURED_FIGURE} {captured} (API {wanted_captured})"
                 + (f" {note}" if note else ""))
    record.check(awaiting == wanted_awaiting, f"{label}: totals show {AWAITING_FIGURE} {awaiting} (API {wanted_awaiting})"
                 + (f" {note}" if note else ""))
    return figures


TOTALS_MOVED = ("[UI-A-adjusted: the whole-queue totals moved from the queue panel's div.facts to the money strip in "
                "the run header; the same API values, shown as label, value and order count]")


def check_not_yet_known(record: StepRecord, page: Page, where: str) -> None:
    """[UI-A] Before an execution, restored value and new capture read as not yet known, never as 0."""
    figures = money_strip(page)["figures"]
    shown = {name: figures.get(name) for name in (RESTORED_FIGURE, NEW_CAPTURE_FIGURE)}
    record.check(all(item == {"value": "Not yet known", "detail": NOT_EXECUTED} for item in shown.values()),
                 f"[UI-A] {where}: restored value and new capture read 'Not yet known', never INR 0.00: {shown}")


def read_rail(page: Page) -> dict:
    rail = page.evaluate(RAIL_JS)
    if rail is None:
        raise CheckFailed("the run header has no recovery rail")
    return rail


def check_rail(record: StepRecord, page: Page, where: str, states: list[str], *, path: str | None = None,
               next_step: str | None = None, proofs: dict[int, list[str]] | None = None, tag: str = "[UI-A]",
               structure_tag: str | None = None, note: str = "") -> dict:
    """[UI-A] The rail's six steps: each state, as text beside a hidden icon; aria-current on the current step; and,
    where given, the path, the next safe step and the proof lines of some steps (index 0 is Diagnose). `tag` starts
    every check but the words-and-icon one, which `structure_tag` starts when given (a fix's checks keep that one as a
    guard: it holds on any build); `note` (an "[UI-B-adjusted: ...]" mark) ends the two checks whose expectation it
    changed."""
    rail = read_rail(page)
    steps = rail["steps"]
    shown = [step["state"] for step in steps]
    adjusted = f" {note}" if note else ""
    record.check([step["name"] for step in steps] == list(RAIL_STEPS) and shown == states,
                 f"{tag} {where}: the rail's six steps read {shown}{adjusted}")
    record.check(len(steps) == 6 and all(step["state"] in RAIL_STATES and step["icon"] for step in steps),
                 f"{structure_tag or tag} {where}: each step says its state in words, beside an icon hidden from "
                 f"assistive technology{adjusted}")
    current = [step["name"] for step in steps if step["current"] == "step"]
    wanted_current = [name for name, state in zip(RAIL_STEPS, states) if state == "Current"]
    record.check(current == wanted_current, f"{tag} {where}: aria-current=step marks {current}")
    if path is not None:
        record.check(rail["facts"].get("Path") == path, f"{tag} {where}: the path reads {rail['facts'].get('Path')!r}")
    if next_step is not None:
        record.check(rail["facts"].get("Next safe step") == next_step,
                     f"{tag} {where}: the next safe step reads {rail['facts'].get('Next safe step')!r}")
    for index, proof in (proofs or {}).items():
        record.check(steps[index]["proof"] == proof,
                     f"{tag} {where}: {RAIL_STEPS[index]}'s proof is {steps[index]['proof']} (API {proof})")
    return rail


def job_of_page(page: Page, run_id: str) -> dict:
    """The diagnosis job the page shows, read back from GET /diagnoses/{job_id} with the page's session."""
    line = page.locator("section.panel.diagnosis p.job-line").inner_text()
    match = re.search(r"Job (job_[0-9a-f]+) ", line)
    if match is None:
        raise CheckFailed(f"the diagnosis panel has no job line: {line!r}")
    return get_json(page, f"{run_path(run_id)}/diagnoses/{match.group(1)}")


def diagnose_proof(job: dict) -> list[str]:
    """The Diagnose step's proof for a done job: the mode's label and the packet hash."""
    labels = {"local": "Local model", "recorded": "Recorded run", "rules": "Rules baseline", "hosted": "Hosted model"}
    return [labels[job["diagnosis"]["mode"]], hash_proof("packet", job["diagnosis"]["packet_hash"])]


def plan_proof(plan_id: str, targets: int, plan_hash: str) -> list[str]:
    return [plan_id, count_of(targets, "target order"), hash_proof("hash", plan_hash)]


# --------------------------------------------------------------------------- the case view

CASE_DOM_JS = """(detail) => {
  const fields = {};
  for (const term of detail.querySelectorAll(":scope > dl > dt")) {
    fields[term.textContent] = term.nextElementSibling ? term.nextElementSibling.textContent : null;
  }
  const timeline = [...detail.querySelectorAll(":scope > ol.timeline > li")].map((item) => ({
    clock: item.querySelector(":scope > span.clock")?.textContent ?? null,
    line: [...item.childNodes].filter((node) => node.nodeName !== "DETAILS").map((node) => node.textContent).join(""),
    summary: item.querySelector(":scope > details > summary")?.textContent ?? null,
    json: item.querySelector(":scope > details > pre.json")?.textContent ?? null,
    untrusted: item.querySelector(":scope > details > div.untrusted > pre")?.textContent ?? null,
    untrustedLabel: item.querySelector(":scope > details > div.untrusted > div.untrusted-label")?.textContent ?? null,
  }));
  const tags = {};
  const attributes = new Set();
  for (const element of detail.querySelectorAll("*")) {
    const tag = element.tagName.toLowerCase();
    tags[tag] = (tags[tag] || 0) + 1;
    for (const attribute of element.attributes) attributes.add(attribute.name);
  }
  const links = [...detail.querySelectorAll("a")].map((link) => ({
    href: link.getAttribute("href"), text: link.textContent, target: link.getAttribute("target"), rel: link.getAttribute("rel"),
  }));
  const tabindexed = [...detail.querySelectorAll("[tabindex]")].map((element) => [
    element.tagName.toLowerCase(), element.getAttribute("tabindex"), element.parentElement === detail,
  ]);
  return {
    fields, timeline, tags, attributes: [...attributes].sort(), links, tabindexed,
    receiptItems: [...detail.querySelectorAll(":scope > ul.compact > li")].map((item) => item.textContent),
    paymentRows: detail.querySelectorAll(":scope > div.table-scroll > table > tbody > tr").length,
    pre: detail.querySelectorAll("pre").length,
    unknownInPage: [...document.querySelectorAll("*")].filter((element) => element instanceof HTMLUnknownElement)
      .map((element) => element.tagName),
  };
}"""
EXPECTED_TEXT_JS = """async (path) => {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
  const detail = await response.json();
  return detail.timeline.map((entry) => ({ json: JSON.stringify(entry.facts, null, 2), text: entry.text }));
}"""


def open_case(panel: Locator, order_id: str) -> Locator:
    panel.locator(":scope > div.scroll").get_by_role("button", name=order_id, exact=True).click()
    detail = panel.locator(":scope > div.case-detail")
    expect(detail.locator(":scope > h3")).to_contain_text(f"Case {order_id}")
    expect(detail).to_contain_text("As of replay time")
    return detail


def close_case(panel: Locator) -> None:
    detail = panel.locator(":scope > div.case-detail")
    if detail.count():
        detail.get_by_role("button", name="Close").click()
        expect(detail).to_have_count(0)


def expand_timeline(detail: Locator) -> int:
    """Open every record's details the way a person does, by clicking its summary; returns how many stayed shut."""
    for summary in detail.locator(":scope > ol.timeline > li > details > summary").all():
        summary.click()
    return detail.locator(":scope > ol.timeline > li > details:not([open])").count()


def collapse(labels: list[str]) -> list[str]:
    collapsed: list[str] = []
    for label in labels:
        if not collapsed or collapsed[-1] != label:
            collapsed.append(label)
    return collapsed


def verify_case(record: StepRecord, page: Page, run_id: str, as_of_ms: int, order_id: str, detail: Locator,
                label: str) -> dict:
    """The case view against GET /orders/{order}: fields, timeline order, and text that stays text."""
    path = case_path(run_id, order_id, as_of_ms)
    doc = get_json(page, path)
    dom = detail.evaluate(CASE_DOM_JS)
    wanted_text = page.evaluate(EXPECTED_TEXT_JS, path)
    case, timeline, fields = doc["case"], doc["timeline"], dom["fields"]

    record.check(fields.get("Amount") == order_amount(case["amount_paise"], case["currency"], case["amount_status"]),
                 f"{label}: Amount reads {fields.get('Amount')!r}")
    record.check(fields.get("Case state") == f"{CASE_STATE_LABELS[case['state']]}: {doc['state_condition']}",
                 f"{label}: Case state reads {fields.get('Case state')!r}")
    record.check(fields.get("Next step") == NEXT_STEP_LABELS[case["next_step"]],
                 f"{label}: Next step reads {fields.get('Next step')!r} (API {case['next_step']})")
    record.check(fields.get("Blockers (every one)") == (", ".join(case["blockers"]) or "none"),
                 f"{label}: Blockers read {fields.get('Blockers (every one)')!r} (API {case['blockers']})")
    record.check(fields.get("Age") == duration(doc["summary"]["age_ms"]), f"{label}: Age reads {fields.get('Age')!r}")
    record.check(dom["paymentRows"] == len(case["payments"]),
                 f"{label}: the payments table has {dom['paymentRows']} rows for {len(case['payments'])} payments "
                 "[UI-A-adjusted: the table now sits in its own sideways-scrolling box, div.table-scroll, so a narrow "
                 "case view never widens the page; the count is the same]")

    shown = dom["timeline"]
    record.check([item["summary"] for item in shown] == [f"{entry['kind']} record {entry['record_id']}" for entry in timeline],
                 f"{label}: the page lists the API's {len(timeline)} timeline records, in the API's order")
    record.check([item["clock"] for item in shown] == [replay_time(entry["at_ms"]) for entry in timeline],
                 f"{label}: each timeline line starts with its record's replay time")
    record.check(all(item["line"].startswith(f"{replay_time(entry['at_ms'])} {STAGE_LABELS[entry['stage']]}: ")
                     for item, entry in zip(shown, timeline)), f"{label}: each line names its stage")
    keys = [(entry["at_ms"], TIMELINE_KIND_ORDER.index(entry["kind"])) for entry in timeline]
    record.check(keys == sorted(keys), f"{label}: ordered by replay time; at one time order, provider, delivery, "
                 "inbox, worker, then the order's outcome")
    record.check(all(entry["at_ms"] <= as_of_ms for entry in timeline), f"{label}: no record after the queue's time")
    stages = collapse([STAGE_LABELS[entry["stage"]] for entry in timeline])
    record.note(f"{label}: stage sequence {' > '.join(stages)}; kinds {dict(Counter(entry['kind'] for entry in timeline))}")

    traces = sum(wanted["text"] is not None for wanted in wanted_text)
    record.check(len(shown) == len(wanted_text) and all(item["json"] == wanted["json"] for item, wanted in zip(shown, wanted_text)),
                 f"{label}: every record's facts render as exactly the text of JSON.stringify(facts, null, 2) "
                 f"({len(shown)} records)")
    record.check(all(item["untrusted"] == wanted["text"] for item, wanted in zip(shown, wanted_text))
                 and all((item["untrustedLabel"] is not None) == (wanted["text"] is not None)
                         for item, wanted in zip(shown, wanted_text)),
                 f"{label}: each stack trace renders as exactly the API's text under the untrusted-text label ({traces} traces)")
    markup = Counter(character for wanted in wanted_text for value in (wanted["json"], wanted["text"] or "")
                     for character in value if character in "<>&\"'")
    record.note(f"{label}: markup-significant characters in the compared record text: {dict(markup) or 'none'}")
    tags = set(dom["tags"])
    record.check(tags <= CASE_DETAIL_TAGS, f"{label}: the case view holds only the component's own element types "
                 f"{sorted(tags)}{'; unexpected ' + str(sorted(tags - CASE_DETAIL_TAGS)) if tags - CASE_DETAIL_TAGS else ''}")
    # U-1's fix gives the case heading tabindex="-1"; any other tabindex would still fail, as any other attribute does.
    heading_tabindex_only = all(item == ["h3", "-1", True] for item in dom["tabindexed"])
    record.check(set(dom["attributes"]) <= CASE_DETAIL_ATTRIBUTES | {"tabindex"} and heading_tabindex_only,
                 f"{label}: attributes in the case view {dom['attributes']} [V-adjusted: tabindex is allowed only as the "
                 f"heading's -1; tabindex found on {dom['tabindexed']}]")
    record.check(dom["pre"] == len(timeline) + traces,
                 f"{label}: {dom['pre']} <pre> blocks = {len(timeline)} records + {traces} traces")
    record.check(not dom["unknownInPage"], f"{label}: no unknown element anywhere in the page {dom['unknownInPage']}")
    wanted_links = [
        link
        for receipt in doc["receipts"]
        for link in (
            {"href": receipt_path(run_id, receipt["action_id"]), "text": "Receipt (JSON)", "target": "_blank",
             "rel": "noopener noreferrer"},
            {"href": receipt_path(run_id, receipt["action_id"], markdown=True), "text": "Receipt (Markdown)",
             "target": "_blank", "rel": "noopener noreferrer"},
        )
    ]
    record.check(dom["links"] == wanted_links, f"{label}: the only links are the receipt links built from the run and "
                 f"action IDs ({len(dom['links'])} links for {len(doc['receipts'])} receipts)")
    for receipt, item in zip(doc["receipts"], dom["receiptItems"]):
        sentence = (f"Action {receipt['action_id']} of plan {receipt['plan_id']}: {receipt['status']} at "
                    f"{replay_time(receipt['executed_at_ms'])}; result for this order: {receipt['order_result'] or 'not recorded'}")
        record.check(item.startswith(sentence), f"{label}: the receipt line reads {item[:160]!r}")
    if not doc["receipts"]:
        record.check(dom["receiptItems"] == [] and "there is no receipt" in detail.inner_text(),
                     f"{label}: no receipt, and the view says so")
    return doc


# --------------------------------------------------------------------------- the receipt links


def receipt_tab(context: BrowserContext, link: Locator, *, keyboard_page: Page | None = None) -> tuple[Page, Response | None]:
    """Open a receipt link as a person does (click, or Enter when keyboard_page is given); the tab and its response."""
    navigations: list[Response] = []

    def remember(response: Response) -> None:
        if "/receipt" in response.url and response.request.is_navigation_request():
            navigations.append(response)

    context.on("response", remember)
    try:
        with context.expect_page() as opened:
            if keyboard_page is None:
                link.click()
            else:
                link.focus()
                keyboard_page.keyboard.press("Enter")
        tab = opened.value
        tab.wait_for_load_state("load")
    finally:
        context.remove_listener("response", remember)
    response = next((item for item in navigations if item.url == tab.url), None)
    return tab, response


def tab_text(tab: Page) -> str:
    pre = tab.locator("pre")
    return pre.first.inner_text() if pre.count() else tab.locator("body").inner_text()


def verify_receipt(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, run_id: str,
                   order_id: str, detail: Locator, doc: dict, *, expected_restored_paise: int | None,
                   shot_prefix: str) -> dict:
    """Both receipt links of the case's first receipt open, and say what GET .../receipt says."""
    receipts = doc["receipts"]
    if not record.check(len(receipts) >= 1, f"{order_id} lists {len(receipts)} receipt(s)"):
        raise CheckFailed(f"{order_id} has no receipt to open")
    receipt = receipts[0]
    record.check(receipt["order_result"] == "completed",
                 f"the case says the recovery's result for {order_id} is {receipt['order_result']!r}")

    json_link = detail.get_by_role("link", name="Receipt (JSON)").first
    href = json_link.get_attribute("href")
    record.check(href == receipt_path(run_id, receipt["action_id"]) == receipt["receipt_path"],
                 f"the JSON link's href {href!r} is built from the run and action IDs, and is the server's receipt_path")
    tab, response = receipt_tab(context, json_link)
    try:
        api = fetch_in_page(page, href)
        record.check(response is not None and response.status == 200,
                     f"the tab opened {tab.url} and its navigation answered {None if response is None else response.status}")
        record.check(api["status"] == 200, f"GET {href} with the page's session answers {api['status']}")
        content_type = "" if response is None else response.headers.get("content-type", "")
        record.check(content_type.startswith("application/json"), f"the tab's response is {content_type!r}")
        body = "" if response is None else response.body().decode("utf-8")
        record.check(body == api["text"], f"the tab received exactly the bytes GET receipt answers ({len(body)} bytes)")
        shown = tab_text(tab)
        receipt_doc = json.loads(api["text"])
        record.check(json.loads(shown) == receipt_doc, "what the tab shows parses to the same receipt")
        identity, value = receipt_doc["identity"], receipt_doc["value"]
        record.check(identity["action_id"] == receipt["action_id"] and identity["plan_id"] == receipt["plan_id"],
                     f"the receipt names action {identity['action_id']} and plan {identity['plan_id']}, as the case does")
        order_entry = next((item for item in receipt_doc["execution"].get("orders", []) if item["order_id"] == order_id), None)
        record.check(order_entry is not None and order_entry["result"] == receipt["order_result"],
                     f"the receipt's per-order result for {order_id} is {None if order_entry is None else order_entry['result']!r}, "
                     f"as the case says ({receipt['order_result']!r})")
        restored = value["restored_processing_value_paise"]
        record.note(f"receipt {receipt['action_id']}: restored processing value {rupees(restored)}, additional capture "
                    f"{rupees(value['additional_captured_paise'])}, per-order results {receipt_doc['execution'].get('results')}, "
                    f"hash {receipt_doc['receipt_hash'][:16]}")
        if expected_restored_paise is not None:
            record.check(restored == expected_restored_paise,
                         f"the restored value {rupees(restored)} is the execution's {rupees(expected_restored_paise)}")
        record.check(value["additional_captured_paise"] == 0, "the receipt records no new capture")
        record.note(f"receipt tab headers: cache-control {None if response is None else response.headers.get('cache-control')!r}")
        walk.shot(record, tab, f"{shot_prefix}a_receipt_json_tab.png")
    finally:
        tab.close()

    markdown_link = detail.get_by_role("link", name="Receipt (Markdown)").first
    markdown_href = markdown_link.get_attribute("href")
    record.check(markdown_href == receipt_path(run_id, receipt["action_id"], markdown=True),
                 f"the Markdown link's href is {markdown_href!r}")
    tab, response = receipt_tab(context, markdown_link)
    try:
        api = fetch_in_page(page, markdown_href, accept="text/markdown")
        content_type = "" if response is None else response.headers.get("content-type", "")
        record.check(response is not None and response.status == 200 and content_type.startswith("text/markdown"),
                     f"the Markdown tab answered {None if response is None else response.status} with {content_type!r}")
        body = "" if response is None else response.body().decode("utf-8")
        record.check(body == api["text"], f"the Markdown tab received exactly the bytes GET receipt?format=markdown "
                     f"answers ({len(body)} bytes)")
        shown = tab_text(tab)
        record.check(shown.strip() == api["text"].strip(), "the Markdown tab shows that text, as text")
        walk.shot(record, tab, f"{shot_prefix}b_receipt_markdown_tab.png")
    finally:
        tab.close()
    return receipt


# --------------------------------------------------------------------------- accessibility

ACTIVE_JS = """() => {
  const element = document.activeElement;
  if (!element || element === document.body) return { tag: "body", text: "" };
  const style = getComputedStyle(element);
  return {
    tag: element.tagName.toLowerCase(), text: (element.textContent || "").trim().slice(0, 80),
    inQueueHead: !!element.closest("section.panel.queue thead"),
    inQueueBody: !!element.closest("section.panel.queue div.scroll tbody"),
    inCase: !!element.closest("section.panel.queue div.case-detail"),
    focusVisible: element.matches(":focus-visible"), outline: `${style.outlineStyle} ${style.outlineWidth}`,
    isCaseHeading: element.matches("section.panel.queue > div.case-detail > h3"),
    isQueueTable: element.matches("section.panel.queue > div.scroll > table"),
    tabindexAttribute: element.getAttribute("tabindex"),
  };
}"""


def computed_names(context: BrowserContext, page: Page, selectors: dict[str, str]) -> dict[str, dict]:
    """Chromium's own role and accessible name for the first element each selector finds."""
    session = context.new_cdp_session(page)
    try:
        root = session.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        names: dict[str, dict] = {}
        for label, selector in selectors.items():
            node_id = session.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
            if not node_id:
                names[label] = {"role": None, "name": None, "selector": selector}
                continue
            node = session.send("Accessibility.getPartialAXTree", {"nodeId": node_id, "fetchRelatives": False})["nodes"][0]
            names[label] = {"role": (node.get("role") or {}).get("value"), "name": (node.get("name") or {}).get("value", ""),
                            "selector": selector}
        return names
    finally:
        session.detach()


def interactive_controls(context: BrowserContext, page: Page) -> list[dict]:
    session = context.new_cdp_session(page)
    try:
        tree = session.send("Accessibility.getFullAXTree")
    finally:
        session.detach()
    controls = []
    for node in tree["nodes"]:
        role = (node.get("role") or {}).get("value")
        if node.get("ignored") or role not in INTERACTIVE_ROLES:
            continue
        controls.append({"role": role, "name": (node.get("name") or {}).get("value", "")})
    return controls


# --------------------------------------------------------------------------- the CLI demo's own account


@dataclass(frozen=True)
class DemoFacts:
    run_id: str | None
    passing_fix_id: str | None
    plans: tuple[tuple[int, str], ...]  # (target orders, value) in the order the demo printed them
    action_id: str | None
    fulfilled: int | None
    restored_value: str | None
    new_capture: str | None


def read_demo_facts(text: str) -> DemoFacts:
    title, passing_fix = "", None
    for line in text.splitlines():
        heading = re.match(r"^\[\d+\] (.*)$", line)
        if heading:
            title = heading.group(1)
        elif title.startswith("validate-fix ") and "passed: True" in line and passing_fix is None:
            passing_fix = title.split()[1]
    run = re.search(r"^    run (run_\w+): ", text, re.M)
    action = re.search(r"^    action (act_\w+): (\w+); fulfilled (\d+) orders exactly once", text, re.M)
    restored = re.search(r"^    restored processing value: (INR [\d,]+\.\d{2})", text, re.M)
    capture = re.search(r"^    new capture: (INR [\d,]+\.\d{2})", text, re.M)
    return DemoFacts(
        run_id=None if run is None else run.group(1),
        passing_fix_id=passing_fix,
        plans=tuple((int(count), value) for count, value in
                    re.findall(r"^    (\d+) target orders, value (INR [\d,]+\.\d{2})$", text, re.M)),
        action_id=None if action is None else action.group(1),
        fulfilled=None if action is None else int(action.group(3)),
        restored_value=None if restored is None else restored.group(1),
        new_capture=None if capture is None else capture.group(1),
    )


def read_token(log_path: Path) -> str:
    for line in log_path.read_text(encoding="utf-8").splitlines():
        match = TOKEN_LINE.search(line)
        if match:
            return match.group(1)
    raise SystemExit(f"no operator token line in {log_path}: the API did not print one at start")


# --------------------------------------------------------------------------- the steps


def new_context(browser: Browser, recorder: Recorder, label: str, base_url: str) -> BrowserContext:
    context = browser.new_context(base_url=base_url, viewport=VIEWPORT, accept_downloads=True)
    context.set_default_timeout(EXPECT_TIMEOUT_MS)
    context.add_init_script(path=str(Path(__file__).with_name("census.js")))
    recorder.attach(context, label)
    return context


def log_in(page: Page, base_url: str, token: str) -> None:
    page.goto(f"{base_url}/")
    expect(page.get_by_role("heading", name="Operator login")).to_be_visible()
    page.get_by_label("Operator token").fill(token)
    page.get_by_label("Operator ID (op_...)").fill(OPERATOR_ID)
    page.get_by_role("button", name="Start session").click()
    expect(page.locator("header.topbar .operator")).to_contain_text(f"Operator {OPERATOR_ID}")


def step_login(walk: Walkthrough, record: StepRecord, page: Page, token: str) -> None:
    page.goto(f"{SERVER_A}/")
    expect(page.get_by_role("heading", name="Operator login")).to_be_visible()
    badge = page.locator("header.topbar .badge.sandbox")
    record.check(badge.inner_text() == "Synthetic demonstration", f"before login the banner reads {badge.inner_text()!r}")
    walk.shot(record, page, "ui_01a_login_form.png")
    page.get_by_label("Operator token").fill(token)
    page.get_by_label("Operator ID (op_...)").fill(OPERATOR_ID)
    page.get_by_role("button", name="Start session").click()
    expect(page.locator("header.topbar .operator")).to_contain_text(f"Operator {OPERATOR_ID}")
    record.check(get_json(page, "/api/session")["operator_id"] == OPERATOR_ID,
                 f"GET /api/session answers operator {OPERATOR_ID} for this browser's session cookie")
    record.check(badge.inner_text() == "Synthetic demonstration", f"after login the banner reads {badge.inner_text()!r}")
    body = page.locator("body").inner_text()
    record.check(body.count("Synthetic demonstration") == 1, "the banner text appears once on the page")
    record.check("Synthetic sandbox" not in body, "the old banner wording 'Synthetic sandbox' is gone from the page text")
    record.note(f"document title: {page.title()!r}")
    cookie = next((item for item in page.context.cookies() if item["name"] == "workbench_session"), None)
    record.check(cookie is not None and cookie["httpOnly"] and cookie["sameSite"] == "Strict",
                 f"the session cookie is httpOnly and SameSite=Strict ({None if cookie is None else (cookie['httpOnly'], cookie['sameSite'])})")
    no_error_banner(record, page, "after login")
    walk.shot(record, page, "ui_01b_logged_in.png")


def step_open_run(walk: Walkthrough, record: StepRecord, page: Page, demo: DemoFacts) -> None:
    runs = get_json(page, "/api/runs")
    record.check(len(runs) == 1, f"server A lists {[(run['run_id'], run['scenario'], run['seed']) for run in runs]}")
    seeded = next(run for run in runs if run["scenario"] == "worker_backlog" and run["seed"] == 101)
    run_id = seeded["run_id"]
    record.check(demo.run_id is None or run_id == demo.run_id, f"it is the run the CLI demo seeded ({demo.run_id})")
    page.get_by_label("Open run").select_option(run_id)
    expect(page.locator("header.topbar")).to_contain_text(f"Run: {run_id}")
    panel = queue_panel(page)
    expect(panel.get_by_role("heading", name="Order recovery queue", exact=True)).to_be_visible()
    run_doc = get_json(page, run_path(run_id))
    whole = get_json(page, queue_path(run_id, {}))
    rows = wait_for_row_ids(page, panel, [row["order_id"] for row in whole["orders"]], "the unfiltered queue")
    walk.shared["view_a"] = RunView(run_id, whole["as_of_ms"], whole)

    first_panel = page.locator("main div.grid > *").first
    record.check(first_panel.evaluate("(element) => element.matches('section.panel.queue')"),
                 "the queue panel is the first panel of the run view")
    queue_top = panel.bounding_box()["y"]
    incidents_top = page.locator("section.panel.incidents").bounding_box()["y"]
    record.check(queue_top < incidents_top, f"and it sits above the incidents panel (y {queue_top:.0f} < {incidents_top:.0f})")
    record.check(whole["as_of_ms"] == run_doc["replay_clock_ms"], "the queue is read at the run's replay clock")
    lead = panel.locator(":scope > p").first.inner_text()
    record.check(f"as of replay time {replay_time(whole['as_of_ms'])}" in lead, f"the panel states its time: {lead[:90]!r}")
    headers = panel.evaluate(HEADERS_JS)
    record.check(headers == QUEUE_COLUMNS, f"columns {headers}")
    record.check(len(rows) == whole["matching_orders"] == len(whole["orders"]),
                 f"{len(rows)} rows = matching_orders {whole['matching_orders']} = API rows {len(whole['orders'])}")
    problems = row_mismatches(rows, whole["orders"])
    record.check(not problems, f"every row shows order, amount, case state, age, freshness, next step and blockers as "
                 f"the API row says ({len(problems)} mismatches{': ' + problems[0] if problems else ''})")
    amounts_back = all(parse_rupees(shown[1]) == api_row["amount_paise"] for shown, api_row in zip(rows, whole["orders"])
                       if api_row["amount_status"] == "asserted" and api_row["currency"] == "INR")
    record.check(amounts_back, "every asserted INR amount parses back to the API's paise")
    totals, coverage = whole["totals"], whole["coverage"]
    check_queue_figures(record, page, totals, "the run view", TOTALS_MOVED)
    strip = money_strip(page)
    record.check(strip["meta"] == f"as of replay time {replay_time(whole['as_of_ms'])}",
                 f"[UI-A] the money strip states the queue's replay time: {strip['meta']!r}")
    check_not_yet_known(record, page, "the seeded run, with no execution in this session")
    stored = get_json(page, f"{run_path(run_id)}/diagnoses")
    rail = check_rail(record, page, "the seeded run, not diagnosed in this session",
                      rail_states("Current"), path="set by the diagnosis",
                      next_step="Diagnose the latest incident in the Diagnosis card.")
    wanted_cause = (f"Not diagnosed in this session; the Diagnosis card lists "
                    f"{count_of(len(stored), 'stored diagnosis', 'stored diagnoses')} of this run.")
    record.check(rail["facts"].get("Cause") == wanted_cause,
                 f"[UI-A] the rail's cause says the run's {len(stored)} stored diagnoses are not this session's: "
                 f"{rail['facts'].get('Cause')!r}")
    record.check(all(not step["proof"] and step["link"] is None for step in rail["steps"]),
                 "[UI-A] no step shows a proof before this session diagnoses: the CLI's earlier recovery is not "
                 "presented as this session's")
    coverage_line = panel.locator(":scope > p.versions").inner_text()
    record.check(coverage_line.startswith(f"Read by this time: {coverage['orders']} orders ({coverage['unknown_orders']} "
                                          "named only by payment evidence)"), f"coverage line: {coverage_line[:100]!r}")
    by_state = Counter(row["state"] for row in whole["orders"])
    record.note(f"the queue at {replay_time(whole['as_of_ms'])}: {len(rows)} orders by state {dict(by_state)}; totals {totals}")
    record.check(panel.locator(":scope > div.case-detail").count() == 0, "no case is open until an order is clicked")
    no_error_banner(record, page, "the run view")
    walk.shot(record, page, "ui_02a_run_view_queue_first.png")
    walk.shot(record, panel, "ui_02b_queue_panel.png")


def check_filters(walk: Walkthrough, record: StepRecord, page: Page, view: RunView, *, shot_prefix: str,
                  select_keys: set[str] | None) -> None:
    """Every Show option's count against the API; selecting an option (each, or those in select_keys) re-queries."""
    panel = queue_panel(page)
    select = show_select(panel)
    options = select.locator("option").evaluate_all(
        "(options) => options.map((option) => ({ value: option.value, text: option.textContent }))")
    keys = [option["value"] for option in options]
    record.check(set(MAIN_FILTERS) <= set(keys), f"the Show select offers the four worklists; options {keys}")
    record.check(select.input_value() == "all", f"the queue opens on {select.input_value()!r}")
    all_ids = [row["order_id"] for row in view.whole["orders"]]
    baseline_totals = money_strip(page)["figures"]
    for option in options:
        key, label = option["value"], option["text"]
        query = option_query(key, view.whole["states"])
        answer = get_json(page, queue_path(view.run_id, query))
        count = re.search(r"\((\d+)\)$", label)
        shown_count = None if count is None else int(count.group(1))
        record.check(shown_count == answer["matching_orders"],
                     f"option {label!r}: its count equals GET orders?{describe_query(query)} -> matching_orders {answer['matching_orders']}")
        if key == "all" or (select_keys is not None and key not in select_keys):
            continue
        with page.expect_response(lambda response: "/orders?" in response.url and response.request.method == "GET") as sent:
            select.select_option(key)
        sent_params = sorted(parse_qsl(urlsplit(sent.value.url).query))
        record.check(sent_params == sorted(query_params(query)), f"{label}: the page asked the server for {sent_params}")
        expected_ids = [row["order_id"] for row in answer["orders"]]
        rows = wait_for_row_ids(page, panel, expected_ids, label)
        line = panel.locator(":scope > div.row > span").inner_text()
        record.check(line.startswith(f"{answer['matching_orders']} orders match"),
                     f"{label}: {len(rows)} rows; the page says {line.split(';')[0]!r}; the API says {answer['matching_orders']}")
        record.check(len(rows) == answer["matching_orders"] == len(answer["orders"]), f"{label}: row count agrees")
        record.check(set(expected_ids) != set(all_ids) or len(expected_ids) == len(all_ids),
                     f"{label}: the rows changed ({len(all_ids)} -> {len(expected_ids)})")
        if "state" in query:
            labels = {CASE_STATE_LABELS[state] for state in query["state"]}
            record.check(all(row[2] in labels for row in rows), f"{label}: every row's case state is one of {sorted(labels)}")
        if "next_step" in query:
            record.check(all(row[5] == NEXT_STEP_LABELS["manual_review"] for row in rows), f"{label}: every row's next step is Manual review")
        problems = row_mismatches(rows, answer["orders"])
        record.check(not problems, f"{label}: every row matches its API row ({len(problems)} mismatches)")
        record.check(money_strip(page)["figures"] == baseline_totals,
                     f"{label}: the whole-queue totals did not change {TOTALS_MOVED}")
        if key in MAIN_FILTERS:
            walk.shot(record, panel, f"{shot_prefix}_{key}.png")
    with page.expect_response(lambda response: "/orders?" in response.url and response.request.method == "GET"):
        select.select_option("all")
    wait_for_row_ids(page, panel, all_ids, "All orders again")
    no_error_banner(record, page, "after the filters")


def step_sorting(walk: Walkthrough, record: StepRecord, page: Page, view: RunView) -> None:
    panel = queue_panel(page)
    by_id = {row["order_id"]: row for row in view.whole["orders"]}
    plan = [
        ("Age", "age", "age_ms", True, "ui_04a_sort_age_largest_first.png"),
        ("Age", "age", "age_ms", False, "ui_04b_sort_age_smallest_first.png"),
        ("Amount", "amount", "amount_paise", True, "ui_04c_sort_amount_largest_first.png"),
        ("Amount", "amount", "amount_paise", False, "ui_04d_sort_amount_smallest_first.png"),
    ]
    for label, key, field_name, descending, shot in plan:
        sort_header(panel, label).click()
        wanted_label = f"{label} ({'largest' if descending else 'smallest'} first)"
        expect(sort_header(panel, label)).to_have_text(wanted_label)
        ids = [row[0] for row in queue_rows(panel)]
        values = [by_id[order_id][field_name] for order_id in ids]
        first_null = next((index for index, value in enumerate(values) if value is None), len(values))
        present = values[:first_null]
        ordered = all((left >= right) if descending else (left <= right) for left, right in zip(present, present[1:]))
        record.check(ordered and all(value is None for value in values[first_null:]),
                     f"{wanted_label}: the API's {field_name} runs {'down' if descending else 'up'} the table "
                     f"({present[0] if present else None} ... {present[-1] if present else None}), "
                     f"{len(values) - first_null} rows without a value last")
        ties = [(left, right) for left, right, left_value, right_value in zip(ids, ids[1:], values, values[1:])
                if left_value == right_value and left > right]
        record.check(not ties, f"{wanted_label}: equal values keep order-ID order ({len(ties)} out of order)")
        record.check(ids == page.evaluate(SORT_JS, [view.whole["orders"], key, descending]),
                     f"{wanted_label}: the exact row order matches the stated rule")
        record.check(len(ids) == len(by_id), f"{wanted_label}: still {len(ids)} rows")
        walk.shot(record, panel, shot)
    sort_header(panel, "Order").click()
    expect(sort_header(panel, "Order")).to_have_text("Order")
    ids = [row[0] for row in queue_rows(panel)]
    record.check(ids == [row["order_id"] for row in view.whole["orders"]], "clicking Order restores order-ID order")


def find_recovered_order(page: Page, view: RunView) -> tuple[str, dict]:
    """The first complete order whose case has a completed receipt and a worker stack trace in its timeline."""
    for row in view.whole["orders"]:
        if row["state"] != "complete":
            continue
        doc = get_json(page, case_path(view.run_id, row["order_id"], view.as_of_ms))
        has_receipt = any(item["order_result"] == "completed" for item in doc["receipts"])
        if has_receipt and any(entry["text"] for entry in doc["timeline"]):
            return row["order_id"], doc
    raise CheckFailed("no complete order has both a completed receipt and a worker stack trace")


def step_case_detail(walk: Walkthrough, record: StepRecord, page: Page, view: RunView) -> None:
    panel = queue_panel(page)
    order_id, _ = find_recovered_order(page, view)
    walk.shared["recovered_order"] = order_id
    detail = open_case(panel, order_id)
    record.check(panel.locator(f":scope > div.scroll tr.selected").inner_text().startswith(order_id),
                 f"the clicked row ({order_id}) is marked selected")
    doc = verify_case(record, page, view.run_id, view.as_of_ms, order_id, detail, f"recovered case {order_id}")
    walk.shared["recovered_doc"] = doc
    traces = [entry for entry in doc["timeline"] if entry["text"]]
    record.note(f"{order_id}: {len(doc['timeline'])} timeline records, {len(traces)} worker stack traces; the first "
                f"trace's last line {traces[0]['text'].strip().splitlines()[-1]!r}" if traces else f"{order_id}: no trace")
    walk.shot(record, detail, "ui_05a_case_detail_recovered_order.png")
    entries = detail.locator(":scope > ol.timeline > li")
    if entries.count() >= 10:  # how the two-digit list numbers look in the page, outside an element crop
        entries.nth(9).scroll_into_view_if_needed()
        walk.shot(record, page, "ui_05a_case_timeline_numbers_in_page.png")
    record.check(expand_timeline(detail) == 0, "every timeline record's details opened from its summary")
    first_trace = detail.locator("div.untrusted").first
    if first_trace.count():
        first_trace.scroll_into_view_if_needed()
    walk.shot(record, detail, "ui_05b_case_detail_timeline_expanded.png")
    walk.shot(record, page, "ui_05c_case_detail_in_page.png")

    shown_states = {"complete"}
    for row in view.whole["orders"]:
        if row["state"] in shown_states:
            continue
        shown_states.add(row["state"])
        other = open_case(panel, row["order_id"])
        verify_case(record, page, view.run_id, view.as_of_ms, row["order_id"], other, f"{row['state']} case {row['order_id']}")
        walk.shot(record, other, f"ui_05d_case_{row['state']}.png")
    record.note(f"cases opened, one per state present: {sorted(shown_states)}")
    open_case(panel, order_id)
    no_error_banner(record, page, "the case views")


def step_receipt(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, view: RunView,
                 demo: DemoFacts) -> None:
    panel = queue_panel(page)
    order_id = walk.shared["recovered_order"]
    detail = panel.locator(":scope > div.case-detail")
    expect(detail.locator(":scope > h3")).to_contain_text(f"Case {order_id}")
    doc = get_json(page, case_path(view.run_id, order_id, view.as_of_ms))
    restored = None if demo.restored_value is None else parse_rupees(demo.restored_value)
    receipt = verify_receipt(walk, record, page, context, view.run_id, order_id, detail, doc,
                             expected_restored_paise=restored, shot_prefix="ui_06")
    record.check(demo.action_id is None or receipt["action_id"] == demo.action_id,
                 f"the receipt's action {receipt['action_id']} is the one the CLI demo executed ({demo.action_id})")
    walk.shared["receipt_action"] = receipt["action_id"]
    no_error_banner(record, page, "after the receipt links")


def step_keyboard(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, view: RunView) -> None:
    panel = queue_panel(page)
    close_case(panel)
    select = show_select(panel)
    select.focus()
    stops = []
    for _ in range(13):
        page.keyboard.press("Tab")
        stops.append(page.evaluate(ACTIVE_JS))
    record.check([stop["text"] for stop in stops[:3]] == ["Order", "Amount", "Age"] and all(stop["inQueueHead"] for stop in stops[:3]),
                 f"Tab from the Show select reaches the Order, Amount and Age sort buttons: {[stop['text'] for stop in stops[:3]]}")
    first_ids = [row["order_id"] for row in view.whole["orders"][:10]]
    record.check([stop["text"] for stop in stops[3:]] == first_ids and all(stop["inQueueBody"] for stop in stops[3:]),
                 "then each row's order button, top to bottom")
    record.check(all(stop["focusVisible"] and not stop["outline"].startswith("none") for stop in stops),
                 f"each stop shows a focus ring (outline {sorted({stop['outline'] for stop in stops})})")
    walk.shot(record, panel, "ui_08a_keyboard_focus_in_queue.png")
    for _ in range(13):
        page.keyboard.press("Shift+Tab")
    record.check(page.evaluate("() => document.activeElement === document.querySelector('section.panel.queue > div.row select')"),
                 "Shift+Tab walks back to the Show select")

    age = sort_header(panel, "Age")
    age.focus()
    page.keyboard.press("Enter")
    expect(age).to_have_text("Age (largest first)")
    page.keyboard.press("Space")
    expect(age).to_have_text("Age (smallest first)")
    record.check(True, "Enter and Space on the Age header sort and reverse")
    order = sort_header(panel, "Order")
    order.focus()
    page.keyboard.press("Enter")
    expect(order).to_have_text("Order")
    aria_sort = panel.locator(":scope > div.scroll > table > thead th").evaluate_all(
        "(cells) => cells.map((cell) => cell.getAttribute('aria-sort'))")
    age.click()
    sorted_aria = panel.locator(":scope > div.scroll > table > thead th").evaluate_all(
        "(cells) => cells.map((cell) => cell.getAttribute('aria-sort'))")
    record.note(f"aria-sort on the header cells: {aria_sort} unsorted; {sorted_aria} while Age sorts largest first")
    walk.shared["aria_sort"] = sorted_aria
    order.click()
    expect(order).to_have_text("Order")

    order_id = walk.shared.get("recovered_order") or view.whole["orders"][0]["order_id"]
    ids = [row["order_id"] for row in view.whole["orders"]]
    button = panel.locator(":scope > div.scroll").get_by_role("button", name=order_id, exact=True)
    button.focus()
    page.keyboard.press("Enter")
    detail = panel.locator(":scope > div.case-detail")
    expect(detail).to_contain_text("As of replay time")
    after_open = page.evaluate(ACTIVE_JS)
    heading = detail.locator(":scope > h3")
    record.check(heading.inner_text().startswith(f"Case {order_id}"),
                 f"Enter on {order_id}'s button opens its case [V-adjusted: U's check also asserted that focus stays on the "
                 f"button, the U-1 defect; focus is on {after_open['tag']} {after_open['text'][:48]!r}]")
    record.check(after_open.get("isCaseHeading", False) and after_open["text"].startswith(f"Case {order_id}"),
                 f"[V] after opening a case with the keyboard, document.activeElement is the case heading "
                 f"(it is {after_open['tag']} {after_open['text'][:48]!r})")
    heading_tabindex = heading.get_attribute("tabindex")
    record.check(heading_tabindex == "-1", f"[V] the heading takes focus from script but is not a Tab stop "
                 f"(tabindex attribute {heading_tabindex!r})")
    record.check(after_open.get("isCaseHeading", False) and after_open["focusVisible"]
                 and not after_open["outline"].startswith("none"),
                 f"[V] the focused heading shows a focus ring (:focus-visible {after_open.get('focusVisible')}, "
                 f"outline {after_open.get('outline')})")
    walk.shot(record, page, "ui_08c_keyboard_open_heading_focused.png")
    box = detail.bounding_box()
    record.note(f"after opening, the case view's top is at y={box['y']:.0f} of a {VIEWPORT['height']} px viewport")
    later_rows = len(ids) - ids.index(order_id) - 1
    presses, stop = 0, after_open
    while presses < 1000 and not stop.get("inCase"):
        page.keyboard.press("Tab")
        presses += 1
        stop = page.evaluate(ACTIVE_JS)
    walk.shared["tabs_to_case"] = (presses, later_rows, ids.index(order_id) + 1, len(ids))
    record.check(stop.get("inCase", False), f"Tab reaches the case view after {presses} presses (first stop {stop['text']!r}); "
                 f"{later_rows} order buttons lie between row {ids.index(order_id) + 1} and it")
    record.check(presses == 0, f"[V] focus is already in the case view: {presses} Tab presses from the opened row to reach it")
    walk.shot(record, page, "ui_08b_keyboard_reached_case_view.png")
    inside = []
    for _ in range(4):
        page.keyboard.press("Tab")
        inside.append(page.evaluate(ACTIVE_JS)["text"])
    record.check("Receipt (JSON)" in inside and "Receipt (Markdown)" in inside,
                 f"the next stops are the receipt links: {inside}")
    record.check(inside[0] == "Close", f"[V] the first Tab stop after the opened case's heading is its Close button ({inside[0]!r})")
    tab, response = receipt_tab(context, detail.get_by_role("link", name="Receipt (JSON)").first, keyboard_page=page)
    try:
        record.check(response is not None and response.status == 200 and '"receipt_version"' in tab_text(tab),
                     f"Enter on the focused JSON receipt link opens the receipt ({None if response is None else response.status})")
    finally:
        tab.close()

    names = computed_names(context, page, {
        "Show (queue filter)": "section.panel.queue > div.row select",
        "Order sort": "section.panel.queue > div.scroll thead th:nth-child(1) button",
        "Amount sort": "section.panel.queue > div.scroll thead th:nth-child(2) button",
        "Age sort": "section.panel.queue > div.scroll thead th:nth-child(4) button",
        "first order button": "section.panel.queue > div.scroll tbody tr:first-child button",
        "case Close": "section.panel.queue > div.case-detail > h3 button",
        "receipt JSON link": "section.panel.queue > div.case-detail a",
        "first timeline summary": "section.panel.queue > div.case-detail ol.timeline summary",
    })
    walk.shared["names_a"] = names
    show = names["Show (queue filter)"]
    record.check(show["role"] == "combobox" and bool(show["name"].strip()),
                 f"the queue filter control has an accessible name: Chromium computes role {show['role']!r}, name {show['name']!r}")
    record.check(page.get_by_role("combobox", name="Show", exact=True).count() == 1,
                 "Playwright finds it by role and name: combobox 'Show'")
    record.note(f"Chromium names: { {label: (item['role'], item['name']) for label, item in names.items()} }")
    controls = interactive_controls(context, page)
    unnamed = [control for control in controls if not control["name"].strip()]
    record.note(f"{len(controls)} interactive controls on the page; {len(unnamed)} without a name: {unnamed}")
    combos = [control for control in controls if control["role"] == "combobox"]
    record.check(all(control["name"].strip() for control in combos), f"every select on the page is named: {combos}")

    detail.get_by_role("button", name="Close").focus()
    page.keyboard.press("Enter")
    expect(detail).to_have_count(0)
    after_close = page.evaluate(ACTIVE_JS)
    record.note(f"after Close, focus is on {after_close['tag']} {after_close['text'][:40]!r}")
    walk.shared["focus_after_close"] = after_close
    record.check(after_close.get("inQueueBody", False) and after_close["text"] == order_id,
                 f"[V] after closing with the keyboard, focus is back on the row that opened the case ({order_id}); it is on "
                 f"{after_close['tag']} {after_close['text'][:40]!r}")
    record.check(after_close.get("focusVisible", False) and not after_close.get("outline", "none").startswith("none"),
                 f"[V] and that row's button shows its focus ring (outline {after_close.get('outline')})")
    walk.shot(record, panel, "ui_08d_keyboard_close_row_focused.png")
    check_focus_paths(walk, record, page, panel, view, order_id)
    no_error_banner(record, page, "the keyboard pass")


def check_focus_paths(walk: Walkthrough, record: StepRecord, page: Page, panel: Locator, view: RunView, order_id: str) -> None:
    """[V] U-1 beyond one keyboard open and close: the mouse, a switch between cases, and a case whose row is gone."""
    ids = [row["order_id"] for row in view.whole["orders"]]
    scroll = panel.locator(":scope > div.scroll")
    detail = panel.locator(":scope > div.case-detail")

    # A click on plain text first, so the row click is a mouse interaction from scratch rather than a click on a button
    # that still holds keyboard focus (Chromium would carry the keyboard's :focus-visible over to the heading).
    panel.locator(":scope > h2").click()
    record.note(f"[V] before the mouse pass, focus is on {page.evaluate(ACTIVE_JS)['tag']}")
    scroll.get_by_role("button", name=order_id, exact=True).click()
    expect(detail).to_contain_text("As of replay time")
    clicked = page.evaluate(ACTIVE_JS)
    record.check(clicked.get("isCaseHeading", False) and clicked["text"].startswith(f"Case {order_id}"),
                 f"[V] after opening a case with the mouse, focus is on the case heading ({clicked['tag']} {clicked['text'][:48]!r})")
    record.note(f"[V] a mouse-opened heading: :focus-visible {clicked.get('focusVisible')}, outline {clicked.get('outline')}")
    detail.get_by_role("button", name="Close").click()
    expect(detail).to_have_count(0)
    closed = page.evaluate(ACTIVE_JS)
    record.check(closed.get("inQueueBody", False) and closed["text"] == order_id,
                 f"[V] after closing with the mouse, focus is on the row that opened the case ({closed['tag']} {closed['text'][:40]!r})")

    # The case view stays mounted when another row is chosen; the new heading must still take focus.
    other_id = ids[(ids.index(order_id) + 1) % len(ids)]
    scroll.get_by_role("button", name=order_id, exact=True).focus()
    page.keyboard.press("Enter")
    expect(detail.locator(":scope > h3")).to_contain_text(f"Case {order_id}")
    scroll.get_by_role("button", name=other_id, exact=True).focus()
    page.keyboard.press("Enter")
    expect(detail.locator(":scope > h3")).to_contain_text(f"Case {other_id}")
    expect(detail).to_contain_text("As of replay time")  # the new case's load starts in the same effects as the focus move
    switched = page.evaluate(ACTIVE_JS)
    record.check(switched.get("isCaseHeading", False) and switched["text"].startswith(f"Case {other_id}"),
                 f"[V] opening another order's case while one is open moves focus to the new heading "
                 f"({switched['tag']} {switched['text'][:48]!r})")

    # A filter that leaves the open case's row out; the case stays open until it is closed.
    worklist, kept_ids = "", []
    for key, query in MAIN_FILTERS.items():
        answer_ids = [row["order_id"] for row in get_json(page, queue_path(view.run_id, query))["orders"]]
        if answer_ids and other_id not in answer_ids:
            worklist, kept_ids = key, answer_ids
            break
    if not worklist:
        raise CheckFailed(f"no non-empty worklist leaves out {other_id}, so the row-gone path cannot be shown")
    select = show_select(panel)
    with page.expect_response(lambda response: "/orders?" in response.url and response.request.method == "GET"):
        select.select_option(worklist)
    wait_for_row_ids(page, panel, kept_ids, f"the {worklist} worklist")
    record.check(scroll.get_by_role("button", name=other_id, exact=True).count() == 0 and detail.count() == 1,
                 f"[V] with the {worklist} worklist shown ({len(kept_ids)} rows), {other_id}'s row is gone and its case is still open")
    detail.get_by_role("button", name="Close").focus()
    page.keyboard.press("Enter")
    expect(detail).to_have_count(0)
    fallback = page.evaluate(ACTIVE_JS)
    record.check(fallback.get("isQueueTable", False),
                 f"[V] closing a case whose row is gone puts focus on the queue table (it is on {fallback['tag']} "
                 f"{fallback['text'][:40]!r})")
    record.check(fallback.get("isQueueTable", False) and fallback["focusVisible"] and not fallback["outline"].startswith("none"),
                 f"[V] and the table shows a focus ring (:focus-visible {fallback.get('focusVisible')}, outline {fallback.get('outline')})")
    walk.shot(record, panel, "ui_08e_row_gone_table_focused.png")
    with page.expect_response(lambda response: "/orders?" in response.url and response.request.method == "GET"):
        select.select_option("all")
    wait_for_row_ids(page, panel, ids, "All orders again")


def step_seeded_flow_a(walk: Walkthrough, record: StepRecord, page: Page, view: RunView) -> None:
    incidents = get_json(page, f"{run_path(view.run_id)}/incidents")
    diagnoses = get_json(page, f"{run_path(view.run_id)}/diagnoses")
    panel = page.locator("section.panel.incidents")
    expect(panel.locator("h2")).to_have_text("Incidents")
    text = panel.inner_text()
    record.check(all(item["incident_id"] in text for item in incidents["incidents"]),
                 f"incidents panel lists {[item['incident_id'] for item in incidents['incidents']]}")
    record.check(panel.locator("ul.compact > li").count() == len(incidents["detectors_now"]),
                 f"it lists all {len(incidents['detectors_now'])} detectors now")
    metrics = page.locator("section.panel.metrics")
    expect(metrics.locator("h2")).to_contain_text("Provider versus merchant, as of")
    record.check(metrics.locator("table tbody tr").count() == len(incidents["provider"]["current"]),
                 f"the provider table has a row per cohort ({len(incidents['provider']['current'])})")
    deployments = metrics.locator("ul.compact > li").all_inner_texts()
    record.note(f"deployments shown: {deployments}")
    diagnosis = page.locator("section.panel.diagnosis")
    alternatives = diagnosis.locator("div.alternatives li").all_inner_texts()
    record.check(len(alternatives) == len(diagnoses) and any("merchant_worker_regression proposing reprocess_inbox" in item for item in alternatives),
                 f"the diagnosis panel lists the stored diagnosis: {alternatives}")
    record.check("Run a diagnosis to assemble the evidence packet" in page.locator("section.panel.evidence").inner_text(),
                 "the evidence panel waits for a diagnosis")
    record.check("Run a diagnosis first." in page.locator("section.panel.recovery").inner_text(),
                 "the recovery panel waits for a diagnosis in this session")
    walk.shot(record, page, "ui_07a_seeded_run_flow_a_panels.png", full_page=True)
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Export report (JSON)").click()
    saved = walk.out / "ui_07a_report_seeded_run.json"
    downloaded.value.save_as(str(saved))
    report = json.loads(saved.read_text(encoding="utf-8"))
    action_ids = [item["action_id"] for item in report["actions"]]
    record.check(bool(report["plans"]) and bool(report["approvals"]) and bool(report["actions"]),
                 f"the exported report holds the plan, approval and execution state: {len(report['plans'])} plans, "
                 f"{len(report['approvals'])} approvals, actions {action_ids}")
    record.check(walk.shared.get("receipt_action") in action_ids, "including the action whose receipt step 6 opened")
    record.note(f"report outcome: {report.get('outcome')}")
    expect(page.locator("section.panel.report")).to_contain_text(f"Saved as report-{view.run_id}.json")
    no_error_banner(record, page, "the seeded run's Flow A panels")


def dl_fields(container: Locator) -> dict[str, str]:
    return container.evaluate("""(element) => {
      const fields = {};
      for (const term of element.querySelectorAll("dl > dt")) fields[term.textContent] = term.nextElementSibling?.textContent ?? null;
      return fields;
    }""")


def plan_numbers(plan: Locator) -> tuple[int, str]:
    match = re.search(r"(\d+) target orders, value (INR [\d,]+\.\d{2})", plan.inner_text())
    if match is None:
        raise CheckFailed(f"the plan view has no 'N target orders, value' line: {plan.inner_text()[:200]!r}")
    return int(match.group(1)), match.group(2)


EXECUTION_LINE = re.compile(
    r"Action (act_\w+): (\w+)( \(replayed[^)]*\))?; fulfilled (\d+) orders; restored processing value "
    r"(INR [\d,]+\.\d{2}).*new capture (INR [\d,]+\.\d{2})")
LABELS_JS = """(select) => [...select.labels].map((label) => ({
  text: [...label.childNodes].filter((node) => node !== select).map((node) => node.textContent).join("").trim(),
  visible: label.checkVisibility(),
}))"""


def check_fix_select_name(record: StepRecord, context: BrowserContext, page: Page, recovery: Locator, unnamed: list[dict]) -> None:
    """[V] U-2: the fix-validation select has an accessible name, from a visible label."""
    fix = computed_names(context, page, {"fix select": "section.panel.recovery select"})["fix select"]
    name = (fix["name"] or "").strip()
    record.check(fix["role"] == "combobox" and bool(name),
                 f"[V] the fix select has an accessible name: Chromium computes role {fix['role']!r}, name {fix['name']!r}")
    labels = recovery.locator("select").evaluate(LABELS_JS)
    record.check(bool(name) and any(label["visible"] and label["text"] == name for label in labels)
                 and recovery.get_by_role("combobox", name=name, exact=True).count() == 1,
                 f"[V] the name comes from a visible <label>, and Playwright finds the select by it: labels {labels}")
    record.check(not unnamed, f"[V] no interactive control is without an accessible name with the recovery panel open: {unnamed}")


def check_target_amounts(record: StepRecord, plan: Locator, when: str) -> None:
    """[V] U-3: each exact target row names its currency once, and the rows add up to the plan's value."""
    rows = plan.locator("table tbody tr").evaluate_all(
        "(rows) => rows.map((row) => [...row.children].map((cell) => cell.textContent))")
    twice = [cells for cells in rows
             if any(len(re.findall(rf"\b{code}\b", " ".join(cells))) > 1 for code in set(re.findall(r"\b[A-Z]{3}\b", cells[3])))]
    record.check(bool(rows) and not twice, f"[V] {when}: no exact target row shows its currency code twice "
                 f"({len(twice)} of {len(rows)} rows do{'; the first reads ' + repr(twice[0]) if twice else ''})")
    amounts = [parse_rupees(cells[3]) for cells in rows]
    unread = [cells[3] for cells, amount in zip(rows, amounts) if amount is None]
    _, value = plan_numbers(plan)
    total = rupees(sum(amount for amount in amounts if amount is not None))
    record.check(bool(rows) and not unread and total == value,
                 f"[V] {when}: every Amount cell reads as one INR amount, and the {len(rows)} rows add up to the plan's value "
                 f"{value} (sum {total}; {len(unread)} cells unread{', the first ' + repr(unread[0]) if unread else ''})")


def check_clock_move_keeps_focus(record: StepRecord, page: Page, panel: Locator, run_id: str, order_id: str) -> None:
    """[V] U-1: a case left open reloads when the replay clock moves; that reload is not an opening and takes no focus."""
    detail = panel.locator(":scope > div.case-detail")
    advance = page.locator("section.panel.controls").get_by_role("button", name="+1 min", exact=True)
    expect(advance).to_be_enabled()
    moved_to = replay_time(get_json(page, run_path(run_id))["replay_clock_ms"] + 60_000)
    advance.focus()
    page.keyboard.press("Enter")
    expect(panel.locator(":scope > p").first).to_contain_text(f"as of replay time {moved_to}")
    expect(detail).to_contain_text(f"As of replay time {moved_to}")
    expect(advance).to_be_enabled()
    active = page.evaluate(ACTIVE_JS)
    record.check(detail.locator(":scope > h3").inner_text().startswith(f"Case {order_id}") and not active.get("isCaseHeading", False),
                 f"[V] with case {order_id} open, '+1 min' moved the clock to {moved_to} and the case reloaded at that time "
                 f"without taking focus (focus is on {active['tag']} {active['text'][:40]!r})")


def step_ui_flow_a(walk: Walkthrough, record: StepRecord, browser: Browser, recorder: Recorder, token: str,
                   demo: DemoFacts, holder: dict) -> None:
    context = new_context(browser, recorder, "B", SERVER_B)
    page = context.new_page()
    holder["page"] = page
    try:
        log_in(page, SERVER_B, token)
        record.check(get_json(page, "/api/runs") == [], "server B starts with no run")
        page.get_by_label("Scenario").select_option("worker_backlog")
        page.get_by_label("Seed").fill("101")
        page.get_by_role("button", name="Create synthetic run").click()
        expect(page.locator("header.topbar")).to_contain_text("Run: run_", timeout=SLOW_TIMEOUT_MS)
        run_id = get_json(page, "/api/runs")[0]["run_id"]
        seeded = walk.shared.get("view_a")
        record.check(seeded is None or run_id == seeded.run_id,
                     f"the UI created {run_id}: the run ID the CLI demo got for the same scenario and seed")
        panel = queue_panel(page)
        whole = get_json(page, queue_path(run_id, {}))
        wait_for_row_ids(page, panel, [row["order_id"] for row in whole["orders"]], "the fresh run's queue")
        view = RunView(run_id, whole["as_of_ms"], whole)
        record.note(f"fresh queue at {replay_time(whole['as_of_ms'])}: {dict(Counter(row['state'] for row in whole['orders']))}; totals {whole['totals']}")
        walk.shot(record, page, "ui_07b_fresh_run_queue.png")
        check_queue_figures(record, page, whole["totals"], "[UI-A] the fresh run")
        check_not_yet_known(record, page, "the fresh run")
        check_rail(record, page, "the fresh run", rail_states("Current"),
                   path="set by the diagnosis", next_step="Diagnose the latest incident in the Diagnosis card.")
        check_filters(walk, record, page, view, shot_prefix="ui_07c_fresh_filter", select_keys=set(MAIN_FILTERS))
        for state in ("captured_processing_incomplete", "payment_state_unresolved"):
            row = next((item for item in whole["orders"] if item["state"] == state), None)
            if not record.check(row is not None, f"the unrecovered queue has a {state} order"):
                continue
            detail = open_case(panel, row["order_id"])
            verify_case(record, page, run_id, whole["as_of_ms"], row["order_id"], detail, f"fresh {state} case {row['order_id']}")
            walk.shot(record, detail, f"ui_07d_fresh_case_{state}.png")
        close_case(panel)

        diagnosis = page.locator("section.panel.diagnosis")
        record.check(diagnosis.get_by_label("Mode").input_value() == "rules", "the diagnosis mode defaults to the server's rules mode")
        diagnosis.get_by_role("button", name="Diagnose the latest incident").click()
        expect(diagnosis.locator("p", has_text="Job job_").locator("strong")).to_have_text("done", timeout=SLOW_TIMEOUT_MS)
        fields = dl_fields(diagnosis.locator("div.diagnosis-result"))
        record.check(fields.get("Leading hypothesis") == "merchant_worker_regression"
                     and fields.get("Faulting change") == "deploy_9b85880366"
                     and (fields.get("Proposed operation") or "").startswith("reprocess_inbox"),
                     f"the rules diagnosis names {fields.get('Leading hypothesis')}, {fields.get('Faulting change')}, "
                     f"{fields.get('Proposed operation')!r}")
        record.note(f"evidence level: {fields.get('Evidence level (a label, not a probability)')!r}")
        job = job_of_page(page, run_id)
        output = job["diagnosis"]["output"]
        rail = check_rail(record, page, "after the diagnosis", rail_states("Done", "Current"),
                          path="reprocess_inbox: validate a fix, plan, approve, execute, receipt",
                          next_step="Validate a corrected handler in the Recovery card.", proofs={0: diagnose_proof(job)})
        wanted_cause = (f"{output['hypothesis']}; faulting change {output['faulting_change_id']}; affected method "
                        f"{output['affected_method']}")
        record.check(rail["facts"].get("Cause") == wanted_cause,
                     f"[UI-A] the rail's cause is the diagnosis's: {rail['facts'].get('Cause')!r}")
        evidence = page.locator("section.panel.evidence")
        expect(evidence.locator("h2")).to_contain_text("Evidence packet")
        citation = diagnosis.locator("button.citation").first
        cited = citation.inner_text()
        citation.click()
        expect(evidence.locator("li.selected pre.json")).to_be_visible()
        record.check(f"[{cited}]" in evidence.locator("li.selected").inner_text(), f"a citation ({cited}) opens its evidence item")
        walk.shot(record, page, "ui_07e_diagnosis_and_evidence.png", full_page=True)

        recovery = page.locator("section.panel.recovery")
        unnamed = [control for control in interactive_controls(context, page) if not control["name"].strip()]
        walk.shared["unnamed_b"] = unnamed
        record.note(f"controls without an accessible name with the recovery panel open: {unnamed}")
        check_fix_select_name(record, context, page, recovery, unnamed)
        fix_select = recovery.locator("select")
        fix_ids = fix_select.locator("option").evaluate_all("(options) => options.map((option) => option.value)")
        candidates = [demo.passing_fix_id] if demo.passing_fix_id in fix_ids else fix_ids
        verdict = ""
        for fix_id in candidates:
            fix_select.select_option(fix_id)
            recovery.get_by_role("button", name="Validate this corrected handler").click()
            line = recovery.locator("p", has_text=f"{fix_id}: reproduced on the deployed handler")
            expect(line).to_be_visible()
            verdict = line.inner_text()
            if "did not pass" not in verdict:
                break
        record.check("passed" in verdict and "did not pass" not in verdict and f"validated by {OPERATOR_ID}" in verdict,
                     f"fix validation: {verdict!r}")
        check_rail(record, page, "after the fix validation", rail_states("Done", "Done", "Current"),
                   next_step="Build the reprocessing plan.", proofs={1: [fix_id, "passed"]})
        walk.shot(record, recovery, "ui_07f_fix_validated.png")

        plans_answered = path_matcher("POST", re.compile(r"/plans$"))
        with page.expect_response(plans_answered) as planned:
            recovery.get_by_role("button", name="Build reprocessing plan").click()
        plan = recovery.locator("div.plan")
        expect(plan.locator("h3")).to_contain_text("Plan plan_")
        first_plan_id = plan.locator("h3").inner_text().removeprefix("Plan ").strip()
        first = plan_numbers(plan)
        record.check(not demo.plans or first == demo.plans[0], f"first plan: {first[0]} targets, {first[1]} (CLI demo: {demo.plans[:1]})")
        answer = planned.value.json()
        check_rail(record, page, "after the first plan", rail_states("Done", "Done", "Done", "Current"),
                   next_step="Check the plan's exact target rows, then approve it.",
                   proofs={2: plan_proof(answer["plan_id"], answer["target_count"], answer["plan_hash"])})
        walk.shot(record, recovery, "ui_07g_plan.png")
        check_target_amounts(record, plan, "first plan")
        stale = recovery.locator("div.row", has_text="excluded only because their provider state is stale")
        if stale.count():
            with page.expect_response(plans_answered) as replanned:
                stale.get_by_role("button", name="Refresh provider state (read-only) and re-plan").click()
            expect(plan).to_contain_text(f"Replaces draft {first_plan_id}")
            second = plan_numbers(plan)
            answer = replanned.value.json()
            check_rail(record, page, "after the re-plan", rail_states("Done", "Done", "Done", "Current"),
                       proofs={2: plan_proof(answer["plan_id"], answer["target_count"], answer["plan_hash"])})
            record.check(len(demo.plans) < 2 or second == demo.plans[1],
                         f"after the read-only refresh: {second[0]} targets, {second[1]} (CLI demo: {demo.plans[1:2]})")
            walk.shot(record, recovery, "ui_07h_plan_after_refresh.png")
            check_target_amounts(record, plan, "plan after the refresh")
        target_count, target_value = plan_numbers(plan)
        targets = plan.locator("table tbody tr td:first-child").all_inner_texts()
        record.check(len(targets) == target_count, f"the plan lists {len(targets)} exact target rows")

        with page.expect_response(path_matcher("POST", re.compile(r"/approve$"))) as approved:
            recovery.get_by_role("button", name=re.compile(r"^Approve plan ")).click()
        expect(recovery).to_contain_text(f"Approved by {OPERATOR_ID}")
        approval = approved.value.json()
        check_rail(record, page, "after the approval", rail_states("Done", "Done", "Done", "Done", "Current"),
                   next_step="Execute the approved plan.",
                   proofs={3: [f"by {approval['operator_id']}", hash_proof("hash", approval["plan_hash"])]})
        check_not_yet_known(record, page, "approved, not yet executed")
        clock_before = get_json(page, run_path(run_id))["replay_clock_ms"]
        with page.expect_response(path_matcher("POST", re.compile(r"/execute$"))) as executed_answer:
            recovery.get_by_role("button", name="Execute", exact=True).click()
        executions = recovery.locator(":scope > ul.compact > li", has_text="Action act_")
        expect(executions).to_have_count(1)
        executed = EXECUTION_LINE.search(executions.first.inner_text())
        if executed is None:
            raise CheckFailed(f"unreadable execution line: {executions.first.inner_text()!r}")
        action_id, status, _, fulfilled, restored, new_capture = executed.groups()
        record.check(status == "succeeded" and int(fulfilled) == target_count and restored == target_value
                     and new_capture == "INR 0.00",
                     f"execution: {status}, fulfilled {fulfilled}, restored {restored}, new capture {new_capture}")
        record.check(demo.fulfilled is None or (int(fulfilled), restored) == (demo.fulfilled, demo.restored_value),
                     f"the same as the CLI demo's execution ({demo.fulfilled} orders, {demo.restored_value})")
        result = executed_answer.value.json()
        receipt_step = page.locator("main .run-header .rail-step").nth(5)
        expect(receipt_step.locator(".step-state")).to_have_text("Done")
        receipt_doc = get_json(page, receipt_path(run_id, result["action_id"]))
        rail = check_rail(record, page, "after the execution", ["Done"] * 6,
                          next_step=f"Action {result['action_id']} succeeded and its receipt is recorded.",
                          proofs={4: [result["action_id"], "succeeded"], 5: [hash_proof("hash", receipt_doc["receipt_hash"])]})
        link = rail["steps"][5]["link"]
        record.check(link == {"href": receipt_path(run_id, result["action_id"]), "text": "Open receipt (JSON)",
                              "target": "_blank", "rel": "noopener noreferrer"},
                     f"[UI-A] the Receipt step links the receipt built from the run and action IDs: {link}")
        figures = money_strip(page)["figures"]
        wanted_values = {RESTORED_FIGURE: rupees(result["restored_processing_value_paise"]),
                         NEW_CAPTURE_FIGURE: rupees(result["additional_captured_paise"])}
        shown_values = {name: figures.get(name, {}).get("value") for name in wanted_values}
        record.check(shown_values == wanted_values and shown_values[RESTORED_FIGURE] == restored,
                     f"[UI-A] after the execution the money strip shows the Execute answer's values {shown_values}, as the "
                     f"execution line does (restored {restored})")
        recovery.get_by_role("button", name="Execute again (shows the replay)").click()
        expect(executions).to_have_count(2)
        replay = EXECUTION_LINE.search(executions.nth(1).inner_text())
        record.check(replay is not None and replay.group(1) == action_id and replay.group(3) is not None,
                     f"executing again replays the same action: {executions.nth(1).inner_text()[:140]!r}")
        check_rail(record, page, "after executing again", ["Done"] * 6,
                   proofs={4: [action_id, "succeeded", "replayed"], 5: [hash_proof("hash", receipt_doc["receipt_hash"])]})
        replayed_figures = money_strip(page)["figures"]
        record.check(all(replayed_figures.get(name) == figures.get(name) for name in (RESTORED_FIGURE, NEW_CAPTURE_FIGURE)),
                     "[UI-A] the replay changes neither restored value nor new capture in the money strip: the same action, "
                     f"the same values ({ {name: replayed_figures.get(name) for name in (RESTORED_FIGURE, NEW_CAPTURE_FIGURE)} })")
        walk.shot(record, recovery, "ui_07i_approved_executed_replayed.png")
        walk.shot(record, page.locator("main .run-header"), "ui_07m_run_header_after_recovery.png")
        no_error_banner(record, page, "the UI-driven Flow A")

        clock_after = get_json(page, run_path(run_id))["replay_clock_ms"]
        record.check(clock_after > clock_before, f"the operator steps moved the replay clock ({clock_before} -> {clock_after})")
        after = get_json(page, queue_path(run_id, {}))
        expect(panel.locator(":scope > p").first).to_contain_text(f"as of replay time {replay_time(after['as_of_ms'])}")
        rows = wait_for_row_ids(page, panel, [row["order_id"] for row in after["orders"]], "the queue after the recovery")
        problems = row_mismatches(rows, after["orders"])
        record.check(not problems, f"the queue reloaded at the new clock and matches the API ({len(problems)} mismatches)")
        states_after = {row[0]: row[2] for row in rows}
        record.check(all(states_after.get(order_id) == "Complete" for order_id in targets),
                     f"all {len(targets)} plan targets now read Complete in the queue")
        totals = after["totals"]
        captured_after = money_strip(page)["figures"].get(CAPTURED_FIGURE)
        record.check(captured_after == {"value": rupees(totals["captured_unprocessed_value_paise"]),
                                        "detail": captured_detail(totals)},
                     f"totals after: {totals}; the strip shows {captured_after} {TOTALS_MOVED}")
        panel.locator(":scope > div.scroll").get_by_role("button", name=targets[0], exact=True).scroll_into_view_if_needed()
        walk.shot(record, panel, "ui_07j_queue_after_ui_recovery.png")

        detail = open_case(panel, targets[0])
        doc = verify_case(record, page, run_id, after["as_of_ms"], targets[0], detail, f"UI-recovered case {targets[0]}")
        record.check([item["action_id"] for item in doc["receipts"]] == [action_id],
                     f"its receipt is the UI's own action {action_id}")
        walk.shot(record, detail, "ui_07k_case_after_ui_recovery.png")
        verify_receipt(walk, record, page, context, run_id, targets[0], detail, doc,
                       expected_restored_paise=parse_rupees(restored), shot_prefix="ui_07l")
        check_clock_move_keeps_focus(record, page, panel, run_id, targets[0])
        walk.shared["census_b"] = page.evaluate("() => window.__walkthroughCensus")
        no_error_banner(record, page, "after the UI-driven recovery")
    finally:
        context.close()


# --------------------------------------------------------------------------- [X] controls that wait on their own request

# The label colour Chromium gives a disabled button (measured in this image); a busy control keeps it.
DISABLED_LABEL_COLOR = "rgba(16, 16, 16, 0.3)"
SETTLE_MS = 400  # time a second press gets to send the request it must not send
QUIET_MS = 300  # how long the busy line must stay gone before the page counts as idle
BUSY_SCENARIO, BUSY_SEED = "worker_backlog", 202  # a demo seed server B has not seeded
ADVANCE_STEPS = (("+1 min", 60_000), ("+15 min", 15 * 60_000), ("+1 h", 60 * 60_000))
CREATE_SELECTOR = "section.panel.controls > button"
ADVANCE_SELECTOR = "section.panel.controls > span.advance > button:nth-of-type({position})"
DIAGNOSE_SELECTOR = "section.panel.diagnosis > div.row > button"
DIAGNOSE_LABEL = "Diagnose the latest incident"
CREATE_RUN = re.compile(r"/api/runs$")
JOB_POLL = re.compile(r"/api/runs/[^/?]+/diagnoses/job_[0-9a-f]+$")
CONTROL_JS = """(element) => {
  const active = document.activeElement;
  const onBody = !active || active === document.body;
  const style = getComputedStyle(element);
  return {
    focused: active === element, activeTag: onBody ? "body" : active.tagName.toLowerCase(),
    activeText: onBody ? "" : (active.textContent || "").trim().slice(0, 60),
    disabled: element.disabled, ariaDisabled: element.getAttribute("aria-disabled"),
    color: style.color, cursor: style.cursor, opacity: style.opacity,
    focusVisible: element.matches(":focus-visible"), outline: `${style.outlineStyle} ${style.outlineWidth}`,
  };
}"""
# Chromium moves focus off a newly disabled element at its next rendering update, not at once (run 4 of the record
# caught a disabled +15 min still focused); two frames make the read independent of that timing.
TWO_FRAMES_JS = "() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))"


def run_requests(run_id: str, route: str) -> re.Pattern[str]:
    return re.compile(rf"/api/runs/{re.escape(run_id)}/{route}$")


@dataclass
class RequestLog:
    """[X] Every request one page sends, so a second press that sends anything cannot go unseen."""

    sent: list[tuple[str, str]] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        page.on("request", lambda request: self.sent.append((request.method, request.url)))

    def count(self, method: str, path: re.Pattern[str]) -> int:
        return sum(1 for sent_method, url in self.sent if sent_method == method and path.search(urlsplit(url).path))


class HeldRequests:
    """[X] Holds the page's requests of one method to one route until released, so the request a control started is
    still running while the walkthrough looks at the control and presses it again. Other methods pass through."""

    def __init__(self, page: Page, method: str, path: re.Pattern[str]) -> None:
        self.page, self.method, self.path = page, method, path
        self.held: list[Route] = []
        self._handler = self._hold
        page.route(path, self._handler)

    def _hold(self, route: Route) -> None:
        if route.request.method != self.method:
            route.continue_()
            return
        self.held.append(route)

    def wait_for_one(self, what: str) -> None:
        deadline = time.monotonic() + EXPECT_TIMEOUT_MS / 1000
        while not self.held:
            if time.monotonic() > deadline:
                raise CheckFailed(f"{what}: no {self.method} {self.path.pattern} request within {EXPECT_TIMEOUT_MS} ms")
            self.page.wait_for_timeout(20)

    def take(self) -> list[Route]:
        """The requests held so far, for the caller to answer before stop()."""
        held, self.held = self.held, []
        return held

    def stop(self) -> None:
        """Later requests pass. Removing the page's last route lets a still-held request through on its own, and
        answering it after that fails ("Route is already handled"), so every held request is answered first."""
        self.page.unroute(self.path, self._handler)

    def release(self) -> None:
        for route in self.take():
            route.continue_()
        self.stop()


def ax_state(context: BrowserContext, page: Page, selector: str) -> dict:
    """[X] Chromium's accessibility node for the element a selector finds: its role, name and announced states."""
    session = context.new_cdp_session(page)
    try:
        root = session.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        node_id = session.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
        if not node_id:
            raise CheckFailed(f"no element matches {selector!r}")
        node = session.send("Accessibility.getPartialAXTree", {"nodeId": node_id, "fetchRelatives": False})["nodes"][0]
    finally:
        session.detach()
    states = {item["name"]: (item.get("value") or {}).get("value") for item in node.get("properties", [])}
    return {"role": (node.get("role") or {}).get("value"), "name": (node.get("name") or {}).get("value", ""),
            "disabled": states.get("disabled") is True, "focusable": states.get("focusable") is True}


def settle(page: Page) -> None:
    """[X] Waits until the busy line has stayed gone for QUIET_MS: the request and every reload it started are done."""
    deadline = time.monotonic() + SLOW_TIMEOUT_MS / 1000
    busy_line = page.locator("p.busy")
    while time.monotonic() < deadline:
        expect(busy_line).to_have_count(0)
        page.wait_for_timeout(QUIET_MS)
        if busy_line.count() == 0:
            return
    raise CheckFailed(f"the page did not stay idle for {QUIET_MS} ms within {SLOW_TIMEOUT_MS} ms")


@dataclass(frozen=True)
class BusyControl:
    """[X] One control that waits on a request: how to find it, and the request whose count must not grow."""

    label: str
    locator: Locator
    selector: str  # the same element for Chromium's accessibility tree
    method: str
    path: re.Pattern[str]


def press_while_busy(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, control: BusyControl,
                     held: HeldRequests, log: RequestLog, shot: str) -> int:
    """[X] Enter on the control starts its request, which `held` keeps running. Checks focus, what the control says
    and shows, and that Enter, Space and a mouse click send nothing more. Returns the count of requests before."""
    label = control.label
    sent_before = log.count(control.method, control.path)
    control.locator.focus()
    page.keyboard.press("Enter")
    held.wait_for_one(label)
    page.evaluate(TWO_FRAMES_JS)
    during = control.locator.evaluate(CONTROL_JS)
    node = ax_state(context, page, control.selector)
    busy_line = page.locator("p.busy").all_inner_texts()
    record.check(node["name"] == label, f"[X] {label}: the accessibility check reads the pressed control ({node['role']} {node['name']!r})")
    record.check(during["focused"], f"[X] {label}: while its request runs, focus stays on it (focus is on "
                 f"{during['activeTag']} {during['activeText']!r})")
    record.check(during["focused"] and during["focusVisible"] and not during["outline"].startswith("none")
                 and during["opacity"] == "1", f"[X] {label}: and shows its focus ring at full strength (:focus-visible "
                 f"{during['focusVisible']}, outline {during['outline']}, opacity {during['opacity']})")
    record.check(during["ariaDisabled"] == "true" and not during["disabled"],
                 f"[X] {label}: while busy it is aria-disabled, not disabled (aria-disabled {during['ariaDisabled']!r}, "
                 f"disabled {during['disabled']})")
    record.check(node["disabled"], f"[X] {label}: Chromium's accessibility tree announces it as unavailable "
                 f"(disabled {node['disabled']})")
    record.check(node["focusable"], f"[X] {label}: and keeps it focusable (focusable {node['focusable']})")
    record.check(during["color"] == DISABLED_LABEL_COLOR and during["cursor"] == "default",
                 f"[X] {label}: it looks unavailable as a disabled button does: label colour {during['color']}, cursor "
                 f"{during['cursor']} (a disabled button's: {DISABLED_LABEL_COLOR}, default)")
    record.check(len(busy_line) == 1 and busy_line[0].startswith("Working: "), f"[X] {label}: the page's busy line shows {busy_line}")
    walk.shot(record, page.locator("main"), shot)
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    control.locator.click(force=True)  # force: a busy control is not actionable to Playwright, which is the point
    page.wait_for_timeout(SETTLE_MS)
    sent = log.count(control.method, control.path) - sent_before
    record.check(sent == 1, f"[X] {label}: Enter, Space and a mouse click while it is busy send no second request "
                 f"({sent} {control.method} {control.path.pattern} sent)")
    return sent_before


def check_after_request(record: StepRecord, control: BusyControl, log: RequestLog, sent_before: int) -> None:
    """[X] After the control's request and its reloads: focus, availability, and one request for one press."""
    label = control.label
    expect(control.locator).to_be_enabled()
    after = control.locator.evaluate(CONTROL_JS)
    record.check(after["focused"], f"[X] {label}: after its request completes, focus is on it (focus is on "
                 f"{after['activeTag']} {after['activeText']!r}; :focus-visible {after['focusVisible']})")
    record.check(after["ariaDisabled"] is None and not after["disabled"] and after["color"] != DISABLED_LABEL_COLOR,
                 f"[X] {label}: and it is available again (aria-disabled {after['ariaDisabled']!r}, disabled "
                 f"{after['disabled']}, label colour {after['color']})")
    sent = log.count(control.method, control.path) - sent_before
    record.check(sent == 1, f"[X] {label}: one press sent exactly one {control.method} ({sent})")


def check_create(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, log: RequestLog) -> str:
    controls = page.locator("section.panel.controls")
    create = BusyControl("Create synthetic run", controls.get_by_role("button", name="Create synthetic run", exact=True),
                         CREATE_SELECTOR, "POST", CREATE_RUN)
    page.get_by_label("Scenario").select_option(BUSY_SCENARIO)
    page.get_by_role("textbox", name="Seed", exact=True).fill(str(BUSY_SEED))  # "Open run" options also say "seed"
    settle(page)
    expect(create.locator).to_be_enabled()
    known = {run["run_id"] for run in get_json(page, "/api/runs")}
    held = HeldRequests(page, "POST", CREATE_RUN)
    sent_before = press_while_busy(walk, record, page, context, create, held, log, "ui_09a_create_busy.png")
    held.release()
    expect(page.locator("header.topbar")).to_contain_text("Run: run_", timeout=SLOW_TIMEOUT_MS)
    created = [run for run in get_json(page, "/api/runs") if run["run_id"] not in known]
    if len(created) != 1 or (created[0]["scenario"], created[0]["seed"]) != (BUSY_SCENARIO, BUSY_SEED):
        raise CheckFailed(f"expected one new {BUSY_SCENARIO} seed {BUSY_SEED} run, found {created}")
    run_id = created[0]["run_id"]
    expect(page.locator("header.topbar")).to_contain_text(f"Run: {run_id}")
    whole = get_json(page, queue_path(run_id, {}))
    wait_for_row_ids(page, queue_panel(page), [row["order_id"] for row in whole["orders"]], "the new run's queue")
    settle(page)
    check_after_request(record, create, log, sent_before)
    record.note(f"[X] Create synthetic run made {run_id} ({BUSY_SCENARIO}, seed {BUSY_SEED})")
    walk.shot(record, page.locator("main"), "ui_09b_create_done.png")
    return run_id


def check_advance(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, log: RequestLog,
                  run_id: str) -> None:
    controls = page.locator("section.panel.controls")
    lead = queue_panel(page).locator(":scope > p").first
    for position, (label, step_ms) in enumerate(ADVANCE_STEPS, start=1):
        control = BusyControl(label, controls.get_by_role("button", name=label, exact=True),
                              ADVANCE_SELECTOR.format(position=position), "POST", run_requests(run_id, "advance"))
        expect(control.locator).to_be_enabled()
        clock_before = get_json(page, run_path(run_id))["replay_clock_ms"]
        held = HeldRequests(page, "POST", control.path)
        sent_before = press_while_busy(walk, record, page, context, control, held, log, f"ui_09c_advance_{position}_busy.png")
        held.release()
        expect(lead).to_contain_text(f"as of replay time {replay_time(clock_before + step_ms)}")
        settle(page)
        check_after_request(record, control, log, sent_before)
        clock_after = get_json(page, run_path(run_id))["replay_clock_ms"]
        record.check(clock_after == clock_before + step_ms,
                     f"[X] {label}: the clock moved by one step, {clock_before} -> {clock_after} (+{clock_after - clock_before} ms)")

    # Two presses back to back, nothing held: the second must meet the busy control (or, before the fix, the body).
    control = BusyControl("+1 min", controls.get_by_role("button", name="+1 min", exact=True),
                          ADVANCE_SELECTOR.format(position=1), "POST", run_requests(run_id, "advance"))
    clock_before = get_json(page, run_path(run_id))["replay_clock_ms"]
    sent_before = log.count(control.method, control.path)
    control.locator.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")
    expect(lead).to_contain_text(f"as of replay time {replay_time(clock_before + 60_000)}")
    page.wait_for_timeout(SETTLE_MS)
    settle(page)
    sent = log.count(control.method, control.path) - sent_before
    clock_after = get_json(page, run_path(run_id))["replay_clock_ms"]
    record.check(sent == 1 and clock_after == clock_before + 60_000,
                 f"[X] +1 min: two Enter presses back to back send one request and move the clock once ({sent} sent; "
                 f"{clock_before} -> {clock_after})")
    after = control.locator.evaluate(CONTROL_JS)
    record.check(after["focused"], f"[X] +1 min: after the double press, focus is on it (focus is on {after['activeTag']} "
                 f"{after['activeText']!r})")
    walk.shot(record, page.locator("main"), "ui_09d_advance_done.png")


def check_diagnose(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, log: RequestLog) -> str:
    """[X] Diagnose is busy from the start request's answer until a poll says the job is done; the poll is held."""
    target = next((run for run in get_json(page, "/api/runs") if run["seed"] == 101 and run["incident_ids"]), None)
    if target is None:
        raise CheckFailed("server B has no seed-101 run with an incident (step 7b creates it)")
    run_id = target["run_id"]
    page.get_by_label("Open run").select_option(run_id)
    expect(page.locator("header.topbar")).to_contain_text(f"Run: {run_id}")
    settle(page)
    panel = page.locator("section.panel.diagnosis")
    control = BusyControl(DIAGNOSE_LABEL, panel.get_by_role("button", name=DIAGNOSE_LABEL, exact=True), DIAGNOSE_SELECTOR,
                          "POST", run_requests(run_id, "diagnoses"))
    expect(control.locator).to_be_enabled()
    held = HeldRequests(page, "GET", JOB_POLL)
    sent_before = press_while_busy(walk, record, page, context, control, held, log, "ui_09e_diagnose_busy.png")
    held.release()
    expect(panel.locator("p", has_text="Job job_").locator("strong")).to_have_text("done", timeout=SLOW_TIMEOUT_MS)
    settle(page)
    check_after_request(record, control, log, sent_before)
    walk.shot(record, page.locator("main"), "ui_09f_diagnose_done.png")
    return run_id


def step_busy_controls(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, token: str,
                       log: RequestLog) -> None:
    log_in(page, SERVER_B, token)
    run_id = check_create(walk, record, page, context, log)
    check_advance(walk, record, page, context, log, run_id)
    walk.shared["x_diagnosed_run"] = check_diagnose(walk, record, page, context, log)
    no_error_banner(record, page, "[X] after the busy-control presses")


def step_start_request_gap(walk: Walkthrough, record: StepRecord, page: Page, log: RequestLog) -> None:
    """[X] Found, not changed: Diagnose is not busy while its start request is in flight, so a second press there sends
    a second start. The server would refuse it (409 DiagnosisBusyError); here every held start gets the first one's
    answer, so the page shows no error."""
    run_id = walk.shared["x_diagnosed_run"]
    panel = page.locator("section.panel.diagnosis")
    button = panel.get_by_role("button", name=DIAGNOSE_LABEL, exact=True)
    expect(button).to_be_enabled()
    starts = run_requests(run_id, "diagnoses")
    sent_before = log.count("POST", starts)
    held = HeldRequests(page, "POST", starts)
    button.focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the diagnosis start request")
    during = button.evaluate(CONTROL_JS)
    page.keyboard.press("Enter")
    page.wait_for_timeout(SETTLE_MS)
    sent = log.count("POST", starts) - sent_before
    first, *rest = held.take()
    with page.expect_response(lambda response: response.request.method == "POST" and starts.search(urlsplit(response.url).path)) as started:
        first.continue_()
    answer = started.value
    for route in rest:
        route.fulfill(status=answer.status, headers=answer.headers, body=answer.body())
    held.stop()
    expect(panel.locator("p", has_text="Job job_").locator("strong")).to_have_text("done", timeout=SLOW_TIMEOUT_MS)
    settle(page)
    record.note(f"[X] found, not changed: while the start request is in flight, Diagnose has aria-disabled "
                f"{during['ariaDisabled']!r} and disabled {during['disabled']}, and focus is on {during['activeTag']} "
                f"{during['activeText']!r}. A second Enter then sent {sent} start request(s) in all; the server answered "
                f"the first with {answer.status}")
    record.check(sent >= 1 and answer.status == 202, f"[X] the start-request probe ran: {sent} start request(s) sent, "
                 f"the first answered {answer.status}")
    walk.shared["start_requests_on_double_press"] = sent
    walk.shared["census_x"] = page.evaluate("() => window.__walkthroughCensus")
    no_error_banner(record, page, "[X] after the start-request probe")


# --------------------------------------------------------------------------- [Q33] owner decision Q33

# Checks starting "[Q33]" assert what the four approved fixes change, so each must fail on ca1971e. Checks starting
# "[Q33 guard]" assert what the fixes must not change (focus the user moved elsewhere stays there; Diagnose stays
# available during requests that are not its own), so they pass on both builds by design.
Q33_BAD_TOKEN = "q33-not-the-operator-token"  # synthetic; no server prints it, so the login is refused
FLOW_A_SEEDS = (303, 404)  # worker_backlog seeds server B has not seeded: the first for the fixes, the second for guards
FLOW_BC_SEED = 101
LOGIN_TOKEN_FIELD = "section.panel.login input[type=password]"
LOGIN_OPERATOR_FIELD = "section.panel.login input:not([type=password])"
RECOVERY_HEADING = "section.panel.recovery > h2"
PLAN_HEADING = "section.panel.recovery > div.plan > h3"
RECOVERY_SCRIPT_TARGET = 'section.panel.recovery [tabindex="-1"]'  # a heading or line that takes focus from script
FIX_SELECT = "section.panel.recovery select"
MODE_SELECT = "section.panel.diagnosis select"
DIAGNOSE_BUTTON = "section.panel.diagnosis > div.row > button"
FOCUS_JS = """(selectors) => {
  const active = document.activeElement;
  const onBody = !active || active === document.body;
  const style = onBody ? null : getComputedStyle(active);
  const matched = {};
  for (const [name, selector] of Object.entries(selectors)) matched[name] = !onBody && active.matches(selector);
  return {
    tag: onBody ? "body" : active.tagName.toLowerCase(),
    text: onBody ? "" : (active.textContent || "").trim().slice(0, 80),
    matched, focusVisible: !onBody && active.matches(":focus-visible"),
    outline: style === null ? "none" : `${style.outlineStyle} ${style.outlineWidth}`,
    tabindex: onBody ? null : active.getAttribute("tabindex"),
  };
}"""
MARK_REGION_JS = """() => {
  const region = document.querySelector('[role="status"]');
  if (region === null) return false;
  region.__q33Region = true;
  window.__q33RegionMutations = [];
  window.__q33RegionTexts = [];
  new MutationObserver((records) => {
    window.__q33RegionTexts.push(region.textContent);  // what a screen reader is given, once per change
    for (const record of records) {
      window.__q33RegionMutations.push({
        type: record.type, target: record.target.nodeName, attribute: record.attributeName,
        added: [...record.addedNodes].map((node) => node.nodeName),
        removed: [...record.removedNodes].map((node) => node.nodeName),
      });
    }
  }).observe(region, { childList: true, subtree: true, characterData: true, attributes: true });
  return true;
}"""
REGION_JS = """() => {
  const regions = [...document.querySelectorAll('[role="status"]')];
  const region = regions[0] ?? null;
  const main = document.querySelector("main");
  return {
    count: regions.length,
    tag: region === null ? null : region.tagName.toLowerCase(),
    className: region === null ? null : region.className,
    ariaLive: region === null ? null : region.getAttribute("aria-live"),
    text: region === null ? null : region.textContent,
    sameAsMarked: region !== null && region.__q33Region === true,
    isBusyLine: region !== null && document.querySelector("p.busy") === region,
    busyLines: document.querySelectorAll("p.busy").length,
    height: region === null ? null : region.getBoundingClientRect().height,
    mainTop: main === null ? null : main.getBoundingClientRect().top,
    mutations: window.__q33RegionMutations ?? null,
  };
}"""
MAIN_TOP_JS = """() => document.querySelector("main")?.getBoundingClientRect().top ?? null"""


def ax_named(context: BrowserContext, page: Page, role: str, name: str) -> dict:
    """[Q33] Chromium's accessibility node for the one element with this role and accessible name."""
    session = context.new_cdp_session(page)
    try:
        root = session.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        nodes = session.send("Accessibility.queryAXTree", {"nodeId": root, "accessibleName": name, "role": role})["nodes"]
    finally:
        session.detach()
    shown = [node for node in nodes if not node.get("ignored")]
    if len(shown) != 1:
        raise CheckFailed(f"expected one {role} named {name!r} in Chromium's accessibility tree, found {len(shown)}")
    states = {item["name"]: (item.get("value") or {}).get("value") for item in shown[0].get("properties", [])}
    return {"disabled": states.get("disabled") is True, "focusable": states.get("focusable") is True,
            "focused": states.get("focused") is True}


def ax_node(context: BrowserContext, page: Page, selector: str) -> dict | None:
    """[Q33] Chromium's accessibility node for the first element a selector finds: role, ignored and every property."""
    session = context.new_cdp_session(page)
    try:
        root = session.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        node_id = session.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
        if not node_id:
            return None
        node = session.send("Accessibility.getPartialAXTree", {"nodeId": node_id, "fetchRelatives": False})["nodes"][0]
    finally:
        session.detach()
    return {"role": (node.get("role") or {}).get("value"), "ignored": bool(node.get("ignored")),
            "properties": {item["name"]: (item.get("value") or {}).get("value") for item in node.get("properties", [])}}


LEFTOVER_HOLDS: list[HeldRequests] = []


def hold(page: Page, method: str, path: re.Pattern[str]) -> HeldRequests:
    """[Q33] X's HeldRequests, remembered so that a step that fails while holding a request can let it go."""
    held = HeldRequests(page, method, path)
    LEFTOVER_HOLDS.append(held)
    return held


def releasing_holds(body: Callable[[], None]) -> None:
    """[Q33] Runs a step; afterwards, any request a failed step left held goes on to the server, so no later step
    waits on it. A step that finished has answered all of its held requests already."""
    try:
        body()
    finally:
        while LEFTOVER_HOLDS:
            held = LEFTOVER_HOLDS.pop()
            for route in held.take():
                route.continue_()
            held.stop()


def path_matcher(method: str, path: re.Pattern[str]) -> Callable[[Response], bool]:
    return lambda response: response.request.method == method and bool(path.search(urlsplit(response.url).path))


def release_once(page: Page, held: HeldRequests, method: str, path: re.Pattern[str]) -> Response:
    """[Q33] The first held request goes on to the server. Any later one (only a build that takes a second press sends
    them) is answered in the browser with a copy of the first's answer, as step 9g did, so the server sees one request
    on either build and both runs go on from the same server state."""
    first, *rest = held.take()
    with page.expect_response(path_matcher(method, path)) as answered:
        first.continue_()
    answer = answered.value
    for route in rest:
        route.fulfill(status=answer.status, headers=answer.headers, body=answer.body())
    held.stop()
    return answer


@dataclass(frozen=True)
class OwnRequestControl:
    """[Q33] A control whose own request must make it refuse a second press: how to find it, and its request."""

    label: str  # how the checks name it
    locator: Locator
    method: str
    path: re.Pattern[str]  # the request's URL path, for HeldRequests and the request log


def press_own_request(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext,
                      control: OwnRequestControl, log: RequestLog, shot: str) -> tuple[int, HeldRequests]:
    """[Q33] Enter on the control sends its request, which is held. While it is held: the control is busy and keeps
    focus, and Enter, Space and a mouse click send nothing more. Returns the request count before, and the hold."""
    label = control.label
    name = " ".join(control.locator.inner_text().split())
    sent_before = log.count(control.method, control.path)
    held = hold(page, control.method, control.path)
    control.locator.focus()
    page.keyboard.press("Enter")
    held.wait_for_one(label)
    page.evaluate(TWO_FRAMES_JS)
    during = control.locator.evaluate(CONTROL_JS)
    node = ax_named(context, page, "button", name)
    record.check(during["ariaDisabled"] == "true" and not during["disabled"] and node["disabled"] and node["focusable"]
                 and during["focused"],
                 f"[Q33] {label}: while its own request runs it is busy and keeps focus: aria-disabled "
                 f"{during['ariaDisabled']!r}, disabled {during['disabled']}; Chromium: disabled {node['disabled']}, "
                 f"focusable {node['focusable']}; focus is on {during['activeTag']} {during['activeText'][:40]!r}")
    record.check(during["color"] == DISABLED_LABEL_COLOR and during["cursor"] == "default" and during["focusVisible"]
                 and not during["outline"].startswith("none") and during["opacity"] == "1",
                 f"[Q33] {label}: it looks unavailable as a disabled button does (label colour {during['color']}, cursor "
                 f"{during['cursor']}), with its focus ring at full strength (:focus-visible {during['focusVisible']}, "
                 f"outline {during['outline']}, opacity {during['opacity']})")
    walk.shot(record, page, shot)
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    control.locator.click(force=True)  # force: a busy control is not actionable to Playwright, which is the point
    page.wait_for_timeout(SETTLE_MS)
    sent = log.count(control.method, control.path) - sent_before
    still = control.locator.evaluate(CONTROL_JS)
    record.check(sent == 1 and still["focused"],
                 f"[Q33] {label}: Enter, Space and a mouse click while its request runs send no second request, and "
                 f"focus stays on it ({sent} {control.method} {control.path.pattern} sent; focus is on "
                 f"{still['activeTag']} {still['activeText'][:40]!r})")
    return sent_before, held


def check_own_request_after(record: StepRecord, control: OwnRequestControl, log: RequestLog, sent_before: int, *,
                            stays: bool) -> None:
    """[Q33] After the request and what it reloads: one request for one press, and a control that stays is available
    again with focus on it. A control that goes away is checked for where focus went instead."""
    sent = log.count(control.method, control.path) - sent_before
    if not stays:
        record.check(sent == 1, f"[Q33] {control.label}: one press sent exactly one {control.method} ({sent})")
        return
    after = control.locator.evaluate(CONTROL_JS)
    record.check(sent == 1 and after["focused"] and after["ariaDisabled"] is None and not after["disabled"]
                 and after["color"] != DISABLED_LABEL_COLOR,
                 f"[Q33] {control.label}: after its request, one press sent exactly one {control.method} ({sent}), focus "
                 f"is on it ({after['activeTag']} {after['activeText'][:40]!r}) and it is available again (aria-disabled "
                 f"{after['ariaDisabled']!r}, disabled {after['disabled']}, label colour {after['color']})")


def check_focus_lands(record: StepRecord, page: Page, what: str, target: str, selector: str, prefix: str,
                      tag: str = "Q33") -> dict:
    """[Q33] Fix 3: where focus goes when the control that held it went away or was disabled."""
    page.evaluate(TWO_FRAMES_JS)
    focus = page.evaluate(FOCUS_JS, {"target": selector})
    landed = focus["matched"]["target"] and focus["text"].startswith(prefix)
    record.check(landed, f"[{tag}] {what}: focus moves to {target}, not to the page body (it is on {focus['tag']} "
                 f"{focus['text'][:48]!r}, tabindex {focus['tabindex']!r})")
    record.check(landed and focus["focusVisible"] and not focus["outline"].startswith("none"),
                 f"[{tag}] {what}: and {target} shows its focus ring (:focus-visible {focus['focusVisible']}, outline "
                 f"{focus['outline']})")
    return focus


def viewport_note(record: StepRecord, what: str, places: dict[str, Locator]) -> None:
    """[Q33] Where the focus target and the result of the request sit in the viewport after focus moved (a note)."""
    shown = {}
    for name, locator in places.items():
        box = locator.first.bounding_box() if locator.count() else None
        shown[name] = None if box is None else {
            "top": round(box["y"]), "bottom": round(box["y"] + box["height"]),
            "in view": box["y"] >= 0 and box["y"] + box["height"] <= VIEWPORT["height"]}
    record.note(f"[Q33] {what}, viewport {VIEWPORT['height']} px high: {shown}")


def tab_path_note(record: StepRecord, page: Page, what: str, until: re.Pattern[str], presses: int = 6) -> None:
    """[Q33] Tab presses from wherever focus is now until a control whose text matches `until` (a note)."""
    stops = []
    for _ in range(presses):
        page.keyboard.press("Tab")
        stop = page.evaluate(FOCUS_JS, {})
        stops.append(f"{stop['tag']} {stop['text'][:30]!r}")
        if until.match(stop["text"]):
            break
    record.note(f"[Q33] {what}: Tab stops {stops}")


def check_focus_stays(record: StepRecord, page: Page, what: str, other: str, selector: str, tag: str = "Q33") -> None:
    """[Q33 guard] Fix 3 never steals focus: focus the user moved before the request finished stays there."""
    page.evaluate(TWO_FRAMES_JS)
    focus = page.evaluate(FOCUS_JS, {"other": selector})
    record.check(focus["matched"]["other"],
                 f"[{tag} guard] {what}: focus the user moved to {other} before the request finished stays there (it "
                 f"is on {focus['tag']} {focus['text'][:48]!r})")


def check_not_busy(record: StepRecord, page: Page, context: BrowserContext, request: str) -> None:
    """[Q33 guard] Fix 1 must not grey Diagnose out during a request that is not its own."""
    diagnose = page.locator(DIAGNOSE_BUTTON)
    state = diagnose.evaluate(CONTROL_JS)
    node = ax_named(context, page, "button", DIAGNOSE_LABEL)
    busy_line = page.locator("p.busy").all_inner_texts()
    record.check(bool(busy_line) and state["ariaDisabled"] is None and not state["disabled"] and not node["disabled"]
                 and state["color"] != DISABLED_LABEL_COLOR,
                 f"[Q33 guard] Diagnose stays available while {request} runs (the busy line says {busy_line}): "
                 f"aria-disabled {state['ariaDisabled']!r}, disabled {state['disabled']}, Chromium disabled "
                 f"{node['disabled']}, label colour {state['color']}")


def open_run_and_settle(page: Page, run_id: str) -> None:
    page.get_by_label("Open run").select_option(run_id)
    expect(page.locator("header.topbar")).to_contain_text(f"Run: {run_id}")
    settle(page)


def create_run_in_ui(page: Page, scenario: str, seed: int) -> str:
    """[Q33] Create synthetic run, as an operator would; returns the new run's ID once the page shows it."""
    page.get_by_label("Scenario").select_option(scenario)
    page.get_by_role("textbox", name="Seed", exact=True).fill(str(seed))  # "Open run" options also say "seed"
    settle(page)
    create = page.locator("section.panel.controls").get_by_role("button", name="Create synthetic run", exact=True)
    with page.expect_response(path_matcher("POST", CREATE_RUN), timeout=SLOW_TIMEOUT_MS) as created:
        create.click()
    if created.value.status != 201:
        raise CheckFailed(f"creating {scenario} seed {seed} answered {created.value.status}")
    run_id = created.value.json()["run_id"]
    expect(page.locator("header.topbar")).to_contain_text(f"Run: {run_id}", timeout=SLOW_TIMEOUT_MS)
    settle(page)
    return run_id


def diagnose_in_ui(page: Page) -> str:
    """[Q33] One mouse press of Diagnose; returns the job ID once its job is done and the page is idle."""
    panel = page.locator("section.panel.diagnosis")
    with page.expect_response(path_matcher("POST", re.compile(r"/api/runs/[^/]+/diagnoses$"))) as started:
        panel.get_by_role("button", name=DIAGNOSE_LABEL, exact=True).click()
    job_id = started.value.json()["job_id"]
    expect(panel.locator("p", has_text=f"Job {job_id} ").locator("strong")).to_have_text("done", timeout=SLOW_TIMEOUT_MS)
    settle(page)
    return job_id


def proposed_operation(page: Page) -> str:
    return page.locator("section.panel.recovery > p").first.locator("strong").inner_text()


class ResponseRewrite:
    """[Q33] Rewrites one route's GET answers in the browser: the server answers, `change` edits the JSON."""

    def __init__(self, page: Page, path: re.Pattern[str], change: Callable[[dict], bool]) -> None:
        self.page, self.path, self.change = page, path, change
        self.rewritten = 0
        page.route(path, self._rewrite)

    def _rewrite(self, route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        body = response.json()
        if self.change(body):
            self.rewritten += 1
        route.fulfill(response=response, json=body)

    def stop(self) -> None:
        self.page.unroute(self.path, self._rewrite)


def no_candidates(state: dict) -> bool:
    state["refresh_candidates"] = []
    return True


def propose_replay(job: dict) -> bool:
    diagnosis = job.get("diagnosis") or {}
    if job.get("status") != "done" or not diagnosis.get("output"):
        return False
    diagnosis["output"]["proposed_operation"] = "prepare_provider_replay"
    return True


def step_q33_login_region_diagnose(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext,
                                   token: str, log: RequestLog) -> None:
    """[Q33] Fix 3 for Start session, fix 4 (the live region) and fix 1 (Diagnose), on server B."""
    login = page.locator("section.panel.login")
    start = login.get_by_role("button", name="Start session")
    token_field = login.get_by_label("Operator token")
    operator_field = login.get_by_label("Operator ID (op_...)")
    page.goto(f"{SERVER_B}/")
    expect(page.get_by_role("heading", name="Operator login")).to_be_visible()

    # Start session, pressed from the keyboard, with a token the server refuses.
    token_field.fill(Q33_BAD_TOKEN)
    operator_field.fill(OPERATOR_ID)
    start.focus()
    with page.expect_response(path_matcher("POST", re.compile(r"^/api/session$"))) as refused:
        page.keyboard.press("Enter")
    expect(page.locator("p.banner.error")).to_contain_text("log in failed")
    settle(page)
    focus = check_focus_lands(record, page, "Start session, after a failed login (HTTP "
                              f"{refused.value.status}; the submit cleared the token, which disabled the button)",
                              "the Operator token field", LOGIN_TOKEN_FIELD, "")
    record.note(f"[Q33] after the failed login: Start session disabled {start.is_disabled()}, token field value "
                f"{token_field.input_value()!r}, focus on {focus['tag']}")
    walk.shot(record, page, "ui_10a_failed_login_focus.png")

    # The same refusal, submitted with Enter in the Operator ID field: focus is not the button's to move.
    token_field.fill(Q33_BAD_TOKEN)
    operator_field.focus()
    with page.expect_response(path_matcher("POST", re.compile(r"^/api/session$"))):
        page.keyboard.press("Enter")
    expect(page.locator("p.banner.error")).to_contain_text("log in failed")
    settle(page)
    check_focus_stays(record, page, "Start session, a failed login submitted from the Operator ID field",
                      "the Operator ID field", LOGIN_OPERATOR_FIELD)
    log_in(page, SERVER_B, token)
    walk.shared["q33_logged_in"] = True
    settle(page)
    after_login = page.evaluate(FOCUS_JS, {})
    walk.shared["q33_focus_after_login"] = after_login["tag"]
    record.note(f"[Q33] found, left alone: after a successful login the login panel is gone and focus is on "
                f"{after_login['tag']} {after_login['text'][:40]!r} (not one of the brief's cases)")
    no_error_banner(record, page, "[Q33 guard] after the refused logins and a good one")

    # Fix 4: the busy line is a live region that stays in the page.
    run_id = walk.shared["x_diagnosed_run"]
    open_run_and_settle(page, run_id)
    record.note(f"[Q33] page B run {run_id}; the busy line before any check: {page.evaluate(REGION_JS)}")
    marked = page.evaluate(MARK_REGION_JS)
    idle = page.evaluate(REGION_JS)
    record.check(marked and idle["count"] == 1 and idle["tag"] == "p" and idle["text"] == "" and idle["height"] == 0
                 and idle["busyLines"] == 0,
                 f"[Q33] the busy line is one role=status element that stays in the page while idle, empty and taking "
                 f"no space: {idle['count']} role=status elements, <{idle['tag']}>, text {idle['text']!r}, height "
                 f"{idle['height']}, p.busy lines {idle['busyLines']}")
    node = ax_node(context, page, '[role="status"]')
    record.check(node is not None and node["role"] == "status" and node["properties"].get("live") == "polite"
                 and node["properties"].get("atomic") is True,
                 f"[Q33] Chromium exposes it as a polite live region: {node}")
    walk.shared["q33_region_idle"] = {"region": idle, "ax": node}

    # An unrelated request (the queue reloads for another filter): the region speaks; Diagnose is not busy.
    panel = queue_panel(page)
    held = hold(page, "GET", re.compile(rf"/api/runs/{re.escape(run_id)}/orders\?"))
    show_select(panel).select_option("captured_unprocessed")
    held.wait_for_one("the queue reload")
    page.evaluate(TWO_FRAMES_JS)
    busy = page.evaluate(REGION_JS)
    record.check(busy["sameAsMarked"] and busy["isBusyLine"] and busy["text"] == "Working: load order queue...",
                 f"[Q33] while a request runs, the same element carries the text and is the page's busy line: same "
                 f"element {busy['sameAsMarked']}, p.busy {busy['isBusyLine']}, text {busy['text']!r}")
    walk.shared["q33_main_top"] = {"idle": idle["mainTop"], "busy": busy["mainTop"]}
    check_not_busy(record, page, context, "the queue reload (load order queue)")
    walk.shot(record, page, "ui_10b_queue_reload_region_and_diagnose.png")
    held.release()
    settle(page)
    after = page.evaluate(REGION_JS)
    kinds = sorted({(item["type"], item["target"], tuple(item["added"]), tuple(item["removed"]), item["attribute"])
                    for item in after["mutations"] or []})
    elements_changed = [item for item in after["mutations"] or []
                        if any(name != "#text" for name in item["added"] + item["removed"])]
    record.check(after["sameAsMarked"] and after["text"] == "" and after["busyLines"] == 0 and after["height"] == 0
                 and after["mutations"] is not None and not elements_changed,
                 f"[Q33] after the request the same element is still in the page and empty, and only text changed "
                 f"inside it: same element {after['sameAsMarked']}, text {after['text']!r}, height {after['height']}; "
                 f"mutations {kinds}")
    show_select(panel).select_option("all")
    settle(page)

    # A second unrelated request: a case loads.
    held = hold(page, "GET", re.compile(rf"/api/runs/{re.escape(run_id)}/orders/[^/?]+\?as_of_ms="))
    first_order = panel.locator(":scope > div.scroll tbody tr:first-child button")
    first_order.click()
    held.wait_for_one("the case load")
    page.evaluate(TWO_FRAMES_JS)
    check_not_busy(record, page, context, "a case load (load order case)")
    held.release()
    settle(page)
    close_case(panel)
    settle(page)

    # Fix 1: Diagnose is busy during its own start request, before any job exists.
    page.evaluate("() => { if (window.__q33RegionTexts) window.__q33RegionTexts = []; }")
    diagnose = OwnRequestControl(DIAGNOSE_LABEL, page.locator(DIAGNOSE_BUTTON), "POST", run_requests(run_id, "diagnoses"))
    sent_before, held = press_own_request(walk, record, page, context, diagnose, log, "ui_10c_diagnose_start_busy.png")
    region = page.evaluate(REGION_JS)
    record.check(region["sameAsMarked"] and region["text"] == "Working: start diagnosis...",
                 f"[Q33] the live region says {region['text']!r} during the start request (same element "
                 f"{region['sameAsMarked']})")
    answer = release_once(page, held, "POST", diagnose.path)
    job_id = answer.json()["job_id"]
    expect(page.locator("section.panel.diagnosis p", has_text=f"Job {job_id} ").locator("strong")).to_have_text(
        "done", timeout=SLOW_TIMEOUT_MS)
    settle(page)
    check_own_request_after(record, diagnose, log, sent_before, stays=True)
    texts = page.evaluate("() => window.__q33RegionTexts ?? null")
    spoken = None if texts is None else [item for item in texts if item]
    walk.shared["q33_region_texts_one_diagnosis"] = spoken
    record.note(f"[Q33] the live region's non-empty texts from the Diagnose press until the job is done and the page "
                f"idle (rules mode), one announcement each: {spoken}")

    # Fix 1, measured as step 9g measured it: Enter, wait until the start request is in flight, Enter again.
    starts = run_requests(run_id, "diagnoses")
    sent_before = log.count("POST", starts)
    held = hold(page, "POST", starts)
    diagnose.locator.focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the diagnosis start request")
    page.keyboard.press("Enter")
    page.wait_for_timeout(SETTLE_MS)
    sent = log.count("POST", starts) - sent_before
    answer = release_once(page, held, "POST", starts)
    job_id = answer.json()["job_id"]
    expect(page.locator("section.panel.diagnosis p", has_text=f"Job {job_id} ").locator("strong")).to_have_text(
        "done", timeout=SLOW_TIMEOUT_MS)
    settle(page)
    walk.shared["q33_start_requests_on_double_press"] = sent
    record.check(sent == 1, f"[Q33] Diagnose: a quick double press while the start request is in flight sends "
                 f"{sent} start request(s); one is right (step 9g measured 2 on ca1971e)")
    no_error_banner(record, page, "[Q33 guard] after the live-region and Diagnose checks")


def step_q33_flow_a(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, log: RequestLog,
                    demo: DemoFacts) -> None:
    """[Q33] Fixes 2 and 3 for the reprocess controls, on two fresh worker_backlog runs."""
    recovery = page.locator("section.panel.recovery")
    plan = recovery.locator(":scope > div.plan")
    stale_row = recovery.locator(":scope > div.row", has_text="excluded only because their provider state is stale")

    def prepare(seed: int) -> str:
        run_id = create_run_in_ui(page, "worker_backlog", seed)
        diagnose_in_ui(page)
        if proposed_operation(page) != "reprocess_inbox":
            raise CheckFailed(f"worker_backlog seed {seed} proposes {proposed_operation(page)!r}, not reprocess_inbox")
        recovery.locator("select").select_option(demo.passing_fix_id)
        return run_id

    run_id = prepare(FLOW_A_SEEDS[0])
    validate = OwnRequestControl("Validate this corrected handler",
                                 recovery.get_by_role("button", name="Validate this corrected handler", exact=True),
                                 "POST", run_requests(run_id, r"fixes/[^/]+/validate"))
    sent_before, held = press_own_request(walk, record, page, context, validate, log, "ui_11a_validate_busy.png")
    release_once(page, held, validate.method, validate.path)
    expect(recovery.locator("p", has_text=f"{demo.passing_fix_id}: reproduced on the deployed handler")).to_contain_text("passed")
    settle(page)
    check_own_request_after(record, validate, log, sent_before, stays=True)

    build = OwnRequestControl("Build reprocessing plan",
                              recovery.get_by_role("button", name="Build reprocessing plan", exact=True),
                              "POST", run_requests(run_id, "plans"))
    sent_before, held = press_own_request(walk, record, page, context, build, log, "ui_11b_build_busy.png")
    release_once(page, held, build.method, build.path)
    expect(plan.locator(":scope > h3")).to_contain_text("Plan plan_")
    settle(page)
    check_own_request_after(record, build, log, sent_before, stays=True)
    first_plan = plan.locator(":scope > h3").inner_text().removeprefix("Plan ").strip()
    if stale_row.count() != 1:
        raise CheckFailed(f"plan {first_plan} lists no stale-only orders, so the stale-orders row cannot be shown")

    stale = OwnRequestControl("Refresh provider state (read-only) and re-plan",
                              stale_row.get_by_role("button", name="Refresh provider state (read-only) and re-plan"),
                              "POST", run_requests(run_id, "refresh"))
    sent_before, held = press_own_request(walk, record, page, context, stale, log, "ui_11c_stale_refresh_busy.png")
    release_once(page, held, stale.method, stale.path)
    expect(plan).to_contain_text(f"Replaces draft {first_plan}")
    settle(page)
    if stale_row.count() != 0:
        raise CheckFailed("the re-plan still lists stale-only orders, so the row does not go away")
    check_own_request_after(record, stale, log, sent_before, stays=False)
    check_focus_lands(record, page, "Refresh provider state (read-only) and re-plan, once the new plan has no "
                      "stale-only orders and its row is gone", "the new plan's heading", PLAN_HEADING, "Plan plan_")
    viewport_note(record, "after the stale-orders row went", {
        "plan heading (focused)": plan.locator(":scope > h3"),
        "Replaces draft line": plan.locator(":scope > p", has_text="Replaces draft"),
        "Approve button": recovery.get_by_role("button", name=re.compile(r"^Approve plan "))})
    walk.shot(record, page, "ui_11d_stale_row_gone_plan_heading.png")

    approve = OwnRequestControl("Approve plan", recovery.get_by_role("button", name=re.compile(r"^Approve plan ")),
                                "POST", run_requests(run_id, r"plans/[^/]+/approve"))
    sent_before, held = press_own_request(walk, record, page, context, approve, log, "ui_11e_approve_busy.png")
    release_once(page, held, approve.method, approve.path)
    expect(recovery).to_contain_text(f"Approved by {OPERATOR_ID}")
    settle(page)
    check_own_request_after(record, approve, log, sent_before, stays=False)
    # [Q36-adjusted] Q33 asserted the plan heading here. Q36 moves Approve's focus target to the "Approved by" line,
    # which step 17 asserts; this check now asserts only what both targets share.
    check_focus_lands(record, page, "Approve plan, once the plan is approved and the button is gone [Q36-adjusted: "
                      "Q33 asserted the plan heading; Q36 moves focus to the 'Approved by' line, which step 17 asserts]",
                      "a script-focus target in the Recovery panel", RECOVERY_SCRIPT_TARGET, "")
    viewport_note(record, "after Approve went", {
        "plan heading": plan.locator(":scope > h3"),
        "Approved by line": recovery.locator(":scope > p", has_text="Approved by"),
        "Execute button": recovery.locator(":scope > button", has_text=re.compile(r"^Execute"))})
    tab_path_note(record, page, "from focus after Approve went to the Execute button", re.compile(r"^Execute"))
    walk.shot(record, page, "ui_11f_approved_focus.png")

    execute = OwnRequestControl("Execute", recovery.locator(":scope > button", has_text=re.compile(r"^Execute")),
                                "POST", run_requests(run_id, r"plans/[^/]+/execute"))
    sent_before, held = press_own_request(walk, record, page, context, execute, log, "ui_11g_execute_busy.png")
    release_once(page, held, execute.method, execute.path)
    expect(recovery.locator(":scope > ul.compact > li", has_text="Action act_").first).to_be_visible()
    settle(page)
    check_own_request_after(record, execute, log, sent_before, stays=True)
    record.note(f"[Q33] after Execute: the button reads {execute.locator.inner_text()!r}; "
                f"{recovery.locator(':scope > ul.compact > li', has_text='Action act_').count()} execution line(s)")
    no_error_banner(record, page, f"[Q33 guard] after the Flow A controls on {run_id}")

    # The no-steal guards need the stale-orders row and Approve again: a second fresh run.
    walk.shared["q33_flow_a_runs"] = (run_id, prepare(FLOW_A_SEEDS[1]))
    recovery.get_by_role("button", name="Validate this corrected handler", exact=True).click()
    expect(recovery.locator("p", has_text=f"{demo.passing_fix_id}: reproduced on the deployed handler")).to_contain_text("passed")
    settle(page)
    recovery.get_by_role("button", name="Build reprocessing plan", exact=True).click()
    expect(plan.locator(":scope > h3")).to_contain_text("Plan plan_")
    settle(page)
    if stale_row.count() != 1:
        raise CheckFailed("the guard run's plan lists no stale-only orders")
    guard_plan = plan.locator(":scope > h3").inner_text().removeprefix("Plan ").strip()
    refreshes = re.compile(r"/api/runs/[^/]+/refresh$")
    held = hold(page, "POST", refreshes)
    stale_row.get_by_role("button", name="Refresh provider state (read-only) and re-plan").focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the guard run's refresh")
    page.locator(FIX_SELECT).focus()
    release_once(page, held, "POST", refreshes)
    expect(plan).to_contain_text(f"Replaces draft {guard_plan}")
    settle(page)
    check_focus_stays(record, page, "Refresh provider state (read-only) and re-plan, its row gone",
                      "the Corrected handler select", FIX_SELECT)

    approvals = re.compile(r"/api/runs/[^/]+/plans/[^/]+/approve$")
    held = hold(page, "POST", approvals)
    recovery.get_by_role("button", name=re.compile(r"^Approve plan ")).focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the guard run's approval")
    page.locator(FIX_SELECT).focus()
    release_once(page, held, "POST", approvals)
    expect(recovery).to_contain_text(f"Approved by {OPERATOR_ID}")
    settle(page)
    check_focus_stays(record, page, "Approve plan, the button gone", "the Corrected handler select", FIX_SELECT)
    no_error_banner(record, page, "[Q33 guard] after the Flow A guards")


def step_q33_escalate(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, log: RequestLog) -> None:
    """[Q33] Fixes 2 and 3 for Escalate, on a method_degradation run (Flow B)."""
    recovery = page.locator("section.panel.recovery")
    run_id = create_run_in_ui(page, "method_degradation", FLOW_BC_SEED)
    diagnose_in_ui(page)
    if proposed_operation(page) != "escalate":
        raise CheckFailed(f"method_degradation proposes {proposed_operation(page)!r}, not escalate")
    check_rail(record, page, "Flow B, escalate proposed", ["Done"] + NOT_APPLICABLE,
               path="escalate: hand over with the evidence needed; no fix, plan, approval, execution or receipt applies",
               next_step="Escalate with the evidence needed in the Recovery card.",
               proofs={0: diagnose_proof(job_of_page(page, run_id))})
    escalate_label = "Escalate with the evidence needed (no reprocessing)"
    escalate = OwnRequestControl("Escalate", recovery.get_by_role("button", name=escalate_label, exact=True),
                                 "POST", run_requests(run_id, "plans"))
    sent_before, held = press_own_request(walk, record, page, context, escalate, log, "ui_12a_escalate_busy.png")
    escalated = release_once(page, held, escalate.method, escalate.path).json()
    expect(recovery).to_contain_text("Escalated as action act_")
    settle(page)
    check_own_request_after(record, escalate, log, sent_before, stays=False)
    check_focus_lands(record, page, "Escalate, once escalated and the button is gone", "the Recovery heading",
                      RECOVERY_HEADING, "Recovery")
    check_rail(record, page, "Flow B, escalated", ["Done"] + NOT_APPLICABLE,
               next_step=f"Escalated as action {escalated['action_id']} by {escalated['operator_id']}.")
    viewport_note(record, "after Escalate went", {
        "Recovery heading (focused)": recovery.locator(":scope > h2"),
        "Escalated as line": recovery.locator(":scope > p", has_text="Escalated as action")})
    walk.shot(record, page, "ui_12b_escalated_recovery_heading.png")

    diagnose_in_ui(page)  # a new diagnosis: a new Recovery panel, with Escalate again
    held = hold(page, "POST", escalate.path)
    escalate.locator.focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the second escalation")
    page.locator(MODE_SELECT).focus()
    release_once(page, held, escalate.method, escalate.path)
    expect(recovery).to_contain_text("Escalated as action act_")
    settle(page)
    check_focus_stays(record, page, "Escalate, the button gone", "the diagnosis Mode select", MODE_SELECT)
    no_error_banner(record, page, f"[Q33 guard] after Escalate on {run_id}")


def step_q33_flow_c(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, log: RequestLog) -> None:
    """[Q33] Refresh provider state for N payments (fixes 2 and 3), the remounted Recovery panel (fix 3) and Prepare
    provider replay instructions (fix 2), on a late_authorization run (Flow C)."""
    recovery = page.locator("section.panel.recovery")
    run_id = create_run_in_ui(page, "late_authorization", FLOW_BC_SEED)
    walk.shared["q33_flow_c_run"] = run_id
    diagnose_in_ui(page)
    if proposed_operation(page) != "refresh_provider_state":
        raise CheckFailed(f"late_authorization proposes {proposed_operation(page)!r}, not refresh_provider_state")
    check_rail(record, page, "Flow C, refresh proposed", ["Done"] + NOT_APPLICABLE,
               path="refresh_provider_state: a read-only refresh; no fix, plan, approval, execution or receipt applies",
               next_step="Refresh provider state (read-only) in the Recovery card.")
    refresh_button = recovery.locator(":scope > div.row > button", has_text=re.compile(r"^Refresh provider state for \d+ "))
    refresh = OwnRequestControl("Refresh provider state for N payments", refresh_button, "POST",
                                run_requests(run_id, "refresh"))
    payment_state = f"{run_path(run_id)}/payment-state"
    candidates_before = len(get_json(page, payment_state)["refresh_candidates"])
    sent_before, held = press_own_request(walk, record, page, context, refresh, log, "ui_13a_refresh_busy.png")
    refreshed = release_once(page, held, refresh.method, refresh.path).json()
    expect(recovery.locator(":scope > div.row > span")).to_contain_text("Refreshed")
    settle(page)
    check_own_request_after(record, refresh, log, sent_before, stays=True)
    check_rail(record, page, "Flow C, refreshed", ["Done"] + NOT_APPLICABLE,
               next_step=f"Refreshed {count_of(len(refreshed['refreshed']), 'payment')} at replay time "
                         f"{replay_time(refreshed['now_ms'])}.")
    candidates_after = len(get_json(page, payment_state)["refresh_candidates"])
    record.note(f"[Q33] a real refresh of {candidates_before} candidates leaves {candidates_after}: late-authorised "
                f"payments stay candidates, so in this world the button is never disabled by its own refresh")
    walk.shared["q33_refresh_candidates"] = (candidates_before, candidates_after)

    advance = page.locator("section.panel.controls").get_by_role("button", name="+1 min", exact=True)

    def reenable() -> None:
        advance.click()  # a reload that reads the real payment state again
        expect(refresh_button).to_be_enabled()
        settle(page)

    # The reload after the refresh answers no candidate (rewritten in the browser), which disables the button.
    rewrite = ResponseRewrite(page, run_requests(run_id, "payment-state"), no_candidates)
    refresh_button.focus()
    page.keyboard.press("Enter")
    expect(refresh_button).to_be_disabled()
    settle(page)
    rewrite.stop()
    record.note(f"[Q33] payment-state answers rewritten to no candidate: {rewrite.rewritten}")
    check_focus_lands(record, page, "Refresh provider state for N payments, once the reload after its request leaves no "
                      "candidate and it is disabled", "the Recovery heading", RECOVERY_HEADING, "Recovery")
    viewport_note(record, "after Refresh provider state for N payments was disabled", {
        "Recovery heading (focused)": recovery.locator(":scope > h2"),
        "the disabled button": refresh_button,
        "Refreshed line": recovery.locator(":scope > div.row > span")})
    walk.shot(record, page, "ui_13b_refresh_disabled_recovery_heading.png")
    reenable()

    rewrite = ResponseRewrite(page, run_requests(run_id, "payment-state"), no_candidates)
    held = hold(page, "POST", refresh.path)
    refresh_button.focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the guard refresh")
    page.locator(MODE_SELECT).focus()
    release_once(page, held, refresh.method, refresh.path)
    expect(refresh_button).to_be_disabled()
    settle(page)
    rewrite.stop()
    check_focus_stays(record, page, "Refresh provider state for N payments, disabled after its request",
                      "the diagnosis Mode select", MODE_SELECT)
    reenable()

    # A new diagnosis remounts the Recovery panel twice: at the start answer (no diagnosis yet) and when the job is done.
    starts = run_requests(run_id, "diagnoses")

    def rediagnose(move_focus_to: str) -> None:
        held_start = hold(page, "POST", starts)
        held_polls = hold(page, "GET", JOB_POLL)
        page.locator(DIAGNOSE_BUTTON).focus()
        page.keyboard.press("Enter")
        held_start.wait_for_one("the diagnosis start")
        page.locator(move_focus_to).focus()
        answer = release_once(page, held_start, "POST", starts)
        expect(recovery).to_contain_text("Run a diagnosis first.")
        walk.shared.setdefault("q33_remount_jobs", []).append(answer.json()["job_id"])
        if move_focus_to != MODE_SELECT:
            check_focus_lands(record, page, "the Recovery panel, replaced at the start answer while a control in it held "
                              "focus", "the new panel's heading", RECOVERY_HEADING, "Recovery")
        held_polls.wait_for_one("the first job poll")
        held_polls.release()
        expect(page.locator("section.panel.diagnosis p", has_text=f"Job {answer.json()['job_id']} ").locator("strong")
               ).to_have_text("done", timeout=SLOW_TIMEOUT_MS)
        expect(recovery).to_contain_text("Proposed operation: refresh_provider_state")
        settle(page)

    rediagnose("section.panel.recovery > div.row > button")
    check_focus_lands(record, page, "the Recovery panel, replaced again when the new diagnosis arrives",
                      "the new panel's heading", RECOVERY_HEADING, "Recovery")
    walk.shot(record, page, "ui_13c_remounted_recovery_heading.png")
    rediagnose(MODE_SELECT)
    check_focus_stays(record, page, "the Recovery panel, replaced twice by a new diagnosis",
                      "the diagnosis Mode select", MODE_SELECT)

    # Prepare provider replay instructions: no demo scenario's rules diagnosis proposes it, so the done job's proposal
    # is rewritten in the browser; the request itself goes to the server, which builds instructions from the run.
    rewrite = ResponseRewrite(page, JOB_POLL, propose_replay)
    diagnose_in_ui(page)
    rewrite.stop()
    record.note(f"[Q33] job answers rewritten to propose prepare_provider_replay: {rewrite.rewritten}")
    if proposed_operation(page) != "prepare_provider_replay":
        raise CheckFailed(f"the rewritten diagnosis shows {proposed_operation(page)!r}")
    replay_states = ["Done", NOT_APPLICABLE[0], "Current", *NOT_APPLICABLE[:3]]
    check_rail(record, page, "Flow C, replay proposed (rewritten in the browser)", replay_states,
               path="prepare_provider_replay: instructions only, no provider call; no fix, approval, execution or "
                    "receipt applies",
               next_step="Prepare provider replay instructions (no provider call) in the Recovery card.")
    prepare_label = "Prepare provider replay instructions (no provider call)"
    prepare = OwnRequestControl("Prepare provider replay instructions",
                                recovery.get_by_role("button", name=prepare_label, exact=True),
                                "POST", run_requests(run_id, "plans"))
    sent_before, held = press_own_request(walk, record, page, context, prepare, log, "ui_13d_prepare_busy.png")
    answer = release_once(page, held, prepare.method, prepare.path)
    settle(page)
    record.note(f"[Q33] the replay request answered {answer.status} with {len(answer.json().get('instructions', []))} "
                f"instruction lines; the page shows {recovery.locator(':scope > ul.compact > li').count()}")
    check_own_request_after(record, prepare, log, sent_before, stays=True)
    replay = answer.json()
    check_rail(record, page, "Flow C, replay instructions prepared",
               ["Done", NOT_APPLICABLE[0], "Done", *NOT_APPLICABLE[:3]],
               next_step=f"Instructions prepared: {count_of(len(replay['instructions']), 'line')}; provider calls made: "
                         f"{replay['provider_calls_made']}.",
               proofs={2: plan_proof(replay["plan"]["plan_id"], len(replay["plan"]["targets"]), replay["plan"]["plan_hash"])})
    walk.shot(record, page.locator("main .run-header"), "ui_13e_run_header_replay_path.png")
    no_error_banner(record, page, f"[Q33 guard] after the Flow C controls on {run_id}")


def step_q33_report_logout(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext,
                           log: RequestLog) -> None:
    """[Q33] Fix 2 for Export report (JSON) and Log out."""
    run_id = walk.shared["q33_flow_c_run"]
    report_panel = page.locator("section.panel.report")
    export = OwnRequestControl("Export report (JSON)",
                               report_panel.get_by_role("button", name="Export report (JSON)", exact=True),
                               "GET", run_requests(run_id, "report"))
    sent_before, held = press_own_request(walk, record, page, context, export, log, "ui_14a_export_busy.png")
    with page.expect_download() as downloaded:
        release_once(page, held, export.method, export.path)
    record.note(f"[Q33] the export downloaded {downloaded.value.suggested_filename}")
    expect(report_panel).to_contain_text(f"Saved as report-{run_id}.json")
    settle(page)
    check_own_request_after(record, export, log, sent_before, stays=True)

    logout = OwnRequestControl("Log out", page.locator("header.topbar .operator").get_by_role("button", name="Log out"),
                               "DELETE", re.compile(r"/api/session$"))  # page.route matches the whole URL
    sent_before, held = press_own_request(walk, record, page, context, logout, log, "ui_14b_logout_busy.png")
    release_once(page, held, logout.method, logout.path)
    expect(page.get_by_role("heading", name="Operator login")).to_be_visible()
    settle(page)
    check_own_request_after(record, logout, log, sent_before, stays=False)
    after = page.evaluate(FOCUS_JS, {})
    record.note(f"[Q33] found, left alone: after Log out the button is gone and focus is on {after['tag']} "
                f"{after['text'][:40]!r} (not one of the brief's cases)")
    walk.shared["q33_focus_after_logout"] = after["tag"]
    walk.shared["census_q"] = page.evaluate("() => window.__walkthroughCensus")
    no_error_banner(record, page, "[Q33 guard] after Export report and Log out")


# --------------------------------------------------------------------------- [Q36] owner decision Q36

# Checks starting "[Q36]" assert what the three Q36 changes make, so each fails on 0ca6a35 (the Q33 build); checks
# starting "[Q36 guard]" assert what must not change, so they pass on both builds by design.
Q36_SEED = 505  # a worker_backlog seed server B has not seeded
LONG_JOB_POLLS = 6  # job answers kept at "running" in the browser: about three seconds of a long diagnosis
POLL_GAP_MS = 250  # halfway between two of the job's 500 ms polls
RUNNING_TEXT = "Working: diagnosis running..."
APPROVAL_LINE = 'section.panel.recovery > p[tabindex="-1"]'
SCENARIO_SELECT = "section.panel.controls > label:first-of-type > select"
# Wraps HTMLElement.prototype.focus to record the page offset just before and just after each script focus() call:
# Chromium scrolls a newly focused element into view inside that call, before any focus event is dispatched (measured:
# a focusin listener already saw the scrolled offset). The pairs isolate the focus move from later re-renders, such as
# the reload after an approval, whose layout changes Chromium's scroll anchoring may offset. Restored afterwards.
FOCUS_SCROLL_JS = """() => {
  const original = HTMLElement.prototype.focus;
  window.__q36FocusMoves = [];
  HTMLElement.prototype.focus = function (...args) {
    const before = window.scrollY;
    const result = original.apply(this, args);
    window.__q36FocusMoves.push({ target: (this.textContent || "").trim().slice(0, 40), before, after: window.scrollY });
    return result;
  };
  window.__q36RestoreFocus = () => { HTMLElement.prototype.focus = original; };
}"""


def ax_focused(context: BrowserContext, page: Page) -> dict | None:
    """[Q36] Chromium's accessibility node for the focused element: what a screen reader is given on focus."""
    session = context.new_cdp_session(page)
    try:
        focused = session.send("Runtime.evaluate", {"expression": "document.activeElement"})["result"]
        if "objectId" not in focused:
            return None
        node = session.send("Accessibility.getPartialAXTree", {"objectId": focused["objectId"], "fetchRelatives": False})["nodes"][0]
    finally:
        session.detach()
    return {"role": (node.get("role") or {}).get("value"), "name": (node.get("name") or {}).get("value", ""),
            "ignored": bool(node.get("ignored")),
            "properties": {item["name"]: (item.get("value") or {}).get("value") for item in node.get("properties", [])}}


def check_focus_on_page(record: StepRecord, page: Page, what: str) -> None:
    """[Q36 guard] Focus the user had put on the page itself, by a click on plain text, is not moved."""
    page.evaluate(TWO_FRAMES_JS)
    focus = page.evaluate(FOCUS_JS, {})
    record.check(focus["tag"] == "body", f"[Q36 guard] {what}: focus the user put on the page itself before the answer "
                 f"stays there (it is on {focus['tag']} {focus['text'][:48]!r})")


def wait_for_job_done(page: Page, job_id: str) -> None:
    job_line = page.locator("section.panel.diagnosis p", has_text=f"Job {job_id} ").locator("strong")
    expect(job_line).to_have_text("done", timeout=SLOW_TIMEOUT_MS)


def region_texts(page: Page) -> list[str]:
    """[Q36] The region's text after each change since the last reset (MARK_REGION_JS records them)."""
    return page.evaluate("() => window.__q33RegionTexts ?? []")


def step_q36_login_logout(walk: Walkthrough, record: StepRecord, page: Page, token: str) -> None:
    """[Q36] (c): after Log out, focus goes to the Operator token field; after a login, to the Scenario select."""
    login_heading = page.get_by_role("heading", name="Operator login")
    signed_in = page.locator("header.topbar .operator")
    logout = signed_in.get_by_role("button", name="Log out")
    token_field = page.locator(LOGIN_TOKEN_FIELD)
    sessions = re.compile(r"/api/session$")  # page.route matches the whole URL
    page.goto(f"{SERVER_B}/")
    expect(login_heading).to_be_visible()

    # Guard: the login answer arrives after the user clicked plain text, so focus is on the page; it stays there.
    token_field.fill(token)
    held = hold(page, "POST", sessions)
    token_field.focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the login request")
    login_heading.click()
    release_once(page, held, "POST", sessions)
    expect(signed_in).to_contain_text(f"Operator {OPERATOR_ID}")
    settle(page)
    check_focus_on_page(record, page, "a login answered after the user clicked the page")

    # Log out from the keyboard: the Operator token field takes the focus Log out had.
    logout.focus()
    page.keyboard.press("Enter")
    expect(login_heading).to_be_visible()
    settle(page)
    check_focus_lands(record, page, "Log out, once the session has ended and the button is gone",
                      "the Operator token field", LOGIN_TOKEN_FIELD, "", tag="Q36")
    walk.shot(record, page, "ui_15a_logout_token_field.png")

    # Log in from the keyboard: the Scenario select, the workbench's first control, takes the focus the form had.
    token_field.fill(token)
    page.get_by_role("button", name="Start session").focus()
    page.keyboard.press("Enter")
    expect(signed_in).to_contain_text(f"Operator {OPERATOR_ID}")
    settle(page)
    check_focus_lands(record, page, "Start session, once the login succeeded and the login form is gone",
                      "the Scenario select", SCENARIO_SELECT, "", tag="Q36")
    walk.shot(record, page, "ui_15b_login_scenario_select.png")

    # Guard: the Log out answer arrives after the user clicked plain text; focus stays on the page.
    held = hold(page, "DELETE", sessions)
    logout.focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the logout request")
    page.locator("header.topbar .badge.sandbox").click()
    release_once(page, held, "DELETE", sessions)
    expect(login_heading).to_be_visible()
    settle(page)
    check_focus_on_page(record, page, "Log out answered after the user clicked the page")

    log_in(page, SERVER_B, token)
    walk.shared["q36_logged_in"] = True
    no_error_banner(record, page, "[Q36 guard] after the logins and logouts")


def step_q36_running_diagnosis(walk: Walkthrough, record: StepRecord, page: Page) -> None:
    """[Q36] (b): a running diagnosis is announced once, not on each of its 500 ms polls."""
    run_id = walk.shared["x_diagnosed_run"]
    open_run_and_settle(page, run_id)
    if not page.evaluate(MARK_REGION_JS):
        raise CheckFailed("the page has no role=status region to listen to")
    diagnose = page.locator(DIAGNOSE_BUTTON)
    starts = run_requests(run_id, "diagnoses")

    def press_diagnose() -> str:
        page.evaluate("() => { window.__q33RegionTexts = []; }")
        diagnose.focus()
        with page.expect_response(path_matcher("POST", starts)) as started:
            page.keyboard.press("Enter")
        return started.value.json()["job_id"]

    # One rules-mode diagnosis, from the press until the page is idle.
    wait_for_job_done(page, press_diagnose())
    settle(page)
    texts = region_texts(page)
    spoken = [item for item in texts if item]
    walk.shared["q36_region_rules_job"] = {"text changes": len(texts), "announcements": spoken}
    record.check(spoken.count(RUNNING_TEXT) == 1 and not any("poll diagnosis" in item for item in spoken),
                 f"[Q36] one rules-mode diagnosis, press to idle: the region says {RUNNING_TEXT!r} once and never "
                 f"announces a poll ({len(texts)} text changes; announcements {spoken})")

    # A long job: its first polls are answered "running" in the browser, after the server has answered them.
    remaining = [LONG_JOB_POLLS]

    def keep_running(job: dict) -> bool:
        if remaining[0] == 0:
            return False
        remaining[0] -= 1
        job.update(status="running", diagnosis=None, packet=None, error=None, finished_wall_ms=None)
        return True

    rewrite = ResponseRewrite(page, JOB_POLL, keep_running)
    job_id = press_diagnose()
    deadline = time.monotonic() + EXPECT_TIMEOUT_MS / 1000
    while rewrite.rewritten < 3:
        if time.monotonic() > deadline:
            raise CheckFailed(f"the job was polled {rewrite.rewritten} times within {EXPECT_TIMEOUT_MS} ms, not 3")
        page.wait_for_timeout(20)
    page.wait_for_timeout(POLL_GAP_MS)
    between = page.evaluate(REGION_JS)
    record.check(between["sameAsMarked"] and between["busyLines"] == 1 and between["text"] == RUNNING_TEXT,
                 f"[Q36] between two polls of a running job the busy line stays, with the same text: p.busy lines "
                 f"{between['busyLines']}, text {between['text']!r}, same element {between['sameAsMarked']}")
    walk.shot(record, page, "ui_16a_running_job_between_polls.png")
    wait_for_job_done(page, job_id)
    settle(page)
    rewrite.stop()
    texts = region_texts(page)
    spoken = [item for item in texts if item]
    walk.shared["q36_region_long_job"] = {"polls kept running": rewrite.rewritten, "text changes": len(texts),
                                          "announcements": spoken}
    record.check(rewrite.rewritten == LONG_JOB_POLLS and spoken.count(RUNNING_TEXT) == 1
                 and not any("poll diagnosis" in item for item in spoken),
                 f"[Q36] a job kept running for {rewrite.rewritten} polls, press to idle: the region says "
                 f"{RUNNING_TEXT!r} once and never announces a poll ({len(texts)} text changes; announcements {spoken})")
    no_error_banner(record, page, "[Q36 guard] after the diagnoses")


def step_q36_approve(walk: Walkthrough, record: StepRecord, page: Page, context: BrowserContext, demo: DemoFacts) -> None:
    """[Q36] (a): Approve's focus target is the "Approved by" line: nothing scrolls, Execute is one Tab away."""
    recovery = page.locator("section.panel.recovery")
    plan = recovery.locator(":scope > div.plan")
    run_id = create_run_in_ui(page, "worker_backlog", Q36_SEED)
    diagnose_in_ui(page)
    if proposed_operation(page) != "reprocess_inbox":
        raise CheckFailed(f"worker_backlog seed {Q36_SEED} proposes {proposed_operation(page)!r}, not reprocess_inbox")
    recovery.locator("select").select_option(demo.passing_fix_id)
    recovery.get_by_role("button", name="Validate this corrected handler", exact=True).click()
    expect(recovery.locator("p", has_text=f"{demo.passing_fix_id}: reproduced on the deployed handler")).to_contain_text("passed")
    settle(page)
    build = recovery.get_by_role("button", name="Build reprocessing plan", exact=True)
    build.click()
    expect(plan.locator(":scope > h3")).to_contain_text("Plan plan_")
    settle(page)
    approve = recovery.get_by_role("button", name=re.compile(r"^Approve plan "))
    approvals = run_requests(run_id, r"plans/[^/]+/approve")

    held = hold(page, "POST", approvals)
    approve.focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the approval")
    offset_before = page.evaluate("() => window.scrollY")
    approve_box = approve.bounding_box()
    page.evaluate(FOCUS_SCROLL_JS)
    release_once(page, held, "POST", approvals)
    expect(recovery).to_contain_text(f"Approved by {OPERATOR_ID}")
    settle(page)
    check_focus_lands(record, page, "Approve plan, once the plan is approved and the button is gone",
                      "the 'Approved by' line", APPROVAL_LINE, f"Approved by {OPERATOR_ID}", tag="Q36")
    moves = page.evaluate("() => { window.__q36RestoreFocus(); return window.__q36FocusMoves; }")
    offset_after = page.evaluate("() => window.scrollY")
    box = recovery.locator(":scope > p", has_text="Approved by").bounding_box()
    in_view = box is not None and box["y"] >= 0 and box["y"] + box["height"] <= VIEWPORT["height"]
    shown = [f"{move['target']!r}: {move['before']} -> {move['after']} px" for move in moves]
    record.check(len(moves) == 1 and moves[0]["after"] == moves[0]["before"] and in_view,
                 f"[Q36] Approve plan: moving focus scrolls nothing (script focus moves, with the page offset just "
                 f"before and after each: {shown}), and the 'Approved by' line is in view (top "
                 f"{None if box is None else round(box['y'])} px of {VIEWPORT['height']})")
    record.note(f"[Q36] from the press to idle the page offset went {offset_before} -> {offset_after} px; Approve's top "
                f"was at {None if approve_box is None else round(approve_box['y'])} px, the line's is at "
                f"{None if box is None else round(box['y'])} px")
    node = ax_focused(context, page)
    record.note(f"[Q36] the focused element's accessibility node after Approve: {node}")
    record.check(node is not None and not node["ignored"]
                 and (node["name"] or "").startswith(f"Approved by {OPERATOR_ID}, bound to hash "),
                 f"[Q36] Approve plan: Chromium's accessibility tree names the focused element with the confirmation "
                 f"(role {None if node is None else node['role']!r}, name {None if node is None else node['name'][:90]!r})")
    walk.shot(record, page, "ui_17a_approved_line_focused.png")
    page.keyboard.press("Tab")
    stop = page.evaluate(FOCUS_JS, {})
    record.check(stop["tag"] == "button" and stop["text"].startswith("Execute"),
                 f"[Q36] Approve plan: one Tab from where focus went reaches Execute (it reached {stop['tag']} "
                 f"{stop['text'][:40]!r})")

    # Guard: a new plan brings Approve back; focus the user moves away during the approval stays where it was put.
    build.click()
    expect(approve).to_be_visible()
    settle(page)
    held = hold(page, "POST", approvals)
    approve.focus()
    page.keyboard.press("Enter")
    held.wait_for_one("the second approval")
    page.locator(FIX_SELECT).focus()
    release_once(page, held, "POST", approvals)
    expect(recovery).to_contain_text(f"Approved by {OPERATOR_ID}")
    settle(page)
    check_focus_stays(record, page, "Approve plan, the button gone", "the Corrected handler select", FIX_SELECT,
                      tag="Q36")
    walk.shared["census_r"] = page.evaluate("() => window.__walkthroughCensus")
    no_error_banner(record, page, f"[Q36 guard] after Approve on {run_id}")


# --------------------------------------------------------------------------- [UI-A] the guided console

UI_A_SEED = 606  # a worker_backlog seed server B has not seeded
LAYOUTS = ((1440, 900, "wide"), (1024, 800, "medium"), (390, 844, "narrow"))
# Every element whose box ends past the right edge of the viewport, unless a box that scrolls or clips sideways holds it.
OVERFLOW_JS = """() => {
  const root = document.documentElement;
  const held = (element) => {
    for (let parent = element.parentElement; parent !== null && parent !== document.body; parent = parent.parentElement) {
      if (["auto", "scroll", "hidden", "clip"].includes(getComputedStyle(parent).overflowX)) return true;
    }
    return false;
  };
  // checkVisibility() leaves out what is not rendered, such as the content of a closed <details>.
  const past = [...document.body.querySelectorAll("*")]
    .filter((element) => element.checkVisibility() && element.getBoundingClientRect().right > root.clientWidth + 0.5
      && !held(element))
    .map((element) => `${element.tagName.toLowerCase()}.${element.getAttribute("class") ?? ""} ends at `
      + `${Math.round(element.getBoundingClientRect().right)}`);
  return { scrollWidth: root.scrollWidth, clientWidth: root.clientWidth, past: past.slice(0, 5), pastCount: past.length };
}"""
SPLIT_JS = """() => {
  const queue = document.querySelector("main section.panel.queue");
  const table = queue?.querySelector(":scope > div.scroll") ?? null;
  const detail = queue?.querySelector(":scope > div.case-detail") ?? null;
  if (table === null || detail === null) return null;
  const box = (element) => {
    const rect = element.getBoundingClientRect();
    return { left: Math.round(rect.left), right: Math.round(rect.right), top: Math.round(rect.top), bottom: Math.round(rect.bottom) };
  };
  const payments = detail.querySelector(":scope > div.table-scroll");
  return {
    table: box(table), detail: box(detail), position: getComputedStyle(detail).position,
    tableScrolls: table.scrollWidth > table.clientWidth,
    paymentsScroll: payments === null ? null : payments.scrollWidth > payments.clientWidth,
  };
}"""
MOTION_JS = """(selector) => {
  const style = getComputedStyle(document.querySelector(selector));
  return { durations: style.transitionDuration.split(", "), properties: style.transitionProperty.split(", "),
           running: document.getAnimations().map((animation) => animation.animationName ?? animation.transitionProperty) };
}"""


def needs_review(job: dict) -> bool:
    diagnosis = job.get("diagnosis") or {}
    if job.get("status") != "done" or not diagnosis:
        return False
    diagnosis.update(status="needs_review", output=None,
                     reasons=["[UI-A] rewritten in the browser: every answer failed validation"])
    return True


def proposes_no_action(job: dict) -> bool:
    diagnosis = job.get("diagnosis") or {}
    if job.get("status") != "done" or not diagnosis.get("output"):
        return False
    diagnosis["output"]["proposed_operation"] = "no_action"
    return True


def recover_in_ui(page: Page, demo: DemoFacts) -> tuple[str, str]:
    """[UI-A] Flow A by mouse on a fresh worker_backlog run; returns the run ID and the executed action's ID."""
    recovery = page.locator("section.panel.recovery")
    run_id = create_run_in_ui(page, "worker_backlog", UI_A_SEED)
    diagnose_in_ui(page)
    if proposed_operation(page) != "reprocess_inbox":
        raise CheckFailed(f"worker_backlog seed {UI_A_SEED} proposes {proposed_operation(page)!r}, not reprocess_inbox")
    recovery.locator("select").select_option(demo.passing_fix_id)
    recovery.get_by_role("button", name="Validate this corrected handler", exact=True).click()
    expect(recovery.locator("p", has_text=f"{demo.passing_fix_id}: reproduced on the deployed handler")).to_contain_text("passed")
    settle(page)
    recovery.get_by_role("button", name="Build reprocessing plan", exact=True).click()
    expect(recovery.locator(":scope > div.plan > h3")).to_contain_text("Plan plan_")
    settle(page)
    recovery.get_by_role("button", name=re.compile(r"^Approve plan ")).click()
    expect(recovery).to_contain_text(f"Approved by {OPERATOR_ID}")
    settle(page)
    with page.expect_response(path_matcher("POST", re.compile(r"/execute$"))) as executed:
        recovery.get_by_role("button", name="Execute", exact=True).click()
    expect(page.locator("main .run-header .rail-step").nth(5).locator(".step-state")).to_have_text("Done")
    settle(page)
    return run_id, executed.value.json()["action_id"]


def check_layouts(walk: Walkthrough, record: StepRecord, page: Page) -> None:
    """[UI-A] The page at each width: never wider than the viewport; the case beside the queue when wide, below it
    otherwise; the wide tables scrolling inside their own boxes when narrow."""
    for width, height, name in LAYOUTS:
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate("() => window.scrollTo(0, 0)")
        page.evaluate(TWO_FRAMES_JS)
        overflow = page.evaluate(OVERFLOW_JS)
        record.check(overflow["scrollWidth"] <= overflow["clientWidth"] and overflow["pastCount"] == 0,
                     f"[UI-A] {width} px ({name}): the page does not scroll sideways (scroll width "
                     f"{overflow['scrollWidth']}, viewport {overflow['clientWidth']}; {overflow['pastCount']} elements "
                     f"past the right edge {overflow['past']})")
        split = page.evaluate(SPLIT_JS)
        if split is None:
            raise CheckFailed(f"{width} px: the queue has no table or no open case")
        if name == "wide":
            record.check(split["detail"]["left"] >= split["table"]["right"] and split["detail"]["top"] == split["table"]["top"]
                         and split["position"] == "sticky",
                         f"[UI-A] {width} px: the open case sits beside the queue table, in the same row, and is sticky "
                         f"(table {split['table']}, case {split['detail']}, position {split['position']})")
            record.check(not split["tableScrolls"],
                         f"[UI-A] {width} px: the queue table fits its column beside the case, with no sideways scroll in "
                         f"its box (scrolls {split['tableScrolls']})")
        else:
            record.check(split["detail"]["top"] >= split["table"]["bottom"] and split["position"] != "sticky",
                         f"[UI-A] {width} px: the open case stacks below the queue table and is not sticky (table "
                         f"{split['table']}, case {split['detail']}, position {split['position']})")
        if name == "narrow":
            record.check(split["tableScrolls"] and split["paymentsScroll"] is True,
                         f"[UI-A] {width} px: the queue and payments tables scroll sideways inside their own boxes "
                         f"(queue {split['tableScrolls']}, payments {split['paymentsScroll']})")
        walk.shot(record, page, f"ui_18a_{name}_{width}_viewport.png")
        walk.shot(record, page, f"ui_18b_{name}_{width}_full_page.png", full_page=True)
    page.set_viewport_size(VIEWPORT)


def check_motion(record: StepRecord, page: Page, run_id: str) -> None:
    """[UI-A] Motion for state changes only, and none at all under prefers-reduced-motion: the buttons' transitions,
    and every animation in the page while the busy line shows its spinner."""
    advance = page.locator("section.panel.controls").get_by_role("button", name="+1 min", exact=True)
    advances = run_requests(run_id, "advance")
    seen = {}
    for preference in ("no-preference", "reduce"):
        page.emulate_media(reduced_motion=preference)
        held = hold(page, "POST", advances)
        advance.click()
        held.wait_for_one(f"+1 min with prefers-reduced-motion {preference}")
        page.evaluate(TWO_FRAMES_JS)
        seen[preference] = page.evaluate(MOTION_JS, CREATE_SELECTOR)
        release_once(page, held, "POST", advances)
        settle(page)
    page.emulate_media(reduced_motion="no-preference")
    moving, still = seen["no-preference"], seen["reduce"]
    record.check(any(duration != "0s" for duration in moving["durations"])
                 and not {"color", "opacity", "all"} & set(moving["properties"]) and "spin" in moving["running"],
                 f"[UI-A] without a motion preference, buttons ease only their background, border and shadow "
                 f"({moving['properties']}, {moving['durations']}), and the busy line's spinner turns ({moving['running']})")
    record.check(set(still["durations"]) == {"0s"} and not still["running"],
                 f"[UI-A] under prefers-reduced-motion nothing moves: button transitions {still['durations']}, animations "
                 f"running while the busy line shows {still['running']}")


def step_ui_a_console(walk: Walkthrough, record: StepRecord, page: Page, token: str, demo: DemoFacts) -> None:
    """[UI-A] The guided console on server B: a recovery through the UI, the page at three widths, motion, and the
    rail on the needs-review and no_action paths, which no demo scenario's rules diagnosis takes (rewritten in the
    browser, as step 13 does for the replay)."""
    log_in(page, SERVER_B, token)
    panel = queue_panel(page)
    recovery = page.locator("section.panel.recovery")
    run_id, action_id = recover_in_ui(page, demo)
    check_rail(record, page, "a recovery executed through the UI", ["Done"] * 6,
               next_step=f"Action {action_id} succeeded and its receipt is recorded.")
    target = recovery.locator(":scope > div.plan table tbody tr td:first-child").first.inner_text()
    page.evaluate("() => window.scrollTo(0, 0)")
    open_case(panel, target)
    settle(page)
    check_layouts(walk, record, page)
    check_motion(record, page, run_id)

    rewrite = ResponseRewrite(page, JOB_POLL, needs_review)
    diagnose_in_ui(page)
    rewrite.stop()
    rail = check_rail(record, page, "a diagnosis that needs review (rewritten in the browser)",
                      ["Needs review"] + NOT_APPLICABLE,
                      path="needs review: nothing is proposed; escalate with the deterministic evidence",
                      next_step="Escalate with the deterministic evidence in the Recovery card.",
                      note="[UI-B-adjusted: a diagnosis that needs review is not a done step, so step 1 now reads "
                           "Needs review instead of Done]")
    reviewed = rail["steps"][0]
    job = job_of_page(page, run_id)  # the server's job: the rewrite changed only its status and output
    wanted_proof = [diagnose_proof(job)[0], "needs review", hash_proof("packet", job["diagnosis"]["packet_hash"])]
    record.check(reviewed["state"] == "Needs review" and reviewed["proof"] == wanted_proof
                 and "step-review" in reviewed["className"].split(),
                 f"[UI-B] needs review: step 1 reads {reviewed['state']!r} in the manual-review tone "
                 f"({reviewed['className']!r}), with the proof {reviewed['proof']} (wanted {wanted_proof})")
    record.check(rail["facts"].get("Cause") == "Needs review: the model did not produce a valid diagnosis, so nothing is proposed."
                 and page.locator("section.panel.diagnosis div.banner.needs-review").is_visible()
                 and proposed_operation(page) == "none (needs review)",
                 f"[UI-A] needs review: the rail's cause {rail['facts'].get('Cause')!r}, the Diagnosis card's banner and "
                 f"Recovery's {proposed_operation(page)!r} all say nothing is proposed")
    check_not_yet_known(record, page, "a new diagnosis after an execution (the strip follows the latest diagnosis's recovery)")
    walk.shot(record, page.locator("main .run-header"), "ui_18c_run_header_needs_review.png")

    rewrite = ResponseRewrite(page, JOB_POLL, proposes_no_action)
    diagnose_in_ui(page)
    rewrite.stop()
    check_rail(record, page, "a diagnosis proposing no_action (rewritten in the browser)", ["Done"] + NOT_APPLICABLE,
               path="no_action: the diagnosis found no incident; nothing is changed",
               next_step="Nothing to do: the diagnosis found no incident; nothing is changed.")
    record.check("The diagnosis found no incident; nothing is changed." in recovery.inner_text(),
                 "[UI-A] no_action: the Recovery card says nothing is changed")
    walk.shot(record, page.locator("main .run-header"), "ui_18d_run_header_no_action.png")
    walk.shared["census_g"] = page.evaluate("() => window.__walkthroughCensus")
    no_error_banner(record, page, "[UI-A] after the guided-console checks")


# --------------------------------------------------------------------------- [UI-B] round 2 of the guided console

UI_B_SEEDS = (707, 808)  # worker_backlog seeds server B has not seeded: the re-plan run, the rejection run
PLANS = re.compile(r"/plans$")
EXECUTIONS = re.compile(r"/execute$")
RECEIPT_READS = re.compile(r"/api/runs/[^/]+/actions/[^/]+/receipt$")
STALE_WAIT_MS = 1_500  # how long a stale answer, once released, gets to change the page
DIFFERS_MS = 8_000  # how long to wait for what a build without the fix never shows


def eventually(page: Page, condition: Callable[[], bool], timeout_ms: int = DIFFERS_MS) -> bool:
    """[UI-B] Whether `condition` holds within `timeout_ms`: a wait, not an assertion. The caller records its checks
    either way, so a build without the fix fails those checks instead of stopping the step."""
    deadline = time.monotonic() + timeout_ms / 1000
    while not condition():
        if time.monotonic() > deadline:
            return False
        page.wait_for_timeout(100)
    return True


def rail_state(page: Page, index: int) -> str | None:
    rail = page.evaluate(RAIL_JS)
    return None if rail is None else rail["steps"][index]["state"]


def execution_items(page: Page) -> list[str]:
    return page.locator("section.panel.recovery > ul.compact > li", has_text="Action act_").all_inner_texts()


def strip_execution(page: Page) -> dict:
    """The money strip's two execution figures: what the latest Execute answer of the recovery said."""
    figures = money_strip(page)["figures"]
    return {name: figures.get(name) for name in (RESTORED_FIGURE, NEW_CAPTURE_FIGURE)}


def executed_figures(result: dict) -> dict:
    detail = f"action {result['action_id']}, {result['status']}"
    return {RESTORED_FIGURE: {"value": rupees(result["restored_processing_value_paise"]), "detail": detail},
            NEW_CAPTURE_FIGURE: {"value": rupees(result["additional_captured_paise"]), "detail": detail}}


def approved_plan_in_ui(page: Page, demo: DemoFacts, seed: int) -> tuple[str, dict]:
    """[UI-B] A fresh worker_backlog run diagnosed, validated, planned and approved by mouse: its ID and the plan."""
    recovery = page.locator("section.panel.recovery")
    run_id = create_run_in_ui(page, "worker_backlog", seed)
    diagnose_in_ui(page)
    if proposed_operation(page) != "reprocess_inbox":
        raise CheckFailed(f"worker_backlog seed {seed} proposes {proposed_operation(page)!r}, not reprocess_inbox")
    recovery.locator("select").select_option(demo.passing_fix_id)
    recovery.get_by_role("button", name="Validate this corrected handler", exact=True).click()
    expect(recovery.locator("p", has_text=f"{demo.passing_fix_id}: reproduced on the deployed handler")).to_contain_text("passed")
    settle(page)
    with page.expect_response(path_matcher("POST", PLANS)) as planned:
        recovery.get_by_role("button", name="Build reprocessing plan", exact=True).click()
    expect(recovery.locator(":scope > div.plan > h3")).to_contain_text("Plan plan_")
    settle(page)
    recovery.get_by_role("button", name=re.compile(r"^Approve plan ")).click()
    expect(recovery).to_contain_text(f"Approved by {OPERATOR_ID}")
    settle(page)
    return run_id, planned.value.json()


def check_rejection(walk: Walkthrough, record: StepRecord, page: Page, demo: DemoFacts) -> str:
    """[UI-B] Defect 1: the server rejects an execution whose plan and approval expired on the replay clock; the page
    records that answer (rail, Recovery card, money strip) and still shows the error banner as before."""
    recovery = page.locator("section.panel.recovery")
    run_id, _ = approved_plan_in_ui(page, demo, UI_B_SEEDS[1])
    clock = get_json(page, run_path(run_id))["replay_clock_ms"]
    page.locator("section.panel.controls").get_by_role("button", name="+1 h", exact=True).click()
    expect(queue_panel(page).locator(":scope > p").first).to_contain_text(f"as of replay time {replay_time(clock + 3_600_000)}")
    settle(page)
    with page.expect_response(path_matcher("POST", EXECUTIONS)) as executed:
        recovery.get_by_role("button", name="Execute", exact=True).click()
    answer, body = executed.value, executed.value.json()
    result = body.get("result") or {}
    if answer.status != 409 or body.get("error") != "PlanRejectedError" or result.get("status") != "rejected":
        raise CheckFailed(f"executing the expired plan answered {answer.status} {body.get('error')!r}, not a rejection")
    expect(page.locator("p.banner.error")).to_be_visible()
    banner = page.locator("p.banner.error").inner_text()
    record.check(banner == f"execute plan failed: PlanRejectedError: {body['detail']}",
                 f"[UI-B guard] the rejection still shows in the error banner, as before: {banner!r}")
    action_id = result["action_id"]
    eventually(page, lambda: rail_state(page, 5) == "Done")
    receipt_doc = get_json(page, receipt_path(run_id, action_id))  # the server keeps a receipt for a rejected action
    check_rail(record, page, "a rejected execution", ["Done", "Done", "Done", "Done", "Failed", "Done"],
               next_step=f"Action {action_id} was rejected; its reasons are in the Recovery card.",
               proofs={4: [action_id, "rejected"], 5: [hash_proof("hash", receipt_doc["receipt_hash"])]}, tag="[UI-B]",
               structure_tag="[UI-B guard]")
    items = execution_items(page)
    reasons = f"reasons: {'; '.join(result['reasons'])}"
    record.check(len(items) == 1 and items[0].startswith(f"Action {action_id}: rejected") and reasons in items[0],
                 f"[UI-B] the Recovery card lists the rejected action with the 409 answer's reasons: {items}")
    strip = money_strip(page)
    shown = strip_execution(page)
    tone = strip["classes"].get(RESTORED_FIGURE, "")
    record.check(shown == executed_figures(result) and "figure-failed" in tone.split(),
                 f"[UI-B] the money strip shows the 409 answer's values {shown} in the failed tone ({tone!r})")
    walk.shot(record, page.locator("main .run-header"), "ui_19a_run_header_rejected.png")
    return run_id


def check_kept_execution(walk: Walkthrough, record: StepRecord, page: Page, demo: DemoFacts, other_run: str) -> str:
    """[UI-B] Defect 2: validating again or planning again after an execution keeps that execution's answer in the money
    strip, while the rail and the Recovery card describe the new plan only. Defect 4: the receipt read of a later
    execution, failing after another run was opened, shows nothing on that run."""
    recovery = page.locator("section.panel.recovery")
    plan_box = recovery.locator(":scope > div.plan")
    run_id, _ = approved_plan_in_ui(page, demo, UI_B_SEEDS[0])
    with page.expect_response(path_matcher("POST", EXECUTIONS)) as executed:
        recovery.get_by_role("button", name="Execute", exact=True).click()
    kept = executed_figures(executed.value.json())
    eventually(page, lambda: rail_state(page, 5) == "Done")
    settle(page)

    with page.expect_response(path_matcher("POST", re.compile(r"/validate$"))) as validated:
        recovery.get_by_role("button", name="Validate this corrected handler", exact=True).click()
    expect(plan_box).to_have_count(0)
    settle(page)
    record.check(strip_execution(page) == kept, f"[UI-B] after validating again, the money strip still shows the executed "
                 f"answer: {strip_execution(page)} (the answer: {kept})")
    check_rail(record, page, "validated again after an execution", rail_states("Done", "Done", "Current"),
               next_step="Build the reprocessing plan.", proofs={1: [validated.value.json()["fix_id"], "passed"]},
               tag="[UI-B guard]")
    record.check(not execution_items(page) and recovery.get_by_role("button", name=re.compile(r"^Execute")).count() == 0,
                 "[UI-B guard] validated again: the Recovery card lists no execution and offers no Execute")
    walk.shot(record, page.locator("main .run-header"), "ui_19b_run_header_after_revalidate.png")

    with page.expect_response(path_matcher("POST", PLANS)) as planned:
        recovery.get_by_role("button", name="Build reprocessing plan", exact=True).click()
    plan = planned.value.json()
    expect(plan_box.locator(":scope > h3")).to_have_text(f"Plan {plan['plan_id']}")
    settle(page)
    record.check(strip_execution(page) == kept, f"[UI-B] after building a new plan, the money strip still shows the "
                 f"executed answer: {strip_execution(page)} (the answer: {kept})")
    has_targets = plan["target_count"] > 0
    check_rail(record, page, "a new plan after an execution",
               ["Done", "Done", "Done", *(["Current", "Not yet", "Not yet"] if has_targets else NOT_APPLICABLE[:3])],
               next_step=("Check the plan's exact target rows, then approve it." if has_targets
                          else "Nothing to approve: the plan has no target orders."),
               proofs={2: plan_proof(plan["plan_id"], plan["target_count"], plan["plan_hash"])}, tag="[UI-B guard]")
    record.check(not execution_items(page), "[UI-B guard] the new plan lists no execution: the earlier plan's action is "
                 "not shown as this plan's")
    walk.shot(record, page.locator("main .run-header"), "ui_19c_run_header_after_replan.png")

    if not has_targets:  # its stale-only orders become targets once their provider state is refreshed
        stale = recovery.locator(":scope > div.row", has_text="excluded only because their provider state is stale")
        if stale.count() != 1:
            raise CheckFailed(f"the new plan {plan['plan_id']} has no target and no stale-only order to refresh")
        with page.expect_response(path_matcher("POST", PLANS)) as replanned:
            stale.get_by_role("button", name="Refresh provider state (read-only) and re-plan").click()
        plan = replanned.value.json()
        expect(plan_box.locator(":scope > h3")).to_have_text(f"Plan {plan['plan_id']}")
        settle(page)
        if plan["target_count"] == 0:
            raise CheckFailed(f"the refreshed plan {plan['plan_id']} still has no target")
    recovery.get_by_role("button", name=re.compile(r"^Approve plan ")).click()
    expect(recovery).to_contain_text(f"Approved by {OPERATOR_ID}")
    settle(page)
    check_rail(record, page, "a new plan with targets, approved after an earlier execution",
               rail_states("Done", "Done", "Done", "Done", "Current"), next_step="Execute the approved plan.",
               tag="[UI-B guard]")
    execute = recovery.locator(":scope > button", has_text=re.compile(r"^Execute"))
    record.check(execute.inner_text() == "Execute" and not execution_items(page),
                 f"[UI-B guard] the approved new plan offers {execute.inner_text()!r} and lists no execution")

    receipts = hold(page, "GET", RECEIPT_READS)
    with page.expect_response(path_matcher("POST", EXECUTIONS)) as executed:
        execute.click()
    second = executed.value.json()
    receipts.wait_for_one("the new action's receipt read")
    settle(page)
    check_rail(record, page, "the new plan executed, its receipt still being read",
               rail_states("Done", "Done", "Done", "Done", "Done", "Current"),
               proofs={4: [second["action_id"], "succeeded"]}, tag="[UI-B guard]")
    page.get_by_label("Open run").select_option(other_run)
    expect(page.locator("header.topbar")).to_contain_text(f"Run: {other_run}")
    settle(page)
    for route in receipts.take():
        route.fulfill(status=500, json={"error": "WalkthroughFailure", "detail": "[UI-B] a receipt read that fails late"})
    receipts.stop()
    page.wait_for_timeout(STALE_WAIT_MS)
    banners = page.locator("p.banner.error").all_inner_texts()
    record.check(not banners, f"[UI-B] a receipt read that fails after another run was opened shows no banner there "
                 f"({banners})")
    rail = read_rail(page)
    record.check(f"Run: {other_run}" in page.locator("header.topbar").inner_text()
                 and [step["state"] for step in rail["steps"]] == rail_states("Current")
                 and all(not step["proof"] and step["link"] is None for step in rail["steps"]),
                 "[UI-B guard] the page stays on the run that was opened, and its rail shows no step of the other run")
    return run_id


def check_stale_poll(record: StepRecord, page: Page, run_from: str, run_to: str) -> None:
    """[UI-B] Defect 5: a diagnosis poll answered after the operator opened another run changes nothing on it."""
    diagnosis = page.locator("section.panel.diagnosis")
    open_run_and_settle(page, run_from)
    polls = hold(page, "GET", JOB_POLL)
    diagnosis.get_by_role("button", name=DIAGNOSE_LABEL, exact=True).click()
    polls.wait_for_one("the job's first poll")
    page.get_by_label("Open run").select_option(run_to)
    expect(page.locator("header.topbar")).to_contain_text(f"Run: {run_to}")
    settle(page)
    release_once(page, polls, "GET", JOB_POLL)
    page.wait_for_timeout(STALE_WAIT_MS)
    settle(page)
    top = page.locator("header.topbar").inner_text()
    record.check(f"Run: {run_to}" in top and f"Run: {run_from}" not in top,
                 f"[UI-B] a diagnosis poll of {run_from} answered after {run_to} was opened leaves the page on {run_to} "
                 f"(top bar: {' '.join(top.split())[:120]!r})")
    record.check(diagnosis.locator("p.job-line").count() == 0,
                 "[UI-B] and the diagnosis card shows no job of the other run")
    rail = read_rail(page)
    record.check(rail["steps"][0]["state"] == "Current" and not rail["steps"][0]["proof"],
                 f"[UI-B] and the rail's Diagnose step shows no diagnosis ({rail['steps'][0]['state']!r}, "
                 f"proof {rail['steps'][0]['proof']})")
    banners = page.locator("p.banner.error").all_inner_texts()
    record.check(not banners, f"[UI-B guard] no error banner after the stale poll ({banners})")


def check_stale_poll_after_logout(walk: Walkthrough, record: StepRecord, page: Page) -> None:
    """[UI-B] Defect 5, its error path: a diagnosis poll answered after Log out (403: the session ended) shows nothing
    on the login page."""
    polls = hold(page, "GET", JOB_POLL)
    page.locator("section.panel.diagnosis").get_by_role("button", name=DIAGNOSE_LABEL, exact=True).click()
    polls.wait_for_one("the job's first poll")
    page.locator("header.topbar .operator").get_by_role("button", name="Log out").click()
    expect(page.get_by_role("heading", name="Operator login")).to_be_visible()
    settle(page)
    answer = release_once(page, polls, "GET", JOB_POLL)
    page.wait_for_timeout(STALE_WAIT_MS)
    banners = page.locator("p.banner.error").all_inner_texts()
    record.check(not banners, f"[UI-B] a diagnosis poll answered after Log out (HTTP {answer.status}) shows no banner on "
                 f"the login page ({banners})")
    top = " ".join(page.locator("header.topbar").inner_text().split())
    record.check("Evidence cutoff: no diagnosis yet" in top,
                 f"[UI-B guard] and the top bar shows no diagnosis ({top[:140]!r})")
    walk.shot(record, page, "ui_19d_login_after_stale_poll.png")


def step_ui_b_round_two(walk: Walkthrough, record: StepRecord, page: Page, token: str, demo: DemoFacts) -> None:
    """[UI-B] The five round-2 defects, on server B, in a browser context of their own."""
    log_in(page, SERVER_B, token)
    rejected_run = check_rejection(walk, record, page, demo)
    kept_run = check_kept_execution(walk, record, page, demo, rejected_run)
    check_stale_poll(record, page, rejected_run, kept_run)
    check_stale_poll_after_logout(walk, record, page)
    walk.shared["census_u"] = page.evaluate("() => window.__walkthroughCensus")


# --------------------------------------------------------------------------- [UI-C] round 3: answers for a run the page has left


def run_summary_reads(run_id: str) -> re.Pattern[str]:
    """[UI-C] GET /api/runs/{run_id} alone: the run summary, one of the four reads of a run reload."""
    return re.compile(rf"/api/runs/{re.escape(run_id)}$")


def check_page_shows(record: StepRecord, page: Page, where: str, run_id: str, left: str) -> None:
    """[UI-C] The page shows `run_id` and nothing of `left`: the top bar, the queue and the metrics, at the API's time."""
    run_doc = get_json(page, run_path(run_id))
    observed = get_json(page, f"{run_path(run_id)}/incidents")
    top = " ".join(page.locator("header.topbar").inner_text().split())
    lead = queue_panel(page).locator(":scope > p").first.inner_text()
    metrics = page.locator("section.panel.metrics > h2").inner_text()
    clock = replay_time(run_doc["replay_clock_ms"])
    record.check(f"Run: {run_id}" in top and f"Run: {left}" not in top and f"Replay clock: {clock}" in top
                 and f"as of replay time {clock}" in lead
                 and metrics == f"Provider versus merchant, as of {replay_time(observed['run']['as_of_ms'])}",
                 f"[UI-C] {where}: the page stays on {run_id} at its replay clock {clock}, with its queue and metrics "
                 f"(top bar {top[:110]!r}; queue {lead[:60]!r}; metrics {metrics!r})")
    cards = page.evaluate("""() => [document.querySelectorAll("main section.panel.queue").length,
                                    document.querySelectorAll("main section.panel.report").length]""")
    record.check(cards == [1, 1], f"[UI-C guard] {where}: one queue card and one report card, not a stale copy left by "
                 f"the run change (queue, report: {cards})")


def check_no_diagnosis_shown(record: StepRecord, page: Page, where: str, tag: str) -> None:
    rail = read_rail(page)
    diagnose = rail["steps"][0]
    record.check(page.locator("section.panel.diagnosis p.job-line").count() == 0
                 and diagnose["state"] == "Current" and not diagnose["proof"],
                 f"{tag} {where}: no job of the other run in the diagnosis card, and the Diagnose step is empty "
                 f"({diagnose['state']!r}, proof {diagnose['proof']})")


def check_no_banner(record: StepRecord, page: Page, where: str, tag: str) -> None:
    banners = page.locator("p.banner.error").all_inner_texts()
    record.check(not banners, f"{tag} {where}: no error banner ({banners})")


def release_after(page: Page, held: HeldRequests, method: str, path: re.Pattern[str]) -> Response:
    """[UI-C] Lets the held request go, then gives its answer time to land and the page time to settle."""
    answer = release_once(page, held, method, path)
    page.wait_for_timeout(STALE_WAIT_MS)
    settle(page)
    return answer


def log_out_now(page: Page) -> None:
    page.locator("header.topbar .operator").get_by_role("button", name="Log out").click()
    expect(page.get_by_role("heading", name="Operator login")).to_be_visible()
    settle(page)


UI_C_SEED = 909  # a worker_backlog seed server B has not seeded, for the refresh-and-re-plan race


def check_replan_after_run_change(record: StepRecord, page: Page, demo: DemoFacts, log: RequestLog, other: str) -> None:
    """[UI-C] Refresh provider state (read-only) and re-plan is two writes. When the refresh is answered after another
    run was opened, the second (POST plans, which stores a plan and steps the replay clock) is not sent."""
    recovery = page.locator("section.panel.recovery")
    run_id = create_run_in_ui(page, "worker_backlog", UI_C_SEED)
    diagnose_in_ui(page)
    if proposed_operation(page) != "reprocess_inbox":
        raise CheckFailed(f"worker_backlog seed {UI_C_SEED} proposes {proposed_operation(page)!r}, not reprocess_inbox")
    recovery.locator("select").select_option(demo.passing_fix_id)
    recovery.get_by_role("button", name="Validate this corrected handler", exact=True).click()
    expect(recovery.locator("p", has_text=f"{demo.passing_fix_id}: reproduced on the deployed handler")).to_contain_text("passed")
    settle(page)
    recovery.get_by_role("button", name="Build reprocessing plan", exact=True).click()
    expect(recovery.locator(":scope > div.plan > h3")).to_contain_text("Plan plan_")
    settle(page)
    stale = recovery.locator(":scope > div.row", has_text="excluded only because their provider state is stale")
    if stale.count() != 1:
        raise CheckFailed(f"seed {UI_C_SEED}'s first plan lists no stale-only order to refresh")
    plans, refreshes = run_requests(run_id, "plans"), run_requests(run_id, "refresh")
    plans_before = log.count("POST", plans)
    held = hold(page, "POST", refreshes)
    stale.get_by_role("button", name="Refresh provider state (read-only) and re-plan").click()
    held.wait_for_one("the stale-orders refresh")
    open_run_and_settle(page, other)
    release_after(page, held, "POST", refreshes)
    sent = log.count("POST", plans) - plans_before
    record.check(sent == 0, f"[UI-C] a refresh answered after another run was opened builds no plan for the run left "
                 f"({sent} POST plans sent for {run_id})")
    check_page_shows(record, page, "a refresh answered after another run was opened", other, run_id)
    check_no_banner(record, page, "a refresh answered after another run was opened", "[UI-C guard]")


def step_ui_c_left_runs(walk: Walkthrough, record: StepRecord, page: Page, token: str, demo: DemoFacts) -> None:
    """[UI-C] A diagnosis start answer, or a run reload, that arrives after the operator opened another run or logged
    out writes nothing and shows no error. Each request is held in the browser until the page has changed."""
    log = RequestLog()
    log.attach(page)
    log_in(page, SERVER_B, token)
    runs = [run["run_id"] for run in get_json(page, "/api/runs") if run["incident_ids"]]
    if len(runs) < 2:
        raise CheckFailed(f"server B has {len(runs)} runs with an incident; two are needed")
    first, second = runs[0], runs[1]
    diagnose = page.locator("section.panel.diagnosis").get_by_role("button", name=DIAGNOSE_LABEL, exact=True)
    advance = page.locator("section.panel.controls").get_by_role("button", name="+1 min", exact=True)

    # A diagnosis start answered after another run was opened.
    open_run_and_settle(page, first)
    starts = run_requests(first, "diagnoses")
    held = hold(page, "POST", starts)
    diagnose.click()
    held.wait_for_one("the diagnosis start")
    open_run_and_settle(page, second)
    release_after(page, held, "POST", starts)
    check_page_shows(record, page, "a diagnosis start answered after another run was opened", second, first)
    check_no_diagnosis_shown(record, page, "a diagnosis start answered after another run was opened", "[UI-C]")
    check_no_banner(record, page, "a diagnosis start answered after another run was opened", "[UI-C guard]")
    walk.shot(record, page, "ui_20a_start_answered_after_run_change.png")

    # A run reload (after +1 min) answered after another run was opened. Each part first opens the run it needs: on a
    # build without the fix, the previous part's stale answer has switched the page back.
    open_run_and_settle(page, second)
    reloads = run_summary_reads(second)
    held = hold(page, "GET", reloads)
    advance.click()
    held.wait_for_one("the run reload after +1 min")
    open_run_and_settle(page, first)
    release_after(page, held, "GET", reloads)
    check_page_shows(record, page, "a run reload answered after another run was opened", first, second)
    check_no_diagnosis_shown(record, page, "a run reload answered after another run was opened", "[UI-C guard]")
    check_no_banner(record, page, "a run reload answered after another run was opened", "[UI-C guard]")
    walk.shot(record, page, "ui_20b_reload_answered_after_run_change.png")
    check_replan_after_run_change(record, page, demo, log, first)

    # The same two, answered after Log out: the session has ended, so each answers 403.
    open_run_and_settle(page, first)
    starts = run_requests(first, "diagnoses")
    held = hold(page, "POST", starts)
    diagnose.click()
    held.wait_for_one("the diagnosis start before Log out")
    log_out_now(page)
    answer = release_after(page, held, "POST", starts)
    check_no_banner(record, page, f"a diagnosis start answered after Log out (HTTP {answer.status}), on the login page",
                    "[UI-C]")
    top = " ".join(page.locator("header.topbar").inner_text().split())
    record.check("Run: none" in top and "Evidence cutoff: no diagnosis yet" in top,
                 f"[UI-C guard] and the top bar shows no run and no diagnosis ({top[:120]!r})")

    log_in(page, SERVER_B, token)
    open_run_and_settle(page, second)
    reloads = run_summary_reads(second)
    held = hold(page, "GET", reloads)
    advance.click()
    held.wait_for_one("the run reload before Log out")
    log_out_now(page)
    answer = release_after(page, held, "GET", reloads)
    check_no_banner(record, page, f"a run reload answered after Log out (HTTP {answer.status}), on the login page", "[UI-C]")
    top = " ".join(page.locator("header.topbar").inner_text().split())
    record.check("Run: none" in top, f"[UI-C guard] and the top bar shows no run ({top[:80]!r})")
    walk.shot(record, page, "ui_20c_login_after_stale_answers.png")
    walk.shared["census_v"] = page.evaluate("() => window.__walkthroughCensus")


# --------------------------------------------------------------------------- main


# --------------------------------------------------------------------------- [MC] the owner's model chain (Q46)

MC_SEED = 20831  # the model chain's seeds are 20801-20900; server B has not seeded it
RULES_SOURCE = {"mode": "rules", "model_id": None, "provider_host": None, "label": "rules baseline (no model)"}
RULES_SOURCE_FACT = "Model source: rules baseline (no model)"
RULES_INSTEAD = "Diagnose with the rules instead"
START_DIAGNOSIS = re.compile(r"/api/runs/[^/]+/diagnoses$")


def step_model_chain(walk: Walkthrough, record: StepRecord, page: Page, token: str) -> None:
    """[MC] The model source a rules server shows, and the rules-instead action after a local diagnosis that needs review."""
    page.goto(f"{SERVER_B}/")
    expect(page.get_by_role("heading", name="Operator login")).to_be_visible()
    facts = page.locator("header.topbar .topbar-facts")
    record.check("Model source" not in facts.inner_text(),
                 "[MC guard] before login the top bar names no model source: it comes from an authenticated route")
    log_in(page, SERVER_B, token)
    listed = get_json(page, "/api/scenarios")
    record.check(listed.get("model_source") == RULES_SOURCE,
                 f"[MC] GET /api/scenarios gives the source the server chose at start: {listed.get('model_source')}")
    record.check(listed.get("model_modes") == ["local", "recorded", "rules"],
                 f"[MC guard] hosted is not offered on a server without its settings: {listed.get('model_modes')}")
    record.check(get_json(page, "/health").get("model_source") == RULES_SOURCE,
                 "[MC] /health gives the same source, outside public mode")
    try:  # the source arrives with GET /api/scenarios, just after the login; a build without it fails the check below
        expect(facts).to_contain_text(RULES_SOURCE_FACT)
    except AssertionError:
        record.note("the top bar never showed the model source")
    shown = " ".join(facts.inner_text().split())
    record.check(shown.startswith(RULES_SOURCE_FACT), f"[MC] the top bar's first fact reads {shown[:60]!r}")
    record.check(page.locator("header.topbar .badge.mode").inner_text() == "Rules baseline",
                 "[MC guard] the mode badge still reads Rules baseline")
    record.check(page.locator("header.topbar .badge.hosted").count() == 0, "[MC guard] no HOSTED badge on a rules server")
    walk.shot(record, page.locator("header.topbar"), "ui_21a_topbar_rules_source.png")

    run_id = create_run_in_ui(page, "worker_backlog", MC_SEED)
    panel = page.locator("section.panel.diagnosis")
    options = panel.get_by_label("Mode").locator("option").all_inner_texts()
    record.check(options == ["Local model", "Recorded run", "Rules baseline"],
                 f"[MC guard] the Mode select offers what the server accepts: {options}")
    panel.get_by_label("Mode").select_option("local")
    failed = get_json(page, f"{run_path(run_id)}/diagnoses/{diagnose_in_ui(page)}")["diagnosis"]
    record.check((failed["mode"], failed["status"]) == ("local", "needs_review")
                 and failed["reasons"][0].startswith("the model is unavailable: "),
                 f"[MC guard] with no Ollama here a local diagnosis needs review: {failed['reasons'][:1]}")
    action = panel.get_by_role("button", name=RULES_INSTEAD, exact=True)
    try:
        expect(action).to_be_visible()
    except AssertionError:
        record.note(f"no {RULES_INSTEAD!r} button appeared")
    offered = action.count() == 1 and action.is_enabled()
    record.check(offered, f"[MC] the diagnosis that needs review offers {RULES_INSTEAD!r}")
    banner = " ".join(panel.locator("div.banner.needs-review").inner_text().split())
    record.check("Needs review. The model did not produce a valid diagnosis, so nothing is proposed." in banner
                 and "The deterministic evidence is kept; escalate with it." in banner,
                 "[MC guard] the needs-review safety sentences are unchanged")
    record.check(len(get_json(page, f"{run_path(run_id)}/diagnoses")) == 1,
                 "[MC guard] nothing fell back to the rules on its own: the run holds the one local diagnosis")
    walk.shot(record, panel, "ui_21b_rules_instead_offered.png")
    if not offered:
        return  # nothing to press: the checks above have recorded the failure

    action.focus()
    with page.expect_response(path_matcher("POST", START_DIAGNOSIS)) as started:
        page.keyboard.press("Enter")
    asked = started.value.request.post_data_json
    record.check(asked == {"mode": "rules", "incident_id": failed["incident_id"]},
                 f"[MC] the action asks for a rules diagnosis of the same incident: {asked}")
    rules_job = started.value.json()["job_id"]
    wait_for_job_done(page, rules_job)
    settle(page)
    rules = get_json(page, f"{run_path(run_id)}/diagnoses/{rules_job}")
    record.check((rules["mode"], rules["diagnosis"]["status"], rules["incident_id"])
                 == ("rules", "validated", failed["incident_id"]),
                 f"[MC] the rules diagnosis is validated, for incident {rules['incident_id']}")
    focused = page.evaluate("() => (document.activeElement && document.activeElement.textContent) || ''")
    record.check(focused.strip() == DIAGNOSE_LABEL, f"[MC] focus went to Diagnose when the action went away ({focused!r})")
    record.check(panel.get_by_role("button", name=RULES_INSTEAD).count() == 0,
                 "[MC] a validated rules diagnosis offers no rules-instead action")
    expect(panel.locator("p.mode-label")).to_have_text(rules["diagnosis"]["mode_label"])
    others = panel.locator("div.alternatives li").all_inner_texts()
    record.check(any(item.startswith("live local model") and item.endswith("needs_review") for item in others),
                 f"[MC guard] the local diagnosis that needs review stays listed: {others}")
    no_error_banner(record, page, "[MC] after the rules-instead diagnosis")
    walk.shot(record, page, "ui_21c_rules_instead_done.png", full_page=True)
    walk.shared["census_m"] = page.evaluate("() => window.__walkthroughCensus")


STATUS_ONLY_HEALTH = '{"status":"ok"}'  # all that /health answers in the server's public mode


def step_status_only_health(walk: Walkthrough, record: StepRecord, page: Page, token: str) -> None:
    """[MC] A /health that holds only its status: the page renders, and shows its model source once logged in."""
    served: list[str] = []

    def status_only(route: Route) -> None:
        served.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=STATUS_ONLY_HEALTH)

    page.route(f"{SERVER_B}/health", status_only)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/health"):
        page.goto(f"{SERVER_B}/")
    heading = page.get_by_role("heading", name="Operator login")
    try:
        expect(heading).to_be_visible()
    except AssertionError:
        record.note("the login form never rendered")
    page.wait_for_timeout(QUIET_MS)  # a page that cannot read the answer fails as it renders it
    badge = page.locator("header.topbar .badge.mode")
    record.check(len(served) >= 1, f"[MC guard] the page read /health and got only its status ({len(served)} reads)")
    record.check(heading.count() == 1 and heading.is_visible(), "[MC] with a status-only /health the login page renders")
    record.check(badge.count() == 1 and badge.inner_text() == "Mode unknown",
                 "[MC] before login, with no mode in /health, the badge reads Mode unknown")
    if heading.count() == 1:
        log_in(page, SERVER_B, token)
        facts = page.locator("header.topbar .topbar-facts")
        try:
            expect(facts).to_contain_text(RULES_SOURCE_FACT)
        except AssertionError:
            record.note("the top bar never showed the model source")
        shown = " ".join(facts.inner_text().split())
        record.check(shown.startswith(RULES_SOURCE_FACT) and "Model: none (rules)" in shown,
                     f"[MC] once logged in, the source comes from GET /api/scenarios: {shown[:90]!r}")
        record.check(badge.inner_text() == "Rules baseline", f"[MC] the badge takes the source's mode: {badge.inner_text()!r}")
        create = page.get_by_role("button", name="Create synthetic run", exact=True)
        record.check(create.count() == 1 and create.is_visible(), "[MC] the console renders below the top bar")
        walk.shot(record, page, "ui_22_status_only_health.png")
    errors = [item["message"] for item in walk.recorder.page_errors if item["context"] == "P"]
    record.check(not errors, f"[MC] no page error while /health held only its status: {errors[:2]}")
    page.unroute(f"{SERVER_B}/health", status_only)
    walk.shared["census_p"] = page.evaluate("() => window.__walkthroughCensus")


def step_existing_run_listed_once(walk: Walkthrough, record: StepRecord, page: Page, token: str) -> None:
    """[MC] A 200 with a run that is already listed, as public mode answers a seed that has one: listed once, in place."""
    log_in(page, SERVER_B, token)
    settle(page)
    runs = get_json(page, "/api/runs")
    if not runs:
        raise CheckFailed("server B holds no run to seed again")
    listed = runs[-1]  # the oldest: replaced where it stands, it must not move to the top
    open_run = page.get_by_label("Open run")
    before = open_run.locator("option").all_inner_texts()

    def existing_run(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(listed))

    page.route(f"{SERVER_B}/api/runs", existing_run)
    page.get_by_label("Scenario").select_option(listed["scenario"])
    page.get_by_role("textbox", name="Seed", exact=True).fill(str(listed["seed"]))
    settle(page)
    with page.expect_response(path_matcher("POST", CREATE_RUN)) as answered:
        page.locator("section.panel.controls").get_by_role("button", name="Create synthetic run", exact=True).click()
    record.check(answered.value.status == 200, "[MC guard] the seeding answered 200 with the run that exists")
    expect(page.locator("header.topbar")).to_contain_text(f"Run: {listed['run_id']}")
    settle(page)
    page.unroute(f"{SERVER_B}/api/runs", existing_run)
    after = open_run.locator("option").all_inner_texts()
    record.check(after == before, f"[MC] the run list is as it was, the run listed once: {len(before)} options before, "
                 f"{len(after)} after, {sum(listed['run_id'] in item for item in after)} for {listed['run_id']}")
    record.check(open_run.input_value() == listed["run_id"], f"[MC guard] the page opened {listed['run_id']}")
    no_error_banner(record, page, "[MC guard] after a 200 for a run that exists")
    walk.shared["census_d"] = page.evaluate("() => window.__walkthroughCensus")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-a-log", type=Path, required=True)
    parser.add_argument("--server-b-log", type=Path, required=True)
    parser.add_argument("--cli-log", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    token_a, token_b = read_token(args.server_a_log), read_token(args.server_b_log)
    demo = read_demo_facts(args.cli_log.read_text(encoding="utf-8"))
    recorder = Recorder()
    walk = Walkthrough(args.out, recorder)
    expect.set_options(timeout=EXPECT_TIMEOUT_MS)
    started = datetime.now(tz=UTC).isoformat()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--disable-dev-shm-usage"])
        context = new_context(browser, recorder, "A", SERVER_A)
        page = context.new_page()
        current = lambda: page  # noqa: E731 - the page a failed step is photographed on
        shared = walk.shared

        walk.run("1", "Log in with the operator token and op_demo_operator; the banner reads Synthetic demonstration",
                 lambda record: step_login(walk, record, page, token_a), current)
        walk.run("2", "Open the demo run: the queue panel comes first, with its rows and totals",
                 lambda record: step_open_run(walk, record, page, demo), current)
        walk.run("3", "Filters: each changes the rows, and the counts agree with GET /orders with the same session",
                 lambda record: check_filters(walk, record, page, shared["view_a"], shot_prefix="ui_03_filter",
                                              select_keys=None), current, needs=("view_a",))
        walk.run("4", "Sorting by age and by amount",
                 lambda record: step_sorting(walk, record, page, shared["view_a"]), current, needs=("view_a",))
        walk.run("5", "Case detail: timeline order, blockers, next step, and record text shown as text",
                 lambda record: step_case_detail(walk, record, page, shared["view_a"]), current, needs=("view_a",))
        walk.run("6", "Receipt links open and match GET /actions/{id}/receipt",
                 lambda record: step_receipt(walk, record, page, context, shared["view_a"], demo), current,
                 needs=("view_a", "recovered_order"))
        walk.run("8", "Keyboard focus order on the queue table; accessible names of the controls",
                 lambda record: step_keyboard(walk, record, page, context, shared["view_a"]), current, needs=("view_a",))
        walk.run("7a", "Regression: the seeded run's incidents, diagnosis, recovery panels and exported plan/approval state",
                 lambda record: step_seeded_flow_a(walk, record, page, shared["view_a"]), current, needs=("view_a",))
        census_a = page.evaluate("() => window.__walkthroughCensus")
        holder: dict[str, Page] = {}
        walk.run("7b", "Regression: Flow A driven through the UI on a fresh server (diagnose, validate, plan, refresh, "
                 "approve, execute, replay), then the queue and receipt of that recovery",
                 lambda record: step_ui_flow_a(walk, record, browser, recorder, token_b, demo, holder),
                 lambda: holder.get("page"))
        # [X] After every one of U's and V's steps, so none of them runs on a different state than before.
        x_context = new_context(browser, recorder, "X", SERVER_B)
        x_page = x_context.new_page()
        request_log = RequestLog()
        request_log.attach(x_page)
        walk.run("9", "[X] Controls that wait on their own request keep focus and refuse a second press: Create synthetic "
                 "run, +1 min, +15 min, +1 h, Diagnose the latest incident",
                 lambda record: step_busy_controls(walk, record, x_page, x_context, token_b, request_log), lambda: x_page)
        walk.run("9g", "[X] Found, not changed: a second press while the diagnosis start request is in flight",
                 lambda record: step_start_request_gap(walk, record, x_page, request_log), lambda: x_page,
                 needs=("x_diagnosed_run",))
        x_context.close()
        # [Q33] After every one of U's, V's and X's steps, in a context of its own on server B.
        q_context = new_context(browser, recorder, "Q", SERVER_B)
        q_page = q_context.new_page()
        q_log = RequestLog()
        q_log.attach(q_page)
        walk.run("10", "[Q33] Start session keeps focus after a failed login; the busy line is a polite live region; "
                 "Diagnose is busy during its own start request only",
                 lambda record: releasing_holds(lambda: step_q33_login_region_diagnose(walk, record, q_page, q_context,
                                                                                       token_b, q_log)),
                 lambda: q_page, needs=("x_diagnosed_run",))
        walk.run("11", "[Q33] Flow A: Validate, Build plan, Refresh and re-plan, Approve and Execute refuse a second "
                 "press; focus when the stale-orders row and Approve go",
                 lambda record: releasing_holds(lambda: step_q33_flow_a(walk, record, q_page, q_context, q_log, demo)),
                 lambda: q_page, needs=("q33_logged_in",))
        walk.run("12", "[Q33] Flow B: Escalate refuses a second press; focus when it goes",
                 lambda record: releasing_holds(lambda: step_q33_escalate(walk, record, q_page, q_context, q_log)),
                 lambda: q_page, needs=("q33_logged_in",))
        walk.run("13", "[Q33] Flow C: Refresh provider state for N payments, the remounted Recovery panel, Prepare "
                 "provider replay instructions",
                 lambda record: releasing_holds(lambda: step_q33_flow_c(walk, record, q_page, q_context, q_log)),
                 lambda: q_page, needs=("q33_logged_in",))
        walk.run("14", "[Q33] Export report (JSON) and Log out refuse a second press",
                 lambda record: releasing_holds(lambda: step_q33_report_logout(walk, record, q_page, q_context, q_log)),
                 lambda: q_page, needs=("q33_flow_c_run",))
        q_context.close()
        # [Q36] After every earlier step, in a context of its own on server B.
        r_context = new_context(browser, recorder, "R", SERVER_B)
        r_page = r_context.new_page()
        walk.run("15", "[Q36] Focus after Log out goes to the Operator token field, after a login to the Scenario select",
                 lambda record: releasing_holds(lambda: step_q36_login_logout(walk, record, r_page, token_b)),
                 lambda: r_page)
        walk.run("16", "[Q36] A running diagnosis is announced once, not on each poll",
                 lambda record: releasing_holds(lambda: step_q36_running_diagnosis(walk, record, r_page)),
                 lambda: r_page, needs=("q36_logged_in", "x_diagnosed_run"))
        walk.run("17", "[Q36] Approve's focus target is the 'Approved by' line: nothing scrolls, Execute is one Tab away",
                 lambda record: releasing_holds(lambda: step_q36_approve(walk, record, r_page, r_context, demo)),
                 lambda: r_page, needs=("q36_logged_in",))
        r_context.close()
        # [UI-A] After every earlier step, in a context of its own on server B.
        g_context = new_context(browser, recorder, "G", SERVER_B)
        g_page = g_context.new_page()
        walk.run("18", "[UI-A] The guided console: a recovery through the UI, the page at 1440, 1024 and 390 px, motion "
                 "under prefers-reduced-motion, and the rail's needs-review and no_action paths",
                 lambda record: releasing_holds(lambda: step_ui_a_console(walk, record, g_page, token_b, demo)),
                 lambda: g_page)
        g_context.close()
        # [UI-B] After every earlier step, in a context of its own on server B.
        u_context = new_context(browser, recorder, "U", SERVER_B)
        u_page = u_context.new_page()
        walk.run("19", "[UI-B] Round 2: a rejected execution reaches the rail and the strip; validating or planning again "
                 "keeps an execution's answer; a late receipt read or diagnosis poll changes nothing after the run "
                 "changes or the operator logs out",
                 lambda record: releasing_holds(lambda: step_ui_b_round_two(walk, record, u_page, token_b, demo)),
                 lambda: u_page)
        u_context.close()
        # [UI-C] After every earlier step, in a context of its own on server B.
        v_context = new_context(browser, recorder, "V", SERVER_B)
        v_page = v_context.new_page()
        walk.run("20", "[UI-C] Round 3: a diagnosis start answer or a run reload that arrives after another run was opened, "
                 "or after Log out, writes nothing and shows no error",
                 lambda record: releasing_holds(lambda: step_ui_c_left_runs(walk, record, v_page, token_b, demo)),
                 lambda: v_page)
        v_context.close()
        # [MC] After every earlier step, in a context of its own on server B.
        m_context = new_context(browser, recorder, "M", SERVER_B)
        m_page = m_context.new_page()
        walk.run("21", "[MC] The model source in the top bar of a rules server, and the rules-instead action after a "
                 "local diagnosis that needs review",
                 lambda record: releasing_holds(lambda: step_model_chain(walk, record, m_page, token_b)),
                 lambda: m_page)
        m_context.close()
        # [MC] /health as the server's public mode answers it, in a context of its own on server B.
        p_context = new_context(browser, recorder, "P", SERVER_B)
        p_page = p_context.new_page()
        walk.run("22", "[MC] A /health that holds only its status, as public mode answers: the page renders, and shows "
                 "the model source once logged in",
                 lambda record: releasing_holds(lambda: step_status_only_health(walk, record, p_page, token_b)),
                 lambda: p_page)
        p_context.close()
        # [MC] Public mode's 200 for a seed that already has a run, in a context of its own on server B.
        d_context = new_context(browser, recorder, "D", SERVER_B)
        d_page = d_context.new_page()
        walk.run("23", "[MC] A 200 with a run that is already listed, as public mode answers a seed that has one: the run "
                 "list stays as it was",
                 lambda record: releasing_holds(lambda: step_existing_run_listed_once(walk, record, d_page, token_b)),
                 lambda: d_page)
        d_context.close()
        recorder.step = "teardown"
        browser_version = browser.version
        context.close()
        browser.close()

    finished = datetime.now(tz=UTC).isoformat()
    results = {
        "started": started, "finished": finished, "browser": f"chromium {browser_version}",
        "demo_facts": asdict(demo),
        "steps": [asdict(step) for step in walk.steps],
        "console": recorder.console, "page_errors": recorder.page_errors, "failed_requests": recorder.failed_requests,
        "http_errors": recorder.http_errors,
        "responses": {"total": len(recorder.responses),
                      "by_status": dict(Counter(str(item["status"]) for item in recorder.responses))},
        "census": {"A": census_a, "B": walk.shared.get("census_b"), "X": walk.shared.get("census_x"),
                   "Q": walk.shared.get("census_q"), "R": walk.shared.get("census_r"), "G": walk.shared.get("census_g"),
                   "U": walk.shared.get("census_u"), "V": walk.shared.get("census_v"), "M": walk.shared.get("census_m"),
                   "P": walk.shared.get("census_p"), "D": walk.shared.get("census_d")},
        "shared": {key: value for key, value in walk.shared.items()
                   if key not in {"view_a", "recovered_doc", "census_b", "census_x", "census_q", "census_r", "census_g",
                                  "census_u", "census_v", "census_m", "census_p", "census_d"}},
    }
    (args.out / "ui_results.json").write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
    print("\n==== summary ====")
    for step in walk.steps:
        added = [item for item in step.checks if item.split(": ", 1)[1].startswith("[V]")]
        added_x = [item for item in step.checks if item.split(": ", 1)[1].startswith("[X]")]
        added_q = [item for item in step.checks if item.split(": ", 1)[1].startswith("[Q33]")]
        guards_q = [item for item in step.checks if item.split(": ", 1)[1].startswith("[Q33 guard]")]
        added_q36 = [item for item in step.checks if item.split(": ", 1)[1].startswith("[Q36]")]
        guards_q36 = [item for item in step.checks if item.split(": ", 1)[1].startswith("[Q36 guard]")]
        added_ui_a = [item for item in step.checks if item.split(": ", 1)[1].startswith("[UI-A]")]
        added_ui_b = [item for item in step.checks if item.split(": ", 1)[1].startswith("[UI-B]")]
        guards_ui_b = [item for item in step.checks if item.split(": ", 1)[1].startswith("[UI-B guard]")]
        added_ui_c = [item for item in step.checks if item.split(": ", 1)[1].startswith("[UI-C]")]
        guards_ui_c = [item for item in step.checks if item.split(": ", 1)[1].startswith("[UI-C guard]")]
        added_mc = [item for item in step.checks if item.split(": ", 1)[1].startswith("[MC]")]
        guards_mc = [item for item in step.checks if item.split(": ", 1)[1].startswith("[MC guard]")]
        labelled = (added + added_x + added_q + guards_q + added_q36 + guards_q36 + added_ui_a + added_ui_b + guards_ui_b
                    + added_ui_c + guards_ui_c + added_mc + guards_mc)
        kept = [item for item in step.checks if item not in labelled]
        adjusted = [item for item in kept if "[V-adjusted" in item]
        adjusted_ui_a = [item for item in step.checks if "[UI-A-adjusted" in item]
        adjusted_ui_b = [item for item in step.checks if "[UI-B-adjusted" in item]
        adjusted_q = [item for item in added_q if "[Q36-adjusted" in item]
        failed = lambda items: sum(item.startswith("FAIL") for item in items)  # noqa: E731 - a count for the line below
        print(f"step {step.number}: {step.status} ({len(step.checks)} checks, {len(step.failed_checks)} failed)"
              f"{'; ' + step.error if step.error else ''}"
              f" | U's {len(kept)} ({failed(kept)} failed, {len(adjusted)} [V-adjusted])"
              f" + V's {len(added)} ({failed(added)} failed)"
              f" + X's {len(added_x)} ({failed(added_x)} failed)"
              f" + Q33's {len(added_q)} ({failed(added_q)} failed, {len(adjusted_q)} [Q36-adjusted])"
              f" + Q33 guards {len(guards_q)} ({failed(guards_q)} failed)"
              f" + Q36's {len(added_q36)} ({failed(added_q36)} failed)"
              f" + Q36 guards {len(guards_q36)} ({failed(guards_q36)} failed)"
              f" + UI-A's {len(added_ui_a)} ({failed(added_ui_a)} failed); {len(adjusted_ui_a)} [UI-A-adjusted]"
              f" + UI-B's {len(added_ui_b)} ({failed(added_ui_b)} failed) + UI-B guards {len(guards_ui_b)} "
              f"({failed(guards_ui_b)} failed); {len(adjusted_ui_b)} [UI-B-adjusted] ({failed(adjusted_ui_b)} failed)"
              f" + UI-C's {len(added_ui_c)} ({failed(added_ui_c)} failed) + UI-C guards {len(guards_ui_c)} "
              f"({failed(guards_ui_c)} failed)"
              f" + MC's {len(added_mc)} ({failed(added_mc)} failed) + MC guards {len(guards_mc)} ({failed(guards_mc)} failed)")
    print(f"console messages {len(recorder.console)} ({Counter(item['type'] for item in recorder.console)}); "
          f"page errors {len(recorder.page_errors)}; failed requests {len(recorder.failed_requests)}; "
          f"HTTP errors {len(recorder.http_errors)}; responses {len(recorder.responses)}")
    print("census: " + "; ".join(f"page {label}: unknown elements {len(item['unknown'])}, CSP violations {len(item['csp'])}"
                                 if item else f"page {label}: not read" for label, item in results["census"].items()))
    return 0 if all(step.status == "pass" for step in walk.steps) else 1


if __name__ == "__main__":
    sys.exit(main())
