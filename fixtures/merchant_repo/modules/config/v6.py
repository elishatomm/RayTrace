"""Settings shared by the storefront, the webhook receiver and the order worker."""

CURRENCY = "INR"

# Key on the payment entity that carries the storefront's order reference.
ORDER_REFERENCE_FIELD = "order_id"

# Events that complete an order. An authorisation on its own never ships anything.
FULFIL_ON_EVENTS = ("payment.captured",)

# Name of the signing secret deliveries are verified with; the value lives in the secret store.
WEBHOOK_SECRET_REF = "webhook_secret_primary"

# Staged for the next rotation; verification moves to it once the provider signs with it.
NEXT_WEBHOOK_SECRET_REF = "webhook_secret_next"

CHECKOUT_SESSION_TTL_SECONDS = 900

RECEIPT_CHANNEL = "email"
