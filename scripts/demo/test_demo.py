#!/usr/bin/env python3
"""Tests the built recorded demo as a visitor meets it on a static host (scripts/demo/test-demo.sh runs it).

It runs inside the UI walkthrough image with no network. static_server.py serves the built site on 127.0.0.1 with the
headers of the site's own _headers file, and headless Chromium takes the README's three flows at 1440 px, then again in
part at 390 px. Every value the test expects is read from the site's recording (demo-data/), so a check says the page
shows what the real API answered; the values the demo walkthrough shows are checked as well. It also checks:
  * the recorded-demo banner at the top of every view;
  * every Show filter at every step of every flow, and the case view of the orders the queue lists first;
  * the receipts open from the site's static files;
  * the controls the recording cannot answer get an honest message, and logging out asks for no token;
  * no request leaves the origin, no Content-Security-Policy violation, no page error.
It writes demo_results.json and screenshots (demo_*_1440.png, demo_*_390.png) to --out; exit 0 only if all pass.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import Browser, BrowserContext, Locator, Page, Response, expect, sync_playwright

PORT = 8080
ORIGIN = f"http://127.0.0.1:{PORT}"
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
CENSUS_SCRIPT = Path("/opt/walkthrough/census.js")  # the UI walkthrough's census, in the image
EXPECT_TIMEOUT_MS = 15_000
OPERATOR = "op_demo_operator"
BANNER_PHRASES = (
    "Recorded demo.",
    "Every answer on this page is a recorded response from the real RayTrace API in rules mode.",
    "Nothing runs here: no provider is called and no money moves.",
)
REFUSAL = "Not in this recorded demo: "
CAPTURED = "Captured, processing incomplete"
AWAITING = "Still awaiting an action or a hold"
RESTORED = "Restored processing value"
NEW_CAPTURE = "New capture"
NOT_YET_KNOWN = "Not yet known"
MAIN_FILTERS: dict[str, dict[str, list[str]]] = {
    "captured_unprocessed": {"state": ["captured_processing_incomplete"]},
    "unresolved": {"state": ["payment_state_unresolved"]},
    "manual_review": {"next_step": ["manual_review"]},
    "completed": {"state": ["complete"]},
}
ROW_IDS_JS = """(panel) => [...panel.querySelectorAll(":scope > div.scroll > table > tbody > tr > td:first-child > button")]
  .map((button) => button.textContent)"""
SHOW_OPTIONS_JS = "(select) => [...select.options].map((option) => [option.value, option.textContent])"
FIELDS_JS = """(list) => Object.fromEntries([...list.querySelectorAll(":scope > dt")]
  .map((term) => [term.textContent, term.nextElementSibling?.textContent ?? null]))"""
MONEY_JS = """() => {
  const strip = document.querySelector("main .run-header .money-strip");
  if (strip === null) return null;
  const figures = {};
  for (const figure of strip.querySelectorAll(":scope > dl.figures > .figure")) {
    figures[figure.querySelector(":scope > dt").textContent] = {
      value: figure.querySelector(":scope > dd.figure-value")?.textContent ?? null,
      detail: figure.querySelector(":scope > dd.figure-detail")?.textContent ?? null,
    };
  }
  return figures;
}"""
RAIL_JS = """() => [...document.querySelectorAll("main .run-header .rail > ol.rail-steps > li")].map((step) => ({
  state: step.querySelector(":scope > .step-state")?.textContent ?? null,
  proof: [...step.querySelectorAll(":scope > .step-proof > .proof-line")].map((line) => line.textContent),
  link: step.querySelector(":scope > .step-proof > a")?.getAttribute("href") ?? null,
}))"""


class CheckFailed(AssertionError):
    """A check the step cannot go on without."""


# --------------------------------------------------------------------------- the page's formatting (src/format.ts)


def replay_time(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def rupees(paise: int) -> str:
    whole, fraction = divmod(abs(paise), 100)
    return f"{'-' if paise < 0 else ''}INR {whole:,}.{fraction:02d}"


def count_of(count: int, noun: str) -> str:
    return f"{count} {noun if count == 1 else noun + 's'}"


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


def hash_proof(digest: str) -> str:
    return f"hash {digest.removeprefix('sha256:')[:12]}…"


# --------------------------------------------------------------------------- the recording the site carries


class Recording:
    """The site's demo-data: what the real API answered at each stage of each run's flow."""

    def __init__(self, site: Path) -> None:
        self.site = site
        self.manifest = json.loads((site / "demo-data" / "manifest.json").read_text(encoding="utf-8"))
        self.runs = {run["flow"]: run for run in self.manifest["runs"]}
        self._files: dict[str, dict] = {}

    def _stage(self, run: dict, stage: int) -> dict:
        name = run["stages"][stage]["file"]
        if name not in self._files:
            self._files[name] = json.loads((self.site / name).read_text(encoding="utf-8"))
        return self._files[name]

    def answer(self, run: dict, stage: int, key: str) -> dict:
        entry = self._stage(run, stage)["answers"].get(key)
        if entry is None:
            raise CheckFailed(f"the recording holds no answer to {key} at stage {stage} of {run['run_id']}")
        if "same_as_stage" in entry:
            entry = self._stage(run, entry["same_as_stage"])["answers"][key]
        return json.loads(entry["answers"][-1]["body"])

    def job(self, run: dict, stage: int) -> dict:
        polls = [key for key in self._stage(run, stage)["answers"] if "/diagnoses/job" in key]
        return self.answer(run, stage, polls[0])

    @staticmethod
    def change(run: dict, label: str) -> dict:
        return next(json.loads(item["answer"]["body"]) for item in run["transitions"] if item["label"] == label)

    @staticmethod
    def clock(run: dict, stage: int) -> int:
        return run["stages"][stage]["clock_ms"]


def queue_key(run_id: str, query: dict[str, list[str]]) -> str:
    params = [("limit", "5000"), *(("state", item) for item in query.get("state", [])),
              *(("next_step", item) for item in query.get("next_step", []))]
    return f"GET /api/runs/{run_id}/orders?{urlencode(params)}"


def option_query(key: str, states: list[dict]) -> dict[str, list[str]]:
    """What each Show option asks the server for (src/components/QueuePanel.tsx, filterChoices)."""
    if key == "all":
        return {}
    if key in MAIN_FILTERS:
        return MAIN_FILTERS[key]
    if key == "every_unresolved":
        return {"state": [rule["state"] for rule in states if rule["unresolved"]]}
    if key.startswith("state:"):
        return {"state": [key.removeprefix("state:")]}
    raise CheckFailed(f"unexpected Show option {key!r}")


# --------------------------------------------------------------------------- results


@dataclass
class StepRecord:
    name: str
    status: str = "not run"
    checks: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    error: str | None = None

    def check(self, passed: bool, text: str) -> bool:
        self.checks.append(f"{'PASS' if passed else 'FAIL'}: {text}")
        return passed

    @property
    def failed(self) -> list[str]:
        return [item for item in self.checks if item.startswith("FAIL")]


@dataclass
class Observed:
    """Everything the browser reported, in every context and tab."""

    requests: list[str] = field(default_factory=list)
    console: list[dict] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    http_errors: list[str] = field(default_factory=list)
    census: dict[str, dict] = field(default_factory=dict)
    downloads: list[str] = field(default_factory=list)

    def attach(self, context: BrowserContext, label: str) -> None:
        context.on("request", lambda request: self.requests.append(request.url))
        context.on("requestfailed", lambda request: self.failed_requests.append(f"{request.url}: {request.failure}"))
        context.on("response", lambda response: self.http_errors.append(f"{response.status} {response.url}")
                   if response.status >= 400 else None)
        context.on("console", lambda message: self.console.append(
            {"context": label, "type": message.type, "text": message.text}))
        context.on("weberror", lambda problem: self.page_errors.append(f"{label}: {problem.error.message}"))

    def read_census(self, page: Page, label: str) -> None:
        self.census[label] = page.evaluate("() => window.__walkthroughCensus ?? null")


class Suite:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.steps: list[StepRecord] = []

    @contextmanager
    def step(self, name: str, page: Page) -> Iterator[StepRecord]:
        record = StepRecord(name)
        self.steps.append(record)
        print(f"[test] {name}", flush=True)
        try:
            yield record
        except Exception as problem:  # a failed step is recorded and the next one runs
            record.error = f"{type(problem).__name__}: {problem}"
            record.check(False, f"the step ran to its end ({record.error.splitlines()[0][:400]})")
            traceback.print_exc()
            shot = f"failed_{len(self.steps):02d}.png"
            try:
                page.screenshot(path=str(self.out / shot), full_page=True)
                record.screenshots.append(shot)
            except Exception as shot_problem:  # noqa: BLE001 - the step's own failure is already recorded
                record.checks.append(f"NOTE: no failure screenshot: {shot_problem}")
        record.status = "pass" if not record.failed else "fail"

    def shot(self, record: StepRecord, page: Page, name: str, *, full_page: bool = False) -> None:
        page.screenshot(path=str(self.out / name), full_page=full_page)
        record.screenshots.append(name)


# --------------------------------------------------------------------------- the page


def topbar(page: Page) -> Locator:
    return page.locator("header.topbar")


def queue(page: Page) -> Locator:
    return page.locator("main section.panel.queue")


def show(page: Page) -> Locator:
    return queue(page).locator(":scope > div.row select")


def recovery(page: Page) -> Locator:
    return page.locator("section.panel.recovery")


def diagnosis(page: Page) -> Locator:
    return page.locator("section.panel.diagnosis")


def errors_shown(page: Page) -> list[str]:
    return page.locator("p.banner.error").all_inner_texts()


def wait_rows(page: Page, wanted: list[str]) -> bool:
    deadline = time.monotonic() + EXPECT_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if queue(page).evaluate(ROW_IDS_JS) == wanted:
            return True
        page.wait_for_timeout(40)
    return False


def check_banner(record: StepRecord, page: Page, where: str) -> None:
    banner = page.locator("section.demo-banner")
    visible = banner.count() == 1 and banner.is_visible()
    box = banner.bounding_box() if visible else None
    text = banner.inner_text() if visible else ""
    width = page.viewport_size["width"] if page.viewport_size else 0
    at_top = box is not None and abs(box["y"]) <= 1 and box["width"] >= width - 20
    record.check(visible and at_top and all(phrase in text for phrase in BANNER_PHRASES),
                 f"{where}: the recorded-demo banner is at the top of the view, across its width, and says it")
    badge = page.locator("header.topbar .badge.sandbox")
    record.check(badge.count() == 1 and badge.inner_text() == "Synthetic demonstration",
                 f"{where}: the top bar keeps its 'Synthetic demonstration' badge")


def check_no_error(record: StepRecord, page: Page, where: str) -> None:
    shown = errors_shown(page)
    record.check(not shown, f"{where}: no error banner{'; it says ' + repr(shown) if shown else ''}")


def check_refusal(record: StepRecord, page: Page, where: str, *phrases: str) -> None:
    banner = page.locator("p.banner.error")
    expect(banner).to_contain_text(REFUSAL)
    text = banner.inner_text()
    notice = page.locator("section.demo-banner p.demo-notice")
    record.check(all(phrase in text for phrase in phrases) and "The demo covers the README's three flows." in text,
                 f"{where}: the error banner gives the honest message {text!r}")
    record.check(notice.count() == 1 and REFUSAL in notice.inner_text(),
                 f"{where}: the sticky demo banner repeats it where the visitor is looking")


def check_money(record: StepRecord, page: Page, totals: dict, where: str) -> None:
    figures = page.evaluate(MONEY_JS) or {}
    wanted = {
        CAPTURED: {"value": rupees(totals["captured_unprocessed_value_paise"]),
                   "detail": captured_detail(totals)},
        AWAITING: {"value": rupees(totals["unresolved_value_paise"]), "detail": awaiting_detail(totals)},
    }
    shown = {name: figures.get(name) for name in wanted}
    record.check(shown == wanted, f"{where}: Money at stake shows {shown}; the recorded queue totals {wanted}")


def check_every_filter(record: StepRecord, page: Page, recording: Recording, run: dict, stage: int, whole: dict,
                       where: str) -> None:
    options = show(page).evaluate(SHOW_OPTIONS_JS)
    problems = []
    for value, label in options:
        answer = recording.answer(run, stage, queue_key(run["run_id"], option_query(value, whole["states"])))
        show(page).select_option(value)
        if not wait_rows(page, [row["order_id"] for row in answer["orders"]]):
            problems.append(f"{label}: the table never listed the recorded {answer['matching_orders']} orders")
        count = re.search(r"\((\d+)\)$", label)
        if count is None or int(count.group(1)) != answer["matching_orders"]:
            problems.append(f"{label}: the recorded answer matches {answer['matching_orders']}")
        if errors_shown(page):
            problems.append(f"{label}: {errors_shown(page)}")
    show(page).select_option("all")
    wait_rows(page, [row["order_id"] for row in whole["orders"]])
    record.check(not problems and len(options) == 18,
                 f"{where}: each of the {len(options)} Show options lists exactly the recorded orders, with the recorded "
                 f"count{'; ' + '; '.join(problems) if problems else ''}")


def sort_by(page: Page, label: str, direction: str) -> None:
    header = queue(page).locator(":scope > div.scroll > table > thead th", has=page.locator("button", has_text=label))
    header.locator("button").click()
    expect(header).to_have_attribute("aria-sort", direction)


def open_case(page: Page, order_id: str) -> Locator:
    queue(page).locator(f':scope > div.scroll > table > tbody > tr > td:first-child > button:text-is("{order_id}")').click()
    detail = queue(page).locator(":scope > div.case-detail")
    expect(detail.locator(":scope > h3")).to_contain_text(f"Case {order_id}")
    return detail


def close_case(page: Page) -> None:
    detail = queue(page).locator(":scope > div.case-detail")
    if detail.count():
        detail.get_by_role("button", name="Close").click()
        expect(detail).to_have_count(0)


def check_first_cases(record: StepRecord, page: Page, whole: dict, where: str) -> None:
    """The case of the first order the whole queue lists in order-ID order, by Amount and by Age."""
    as_of = f"As of replay time {replay_time(whole['as_of_ms'])}."
    opened = []
    for label, direction in (("Order", "ascending"), ("Amount", "descending"), ("Age", "descending")):
        if label != "Order":
            sort_by(page, label, direction)
        first = queue(page).evaluate(ROW_IDS_JS)[0]
        detail = open_case(page, first)
        expect(detail).to_contain_text(as_of)
        opened.append(first)
        close_case(page)
    sort_by(page, "Order", "ascending")
    record.check(not errors_shown(page), f"{where}: the cases of {opened}, listed first by order, Amount and Age, open "
                                         f"as of {replay_time(whole['as_of_ms'])}")


def check_stage(record: StepRecord, page: Page, recording: Recording, run: dict, stage: int, where: str) -> dict:
    """At one stage of a run's flow: the banner, the clock, the money strip, every Show filter and the first cases."""
    check_banner(record, page, where)
    whole = recording.answer(run, stage, queue_key(run["run_id"], {}))
    expect(topbar(page).locator(".topbar-facts")).to_contain_text(f"Replay clock: {replay_time(recording.clock(run, stage))}")
    expect(queue(page).locator(":scope > p.lead")).to_contain_text(f"as of replay time {replay_time(whole['as_of_ms'])}")
    check_money(record, page, whole["totals"], where)
    check_every_filter(record, page, recording, run, stage, whole, where)
    check_first_cases(record, page, whole, where)
    check_no_error(record, page, where)
    return whole


def open_run(page: Page, run: dict) -> None:
    page.get_by_label("Open run").select_option(run["run_id"])
    expect(topbar(page).locator(".topbar-facts")).to_contain_text(f"Run: {run['run_id']}")
    expect(show(page)).to_be_visible()


def rail_states(page: Page) -> list[str | None]:
    return [step["state"] for step in page.evaluate(RAIL_JS)]


def diagnose(page: Page) -> dict:
    diagnosis(page).get_by_role("button", name="Diagnose the latest incident").click()
    expect(diagnosis(page).locator("p.job-line strong.status")).to_have_text("done")
    return diagnosis(page).locator("dl.diagnosis-fields").evaluate(FIELDS_JS)


def check_diagnosis(record: StepRecord, fields: dict, job: dict, where: str) -> None:
    output = job["diagnosis"]["output"]
    wanted = {"Leading hypothesis": output["hypothesis"], "Faulting change": output["faulting_change_id"],
              "Affected method": output["affected_method"]}
    shown = {name: fields.get(name) for name in wanted}
    proposed = fields.get("Proposed operation") or ""
    record.check(shown == wanted and proposed.startswith(output["proposed_operation"]),
                 f"{where}: the diagnosis shows {shown}, proposing {proposed!r}: the recorded answer")


def receipt_tab(context: BrowserContext, observed: Observed, link: Locator, label: str,
                shot: Path | None = None) -> tuple[str, int, str, str]:
    """Opens a receipt link as a visitor does; the tab's URL, and its navigation's status, content type and text."""
    navigations: list[Response] = []

    def remember(response: Response) -> None:
        if response.request.is_navigation_request() and "/receipts/" in response.url:
            navigations.append(response)

    context.on("response", remember)
    try:
        with context.expect_page() as opened:
            link.click()
        tab = opened.value
        tab.wait_for_load_state("load")
    finally:
        context.remove_listener("response", remember)
    response = next((item for item in navigations if item.url == tab.url), None)
    if response is None:
        raise CheckFailed(f"no navigation response was seen for the receipt tab {tab.url}")
    text = tab.locator("body").inner_text()
    observed.read_census(tab, label)
    if shot is not None:
        tab.screenshot(path=str(shot))
    url, status, content_type = tab.url, response.status, response.headers.get("content-type", "")
    tab.close()
    return url, status, content_type, text


# --------------------------------------------------------------------------- the flows


def step_start(suite: Suite, page: Page, recording: Recording, headers_wanted: dict[str, str]) -> None:
    with suite.step("The page opens as a recorded demo, logged in, with no token prompt", page) as record:
        response = page.goto(f"{ORIGIN}/")
        if response is None:
            raise CheckFailed("no response for the page")
        sent = {name: response.headers.get(name.lower()) for name in headers_wanted}
        record.check(sent == headers_wanted, f"the page arrives with the _headers file's headers {sent}")
        meta = page.locator('meta[http-equiv="Content-Security-Policy"]').get_attribute("content")
        record.check(meta is not None and "frame-ancestors" not in meta and
                     meta == "; ".join(part for part in headers_wanted["Content-Security-Policy"].split("; ")
                                       if not part.startswith("frame-ancestors")),
                     f"the page carries the same policy in a meta element, without frame-ancestors: {meta!r}")
        expect(topbar(page).locator(".operator")).to_contain_text(f"Operator {OPERATOR}")
        record.check("recorded demo" in page.title(), f"the page title says recorded demo: {page.title()!r}")
        check_banner(record, page, "start")
        record.check(page.locator("input[type=password]").count() == 0 and page.get_by_text("Operator token").count() == 0,
                     "no field asks for a token or a password")
        expect(topbar(page).locator(".badge.mode")).to_have_text("Rules baseline")
        listed = page.get_by_label("Open run").locator("option").all_inner_texts()[1:]
        wanted = sorted(f"{run['run_id']} ({run['scenario']}, seed {run['seed']})" for run in recording.manifest["runs"])
        record.check(sorted(listed) == wanted, f"Open run lists the three recorded runs: {listed}")
        page.locator("details.demo-guide > summary").click()
        guide = page.locator("details.demo-guide").inner_text()
        record.check(all(run["run_id"] in guide for run in recording.manifest["runs"]) and "Not in the recording" in guide,
                     "the guide names each flow's run and what the recording does not cover")
        suite.shot(record, page, "demo_01_start_guide_1440.png")
        page.locator("details.demo-guide > summary").click()
        check_no_error(record, page, "start")


def step_flow_a(suite: Suite, page: Page, context: BrowserContext, recording: Recording, observed: Observed) -> None:
    run = recording.runs["A"]
    run_id = run["run_id"]
    with suite.step(f"Flow A ({run_id}): opened", page) as record:
        open_run(page, run)
        whole = check_stage(record, page, recording, run, 0, "A opened")
        options = [label for _, label in show(page).evaluate(SHOW_OPTIONS_JS)]
        documented = ["All orders (168)", "Captured, processing incomplete (98)", "Payment state unresolved (7)",
                      "Manual review (9)", "Complete (45)"]
        record.check(options[:5] == documented, f"A opened: the Show list begins {options[:5]} (docs: {documented})")
        figures = page.evaluate(MONEY_JS)
        record.check(figures[CAPTURED] == {"value": "INR 119,592.00", "detail": "98 orders"}
                     and figures[RESTORED]["value"] == NOT_YET_KNOWN and figures[NEW_CAPTURE]["value"] == NOT_YET_KNOWN,
                     f"A opened: the documented INR 119,592.00 over 98 orders, and nothing executed yet: {figures}")
        record.check(whole["matching_orders"] == 168, "A opened: the recorded queue holds 168 orders")
        page.evaluate("() => window.scrollTo(0, 0)")
        suite.shot(record, page, "demo_02_flow_a_opened_1440.png", full_page=True)

    with suite.step("Flow A: diagnose", page) as record:
        fields = diagnose(page)
        check_diagnosis(record, fields, recording.job(run, 1), "A diagnosed")
        record.check(fields.get("Faulting change") == "deploy_9b85880366", "A diagnosed: the documented faulting change")
        record.check(rail_states(page) == ["Done", "Current", "Not yet", "Not yet", "Not yet", "Not yet"],
                     f"A diagnosed: the rail reads {rail_states(page)}")
        check_stage(record, page, recording, run, 1, "A diagnosed")

    with suite.step("Flow A: the corrected handler the page opens on is not recorded, and says so", page) as record:
        chosen = recovery(page).get_by_label("Corrected handler").input_value()
        recovery(page).get_by_role("button", name="Validate this corrected handler").click()
        check_refusal(record, page, f"A validate {chosen}", f"validating the corrected handler {chosen}",
                      "The recording validated payload@5")
        record.check(recovery(page).locator("p.verdict").count() == 0, "no validation verdict is invented")
        page.locator("section.panel.recovery").scroll_into_view_if_needed()
        check_banner(record, page, "A refused validation, scrolled to Recovery")
        suite.shot(record, page, "demo_03_flow_a_refusal_1440.png")

    with suite.step("Flow A: validate payload@5", page) as record:
        recovery(page).get_by_label("Corrected handler").select_option("payload@5")
        recovery(page).get_by_role("button", name="Validate this corrected handler").click()
        validation = recording.change(run, "validate the corrected handler payload@5")
        verdict = (f"{validation['fix_id']}: reproduced on the deployed handler {validation['deployed_version']}: "
                   f"{'yes' if validation['reproduced'] else 'no'} (failed {', '.join(validation['deployed_failures']) or 'nothing'}); "
                   f"corrected handler failed {', '.join(validation['corrected_failures']) or 'nothing'}; "
                   f"{'passed' if validation['passed'] else 'did not pass'} (validated by {validation['operator_id']})")
        expect(recovery(page).locator("p.verdict")).to_have_text(verdict)
        record.check(True, f"A validated: the verdict reads the recorded answer {verdict!r}")
        record.check(page.locator("section.demo-banner p.demo-notice").count() == 0,
                     "the earlier refusal is cleared once the flow moves on")
        check_stage(record, page, recording, run, 2, "A validated")

    with suite.step("Flow A: build the plan", page) as record:
        recovery(page).get_by_role("button", name="Build reprocessing plan").click()
        plan = recording.change(run, "build the reprocessing plan")
        total = (f"{plan['target_count']} target orders, value {rupees(plan['target_value_paise'])} (from "
                 f"{plan['candidate_orders']} candidate orders). Dry runs passed: {plan['dry_runs_passed']} of {plan['target_count']}.")
        expect(recovery(page).locator("p.plan-total")).to_have_text(total)
        record.check(total.startswith("98 target orders, value INR 119,592.00"), f"A planned: {total!r} (docs: 98, INR 119,592.00)")
        check_stage(record, page, recording, run, 3, "A planned")
        recovery(page).get_by_role("button", name=re.compile(r"^Approve plan ")).click()
        check_refusal(record, page, "A approve the first plan", f"approving plan {plan['plan_id']} on {run_id}",
                      "next step for " + run_id + " is to refresh the stale-only orders' provider state")
        record.check(recovery(page).locator("p.approved").count() == 0, "no approval is invented for the first plan")

    with suite.step("Flow A: refresh the stale-only orders and re-plan", page) as record:
        recovery(page).get_by_role("button", name="Refresh provider state (read-only) and re-plan").click()
        expect(recovery(page).locator("div.plan p.note")).to_contain_text("Replaces draft")
        plan = recording.change(run, "build the plan again")
        refreshed = recording.change(run, "refresh the stale-only orders' provider state (read-only)")
        total = (f"{plan['target_count']} target orders, value {rupees(plan['target_value_paise'])} (from "
                 f"{plan['candidate_orders']} candidate orders). Dry runs passed: {plan['dry_runs_passed']} of {plan['target_count']}.")
        expect(recovery(page).locator("p.plan-total")).to_have_text(total)
        expect(recovery(page).locator("div.plan > p > code").first).to_have_text(plan["plan_hash"])
        note = (f"Refreshed {len(refreshed['refreshed'])} payments at replay time {replay_time(refreshed['now_ms'])}; "
                f"unknown at provider {len(refreshed['unknown_at_provider'])}.")
        expect(recovery(page).get_by_text(note)).to_be_visible()
        record.check(total.startswith("103 target orders, value INR 125,027.00") and plan["plan_hash"].startswith("9db6c8c60bee4995"),
                     f"A re-planned: {total!r}, hash {plan['plan_hash'][:16]} (docs: 103, INR 125,027.00, 9db6c8c60bee4995)")
        check_stage(record, page, recording, run, 5, "A re-planned")

    with suite.step("Flow A: approve", page) as record:
        recovery(page).get_by_role("button", name=re.compile(r"^Approve plan ")).click()
        approval = recording.change(run, "approve the plan")
        line = (f"Approved by {approval['operator_id']}, bound to hash {approval['plan_hash']}; the approval expires at "
                f"replay time {replay_time(approval['expires_at_ms'])}.")
        expect(recovery(page).locator("p.approved")).to_have_text(line)
        record.check(True, f"A approved: {line!r}")
        check_stage(record, page, recording, run, 6, "A approved")

    with suite.step("Flow A: execute", page) as record:
        recovery(page).get_by_role("button", name="Execute", exact=True).click()
        executed = recording.change(run, "execute the approved plan")
        expect(recovery(page).locator("ul.executions > li")).to_have_count(1)
        receipt = run["receipts"][0]
        expect(page.locator("main .run-header .rail > ol.rail-steps > li").nth(5)).to_contain_text(hash_proof(receipt["receipt_hash"]))
        figures = page.evaluate(MONEY_JS)
        wanted = {RESTORED: {"value": rupees(executed["restored_processing_value_paise"]),
                             "detail": f"action {executed['action_id']}, {executed['status']}"},
                  NEW_CAPTURE: {"value": rupees(executed["additional_captured_paise"]),
                                "detail": f"action {executed['action_id']}, {executed['status']}"}}
        record.check({name: figures.get(name) for name in wanted} == wanted,
                     f"A executed: Money at stake shows {wanted}, the recorded Execute answer")
        record.check(wanted[RESTORED]["value"] == "INR 125,027.00" and wanted[NEW_CAPTURE]["value"] == "INR 0.00",
                     "A executed: the documented restored INR 125,027.00 and new capture INR 0.00")
        rail = page.evaluate(RAIL_JS)
        record.check([step["state"] for step in rail] == ["Done"] * 6 and rail[5]["proof"] == [hash_proof(receipt["receipt_hash"])]
                     and receipt["receipt_hash"].startswith("eb9d6f30f04d"),
                     f"A executed: all six steps Done; the rail's receipt hash {rail[5]['proof']} is the recorded "
                     f"receipt's (docs: eb9d6f30f04d...)")
        record.check(rail[5]["link"] == f"receipts/{run_id}/{receipt['action_id']}.json",
                     f"A executed: the rail's receipt link is the static file {rail[5]['link']}")
        check_stage(record, page, recording, run, 7, "A executed")
        page.evaluate("() => window.scrollTo(0, 0)")
        suite.shot(record, page, "demo_04_flow_a_executed_1440.png", full_page=True)

    with suite.step("Flow A: execute again (the replay), and a third time is refused", page) as record:
        recovery(page).get_by_role("button", name="Execute again (shows the replay)").click()
        expect(recovery(page).locator("ul.executions > li")).to_have_count(2)
        again = recording.change(run, "execute the plan again (the replay)")
        second = recovery(page).locator("ul.executions > li").nth(1).inner_text()
        record.check(again["replayed"] and f"Action {again['action_id']}: succeeded (replayed: the action was already "
                     "recorded, no new effects)" in second, f"A executed again: {second!r}")
        check_stage(record, page, recording, run, 8, "A executed again")
        recovery(page).get_by_role("button", name="Execute again (shows the replay)").click()
        check_refusal(record, page, "A third execution", f"executing plan {again['plan_id']}", "ends at this point")
        expect(recovery(page).locator("ul.executions > li")).to_have_count(2)
        page.locator("section.demo-banner").get_by_role("button", name="Dismiss").click()
        record.check(page.locator("section.demo-banner p.demo-notice").count() == 0, "Dismiss clears the banner's message")

    with suite.step("Flow A: a recovered order's case, and its receipts from static files", page) as record:
        complete = recording.answer(run, 8, queue_key(run_id, {"state": ["complete"]}))
        show(page).select_option("completed")
        wait_rows(page, [row["order_id"] for row in complete["orders"]])
        label = show(page).locator("option:checked").inner_text()
        record.check(label == f"Complete ({complete['matching_orders']})" == "Complete (148)", f"A after: Show reads {label!r}")
        sort_by(page, "Amount", "descending")
        largest = sorted(complete["orders"], key=lambda row: (-(row["amount_paise"] or 0), row["order_id"]))[0]
        first = queue(page).evaluate(ROW_IDS_JS)[0]
        record.check(first == largest["order_id"] == "order_SYNTHETIC_547a954d_0099",
                     f"A after: by Amount the first row is {first} (recorded {largest['order_id']}; docs ..._0099)")
        detail = open_case(page, first)
        case = recording.answer(run, 8, f"GET /api/runs/{run_id}/orders/{first}?as_of_ms={complete['as_of_ms']}")
        expect(detail).to_contain_text(f"As of replay time {replay_time(complete['as_of_ms'])}.")
        receipts = detail.locator("ul.compact").first
        action = case["receipts"][0]
        expect(receipts).to_contain_text(f"Action {action['action_id']} of plan {action['plan_id']}: {action['status']}")
        record.check(True, f"A after: the case of {first} lists the recorded receipt of {action['action_id']}")
        suite.shot(record, page, "demo_05_flow_a_case_and_receipt_links_1440.png")
        markdown_link = detail.get_by_role("link", name="Receipt (Markdown)")
        json_link = detail.get_by_role("link", name="Receipt (JSON)")
        receipt = run["receipts"][0]
        for link, kind, path_key in ((markdown_link, "Markdown", "markdown"), (json_link, "JSON", "json")):
            url, status, content_type, text = receipt_tab(context, observed, link, f"receipt {kind}",
                                                          suite.out / f"demo_05_receipt_{path_key}_tab_1440.png")
            record.screenshots.append(f"demo_05_receipt_{path_key}_tab_1440.png")
            file_text = (recording.site / receipt[path_key]).read_text(encoding="utf-8")
            record.check(url == f"{ORIGIN}/{receipt[path_key]}" and status == 200,
                         f"the case's Receipt ({kind}) opens the static file {urlsplit(url).path} with status {status}")
            record.check(content_type.split(";")[0] == receipt[f"{path_key}_type"].split(";")[0],
                         f"Receipt ({kind}) is served as {content_type!r}, as the API served it ({receipt[f'{path_key}_type']!r})")
            if kind == "Markdown":
                record.check(text.startswith("# Recovery receipt `receipt-2`") and receipt["receipt_hash"] in text
                             and text.strip() == file_text.strip(),
                             "the Markdown receipt is the recorded file, with its title and the recorded hash")
            else:
                shown_hash = json.loads(text)["receipt_hash"] if text.lstrip().startswith("{") else None
                record.check(shown_hash == receipt["receipt_hash"] and json.loads(file_text)["receipt_hash"] == shown_hash,
                             f"the JSON receipt is the recorded file, with receipt_hash {str(shown_hash)[:16]}...")
        rail_link = page.locator("main .run-header .rail > ol.rail-steps > li").nth(5).locator("a")
        url, status, _, text = receipt_tab(context, observed, rail_link, "receipt from the rail")
        record.check(url == f"{ORIGIN}/{receipt['json']}" and status == 200 and receipt["receipt_hash"] in text,
                     f"the rail's Open receipt (JSON) opens the same static file ({urlsplit(url).path}, {status})")
        close_case(page)
        sort_by(page, "Order", "ascending")
        show(page).select_option("all")


def step_flow_b(suite: Suite, page: Page, recording: Recording) -> None:
    run = recording.runs["B"]
    with suite.step(f"Flow B ({run['run_id']}): opened", page) as record:
        open_run(page, run)
        check_stage(record, page, recording, run, 0, "B opened")
        options = [label for _, label in show(page).evaluate(SHOW_OPTIONS_JS)]
        documented = ["All orders (1200)", "Complete (1082)", "State: Payment failed (118)"]
        record.check(all(item in options for item in documented), f"B opened: the Show list has {documented}")
        figures = page.evaluate(MONEY_JS)
        record.check(figures[CAPTURED] == {"value": "INR 0.00", "detail": "0 orders: no captured payment is waiting on our processing"} and
                     figures[AWAITING] == {"value": "INR 0.00", "detail": "0 orders: nothing is waiting"},
                     f"B opened: nothing waits to be reprocessed (docs): {figures[CAPTURED]}, {figures[AWAITING]}")
        page.evaluate("() => window.scrollTo(0, 0)")
        suite.shot(record, page, "demo_06_flow_b_opened_1440.png", full_page=True)
    with suite.step("Flow B: diagnose", page) as record:
        fields = diagnose(page)
        check_diagnosis(record, fields, recording.job(run, 1), "B diagnosed")
        record.check((fields.get("Leading hypothesis"), fields.get("Affected method")) == ("provider_method_degradation", "upi"),
                     "B diagnosed: the documented provider_method_degradation for upi")
        check_stage(record, page, recording, run, 1, "B diagnosed")
    with suite.step("Flow B: escalate", page) as record:
        recovery(page).get_by_role("button", name="Escalate with the evidence needed (no reprocessing)").click()
        escalation = recording.change(run, "escalate with the evidence needed")
        line = (f"Escalated as action {escalation['action_id']} by {escalation['operator_id']} for "
                f"{', '.join(escalation['subject_ids'])}; evidence needed: {', '.join(escalation['evidence_needed']) or 'none named'}.")
        expect(recovery(page).locator("p.outcome-complete")).to_have_text(line)
        record.check(line.endswith("for upi; evidence needed: compare_method_cohorts."), f"B escalated: {line!r} (docs)")
        record.check(rail_states(page) == ["Done"] + ["Not applicable"] * 5, f"B escalated: the rail reads {rail_states(page)}")
        check_stage(record, page, recording, run, 2, "B escalated")
        page.evaluate("() => window.scrollTo(0, 0)")
        suite.shot(record, page, "demo_07_flow_b_escalated_1440.png", full_page=True)


def step_flow_c(suite: Suite, page: Page, recording: Recording) -> None:
    run = recording.runs["C"]
    with suite.step(f"Flow C ({run['run_id']}): opened and diagnosed", page) as record:
        open_run(page, run)
        check_stage(record, page, recording, run, 0, "C opened")
        fields = diagnose(page)
        check_diagnosis(record, fields, recording.job(run, 1), "C diagnosed")
        record.check(fields.get("Leading hypothesis") == "ambiguous_payment_state", "C diagnosed: the documented hypothesis")
        check_stage(record, page, recording, run, 1, "C diagnosed")
    with suite.step("Flow C: escalating before the refresh is refused; refresh, then escalate", page) as record:
        recovery(page).get_by_role("button", name="Escalate with the evidence needed (no reprocessing)").click()
        check_refusal(record, page, "C escalate first", f"escalating on {run['run_id']}",
                      "next step for " + run["run_id"] + " is to refresh provider state (read-only)")
        refreshed = recording.change(run, "refresh provider state (read-only)")
        recovery(page).get_by_role("button", name=re.compile(r"^Refresh provider state for \d+ open")).click()
        statuses: dict[str, int] = {}
        for status in refreshed["statuses"].values():
            statuses[status] = statuses.get(status, 0) + 1
        line = (f"Refreshed {len(refreshed['refreshed'])} payments at {replay_time(refreshed['now_ms'])}; statuses now "
                f"{json.dumps(statuses, separators=(',', ':'))}")
        expect(recovery(page).get_by_text(line)).to_be_visible()
        record.check(True, f"C refreshed: {line!r}, the recorded answer")
        check_stage(record, page, recording, run, 2, "C refreshed")
        recovery(page).get_by_role("button", name="Escalate with the evidence needed (no reprocessing)").click()
        escalation = recording.change(run, "escalate with the evidence needed")
        expect(recovery(page).locator("p.outcome-complete")).to_contain_text(f"Escalated as action {escalation['action_id']}")
        check_stage(record, page, recording, run, 3, "C escalated")
        page.evaluate("() => window.scrollTo(0, 0)")
        suite.shot(record, page, "demo_08_flow_c_escalated_1440.png", full_page=True)


def step_back_to_a(suite: Suite, page: Page, recording: Recording) -> None:
    run = recording.runs["A"]
    with suite.step("Back to Flow A after B and C: its recovered state, kept", page) as record:
        open_run(page, run)
        whole = check_stage(record, page, recording, run, 8, "A reopened")
        record.check(whole["totals"]["unresolved_value_paise"] == 1_235_800,
                     "A reopened: the documented INR 12,358.00 still awaiting, 11 orders")
        expect(recovery(page)).to_contain_text("Run a diagnosis first.")
        diagnosis(page).get_by_role("button", name="Diagnose the latest incident").click()
        check_refusal(record, page, "A second diagnosis", f"a second diagnosis of {run['run_id']}", "Restart the demo")
        diagnosis(page).get_by_label("Mode").select_option("local")
        diagnosis(page).get_by_role("button", name="Diagnose the latest incident").click()
        check_refusal(record, page, "A Local model", "a diagnosis in Local model mode", "nothing here calls a model")
        diagnosis(page).get_by_label("Mode").select_option("rules")


def step_unrecorded_controls(suite: Suite, page: Page, recording: Recording, observed: Observed) -> None:
    run = recording.runs["A"]
    controls = page.locator("section.panel.controls")
    with suite.step("Controls the recording cannot answer say so; nothing is invented", page) as record:
        controls.get_by_label("Scenario").select_option("method_degradation")
        controls.get_by_role("textbox", name="Seed", exact=True).fill("102")
        controls.get_by_role("button", name="Create synthetic run").click()
        check_refusal(record, page, "create another seed", "creating a run (method_degradation, seed 102)")
        other = recording.runs["B"]
        controls.get_by_label("Scenario").select_option(other["scenario"])
        controls.get_by_role("textbox", name="Seed", exact=True).fill(str(other["seed"]))
        controls.get_by_role("button", name="Create synthetic run").click()
        expect(topbar(page).locator(".topbar-facts")).to_contain_text(f"Run: {other['run_id']}")
        record.check(not errors_shown(page),
                     f"creating a recorded world opens its recorded run {other['run_id']}, with no error")
        open_run(page, run)
        controls.get_by_role("button", name="+1 min", exact=True).click()
        check_refusal(record, page, "advance the clock", f"advancing the replay clock of {run['run_id']} by 1 min")
        expect(topbar(page).locator(".topbar-facts")).to_contain_text(f"Replay clock: {replay_time(recording.clock(run, 8))}")
        whole = recording.answer(run, 8, queue_key(run["run_id"], {}))
        unrecorded = next(row["order_id"] for row in whole["orders"] if row["order_id"] not in run["case_orders"])
        detail = open_case(page, unrecorded)
        check_refusal(record, page, "an unrecorded case", f"the case of {unrecorded} at replay time "
                      f"{replay_time(whole['as_of_ms'])}", "the orders each Show filter lists first")
        expect(detail).to_contain_text("The case could not be loaded")
        close_case(page)
        with page.expect_download() as download:
            page.locator("section.panel.report").get_by_role("button", name="Export report (JSON)").click()
        observed.downloads.append(download.value.suggested_filename)
        record.check(download.value.suggested_filename == f"report-{run['run_id']}.json" and not errors_shown(page),
                     f"Export report replays the recorded report: {download.value.suggested_filename}")
        suite.shot(record, page, "demo_09_refusal_unrecorded_case_1440.png")


def step_logout(suite: Suite, page: Page, recording: Recording) -> None:
    with suite.step("Log out asks for no token; Continue replays the recorded login", page) as record:
        topbar(page).get_by_role("button", name="Log out").click()
        expect(page.get_by_role("heading", name="Operator login")).to_be_visible()
        check_banner(record, page, "login view")
        record.check(page.locator("input").count() == 0 and page.get_by_text("No token is needed").count() == 1,
                     "the login view says no token is needed and has no input field at all")
        record.check(page.locator("section.demo-banner p.demo-notice").count() == 0 and not errors_shown(page),
                     "no earlier refusal lingers: the page's error banner and the demo banner's copy are both clear")
        suite.shot(record, page, "demo_10_login_no_token_1440.png")
        page.get_by_role("button", name=f"Continue as {OPERATOR}").click()
        expect(topbar(page).locator(".operator")).to_contain_text(f"Operator {OPERATOR}")
        listed = page.get_by_label("Open run").locator("option").all_inner_texts()[1:]
        record.check(len(listed) == len(recording.manifest["runs"]), f"logged in again, the runs are listed: {listed}")
        check_no_error(record, page, "after logging in again")


def step_restart(suite: Suite, page: Page, recording: Recording, observed: Observed) -> None:
    run = recording.runs["A"]
    with suite.step("Restart the demo starts every flow from the beginning", page) as record:
        observed.read_census(page, "page 1440, the three flows")  # the reload starts a new document and census
        with page.expect_navigation():
            page.locator("section.demo-banner").get_by_role("button", name="Restart the demo").click()
        open_run(page, run)
        check_banner(record, page, "after restart")
        expect(topbar(page).locator(".topbar-facts")).to_contain_text(f"Replay clock: {replay_time(recording.clock(run, 0))}")
        figures = page.evaluate(MONEY_JS)
        record.check(figures[CAPTURED]["value"] == "INR 119,592.00", f"after restart A is back at its start: {figures[CAPTURED]}")
        expect(recovery(page)).to_contain_text("Run a diagnosis first.")
        record.check(True, "after restart the Recovery card asks for a diagnosis again")


def step_scrolled(suite: Suite, page: Page) -> None:
    with suite.step("The banner stays at the top of the view when the page scrolls (1440 px)", page) as record:
        page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(100)
        check_banner(record, page, "scrolled to the bottom at 1440 px")
        suite.shot(record, page, "demo_11_scrolled_1440.png")
        page.evaluate("() => window.scrollTo(0, 0)")


def step_phone(suite: Suite, page: Page, recording: Recording) -> None:
    with suite.step("At 390 px: the banner on every view, no sideways scroll, the flows' key views", page) as record:
        page.goto(f"{ORIGIN}/")
        expect(topbar(page).locator(".operator")).to_contain_text(f"Operator {OPERATOR}")

        def fits(where: str) -> None:
            width = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
            record.check(width[0] <= width[1], f"{where}: the page does not scroll sideways ({width[0]} <= {width[1]})")
            check_banner(record, page, where)

        fits("390 start")
        suite.shot(record, page, "demo_12_start_390.png")
        open_run(page, recording.runs["A"])
        fits("390 A opened")
        suite.shot(record, page, "demo_13_flow_a_opened_390.png")
        diagnose(page)
        recovery(page).scroll_into_view_if_needed()
        fits("390 A diagnosed, scrolled to Recovery")
        suite.shot(record, page, "demo_14_flow_a_recovery_390.png")
        recovery(page).get_by_role("button", name="Validate this corrected handler").click()
        check_refusal(record, page, "390 refused validation", "validating the corrected handler")
        fits("390 refusal")
        suite.shot(record, page, "demo_15_refusal_390.png")
        open_run(page, recording.runs["B"])
        diagnose(page)
        recovery(page).get_by_role("button", name="Escalate with the evidence needed (no reprocessing)").click()
        expect(recovery(page).locator("p.outcome-complete")).to_contain_text("Escalated as action")
        fits("390 B escalated")
        suite.shot(record, page, "demo_16_flow_b_escalated_390.png")
        open_run(page, recording.runs["C"])
        fits("390 C opened")
        suite.shot(record, page, "demo_17_flow_c_opened_390.png")


def step_hygiene(suite: Suite, page: Page, observed: Observed, server_log: Path) -> dict:
    with suite.step("No request left the origin; no CSP violation, page error or failed request", page) as record:
        foreign = [url for url in observed.requests
                   if not (url.startswith(f"{ORIGIN}/") or url.startswith(("data:", "blob:")))]
        record.check(not foreign, f"{len(observed.requests)} requests, {len(foreign)} outside {ORIGIN}: {foreign[:5]}")
        unread = [label for label, census in observed.census.items() if census is None]
        record.check(not unread, f"the census script ran in every page and tab read ({len(observed.census)}): unread {unread}")
        violations = {label: len((census or {}).get("csp", [])) for label, census in observed.census.items()}
        record.check(len(violations) >= 6 and not any(violations.values()),
                     f"Content-Security-Policy violations per page and tab: {violations}")
        unknown = {label: (census or {}).get("unknown", []) for label, census in observed.census.items()}
        record.check(not any(unknown.values()), f"no element the HTML parser did not recognise: {unknown}")
        csp_console = [item for item in observed.console if "Content Security Policy" in item["text"]]
        record.check(not csp_console, f"no console message about the Content Security Policy: {csp_console[:3]}")
        record.check(not observed.page_errors, f"no page error: {observed.page_errors[:3]}")
        record.check(not observed.failed_requests, f"no failed request: {observed.failed_requests[:3]}")
        record.check(not observed.http_errors, f"no HTTP error status: {observed.http_errors[:3]}")
        served = [line.split(" ")[1] for line in server_log.read_text(encoding="utf-8").splitlines()
                  if line.startswith(("GET ", "HEAD "))]
        api_paths = [path for path in served if path.startswith(("/api/", "/health"))]
        record.check(not api_paths, f"the static server was never asked for an API path ({len(served)} requests served)")
        return {"requests": len(observed.requests), "foreign": foreign, "csp_violations": violations,
                "served": dict(Counter(path.split("/")[1] or "/" for path in served))}


# --------------------------------------------------------------------------- main


def headers_file(site: Path) -> dict[str, str]:
    wanted: dict[str, str] = {}
    for line in (site / "_headers").read_text(encoding="utf-8").splitlines():
        if line[:1].isspace() and ":" in line:
            name, _, value = line.strip().partition(":")
            wanted[name.strip()] = value.strip()
    return wanted


def new_context(browser: Browser, observed: Observed, label: str, viewport: dict) -> tuple[BrowserContext, Page]:
    context = browser.new_context(viewport=viewport, accept_downloads=True)
    context.set_default_timeout(EXPECT_TIMEOUT_MS)
    context.add_init_script(path=str(CENSUS_SCRIPT))
    observed.attach(context, label)
    return context, context.new_page()


def main() -> int:
    parser = argparse.ArgumentParser(description="Test the built recorded demo on a plain static server.")
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    started = datetime.now(tz=UTC).isoformat()
    recording = Recording(args.site)
    server_log = Path("/tmp/static_server.log")
    server = subprocess.Popen([sys.executable, str(Path(__file__).with_name("static_server.py")), "--root",
                               str(args.site), "--port", str(PORT), "--log", str(server_log)])
    suite = Suite(args.out)
    observed = Observed()
    hygiene: dict = {"requests": len(observed.requests), "foreign": ["not checked"], "csp_violations": {}, "served": {}}
    version = "not launched"
    try:
        for _ in range(100):
            if server_log.exists() or server.poll() is not None:
                break
            time.sleep(0.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context, page = new_context(browser, observed, "1440", DESKTOP)
            for _ in range(50):
                try:
                    page.goto(f"{ORIGIN}/demo-data/manifest.json")
                    break
                except Exception:  # noqa: BLE001 - the server is still starting
                    time.sleep(0.1)
            step_start(suite, page, recording, headers_file(args.site))
            step_flow_a(suite, page, context, recording, observed)
            step_flow_b(suite, page, recording)
            step_flow_c(suite, page, recording)
            step_back_to_a(suite, page, recording)
            step_unrecorded_controls(suite, page, recording, observed)
            step_logout(suite, page, recording)
            step_restart(suite, page, recording, observed)
            step_scrolled(suite, page)
            observed.read_census(page, "page 1440, after the restart")
            context.close()
            phone_context, phone = new_context(browser, observed, "390", PHONE)
            step_phone(suite, phone, recording)
            observed.read_census(phone, "page 390")
            hygiene = step_hygiene(suite, phone, observed, server_log) or hygiene
            phone_context.close()
            version = browser.version
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    checks = [item for step in suite.steps for item in step.checks if not item.startswith("NOTE")]
    failed = [item for item in checks if item.startswith("FAIL")]
    results = {
        "started": started, "finished": datetime.now(tz=UTC).isoformat(), "browser": f"chromium {version}",
        "site": {"runs": [run["run_id"] for run in recording.manifest["runs"]]},
        "summary": {"steps": len(suite.steps), "steps_passed": sum(step.status == "pass" for step in suite.steps),
                    "checks": len(checks), "failed": len(failed)},
        "hygiene": hygiene, "downloads": observed.downloads,
        "console": observed.console, "page_errors": observed.page_errors,
        "failed_requests": observed.failed_requests, "http_errors": observed.http_errors, "census": observed.census,
        "steps": [asdict(step) for step in suite.steps],
    }
    (args.out / "demo_results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print("\n==== summary ====")
    for step in suite.steps:
        print(f"{step.status:4} {len(step.checks):3} checks, {len(step.failed)} failed: {step.name}"
              f"{'; ' + step.error.splitlines()[0][:200] if step.error else ''}")
    for item in failed:
        print(f"  {item[:400]}")
    print(f"steps {results['summary']['steps_passed']}/{len(suite.steps)} passed; checks {len(checks)}, failed {len(failed)}; "
          f"requests {hygiene['requests']}, outside the origin {len(hygiene['foreign'])}; "
          f"CSP violations {sum(hygiene['csp_violations'].values())}")
    return 0 if not failed and all(step.status == "pass" for step in suite.steps) else 1


if __name__ == "__main__":
    sys.exit(main())
