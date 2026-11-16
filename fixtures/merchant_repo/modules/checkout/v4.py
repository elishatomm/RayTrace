"""Payment options the storefront offers at checkout."""
from merchant_app import config

METHOD_ORDER = ("card", "upi", "netbanking")

# Netbanking bank codes, most used first.
NETBANKING_BANKS = ("NB04", "NB01", "NB03", "NB02")

# Card instalments start at this order value; below it the monthly amounts are too small to be worth offering.
EMI_MIN_AMOUNT_PAISE = 3_00_000
EMI_TENURES_MONTHS = (3, 6, 9, 12)


def emi_tenures(amount_paise):
    """Instalment plans offered on a card payment for an order of this value."""
    if amount_paise < EMI_MIN_AMOUNT_PAISE:
        return []
    return list(EMI_TENURES_MONTHS)


def checkout_options(order_id, amount_paise):
    return {
        "order_id": order_id,
        "amount_paise": amount_paise,
        "currency": config.CURRENCY,
        "methods": list(METHOD_ORDER),
        "default_method": METHOD_ORDER[0],
        "netbanking_banks": list(NETBANKING_BANKS),
        "card_emi_tenures_months": emi_tenures(amount_paise),
        "session_ttl_seconds": config.CHECKOUT_SESSION_TTL_SECONDS,
    }
