"""Turn a verified webhook event into the flat fields the order flow uses."""
from merchant_app import config

SUPPORTED_EVENTS = ("payment.authorized", "payment.captured", "payment.failed")
CAPTURED_EVENT = "payment.captured"


class UnsupportedEvent(ValueError):
    """The integration does not subscribe to this event type."""


def epoch_ms(seconds):
    """Provider timestamps are whole seconds; the dispatch SLA clock counts milliseconds."""
    return seconds * 1000


def parse_event(event):
    event_type = event["event"]
    if event_type not in SUPPORTED_EVENTS:
        raise UnsupportedEvent(f"unsupported event type {event_type!r}")
    entity = event["payload"]["payment"]["entity"]
    parsed = {
        "event_type": event_type,
        "payment_id": entity["id"],
        "order_id": entity[config.ORDER_REFERENCE_FIELD],
        "status": entity["status"],
        "method": entity.get("method"),
        "amount_paise": entity["amount"],
        "currency": entity["currency"],
    }
    if event_type == CAPTURED_EVENT:
        # Dispatch is promised within 24 hours of capture, so the SLA clock starts at the capture itself.
        parsed["captured_at_ms"] = epoch_ms(entity["captured_at"])
    return parsed
