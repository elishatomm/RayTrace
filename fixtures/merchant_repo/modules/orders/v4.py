"""Decide what the order system does with a parsed payment event."""
from merchant_app import config


class InvalidOrderAmount(ValueError):
    """The event's amount cannot settle an order."""


def check_settlement(order_id, amount_paise, currency):
    """Raise unless the amount and currency can settle the order in full."""
    if not isinstance(amount_paise, int) or amount_paise <= 0:
        raise InvalidOrderAmount(f"order {order_id}: expected positive integer paise, got {amount_paise!r}")
    if currency != config.CURRENCY:
        raise InvalidOrderAmount(f"order {order_id}: unsupported currency {currency!r}")


def fulfilment(order_id, payment_id, amount_paise, currency):
    """The instruction that ships one order once its payment settles it."""
    check_settlement(order_id, amount_paise, currency)
    return {
        "action": "fulfil",
        "order_id": order_id,
        "payment_id": payment_id,
        "amount_paise": amount_paise,
        "currency": currency,
    }


def build_instruction(parsed):
    if parsed["event_type"] not in config.FULFIL_ON_EVENTS:
        return {"action": "ignore"}
    return fulfilment(**parsed)
