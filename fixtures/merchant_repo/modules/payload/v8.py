"""Turn a verified webhook event into the flat fields the order flow uses."""
from merchant_app import config

SUPPORTED_EVENTS = ("payment.authorized", "payment.captured", "payment.failed")


class UnsupportedEvent(ValueError):
    """The integration does not subscribe to this event type."""


def money(payment):
    """Amount, refunded amount and currency as one value, so no consumer pairs paise with the wrong currency."""
    return {
        "paise": payment["amount"],
        "refunded_paise": payment["amount_refunded"],
        "currency": payment["currency"],
    }


def parse_event(event):
    event_type = event["event"]
    if event_type not in SUPPORTED_EVENTS:
        raise UnsupportedEvent(f"unsupported event type {event_type!r}")
    payment = event["payload"]["payment"]["entity"]
    return {
        "event_type": event_type,
        "payment_id": payment["id"],
        "order_id": payment[config.ORDER_REFERENCE_FIELD],
        "status": payment["status"],
        "method": payment.get("method"),
        "amount": money(payment),
    }
