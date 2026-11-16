"""Webhook conformance (phase 12): four delivery behaviours, each end to end on simulator-generated worlds.

1. A lost ACK after a durable commit: the provider times out on a delivery the receiver had committed and answered
   2xx, and its retry arrives as a duplicate. One inbox row, one business effect.
2. A cancellation or a refund after approval and before execution, applied with
   ``ReplayedWorld.apply_scripted_event``: the executor rejects the stale plan, and the receipt shows the failed
   checks and each order's result.
3. A late authorisation beside a paid retry: the projection, the reprocess plan's exclusions, the replay plan's
   holds and the order queue agree.
4. A provider secret rotation: retries keep the old signature, new events carry the new one, and a manual resend
   from the provider's dashboard (``ReplayedWorld.manual_replay``) is signed anew, refused for an acknowledged event
   and limited to a window. The rejected old-signature retries open an incident.

Every behaviour is opt-in (``scenarios.WebhookConformance``); without it a world is what it was before, which the
first test pins. Seeds 17101-17300 are reserved for this file (phase 12, agent P12a); it uses 17101-17107.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_order_cases import ensure_incident, planner_disagreements, replay_disagreements
from test_planted_instruction import world_digest
from test_plans import worker_regression_output

from recovery_workbench import (
    diagnosis_store,
    executor,
    fixes,
    ingestion,
    metrics,
    order_cases,
    packet,
    plans,
    policy,
    report,
    settings,
    storage,
    worker,
)
from recovery_workbench.contracts import (
    CaseState,
    ContractError,
    Hypothesis,
    NextStep,
    Operation,
    PlanStatus,
    ReceiveOutcome,
    WorkerResult,
    sha256_hex,
)
from recovery_workbench.simulator import driver, merchant, runtime, scenarios
from recovery_workbench.simulator.driver import ManualReplayRefusal, ProviderRefund, ReplayedWorld
from recovery_workbench.simulator.scenarios import (
    LateAuthorizationAfterRetry,
    LostDeliveries,
    ScenarioSpec,
    SecretRotation,
    WebhookConformance,
)
from recovery_workbench.simulator.world import (
    INTEGRATION_ID,
    ROTATED_SIGNING_REF,
    ROTATION_CONFIG_VARIANT,
    DeliveryPlan,
    OrderKind,
    OrderUpdate,
    World,
    generate_world,
)

MINUTE = 60_000
SETTINGS = settings.load_settings()
STALE_AFTER_MS = SETTINGS.stale_after_seconds * 1_000
PLAN_TTL_MS = SETTINGS.plan_ttl_seconds * 1_000
APPROVAL_TTL_MS = SETTINGS.approval_ttl_seconds * 1_000
OPERATOR = "op_conformance"
OLD_SECRET = merchant.PROVIDER_SIGNING_REF
TRUTH = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "manifests" / "merchant_variant_truth.json").read_text()
)
ALL_CHECKS = (*(code.value for code in policy.RECHECK_DESCRIPTIONS), executor.RERUN_CHECK)

RESERVED_SEEDS = range(17101, 17301)
LOST_ACK_SEED = 17101
CANCELLATION_SEED = 17102
REFUND_SEED = 17103
LATE_AFTER_RETRY_SEED = 17104
ROTATION_SEED = 17105

# A healthy shop: no faulting deploy, no planned duplicate copies, so every duplicate here comes from a lost ACK.
HEALTHY = ScenarioSpec(
    name="webhook_conformance", family="D5", variant_label="webhook_conformance", split="unit",
    expected_hypothesis=Hypothesis.NO_INCIDENT, expected_operation=Operation.NO_ACTION, duration_minutes=120,
    orders_per_hour=120, method_mix={"upi": 0.5, "card": 0.5}, authorization_rate={"upi": 0.95, "card": 0.95},
    duplicate_delivery_rate=0.0,
)
BUSY = HEALTHY.model_copy(update={"orders_per_hour": 360})  # more events in the minute before a rotation
LOST_ACKS = WebhookConformance(lost_deliveries=LostDeliveries(start_fraction=0.1, end_fraction=0.9, rate=0.3))
LATE_AFTER_RETRY = WebhookConformance(
    late_authorization_after_retry=LateAuthorizationAfterRetry(orders=6, unnotified=3)
)
# The provider and the merchant rotate together at half time. In the 3.6 minutes before, every first attempt is
# lost on the way, so the retries of the events first attempted in the last minute come after the rotation.
COORDINATED_ROTATION = WebhookConformance(
    secret_rotation=SecretRotation(at_fraction=0.5, merchant_deploy=True),
    lost_deliveries=LostDeliveries(start_fraction=0.47, end_fraction=0.5, rate=1.0, lost="request"),
)
STORY_KINDS = (OrderKind.LATE_AUTHORIZED_AFTER_RETRY, OrderKind.LATE_AUTHORIZED_AFTER_UNNOTIFIED_RETRY)

# (world digest, observable dataset sha256, oracle sha256) of two demo worlds, measured on HEAD 70b0736 before the
# overlay existed (the demo scenarios are the only specs with hand-placed special orders, which the overlay extends).
PRE_OVERLAY = {
    ("late_authorization", 17107): (
        "0a2eea6e53bf6cd56f803c411ee4862fc1cb2e3a4cdfdd3c2f6b031eccfa5af1",
        "33cbbd6f78f9e98dedc223d10edbf602581ea2b72dfb30073ab2649c6d2404f9",
        "62df193bb45485fc45920d20fd543a1fc19a11209b755947cf2dc9f92ceb3f0f",
    ),
    ("worker_backlog", 17106): (
        "605241d6900dbcfe9778ae4a3f4450746dac086433c0b0ff5d1af2920deec6bc",
        "393902b39fec2837e6c736657e7f592305d37020e0e067aea2c9841a4665678b",
        "d8b4920d66d90c4c4fa680b38311a77f71a743723fde7cdd2c690489e93d3663",
    ),
}


def test_this_file_uses_only_its_reserved_seeds() -> None:
    used = {LOST_ACK_SEED, CANCELLATION_SEED, REFUND_SEED, LATE_AFTER_RETRY_SEED, ROTATION_SEED,
            *(seed for _, seed in PRE_OVERLAY)}
    assert all(seed in RESERVED_SEEDS for seed in used)


# --------------------------------------------------------------------------- off by default


@pytest.mark.parametrize(("name", "seed"), sorted(PRE_OVERLAY))
def test_without_the_overlay_a_world_and_its_oracle_are_byte_identical_to_before_it(
    tmp_path: Path, name: str, seed: int
) -> None:
    spec = scenarios.DEMO_SCENARIOS[name]
    world = generate_world(spec, seed)
    explicitly_off = generate_world(spec, seed, conformance=None)
    assert world == explicitly_off
    assert world.conformance is None
    assert world.signing_rotation_at_ms is None
    assert all(plan.lost_first_attempt is None for plan in world.deliveries)
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        summary = driver.seed_world(conn, world, oracle_dir=tmp_path / "oracle", provider_dir=tmp_path / "provider")
    oracle = json.loads(Path(str(summary.oracle_path)).read_text(encoding="utf-8"))
    assert oracle.get("webhook_conformance") is None
    measured_now = (world_digest(world), summary.observable_sha256, summary.oracle_sha256)
    assert measured_now == PRE_OVERLAY[(name, seed)]


def test_the_overlay_names_at_least_one_valid_behaviour() -> None:
    with pytest.raises(ValidationError, match="at least one behaviour"):
        WebhookConformance()
    with pytest.raises(ValidationError, match="cannot exceed orders"):
        LateAuthorizationAfterRetry(orders=2, unnotified=3)
    with pytest.raises(ValidationError, match="start_fraction < end_fraction"):
        LostDeliveries(start_fraction=0.5, end_fraction=0.5, rate=1.0)
    rotation = WebhookConformance(secret_rotation=SecretRotation(at_fraction=0.5, merchant_deploy=True))
    with pytest.raises(ValueError, match="already deploys the config module"):  # one deployment per module per run
        generate_world(scenarios.WORKER_BACKLOG, ROTATION_SEED, conformance=rotation)
    # The baseline receiver verifies with the provider's first secret; the rotation's config deploy, with the next.
    module, number = merchant.parse_variant_id(ROTATION_CONFIG_VARIANT)
    assert merchant.compose().secret_ref == OLD_SECRET
    assert merchant.compose({module: number}).secret_ref == ROTATED_SIGNING_REF


# --------------------------------------------------------------------------- 1. a lost ACK after a durable commit


def unmarked(plan: DeliveryPlan) -> DeliveryPlan:
    return DeliveryPlan(plan.event_id, plan.payment_id, plan.order_id, plan.status, plan.send_at_ms, plan.raw_body,
                        plan.copy)


def business_rows(conn: sqlite3.Connection, run_id: str) -> dict[str, list[tuple[object, ...]]]:
    """What a delivery or the worker can change in business terms, in a form two databases can compare."""
    inbox = storage.list_inbox_rows(conn, run_id=run_id)
    events = {row.inbox_id: row.event_id for row in inbox}
    return {
        "inbox": sorted((row.event_id, row.event_type, row.body_sha256, row.received_at_ms, row.processing_status.value,
                         row.attempts, row.processed_at_ms) for row in inbox),
        "observations": sorted((item.source.value, item.source_ref, item.payment_id, item.status.value,
                                item.known_at_ms) for item in storage.list_observations(conn, run_id=run_id,
                                                                                         as_of_ms=None)),
        "worker_attempts": sorted((events[item.inbox_id], item.result.value, item.started_at_ms)
                                  for item in storage.list_worker_attempts(conn, run_id=run_id)),
        "fulfilments": sorted((item.order_id, item.payment_id, item.amount_paise, item.via, item.fulfilled_at_ms)
                              for item in storage.list_fulfilments(conn, run_id=run_id)),
        "orders": sorted((order.order_id, order.fulfilment_state) for order in storage.list_orders(conn, run_id=run_id)),
    }


def test_a_lost_ack_after_a_durable_commit_is_retried_as_a_duplicate_with_one_business_effect(tmp_path: Path) -> None:
    world = generate_world(HEALTHY, LOST_ACK_SEED, conformance=LOST_ACKS)
    twin = generate_world(HEALTHY, LOST_ACK_SEED)
    # The overlay changes how deliveries travel, nothing the provider or the shop did.
    assert [unmarked(plan) for plan in world.deliveries] == list(twin.deliveries)
    assert (world.orders, world.attempts, world.deployments) == (twin.orders, twin.attempts, twin.deployments)
    lost = {plan.event_id: plan for plan in world.deliveries if plan.lost_first_attempt == "ack"}
    assert len(lost) >= 20
    assert sum(plan.status.value == "captured" for plan in lost.values()) >= 5
    with (closing(storage.connect(tmp_path / "lost.sqlite")) as conn,
          closing(storage.connect(tmp_path / "twin.sqlite")) as twin_conn):
        replayed = driver.replay_world(conn, world, oracle_dir=None)
        driver.seed_world(twin_conn, twin, oracle_dir=None)
        run_id = world.run_id
        rows_by_event = defaultdict(list)
        for row in storage.list_delivery_attempts(conn, run_id=run_id):
            rows_by_event[row.event_id].append(row)
        inbox_by_event = Counter(row.event_id for row in storage.list_inbox_rows(conn, run_id=run_id))
        for event_id, plan in lost.items():
            # The provider saw no answer to its first attempt and retried one minute later, on its schedule.
            seen = [(attempt.at_ms, attempt.http_status) for attempt in replayed.provider_attempts(event_id)]
            assert seen == [(plan.send_at_ms, None), (plan.send_at_ms + driver.RETRY_BACKOFF_MS[0], 200)], event_id
            # The receiver committed and answered the first attempt; the retry is the same event ID and bytes.
            received = [(row.attempt_no, row.http_status, row.outcome, row.received_at_ms, row.body_sha256)
                        for row in rows_by_event[event_id]]
            assert received == [
                (1, 200, ReceiveOutcome.ACCEPTED, plan.send_at_ms, sha256_hex(plan.raw_body)),
                (2, 200, ReceiveOutcome.DUPLICATE, plan.send_at_ms + driver.RETRY_BACKOFF_MS[0], sha256_hex(plan.raw_body)),
            ], event_id
            assert inbox_by_event[event_id] == 1
        # One business effect: everything but the duplicate delivery rows is what the twin without lost ACKs has.
        assert business_rows(conn, run_id) == business_rows(twin_conn, run_id)
        outcomes = Counter(row.outcome for row in storage.list_delivery_attempts(conn, run_id=run_id))
        twin_outcomes = Counter(row.outcome for row in storage.list_delivery_attempts(twin_conn, run_id=run_id))
        assert dict(outcomes - twin_outcomes) == {ReceiveOutcome.DUPLICATE: len(lost)}
        assert twin_outcomes[ReceiveOutcome.DUPLICATE] == 0
        queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=world.cutoff_ms, stale_after_ms=STALE_AFTER_MS)
        twin_queue = order_cases.classify_orders(twin_conn, run_id=run_id, as_of_ms=world.cutoff_ms,
                                                 stale_after_ms=STALE_AFTER_MS)
        assert [(case.order_id, case.state, case.blockers) for case in queue.cases] == [
            (case.order_id, case.state, case.blockers) for case in twin_queue.cases]
        cases = {case.order_id: case for case in queue.cases}
        fulfilments = {item.order_id: item for item in storage.list_fulfilments(conn, run_id=run_id)}
        inbox = {row.event_id: row for row in storage.list_inbox_rows(conn, run_id=run_id)}
        for event_id, plan in lost.items():
            if plan.status.value != "captured":
                continue
            case = cases[plan.order_id]
            assert case.state is CaseState.COMPLETE, plan.order_id
            assert case.capture_processing.captured_inbox_ids == (inbox[event_id].inbox_id,)
            assert {row.id for row in rows_by_event[event_id]} <= set(case.evidence.delivery_attempt_ids)
            assert (fulfilments[plan.order_id].payment_id, fulfilments[plan.order_id].via) == (plan.payment_id, "worker")
            worked = storage.list_worker_attempts(conn, run_id=run_id, inbox_id=inbox[event_id].inbox_id)
            assert len(worked) == 1  # the duplicate never reached the worker
            assert worked[0].result is WorkerResult.FULFILLED


# --------------------------------------------------------------------------- 2. cancellation or refund after approval


@dataclass(frozen=True)
class Approved:
    replayed: ReplayedWorld
    deployed: merchant.DeployedMerchant
    preview: plans.PlanPreview

    @property
    def run_id(self) -> str:
        return self.replayed.world.run_id

    @property
    def cutoff_ms(self) -> int:
        return self.replayed.world.cutoff_ms


def validated_fix(
    conn: sqlite3.Connection, world: World, deployed: merchant.DeployedMerchant, now: int
) -> fixes.FixValidation:
    """A rules diagnosis of the run's incident and the allowlisted fix that repairs its faulting variant."""
    incident_id = ensure_incident(conn, world.run_id, now)
    diagnosis = diagnosis_store.record_rules_diagnosis(
        conn, run_id=world.run_id, incident_id=incident_id, packet_hash="0" * 64, output=worker_regression_output(),
        rules_version="conformance-test",
    )
    fix_id = TRUTH["variants"][world.spec.faulting_variant]["repaired_by"][0]
    validation = fixes.validate_fix(conn, run_id=world.run_id, deployed=deployed, fix_id=fix_id,
                                    diagnosis_id=diagnosis.diagnosis_id, operator_id=OPERATOR, now_ms=now)
    assert validation.passed
    return validation


def approved_reprocess_plan(conn: sqlite3.Connection, seed: int) -> Approved:
    """A worker-regression world replayed to its cutoff, its reprocess plan built there and approved a minute later."""
    world = generate_world(scenarios.WORKER_BACKLOG, seed)
    replayed = driver.replay_world(conn, world, oracle_dir=None, stale_after_ms=STALE_AFTER_MS)
    now = world.cutoff_ms
    deployed = runtime.active_merchant(conn, run_id=world.run_id, at_ms=now)
    validation = validated_fix(conn, world, deployed, now)
    preview = plans.build_reprocess_plan(conn, run_id=world.run_id, fix_validation_id=validation.validation_id,
                                         deployed=deployed, now_ms=now, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS)
    assert preview.target_count >= 20
    executor.approve_plan(conn, run_id=world.run_id, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                          operator_id=OPERATOR, now_ms=now + MINUTE, ttl_ms=APPROVAL_TTL_MS)
    return Approved(replayed, deployed, preview)


def rejected_receipt(conn: sqlite3.Connection, approved: Approved, target: plans.PlanTarget, reason: str) -> dict:
    """Execute after the scripted event; the stale plan is rejected exactly as today, and its receipt says why."""
    fulfilled_before = len(storage.list_fulfilments(conn, run_id=approved.run_id))
    with pytest.raises(executor.PlanRejectedError) as caught:
        executor.execute_plan(conn, run_id=approved.run_id, plan_id=approved.preview.plan.plan_id,
                              deployed=approved.deployed, now_ms=approved.cutoff_ms + 3 * MINUTE,
                              stale_after_ms=STALE_AFTER_MS)
    result = caught.value.result
    assert {item.split(":", 1)[0] for item in result.reasons} == {"target_ineligible", "evidence_changed"}
    assert result.reasons.count(f"target_ineligible: {target.id}: {reason}") == 1
    assert len(storage.list_fulfilments(conn, run_id=approved.run_id)) == fulfilled_before  # not even the others
    stored = plans.require_plan(conn, run_id=approved.run_id, plan_id=approved.preview.plan.plan_id)
    assert stored.status is PlanStatus.INVALIDATED
    receipt = report.build_receipt(conn, run_id=approved.run_id, action_id=result.action_id)
    checks = {check["code"]: check for check in receipt["preconditions"]["execution"]["checks"]}
    assert {code: check["result"] for code, check in checks.items()} == {
        **dict.fromkeys(ALL_CHECKS, "passed"), "target_ineligible": "failed", "evidence_changed": "failed",
        executor.RERUN_CHECK: "not_run"}
    assert checks["target_ineligible"]["details"] == [f"{target.id}: {reason}"]
    orders = {order["order_id"]: order for order in receipt["execution"]["orders"]}
    assert (orders[target.id]["result"], orders[target.id]["reasons"]) == ("refused", [reason])
    assert {(order["result"], tuple(order["reasons"])) for key, order in orders.items() if key != target.id} == {
        ("no_op", (executor.PLAN_REJECTED,))}
    assert receipt["execution"]["status"] == "rejected"
    assert receipt["execution"]["results"] == {"completed": 0, "no_op": approved.preview.target_count - 1,
                                               "refused": 1, "failed": 0}
    assert receipt["evidence"]["evidence_unchanged"] is False
    assert (receipt["value"]["restored_processing_value_paise"], receipt["value"]["additional_captured_paise"]) == (0, 0)
    assert receipt["boundary"]["original_capture_unchanged"] is True
    return receipt


def test_a_cancellation_after_approval_makes_the_executor_reject_the_plan_and_the_receipt_shows_it(
    tmp_path: Path,
) -> None:
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        approved = approved_reprocess_plan(conn, CANCELLATION_SEED)
        target = approved.preview.targets[0]
        cancelled_at = approved.cutoff_ms + 2 * MINUTE
        approved.replayed.apply_scripted_event(OrderUpdate(target.id, cancelled_at, "cancel"))  # a merchant event
        assert storage.require_order(conn, run_id=approved.run_id, order_id=target.id).cancelled_at_ms == cancelled_at
        assert storage.require_run(conn, approved.run_id).clock_ms == cancelled_at
        rejected_receipt(conn, approved, target, "cancelled")
        other = approved.preview.targets[1].id
        back_dated = OrderUpdate(other, cancelled_at - 1, "cancel")
        with pytest.raises(ContractError, match="before run"):  # a scripted step is never back-dated
            approved.replayed.apply_scripted_event(back_dated)
        assert storage.require_order(conn, run_id=approved.run_id, order_id=other).cancelled_at_ms is None


def test_a_refund_after_approval_makes_the_executor_reject_the_plan_and_the_receipt_shows_it(tmp_path: Path) -> None:
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        approved = approved_reprocess_plan(conn, REFUND_SEED)
        target = approved.preview.targets[0]
        refunded_at = approved.cutoff_ms + 2 * MINUTE
        approved.replayed.apply_scripted_event(ProviderRefund(target.payment_id, refunded_at))  # a provider event
        assert approved.replayed.provider_ledger.attempt(target.payment_id).status_at(refunded_at).value == "refunded"
        receipt = rejected_receipt(conn, approved, target, "refunded")
        [evidence] = [item for item in receipt["evidence"]["targets"] if item["order_id"] == target.id]
        # The provider state fetch that saw the refund is among the observations the executor judged the order on.
        judged_on = {(item["status"], item["source"], item["known_at_ms"]) for item in evidence["observations"]}
        assert judged_on >= {("refunded", "snapshot", refunded_at)}
        assert receipt["evidence"]["snapshot_ids"].count(f"snap_{target.payment_id}_{refunded_at}") == 1
        second_refund = ProviderRefund(target.payment_id, refunded_at + MINUTE)
        with pytest.raises(ContractError, match="cannot follow"):  # the provider's truth refuses a second refund
            approved.replayed.apply_scripted_event(second_refund)


# --------------------------------------------------------------------------- 3. late authorisation beside a paid retry


def test_a_late_authorisation_beside_a_paid_retry_is_held_alike_by_projection_planner_replay_and_queue(
    tmp_path: Path,
) -> None:
    world = generate_world(scenarios.WORKER_BACKLOG, LATE_AFTER_RETRY_SEED, conformance=LATE_AFTER_RETRY)
    stories = {order.order_id: order.kind for order in world.orders if order.kind in STORY_KINDS}
    assert Counter(stories.values()) == {OrderKind.LATE_AUTHORIZED_AFTER_RETRY: 3,
                                         OrderKind.LATE_AUTHORIZED_AFTER_UNNOTIFIED_RETRY: 3}
    attempts = {order_id: sorted((item for item in world.attempts if item.order_id == order_id),
                                 key=lambda item: item.created_at_ms) for order_id in stories}
    for order_id, (first, retry) in attempts.items():  # provider truth: two captures, the late one last
        assert (first.late_authorized, retry.late_authorized) == (True, False), order_id
        assert (retry.captured_at_ms() or 0) < (first.captured_at_ms() or 0) <= world.cutoff_ms, order_id
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        summary = driver.seed_world(conn, world, oracle_dir=tmp_path / "oracle", stale_after_ms=STALE_AFTER_MS)
        now, run_id = world.cutoff_ms, world.run_id
        deployed = runtime.active_merchant(conn, run_id=run_id, at_ms=now)
        validation = validated_fix(conn, world, deployed, now)
        preview = plans.build_reprocess_plan(conn, run_id=run_id, fix_validation_id=validation.validation_id,
                                             deployed=deployed, now_ms=now, ttl_ms=PLAN_TTL_MS,
                                             stale_after_ms=STALE_AFTER_MS)
        replay = plans.prepare_provider_replay(conn, run_id=run_id, incident_id=ensure_incident(conn, run_id, now),
                                               diagnosis_id=None, now_ms=now, ttl_ms=PLAN_TTL_MS,
                                               stale_after_ms=STALE_AFTER_MS)
        queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=now, stale_after_ms=STALE_AFTER_MS)
        view = metrics.load_run_view(conn, run_id, now, stale_after_ms=STALE_AFTER_MS)
    excluded = {item.order_id: set(item.reasons) for item in preview.excluded}
    held = {item.payment_id: item for item in replay.held_for_reconciliation}
    replay_targets = {target.id for target in replay.targets}
    cases = {case.order_id: case for case in queue.cases}
    for order_id, kind in stories.items():
        first, retry = attempts[order_id]
        projection = view.order_projections[order_id]
        exclusions = {reason.value for reason in projection.exclusions}
        assert exclusions >= {"multiple_captures"}, order_id
        assert {payment.payment_id for payment in projection.captured_payments} == {first.payment_id, retry.payment_id}
        assert [payment.payment_id for payment in projection.payments if payment.late_authorization] == [first.payment_id]
        # The planner: excluded for exactly the projection's reasons, never a target.
        assert excluded[order_id] == exclusions, order_id
        assert order_id not in preview.plan.target_ids
        # The replay plan: a capture no webhook carried is held for the same reasons; nothing of the order is resent.
        assert not {first.payment_id, retry.payment_id} & replay_targets
        if kind is OrderKind.LATE_AUTHORIZED_AFTER_UNNOTIFIED_RETRY:
            assert (held[retry.payment_id].order_id, set(held[retry.payment_id].reasons)) == (order_id, exclusions)
        else:
            assert not {first.payment_id, retry.payment_id} & set(held)
        # The queue: one state, the next step a person takes, and the projection's reasons among its blockers.
        case = cases[order_id]
        assert (case.state, case.next_step) == (CaseState.MULTIPLE_CAPTURES, NextStep.MANUAL_REVIEW), order_id
        assert exclusions <= set(case.blockers)
    # On every other order of the world too, the views agree as phase 11 defines it.
    assert planner_disagreements(queue, preview) == {}
    assert replay_disagreements(queue, replay) == {}
    oracle = json.loads(Path(str(summary.oracle_path)).read_text(encoding="utf-8"))
    assert {oracle["orders"][order_id]["expected"] for order_id in stories} == {"multiple_captures"}
    overlay = LATE_AFTER_RETRY.model_dump(mode="json")
    assert oracle["webhook_conformance"]["options"] == overlay


# --------------------------------------------------------------------------- 4. a provider secret rotation


@dataclass(frozen=True)
class Rotated:
    conn: sqlite3.Connection
    replayed: ReplayedWorld
    oracle: dict
    crossing: tuple[str, ...]  # events first attempted before T_r whose automatic retries came after it

    @property
    def world(self) -> World:
        return self.replayed.world

    @property
    def rotation_at(self) -> int:
        rotation_at = self.world.signing_rotation_at_ms
        assert rotation_at is not None
        return rotation_at


@pytest.fixture
def rotated(tmp_path: Path) -> Iterator[Rotated]:
    world = generate_world(BUSY, ROTATION_SEED, conformance=COORDINATED_ROTATION)
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        replayed = driver.replay_world(conn, world, oracle_dir=tmp_path / "oracle", stale_after_ms=STALE_AFTER_MS)
        rotation_at = world.signing_rotation_at_ms
        assert rotation_at is not None
        crossing = tuple(sorted(
            plan.event_id for plan in world.deliveries
            if any(attempt.at_ms >= rotation_at and attempt.secret_ref == OLD_SECRET
                   for attempt in replayed.provider_attempts(plan.event_id))
        ))
        oracle = json.loads(Path(str(replayed.summary.oracle_path)).read_text(encoding="utf-8"))
        yield Rotated(conn, replayed, oracle, crossing)


def original(world: World, event_id: str) -> DeliveryPlan:
    [plan] = [plan for plan in world.deliveries if plan.event_id == event_id and plan.copy == "original"]
    return plan


def created_at(world: World, event_id: str) -> int:
    """When the provider created the event: the envelope time the replay window is measured from."""
    return ingestion.parse_payment_event(original(world, event_id).raw_body).occurred_at_ms


def merchant_side(conn: sqlite3.Connection, run_id: str) -> tuple[int, int, int]:
    """Delivery attempts, inbox rows and log lines: what a resend, refused or not, could leave behind."""
    return (len(storage.list_delivery_attempts(conn, run_id=run_id)), len(storage.list_inbox_rows(conn, run_id=run_id)),
            len(storage.list_merchant_logs(conn, run_id=run_id, as_of_ms=2**62)))


def test_after_a_coordinated_rotation_old_secret_retries_are_rejected_and_a_manual_replay_gets_each_in_once(
    rotated: Rotated,
) -> None:
    conn, replayed, world, rotation_at = rotated.conn, rotated.replayed, rotated.world, rotated.rotation_at
    run_id, cutoff = world.run_id, world.cutoff_ms
    # The merchant moved only through its config deploy at T_r, which pointed the integration at the new secret.
    [deploy] = storage.list_deployments(conn, run_id=run_id, as_of_ms=cutoff)
    assert (deploy.deployed_at_ms, deploy.kind, deploy.files_changed) == (rotation_at, "config", ("merchant_app/config.py",))
    assert [item.secret_ref for item in storage.list_integrations(conn, run_id=run_id)] == [ROTATED_SIGNING_REF]
    assert (rotated.oracle["webhook_conformance"]["signing_rotation_at_ms"],
            rotated.oracle["webhook_conformance"]["rotation_deploy_id"]) == (rotation_at, deploy.deploy_id)
    # The provider signs every automatic attempt with the secret current when the event was first attempted.
    for plan in world.deliveries:
        attempts = replayed.provider_attempts(plan.event_id)
        if plan.send_at_ms > cutoff:  # due after the cutoff, where the replay stops, so never attempted
            assert attempts == (), plan.event_id
            continue
        expected = OLD_SECRET if plan.send_at_ms < rotation_at else ROTATED_SIGNING_REF
        assert {attempt.secret_ref for attempt in attempts} == {expected}, plan.event_id
        if plan.send_at_ms >= rotation_at:  # a new event carries the new secret and is accepted at once
            assert [attempt.http_status for attempt in attempts] == [200], plan.event_id
    # Each old-secret retry after T_r is rejected with 401, and the receiver records each one as a rejection.
    assert len(rotated.crossing) >= 5
    expected_rejections: Counter[tuple[int, str]] = Counter()
    for event_id in rotated.crossing:
        plan = original(world, event_id)
        attempts = replayed.provider_attempts(event_id)
        assert (attempts[0].at_ms, attempts[0].http_status) == (plan.send_at_ms, None)  # the request was lost
        later = attempts[1:]
        assert len(later) == driver.MAX_DELIVERY_ATTEMPTS - 1, event_id  # every retry the schedule allows
        assert all(item.at_ms >= rotation_at and item.http_status == 401 for item in later), event_id
        expected_rejections.update((item.at_ms, sha256_hex(plan.raw_body)) for item in later)
    rejected = [row for row in storage.list_delivery_attempts(conn, run_id=run_id)
                if row.outcome is ReceiveOutcome.REJECTED_SIGNATURE]
    assert all((row.http_status, row.event_id) == (401, None) for row in rejected)
    assert Counter((row.received_at_ms, row.body_sha256) for row in rejected) == expected_rejections
    inbox_events = {row.event_id for row in storage.list_inbox_rows(conn, run_id=run_id)}
    assert not inbox_events & set(rotated.crossing)
    # Those captures reached the provider but never the inbox: the queue and the replay plan both see that.
    captured = {original(world, event_id).payment_id: original(world, event_id).order_id
                for event_id in rotated.crossing if original(world, event_id).status.value == "captured"}
    assert len(captured) >= 3
    queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=cutoff, stale_after_ms=STALE_AFTER_MS)
    cases = {case.order_id: case for case in queue.cases}
    assert {cases[order_id].state for order_id in captured.values()} == {CaseState.NOTIFICATION_NOT_ACCEPTED}
    replay_plan = plans.prepare_provider_replay(conn, run_id=run_id, incident_id=ensure_incident(conn, run_id, cutoff),
                                                diagnosis_id=None, now_ms=cutoff, ttl_ms=PLAN_TTL_MS,
                                                stale_after_ms=STALE_AFTER_MS)
    assert {target.id for target in replay_plan.targets} == captured.keys()
    # The operator resends each from the dashboard: re-signed with the current secret, it gets in once.
    replay_at = cutoff + MINUTE
    for event_id in rotated.crossing:
        resend = replayed.manual_replay(event_id, at_ms=replay_at)
        assert (resend.sent, resend.secret_ref) == (True, ROTATED_SIGNING_REF), event_id
        assert resend.response is not None
        assert (resend.response.http_status, resend.response.outcome) == (200, ReceiveOutcome.ACCEPTED), event_id
        again = replayed.manual_replay(event_id, at_ms=replay_at)
        assert (again.sent, again.refusal, again.response) == (False, ManualReplayRefusal.ALREADY_ACKNOWLEDGED, None)
    inbox_counts = Counter(row.event_id for row in storage.list_inbox_rows(conn, run_id=run_id))
    assert {inbox_counts[event_id] for event_id in rotated.crossing} == {1}
    live = runtime.active_merchant(conn, run_id=run_id, at_ms=replay_at)
    worker.run_worker(conn, run_id=run_id, handler=live, now_ms=replay_at, retry_backoff_ms=worker.DEFAULT_RETRY_BACKOFF_MS)
    for payment_id, order_id in captured.items():  # one business effect per resent capture
        fulfilment = storage.get_fulfilment(conn, run_id=run_id, order_id=order_id)
        assert fulfilment is not None
        assert (fulfilment.payment_id, fulfilment.via, fulfilment.fulfilled_at_ms) == (payment_id, "worker", replay_at)
    after = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=replay_at, stale_after_ms=STALE_AFTER_MS)
    assert {case.state for case in after.cases if case.order_id in captured.values()} == {CaseState.COMPLETE}


def test_after_a_coordinated_rotation_the_rejected_old_signature_retries_open_an_incident(rotated: Rotated) -> None:
    """The old-signature retries stay far under a fifth of the integration's deliveries, so the
    rejection spike never fires; the signature detector opens one incident at the first evaluation after them."""
    conn, world = rotated.conn, rotated.world
    run_id = world.run_id
    opened = storage.list_incidents(conn, run_id=run_id)
    assert len(opened) == 1  # one incident for the whole burst
    [incident] = opened
    first_rejection = min(row.received_at_ms for row in storage.list_delivery_attempts(conn, run_id=run_id)
                          if row.outcome is ReceiveOutcome.REJECTED_SIGNATURE)
    evaluation_ms = world.spec.detector_interval_minutes * MINUTE
    assert first_rejection < incident.evidence_cutoff_ms <= first_rejection + evaluation_ms
    assert (incident.trigger, incident.cohort) == (metrics.SIGNATURE_REJECTIONS, {"integration_id": INTEGRATION_ID})
    assert incident.measured_impact["fired_detectors"] == [metrics.SIGNATURE_REJECTIONS]
    findings = {item.finding["detector"]: item.finding for item in storage.list_evidence(
        conn, run_id=run_id, as_of_ms=incident.evidence_cutoff_ms, source_record_id=incident.incident_id)}
    spike, signature = findings[metrics.REJECTION_SPIKE], findings[metrics.SIGNATURE_REJECTIONS]
    assert (spike["fired"], spike["reason"]) == (False, "share_normal")  # enough rejections, too small a share
    assert (signature["fired"], signature["reason"]) == (True, "rejections_high")
    measured = signature["measurements"]
    assert measured["rejected_signature"] == spike["measurements"]["rejected"]  # every rejection here is a signature's
    assert measured["distinct_bodies_rejected"] == len(rotated.crossing)
    assert measured["accepted_since_first_rejection"] > 0  # the integration kept accepting the new events
    # The diagnosis packet at the cutoff names it among the detectors that fired, then and since the incident opened.
    deployed = runtime.active_merchant(conn, run_id=run_id, at_ms=world.cutoff_ms)
    built = packet.build_packet(conn, run_id=run_id, incident_id=incident.incident_id, cutoff_ms=world.cutoff_ms,
                                deployed=deployed, persist=False)
    [trigger] = [item for item in built.items if item.evidence_id == "ev_trigger"]
    fired_then_and_since_open = (trigger.facts["fired"], trigger.facts["fired_since_open"])
    assert fired_then_and_since_open == ([metrics.SIGNATURE_REJECTIONS],) * 2


def test_a_manual_replay_of_an_event_the_provider_recorded_as_acknowledged_is_refused(rotated: Rotated) -> None:
    conn, replayed, world = rotated.conn, rotated.replayed, rotated.world
    before_rotation = next(plan for plan in world.deliveries if plan.send_at_ms < rotated.rotation_at
                           and plan.lost_first_attempt is None)
    after_rotation = next(plan for plan in world.deliveries if plan.send_at_ms >= rotated.rotation_at)
    for plan in (before_rotation, after_rotation):
        record = replayed.provider_attempts(plan.event_id)
        assert [attempt.http_status for attempt in record] == [200]
        left_behind = merchant_side(conn, world.run_id)
        resend = replayed.manual_replay(plan.event_id, at_ms=world.cutoff_ms + MINUTE)
        assert (resend.sent, resend.refusal, resend.secret_ref, resend.response) == (
            False, ManualReplayRefusal.ALREADY_ACKNOWLEDGED, None, None)
        assert merchant_side(conn, world.run_id) == left_behind  # nothing was sent
        assert replayed.provider_attempts(plan.event_id) == record


def test_a_manual_replay_is_limited_to_the_replay_window(rotated: Rotated) -> None:
    conn, replayed, world = rotated.conn, rotated.replayed, rotated.world
    by_creation = sorted(rotated.crossing, key=lambda event_id: created_at(world, event_id))
    earlier, later = by_creation[0], by_creation[-1]
    assert created_at(world, earlier) < created_at(world, later)
    at = created_at(world, later) + driver.MANUAL_REPLAY_WINDOW_MS  # the later event's last instant in the window
    left_behind = merchant_side(conn, world.run_id)
    refused = replayed.manual_replay(earlier, at_ms=at)
    assert (refused.sent, refused.refusal, refused.response) == (False, ManualReplayRefusal.OUTSIDE_REPLAY_WINDOW, None)
    assert merchant_side(conn, world.run_id) == left_behind
    sent = replayed.manual_replay(later, at_ms=at)
    assert sent.response is not None
    assert (sent.sent, sent.secret_ref, sent.response.outcome) == (True, ROTATED_SIGNING_REF, ReceiveOutcome.ACCEPTED)
    with pytest.raises(ContractError, match="before run"):  # an explicit step is never back-dated
        replayed.manual_replay(earlier, at_ms=at - 1)
    with pytest.raises(ContractError, match="sent no webhook event"):
        replayed.manual_replay("evt_SYNTHETIC_never_sent", at_ms=at)
