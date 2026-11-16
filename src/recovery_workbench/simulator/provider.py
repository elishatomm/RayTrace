"""Latent provider truth: what actually happened to each payment attempt, and when.

This is simulator state, never evidence. The workbench learns about it only through
signed webhook deliveries and reconciler snapshots, and ``ProviderLedger.snapshot``
returns only what the provider knew at the requested time. A read-only refresh also reads
the provider's own records of a run: its payments (``SimulatedProviderAdapter``) and its
webhook delivery log (``SimulatedDeliveryLog``, owner decision Q29 (b)).
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from recovery_workbench import refresh
from recovery_workbench.contracts import ContractError, PaymentStatus

TRANSITION_STATUSES = frozenset(
    {PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.FAILED, PaymentStatus.REFUNDED}
)
# The transitions a provider state machine allows; anything else is a generator error.
_ALLOWED_NEXT: dict[PaymentStatus | None, frozenset[PaymentStatus]] = {
    None: frozenset({PaymentStatus.AUTHORIZED, PaymentStatus.FAILED}),
    PaymentStatus.FAILED: frozenset({PaymentStatus.AUTHORIZED}),  # late authorisation
    PaymentStatus.AUTHORIZED: frozenset({PaymentStatus.CAPTURED}),
    PaymentStatus.CAPTURED: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.REFUNDED: frozenset(),
}


@dataclass(frozen=True)
class Transition:
    status: PaymentStatus
    occurred_at_ms: int
    amount_refunded_paise: int = 0
    error_code: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None


@dataclass(frozen=True)
class AttemptTruth:
    """One payment attempt as the provider really processed it."""

    payment_id: str
    order_id: str
    method: str
    created_at_ms: int
    amount_paise: int
    currency: str
    transitions: tuple[Transition, ...]

    def __post_init__(self) -> None:
        if self.amount_paise <= 0:
            raise ContractError(f"attempt {self.payment_id}: amount must be positive paise, got {self.amount_paise}")
        previous_status: PaymentStatus | None = None
        previous_time = self.created_at_ms
        for transition in self.transitions:
            if transition.status not in _ALLOWED_NEXT[previous_status]:
                raise ContractError(
                    f"attempt {self.payment_id}: {transition.status.value} cannot follow "
                    f"{previous_status.value if previous_status else 'creation'}"
                )
            if transition.occurred_at_ms <= previous_time:
                raise ContractError(f"attempt {self.payment_id}: transitions must move strictly forward in time")
            previous_status, previous_time = transition.status, transition.occurred_at_ms

    def transitions_by(self, as_of_ms: int) -> tuple[Transition, ...]:
        return tuple(transition for transition in self.transitions if transition.occurred_at_ms <= as_of_ms)

    def status_at(self, as_of_ms: int) -> PaymentStatus | None:
        """Provider status at ``as_of_ms``; None before the attempt exists, CREATED before any outcome."""
        if as_of_ms < self.created_at_ms:
            return None
        known = self.transitions_by(as_of_ms)
        return known[-1].status if known else PaymentStatus.CREATED

    @property
    def ever_captured(self) -> bool:
        return any(transition.status is PaymentStatus.CAPTURED for transition in self.transitions)

    @property
    def late_authorized(self) -> bool:
        # A failure can only be followed by an authorisation, so both present means a late one.
        statuses = {transition.status for transition in self.transitions}
        return {PaymentStatus.FAILED, PaymentStatus.AUTHORIZED} <= statuses

    def captured_at_ms(self) -> int | None:
        return next((t.occurred_at_ms for t in self.transitions if t.status is PaymentStatus.CAPTURED), None)


@dataclass(frozen=True)
class ProviderSnapshot:
    """What a provider state fetch returns at ``observed_at_ms``; never anything later."""

    payment_id: str
    order_id: str
    method: str
    status: PaymentStatus
    amount_paise: int
    currency: str
    amount_refunded_paise: int
    error_code: str | None
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    observed_at_ms: int


class ProviderLedger:
    """Every attempt's latent truth, queryable as of a time."""

    def __init__(self, attempts: Iterable[AttemptTruth]) -> None:
        self._attempts: dict[str, AttemptTruth] = {}
        for attempt in attempts:
            if attempt.payment_id in self._attempts:
                raise ContractError(f"payment {attempt.payment_id} appears twice in the provider ledger")
            self._attempts[attempt.payment_id] = attempt

    def __iter__(self) -> Iterator[AttemptTruth]:
        return iter(self._attempts.values())

    def __len__(self) -> int:
        return len(self._attempts)

    def knows(self, payment_id: str) -> bool:
        return payment_id in self._attempts

    def attempt(self, payment_id: str) -> AttemptTruth:
        try:
            return self._attempts[payment_id]
        except KeyError as missing:
            raise ContractError(f"the provider has no payment {payment_id!r}") from missing

    def snapshot(self, payment_id: str, as_of_ms: int) -> ProviderSnapshot | None:
        """The provider's view of one payment at ``as_of_ms``; None if it did not exist yet."""
        attempt = self.attempt(payment_id)
        status = attempt.status_at(as_of_ms)
        if status is None:
            return None
        known = attempt.transitions_by(as_of_ms)
        refunded = max((t.amount_refunded_paise for t in known if t.status is PaymentStatus.REFUNDED), default=0)
        failure = known[-1] if known and known[-1].status is PaymentStatus.FAILED else None
        return ProviderSnapshot(
            payment_id=attempt.payment_id,
            order_id=attempt.order_id,
            method=attempt.method,
            status=status,
            amount_paise=attempt.amount_paise,
            currency=attempt.currency,
            amount_refunded_paise=refunded,
            error_code=failure.error_code if failure else None,
            error_source=failure.error_source if failure else None,
            error_step=failure.error_step if failure else None,
            error_reason=failure.error_reason if failure else None,
            observed_at_ms=as_of_ms,
        )


# --------------------------------------------------------------------------- provider records and adapter

PROVIDER_RECORDS_VERSION = 1


def write_provider_records(run_id: str, attempts: Iterable[AttemptTruth], directory: Path) -> Path:
    """The provider's own payment records for one run: no labels, no oracle content.

    Written to ``<directory>/<run_id>.json`` once; a refresh adapter reads them later.
    """
    payments = [
        {
            "payment_id": attempt.payment_id,
            "order_id": attempt.order_id,
            "method": attempt.method,
            "created_at_ms": attempt.created_at_ms,
            "amount_paise": attempt.amount_paise,
            "currency": attempt.currency,
            "transitions": [
                {
                    "status": t.status.value, "occurred_at_ms": t.occurred_at_ms,
                    "amount_refunded_paise": t.amount_refunded_paise, "error_code": t.error_code,
                    "error_source": t.error_source, "error_step": t.error_step, "error_reason": t.error_reason,
                }
                for t in attempt.transitions
            ],
        }
        for attempt in attempts
    ]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    document = {"schema_version": PROVIDER_RECORDS_VERSION, "run_id": run_id, "payments": payments}
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


def load_provider_ledger(path: Path) -> ProviderLedger:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != PROVIDER_RECORDS_VERSION:
        raise ContractError(f"{path} is not a version {PROVIDER_RECORDS_VERSION} provider records file")
    return ProviderLedger(
        AttemptTruth(
            payment_id=item["payment_id"], order_id=item["order_id"], method=item["method"],
            created_at_ms=item["created_at_ms"], amount_paise=item["amount_paise"], currency=item["currency"],
            transitions=tuple(
                Transition(
                    status=PaymentStatus(t["status"]), occurred_at_ms=t["occurred_at_ms"],
                    amount_refunded_paise=t["amount_refunded_paise"], error_code=t["error_code"],
                    error_source=t["error_source"], error_step=t["error_step"], error_reason=t["error_reason"],
                )
                for t in item["transitions"]
            ),
        )
        for item in document["payments"]
    )


class SimulatedProviderAdapter:
    """``refresh.ProviderAdapter`` over the simulated provider's records; never returns future state."""

    def __init__(self, ledger: ProviderLedger) -> None:
        self._ledger = ledger

    @classmethod
    def from_records(cls, path: Path) -> SimulatedProviderAdapter:
        return cls(load_provider_ledger(path))

    def snapshot_as_of(
        self, payment_ids: Sequence[str], as_of_ms: int
    ) -> dict[str, refresh.ProviderPaymentState | None]:
        states: dict[str, refresh.ProviderPaymentState | None] = {}
        for payment_id in payment_ids:
            snapshot = self._ledger.snapshot(payment_id, as_of_ms) if self._ledger.knows(payment_id) else None
            states[payment_id] = None if snapshot is None else refresh.ProviderPaymentState(
                payment_id=snapshot.payment_id, order_id=snapshot.order_id, method=snapshot.method,
                status=snapshot.status, amount_paise=snapshot.amount_paise, currency=snapshot.currency,
                amount_refunded_paise=snapshot.amount_refunded_paise, error_code=snapshot.error_code,
                error_source=snapshot.error_source, error_step=snapshot.error_step, error_reason=snapshot.error_reason,
                observed_at_ms=snapshot.observed_at_ms,
            )
        return states


# --------------------------------------------------------------------------- the provider's webhook delivery log

# Assumption, dated 2026-11-13, not checked against any provider's documentation (this sandbox makes no network calls):
# a gateway keeps each webhook event's delivery attempts, with the HTTP status it received or a timeout, and a merchant
# can read them through an API. Dashboards commonly show such a log; whether an API serves it varies by provider.
DELIVERY_LOG_VERSION = 1


@dataclass(frozen=True)
class LoggedDelivery:
    """One webhook delivery attempt in the provider's own log: the event it sent, when, and the HTTP status it saw."""

    attempt_id: str  # the provider numbers each event's attempts from 1: "<event_id>:<n>"
    event_id: str
    event_type: str
    payment_id: str
    attempted_at_ms: int
    http_status: int | None  # None: no answer reached the provider, which recorded a timeout
    triggered_by: Literal["automatic", "manual"]


def delivery_log_path(records: Path) -> Path:
    """The delivery log beside a run's provider records: ``<run_id>.json`` -> ``<run_id>.deliveries.json``."""
    return records.with_name(f"{records.stem}.deliveries.json")


def write_provider_delivery_log(
    run_id: str, integration_id: str, deliveries: Iterable[LoggedDelivery], directory: Path
) -> Path:
    """The provider's webhook delivery log of one run as it stood at the end of seeding: no labels, no oracle content.

    Written once, beside the provider records; a refresh reads it later. A dashboard resend made after seeding
    (``ReplayedWorld.manual_replay``) is not in it, as a scripted refund is not in the provider records.
    """
    document = {
        "schema_version": DELIVERY_LOG_VERSION,
        "run_id": run_id,
        "integration_id": integration_id,
        "attempts": [
            {
                "attempt_id": item.attempt_id, "event_id": item.event_id, "event_type": item.event_type,
                "payment_id": item.payment_id, "attempted_at_ms": item.attempted_at_ms,
                "http_status": item.http_status, "triggered_by": item.triggered_by,
            }
            for item in deliveries
        ],
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = delivery_log_path(directory / f"{run_id}.json")
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


class SimulatedDeliveryLog:
    """``refresh.ProviderDeliveryLog`` over the simulated provider's delivery log; never returns a future attempt."""

    def __init__(self, integration_id: str, deliveries: Iterable[LoggedDelivery]) -> None:
        self._integration_id = integration_id
        self._by_payment: dict[str, list[LoggedDelivery]] = {}
        for item in sorted(deliveries, key=lambda delivery: delivery.attempted_at_ms):
            self._by_payment.setdefault(item.payment_id, []).append(item)

    @classmethod
    def from_file(cls, path: Path) -> SimulatedDeliveryLog:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema_version") != DELIVERY_LOG_VERSION:
            raise ContractError(f"{path} is not a version {DELIVERY_LOG_VERSION} provider delivery log")
        return cls(document["integration_id"], (
            LoggedDelivery(
                attempt_id=item["attempt_id"], event_id=item["event_id"], event_type=item["event_type"],
                payment_id=item["payment_id"], attempted_at_ms=item["attempted_at_ms"],
                http_status=item["http_status"], triggered_by=item["triggered_by"],
            )
            for item in document["attempts"]
        ))

    def deliveries_as_of(
        self, payment_ids: Sequence[str], as_of_ms: int
    ) -> dict[str, list[refresh.ProviderDeliveryAttempt]]:
        return {
            payment_id: [
                refresh.ProviderDeliveryAttempt(
                    attempt_id=item.attempt_id, integration_id=self._integration_id, event_id=item.event_id,
                    event_type=item.event_type, payment_id=item.payment_id, attempted_at_ms=item.attempted_at_ms,
                    http_status=item.http_status, triggered_by=item.triggered_by,
                )
                for item in self._by_payment.get(payment_id, ())
                if item.attempted_at_ms <= as_of_ms
            ]
            for payment_id in payment_ids
        }
