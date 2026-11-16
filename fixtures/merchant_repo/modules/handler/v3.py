"""Entry point the worker calls once for each committed webhook event."""
import logging
import time
from collections import Counter

from merchant_app import orders, payload

log = logging.getLogger(__name__)

# Events handled since the worker started, by event type and outcome; the worker's metrics endpoint reads it.
HANDLED_EVENTS = Counter()


class ErrorBoundary:
    """Tag an exception with the processing stage it escaped from, for structured error logs."""

    def __init__(self, stage):
        self.stage = stage

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and not hasattr(exc, "error_boundary"):
            exc.error_boundary = self.stage
        return False


def handle_event(event):
    started = time.monotonic()
    outcome = "raised"
    try:
        with ErrorBoundary("event_parse_error"):
            parsed = payload.parse_event(event)
        with ErrorBoundary("order_validation_error"):
            instruction = orders.build_instruction(parsed)
        outcome = instruction["action"]
        return instruction
    finally:
        HANDLED_EVENTS[(event.get("event"), outcome)] += 1
        elapsed_ms = (time.monotonic() - started) * 1000
        log.debug("handled %s as %s in %.1f ms", event.get("event"), outcome, elapsed_ms)
