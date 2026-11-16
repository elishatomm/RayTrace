"""Replay a generated world through the real workbench components, on the run's replay clock.

Orders and attempts are inserted when they happen. Provider transitions become signed
webhook deliveries that pass through the deployed receiver's
``body_for_verification`` and then ``ingestion.receive_webhook``, with delays,
duplicates, and retries with backoff on non-2xx responses. The worker runs on ticks
with the merchant code live at that tick. A reconciler records provider snapshots.
Deployments go through ``simulator.runtime``, and detectors run on their own ticks.
Business time is always replay time; the wall clock is never consulted for it.

``replay_world`` does the same and keeps the simulator attached (``ReplayedWorld``) for
explicit steps after the cutoff: one scripted merchant or provider event, or a manual
resend from the provider's dashboard. Nothing else runs after the cutoff.
"""
from __future__ import annotations

import heapq
import itertools
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import JsonValue

from recovery_workbench import incidents, ingestion, settings, storage, worker
from recovery_workbench.contracts import (
    ContractError,
    ModelMode,
    ObservationSource,
    PaymentObservation,
    PaymentStatus,
    ReceiveOutcome,
    WorkerResult,
)
from recovery_workbench.simulator import merchant, runtime, signing
from recovery_workbench.simulator.oracle import observable_dataset_hash, write_oracle
from recovery_workbench.simulator.provider import (
    AttemptTruth,
    LoggedDelivery,
    ProviderLedger,
    ProviderSnapshot,
    SimulatedDeliveryLog,
    Transition,
    write_provider_delivery_log,
    write_provider_records,
)
from recovery_workbench.simulator.scenarios import METHODS
from recovery_workbench.simulator.world import (
    GENERATOR_VERSION,
    HOUR_MS,
    INTEGRATION_ID,
    MERCHANT_ID,
    MINUTE_MS,
    ROTATED_SIGNING_REF,
    DeliveryPlan,
    DeploymentPlan,
    LogPlan,
    OrderPlan,
    OrderUpdate,
    World,
)

MAX_BODY_BYTES = 65_536
MAX_DELIVERY_ATTEMPTS = 5
RETRY_BACKOFF_MS = (1 * MINUTE_MS, 5 * MINUTE_MS, 15 * MINUTE_MS, 30 * MINUTE_MS)  # after attempts 1..4
SNAPSHOT_MIN_AGE_MS = 2 * MINUTE_MS  # the reconciler leaves brand-new payments to their webhooks
MAX_SNAPSHOTS_PER_TICK = 400
_FINAL_EVIDENCE = frozenset({"captured", "failed", "refunded"})

# Signing across a provider secret rotation (scenarios.SecretRotation). This follows one gateway's documented
# behaviour, checked against its public documentation on 2026-11-13.
# An event is signed when it is first attempted, so automatic retries of an event first attempted before the rotation
# keep the old secret's signature. A manual resend from the dashboard is signed with the secret current at that moment.
# The dashboard does not resend an event the provider already recorded as delivered with a 2xx answer. Another major
# gateway documents the opposite for retries: it signs every delivery attempt afresh. So this models one gateway, not
# every gateway.
#
# How long after an event's creation the dashboard still resends it: 7 days, the same gateway's documented window and
# the shortest any provider checked documents (others document 15 and 30 days). It covers
# the longest run a ScenarioSpec allows (24 hours) plus days for an operator to act on a replay plan. Only a manual
# replay reads it, after the cutoff, so it changes no seeded world.
MANUAL_REPLAY_WINDOW_MS = 7 * 24 * HOUR_MS

# What the broken receiver of scenarios.AckBeforeCommit answers before it crashes: the production receiver's answer to
# an accepted delivery, sent before anything is verified or stored.
UNSTORED_ACK_STATUS = ingestion.HTTP_STATUS_BY_OUTCOME[ReceiveOutcome.ACCEPTED]

# Same-time ordering: a deploy takes effect before traffic at that instant; detectors run last.
_DEPLOY, _ORDER, _ORDER_UPDATE, _ATTEMPT, _DELIVERY, _LOG, _RECONCILE, _WORKER, _DETECT = range(9)


@dataclass(frozen=True)
class ProviderAttempt:
    """One delivery attempt as the simulated provider recorded it; simulator state, never written to the database."""

    event_id: str
    copy: Literal["original", "duplicate", "altered"]
    trigger: Literal["automatic", "manual"]
    at_ms: int
    secret_ref: str  # the secret its signature was made with
    http_status: int | None  # None: the provider saw no answer and recorded a timeout

    @property
    def acknowledged(self) -> bool:
        return self.http_status is not None and 200 <= self.http_status < 300


@dataclass
class SimulationLog:
    """What the simulator itself caused and saw while replaying.

    The oracle derives its expectations from latent truth plus this log, never from the
    workbench's projection or planner: accepted deliveries and inserted snapshots (when the
    workbench could first see each payment) and the fulfilments the worker reported. The
    provider's own record of each delivery attempt decides what its dashboard may resend.
    """

    observed_at_ms: dict[str, list[int]] = field(default_factory=dict)
    accepted_statuses: dict[str, set[PaymentStatus]] = field(default_factory=dict)
    fulfilled_at_ms: dict[str, int] = field(default_factory=dict)
    provider_attempts: dict[str, list[ProviderAttempt]] = field(default_factory=dict)

    def attempted(self, attempt: ProviderAttempt) -> None:
        self.provider_attempts.setdefault(attempt.event_id, []).append(attempt)

    def acknowledged(self, event_id: str) -> bool:
        """Whether the provider recorded a 2xx answer for any attempt of the event."""
        return any(attempt.acknowledged for attempt in self.provider_attempts.get(event_id, ()))

    def observed(self, payment_id: str, at_ms: int) -> None:
        self.observed_at_ms.setdefault(payment_id, []).append(at_ms)

    def accepted(self, payment_id: str, status: PaymentStatus, at_ms: int) -> None:
        self.accepted_statuses.setdefault(payment_id, set()).add(status)
        self.observed(payment_id, at_ms)

    def fulfilled(self, order_id: str, at_ms: int) -> None:
        self.fulfilled_at_ms.setdefault(order_id, at_ms)

    def last_observed_at(self, payment_id: str, as_of_ms: int) -> int | None:
        return max((t for t in self.observed_at_ms.get(payment_id, ()) if t <= as_of_ms), default=None)


@dataclass(frozen=True)
class SeedSummary:
    run_id: str
    scenario: str
    seed: int
    split: str
    start_ms: int
    cutoff_ms: int
    counts: dict[str, int]
    incident_ids: tuple[str, ...]
    observable_sha256: str
    oracle_path: str | None
    oracle_sha256: str | None
    provider_records_path: str | None = None

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id, "scenario": self.scenario, "seed": self.seed, "split": self.split,
            "start_ms": self.start_ms, "cutoff_ms": self.cutoff_ms, "counts": dict(self.counts),
            "incident_ids": list(self.incident_ids), "observable_sha256": self.observable_sha256,
            "oracle_path": self.oracle_path, "oracle_sha256": self.oracle_sha256,
            "provider_records_path": self.provider_records_path,
        }


def seed_world(
    conn: sqlite3.Connection,
    world: World,
    *,
    oracle_dir: Path | None,
    model_mode: ModelMode = ModelMode.RULES,
    incident_settings: incidents.IncidentSettings = incidents.DEFAULT_INCIDENT_SETTINGS,
    provider_dir: Path | None = None,
    stale_after_ms: int | None = None,
) -> SeedSummary:
    """Create the run, replay the world up to its cutoff and write the oracle outside the database.

    Provider records (no labels) go to ``provider_dir``, by default ``provider/`` next to
    ``oracle_dir``, with the provider's webhook delivery log beside them. ``stale_after_ms``
    (default: settings) is the freshness threshold the oracle applies to reprocessing
    eligibility, the same one the planner uses.
    """
    return replay_world(
        conn, world, oracle_dir=oracle_dir, model_mode=model_mode, incident_settings=incident_settings,
        provider_dir=provider_dir, stale_after_ms=stale_after_ms,
    ).summary


def replay_world(
    conn: sqlite3.Connection,
    world: World,
    *,
    oracle_dir: Path | None,
    model_mode: ModelMode = ModelMode.RULES,
    incident_settings: incidents.IncidentSettings = incidents.DEFAULT_INCIDENT_SETTINGS,
    provider_dir: Path | None = None,
    stale_after_ms: int | None = None,
) -> ReplayedWorld:
    """``seed_world``, keeping the simulated provider and merchant attached for explicit steps after the cutoff."""
    storage.migrate_simulator(conn)
    if storage.get_run(conn, world.run_id) is not None:
        raise ValueError(f"run {world.run_id!r} already exists in this database; use another seed or data directory")
    threshold = stale_after_ms if stale_after_ms is not None else settings.load_settings().stale_after_seconds * 1_000
    _create_run(conn, world, model_mode)
    replay = _Replay(conn, world, incident_settings)
    replay.run()
    oracle = (
        write_oracle(conn, world, oracle_dir, log=replay.log, stale_after_ms=threshold)
        if oracle_dir is not None else None
    )
    records_dir = provider_dir
    if records_dir is None and oracle_dir:
        records_dir = oracle_dir.parent / "provider"
    provider_path = write_provider_records(world.run_id, world.attempts, records_dir) if records_dir else None
    if records_dir:  # what a refresh may read of the provider's deliveries (Q29 (b)); no observable table changes
        write_provider_delivery_log(world.run_id, INTEGRATION_ID, replay.provider_deliveries(), records_dir)
    summary = SeedSummary(
        run_id=world.run_id,
        scenario=world.spec.name,
        seed=world.seed,
        split=world.spec.split,
        start_ms=world.start_ms,
        cutoff_ms=world.cutoff_ms,
        counts=run_counts(conn, world.run_id),
        incident_ids=tuple(item.incident_id for item in storage.list_incidents(conn, run_id=world.run_id)),
        observable_sha256=oracle.observable_sha256 if oracle else observable_dataset_hash(conn, world.run_id),
        oracle_path=str(oracle.path) if oracle else None,
        oracle_sha256=oracle.oracle_sha256 if oracle else None,
        provider_records_path=str(provider_path) if provider_path else None,
    )
    return ReplayedWorld(replay, summary)


def _create_run(conn: sqlite3.Connection, world: World, model_mode: ModelMode) -> None:
    with storage.write_transaction(conn):
        storage.create_run(
            conn,
            storage.RunRecord(
                run_id=world.run_id, seed=world.seed, scenario=world.spec.name, split=world.spec.split,
                merchant_id=MERCHANT_ID, generator_version=GENERATOR_VERSION, clock_ms=world.start_ms,
                model_mode=model_mode,
            ),
        )
        storage.add_integration(
            conn,
            storage.IntegrationRecord(
                run_id=world.run_id, integration_id=INTEGRATION_ID,
                secret_ref=merchant.compose().secret_ref, max_body_bytes=MAX_BODY_BYTES,
            ),
        )
        storage.set_merchant_profile(conn, run_id=world.run_id, emits_categories=world.spec.emits_categories)
        # The provider truth current_secret reads, kept for the receiver fixture check (runtime.active_merchant).
        if world.signing_rotation_at_ms is not None:
            storage.record_provider_rotation(
                conn, run_id=world.run_id, secret_ref=ROTATED_SIGNING_REF, from_ms=world.signing_rotation_at_ms
            )


# One fixed statement per table, built once from storage's own table list: no runtime value reaches SQL text.
_ROW_COUNT_SQL: Mapping[str, str] = MappingProxyType(
    {table: f"SELECT COUNT(*) FROM {table} WHERE run_id = ?" for table in storage.TABLES if table != "runs"}
)


def run_counts(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """Row counts per observable table, plus delivery and worker outcomes."""
    counts = {table: int(conn.execute(query, (run_id,)).fetchone()[0]) for table, query in _ROW_COUNT_SQL.items()}
    for outcome, number in conn.execute(
        "SELECT outcome, COUNT(*) FROM delivery_attempts WHERE run_id = ? GROUP BY outcome ORDER BY outcome", (run_id,)
    ).fetchall():
        counts[f"delivery_{outcome}"] = int(number)
    for result, number in conn.execute(
        "SELECT result, COUNT(*) FROM worker_attempts WHERE run_id = ? GROUP BY result ORDER BY result", (run_id,)
    ).fetchall():
        counts[f"worker_{result}"] = int(number)
    return counts


class _Replay:
    def __init__(self, conn: sqlite3.Connection, world: World, incident_settings: incidents.IncidentSettings) -> None:
        self.conn = conn
        self.world = world
        self.spec = world.spec
        self.run_id = world.run_id
        self.ledger: ProviderLedger = world.ledger
        self.incident_settings = incident_settings
        self.clock = world.start_ms
        self.queue: list[tuple[int, int, int, Callable[[int], None]]] = []
        self.sequence = itertools.count()
        self.log_numbers = itertools.count(1)
        self.last_snapshot_at: dict[str, int] = {}
        self.snapshot_counts: dict[str, int] = {}
        self.log = SimulationLog()
        self.event_orders = {plan.event_id: plan.order_id for plan in world.deliveries}
        self.originals = {plan.event_id: plan for plan in world.deliveries if plan.copy == "original"}
        self.first_sent_at: dict[str, int] = {}
        for plan in world.deliveries:
            earlier = self.first_sent_at.get(plan.event_id, plan.send_at_ms)
            self.first_sent_at[plan.event_id] = min(plan.send_at_ms, earlier)

    # ------------------------------------------------------------------ scheduling

    def schedule(self, at_ms: int, priority: int, action: Callable[[int], None]) -> None:
        if at_ms <= self.world.cutoff_ms:
            heapq.heappush(self.queue, (at_ms, priority, next(self.sequence), action))

    def run(self) -> None:
        world, spec = self.world, self.spec
        for deployment in world.deployments:
            self.schedule(deployment.deployed_at_ms, _DEPLOY, partial(self.deploy, deployment))
        for order in world.orders:
            self.schedule(order.created_at_ms, _ORDER, partial(self.insert_order, order))
        for update in world.order_updates:
            self.schedule(update.at_ms, _ORDER_UPDATE, partial(self.update_order, update))
        for attempt in world.attempts:
            self.schedule(attempt.created_at_ms, _ATTEMPT, partial(self.insert_attempt, attempt.payment_id))
        for delivery in world.deliveries:
            self.schedule(delivery.send_at_ms, _DELIVERY, partial(self.deliver, delivery, 1))
        for log in world.logs:
            self.schedule(log.occurred_at_ms, _LOG, partial(self.write_log, log))
        self.schedule(world.start_ms + spec.worker_tick_seconds * 1_000, _WORKER, self.worker_tick)
        self.schedule(world.start_ms + spec.reconcile_interval_minutes * MINUTE_MS, _RECONCILE, self.reconcile)
        self.schedule(world.start_ms + spec.detector_interval_minutes * MINUTE_MS, _DETECT, self.detect)
        while self.queue:
            at_ms, _, _, action = heapq.heappop(self.queue)
            self.advance(at_ms)
            action(at_ms)
        self.advance(world.cutoff_ms)
        incidents.detect_incidents(self.conn, self.run_id, world.cutoff_ms, self.incident_settings)

    def advance(self, at_ms: int) -> None:
        """Move the run's replay clock forward before anything happens at ``at_ms``."""
        if at_ms <= self.clock:
            return
        with storage.write_transaction(self.conn):
            storage.advance_clock(self.conn, self.run_id, at_ms)
        self.clock = at_ms

    def step_to(self, at_ms: int) -> None:
        """Move the clock to ``at_ms`` for one explicit step after the cutoff; a step is never back-dated."""
        if isinstance(at_ms, bool) or not isinstance(at_ms, int):
            raise ContractError(f"a step's time must be an integer of replay milliseconds, got {at_ms!r}")
        clock = storage.require_run(self.conn, self.run_id).clock_ms
        if at_ms < clock:
            raise ContractError(
                f"replay time {at_ms} is before run {self.run_id!r}'s clock {clock}; pick a time at or after the clock"
            )
        self.clock = clock  # an operator step or a provider refresh may have moved it since the replay ended
        self.advance(at_ms)

    # ------------------------------------------------------------------ merchant and provider events

    def insert_order(self, plan: OrderPlan, at_ms: int) -> None:
        with storage.write_transaction(self.conn):
            storage.insert_order(self.conn, storage.OrderRecord(
                run_id=self.run_id, order_id=plan.order_id, merchant_id=MERCHANT_ID, created_at_ms=plan.created_at_ms,
                amount_paise=plan.amount_paise, currency=plan.currency, allowed_methods=METHODS,
            ))

    def insert_attempt(self, payment_id: str, at_ms: int) -> None:
        attempt = self.ledger.attempt(payment_id)
        with storage.write_transaction(self.conn):
            storage.insert_payment_attempt(self.conn, storage.PaymentAttemptRecord(
                run_id=self.run_id, payment_id=attempt.payment_id, order_id=attempt.order_id, method=attempt.method,
                created_at_ms=attempt.created_at_ms,
            ))

    def update_order(self, update: OrderUpdate, at_ms: int) -> None:
        with storage.write_transaction(self.conn):
            if update.action == "cancel":
                storage.cancel_order(self.conn, run_id=self.run_id, order_id=update.order_id, cancelled_at_ms=at_ms)
            else:
                storage.mark_order_refunded(self.conn, run_id=self.run_id, order_id=update.order_id)

    def deploy(self, plan: DeploymentPlan, at_ms: int) -> None:
        record = runtime.record_deployment(
            self.conn, run_id=self.run_id, deploy_id=plan.deploy_id, variant=plan.variant,
            deployed_at_ms=plan.deployed_at_ms, summary=plan.summary,
        )
        self.write_lines(at_ms, [("deploy_pipeline", "info",
                                  f"deploy {record.deploy_id} rolled out to {record.service_id}: {record.summary}")])

    def deliver(self, plan: DeliveryPlan, attempt_no: int, at_ms: int) -> None:
        """One provider delivery attempt; anything but a 2xx the provider saw is retried with backoff, like a provider.

        Without a LostDeliveries, AckBeforeCommit or SecretRotation overlay this is the delivery every world had before
        them.
        """
        secret_ref = self.signing_secret(plan.event_id)
        lost = plan.lost_first_attempt if attempt_no == 1 else None
        if lost == "request":  # the request never reached the receiver, so the merchant has no record of it
            self.log.attempted(ProviderAttempt(plan.event_id, plan.copy, "automatic", at_ms, secret_ref, None))
            self.retry(plan, attempt_no, at_ms)
            return
        if lost == "commit":
            # The broken receiver answered at once and crashed before its inbox commit. The provider saw a 2xx, so it
            # records the event as delivered and stops; the merchant keeps no record of the attempt at all.
            self.log.attempted(
                ProviderAttempt(plan.event_id, plan.copy, "automatic", at_ms, secret_ref, UNSTORED_ACK_STATUS)
            )
            return
        response = self.send(plan.event_id, plan.raw_body, secret_ref, at_ms)
        if response.outcome is ReceiveOutcome.ACCEPTED:
            self.log.accepted(plan.payment_id, plan.status, at_ms)
        # A lost ACK: the receiver committed and answered, but the provider timed out before the answer arrived.
        seen_status = None if lost == "ack" else response.http_status
        self.log.attempted(ProviderAttempt(plan.event_id, plan.copy, "automatic", at_ms, secret_ref, seen_status))
        if response.acknowledged and lost is None:
            return
        if not response.acknowledged:
            self.write_lines(at_ms, [_rejection_line(response)])
        self.retry(plan, attempt_no, at_ms)

    def retry(self, plan: DeliveryPlan, attempt_no: int, at_ms: int) -> None:
        if attempt_no < MAX_DELIVERY_ATTEMPTS:
            self.schedule(at_ms + RETRY_BACKOFF_MS[attempt_no - 1], _DELIVERY, partial(self.deliver, plan, attempt_no + 1))

    def send(self, event_id: str, raw_body: bytes, secret_ref: str, at_ms: int) -> ingestion.DeliveryResponse:
        """One request to the merchant's receiver deployed at ``at_ms``, signed over the bytes the provider sends."""
        receiver = runtime.active_merchant(self.conn, run_id=self.run_id, at_ms=at_ms)
        headers = {
            signing.EVENT_ID_HEADER: event_id,
            signing.SIGNATURE_HEADER: signing.sign_body(raw_body, secret_ref),
        }
        return ingestion.receive_webhook(
            self.conn, run_id=self.run_id, integration_id=INTEGRATION_ID, headers=headers,
            raw_body=receiver.body_for_verification(raw_body), received_at_ms=at_ms,
        )

    def signing_secret(self, event_id: str) -> str:
        """The secret every automatic delivery of the event carries: the one current when it was first attempted."""
        return self.current_secret(self.first_sent_at[event_id])

    def current_secret(self, at_ms: int) -> str:
        rotation_at = self.world.signing_rotation_at_ms
        if rotation_at is None or at_ms < rotation_at:
            return merchant.PROVIDER_SIGNING_REF
        return ROTATED_SIGNING_REF

    def provider_deliveries(self) -> list[LoggedDelivery]:
        """The provider's delivery log as it stands: each event's attempts numbered from 1 in the order it made them."""
        subjects = {plan.event_id: (plan.payment_id, plan.status) for plan in self.world.deliveries}
        logged = []
        for event_id, attempts in self.log.provider_attempts.items():
            payment_id, status = subjects[event_id]
            logged.extend(
                LoggedDelivery(f"{event_id}:{number}", event_id, signing.EVENT_TYPE_BY_STATUS[status], payment_id,
                               attempt.at_ms, attempt.http_status, attempt.trigger)
                for number, attempt in enumerate(attempts, start=1)
            )
        return sorted(logged, key=lambda item: (item.attempted_at_ms, item.attempt_id))

    def original_delivery(self, event_id: str) -> DeliveryPlan:
        found = self.originals.get(event_id)
        if found is None:
            raise ContractError(
                f"the provider sent no webhook event {event_id!r} in run {self.run_id!r}; "
                "only an event it sent can be resent"
            )
        return found

    def worker_tick(self, at_ms: int) -> None:
        live = runtime.active_merchant(self.conn, run_id=self.run_id, at_ms=at_ms)
        steps = worker.run_worker(self.conn, run_id=self.run_id, handler=live, now_ms=at_ms,
                                  retry_backoff_ms=worker.DEFAULT_RETRY_BACKOFF_MS)
        for step in steps:
            if step.result is WorkerResult.FULFILLED:
                self.log.fulfilled(self.event_orders[step.event_id], at_ms)
        lines = [self._worker_line(step) for step in steps if step.result.value in ("failed", "manual_review")]
        self.write_lines(at_ms, [line for line in lines if line is not None])
        self.schedule(at_ms + self.spec.worker_tick_seconds * 1_000, _WORKER, self.worker_tick)

    def _worker_line(self, step: worker.WorkerStep) -> tuple[str, str, str] | None:
        if step.review_reasons:
            return ("order_worker", "warning",
                    f"event {step.event_id} held for review ({', '.join(step.review_reasons)})")
        row = storage.get_inbox_row(self.conn, run_id=self.run_id, inbox_id=step.inbox_id)
        summary = row.last_error if row is not None else None
        category = f" [category={step.exception_category}]" if step.exception_category else ""
        return ("order_worker", "error", f"event {step.event_id} attempt {step.attempts} not completed{category}: {summary}")

    def reconcile(self, at_ms: int) -> None:
        """Fetch provider snapshots for pending payments and for captured orders not yet processed."""
        pending_every = self.spec.reconcile_interval_minutes * MINUTE_MS
        if not self._reconciler_paused(at_ms):
            self.record_snapshots(self._take_snapshots(at_ms, pending_every), at_ms)
        self.schedule(at_ms + pending_every, _RECONCILE, self.reconcile)

    def _reconciler_paused(self, at_ms: int) -> bool:
        pause = self.spec.reconciler_pause
        if pause is None or at_ms < self._fraction_at(pause.start_fraction):
            return False
        return pause.end_fraction >= 1.0 or at_ms < self._fraction_at(pause.end_fraction)  # "to the end" includes the cutoff

    def _take_snapshots(self, at_ms: int, pending_every: int) -> list[PaymentObservation]:
        """Snapshots due at ``at_ms``, at most ``MAX_SNAPSHOTS_PER_TICK``, in attempt order."""
        seen = storage.observed_statuses(self.conn, run_id=self.run_id, as_of_ms=at_ms)
        fulfilments = storage.list_fulfilments(self.conn, run_id=self.run_id)
        fulfilled = {item.order_id for item in fulfilments if item.fulfilled_at_ms <= at_ms}
        captured_every = self.spec.refresh_captured_minutes * MINUTE_MS
        observations: list[PaymentObservation] = []
        for attempt in self.world.attempts:
            if len(observations) >= MAX_SNAPSHOTS_PER_TICK or attempt.created_at_ms > at_ms - SNAPSHOT_MIN_AGE_MS:
                continue
            interval = _snapshot_interval(attempt, seen.get(attempt.payment_id, frozenset()), fulfilled,
                                          pending_every=pending_every, captured_every=captured_every)
            if interval is None or not self._snapshot_due(attempt.payment_id, at_ms, interval):
                continue
            snapshot = self.ledger.snapshot(attempt.payment_id, at_ms)
            if snapshot is None:
                continue
            self.last_snapshot_at[attempt.payment_id] = at_ms
            self.snapshot_counts[attempt.payment_id] = self.snapshot_counts.get(attempt.payment_id, 0) + 1
            observations.append(_snapshot_observation(self.run_id, snapshot, at_ms))
        return observations

    def _snapshot_due(self, payment_id: str, at_ms: int, interval: int) -> bool:
        last = self.last_snapshot_at.get(payment_id)
        if last is not None and at_ms - last < interval:
            return False
        # The reconciler dropped a stale payment after its first fetch.
        return not (payment_id in self.world.stale_payment_ids and self.snapshot_counts.get(payment_id, 0))

    def record_snapshots(self, observations: list[PaymentObservation], at_ms: int) -> None:
        if not observations:
            return
        with storage.write_transaction(self.conn):
            for observation in observations:
                storage.insert_observation(self.conn, observation)
        for observation in observations:
            self.log.observed(observation.payment_id, at_ms)

    def _fraction_at(self, fraction: float) -> int:
        return self.world.start_ms + int(fraction * (self.world.cutoff_ms - self.world.start_ms))

    def detect(self, at_ms: int) -> None:
        incidents.detect_incidents(self.conn, self.run_id, at_ms, self.incident_settings)
        self.schedule(at_ms + self.spec.detector_interval_minutes * MINUTE_MS, _DETECT, self.detect)

    # ------------------------------------------------------------------ logs

    def write_log(self, plan: LogPlan, at_ms: int) -> None:
        self.write_lines(at_ms, [(plan.service_id, plan.level, plan.message)], log_ids=[plan.log_id])

    def write_lines(self, at_ms: int, lines: list[tuple[str, str, str]], log_ids: list[str] | None = None) -> None:
        if not lines:
            return
        ids = log_ids or [f"log_run_{at_ms}_{next(self.log_numbers):05d}" for _ in lines]
        with storage.write_transaction(self.conn):
            for log_id, (service_id, level, message) in zip(ids, lines, strict=True):
                storage.insert_merchant_log(self.conn, storage.MerchantLogRecord(
                    run_id=self.run_id, log_id=log_id, service_id=service_id, level=level,  # type: ignore[arg-type]
                    message=message, occurred_at_ms=at_ms, known_at_ms=at_ms,
                ))


def _snapshot_interval(
    attempt: AttemptTruth, statuses: frozenset[str], fulfilled: set[str], *, pending_every: int, captured_every: int
) -> int | None:
    """How often the reconciler re-fetches a payment: until it is final, or while its capture waits unprocessed."""
    if not statuses & _FINAL_EVIDENCE:
        return pending_every
    if "captured" in statuses and "refunded" not in statuses and attempt.order_id not in fulfilled:
        return captured_every
    return None


def _snapshot_observation(run_id: str, snapshot: ProviderSnapshot, at_ms: int) -> PaymentObservation:
    return PaymentObservation(
        run_id=run_id, observation_id=f"obs_snap_{snapshot.payment_id}_{at_ms}",
        payment_id=snapshot.payment_id, order_id=snapshot.order_id, status=snapshot.status,
        amount_paise=snapshot.amount_paise, currency=snapshot.currency, method=snapshot.method,
        source=ObservationSource.SNAPSHOT, source_ref=f"snap_{snapshot.payment_id}_{at_ms}",
        occurred_at_ms=at_ms, known_at_ms=at_ms, amount_refunded_paise=snapshot.amount_refunded_paise,
        error_code=snapshot.error_code, error_source=snapshot.error_source, error_step=snapshot.error_step,
        error_reason=snapshot.error_reason,
    )


def _rejection_line(response: ingestion.DeliveryResponse) -> tuple[str, str, str]:
    """The receiver's own log line for a delivery it did not acknowledge; it cannot tell who will retry."""
    return ("webhook_receiver", "warning",
            f"webhook delivery answered HTTP {response.http_status} ({response.outcome.value}); sender will retry")


# --------------------------------------------------------------------------- explicit steps after the cutoff


@dataclass(frozen=True)
class ProviderRefund:
    """A provider event: the provider refunds a captured payment in full at ``at_ms``.

    The merchant takes no refund webhooks (``world._WEBHOOK_STATUSES``), so the refund becomes observable as the
    reconciler would see it: one provider state fetch of the payment at ``at_ms``.
    """

    payment_id: str
    at_ms: int


# A merchant event (an order cancelled or marked refunded by the shop) or a provider event.
ScriptedEvent = OrderUpdate | ProviderRefund


class ManualReplayRefusal(StrEnum):
    ALREADY_ACKNOWLEDGED = "already_acknowledged"  # the provider recorded a 2xx answer for the event
    OUTSIDE_REPLAY_WINDOW = "outside_replay_window"  # created more than MANUAL_REPLAY_WINDOW_MS before the resend


@dataclass(frozen=True)
class ManualReplay:
    """One resend from the provider's dashboard: refused with nothing sent, or sent once and never retried."""

    event_id: str
    at_ms: int
    refusal: ManualReplayRefusal | None
    secret_ref: str | None  # the secret the resend was signed with; None when refused
    response: ingestion.DeliveryResponse | None  # the receiver's answer; None when refused

    @property
    def sent(self) -> bool:
        return self.refusal is None


class ReplayedWorld:
    """A world replayed to its cutoff by ``replay_world``, with its simulated provider and merchant still attached.

    Nothing runs on its own after the cutoff: no worker ticks, reconciler fetches, detectors or automatic retries.
    Each method below is one explicit step at a replay time no earlier than the run's clock, which it advances.
    """

    def __init__(self, replay: _Replay, summary: SeedSummary) -> None:
        self._replay = replay
        self._summary = summary

    @property
    def summary(self) -> SeedSummary:
        return self._summary

    @property
    def world(self) -> World:
        return self._replay.world

    @property
    def provider_ledger(self) -> ProviderLedger:
        """Provider truth including scripted refunds; the provider records file written at seeding has none of them."""
        return self._replay.ledger

    def provider_attempts(self, event_id: str) -> tuple[ProviderAttempt, ...]:
        """The provider's record of every attempt to deliver the event, automatic and manual, in time order."""
        return tuple(self._replay.log.provider_attempts.get(event_id, ()))

    def provider_delivery_log(self) -> SimulatedDeliveryLog:
        """The provider's delivery log now, dashboard resends included; the file written at seeding has none of them."""
        return SimulatedDeliveryLog(INTEGRATION_ID, self._replay.provider_deliveries())

    def apply_scripted_event(self, event: ScriptedEvent) -> None:
        """Apply one merchant or provider event after the cutoff through the replay's own code paths.

        A merchant ``OrderUpdate`` cancels the order or marks it refunded, as during the replay. A ``ProviderRefund``
        adds the refund to the provider's truth, which refuses a refund of a payment that is not captured by then,
        and records the provider state fetch that sees it.
        """
        replay = self._replay
        if isinstance(event, OrderUpdate):
            storage.require_order(replay.conn, run_id=replay.run_id, order_id=event.order_id)
            replay.step_to(event.at_ms)
            replay.update_order(event, event.at_ms)
            return
        if not isinstance(event, ProviderRefund):
            raise ContractError(f"a scripted event is an OrderUpdate or a ProviderRefund, got {type(event).__name__}")
        refunded = _refunded(replay.ledger.attempt(event.payment_id), event.at_ms)
        replay.step_to(event.at_ms)
        replay.ledger = ProviderLedger(
            refunded if item.payment_id == refunded.payment_id else item for item in replay.ledger
        )
        snapshot = replay.ledger.snapshot(event.payment_id, event.at_ms)
        if snapshot is None:  # the payment exists by the refund's time, so the provider always has a state for it
            raise ContractError(f"the provider has no state for payment {event.payment_id!r} at {event.at_ms}")
        replay.record_snapshots([_snapshot_observation(replay.run_id, snapshot, event.at_ms)], event.at_ms)

    def manual_replay(self, event_id: str, *, at_ms: int) -> ManualReplay:
        """Resend one event from the provider's dashboard at ``at_ms``: its original bytes, signed anew.

        Refused, with nothing sent, when the provider already recorded a 2xx answer for the event or when the event
        was created more than ``MANUAL_REPLAY_WINDOW_MS`` earlier. Otherwise sent once, signed with the secret current
        at ``at_ms``, through the receiver deployed then. A refused or failed resend is never retried on its own.
        """
        replay = self._replay
        original = replay.original_delivery(event_id)
        replay.step_to(at_ms)
        if replay.log.acknowledged(event_id):
            return ManualReplay(event_id, at_ms, ManualReplayRefusal.ALREADY_ACKNOWLEDGED, None, None)
        if at_ms - ingestion.parse_payment_event(original.raw_body).occurred_at_ms > MANUAL_REPLAY_WINDOW_MS:
            return ManualReplay(event_id, at_ms, ManualReplayRefusal.OUTSIDE_REPLAY_WINDOW, None, None)
        secret_ref = replay.current_secret(at_ms)
        response = replay.send(event_id, original.raw_body, secret_ref, at_ms)
        if response.outcome is ReceiveOutcome.ACCEPTED:
            replay.log.accepted(original.payment_id, original.status, at_ms)
        replay.log.attempted(
            ProviderAttempt(event_id, original.copy, "manual", at_ms, secret_ref, response.http_status)
        )
        if not response.acknowledged:
            replay.write_lines(at_ms, [_rejection_line(response)])
        return ManualReplay(event_id, at_ms, None, secret_ref, response)


def _refunded(attempt: AttemptTruth, at_ms: int) -> AttemptTruth:
    """The attempt with a full refund at ``at_ms`` appended; AttemptTruth refuses one that does not follow a capture."""
    refund = Transition(PaymentStatus.REFUNDED, at_ms, amount_refunded_paise=attempt.amount_paise)
    return AttemptTruth(attempt.payment_id, attempt.order_id, attempt.method, attempt.created_at_ms,
                        attempt.amount_paise, attempt.currency, (*attempt.transitions, refund))
