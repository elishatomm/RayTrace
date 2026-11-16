"""Messages sent to the customer after an order changes state."""


def rupees(amount_paise):
    return f"{amount_paise // 100}.{amount_paise % 100:02d}"


def order_confirmation(order_id, amount_paise):
    return f"Your order {order_id} is confirmed. Amount paid: INR {rupees(amount_paise)}."


def payment_pending(order_id):
    return f"We are still waiting for payment confirmation for order {order_id}."
