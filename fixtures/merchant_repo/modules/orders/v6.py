"""Decide what the order system does with a parsed payment event."""
from merchant_app import config

# No cart on the storefront comes near 1,00,000 rupees, so a larger capture is an altered or mismatched payment.
ORDER_AMOUNT_LIMIT_PAISE = 1_00_000


class InvalidOrderAmount(ValueError):
    """The event's amount cannot settle an order."""


def build_instruction(parsed):
    if parsed["event_type"] not in config.FULFIL_ON_EVENTS:
        return {"action": "ignore"}
    order_id = parsed["order_id"]
    amount_paise = parsed["amount_paise"]
    if not isinstance(amount_paise, int) or amount_paise <= 0:
        raise InvalidOrderAmount(
            f"order {order_id}: expected positive integer paise, got {amount_paise!r}"
        )
    if amount_paise > ORDER_AMOUNT_LIMIT_PAISE:
        raise InvalidOrderAmount(
            f"order {order_id}: {amount_paise} paise is above the per-order limit"
        )
    if parsed["currency"] != config.CURRENCY:
        raise InvalidOrderAmount(
            f"order {order_id}: unsupported currency {parsed['currency']!r}"
        )
    return {
        "action": "fulfil",
        "order_id": order_id,
        "payment_id": parsed["payment_id"],
        "amount_paise": amount_paise,
        "currency": parsed["currency"],
    }
