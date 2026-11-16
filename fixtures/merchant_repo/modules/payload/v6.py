"""Turn a verified webhook event into the flat fields the order flow uses."""
from decimal import Decimal

from merchant_app import config

SUPPORTED_EVENTS = ("payment.authorized", "payment.captured", "payment.failed")


class UnsupportedEvent(ValueError):
    """The integration does not subscribe to this event type."""


def to_rupees(amount_paise):
    """Rupee amount for receipts; order arithmetic stays in integer paise."""
    return Decimal(amount_paise) / 100


def parse_event(event):
    event_type = event["event"]
    if event_type not in SUPPORTED_EVENTS:
        raise UnsupportedEvent(f"unsupported event type {event_type!r}")
    entity = event["payload"]["payment"]["entity"]
    return {
        "event_type": event_type,
        "payment_id": entity["id"],
        "order_id": entity[config.ORDER_REFERENCE_FIELD],
        "status": entity["status"],
        "method": entity.get("method"),
        "amount_paise": entity["amount"],
        "amount_display": str(to_rupees(entity["amount"])),
        "currency": entity["currency"],
    }
