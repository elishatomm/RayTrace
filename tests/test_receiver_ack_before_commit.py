"""A receiver that acknowledges before its inbox commit, then crashes (phase 12, part B).

``scenarios.AckBeforeCommit`` is an opt-in, deliberately broken merchant receiver in the simulator only: for the
deliveries it selects, it answers 2xx at once and crashes before the commit. The provider records the event as
delivered and never retries; the merchant keeps no delivery-attempt row, no inbox row and no observation of it. The
production receiver (``ingestion.receive_webhook``) is unchanged and still acknowledges only after its commit.

The tests named ``test_today_...`` are characterisation, not specification. They pinned what the workbench did on
such a world, which was the wrong thing: it filed an acknowledged-but-unstored capture as a notification it can recover
by asking the provider to resend, and the provider's dashboard refuses that resend. A change that teaches the
workbench to tell these captures apart must change those tests deliberately.

Owner decision Q29 did. (a): the replay instructions say to escalate when the dashboard refuses a resend. (b): a
refresh reads the provider's delivery log, after which the queue files each such capture's order as
``notification_acknowledged_not_stored`` (next step ``manual_review``) and the replay plan holds it; the oracle labels
it ``capture_acknowledged_not_stored``. Until the log is read, the queue and a replay plan still see what they saw
before, so two tests below keep their characterisation for that part and are renamed for what they now show.
``tests/test_provider_delivery_log.py`` covers the rest.

Seeds 17301-17400 are reserved for this file (phase 12, agent P12b); it uses 17301 and 17303. The byte-identity pins
re-seed phase 12a's own worlds, with phase 12a's seeds.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_order_cases import ensure_incident
from test_planted_instruction import world_digest
from test_webhook_conformance import (
    BUSY,
    COORDINATED_ROTATION,
    HEALTHY,
    LATE_AFTER_RETRY,
    LATE_AFTER_RETRY_SEED,
    LOST_ACK_SEED,
    LOST_ACKS,
    OLD_SECRET,
    PLAN_TTL_MS,
    ROTATION_SEED,
    SETTINGS,
    STALE_AFTER_MS,
    merchant_side,
)

from recovery_workbench import diagnosis_store, order_cases, plans, refresh, storage
from recovery_workbench.contracts import (
    CaseBlocker,
    CaseState,
    CheckType,
    DiagnosisStatus,
    Hypothesis,
    ModelMode,
    NextStep,
    ObservationSource,
    Operation,
    ReceiveOutcome,
)
from recovery_workbench.simulator import driver, runtime, scenarios
from recovery_workbench.simulator.driver import ManualReplayRefusal, ReplayedWorld
from recovery_workbench.simulator.provider import SimulatedDeliveryLog, SimulatedProviderAdapter, delivery_log_path
from recovery_workbench.simulator.scenarios import AckBeforeCommit, LostDeliveries, ScenarioSpec, WebhookConformance
from recovery_workbench.simulator.world import DeliveryPlan, World, generate_world

MINUTE = 60_000
RESERVED_SEEDS = range(17301, 17401)
BROKEN_RECEIVER_SEED = 17301
REJECTED_BESIDE_UNSTORED_SEED = 17303

# For the middle half of a healthy two-hour run, the receiver loses one delivery in ten: few enough that no
# detector fires, so the queue is the only place the loss shows.
UNSTORED_ACKS = WebhookConformance(
    ack_before_commit=AckBeforeCommit(start_fraction=0.25, end_fraction=0.75, rate=0.1)
)
# The same stretch at three in ten: enough lost captures for the processing-gap detector to open an incident.
MANY_UNSTORED_ACKS = WebhookConformance(
    ack_before_commit=AckBeforeCommit(start_fraction=0.25, end_fraction=0.75, rate=0.3)
)
# Phase 12a's coordinated rotation (its rejected old-signature retries are never acknowledged) plus the broken
# receiver earlier in the run, in a window apart from the rotation's lost requests.
ROTATION_BESIDE_UNSTORED_ACKS = WebhookConformance(
    secret_rotation=COORDINATED_ROTATION.secret_rotation,
    lost_deliveries=COORDINATED_ROTATION.lost_deliveries,
    ack_before_commit=AckBeforeCommit(start_fraction=0.1, end_fraction=0.4, rate=0.05),
)
NOT_RECEIVED = (CaseBlocker.CAPTURE_EVENT_NOT_RECEIVED.value,)
ACKNOWLEDGED_NOT_STORED = CaseBlocker.CAPTURE_EVENT_ACKNOWLEDGED_NOT_STORED.value

# (world digest, observable dataset sha256, oracle sha256) of phase 12a's three overlay worlds, measured on HEAD
# 3206c19 before this behaviour existed.
PRE_ACK_BEFORE_COMMIT: dict[str, tuple[ScenarioSpec, int, WebhookConformance, tuple[str, str, str]]] = {
    "lost_acks": (HEALTHY, LOST_ACK_SEED, LOST_ACKS, (
        "0609fec5c170b876ab7b6921a057de4788c0041b659d216708b754df8599aade",
        "eab4a3c9d728329372d5cc022de49b80455201a5023901060bcf0e0664c08916",
        "d3aa6f36da456b6a58b665996369df68164bdc61e51904bea40cc7bec0a00287",
    )),
    "late_after_retry": (scenarios.WORKER_BACKLOG, LATE_AFTER_RETRY_SEED, LATE_AFTER_RETRY, (
        "906c4eb97b7f145a680bea80c1623dc87074b63af3672732c00b736f80fba06f",
        "23a8f44df56566a9a38afe56b4172b8aaecba19b0ac35a00a25ad88ed36a9ec2",
        "e1edc46f3b77373c4408b690a430005f06bb556b4fc1dbc94fa51ca0a6d31cba",
    )),
    # Re-measured on 2026-11-13: the rejected old-signature retries now open an incident, whose
    # incident and evidence rows are observable. The world itself is unchanged.
    "rotation": (BUSY, ROTATION_SEED, COORDINATED_ROTATION, (
        "44e272af8cb13a333f0b7e54c30d707d6c55fa05d66d996885de3e3d6c5c47e3",
        "f48af947e2590a6a06a2cd08d18a3614f7d56a1bd6c2649a2bcb224eef2401e9",
        "c23402aeec9b68276ddad2419f76874a55352a23611867e0955bbff9b01bafa9",
    )),
}


def test_this_file_uses_only_its_reserved_seeds() -> None:
    assert all(seed in RESERVED_SEEDS for seed in (BROKEN_RECEIVER_SEED, REJECTED_BESIDE_UNSTORED_SEED))


# --------------------------------------------------------------------------- off by default


@pytest.mark.parametrize("name", sorted(PRE_ACK_BEFORE_COMMIT))
def test_without_the_broken_receiver_the_phase_12a_overlay_worlds_are_byte_identical_to_before_it(
    tmp_path: Path, name: str
) -> None:
    spec, seed, overlay, measured_before = PRE_ACK_BEFORE_COMMIT[name]
    assert overlay.ack_before_commit is None
    assert "ack_before_commit" not in overlay.model_dump(mode="json")  # the oracle's copy of the overlay is as before
    world = generate_world(spec, seed, conformance=overlay)
    assert all(plan.lost_first_attempt != "commit" for plan in world.deliveries)
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        summary = driver.seed_world(conn, world, oracle_dir=tmp_path / "oracle", provider_dir=tmp_path / "provider")
    measured_now = (world_digest(world), summary.observable_sha256, summary.oracle_sha256)
    assert measured_now == measured_before


def test_the_broken_receiver_needs_an_ordered_window_apart_from_lost_deliveries() -> None:
    with pytest.raises(ValidationError, match="start_fraction < end_fraction"):
        AckBeforeCommit(start_fraction=0.5, end_fraction=0.5, rate=0.1)
    unstored = AckBeforeCommit(start_fraction=0.2, end_fraction=0.4, rate=0.1)
    overlapping = LostDeliveries(start_fraction=0.3, end_fraction=0.5, rate=0.1)
    with pytest.raises(ValidationError, match="windows overlap"):  # a first attempt meets one of the two, never both
        WebhookConformance(lost_deliveries=overlapping, ack_before_commit=unstored)
    adjacent = LostDeliveries(start_fraction=0.4, end_fraction=0.5, rate=0.1)  # windows include start, exclude end
    assert WebhookConformance(lost_deliveries=adjacent, ack_before_commit=unstored).ack_before_commit == unstored
    alone = WebhookConformance(ack_before_commit=unstored)  # a behaviour of its own
    assert alone.model_dump(mode="json")["ack_before_commit"] == unstored.model_dump(mode="json")


# --------------------------------------------------------------------------- the broken receiver


@dataclass(frozen=True)
class Broken:
    """A healthy shop whose receiver loses selected deliveries, beside its twin without the broken receiver."""

    conn: sqlite3.Connection
    twin_conn: sqlite3.Connection
    replayed: ReplayedWorld
    oracle: dict

    @property
    def world(self) -> World:
        return self.replayed.world

    @property
    def unstored(self) -> dict[str, DeliveryPlan]:
        """Every delivery the receiver acknowledged and lost, by event ID."""
        return {plan.event_id: plan for plan in self.world.deliveries if plan.lost_first_attempt == "commit"}

    @property
    def unstored_captures(self) -> dict[str, DeliveryPlan]:
        """The lost payment.captured deliveries, by order ID: the orders the loss leaves unfulfilled."""
        return {plan.order_id: plan for plan in self.unstored.values() if plan.status.value == "captured"}


def replay_with_twin(tmp_path: Path, conformance: WebhookConformance) -> Iterator[Broken]:
    world = generate_world(HEALTHY, BROKEN_RECEIVER_SEED, conformance=conformance)
    twin = generate_world(HEALTHY, BROKEN_RECEIVER_SEED)
    with (closing(storage.connect(tmp_path / "broken.sqlite")) as conn,
          closing(storage.connect(tmp_path / "twin.sqlite")) as twin_conn):
        replayed = driver.replay_world(conn, world, oracle_dir=tmp_path / "oracle", provider_dir=tmp_path / "provider",
                                       stale_after_ms=STALE_AFTER_MS)
        driver.seed_world(twin_conn, twin, oracle_dir=None, stale_after_ms=STALE_AFTER_MS)
        oracle = json.loads(Path(str(replayed.summary.oracle_path)).read_text(encoding="utf-8"))
        yield Broken(conn, twin_conn, replayed, oracle)


@pytest.fixture
def broken(tmp_path: Path) -> Iterator[Broken]:
    yield from replay_with_twin(tmp_path, UNSTORED_ACKS)


@pytest.fixture
def often_broken(tmp_path: Path) -> Iterator[Broken]:
    yield from replay_with_twin(tmp_path, MANY_UNSTORED_ACKS)


def merchant_records(conn: sqlite3.Connection, run_id: str) -> dict[str, list[tuple[object, ...]]]:
    """The merchant's records of each delivery and what followed from it, every row led by its event ID."""
    inbox = storage.list_inbox_rows(conn, run_id=run_id)
    events = {row.inbox_id: row.event_id for row in inbox}
    observations = storage.list_observations(conn, run_id=run_id, as_of_ms=None)
    return {
        "delivery_attempts": sorted((row.event_id, row.attempt_no, row.http_status, row.outcome.value,
                                     row.received_at_ms, row.body_sha256)
                                    for row in storage.list_delivery_attempts(conn, run_id=run_id)),
        "inbox": sorted((row.event_id, row.event_type, row.body_sha256, row.received_at_ms,
                         row.processing_status.value, row.attempts, row.processed_at_ms) for row in inbox),
        "webhook_observations": sorted((item.source_ref, item.payment_id, item.status.value, item.known_at_ms)
                                       for item in observations if item.source is ObservationSource.WEBHOOK),
        "worker_attempts": sorted((events[item.inbox_id], item.result.value, item.started_at_ms)
                                  for item in storage.list_worker_attempts(conn, run_id=run_id)),
    }


def fulfilments(conn: sqlite3.Connection, run_id: str) -> list[tuple[object, ...]]:
    return sorted((item.order_id, item.payment_id, item.amount_paise, item.via, item.fulfilled_at_ms)
                  for item in storage.list_fulfilments(conn, run_id=run_id))


def merchant_logs(conn: sqlite3.Connection, run_id: str) -> list[tuple[object, ...]]:
    return [(item.log_id, item.service_id, item.level, item.message, item.occurred_at_ms)
            for item in storage.list_merchant_logs(conn, run_id=run_id, as_of_ms=2**62)]


def test_the_broken_receiver_acknowledges_selected_deliveries_and_the_merchant_keeps_no_record_of_them(
    broken: Broken,
) -> None:
    world, run_id = broken.world, broken.world.run_id
    twin = generate_world(HEALTHY, BROKEN_RECEIVER_SEED)
    # The overlay changes how selected deliveries end, nothing the provider or the shop did.
    assert [DeliveryPlan(p.event_id, p.payment_id, p.order_id, p.status, p.send_at_ms, p.raw_body, p.copy)
            for p in world.deliveries] == list(twin.deliveries)
    assert (world.orders, world.attempts, world.deployments, world.logs) == (
        twin.orders, twin.attempts, twin.deployments, twin.logs)
    assert len(broken.unstored) >= 10
    assert len(broken.unstored_captures) >= 5
    for event_id, plan in broken.unstored.items():
        # The provider saw a 2xx to its first attempt, so it recorded the event as delivered and never retried.
        seen = [(item.at_ms, item.trigger, item.http_status) for item in broken.replayed.provider_attempts(event_id)]
        assert seen == [(plan.send_at_ms, "automatic", 200)], event_id
    # The merchant has exactly what its twin has, less every trace of the lost deliveries: no delivery attempt, inbox
    # row, observation or worker attempt, no fulfilment for a lost capture, and no log line about any of it.
    twin_records = merchant_records(broken.twin_conn, run_id)
    assert merchant_records(broken.conn, run_id) == {
        table: [row for row in rows if row[0] not in broken.unstored] for table, rows in twin_records.items()}
    assert fulfilments(broken.conn, run_id) == [
        row for row in fulfilments(broken.twin_conn, run_id) if row[0] not in broken.unstored_captures]
    assert merchant_logs(broken.conn, run_id) == merchant_logs(broken.twin_conn, run_id)
    # The oracle records the overlay and each acknowledged-but-unstored first attempt.
    conformance = broken.oracle["webhook_conformance"]
    assert conformance["options"] == UNSTORED_ACKS.model_dump(mode="json")
    assert sorted(conformance["lost_first_attempts"]) == sorted(
        [event_id, "original", "commit"] for event_id in broken.unstored)


# --------------------------------------------------------------------------- what the workbench does


def test_until_the_delivery_log_is_read_the_queue_files_each_acknowledged_but_unstored_capture_as_not_accepted(
    broken: Broken,
) -> None:
    world, run_id = broken.world, broken.world.run_id
    assert broken.replayed.summary.incident_ids == ()  # no detector fires: the queue is the only place it shows
    queue = order_cases.classify_orders(broken.conn, run_id=run_id, as_of_ms=world.cutoff_ms,
                                        stale_after_ms=STALE_AFTER_MS)
    twin_queue = order_cases.classify_orders(broken.twin_conn, run_id=run_id, as_of_ms=world.cutoff_ms,
                                             stale_after_ms=STALE_AFTER_MS)
    cases = {case.order_id: case for case in queue.cases}
    twin_cases = {case.order_id: case for case in twin_queue.cases}
    for order_id, plan in broken.unstored_captures.items():
        case = cases[order_id]
        # Read as a capture whose notification never got in, which a resend from the provider would recover.
        assert (case.state, case.next_step, case.blockers) == (
            CaseState.NOTIFICATION_NOT_ACCEPTED, NextStep.PREPARE_PROVIDER_REPLAY, NOT_RECEIVED), order_id
        assert (case.capture_processing.captured_inbox_ids, case.coverage.captured_events_received) == ((), 0)
        [payment] = [item for item in case.payments if item.payment_id == plan.payment_id]
        assert (payment.status.value, payment.snapshot_observations > 0) == ("captured", True)  # the reconciler's view
        assert twin_cases[order_id].state is CaseState.COMPLETE, order_id
    # Nothing else moves: every other order has its twin's state and blockers.
    assert {order_id: (case.state, case.blockers) for order_id, case in cases.items()
            if order_id not in broken.unstored_captures} == {
        order_id: (case.state, case.blockers) for order_id, case in twin_cases.items()
        if order_id not in broken.unstored_captures}
    by_state = {total.state: total for total in queue.totals.by_state}
    order_amounts = {order.order_id: order.amount_paise for order in world.orders}
    assert by_state[CaseState.NOTIFICATION_NOT_ACCEPTED].orders == len(broken.unstored_captures)
    assert by_state[CaseState.NOTIFICATION_NOT_ACCEPTED].asserted_value_paise == sum(
        order_amounts[order_id] for order_id in broken.unstored_captures)
    # The run-level context the queue offers beside notification_not_accepted is silent: no rejected delivery and no
    # delivery without an inbox row, because the broken receiver left no delivery record at all.
    assert (queue.coverage.rejected_deliveries, queue.coverage.deliveries_without_inbox_row) == (0, 0)
    assert queue.coverage.delivery_attempts == queue.coverage.inbox_rows
    # The oracle knows what the provider recorded (Q29 (b)): its own label, ahead of the one every capture never
    # durably received carries.
    assert {tuple(broken.oracle["orders"][order_id]["reasons"][:2]) for order_id in broken.unstored_captures} == {
        ("capture_acknowledged_not_stored", "capture_not_durably_received")}
    assert {broken.oracle["orders"][order_id]["expected"] for order_id in broken.unstored_captures} == {
        "capture_acknowledged_not_stored"}


def test_the_replay_plan_targets_an_acknowledged_capture_until_the_delivery_log_is_read_then_holds_it(
    broken: Broken,
) -> None:
    conn, world, run_id, cutoff = broken.conn, broken.world, broken.world.run_id, broken.world.cutoff_ms
    lost = {plan.payment_id: plan for plan in broken.unstored_captures.values()}
    incident_id = ensure_incident(conn, run_id, cutoff)
    replay = plans.prepare_provider_replay(conn, run_id=run_id, incident_id=incident_id, diagnosis_id=None,
                                           now_ms=cutoff, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS)
    # Before the delivery log is read: every acknowledged-but-unstored capture is a target, none is held, and their
    # value counts as recoverable...
    assert {target.id for target in replay.targets} == lost.keys()
    assert replay.held_for_reconciliation == ()
    captured_amounts = {attempt.payment_id: attempt.amount_paise for attempt in world.attempts}
    assert replay.total_value_paise == sum(captured_amounts[payment_id] for payment_id in lost)
    for payment_id in lost:
        assert sum(line.startswith(f"Resend payment.captured for {payment_id} ") for line in replay.instructions) == 1
    # ...but the instructions now say what the dashboard's refusal means (Q29 (a)).
    assert plans.REFUSED_RESEND_INSTRUCTION in replay.instructions
    # The operator follows the plan; the dashboard refuses every resend, because the provider recorded a 2xx.
    for plan in lost.values():
        record = broken.replayed.provider_attempts(plan.event_id)
        left_behind = merchant_side(conn, run_id)
        resend = broken.replayed.manual_replay(plan.event_id, at_ms=cutoff + MINUTE)
        assert (resend.sent, resend.refusal, resend.response) == (
            False, ManualReplayRefusal.ALREADY_ACKNOWLEDGED, None), plan.event_id
        assert merchant_side(conn, run_id) == left_behind  # nothing was sent
        assert broken.replayed.provider_attempts(plan.event_id) == record
    # A refresh of payment state alone confirms each capture and nothing else, so the queue still files them alike.
    records = Path(str(broken.replayed.summary.provider_records_path))
    later = cutoff + 2 * MINUTE
    refreshed = refresh.refresh_provider_state(conn, run_id=run_id, payment_ids=sorted(lost),
                                               adapter=SimulatedProviderAdapter.from_records(records), now_ms=later)
    assert refreshed.statuses == dict.fromkeys(lost, "captured")
    queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=later, stale_after_ms=STALE_AFTER_MS)
    cases = {case.order_id: case for case in queue.cases}
    assert {(cases[plan.order_id].state, cases[plan.order_id].blockers) for plan in lost.values()} == {
        (CaseState.NOTIFICATION_NOT_ACCEPTED, NOT_RECEIVED)}
    # The provider's delivery log, read in the same step, holds the 2xx it recorded for each (Q29 (b))...
    read = refresh.record_delivery_log(conn, run_id=run_id, payment_ids=sorted(lost), now_ms=later,
                                       delivery_log=SimulatedDeliveryLog.from_file(delivery_log_path(records)))
    assert read.acknowledged_captures == tuple(sorted(lost))
    # ...so the queue sends each order to a person, and a new plan holds each capture and says to escalate it.
    queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=later, stale_after_ms=STALE_AFTER_MS)
    cases = {case.order_id: case for case in queue.cases}
    assert {(cases[plan.order_id].state, cases[plan.order_id].next_step, cases[plan.order_id].blockers)
            for plan in lost.values()} == {(CaseState.NOTIFICATION_ACKNOWLEDGED_NOT_STORED, NextStep.MANUAL_REVIEW,
                                            (*NOT_RECEIVED, ACKNOWLEDGED_NOT_STORED))}
    again = plans.prepare_provider_replay(conn, run_id=run_id, incident_id=incident_id, diagnosis_id=None,
                                          now_ms=later, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS)
    assert not {target.id for target in again.targets} & lost.keys()
    assert {(item.payment_id, item.reasons) for item in again.held_for_reconciliation} == {
        (payment_id, (ACKNOWLEDGED_NOT_STORED,)) for payment_id in lost}
    for payment_id, plan in lost.items():
        [line] = [line for line in again.instructions if line.startswith(f"Do not resend {payment_id} ")]
        assert plan.event_id in line
        assert "escalate the order" in line


def test_today_an_unstored_capture_looks_like_a_rejected_one_until_the_dashboard_answers(tmp_path: Path) -> None:
    world = generate_world(BUSY, REJECTED_BESIDE_UNSTORED_SEED, conformance=ROTATION_BESIDE_UNSTORED_ACKS)
    rotation_at = world.signing_rotation_at_ms
    assert rotation_at is not None
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        replayed = driver.replay_world(conn, world, oracle_dir=None, stale_after_ms=STALE_AFTER_MS)
        run_id, cutoff = world.run_id, world.cutoff_ms
        originals = [plan for plan in world.deliveries if plan.copy == "original" and plan.status.value == "captured"]

        def retried_with_the_old_secret_after_the_rotation(plan: DeliveryPlan) -> bool:
            attempts = replayed.provider_attempts(plan.event_id)
            return any(item.at_ms >= rotation_at and item.secret_ref == OLD_SECRET for item in attempts)

        # Rejected: first attempted before the rotation, retried after it with the old signature, never acknowledged.
        rejected = {plan.order_id: plan for plan in originals if retried_with_the_old_secret_after_the_rotation(plan)}
        unstored = {plan.order_id: plan for plan in originals if plan.lost_first_attempt == "commit"}
        assert len(rejected) >= 3
        assert len(unstored) >= 3
        for plan in rejected.values():
            assert not any(item.acknowledged for item in replayed.provider_attempts(plan.event_id)), plan.event_id
        for plan in unstored.values():
            assert [item.http_status for item in replayed.provider_attempts(plan.event_id)] == [200], plan.event_id
        stored_events = {row.event_id for row in storage.list_inbox_rows(conn, run_id=run_id)}
        recorded_events = {row.event_id for row in storage.list_delivery_attempts(conn, run_id=run_id)}
        both = [*rejected.values(), *unstored.values()]
        assert not {plan.event_id for plan in both} & (stored_events | recorded_events)  # no row names either kind
        queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=cutoff, stale_after_ms=STALE_AFTER_MS)
        cases = {case.order_id: case for case in queue.cases}
        # One case shape for both kinds: the queue has nothing that tells them apart.
        shapes = {(cases[plan.order_id].state, cases[plan.order_id].next_step, cases[plan.order_id].blockers,
                   cases[plan.order_id].capture_processing, cases[plan.order_id].coverage.captured_events_received)
                  for plan in both}
        assert shapes == {(CaseState.NOTIFICATION_NOT_ACCEPTED, NextStep.PREPARE_PROVIDER_REPLAY, NOT_RECEIVED,
                           order_cases.CaptureProcessing(captured_inbox_ids=(), unsettled_inbox_ids=(),
                                                         target_inbox_id=None, worker_failures=0, settled_results=()),
                           0)}
        replay = plans.prepare_provider_replay(conn, run_id=run_id, incident_id=ensure_incident(conn, run_id, cutoff),
                                               diagnosis_id=None, now_ms=cutoff, ttl_ms=PLAN_TTL_MS,
                                               stale_after_ms=STALE_AFTER_MS)
        assert {plan.payment_id for plan in both} <= {target.id for target in replay.targets}
        assert replay.held_for_reconciliation == ()
        # Only the provider's own record decides: it resends the rejected captures and refuses the unstored ones.
        for plan in rejected.values():
            resend = replayed.manual_replay(plan.event_id, at_ms=cutoff + MINUTE)
            assert resend.response is not None
            assert (resend.sent, resend.response.outcome) == (True, ReceiveOutcome.ACCEPTED), plan.event_id
        for plan in unstored.values():
            resend = replayed.manual_replay(plan.event_id, at_ms=cutoff + MINUTE)
            assert (resend.sent, resend.refusal) == (False, ManualReplayRefusal.ALREADY_ACKNOWLEDGED), plan.event_id


def test_today_a_large_loss_opens_a_processing_gap_incident_that_the_rules_escalate_as_unknown(
    often_broken: Broken,
) -> None:
    conn, world, run_id = often_broken.conn, often_broken.world, often_broken.world.run_id
    incidents = storage.list_incidents(conn, run_id=run_id)
    assert [incident.trigger for incident in incidents] == ["merchant_processing_gap"]
    assert storage.list_incidents(often_broken.twin_conn, run_id=run_id) == ()
    stored = diagnosis_store.diagnose_incident(
        conn, run_id=run_id, incident_id=incidents[0].incident_id, mode=ModelMode.RULES, settings=SETTINGS,
        deployed=runtime.active_merchant(conn, run_id=run_id, at_ms=world.cutoff_ms),
    )
    assert stored.output is not None
    # No rule recognises the pattern: no rejected delivery, no worker error. The rules path escalates as unknown...
    assert (stored.status, stored.output.hypothesis, stored.output.proposed_operation, stored.output.next_check) == (
        DiagnosisStatus.VALIDATED, Hypothesis.UNKNOWN, Operation.ESCALATE, CheckType.INSPECT_ORDER_JOURNEY)
    # ...while the queue sends every one of these orders to a provider replay.
    queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=world.cutoff_ms, stale_after_ms=STALE_AFTER_MS)
    cases = {case.order_id: case for case in queue.cases}
    assert len(often_broken.unstored_captures) >= 20
    assert {cases[order_id].next_step for order_id in often_broken.unstored_captures} == {
        NextStep.PREPARE_PROVIDER_REPLAY}
