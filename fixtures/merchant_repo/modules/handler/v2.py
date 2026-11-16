"""Entry point the worker calls once for each committed webhook event."""
import logging

from merchant_app import orders, payload

log = logging.getLogger(__name__)


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
    log.debug("handling %s created_at=%s", event.get("event"), event.get("created_at"))
    with ErrorBoundary("event_parse_error"):
        parsed = payload.parse_event(event)
    with ErrorBoundary("order_validation_error"):
        return orders.build_instruction(parsed)
