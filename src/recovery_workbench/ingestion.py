"""Verified, durable webhook receipt (SPEC.md §6).

Checks run in this order, all before any business-state change:

1. size: a global ceiling, then the integration's own limit;
2. integration: resolved from the run's configuration, never from the payload;
3. HMAC-SHA256 over the unchanged raw bytes, compared in constant time, with the integration's
   current secret or, during a rotation, the next secret the deployed merchant config stages for
   it; never with any other;
4. the event-ID header: present once and well formed;
5. nesting depth, counted in one pass over the bytes before any parser reads them; then parse
   and validate the supported payload subset;
6. one ``BEGIN IMMEDIATE`` transaction: duplicate, conflicting reuse of an event
   ID, a body already stored or held as a conflict under another event ID (a
   duplicate too), or a new inbox row plus its payment observation.

A 2xx response is returned only after that transaction commits. Every delivery,
rejected or not, leaves one ``delivery_attempts`` row as diagnostic metadata. A delivery the
staged secret verified says so in that row's ``detail``; one with no such note was verified with
the integration's current secret, which is how every row before Q22 reads.

The signature covers the body, not the event-ID header. So the
same signed bytes under a new event ID are the provider's statement once more,
not a new one, and adding them again would only make old evidence look new.
Bytes held as a conflicting delivery are no different (Q34): accepted under a
fresh event ID, they would be processed while review still holds them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from recovery_workbench import storage
from recovery_workbench.contracts import (
    ContractError,
    ObservationSource,
    PaymentObservation,
    PaymentStatus,
    ReceiveOutcome,
    WorkbenchError,
    sha256_hex,
)
from recovery_workbench.simulator import runtime, signing

MAX_BODY_BYTES_CEILING = 1_048_576  # applied before the integration lookup, so huge bodies cost nothing
# How deep arrays and objects may nest in a body. The supported subset nests 4 deep (envelope, payload, payment,
# entity) and ignores fields providers add, so this leaves room for theirs. It also stays far below what readers
# of stored bodies survive: the merchant handler's copy.deepcopy gives up near 480 levels, json.loads near 10,000.
MAX_JSON_NESTING = 32

HTTP_STATUS_BY_OUTCOME: Mapping[ReceiveOutcome, int] = MappingProxyType(
    {
        ReceiveOutcome.ACCEPTED: 200,
        ReceiveOutcome.DUPLICATE: 200,
        ReceiveOutcome.CONFLICTING_ID: 200,  # acknowledged so the provider stops retrying; held for review
        ReceiveOutcome.REJECTED_SIGNATURE: 401,
        ReceiveOutcome.REJECTED_SCHEMA: 400,
        ReceiveOutcome.REJECTED_SIZE: 413,
        ReceiveOutcome.UNKNOWN_INTEGRATION: 404,
    }
)

# Fields compared when an event ID is reused with different bytes (SPEC.md §6 step 5).
BUSINESS_FIELDS: tuple[str, ...] = ("payment_id", "status", "amount", "currency", "order_id")

_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_HEX_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE_MISMATCH = "signature does not match the raw body"
STAGED_SECRET_NOTE = "verified with the staged next secret {secret_ref}"  # a secret's name, never the secret
_QUOTE, _BACKSLASH = ord('"'), ord("\\")
_OPENERS, _CLOSERS = frozenset(b"[{"), frozenset(b"]}")
_STATUS_BY_EVENT_TYPE: Mapping[str, str] = MappingProxyType(
    {event_type: status.value for status, event_type in signing.EVENT_TYPE_BY_STATUS.items()}
)


class ReceiptNotDurableError(WorkbenchError):
    """Storage failed before the receipt committed. Respond 5xx so the provider retries."""


@dataclass(frozen=True)
class DeliveryResponse:
    """What the receiver answers for one delivery, and where the event now lives."""

    outcome: ReceiveOutcome
    http_status: int
    inbox_id: int | None  # the logical event's inbox row (the original one for duplicates and conflicts), if any
    event_id: str | None  # None when the delivery could not be verified
    conflicting_delivery_id: int | None = None  # the held delivery, for a conflict or a resend of a held body

    @property
    def acknowledged(self) -> bool:
        return 200 <= self.http_status < 300


# --------------------------------------------------------------------------- supported payload subset

# At least one character that is not a dot. The UI opens an order's case at a URL path ending in its ID, where a
# browser reads . and .. as "this directory" and "its parent".
_Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]*[A-Za-z0-9_:\-][A-Za-z0-9_.:\-]*$")
]
# Upper bounds keep every integer inside SQLite's 64-bit range and reject absurd values.
_Amount = Annotated[int, Field(gt=0, le=10**12)]
_Seconds = Annotated[int, Field(ge=0, le=10**11)]


class _ProviderPayload(BaseModel):
    # Providers add fields over time, so unknown fields are ignored; the fields we use are
    # strict, so "120000" or 1200.0 is never coerced into paise.
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)


class PaymentEntity(_ProviderPayload):
    id: _Identifier
    entity: Literal["payment"]
    amount: _Amount
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    status: Literal["authorized", "captured", "failed", "refunded"]
    order_id: _Identifier
    method: Annotated[str, Field(max_length=64)] | None = None
    captured: bool
    amount_refunded: Annotated[int, Field(ge=0, le=10**12)] = 0
    error_code: Annotated[str, Field(max_length=128)] | None = None
    error_source: Annotated[str, Field(max_length=128)] | None = None
    error_step: Annotated[str, Field(max_length=128)] | None = None
    error_reason: Annotated[str, Field(max_length=256)] | None = None
    created_at: _Seconds  # when the payment was created, not when this transition happened


class _PaymentWrapper(_ProviderPayload):
    entity: PaymentEntity


class _EventPayload(_ProviderPayload):
    payment: _PaymentWrapper


class PaymentEvent(_ProviderPayload):
    """The supported subset of a provider-shaped payment event (EVALUATION.md §3.2)."""

    entity: Literal["event"]
    account_id: Annotated[str, Field(min_length=1, max_length=128)]
    event: Literal["payment.authorized", "payment.captured", "payment.failed", "payment.refunded"]
    contains: list[Literal["payment"]]
    payload: _EventPayload
    created_at: _Seconds
    x_synthetic_created_at_ms: Annotated[int, Field(ge=0, le=10**14)] | None = None

    @model_validator(mode="after")
    def _fields_agree(self) -> PaymentEvent:
        payment = self.payload.payment.entity
        if self.contains != ["payment"]:
            raise ValueError("contains must be exactly ['payment']")
        if payment.status != _STATUS_BY_EVENT_TYPE[self.event]:
            raise ValueError(f"event {self.event} carries a payment whose status is {payment.status}")
        if payment.captured != (payment.status in ("captured", "refunded")):
            raise ValueError("the captured flag contradicts the payment status")
        if payment.amount_refunded > payment.amount:
            raise ValueError("amount_refunded exceeds the payment amount")
        if payment.status == "refunded" and payment.amount_refunded == 0:
            raise ValueError("a refunded payment must report amount_refunded")
        ms = self.x_synthetic_created_at_ms
        if ms is not None and ms // 1000 != self.created_at:
            raise ValueError(f"{signing.MS_EXTENSION_FIELD} does not fall within the envelope's created_at second")
        return self

    @property
    def payment(self) -> PaymentEntity:
        return self.payload.payment.entity

    @property
    def occurred_at_ms(self) -> int:
        """The envelope time (SPEC.md §7); whole seconds when the ms extension is absent."""
        if self.x_synthetic_created_at_ms is not None:
            return self.x_synthetic_created_at_ms
        return self.created_at * 1000


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # Duplicate keys let two parsers read different values from the same signed bytes.
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate JSON keys {sorted({key for key in keys if keys.count(key) > 1})}")
    return dict(pairs)


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-standard JSON constant {name}")


def _summarise(error: ValidationError) -> str:
    # Locations and error types only: payload values never enter diagnostic text.
    parts = [f"{'.'.join(str(step) for step in item['loc']) or 'body'}: {item['type']}" for item in error.errors()]
    return "; ".join(parts[:5])


def nests_deeper_than(raw_body: bytes, limit: int) -> bool:
    """Whether arrays and objects nest more than ``limit`` deep; brackets inside strings do not count.

    One pass over the bytes and no recursion, so it is safe on a body no parser could read. JSON's
    brackets, quotes and backslashes are ASCII, and UTF-8 never uses an ASCII byte inside a longer
    character, so the bytes answer as the decoded text would. A body that is not JSON can mislead the
    count only after the point where the parser stops reading it anyway.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in raw_body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == _BACKSLASH:
                escaped = True
            elif byte == _QUOTE:
                in_string = False
        elif byte == _QUOTE:
            in_string = True
        elif byte in _OPENERS:
            depth += 1
            if depth > limit:
                return True
        elif byte in _CLOSERS:
            depth -= 1
    return False


def parse_payment_event(raw_body: bytes) -> PaymentEvent:
    """Decode and validate the supported subset; ``ContractError`` for anything else."""
    if nests_deeper_than(raw_body, MAX_JSON_NESTING):
        raise ContractError(f"webhook body nests deeper than {MAX_JSON_NESTING} levels of arrays and objects")
    try:
        text = raw_body.decode("utf-8")
        decoded = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as malformed:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ContractError(f"webhook body is not a valid UTF-8 JSON document: {malformed}") from malformed
    except RecursionError as too_deep:
        # Not a ValueError. The nesting check above keeps bodies this deep from the parser; this stays so
        # that a fault in that check is still a schema rejection, never a crash.
        raise ContractError("webhook body nests deeper than the JSON parser can read") from too_deep
    if not isinstance(decoded, dict):
        raise ContractError("webhook body must be a JSON object")
    try:
        return PaymentEvent.model_validate(decoded)
    except ValidationError as invalid:
        raise ContractError(f"webhook body is outside the supported subset: {_summarise(invalid)}") from invalid


def differing_business_fields(original: PaymentEvent, redelivered: PaymentEvent) -> tuple[str, ...]:
    """Which of payment_id/status/amount/currency/order_id differ between two bodies."""
    before = _business_view(original)
    after = _business_view(redelivered)
    return tuple(name for name in BUSINESS_FIELDS if before[name] != after[name])


def _business_view(event: PaymentEvent) -> dict[str, object]:
    payment = event.payment
    return {
        "payment_id": payment.id,
        "status": payment.status,
        "amount": payment.amount,
        "currency": payment.currency,
        "order_id": payment.order_id,
    }


# --------------------------------------------------------------------------- receiver


def receive_webhook(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    integration_id: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    received_at_ms: int,
) -> DeliveryResponse:
    """Verify and durably receive one delivery; see the module docstring for the order of checks.

    Raises ``ScopeError`` for an unknown run and ``ReceiptNotDurableError`` when the
    receipt transaction fails, in which case nothing was stored and nothing may be
    acknowledged.
    """
    started = time.perf_counter()
    if isinstance(received_at_ms, bool) or not isinstance(received_at_ms, int) or received_at_ms < 0:
        raise ContractError(f"received_at_ms must be a non-negative integer, got {received_at_ms!r}")
    if not isinstance(raw_body, bytes):
        raise ContractError("raw_body must be the unchanged request bytes")
    storage.require_run(conn, run_id)
    body_sha256 = sha256_hex(raw_body)

    def reject(outcome: ReceiveOutcome, detail: str, event_id: str | None = None) -> DeliveryResponse:
        return _record_rejection(
            conn, run_id=run_id, integration_id=integration_id, outcome=outcome, event_id=event_id,
            received_at_ms=received_at_ms, body_sha256=body_sha256, started=started, detail=detail,
        )

    if len(raw_body) > MAX_BODY_BYTES_CEILING:
        return reject(ReceiveOutcome.REJECTED_SIZE, f"body of {len(raw_body)} bytes exceeds the receiver ceiling")
    integration = storage.get_integration(conn, run_id=run_id, integration_id=integration_id)
    if integration is None:
        return reject(ReceiveOutcome.UNKNOWN_INTEGRATION, "integration is not configured for this run")
    if len(raw_body) > integration.max_body_bytes:
        return reject(ReceiveOutcome.REJECTED_SIZE, f"body of {len(raw_body)} bytes exceeds the integration limit")
    signature_problem = _signature_problem(headers, raw_body, integration.secret_ref)
    staged_ref = (_verifying_staged_secret(conn, integration, headers, raw_body, at_ms=received_at_ms)
                  if signature_problem == _SIGNATURE_MISMATCH else None)
    if signature_problem is not None and staged_ref is None:
        return reject(ReceiveOutcome.REJECTED_SIGNATURE, signature_problem)
    event_id = _single_header(headers, signing.EVENT_ID_HEADER)
    if event_id is None or not _EVENT_ID_PATTERN.fullmatch(event_id):
        return reject(ReceiveOutcome.REJECTED_SCHEMA, "event ID header missing, repeated or malformed")
    try:
        event = parse_payment_event(raw_body)
    except ContractError as unsupported:
        return reject(ReceiveOutcome.REJECTED_SCHEMA, str(unsupported), event_id=event_id)

    try:
        with storage.write_transaction(conn):
            return _commit_receipt(
                conn, run_id=run_id, integration_id=integration_id, event_id=event_id, event=event,
                raw_body=raw_body, body_sha256=body_sha256, received_at_ms=received_at_ms, started=started,
                staged_ref=staged_ref,
            )
    except (sqlite3.Error, storage.StorageError) as storage_failure:
        raise ReceiptNotDurableError(
            f"event {event_id!r} for integration {integration_id!r} of run {run_id!r} was not durably received "
            f"({type(storage_failure).__name__}: {storage_failure}); respond 503 so the provider retries"
        ) from storage_failure


def _commit_receipt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    integration_id: str,
    event_id: str,
    event: PaymentEvent,
    raw_body: bytes,
    body_sha256: str,
    received_at_ms: int,
    started: float,
    staged_ref: str | None,
) -> DeliveryResponse:
    """``staged_ref`` names the staged next secret when it, not the current one, verified the signature."""
    staged_note = None if staged_ref is None else STAGED_SECRET_NOTE.format(secret_ref=staged_ref)

    def record(outcome: ReceiveOutcome, detail: str | None = None) -> int:
        notes = [note for note in (detail, staged_note) if note is not None]
        storage.insert_delivery_attempt(
            conn, run_id=run_id, integration_id=integration_id, event_id=event_id,
            http_status=HTTP_STATUS_BY_OUTCOME[outcome], outcome=outcome, latency_ms=_elapsed_ms(started),
            received_at_ms=received_at_ms, body_sha256=body_sha256, detail="; ".join(notes) or None,
        )
        return HTTP_STATUS_BY_OUTCOME[outcome]

    existing = storage.find_inbox_row(conn, run_id=run_id, integration_id=integration_id, event_id=event_id)
    if existing is not None and existing.body_sha256 == body_sha256:
        return DeliveryResponse(ReceiveOutcome.DUPLICATE, record(ReceiveOutcome.DUPLICATE), existing.inbox_id, event_id)
    if existing is not None:
        conflict_id = _hold_conflicting_delivery(
            conn, original=existing, event=event, raw_body=raw_body, body_sha256=body_sha256,
            received_at_ms=received_at_ms,
        )
        status = record(ReceiveOutcome.CONFLICTING_ID)
        return DeliveryResponse(ReceiveOutcome.CONFLICTING_ID, status, existing.inbox_id, event_id, conflict_id)
    # This event ID is new, so a stored row with these bytes holds them under another ID: a resend. Acknowledged so the
    # sender stops retrying, it adds no inbox row and no observation, and does not reserve its event ID.
    stored = storage.find_inbox_row_by_body(conn, run_id=run_id, integration_id=integration_id, body_sha256=body_sha256)
    if stored is not None:
        status = record(ReceiveOutcome.DUPLICATE,
                        f"body already received as event {stored.event_id} (inbox row {stored.inbox_id})")
        return DeliveryResponse(ReceiveOutcome.DUPLICATE, status, stored.inbox_id, event_id)
    # The same for bytes held as a conflicting delivery (Q34): accepted under a fresh ID, they would escape review.
    # No inbox row holds them, so the answer points at the held delivery alone.
    held = storage.find_conflicting_delivery_by_body(
        conn, run_id=run_id, integration_id=integration_id, body_sha256=body_sha256
    )
    if held is not None:
        status = record(ReceiveOutcome.DUPLICATE,
                        f"body already held as conflicting delivery {held.id} of event {held.event_id}")
        return DeliveryResponse(ReceiveOutcome.DUPLICATE, status, None, event_id, held.id)

    inbox_id = storage.insert_inbox_row(
        conn, run_id=run_id, integration_id=integration_id, event_id=event_id, event_type=event.event,
        raw_body=raw_body, body_sha256=body_sha256, received_at_ms=received_at_ms,
    )
    storage.insert_observation(
        conn, _observation_from(event, run_id=run_id, inbox_id=inbox_id, event_id=event_id, known_at_ms=received_at_ms)
    )
    return DeliveryResponse(ReceiveOutcome.ACCEPTED, record(ReceiveOutcome.ACCEPTED), inbox_id, event_id)


def _hold_conflicting_delivery(
    conn: sqlite3.Connection,
    *,
    original: storage.InboxRecord,
    event: PaymentEvent,
    raw_body: bytes,
    body_sha256: str,
    received_at_ms: int,
) -> int:
    """Store a reused event ID with different bytes once per distinct body; never processed."""
    held = storage.find_conflicting_delivery(
        conn, run_id=original.run_id, integration_id=original.integration_id, event_id=original.event_id,
        body_sha256=body_sha256,
    )
    if held is not None:
        return held.id
    # The stored bytes passed this same parser when they were received.
    original_event = parse_payment_event(original.raw_body)
    return storage.insert_conflicting_delivery(
        conn, run_id=original.run_id, integration_id=original.integration_id, event_id=original.event_id,
        raw_body=raw_body, body_sha256=body_sha256, original_sha256=original.body_sha256,
        differing_fields=differing_business_fields(original_event, event), payment_id=event.payment.id,
        order_id=event.payment.order_id, original_payment_id=original_event.payment.id,
        original_order_id=original_event.payment.order_id, received_at_ms=received_at_ms,
    )


def _observation_from(
    event: PaymentEvent, *, run_id: str, inbox_id: int, event_id: str, known_at_ms: int
) -> PaymentObservation:
    payment = event.payment
    return PaymentObservation(
        run_id=run_id,
        observation_id=f"obs_inbox_{inbox_id}",
        payment_id=payment.id,
        order_id=payment.order_id,
        status=PaymentStatus(payment.status),
        amount_paise=payment.amount,
        currency=payment.currency,
        method=payment.method,
        source=ObservationSource.WEBHOOK,
        source_ref=event_id,
        occurred_at_ms=event.occurred_at_ms,
        known_at_ms=known_at_ms,
        amount_refunded_paise=payment.amount_refunded,
        error_code=payment.error_code,
        error_source=payment.error_source,
        error_step=payment.error_step,
        error_reason=payment.error_reason,
    )


def _record_rejection(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    integration_id: str,
    outcome: ReceiveOutcome,
    event_id: str | None,
    received_at_ms: int,
    body_sha256: str,
    started: float,
    detail: str,
) -> DeliveryResponse:
    """Record a rejected delivery's metadata only; inbox, observations and orders stay untouched."""
    http_status = HTTP_STATUS_BY_OUTCOME[outcome]
    with storage.write_transaction(conn):
        storage.insert_delivery_attempt(
            conn, run_id=run_id, integration_id=integration_id, event_id=event_id, http_status=http_status,
            outcome=outcome, latency_ms=_elapsed_ms(started), received_at_ms=received_at_ms,
            body_sha256=body_sha256, detail=detail[:500],
        )
    return DeliveryResponse(outcome, http_status, None, event_id)


def _signature_problem(headers: Mapping[str, str], raw_body: bytes, secret_ref: str) -> str | None:
    """Why the signature does not verify, or None when it does."""
    provided = _single_header(headers, signing.SIGNATURE_HEADER)
    if provided is None:
        return "signature header missing or repeated"
    candidate = provided.strip().lower()
    if not _HEX_SHA256_PATTERN.fullmatch(candidate):
        return "signature header is not 64 hexadecimal characters"
    expected = hmac.new(signing.fixture_secret(secret_ref), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), candidate.encode("ascii")):
        return _SIGNATURE_MISMATCH
    return None


def _verifying_staged_secret(
    conn: sqlite3.Connection,
    integration: storage.IntegrationRecord,
    headers: Mapping[str, str],
    raw_body: bytes,
    *,
    at_ms: int,
) -> str | None:
    """The staged next secret, when the integration has one at ``at_ms`` and the signature verifies with it (Q22).

    Asked only after the current secret failed: during a rotation the provider may already sign with the secret
    the merchant has staged. Outside a rotation nothing is staged, and this answers None.
    """
    staged = runtime.staged_secret_ref(conn, run_id=integration.run_id, secret_ref=integration.secret_ref, at_ms=at_ms)
    if staged is None or _signature_problem(headers, raw_body, staged) is not None:
        return None
    return staged


def _single_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive lookup. The same header under two casings is ambiguous, so treated as absent."""
    wanted = name.lower()
    values = [value for key, value in headers.items() if key.lower() == wanted]
    return values[0] if len(values) == 1 else None


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
