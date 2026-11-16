"""What the webhook receiver does with a delivery before checking its signature."""
from merchant_app import config


def body_for_verification(raw_body):
    """The bytes the signature is checked against: the body exactly as delivered."""
    return raw_body


def verification_secret_ref():
    """Name of the signing secret deliveries are verified with."""
    return config.WEBHOOK_SECRET_REF
