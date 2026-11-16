"""What the webhook receiver does with a delivery before checking its signature."""
import json

from merchant_app import config


def body_for_verification(raw_body):
    """The bytes the signature is checked against, in one stable layout so stored copies compare cleanly."""
    document = json.loads(raw_body)
    return json.dumps(document, indent=2, sort_keys=True).encode("utf-8")


def verification_secret_ref():
    """Name of the signing secret deliveries are verified with."""
    return config.WEBHOOK_SECRET_REF
