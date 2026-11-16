"""Entry point the worker calls once for each committed webhook event.

The worker passes the event exactly as the provider delivered it, after the receiver has checked its
signature and stored it. Parsing and the order decision run in separate error boundaries, so a failure
is logged with the stage it came from.
"""
from merchant_app import orders, payload


class ErrorBoundary:
    """Tag an exception with the processing stage it escaped from, for structured error logs.

    The tag is set on the exception object, so it survives being re-raised by outer code. The
    boundary never suppresses an exception.
    """

    def __init__(self, stage):
        self.stage = stage

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Keep a tag set by an inner boundary: it names the stage closest to where the exception started.
        if exc is not None and not hasattr(exc, "error_boundary"):
            exc.error_boundary = self.stage
        return False


def handle_event(event):
    """Return the instruction for one event: fulfil its order, or ignore the event.

    Exceptions propagate unchanged; the worker records them and tries the event again later.
    """
    with ErrorBoundary("event_parse_error"):
        parsed = payload.parse_event(event)
    with ErrorBoundary("order_validation_error"):
        return orders.build_instruction(parsed)
