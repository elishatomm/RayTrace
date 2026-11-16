"""Read-only provider refresh (SPEC.md §10 ``refresh_provider_state``).

Fetch the provider's current state for named payments and record it as snapshot
evidence known at ``now_ms``. Nothing else changes: no order, fulfilment or inbox row
is touched. The adapter call happens outside any write transaction, so the database is
never locked while waiting on a provider.

``record_delivery_log`` reads the provider's webhook delivery log the same way (owner decision Q29 (b)): each
delivery attempt the provider made for the named payments' events, with the HTTP status it saw, recorded as evidence
known at ``now_ms`` in ``provider_delivery_attempts``. It is the one record that shows a 2xx the receiver answered
for an event it never stored, which the provider will not resend.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from recovery_workbench import policy, storage
from recovery_workbench.contracts import (
    ContractError,
    CurrencyCode,
    EpochMs,
    NonNegativePaise,
    ObservationSource,
    Paise,
    PaymentObservation,
    PaymentStatus,
    ScopeError,
    StrictModel,
)


class ProviderPaymentState(StrictModel):
    """One payment as a provider reports it at ``observed_at_ms``."""

    payment_id: str
    order_id: str
    method: str | None
    status: PaymentStatus
    amount_paise: Paise
    currency: CurrencyCode
    amount_refunded_paise: NonNegativePaise = 0
    error_code: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    observed_at_ms: EpochMs


class ProviderAdapter(Protocol):
    """Read-only access to provider payment state; must never answer with state after ``as_of_ms``."""

    def snapshot_as_of(
        self, payment_ids: Sequence[str], as_of_ms: int
    ) -> Mapping[str, ProviderPaymentState | None]:
        """State per requested payment as of ``as_of_ms``; None when the provider does not know it yet."""
        ...


@dataclass(frozen=True)
class RefreshSummary:
    run_id: str
    now_ms: int
    refreshed: tuple[str, ...]
    already_recorded: tuple[str, ...]  # refreshed earlier at this same replay time
    unknown_at_provider: tuple[str, ...]
    statuses: dict[str, str]


def refresh_provider_state(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    payment_ids: Sequence[str],
    adapter: ProviderAdapter,
    now_ms: int,
) -> RefreshSummary:
    """Record provider state for ``payment_ids`` as snapshot observations known at ``now_ms``.

    Refuses payment IDs the run does not know (``ScopeError``) and a replay time before the
    run's clock (``ContractError``); moves the clock to ``now_ms`` with the inserts.
    """
    requested = sorted(set(payment_ids))
    if not requested:
        raise ContractError("name at least one payment to refresh")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ContractError(f"now_ms must be a non-negative integer of replay time, got {now_ms!r}")
    with storage.read_transaction(conn):
        run = storage.require_run(conn, run_id)
        known = storage.run_payment_ids(conn, run_id=run_id)
    outside = [payment_id for payment_id in requested if payment_id not in known]
    if outside:
        raise ScopeError(f"payments {outside} are not payments of run {run_id!r}; refresh only the run's own payments")
    if now_ms < run.clock_ms:
        raise ContractError(f"replay time {now_ms} is before run {run_id!r}'s clock {run.clock_ms}")
    states = adapter.snapshot_as_of(requested, now_ms)
    observations = _observations(run_id, requested, states, now_ms)
    with storage.write_transaction(conn):
        storage.advance_clock(conn, run_id, now_ms)
        recorded = storage.existing_observation_ids(
            conn, run_id=run_id, observation_ids=[item.observation_id for item in observations]
        )
        for observation in observations:
            if observation.observation_id not in recorded:
                storage.insert_observation(conn, observation)
    return RefreshSummary(
        run_id=run_id,
        now_ms=now_ms,
        refreshed=tuple(o.payment_id for o in observations if o.observation_id not in recorded),
        already_recorded=tuple(o.payment_id for o in observations if o.observation_id in recorded),
        unknown_at_provider=tuple(payment_id for payment_id in requested if states.get(payment_id) is None),
        statuses={o.payment_id: o.status.value for o in observations},
    )


def _observations(
    run_id: str, requested: list[str], states: Mapping[str, ProviderPaymentState | None], now_ms: int
) -> list[PaymentObservation]:
    unexpected = sorted(set(states) - set(requested))
    if unexpected:
        raise ContractError(f"the provider adapter answered for payments that were not requested: {unexpected}")
    observations = []
    for payment_id in requested:
        state = states.get(payment_id)
        if state is None:
            continue
        if state.payment_id != payment_id:
            raise ContractError(f"the provider adapter answered {state.payment_id!r} when asked for {payment_id!r}")
        if state.observed_at_ms > now_ms:
            raise ContractError(
                f"the provider adapter returned state observed at {state.observed_at_ms} for a refresh at {now_ms}; "
                "an adapter must never return future state"
            )
        observations.append(PaymentObservation(
            run_id=run_id, observation_id=f"obs_refresh_{payment_id}_{now_ms}", payment_id=payment_id,
            order_id=state.order_id, status=state.status, amount_paise=state.amount_paise, currency=state.currency,
            method=state.method, source=ObservationSource.SNAPSHOT, source_ref=f"refresh_{now_ms}",
            occurred_at_ms=state.observed_at_ms, known_at_ms=now_ms, amount_refunded_paise=state.amount_refunded_paise,
            error_code=state.error_code, error_source=state.error_source, error_step=state.error_step,
            error_reason=state.error_reason,
        ))
    return observations


# --------------------------------------------------------------------------- the provider's delivery log (Q29 (b))


class ProviderDeliveryAttempt(StrictModel):
    """One webhook delivery attempt as a provider's delivery log reports it."""

    attempt_id: storage.RecordId  # the provider's own ID for the attempt
    integration_id: storage.RecordId  # the integration it delivered to
    event_id: storage.RecordId
    event_type: str
    payment_id: storage.RecordId
    attempted_at_ms: EpochMs
    http_status: storage.HttpStatus | None  # None: the provider saw no answer and recorded a timeout
    triggered_by: Literal["automatic", "manual"]  # its own schedule, or a resend from its dashboard

    @property
    def acknowledged(self) -> bool:
        return self.http_status is not None and 200 <= self.http_status < 300


class ProviderDeliveryLog(Protocol):
    """Read-only access to a provider's webhook delivery log; must never answer with an attempt after ``as_of_ms``."""

    def deliveries_as_of(
        self, payment_ids: Sequence[str], as_of_ms: int
    ) -> Mapping[str, Sequence[ProviderDeliveryAttempt]]:
        """Each requested payment's delivery attempts made by ``as_of_ms``, for every event of the payment."""
        ...


@dataclass(frozen=True)
class DeliveryLogSummary:
    run_id: str
    now_ms: int
    payment_ids: tuple[str, ...]  # whose delivery log was read
    recorded: tuple[str, ...]  # provider attempt IDs this read stored
    already_recorded: tuple[str, ...]  # provider attempt IDs an earlier read stored
    # Payments whose payment.captured event the log shows answered 2xx: the provider will not send it again.
    acknowledged_captures: tuple[str, ...]


def record_delivery_log(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    payment_ids: Sequence[str],
    delivery_log: ProviderDeliveryLog,
    now_ms: int,
) -> DeliveryLogSummary:
    """Record the provider's delivery attempts for ``payment_ids`` as evidence known at ``now_ms``.

    The same rules as ``refresh_provider_state``: payment IDs the run does not know are refused (``ScopeError``), and so
    are a replay time before the run's clock and any answer that breaks the log's contract (``ContractError``), with
    nothing written. Moves the clock to ``now_ms`` with the inserts. An attempt an earlier read stored is not stored
    again; one the provider now reports differently is refused, because a delivery log only grows.
    """
    requested = sorted(set(payment_ids))
    if not requested:
        raise ContractError("name at least one payment whose delivery log to read")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ContractError(f"now_ms must be a non-negative integer of replay time, got {now_ms!r}")
    with storage.read_transaction(conn):
        run = storage.require_run(conn, run_id)
        known = storage.run_payment_ids(conn, run_id=run_id)
        integrations = {item.integration_id for item in storage.list_integrations(conn, run_id=run_id)}
    outside = [payment_id for payment_id in requested if payment_id not in known]
    if outside:
        raise ScopeError(f"payments {outside} are not payments of run {run_id!r}; read only the run's own payments")
    if now_ms < run.clock_ms:
        raise ContractError(f"replay time {now_ms} is before run {run_id!r}'s clock {run.clock_ms}")
    attempts = _logged_attempts(run_id, requested, delivery_log.deliveries_as_of(requested, now_ms), now_ms,
                                integrations)
    recorded: list[str] = []
    already: list[str] = []
    with storage.write_transaction(conn):
        storage.advance_clock(conn, run_id, now_ms)
        stored = storage.find_provider_delivery_attempts(
            conn, run_id=run_id, provider_attempt_ids=[attempt.attempt_id for attempt in attempts]
        )
        for attempt in attempts:
            earlier = stored.get((attempt.integration_id, attempt.attempt_id))
            if earlier is None:
                _insert_attempt(conn, run_id, attempt, now_ms)
                recorded.append(attempt.attempt_id)
                continue
            if _as_logged(earlier) != attempt:
                raise ContractError(
                    f"the provider's delivery log now reports attempt {attempt.attempt_id!r} of event "
                    f"{attempt.event_id!r} differently from the read at {earlier.known_at_ms}; a delivery log only "
                    "grows, so nothing was recorded"
                )
            already.append(attempt.attempt_id)
    return DeliveryLogSummary(
        run_id=run_id,
        now_ms=now_ms,
        payment_ids=tuple(requested),
        recorded=tuple(recorded),
        already_recorded=tuple(already),
        acknowledged_captures=tuple(sorted({
            attempt.payment_id for attempt in attempts
            if attempt.acknowledged and attempt.event_type == policy.CAPTURE_EVENT_TYPE
        })),
    )


def _logged_attempts(
    run_id: str, requested: list[str], answer: Mapping[str, Sequence[ProviderDeliveryAttempt]], now_ms: int,
    integrations: set[str],
) -> list[ProviderDeliveryAttempt]:
    unexpected = sorted(set(answer) - set(requested))
    if unexpected:
        raise ContractError(f"the provider's delivery log answered for payments that were not requested: {unexpected}")
    attempts: list[ProviderDeliveryAttempt] = []
    for payment_id in requested:
        for attempt in answer.get(payment_id, ()):
            if attempt.payment_id != payment_id:
                raise ContractError(f"the provider's delivery log listed attempt {attempt.attempt_id!r} of payment "
                                    f"{attempt.payment_id!r} when asked for {payment_id!r}")
            if attempt.attempted_at_ms > now_ms:
                raise ContractError(
                    f"the provider's delivery log returned attempt {attempt.attempt_id!r} made at "
                    f"{attempt.attempted_at_ms} for a read at {now_ms}; a delivery log must never return future attempts"
                )
            if attempt.integration_id not in integrations:
                raise ContractError(f"the provider's delivery log names integration {attempt.integration_id!r}, "
                                    f"which run {run_id!r} does not have")
            attempts.append(attempt)
    keys = [(attempt.integration_id, attempt.attempt_id) for attempt in attempts]
    if len(keys) != len(set(keys)):
        raise ContractError("the provider's delivery log listed one attempt ID twice")
    return attempts


def _insert_attempt(conn: sqlite3.Connection, run_id: str, attempt: ProviderDeliveryAttempt, now_ms: int) -> None:
    storage.insert_provider_delivery_attempt(
        conn, run_id=run_id, integration_id=attempt.integration_id, provider_attempt_id=attempt.attempt_id,
        event_id=attempt.event_id, event_type=attempt.event_type, payment_id=attempt.payment_id,
        attempted_at_ms=attempt.attempted_at_ms, provider_http_status=attempt.http_status,
        triggered_by=attempt.triggered_by, known_at_ms=now_ms, source_ref=f"delivery_log_{now_ms}",
    )


def _as_logged(record: storage.ProviderDeliveryAttemptRecord) -> ProviderDeliveryAttempt:
    return ProviderDeliveryAttempt(
        attempt_id=record.provider_attempt_id, integration_id=record.integration_id, event_id=record.event_id,
        event_type=record.event_type, payment_id=record.payment_id, attempted_at_ms=record.attempted_at_ms,
        http_status=record.provider_http_status, triggered_by=record.triggered_by,
    )
