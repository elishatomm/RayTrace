"""What the webhook receiver does with a delivery before checking its signature."""
from merchant_app import config

PREVIEW_CHARS = 200


def body_for_verification(raw_body):
    """The bytes the signature is checked against: the body exactly as delivered."""
    return raw_body


def body_preview(raw_body):
    """Short, printable start of a delivery body for receiver logs."""
    return raw_body[:PREVIEW_CHARS].decode("utf-8", errors="replace")


def verification_secret_ref():
    """Name of the signing secret deliveries are verified with."""
    return config.WEBHOOK_SECRET_REF
