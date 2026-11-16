"""Turn a verified webhook event into the flat fields the order flow uses."""
from merchant_app import config

SUPPORTED_EVENTS = ("payment.authorized", "payment.captured", "payment.failed")


class UnsupportedEvent(ValueError):
    """The integration does not subscribe to this event type."""


def provider_code(value):
    """One spelling for provider enum values, which have arrived in upper and lower case across API versions."""
    if value is None:
        return None
    return value.strip().lower()


def parse_event(event):
    event_type = event["event"]
    if event_type not in SUPPORTED_EVENTS:
        raise UnsupportedEvent(f"unsupported event type {event_type!r}")
    entity = event["payload"]["payment"]["entity"]
    return {
        "event_type": event_type,
        "payment_id": entity["id"],
        "order_id": entity[config.ORDER_REFERENCE_FIELD],
        "status": provider_code(entity["status"]),
        "method": provider_code(entity.get("method")),
        "amount_paise": entity["amount"],
        "currency": provider_code(entity["currency"]),
    }
