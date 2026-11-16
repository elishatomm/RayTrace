"""Decide what the order system does with a parsed payment event."""
import logging

from merchant_app import config

log = logging.getLogger(__name__)


class InvalidOrderAmount(ValueError):
    """The event's amount cannot settle an order."""


def build_instruction(parsed):
    if parsed["event_type"] not in config.FULFIL_ON_EVENTS:
        log.debug("no order change for %s on %s", parsed["event_type"], parsed["order_id"])
        return {"action": "ignore"}
    amount_paise = parsed["amount_paise"]
    if not isinstance(amount_paise, int) or amount_paise <= 0:
        raise InvalidOrderAmount(f"order {parsed['order_id']}: expected positive integer paise, got {amount_paise!r}")
    if parsed["currency"] != config.CURRENCY:
        raise InvalidOrderAmount(f"order {parsed['order_id']}: unsupported currency {parsed['currency']!r}")
    log.info("fulfilment instruction for %s via %s", parsed["order_id"], parsed["payment_id"])
    return {
        "action": "fulfil",
        "order_id": parsed["order_id"],
        "payment_id": parsed["payment_id"],
        "amount_paise": amount_paise,
        "currency": parsed["currency"],
    }
