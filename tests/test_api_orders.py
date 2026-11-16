"""The order queue routes (phase 11): GET /api/runs/{run_id}/orders and /orders/{order_id}, through TestClient.

Rules mode on the README's demo world (worker_backlog, seed 101). The routes only read and call no model.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from test_api import create_run, diagnose, get, log_in, make_client, post

from recovery_workbench import api, diagnosis, order_cases, storage
from recovery_workbench.contracts import CaseState

CONDITIONS = {rule.state.value: rule.condition for rule in order_cases.STATE_PRECEDENCE}
TIMELINE_ORDER = list(api.TIMELINE_STAGES)


@dataclass(frozen=True)
class Seeded:
    home: Path  # make_client's directory; the data directory is inside it
    client: TestClient
    run_id: str

    @property
    def database(self) -> Path:
        return Path(self.client.app.state.workbench.database)

    @property
    def queue_path(self) -> str:
        return f"/api/runs/{self.run_id}/orders"

    def clock(self) -> int:
        return int(get(self.client, f"/api/runs/{self.run_id}")["replay_clock_ms"])

    def classified(self, as_of_ms: int) -> order_cases.OrderQueue:
        stale_after_ms = self.client.app.state.workbench.settings.stale_after_seconds * 1000
        with closing(storage.connect(self.database)) as conn:
            return order_cases.classify_orders(conn, run_id=self.run_id, as_of_ms=as_of_ms, stale_after_ms=stale_after_ms)

    def orders(self) -> tuple[storage.OrderRecord, ...]:
        with closing(storage.connect(self.database)) as conn:
            return storage.list_orders(conn, run_id=self.run_id)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seeded]:
    """A run nothing changes after seeding, shared by the read-only tests."""
    home = tmp_path_factory.mktemp("queue")
    with make_client(home) as client:
        log_in(client)
        yield Seeded(home, client, create_run(client))


@dataclass(frozen=True)
class Recovered:
    seeded: Seeded
    earlier_ms: int  # a replay time before every operator step
    queue_then: dict  # the queue at earlier_ms, fetched before any operator step
    plan: dict
    executed: dict


@pytest.fixture(scope="module")
def recovered(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Recovered]:
    """Flow A through the API, with the queue read before the first operator step."""
    home = tmp_path_factory.mktemp("recovered")
    with make_client(home) as client:
        log_in(client)
        run = Seeded(home, client, create_run(client))
        earlier_ms = run.clock() - 1  # before the clock, so the undated refund flag is read in neither request
        queue_then = get(client, f"{run.queue_path}?as_of_ms={earlier_ms}")
        found = diagnose(client, run.run_id)["diagnosis"]
        validation = post(client, f"/api/runs/{run.run_id}/fixes/payload@5/validate",
                          {"diagnosis_id": found["diagnosis_id"]})
        plan = post(client, f"/api/runs/{run.run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        post(client, f"/api/runs/{run.run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
        executed = post(client, f"/api/runs/{run.run_id}/plans/{plan['plan_id']}/execute")
        assert executed["status"] == "succeeded"
        yield Recovered(run, earlier_ms, queue_then, plan, executed)


# --------------------------------------------------------------------------- the session and the request guard


def test_both_routes_need_an_operator_session(seeded: Seeded) -> None:
    order_id = seeded.orders()[0].order_id
    routes = (seeded.queue_path, f"{seeded.queue_path}/{order_id}")
    with make_client(seeded.home) as anonymous:  # the same data directory, and no session
        for route in routes:
            refused = get(anonymous, route, expect=403)
            assert refused["error"] == "ScopeError"
            assert "session" in refused["detail"]
        anonymous.cookies.set(api.SESSION_COOKIE, "forged-session-id")
        for route in routes:
            get(anonymous, route, expect=403)
    for route in routes:  # the request guard is unchanged: reads must name this server's host too
        assert seeded.client.get(route, headers={"host": "attacker.example"}).status_code == 403
        get(seeded.client, route)  # a GET needs no X-Workbench-Request header


# --------------------------------------------------------------------------- the queue


def test_the_queue_at_the_clock_is_the_classification_with_whole_queue_totals(seeded: Seeded) -> None:
    clock = seeded.clock()
    document = get(seeded.client, seeded.queue_path)
    queue = seeded.classified(clock)
    created = {order.order_id: order.created_at_ms for order in seeded.orders()}
    assert (document["as_of_ms"], document["replay_clock_ms"], document["refund_flag_read"]) == (clock, clock, True)
    assert document["totals"] == queue.totals.model_dump(mode="json")
    assert document["coverage"] == queue.coverage.model_dump(mode="json")
    assert (document["matching_orders"], document["next_after"]) == (len(queue.cases), None)
    assert document["orders"] == [
        {
            "order_id": case.order_id, "order_known": case.order_known, "amount_paise": case.amount_paise,
            "currency": case.currency, "amount_status": case.amount_status.value, "state": case.state.value,
            "next_step": case.next_step.value, "blockers": list(case.blockers),
            "age_ms": clock - created[case.order_id] if case.order_known else None,
            "freshness_ms": case.freshness.age_ms, "newest_observation_at_ms": case.freshness.newest_observation_at_ms,
        }
        for case in queue.cases
    ]
    assert len({row["state"] for row in document["orders"]}) >= 5  # a queue with something to filter
    assert [item["state"] for item in document["states"]] == [state.value for state in CaseState]
    assert {item["state"]: item["next_step"] for item in document["states"]} == {
        state.value: step.value for state, step in order_cases.NEXT_STEP.items()}
    assert {item["state"] for item in document["states"] if item["unresolved"]} == {
        state.value for state in order_cases.UNRESOLVED_STATES}
    assert {"age_ms", "freshness_ms", "totals"} <= set(document["definitions"])


def test_filters_choose_the_rows_and_never_change_the_totals(seeded: Seeded) -> None:
    whole = get(seeded.client, seeded.queue_path)
    by_state = {item["state"]: item["orders"] for item in whole["totals"]["by_state"]}
    for state in (state for state, orders in by_state.items() if orders):
        page = get(seeded.client, f"{seeded.queue_path}?state={state}")
        assert {row["state"] for row in page["orders"]} == {state}
        assert len(page["orders"]) == page["matching_orders"] == by_state[state]
        assert (page["totals"], page["coverage"]) == (whole["totals"], whole["coverage"])
        assert page["filters"] == {"state": [state], "next_step": [], "blocker": None}
    two = get(seeded.client, f"{seeded.queue_path}?state=complete&state=captured_processing_incomplete")
    assert len(two["orders"]) == by_state["complete"] + by_state["captured_processing_incomplete"]
    review_states = {item["state"] for item in whole["states"] if item["next_step"] == "manual_review"}
    manual = get(seeded.client, f"{seeded.queue_path}?next_step=manual_review")
    assert {row["state"] for row in manual["orders"]} <= review_states
    assert len(manual["orders"]) == sum(by_state[state] for state in review_states) > 0
    stale = get(seeded.client, f"{seeded.queue_path}?blocker=stale_snapshot")
    assert len(stale["orders"]) == sum("stale_snapshot" in row["blockers"] for row in whole["orders"]) > 0
    assert all("stale_snapshot" in row["blockers"] for row in stale["orders"])
    assert get(seeded.client, f"{seeded.queue_path}?state=complete&next_step=manual_review")["orders"] == []


@pytest.mark.parametrize("query", [
    "state=paid", "next_step=reprocess", "limit=0", f"limit={api.MAX_QUEUE_PAGE_LIMIT + 1}", "after=a%20b",
    "as_of_ms=soon",
])
def test_malformed_filters_are_422(seeded: Seeded, query: str) -> None:
    assert seeded.client.get(f"{seeded.queue_path}?{query}").status_code == 422


def test_an_unknown_blocker_is_refused_by_name(seeded: Seeded) -> None:
    refused = get(seeded.client, f"{seeded.queue_path}?blocker=late_authorization", expect=422)
    assert refused["error"] == "ContractError"
    assert "stale_snapshot" in refused["detail"]  # it lists the blockers that exist


def test_pages_follow_the_order_id_cursor_and_cover_the_queue_once(seeded: Seeded) -> None:
    whole = get(seeded.client, seeded.queue_path)["orders"]
    collected: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        query = {"limit": 25} if after is None else {"limit": 25, "after": after}
        page = get(seeded.client, f"{seeded.queue_path}?{urlencode(query)}")
        collected.extend(page["orders"])
        pages += 1
        assert page["matching_orders"] == len(whole)
        after = page["next_after"]
        if after is None:
            break
        assert after == page["orders"][-1]["order_id"]
    assert collected == whole
    assert pages == -(-len(whole) // 25)


# --------------------------------------------------------------------------- as of a replay time


def test_as_of_must_be_a_replay_time_the_run_has_reached(seeded: Seeded) -> None:
    clock = seeded.clock()
    order_id = seeded.orders()[0].order_id
    for route in (seeded.queue_path, f"{seeded.queue_path}/{order_id}"):
        for as_of_ms in (clock + 1, -1):
            refused = get(seeded.client, f"{route}?as_of_ms={as_of_ms}", expect=422)
            assert refused["error"] == "ContractError"
            assert str(clock) in refused["detail"]


def test_records_known_after_t_never_change_the_queue_at_t(recovered: Recovered) -> None:
    run = recovered.seeded
    again = get(run.client, f"{run.queue_path}?as_of_ms={recovered.earlier_ms}")
    assert again["replay_clock_ms"] > recovered.queue_then["replay_clock_ms"]  # operator steps moved the clock
    assert {**again, "replay_clock_ms": None} == {**recovered.queue_then, "replay_clock_ms": None}
    assert again["refund_flag_read"] is False
    now = get(run.client, run.queue_path)
    then_states = {row["order_id"]: row["state"] for row in recovered.queue_then["orders"]}
    now_states = {row["order_id"]: row["state"] for row in now["orders"]}
    targets = {target["id"] for target in recovered.plan["targets"]}
    assert set(recovered.executed["fulfilled_order_ids"]) == targets
    assert {then_states[order] for order in targets} == {"captured_processing_incomplete"}
    assert {now_states[order] for order in targets} == {"complete"}


def test_an_order_not_in_the_queue_at_that_time_is_404(seeded: Seeded) -> None:
    missing = get(seeded.client, f"{seeded.queue_path}/order_SYNTHETIC_never_made", expect=404)
    assert missing["error"] == "OrderNotInQueueError"
    clock = seeded.clock()
    latest = max((order for order in seeded.orders() if order.created_at_ms <= clock),
                 key=lambda order: (order.created_at_ms, order.order_id))
    before = latest.created_at_ms - 1
    not_yet = get(seeded.client, f"{seeded.queue_path}/{latest.order_id}?as_of_ms={before}", expect=404)
    assert not_yet["error"] == "OrderNotInQueueError"
    assert latest.order_id not in {row["order_id"] for row in get(seeded.client, f"{seeded.queue_path}?as_of_ms={before}")["orders"]}
    created = get(seeded.client, f"{seeded.queue_path}/{latest.order_id}?as_of_ms={latest.created_at_ms}")
    assert created["summary"]["order_id"] == latest.order_id
    assert created["summary"]["age_ms"] == 0
    assert get(seeded.client, "/api/runs/run_does_not_exist/orders", expect=403)["error"] == "ScopeError"
    assert seeded.client.get(f"{seeded.queue_path}/order%20with%20a%20space").status_code == 422


# --------------------------------------------------------------------------- the case detail


def test_a_recovered_case_shows_its_timeline_next_step_and_receipt(recovered: Recovered) -> None:
    run, action_id = recovered.seeded, recovered.executed["action_id"]
    order_id = min(target["id"] for target in recovered.plan["targets"])
    detail = get(run.client, f"{run.queue_path}/{order_id}")
    case = detail["case"]
    assert (detail["summary"]["state"], detail["summary"]["next_step"], case["blockers"]) == (
        "complete", "none", ["already_fulfilled"])
    assert detail["state_condition"] == CONDITIONS["complete"]
    [classified] = [item for item in run.classified(detail["as_of_ms"]).cases if item.order_id == order_id]
    assert case == classified.model_dump(mode="json")

    timeline = detail["timeline"]
    assert timeline == sorted(timeline, key=lambda entry: (entry["at_ms"], TIMELINE_ORDER.index(entry["kind"])))
    assert all(entry["at_ms"] <= detail["as_of_ms"] for entry in timeline)
    assert all(entry["stage"] == api.TIMELINE_STAGES[entry["kind"]] for entry in timeline)
    kinds = [entry["kind"] for entry in timeline]
    story = ["order_created", "payment_attempt", "provider_observation", "delivery_attempt", "inbox_event",
             "worker_attempt", "fulfilment"]
    assert [kinds.index(kind) for kind in story] == sorted(kinds.index(kind) for kind in story)
    refs = case["evidence"]
    named = {kind: sorted(entry["record_id"] for entry in timeline if entry["kind"] == kind) for kind in set(kinds)}
    assert named["provider_observation"] == sorted(refs["observation_ids"])
    assert named["inbox_event"] == sorted(str(item) for item in refs["inbox_ids"])
    assert named["worker_attempt"] == sorted(str(item) for item in refs["worker_attempt_ids"])
    assert named["delivery_attempt"] == sorted(str(item) for item in refs["delivery_attempt_ids"])

    failures = [entry for entry in timeline if entry["kind"] == "worker_attempt" and entry["facts"]["result"] == "failed"]
    assert failures
    assert all("Traceback" in str(entry["text"]) for entry in failures)  # untrusted text, kept out of the facts
    assert not [entry for entry in timeline if "stack_trace" in entry["facts"] or "processing_status" in entry["facts"]]
    [fulfilled] = [entry for entry in timeline if entry["kind"] == "fulfilment"]
    assert (fulfilled["facts"]["via"], fulfilled["facts"]["action_id"]) == ("reprocess", action_id)

    [receipt] = detail["receipts"]
    assert (receipt["action_id"], receipt["status"], receipt["order_result"]) == (action_id, "succeeded", "completed")
    served = get(run.client, receipt["receipt_path"])
    assert served["identity"]["action_id"] == action_id
    assert order_id in {item["order_id"] for item in served["execution"]["orders"]}
    assert run.client.get(f"{receipt['receipt_path']}?format=markdown").status_code == 200


def test_an_order_the_plan_left_out_links_no_receipt(recovered: Recovered) -> None:
    run = recovered.seeded
    left_out = sorted(item["order_id"] for item in recovered.plan["excluded"])
    assert left_out
    for order_id in left_out:
        detail = get(run.client, f"{run.queue_path}/{order_id}")
        assert detail["receipts"] == []
        assert detail["summary"]["state"] != "complete"


def test_the_case_before_the_recovery_has_no_fulfilment_and_no_receipt(recovered: Recovered) -> None:
    run = recovered.seeded
    order_id = min(target["id"] for target in recovered.plan["targets"])
    then = get(run.client, f"{run.queue_path}/{order_id}?as_of_ms={recovered.earlier_ms}")
    assert (then["summary"]["state"], then["summary"]["next_step"]) == (
        "captured_processing_incomplete", "validate_fix_then_reprocess")
    assert then["state_condition"] == CONDITIONS["captured_processing_incomplete"]
    assert then["receipts"] == []
    assert "fulfilment" not in {entry["kind"] for entry in then["timeline"]}
    assert then["case"]["capture_processing"]["unsettled_inbox_ids"]


HEADER = {"run_id", "as_of_ms", "replay_clock_ms", "stale_after_ms", "classifier_version", "refund_flag_read"}


def test_the_documents_have_the_shape_the_ui_reads(recovered: Recovered) -> None:
    """frontend/src/types.ts mirrors these keys (OrderQueuePage, QueueRow, OrderCaseDetail, TimelineEntry, CaseReceipt)."""
    run = recovered.seeded
    page = get(run.client, run.queue_path)
    assert set(page) == HEADER | {"filters", "totals", "coverage", "states", "matching_orders", "orders", "next_after",
                                  "definitions"}
    assert set(page["orders"][0]) == {"order_id", "order_known", "amount_paise", "currency", "amount_status", "state",
                                      "next_step", "blockers", "age_ms", "freshness_ms", "newest_observation_at_ms"}
    assert set(page["states"][0]) == {"state", "next_step", "condition", "unresolved"}
    assert set(page["filters"]) == {"state", "next_step", "blocker"}
    order_id = min(target["id"] for target in recovered.plan["targets"])
    detail = get(run.client, f"{run.queue_path}/{order_id}")
    assert set(detail) == HEADER | {"summary", "case", "state_condition", "timeline", "receipts", "definitions"}
    assert set(detail["summary"]) == set(page["orders"][0])
    assert {key for entry in detail["timeline"] for key in entry} == {
        "at_ms", "stage", "kind", "record_id", "payment_id", "facts", "text"}
    assert set(detail["receipts"][0]) == {"action_id", "plan_id", "status", "executed_at_ms", "order_result",
                                          "order_reasons", "receipt_path"}
    assert {entry["kind"] for entry in detail["timeline"]} <= set(api.TIMELINE_STAGES)


def test_the_queue_needs_no_model(tmp_path: Path) -> None:
    def no_model() -> None:
        raise AssertionError("the order queue asked for a model client")

    def unreachable() -> str:
        raise diagnosis.ModelCallError("connection refused")

    with make_client(tmp_path, mode="local", factory=no_model, probe=unreachable) as client:  # type: ignore[arg-type]
        log_in(client)
        run_id = create_run(client)
        assert get(client, "/health")["model"]["available"] is False
        rows = get(client, f"/api/runs/{run_id}/orders")["orders"]
        assert rows
        assert get(client, f"/api/runs/{run_id}/orders/{rows[0]['order_id']}")["case"]["order_id"] == rows[0]["order_id"]
