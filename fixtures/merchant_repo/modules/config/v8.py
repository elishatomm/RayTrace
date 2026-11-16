"""Settings shared by the storefront, the webhook receiver and the order worker."""
import re

CURRENCY = "INR"

# Key on the payment entity that carries the storefront's order reference.
ORDER_REFERENCE_FIELD = "order_id"

# Events that complete an order. An authorisation on its own never ships anything.
# A pattern rather than a list: the provider's next webhook API sends versioned names such as payment.captured.v2.
FULFIL_ON_EVENTS = re.compile(r"payment\.captured(\.v\d+)?")

# Name of the signing secret deliveries are verified with; the value lives in the secret store.
WEBHOOK_SECRET_REF = "webhook_secret_primary"

CHECKOUT_SESSION_TTL_SECONDS = 900

RECEIPT_CHANNEL = "email"
