"""Payment options the storefront offers at checkout."""
from merchant_app import config

METHOD_ORDER = ("card", "upi", "netbanking")

# Netbanking bank codes, in code order so customers can scan the list.
NETBANKING_BANKS = ("NB01", "NB02", "NB03", "NB04")


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
