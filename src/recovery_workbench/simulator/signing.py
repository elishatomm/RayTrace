"""Synthetic webhook producer: provider-shaped envelopes, stable bytes and HMAC signatures.

Everything here is a fixture. The secrets below are fake, deliberately published in
source, and protect nothing: they only let the synthetic producer and the receiver
agree on an HMAC. They are not credentials of any payment provider. The envelope
follows the supported subset in EVALUATION.md §3.2 and is never attributed to a
real gateway.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from types import MappingProxyType

from pydantic import JsonValue

from recovery_workbench.contracts import ContractError, PaymentStatus, canonical_json

EVENT_ID_HEADER = "X-Webhook-Event-Id"
SIGNATURE_HEADER = "X-Webhook-Signature"  # lowercase hex HMAC-SHA256 of the raw body

# Synthetic extension, not part of the provider-shaped subset: the envelope's
# ``created_at`` is whole seconds, so the fictional provider also states the same
# instant in milliseconds. SPEC.md §7 needs sub-second occurrence times, and the
# payment entity's own ``created_at`` is the payment's creation, not this transition.
MS_EXTENSION_FIELD = "x_synthetic_created_at_ms"

DEFAULT_ACCOUNT_ID = "acc_SYNTHETIC_A"

FIXTURE_SECRETS: Mapping[str, bytes] = MappingProxyType(
    {
        "fixture_secret_a": b"synthetic-fixture-secret-a-not-a-provider-credential",
        "fixture_secret_b": b"synthetic-fixture-secret-b-not-a-provider-credential",
        # Names the merchant fixture's config modules use (fixtures/merchant_repo/modules/config).
        # The simulated provider signs with the primary key; a config deploy that verifies with
        # the next key is the D3 receiver-configuration case.
        "webhook_secret_primary": b"synthetic-merchant-fixture-key-primary",
        "webhook_secret_next": b"synthetic-merchant-fixture-key-next",
    }
)

EVENT_TYPE_BY_STATUS: Mapping[PaymentStatus, str] = MappingProxyType(
    {
        PaymentStatus.AUTHORIZED: "payment.authorized",
        PaymentStatus.CAPTURED: "payment.captured",
        PaymentStatus.FAILED: "payment.failed",
        PaymentStatus.REFUNDED: "payment.refunded",
    }
)


def synthetic_id(kind: str, number: int) -> str:
    """Fictional identifier such as ``pay_SYNTHETIC_0007``."""
    return f"{kind}_SYNTHETIC_{number:04d}"


def fixture_secret(secret_ref: str) -> bytes:
    try:
        return FIXTURE_SECRETS[secret_ref]
    except KeyError as missing:
        raise ContractError(
            f"no fixture secret named {secret_ref!r}; integrations must reference one of {sorted(FIXTURE_SECRETS)}"
        ) from missing


def sign_body(raw_body: bytes, secret_ref: str) -> str:
    """Lowercase hex HMAC-SHA256 over the exact bytes that will be sent."""
    return hmac.new(fixture_secret(secret_ref), raw_body, hashlib.sha256).hexdigest()


def build_payment_event(
    *,  # NOSONAR(S107) keyword-only, one per field of the provider payload, most defaulted; a wrapper would only move them
    payment_id: str,
    order_id: str,
    amount_paise: int,
    status: PaymentStatus,
    occurred_at_ms: int,
    payment_created_at_ms: int,
    currency: str = "INR",
    method: str | None = "upi",
    account_id: str = DEFAULT_ACCOUNT_ID,
    amount_refunded_paise: int = 0,
    error_code: str | None = None,
    error_source: str | None = None,
    error_step: str | None = None,
    error_reason: str | None = None,
    event_type: str | None = None,
    include_ms_extension: bool = True,
) -> dict[str, JsonValue]:
    """A provider-shaped payment event envelope with synthetic values.

    ``occurred_at_ms`` is when the transition happened (the envelope time);
    ``payment_created_at_ms`` is when the payment was created. ``event_type`` may
    be overridden only to build deliberately inconsistent fixtures for tests.
    """
    for name, value in (("amount_paise", amount_paise), ("occurred_at_ms", occurred_at_ms),
                        ("payment_created_at_ms", payment_created_at_ms),
                        ("amount_refunded_paise", amount_refunded_paise)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(f"{name} must be a non-negative integer, got {value!r}")
    resolved_type = event_type if event_type is not None else EVENT_TYPE_BY_STATUS.get(status)
    if resolved_type is None:
        raise ContractError(f"no webhook event type carries status {status.value!r}; pass event_type explicitly")
    envelope: dict[str, JsonValue] = {
        "entity": "event",
        "account_id": account_id,
        "event": resolved_type,
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount_paise,
                    "currency": currency,
                    "status": status.value,
                    "order_id": order_id,
                    "method": method,
                    "captured": status in (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED),
                    "amount_refunded": amount_refunded_paise,
                    "error_code": error_code,
                    "error_source": error_source,
                    "error_step": error_step,
                    "error_reason": error_reason,
                    "created_at": payment_created_at_ms // 1000,
                }
            }
        },
        "created_at": occurred_at_ms // 1000,
    }
    if include_ms_extension:
        envelope[MS_EXTENSION_FIELD] = occurred_at_ms
    return envelope


def serialise_event(envelope: Mapping[str, JsonValue]) -> bytes:
    """Stable bytes for signing: sorted keys, compact separators, ASCII."""
    return canonical_json(dict(envelope))


def signed_delivery(
    envelope: Mapping[str, JsonValue], *, event_id: str, secret_ref: str
) -> tuple[dict[str, str], bytes]:
    """``(headers, raw_body)`` for one delivery; the event ID travels only in a header."""
    raw_body = serialise_event(envelope)
    headers = {EVENT_ID_HEADER: event_id, SIGNATURE_HEADER: sign_body(raw_body, secret_ref)}
    return headers, raw_body
