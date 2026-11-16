"""Owner decision Q29 (b): the provider's webhook delivery log, read by a refresh, and what follows from it.

A receiver that answers 2xx and then loses the event before its commit (``scenarios.AckBeforeCommit``) leaves a capture
the workbench filed as ``notification_not_accepted`` and sent to a provider resend, which the provider's dashboard
refuses because it recorded the 2xx. The provider's delivery log is the one record that shows it:

* seeding writes the log beside the provider records, and ``SimulatedDeliveryLog`` serves it read-only, never ahead
  of the time asked about;
* ``refresh.record_delivery_log`` stores what it returns in ``provider_delivery_attempts`` (migration 6), append-only,
  and changes no business state;
* the queue then files such a capture's order as ``notification_acknowledged_not_stored``, next step
  ``manual_review``, and the replay plan holds the capture with the queue's blocker as its reason;
* a capture whose deliveries were rejected, so the log holds no 2xx for it, stays a replay target;
* the API reads the log in its re-check before replay instructions, and so does the CLI demo.

Seeds 19501-19600 are reserved for agent Q29; this file uses 19501-19505.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import (
    INTEGRATION_A,
    INTEGRATION_B,
    MERCHANT_A,
    MERCHANT_B,
    PAY_1,
    PAY_2,
    RUN_A,
    RUN_B,
    SECRET_A,
    SECRET_B,
    START_MS,
    SeededRun,
    add_order,
    business_state,
    count_rows,
    deliver,
    observation,
    seed_run,
    store_observations,
)
from fastapi.testclient import TestClient
from test_api import log_in, make_client, post, queue_at_clock
from test_order_cases import ensure_incident, replay_disagreements
from test_plans import worker_regression_output
from test_receiver_ack_before_commit import MANY_UNSTORED_ACKS, ROTATION_BESIDE_UNSTORED_ACKS, UNSTORED_ACKS
from test_webhook_conformance import BUSY, HEALTHY, OLD_SECRET, PLAN_TTL_MS, STALE_AFTER_MS

from recovery_workbench import cli, diagnosis_store, order_cases, plans, policy, refresh, storage
from recovery_workbench.contracts import (
    CaseBlocker,
    CaseState,
    ContractError,
    ModelMode,
    NextStep,
    PaymentStatus,
    ReceiveOutcome,
    ScopeError,
)
from recovery_workbench.simulator import driver, families, scenarios, signing
from recovery_workbench.simulator.driver import ManualReplayRefusal, ReplayedWorld
from recovery_workbench.simulator.provider import SimulatedDeliveryLog, delivery_log_path
from recovery_workbench.simulator.scenarios import AckBeforeCommit, ScenarioSpec, WebhookConformance
from recovery_workbench.simulator.world import INTEGRATION_ID, DeliveryPlan, World, generate_world

MINUTE = 60_000
RESERVED_SEEDS = range(19501, 19601)
LOG_SEED = 19501  # the broken receiver alone, in a healthy shop
CONTRAST_SEED = 19502  # beside phase 12a's coordinated rotation, whose old-signature retries are rejected
API_SEED = 19503
NO_LOG_SEED = 19504
DEMO_SEED = 19505  # a D3 world, whose diagnosis proposes a replay, with the broken receiver early in the run
DEMO_NAME = "d3_with_unstored_acknowledgements"  # served to cli.demo only by this file
D3_PARAMS = {"fault": "receiver@2", "harmless_position": "none"}
EARLY_UNSTORED_ACKS = WebhookConformance(ack_before_commit=AckBeforeCommit(start_fraction=0.05, end_fraction=0.3,
                                                                           rate=0.25))

NOT_RECEIVED = CaseBlocker.CAPTURE_EVENT_NOT_RECEIVED.value
ACKNOWLEDGED_NOT_STORED = CaseBlocker.CAPTURE_EVENT_ACKNOWLEDGED_NOT_STORED.value
NOW = START_MS + 10 * MINUTE  # when the constructed runs read the delivery log
CAPTURE_EVENT = "evt_SYNTHETIC_q29_capture"


def test_this_file_uses_only_its_reserved_seeds() -> None:
    assert all(seed in RESERVED_SEEDS for seed in (LOG_SEED, CONTRAST_SEED, API_SEED, NO_LOG_SEED, DEMO_SEED))


def unstored_captures(world: World) -> dict[str, DeliveryPlan]:
    """The payment.captured deliveries the broken receiver acknowledged and lost, by payment ID."""
    return {plan.payment_id: plan for plan in world.deliveries
            if plan.lost_first_attempt == "commit" and plan.status is PaymentStatus.CAPTURED}


# --------------------------------------------------------------------------- the simulated provider's delivery log


@dataclass(frozen=True)
class Seeded:
    """A world replayed into a data directory laid out as the CLI and the API expect it."""

    conn: sqlite3.Connection
    replayed: ReplayedWorld
    data_dir: Path
    oracle: dict

    @property
    def world(self) -> World:
        return self.replayed.world

    @property
    def records(self) -> Path:
        return Path(str(self.replayed.summary.provider_records_path))


def seeded(tmp_path: Path, spec: ScenarioSpec, seed: int, overlay: WebhookConformance) -> Iterator[Seeded]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    world = generate_world(spec, seed, conformance=overlay)
    with closing(storage.connect(data_dir / cli.DATABASE_FILE)) as conn:
        replayed = driver.replay_world(conn, world, oracle_dir=data_dir / cli.ORACLE_DIRECTORY,
                                       stale_after_ms=STALE_AFTER_MS)
        oracle = json.loads(Path(str(replayed.summary.oracle_path)).read_text(encoding="utf-8"))
        yield Seeded(conn, replayed, data_dir, oracle)


@pytest.fixture
def alone(tmp_path: Path) -> Iterator[Seeded]:
    yield from seeded(tmp_path, HEALTHY, LOG_SEED, UNSTORED_ACKS)


@pytest.fixture
def contrast(tmp_path: Path) -> Iterator[Seeded]:
    yield from seeded(tmp_path, BUSY, CONTRAST_SEED, ROTATION_BESIDE_UNSTORED_ACKS)


def test_seeding_writes_the_providers_delivery_log_beside_its_records_without_labels(alone: Seeded) -> None:
    world = alone.world
    path = delivery_log_path(alone.records)
    assert path.parent == alone.data_dir / cli.PROVIDER_DIRECTORY
    raw = path.read_text(encoding="utf-8")
    for label in ("kind", "family", "fault", "oracle", "expected", "variant", "late_authorized", "stale", "commit",
                  "lost", "copy", "duplicate", "altered", "secret"):
        assert label not in raw, label
    document = json.loads(raw)
    assert (document["schema_version"], document["run_id"], document["integration_id"]) == (1, world.run_id,
                                                                                           INTEGRATION_ID)
    # Attempt by attempt, the provider's own record of every event it sent, and nothing after the cutoff.
    logged: defaultdict[str, list[tuple[object, ...]]] = defaultdict(list)
    for item in document["attempts"]:
        logged[item["event_id"]].append((item["attempt_id"], item["attempted_at_ms"], item["http_status"],
                                         item["triggered_by"], item["event_type"], item["payment_id"]))
    sent = {plan.event_id: plan for plan in world.deliveries if alone.replayed.provider_attempts(plan.event_id)}
    assert dict(logged) == {
        event_id: [(f"{event_id}:{number}", attempt.at_ms, attempt.http_status, attempt.trigger,
                    signing.EVENT_TYPE_BY_STATUS[plan.status], plan.payment_id)
                   for number, attempt in enumerate(alone.replayed.provider_attempts(event_id), start=1)]
        for event_id, plan in sent.items()
    }
    assert max(item["attempted_at_ms"] for item in document["attempts"]) <= world.cutoff_ms
    # Each capture the receiver acknowledged and lost: one 200 in the provider's log, and no row of the receiver's.
    unstored = unstored_captures(world)
    assert len(unstored) >= 5
    received = {row.event_id for row in storage.list_delivery_attempts(alone.conn, run_id=world.run_id)}
    for plan in unstored.values():
        assert [entry[1:] for entry in logged[plan.event_id]] == [
            (plan.send_at_ms, 200, "automatic", policy.CAPTURE_EVENT_TYPE, plan.payment_id)]
        assert plan.event_id not in received
    # The provider records beside the log keep their format.
    assert json.loads(alone.records.read_text(encoding="utf-8"))["schema_version"] == 1


def test_the_simulated_delivery_log_never_returns_an_attempt_made_after_the_read(alone: Seeded) -> None:
    log = SimulatedDeliveryLog.from_file(delivery_log_path(alone.records))
    whole = log.deliveries_as_of(sorted({plan.payment_id for plan in alone.world.deliveries}), alone.world.cutoff_ms)
    probed = 0
    for payment_id, attempts in sorted(whole.items())[:60]:
        for probe in (*(item.attempted_at_ms - 1 for item in attempts), *(item.attempted_at_ms for item in attempts)):
            assert log.deliveries_as_of([payment_id], probe) == {
                payment_id: [item for item in attempts if item.attempted_at_ms <= probe]}
            probed += 1
    assert probed > 100
    assert log.deliveries_as_of(["pay_unknown"], alone.world.cutoff_ms) == {"pay_unknown": []}


# --------------------------------------------------------------------------- recording what the log says


class PreparedLog:
    """Answers every read with the same prepared mapping, as a misbehaving log might; records each call."""

    def __init__(self, answer: Mapping[str, Sequence[refresh.ProviderDeliveryAttempt]]) -> None:
        self.answer = dict(answer)
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def deliveries_as_of(
        self, payment_ids: Sequence[str], as_of_ms: int
    ) -> dict[str, Sequence[refresh.ProviderDeliveryAttempt]]:
        self.calls.append((tuple(payment_ids), as_of_ms))
        return self.answer


def delivery(number: int, *, at: int, status: int | None, payment_id: str = PAY_1, event_id: str = CAPTURE_EVENT,
             integration_id: str = INTEGRATION_A) -> refresh.ProviderDeliveryAttempt:
    return refresh.ProviderDeliveryAttempt(
        attempt_id=f"{event_id}:{number}", integration_id=integration_id, event_id=event_id,
        event_type=policy.CAPTURE_EVENT_TYPE, payment_id=payment_id, attempted_at_ms=at, http_status=status,
        triggered_by="automatic",
    )


TIMED_OUT = delivery(1, at=START_MS + 1_100, status=None)
ANSWERED = delivery(2, at=START_MS + 61_100, status=200)


def logged_rows(conn: sqlite3.Connection, run_id: str = RUN_A) -> list[tuple[object, ...]]:
    return [(row.provider_attempt_id, row.provider_http_status, row.known_at_ms, row.source_ref)
            for row in storage.list_provider_delivery_attempts(conn, run_id=run_id, as_of_ms=None)]


def test_a_delivery_log_read_records_each_attempt_once_and_changes_no_business_state(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a)
    store_observations(conn, [observation(PaymentStatus.CAPTURED, START_MS + 1_000, START_MS + 2_000)])
    before = business_state(conn, RUN_A)
    log = PreparedLog({PAY_1: [TIMED_OUT, ANSWERED]})
    first = refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=[PAY_1, PAY_1], delivery_log=log, now_ms=NOW)
    assert (first.payment_ids, first.recorded, first.already_recorded, first.acknowledged_captures) == (
        (PAY_1,), (TIMED_OUT.attempt_id, ANSWERED.attempt_id), (), (PAY_1,))
    assert log.calls == [((PAY_1,), NOW)]
    assert logged_rows(conn) == [(TIMED_OUT.attempt_id, None, NOW, f"delivery_log_{NOW}"),
                                 (ANSWERED.attempt_id, 200, NOW, f"delivery_log_{NOW}")]
    assert storage.require_run(conn, RUN_A).clock_ms == NOW
    assert business_state(conn, RUN_A) == before
    # A later read finds the same attempts and stores none of them again; they stay known from the first read on.
    again = refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=[PAY_1], delivery_log=log, now_ms=NOW + MINUTE)
    assert (again.recorded, again.already_recorded) == ((), (TIMED_OUT.attempt_id, ANSWERED.attempt_id))
    assert logged_rows(conn) == [(TIMED_OUT.attempt_id, None, NOW, f"delivery_log_{NOW}"),
                                 (ANSWERED.attempt_id, 200, NOW, f"delivery_log_{NOW}")]
    assert storage.list_provider_delivery_attempts(conn, run_id=RUN_A, as_of_ms=NOW - 1) == ()
    for statement in ("UPDATE provider_delivery_attempts SET provider_http_status = 500 WHERE run_id = ?",
                      "DELETE FROM provider_delivery_attempts WHERE run_id = ?"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), storage.write_transaction(conn):
            conn.execute(statement, (RUN_A,))


@pytest.mark.parametrize(("answer", "message"), [
    pytest.param({PAY_1: [delivery(1, at=NOW + 1, status=200)]}, "future", id="an-attempt-after-the-read"),
    pytest.param({PAY_1: [TIMED_OUT], PAY_2: []}, "not requested", id="a-payment-not-asked-about"),
    pytest.param({PAY_1: [delivery(1, at=START_MS, status=200, payment_id=PAY_2)]}, "when asked for",
                 id="another-payments-attempt"),
    pytest.param({PAY_1: [delivery(1, at=START_MS, status=200, integration_id="int_SYNTHETIC_OTHER")]},
                 "does not have", id="an-integration-the-run-lacks"),
    pytest.param({PAY_1: [TIMED_OUT, TIMED_OUT]}, "twice", id="one-attempt-listed-twice"),
])
def test_a_delivery_log_that_breaks_its_contract_records_nothing(
    conn: sqlite3.Connection, run_a: SeededRun, answer: Mapping[str, Sequence[refresh.ProviderDeliveryAttempt]],
    message: str,
) -> None:
    add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
    log = PreparedLog(answer)
    with pytest.raises(ContractError, match=message):
        refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=[PAY_1], delivery_log=log, now_ms=NOW)
    assert logged_rows(conn) == []
    assert storage.require_run(conn, RUN_A).clock_ms == START_MS


def test_an_attempt_the_provider_reports_differently_on_a_later_read_is_refused(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    add_order(conn, run_a)
    refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=[PAY_1], delivery_log=PreparedLog({PAY_1: [TIMED_OUT]}),
                                now_ms=NOW)
    rewritten = TIMED_OUT.model_copy(update={"http_status": 200})
    log = PreparedLog({PAY_1: [rewritten]})
    with pytest.raises(ContractError, match="differently"):
        refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=[PAY_1], delivery_log=log, now_ms=NOW + MINUTE)
    assert logged_rows(conn) == [(TIMED_OUT.attempt_id, None, NOW, f"delivery_log_{NOW}")]
    assert storage.require_run(conn, RUN_A).clock_ms == NOW


def test_a_delivery_log_read_keeps_to_the_runs_payments_and_never_goes_back_in_time(
    conn: sqlite3.Connection, run_a: SeededRun, run_b: SeededRun
) -> None:
    add_order(conn, run_a)
    add_order(conn, run_b, payment_ids=(PAY_2,))
    log = PreparedLog({})
    for payment_ids in ([PAY_2], [PAY_1, "pay_never_seen"]):
        with pytest.raises(ScopeError, match="not payments of run"):
            refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=payment_ids, delivery_log=log, now_ms=NOW)
    with pytest.raises(ContractError, match="at least one payment"):
        refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=[], delivery_log=log, now_ms=NOW)
    assert log.calls == []  # refused before any call to the provider
    with storage.write_transaction(conn):
        storage.advance_clock(conn, RUN_A, NOW)
    with pytest.raises(ContractError, match="before run"):
        refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=[PAY_1], delivery_log=log, now_ms=NOW - 1)


# --------------------------------------------------------------------------- migration 6 on an existing database


def test_migration_six_adds_the_provider_delivery_log_to_a_database_that_already_holds_runs(db_path: Path) -> None:
    tables = ("orders", "payment_observations", "webhook_inbox", "delivery_attempts")

    def row_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
        return {(table, run_id): count_rows(conn, table, run_id) for table in tables for run_id in (RUN_A, RUN_B)}

    def names(conn: sqlite3.Connection, kind: str) -> set[str]:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}

    with closing(storage.connect(db_path)) as conn:
        # The database as the previous version left it: migrations 1 to 5 (the private helper takes a prefix) ...
        assert storage._apply_migrations(conn, storage.MIGRATIONS[:5]) == (1, 2, 3, 4, 5)
        run_a = seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)
        run_b = seed_run(conn, RUN_B, MERCHANT_B, INTEGRATION_B, SECRET_B)
        add_order(conn, run_a, payment_ids=(PAY_1, PAY_2))
        add_order(conn, run_b)
        # ... with runs in it: run A's first payment was delivered, its second seen only by a provider snapshot.
        deliver(conn, run_a, "evt_SYNTHETIC_q29_a")
        deliver(conn, run_b, "evt_SYNTHETIC_q29_b")
        store_observations(conn, [observation(PaymentStatus.CAPTURED, START_MS + 1_000, START_MS + 2_000,
                                              payment_id=PAY_2, observation_id="obs_q29_pay_2")])
        rows_before = row_counts(conn)
        assert "provider_delivery_attempts" not in names(conn, "table")
    with closing(storage.connect(db_path)) as conn:  # the upgraded application opens it
        assert storage.migrate(conn) == (6, 7, 8)  # 7: Q40's attempt-number and conflict-subject indexes; 8: Q46's mode
        assert storage.migrate(conn) == ()
        assert [tuple(row) for row in conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")] == [
            (migration.version, migration.name) for migration in storage.MIGRATIONS]
        assert row_counts(conn) == rows_before
        assert {"provider_delivery_attempts_by_payment"} <= names(conn, "index")
        assert {"provider_delivery_attempts_no_update", "provider_delivery_attempts_no_delete"} <= names(conn, "trigger")
        # A run seeded before the migration has its delivery log read like any other.
        read = refresh.record_delivery_log(conn, run_id=RUN_A, payment_ids=[PAY_2], now_ms=NOW,
                                           delivery_log=PreparedLog({PAY_2: [delivery(1, at=START_MS + 1_500,
                                                                                       status=200, payment_id=PAY_2)]}))
        assert read.acknowledged_captures == (PAY_2,)
        # Half a manual revert, the version row deleted and the table kept, does not stop the next start.
        with storage.write_transaction(conn):
            conn.execute("DELETE FROM schema_migrations WHERE version = 6")
    with closing(storage.connect(db_path)) as conn:
        assert storage.migrate(conn) == (6,)
        assert len(storage.list_provider_delivery_attempts(conn, run_id=RUN_A, as_of_ms=None)) == 1
        assert row_counts(conn) == rows_before


# --------------------------------------------------------------------------- the queue and the replay plan


def test_after_the_log_is_read_acknowledged_captures_are_held_for_escalation_and_rejected_ones_stay_targets(
    contrast: Seeded,
) -> None:
    conn, world, run_id, cutoff = contrast.conn, contrast.world, contrast.world.run_id, contrast.world.cutoff_ms
    rotation_at = world.signing_rotation_at_ms
    assert rotation_at is not None

    def rejected_after_the_rotation(plan: DeliveryPlan) -> bool:
        attempts = contrast.replayed.provider_attempts(plan.event_id)
        return (any(item.at_ms >= rotation_at and item.secret_ref == OLD_SECRET for item in attempts)
                and not any(item.acknowledged for item in attempts))

    rejected = {plan.payment_id: plan for plan in world.deliveries
                if plan.copy == "original" and plan.status is PaymentStatus.CAPTURED and rejected_after_the_rotation(plan)}
    unstored = unstored_captures(world)
    assert len(rejected) >= 3
    assert len(unstored) >= 3
    incident_id = ensure_incident(conn, run_id, cutoff)

    def queue_and_replay() -> tuple[order_cases.OrderQueue, plans.ReplayInstructions]:
        return (order_cases.classify_orders(conn, run_id=run_id, as_of_ms=cutoff, stale_after_ms=STALE_AFTER_MS),
                plans.prepare_provider_replay(conn, run_id=run_id, incident_id=incident_id, diagnosis_id=None,
                                              now_ms=cutoff, ttl_ms=PLAN_TTL_MS, stale_after_ms=STALE_AFTER_MS))

    queue_before, replay_before = queue_and_replay()
    cases_before = {case.order_id: case for case in queue_before.cases}
    assert set(rejected) | set(unstored) <= {target.id for target in replay_before.targets}  # one shape for both
    # The CLI's read, at the cutoff itself, so only the log differs between the two classifications.
    read = cli.read_delivery_log(conn, contrast.data_dir, run_id, now_ms=cutoff)
    assert read.skipped_because is None
    assert read.summary is not None
    assert set(rejected) | set(unstored) <= set(read.summary.payment_ids)
    assert read.summary.acknowledged_captures == tuple(sorted(unstored))
    queue, replay = queue_and_replay()
    cases = {case.order_id: case for case in queue.cases}
    for plan in unstored.values():
        earlier, now = cases_before[plan.order_id], cases[plan.order_id]
        assert now.blockers == tuple(value for value in order_cases.BLOCKER_ORDER
                                     if value in {*earlier.blockers, ACKNOWLEDGED_NOT_STORED}), plan.order_id
        if earlier.state is CaseState.NOTIFICATION_NOT_ACCEPTED:  # an earlier state, such as stale state, still wins
            assert (now.state, now.next_step) == (CaseState.NOTIFICATION_ACKNOWLEDGED_NOT_STORED, NextStep.MANUAL_REVIEW)
    assert {cases[plan.order_id].state for plan in rejected.values()} == {CaseState.NOTIFICATION_NOT_ACCEPTED}
    assert {order_id: (case.state, case.blockers) for order_id, case in cases.items()
            if order_id not in {plan.order_id for plan in unstored.values()}} == {
        order_id: (case.state, case.blockers) for order_id, case in cases_before.items()
        if order_id not in {plan.order_id for plan in unstored.values()}}
    # The replay holds each acknowledged capture with the queue's blocker last, and targets everything else as before.
    targets = {target.id for target in replay.targets}
    assert targets == {target.id for target in replay_before.targets} - set(unstored)
    assert set(rejected) <= targets
    held = {item.payment_id: item.reasons for item in replay.held_for_reconciliation}
    assert {payment_id for payment_id, reasons in held.items() if reasons[-1:] == (ACKNOWLEDGED_NOT_STORED,)} == set(
        unstored)
    assert replay_disagreements(queue, replay) == {}
    for payment_id, plan in unstored.items():
        [line] = [line for line in replay.instructions if line.startswith(f"Do not resend {payment_id} ")]
        assert plan.event_id in line
        assert "escalate the order" in line
    # The oracle agrees, and so does the dashboard: it resends each rejected capture and refuses each held one.
    expected = {order_id: order["expected"] for order_id, order in contrast.oracle["orders"].items()}
    assert {expected[plan.order_id] for plan in unstored.values()} == {"capture_acknowledged_not_stored"}
    assert {expected[plan.order_id] for plan in rejected.values()} == {"capture_not_durably_received"}
    for plan in rejected.values():
        resend = contrast.replayed.manual_replay(plan.event_id, at_ms=cutoff + MINUTE)
        assert resend.response is not None, plan.event_id
        assert resend.response.outcome is ReceiveOutcome.ACCEPTED, plan.event_id
    for plan in unstored.values():
        resend = contrast.replayed.manual_replay(plan.event_id, at_ms=cutoff + MINUTE)
        assert resend.refusal is ManualReplayRefusal.ALREADY_ACKNOWLEDGED, plan.event_id
    # The provider's log now holds each accepted resend as a manual attempt; a refused one left nothing in it.
    live = contrast.replayed.provider_delivery_log().deliveries_as_of(sorted({*rejected, *unstored}), cutoff + MINUTE)
    manual = {item.payment_id: (item.event_id, item.http_status) for attempts in live.values() for item in attempts
              if item.triggered_by == "manual"}
    assert manual == {payment_id: (plan.event_id, 200) for payment_id, plan in rejected.items()}


# --------------------------------------------------------------------------- the API and the CLI demo


def seed_for_the_api(client: TestClient, world: World) -> str:
    """Seed into the API's data directory and record a diagnosis to plan from; the replay route needs one."""
    workbench = client.app.state.workbench  # type: ignore[attr-defined]
    with closing(storage.connect(workbench.database)) as conn:
        driver.seed_world(conn, world, oracle_dir=workbench.config.data_dir / cli.ORACLE_DIRECTORY)
        incident_id = ensure_incident(conn, world.run_id, world.cutoff_ms)
        stored = diagnosis_store.record_rules_diagnosis(
            conn, run_id=world.run_id, incident_id=incident_id, packet_hash="0" * 64,
            output=worker_regression_output(), rules_version="q29-unit",
        )
    return stored.diagnosis_id


def test_the_api_replay_reads_the_delivery_log_in_its_recheck_step_and_holds_the_acknowledged_captures(
    tmp_path: Path,
) -> None:
    world = generate_world(HEALTHY, API_SEED, conformance=MANY_UNSTORED_ACKS)
    unstored = unstored_captures(world)
    assert len(unstored) >= 10
    with make_client(tmp_path) as client:
        log_in(client)
        diagnosis_id = seed_for_the_api(client, world)
        replay = post(client, f"/api/runs/{world.run_id}/plans",
                      {"operation": "prepare_provider_replay", "diagnosis_id": diagnosis_id})
        _, rows = queue_at_clock(client, world.run_id)
    log = replay["delivery_log"]
    assert replay["delivery_log_skipped_because"] is None
    assert set(log["acknowledged_captures"]) == set(unstored)
    # The read shares the re-check's operator step, so the plan still follows one step after the refresh.
    assert replay["refresh"] is not None
    assert log["now_ms"] == replay["refresh"]["now_ms"]
    assert replay["plan"]["created_at_ms"] == log["now_ms"] + cli.OPERATOR_STEP_MS
    assert not {target["id"] for target in replay["targets"]} & set(unstored)
    held = {item["payment_id"]: item["reasons"] for item in replay["held_for_reconciliation"]}
    assert {held[payment_id][-1] for payment_id in unstored} == {ACKNOWLEDGED_NOT_STORED}
    assert {(rows[plan.order_id]["state"], rows[plan.order_id]["next_step"]) for plan in unstored.values()} == {
        ("notification_acknowledged_not_stored", "manual_review")}


def test_without_a_delivery_log_the_api_replay_says_so_and_still_targets_the_captures(tmp_path: Path) -> None:
    """A run seeded before the log existed: nothing to read, so the captures stay targets and Q29 (a) covers them."""
    world = generate_world(HEALTHY, NO_LOG_SEED, conformance=UNSTORED_ACKS)
    unstored = unstored_captures(world)
    assert len(unstored) >= 3
    with make_client(tmp_path) as client:
        log_in(client)
        diagnosis_id = seed_for_the_api(client, world)
        records = cli.provider_records_path(client.app.state.workbench.config.data_dir, world.run_id)
        delivery_log_path(records).unlink()
        replay = post(client, f"/api/runs/{world.run_id}/plans",
                      {"operation": "prepare_provider_replay", "diagnosis_id": diagnosis_id})
        _, rows = queue_at_clock(client, world.run_id)
    assert (replay["delivery_log"], replay["delivery_log_skipped_because"]) == (None, cli.NO_DELIVERY_LOG_RECORDS)
    assert set(unstored) <= {target["id"] for target in replay["targets"]}
    assert plans.REFUSED_RESEND_INSTRUCTION in replay["instructions"]
    assert {rows[plan.order_id]["state"] for plan in unstored.values()} == {"notification_not_accepted"}


def test_with_nothing_to_check_or_no_records_no_delivery_log_is_read_and_the_clock_stays(tmp_path: Path) -> None:
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        storage.migrate(conn)
        run = seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A)
        add_order(conn, run, payment_ids=(PAY_1, PAY_2))
        deliver(conn, run, "evt_SYNTHETIC_q29_stored")  # PAY_1's capture, stored
        with storage.write_transaction(conn):
            storage.advance_clock(conn, RUN_A, NOW)
        assert cli.read_delivery_log(conn, tmp_path, RUN_A) == cli.DeliveryLogRead(None, cli.NO_CAPTURE_TO_CHECK)
        store_observations(conn, [observation(PaymentStatus.CAPTURED, START_MS + 1_000, START_MS + 2_000,
                                              payment_id=PAY_2, observation_id="obs_q29_snapshot_only")])
        assert cli.read_delivery_log(conn, tmp_path, RUN_A) == cli.DeliveryLogRead(None, cli.NO_PROVIDER_RECORDS)
        assert storage.require_run(conn, RUN_A).clock_ms == NOW


def test_the_cli_demo_reads_the_delivery_log_before_its_replay_and_the_queue_escalates_what_was_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = next(run for run in families.load_manifest().runs if run.family == "D3" and run.params == D3_PARAMS)
    spec = families.spec_for(run.model_copy(update={"split": "dev"}))
    shipped_scenario, shipped_generate = scenarios.demo_scenario, cli.generate_world
    monkeypatch.setattr(scenarios, "demo_scenario", lambda name: spec if name == DEMO_NAME else shipped_scenario(name))
    monkeypatch.setattr(cli, "generate_world",
                        lambda spec_, seed_: shipped_generate(spec_, seed_, conformance=EARLY_UNSTORED_ACKS))
    world = shipped_generate(spec, DEMO_SEED, conformance=EARLY_UNSTORED_ACKS)
    unstored = unstored_captures(world)
    assert len(unstored) >= 3
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lines: list[str] = []
    assert cli.demo(scenario=DEMO_NAME, seed=DEMO_SEED, mode=ModelMode.RULES, data_dir=data_dir,
                    recordings_dir=data_dir / cli.RECORDINGS_DIRECTORY, write=lines.append) == 0
    steps = [index for index, line in enumerate(lines) if line.startswith("[")]
    titles = [lines[index].split("] ", 1)[1] for index in steps]
    refreshed = titles.index("refresh (read-only provider snapshot)")
    assert titles[refreshed + 1].startswith("prepare provider replay")  # the read is inside the refresh step
    step_lines = [line.strip() for line in lines[steps[refreshed] + 1:steps[refreshed + 1]]]
    assert step_lines[-1].startswith("delivery log read at replay time")
    assert f"; {len(unstored)} of them were answered 2xx and never stored" in step_lines[-1]
    report = json.loads((data_dir / cli.REPORTS_DIRECTORY / f"{world.run_id}.json").read_text(encoding="utf-8"))
    [replay] = [plan for plan in report["plans"] if plan["operation"] == "prepare_provider_replay"]
    assert replay["targets"]
    assert not {target["id"] for target in replay["targets"]} & set(unstored)
    with closing(storage.connect(data_dir / cli.DATABASE_FILE)) as conn:
        clock_ms = storage.require_run(conn, world.run_id).clock_ms
        queue = order_cases.classify_orders(conn, run_id=world.run_id, as_of_ms=clock_ms,
                                            stale_after_ms=STALE_AFTER_MS)
    cases = {case.order_id: case for case in queue.cases}
    assert {cases[plan.order_id].state for plan in unstored.values()} == {
        CaseState.NOTIFICATION_ACKNOWLEDGED_NOT_STORED}
