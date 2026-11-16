"""Decide what the order system does with a parsed payment event."""
from merchant_app import config


class InvalidOrderAmount(ValueError):
    """The event's amount cannot settle an order."""


def settlement_problem(event):
    """Why the event's amount or currency cannot settle its order, or None when it can."""
    amount_paise = event["amount_paise"]
    if not isinstance(amount_paise, int) or amount_paise <= 0:
        return f"expected positive integer paise, got {amount_paise!r}"
    if event["currency"] != config.CURRENCY:
        return f"unsupported currency {event['currency']!r}"
    return None


def instruction_for(event):
    """What the order system does with one parsed payment event."""
    if event["event_type"] not in config.FULFIL_ON_EVENTS:
        return {"action": "ignore"}
    problem = settlement_problem(event)
    if problem is not None:
        raise InvalidOrderAmount(f"order {event['order_id']}: {problem}")
    return {
        "action": "fulfil",
        "order_id": event["order_id"],
        "payment_id": event["payment_id"],
        "amount_paise": event["amount_paise"],
        "currency": event["currency"],
    }


# The handler and the order service still call the old name; drop it once both call instruction_for.
build_instruction = instruction_for
