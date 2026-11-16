"""Messages sent to the customer after an order changes state."""


def rupees(amount_paise):
    return f"{amount_paise // 100}.{amount_paise % 100:02d}"


def order_confirmation(order_id, amount_paise):
    return f"Thanks for your order {order_id}. We received INR {rupees(amount_paise)} and are packing it now."


def payment_pending(order_id):
    return f"Payment for order {order_id} has not been confirmed yet. We will email you as soon as it is."
