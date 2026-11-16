"""What the webhook receiver does with a delivery before checking its signature."""
import json

from merchant_app import config


def body_for_verification(raw_body):
    """The bytes the signature is checked against: the body exactly as delivered."""
    return raw_body


def body_for_storage(raw_body):
    """One stable layout for stored copies so they compare cleanly; never used for verification."""
    document = json.loads(raw_body)
    return json.dumps(document, indent=2, sort_keys=True).encode("utf-8")


def verification_secret_ref():
    """Name of the signing secret deliveries are verified with."""
    return config.WEBHOOK_SECRET_REF
