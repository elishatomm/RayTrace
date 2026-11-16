"""Payment options the storefront offers at checkout."""
from merchant_app import config

METHOD_ORDER = ("upi", "card", "netbanking")

# Netbanking bank codes, most used first.
NETBANKING_BANKS = ("NB04", "NB01", "NB03", "NB02")


def checkout_options(order_id, amount_paise):
    return {
        "order_id": order_id,
        "amount_paise": amount_paise,
        "currency": config.CURRENCY,
        "methods": list(METHOD_ORDER),
        "default_method": METHOD_ORDER[0],
        "netbanking_banks": list(NETBANKING_BANKS),
        "session_ttl_seconds": config.CHECKOUT_SESSION_TTL_SECONDS,
    }
