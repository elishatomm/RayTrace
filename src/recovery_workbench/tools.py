"""The v1 read tools (SPEC.md §8) and strict tool-call validation.

The server injects scope: the run, its merchant and the evidence cutoff (``ToolScope``). A caller
can never supply them, and every tool reads only what was known by the cutoff. Tools take bounded
identifiers only: no SQL, shell commands, file paths or URLs. They read observable tables through
``storage`` and ``metrics``; they never touch the simulator's private ``sim_*`` tables or any oracle
file. ``run_fixture_check`` runs on the deployed handler the caller passes in, and never on a
corrected one.

Each tool is a thin wrapper over a builder that takes an already loaded ``metrics.RunView``, so
``packet.py`` reads the run once and assembles every packet item from the same snapshot.
"""
from __future__ import annotations

import inspect
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import JsonValue

from recovery_workbench import metrics, state, storage, worker
from recovery_workbench.contracts import (
    ContractError,
    ObservationSource,
    PaymentStatus,
    ReceiveOutcome,
    ScopeError,
    WorkerResult,
)
from recovery_workbench.simulator import merchant

V1_READ_TOOLS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "get_metrics": frozenset({"cohort", "window"}),
        "get_order_journey": frozenset({"order_id"}),
        "get_delivery_health": frozenset({"integration_id"}),
        "get_worker_health": frozenset({"worker_id"}),
        "get_recent_changes": frozenset({"service_id"}),
        "run_fixture_check": frozenset({"test_id"}),
    }
)

# Scope and authority come from the server session, never from tool arguments.
SERVER_INJECTED_ARGUMENTS: frozenset[str] = frozenset(
    {"run_id", "merchant_id", "cutoff_ms", "as_of_ms", "evidence_cutoff_ms", "approval", "operator_id"}
)

_ARGUMENT_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")

MINUTE_MS = metrics.MINUTE_MS
HOUR_MS = metrics.HOUR_MS
# Evidence older than this before the cutoff stays out of a packet (deployments, failures, logs).
LOOKBACK_MS = 6 * HOUR_MS
WORKER_ID = "order_worker"  # the one worker; deployments of its modules are reported under this service
ALL = "all"
MAX_DIFF_LINES = 60
MAX_DIFF_CHARS = 2_500
MAX_TRACE_CHARS = 4_000
_TRACE_FRAME = re.compile(r'^ {2}File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<name>\S+)$', re.MULTILINE)
_CAPTURE_EVIDENCE = frozenset({PaymentStatus.CAPTURED, PaymentStatus.REFUNDED})
_OPEN_STATUSES = frozenset({PaymentStatus.UNKNOWN, PaymentStatus.CREATED, PaymentStatus.AUTHORIZED})


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, str]


def validate_tool_call(name: str, arguments: Mapping[str, object]) -> ToolCall:
    """Accept only an allowlisted tool with exactly its arguments, each a bounded identifier.

    Raises ``ContractError`` for an unknown tool, a missing or extra argument (including
    any server-injected one such as ``run_id`` or ``approval``) or a malformed value.
    """
    expected = V1_READ_TOOLS.get(name)
    if expected is None:
        raise ContractError(f"unknown tool {name!r}; allowed tools are {sorted(V1_READ_TOOLS)}")
    supplied = set(arguments)
    injected = sorted(supplied & SERVER_INJECTED_ARGUMENTS)
    if injected:
        raise ContractError(f"tool {name!r} may not receive {injected}; the server injects scope and authority")
    extra = sorted(supplied - expected)
    missing = sorted(expected - supplied)
    if extra or missing:
        raise ContractError(f"tool {name!r} takes exactly {sorted(expected)}; extra {extra}, missing {missing}")
    validated: dict[str, str] = {}
    for argument, value in arguments.items():
        if not isinstance(value, str) or not _ARGUMENT_VALUE_PATTERN.fullmatch(value):
            raise ContractError(f"argument {argument!r} of tool {name!r} must be a short identifier string")
        validated[argument] = value
    return ToolCall(name=name, arguments=MappingProxyType(validated))


# --------------------------------------------------------------------------- scope and shared helpers


@dataclass(frozen=True)
class ToolScope:
    """Server-injected scope; never taken from a tool argument."""

    run_id: str
    merchant_id: str
    cutoff_ms: int

    @property
    def lookback_start_ms(self) -> int:
        return max(0, self.cutoff_ms - LOOKBACK_MS)


def scope_for(conn: sqlite3.Connection, *, run_id: str, cutoff_ms: int) -> ToolScope:
    """The scope for reading ``run_id`` as of ``cutoff_ms``, which may not lie beyond the run's replay clock."""
    if isinstance(cutoff_ms, bool) or not isinstance(cutoff_ms, int) or cutoff_ms < 0:
        raise ContractError(f"cutoff_ms must be a non-negative integer of replay time, got {cutoff_ms!r}")
    run = storage.require_run(conn, run_id)
    if cutoff_ms > run.clock_ms:
        raise ContractError(
            f"cutoff {cutoff_ms} is after run {run_id!r}'s replay clock {run.clock_ms}; evidence cannot come from the future"
        )
    return ToolScope(run_id=run.run_id, merchant_id=run.merchant_id, cutoff_ms=cutoff_ms)


def load_view(conn: sqlite3.Connection, scope: ToolScope) -> metrics.RunView:
    """Everything known about the run by the cutoff, projected with the detectors' staleness setting."""
    return metrics.load_run_view(
        conn, scope.run_id, scope.cutoff_ms, stale_after_ms=metrics.DEFAULT_WINDOWS.stale_after_ms
    )


def _ratio(value: metrics.Ratio) -> float | None:
    return None if value.denominator == 0 else round(value.numerator / value.denominator, 3)


def _wilson(value: metrics.Ratio) -> list[float] | None:
    if value.denominator == 0:
        return None
    low, high = metrics.wilson_interval(value.numerator, value.denominator)
    return [round(low, 3), round(high, 3)]


def _share(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(part / whole, 3)


def _counts(values: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def worker_max_attempts() -> int:
    """The worker's own bounded-retry limit, read from its signature so the two never disagree."""
    default = inspect.signature(worker.run_worker).parameters["max_attempts"].default
    if not isinstance(default, int):
        raise ContractError(f"worker.run_worker has no integer max_attempts default (got {default!r})")
    return default


# --------------------------------------------------------------------------- get_metrics


def provider_cohort_facts(view: metrics.RunView, method: str | None) -> dict[str, JsonValue]:
    """pkt-2 ``metric`` item for one method (or ``all``): the current mature cohort against the baseline."""
    windows = metrics.DEFAULT_WINDOWS
    current_window, baseline_window = windows.current(view.as_of_ms), windows.baseline(view.as_of_ms)
    current = metrics.provider_measures(view, current_window, method=method)
    baseline = metrics.provider_measures(view, baseline_window, method=method)
    every_current = metrics.provider_measures(view, current_window).authorization.denominator
    every_baseline = metrics.provider_measures(view, baseline_window).authorization.denominator
    return {
        "method": method or ALL,
        "mature_cohort_start_ms": current_window[0],
        "mature_cohort_end_ms": current_window[1],
        "attempts": current.authorization.denominator,
        "authorised": current.authorization.numerator,
        "captured": current.capture.numerator,
        "unresolved": current.unresolved.numerator,
        "authorisation_ratio": _ratio(current.authorization),
        "authorisation_ratio_95ci": _wilson(current.authorization),
        "baseline_attempts": baseline.authorization.denominator,
        "baseline_authorisation_ratio": _ratio(baseline.authorization),
        "baseline_95ci": _wilson(baseline.authorization),
        "share_of_attempts": _share(current.authorization.denominator, every_current),
        "baseline_share_of_attempts": _share(baseline.authorization.denominator, every_baseline),
    }


def processing_facts(conn: sqlite3.Connection, scope: ToolScope, view: metrics.RunView) -> dict[str, JsonValue]:
    """pkt-2 ``ev_metric_processing``: captured orders the merchant processed, now and at the baseline's end."""
    detectors = metrics.DEFAULT_INCIDENT_DETECTORS
    windows = detectors.windows
    current = metrics.merchant_measures(
        view, grace_ms=detectors.processing_grace_ms, lookback_ms=detectors.processing_lookback_ms
    )
    baseline_end = windows.baseline(scope.cutoff_ms)[1]
    baseline_view = metrics.load_run_view(conn, scope.run_id, max(0, baseline_end), stale_after_ms=windows.stale_after_ms)
    baseline = metrics.merchant_measures(baseline_view, grace_ms=detectors.processing_grace_ms, lookback_ms=windows.baseline_ms)
    return {
        "captured_orders": current.completion.denominator,
        "processed": current.completion.numerator,
        "pending_captured": current.pending_captured_orders,
        "held_for_review": current.manual_review_rows,
        "pending_captured_value_paise": current.pending_captured_value_paise,
        "processing_completion_ratio": _ratio(current.completion),
        "baseline_processing_completion_ratio": _ratio(baseline.completion),
        "grace_ms": detectors.processing_grace_ms,
        "lookback_ms": detectors.processing_lookback_ms,
    }


def provider_state_facts(view: metrics.RunView, scope: ToolScope) -> dict[str, JsonValue]:
    """pkt-2 ``ev_provider_state``: projected payment states over the lookback, as of the cutoff."""
    start = scope.lookback_start_ms
    attempts = [attempt for attempt in view.attempts if attempt.created_at_ms >= start]
    method_by_payment = {attempt.payment_id: attempt.method for attempt in attempts}
    payments = [view.payments[payment_id] for payment_id in sorted(method_by_payment)]
    snapshots = [
        observation.known_at_ms
        for payment in view.payments.values()
        for observation in payment.history
        if observation.source is ObservationSource.SNAPSHOT
    ]
    failed = [payment for payment in payments if payment.status is PaymentStatus.FAILED]
    reasons, sources, by_method = [], [], {}
    for payment in failed:
        latest = max(
            (item for item in payment.history if item.status is PaymentStatus.FAILED),
            key=lambda item: (item.occurred_at_ms, item.known_at_ms),
        )
        reason = latest.error_reason or "unspecified"
        reasons.append(reason)
        sources.append(latest.error_source or "unspecified")
        by_method.setdefault(method_by_payment[str(payment.payment_id)], []).append(reason)
    coverage = metrics.provider_measures(view, (start, scope.cutoff_ms + 1)).coverage
    return {
        "snapshot_age_ms": scope.cutoff_ms - max(snapshots) if snapshots else None,
        "observation_coverage": _ratio(coverage),
        "payments_by_status": _counts([payment.status.value for payment in payments]),
        "failed_by_reason": _counts(reasons),
        "failed_by_method_and_reason": {method: _counts(items) for method, items in sorted(by_method.items())},
        "failed_by_error_source": _counts(sources),
        "failed_then_authorised_later": sum(payment.late_authorization for payment in payments),
        "authorised_not_captured": sum(payment.status is PaymentStatus.AUTHORIZED for payment in payments),
        "captured_without_received_event": sum(
            payment.has_capture_evidence
            and not any(
                item.source is ObservationSource.WEBHOOK and item.status in _CAPTURE_EVIDENCE for item in payment.history
            )
            for payment in payments
        ),
        "payments_needing_reconciliation": sum(payment.needs_reconciliation for payment in payments),
        "reconciliation_reasons": _counts([reason.value for payment in payments for reason in payment.conflict_reasons]),
        # Only open payments: a settled capture or failure with no recent observation is old, not stale.
        "stale_payments": sum(payment.stale and payment.status in _OPEN_STATUSES for payment in payments),
        "lookback_start_ms": start,
    }


def get_metrics(conn: sqlite3.Connection, scope: ToolScope, *, cohort: str, window: str) -> dict[str, JsonValue]:
    """``cohort``: a method, ``all``, ``processing`` or ``provider_state``. ``window``: ``current`` only in v1."""
    if window != "current":
        raise ContractError(f"get_metrics supports window 'current' (the mature cohort) in v1, got {window!r}")
    view = load_view(conn, scope)
    methods = sorted({attempt.method for attempt in view.attempts})
    if cohort == "processing":
        return processing_facts(conn, scope, view)
    if cohort == "provider_state":
        return provider_state_facts(view, scope)
    if cohort != ALL and cohort not in methods:
        raise ContractError(f"unknown cohort {cohort!r}; choose one of {[ALL, *methods, 'processing', 'provider_state']}")
    return provider_cohort_facts(view, None if cohort == ALL else cohort)


# --------------------------------------------------------------------------- get_order_journey


def order_journey_facts(view: metrics.RunView, order_id: str) -> dict[str, JsonValue]:
    """pkt-2 ``order_journey``: every attempt and visible observation of one order, and its projection."""
    projection = view.order_projections.get(order_id)
    order = view.orders.get(order_id)
    if projection is None or order is None:
        raise ScopeError(f"order {order_id!r} is not an order of run {view.run_id!r} known by the cutoff")
    method_by_payment = {attempt.payment_id: attempt.method for attempt in view.attempts if attempt.order_id == order_id}
    attempts = []
    for payment in projection.payments:
        observations = sorted(payment.history, key=lambda item: (item.occurred_at_ms, item.known_at_ms, item.observation_id))
        attempts.append({
            "payment_id": payment.payment_id,
            "method": method_by_payment.get(str(payment.payment_id)),
            "observations": [
                {"status": item.status.value, "occurred_at_ms": item.occurred_at_ms, "known_at_ms": item.known_at_ms,
                 "source": item.source.value}
                for item in observations
            ],
        })
    return {
        "order_id": order_id,
        "order_amount_paise": order.amount_paise,
        "attempts": attempts,
        "projection": {
            "status": _order_status(projection).value,
            "exclusions": [reason.value for reason in projection.exclusions],
            "late_authorization": any(payment.late_authorization for payment in projection.payments),
            "conflicts": sorted({reason.value for payment in projection.payments for reason in payment.conflict_reasons}),
        },
    }


_STATUS_STRENGTH = (
    PaymentStatus.REFUNDED, PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED, PaymentStatus.FAILED,
    PaymentStatus.CREATED, PaymentStatus.UNKNOWN,
)


def _order_status(projection: state.OrderProjection) -> PaymentStatus:
    """The strongest payment status across the order's attempts (evidence, not a verdict)."""
    statuses = {payment.status for payment in projection.payments}
    return next((status for status in _STATUS_STRENGTH if status in statuses), PaymentStatus.UNKNOWN)


def get_order_journey(conn: sqlite3.Connection, scope: ToolScope, *, order_id: str) -> dict[str, JsonValue]:
    return order_journey_facts(load_view(conn, scope), order_id)


# --------------------------------------------------------------------------- get_delivery_health


def delivery_facts(view: metrics.RunView, scope: ToolScope, integration_id: str) -> dict[str, JsonValue]:
    """pkt-2 ``ev_delivery`` over the lookback; ``integration_id='all'`` covers every integration of the run.

    A request to an integration the run does not configure leaves an ``unknown_integration`` row, whoever sent it.
    It is no integration's delivery, so, as in the rejection detectors, it counts in none.
    """
    if integration_id != ALL and integration_id not in view.integration_ids:
        raise ScopeError(f"integration {integration_id!r} is not part of run {scope.run_id!r}")
    covered = view.integration_ids if integration_id == ALL else (integration_id,)
    start = scope.lookback_start_ms
    deliveries = [item for item in view.deliveries if item.received_at_ms >= start and item.integration_id in covered]
    rejected = [item for item in deliveries if item.outcome in metrics.REJECTED_OUTCOMES]
    first_rejection = min((item.received_at_ms for item in rejected), default=None)

    def count(outcome: ReceiveOutcome) -> int:
        return sum(item.outcome is outcome for item in deliveries)

    return {
        "window_start_ms": start,
        "delivery_attempts": len(deliveries),
        "accepted": count(ReceiveOutcome.ACCEPTED),
        "duplicates": count(ReceiveOutcome.DUPLICATE),
        "conflicting_ids": count(ReceiveOutcome.CONFLICTING_ID),
        "rejected_signature": count(ReceiveOutcome.REJECTED_SIGNATURE),
        "rejected_schema": count(ReceiveOutcome.REJECTED_SCHEMA),
        "rejected_size": count(ReceiveOutcome.REJECTED_SIZE),
        # An unverifiable delivery has no trusted event ID; its body hash identifies it instead.
        "distinct_events_rejected": len({item.event_id or item.body_sha256 for item in rejected}),
        "first_rejection_ms": first_rejection,
        "accepted_since_first_rejection": None
        if first_rejection is None
        else sum(item.outcome is ReceiveOutcome.ACCEPTED and item.received_at_ms >= first_rejection for item in deliveries),
    }


def get_delivery_health(conn: sqlite3.Connection, scope: ToolScope, *, integration_id: str) -> dict[str, JsonValue]:
    return delivery_facts(load_view(conn, scope), scope, integration_id)


# --------------------------------------------------------------------------- get_worker_health


def worker_health_facts(view: metrics.RunView, scope: ToolScope) -> dict[str, JsonValue]:
    """pkt-2 ``ev_worker``, reconstructed as of the cutoff from worker attempts (not current row status)."""
    cutoff = scope.cutoff_ms
    limit = worker_max_attempts()
    failed_attempts = Counter(item.inbox_id for item in view.worker_attempts if item.result is WorkerResult.FAILED)
    unsettled = [row for row in view.inbox if row.inbox_id not in view.settled]
    exhausted = sum(failed_attempts[row.inbox_id] >= limit for row in unsettled)
    return {
        "inbox_pending": len(unsettled) - exhausted,
        "inbox_failed": exhausted,
        "inbox_manual_review": sum(result is WorkerResult.MANUAL_REVIEW for _, result in view.settled.values()),
        "oldest_unprocessed_age_ms": cutoff - min(row.received_at_ms for row in unsettled) if unsettled else None,
        "processed_last_30m": sum(
            result is not WorkerResult.MANUAL_REVIEW and settled_at >= cutoff - 30 * MINUTE_MS
            for settled_at, result in view.settled.values()
        ),
        "failed_attempts_last_60m": sum(
            item.result is WorkerResult.FAILED and item.finished_at_ms >= cutoff - HOUR_MS for item in view.worker_attempts
        ),
        "last_event_received_ms": max((row.received_at_ms for row in view.inbox), default=None),
        "retry_policy": f"{limit} attempts per event, then failed until approved reprocessing",
    }


@dataclass(frozen=True)
class WorkerErrors:
    facts: dict[str, JsonValue]
    trace: str | None  # the most common failure's stack trace, verbatim (bounded)


def _trace_signature(trace: str | None, category: str | None) -> str:
    if not trace:
        return f"no trace | {category or 'no category'}"
    frames = "; ".join(f"{file}:{line}" for file, line, _ in _TRACE_FRAME.findall(trace))
    exception = trace.rstrip().splitlines()[-1].split(":", 1)[0]
    return f"{frames} | {exception}"


def worker_error_facts(view: metrics.RunView, scope: ToolScope) -> WorkerErrors | None:
    """pkt-2 ``ev_worker_errors`` over the lookback, or None when no attempt failed."""
    failures = [
        item for item in view.worker_attempts
        if item.result is WorkerResult.FAILED and item.finished_at_ms >= scope.lookback_start_ms
    ]
    if not failures:
        return None
    rows = {row.inbox_id: row for row in view.inbox}
    categories = Counter(item.exception_category for item in failures if item.exception_category)
    signatures = Counter(_trace_signature(item.stack_trace, item.exception_category) for item in failures)
    top_signature = min(signatures, key=lambda signature: (-signatures[signature], signature))
    latest = max(
        (item for item in failures if _trace_signature(item.stack_trace, item.exception_category) == top_signature),
        key=lambda item: (item.finished_at_ms, item.id),
    )
    failed_rows = sorted({item.inbox_id for item in failures})
    return WorkerErrors(
        facts={
            # Most common structured category; null when the merchant app emits free text only.
            "exception_category": min(categories, key=lambda name: (-categories[name], name)) if categories else None,
            "failed_events": len(failed_rows),
            "failed_by_event_type": _counts([rows[inbox_id].event_type for inbox_id in failed_rows if inbox_id in rows]),
            "first_failure_ms": min(item.finished_at_ms for item in failures),
            "last_failure_ms": max(item.finished_at_ms for item in failures),
            "worker_versions": _counts([item.worker_version for item in failures]),
            "distinct_trace_signatures": len(signatures),
        },
        trace=None if latest.stack_trace is None else latest.stack_trace[:MAX_TRACE_CHARS],
    )


def get_worker_health(conn: sqlite3.Connection, scope: ToolScope, *, worker_id: str) -> dict[str, JsonValue]:
    if worker_id != WORKER_ID:
        raise ContractError(f"unknown worker {worker_id!r}; this merchant runs one worker, {WORKER_ID!r}")
    view = load_view(conn, scope)
    errors = worker_error_facts(view, scope)
    return {
        "health": worker_health_facts(view, scope),
        "errors": None if errors is None else errors.facts,
        "most_common_trace": None if errors is None else errors.trace,
    }


# --------------------------------------------------------------------------- get_recent_changes


@dataclass(frozen=True)
class Change:
    record: storage.DeploymentRecord
    diff: str  # bounded to MAX_DIFF_LINES lines and MAX_DIFF_CHARS characters


def bound_diff(diff: str) -> str:
    lines = diff.splitlines(keepends=True)
    shown = "".join(lines[:MAX_DIFF_LINES])
    hidden_lines = len(lines) - min(len(lines), MAX_DIFF_LINES)
    if len(shown) > MAX_DIFF_CHARS:
        shown = shown[:MAX_DIFF_CHARS].rsplit("\n", 1)[0] + "\n"
        hidden_lines = len(lines) - shown.count("\n")
    return shown if hidden_lines == 0 else shown + f"... {hidden_lines} more diff lines not shown\n"


def recent_changes(conn: sqlite3.Connection, scope: ToolScope, service_id: str) -> tuple[Change, ...]:
    """Deployments known by the cutoff within the lookback, oldest first, with bounded diffs."""
    records = storage.list_deployments(
        conn, run_id=scope.run_id, as_of_ms=scope.cutoff_ms, service_id=None if service_id == ALL else service_id
    )
    return tuple(
        Change(record=record, diff=bound_diff(record.diff_text))
        for record in records
        if record.deployed_at_ms >= scope.lookback_start_ms
    )


def get_recent_changes(conn: sqlite3.Connection, scope: ToolScope, *, service_id: str) -> list[dict[str, JsonValue]]:
    with storage.read_transaction(conn):
        storage.require_run(conn, scope.run_id)
        changes = recent_changes(conn, scope, service_id)
    return [
        {
            "deploy_id": change.record.deploy_id,
            "service": change.record.service_id,
            "kind": change.record.kind,
            "deployed_at_ms": change.record.deployed_at_ms,
            "files_changed": list(change.record.files_changed),
            "summary": change.record.summary,
            "diff": change.diff,
        }
        for change in changes
    ]


# --------------------------------------------------------------------------- run_fixture_check


def run_fixture_check(deployed: merchant.DeployedMerchant, *, test_id: str) -> merchant.FixtureCheckResult:
    """One allowlisted fixture test against the currently deployed handler (never a corrected one)."""
    if test_id not in merchant.FIXTURE_TEST_IDS:
        raise ContractError(f"fixture test {test_id!r} is not allowlisted; choose one of {merchant.FIXTURE_TEST_IDS}")
    return merchant.run_fixture_check(test_id, deployed)


def top_frame(stack_trace: str | None) -> str | None:
    if not stack_trace:
        return None
    frames = _TRACE_FRAME.findall(stack_trace)
    if not frames:
        return None
    file, line, name = frames[-1]
    return f"{file}:{line} in {name}"


# --------------------------------------------------------------------------- server-side helpers (not model tools)


@dataclass(frozen=True)
class LogGroup:
    """Repeated log lines collapsed into one template; the latest line stands for the group."""

    latest: storage.MerchantLogRecord
    occurrences: int


_LOG_IDENTIFIER = re.compile(r"\b[A-Za-z]+_[A-Za-z0-9_]*\d[A-Za-z0-9_]*\b")  # NOSONAR(S6353) ASCII on purpose; Python's \w also matches non-ASCII letters, which would change the log templates in evidence packets
_LOG_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


def _log_template(message: str) -> str:
    return _LOG_NUMBER.sub("<n>", _LOG_IDENTIFIER.sub("<id>", message))


def recent_log_groups(conn: sqlite3.Connection, scope: ToolScope, *, limit: int) -> tuple[LogGroup, ...]:
    """At most ``limit`` distinct kinds of log line from the lookback, the most recently seen kinds.

    Hundreds of identical worker errors would otherwise crowd out everything else; collapsing them
    keeps one example per kind with its count, so rare lines (a deploy, an alert) stay visible.
    """
    logs = [
        log for log in storage.list_merchant_logs(conn, run_id=scope.run_id, as_of_ms=scope.cutoff_ms)
        if log.occurred_at_ms >= scope.lookback_start_ms
    ]
    groups: dict[tuple[str, str, str], list[storage.MerchantLogRecord]] = {}
    for log in logs:
        groups.setdefault((log.service_id, log.level, _log_template(log.message)), []).append(log)
    collapsed = [
        LogGroup(latest=max(items, key=lambda item: (item.occurred_at_ms, item.log_id)), occurrences=len(items))
        for items in groups.values()
    ]
    chosen = sorted(collapsed, key=lambda group: (-group.latest.occurred_at_ms, group.latest.log_id))[:limit]
    return tuple(sorted(chosen, key=lambda group: (group.latest.occurred_at_ms, group.latest.log_id)))


def sample_journey_orders(view: metrics.RunView, scope: ToolScope, *, limit: int) -> tuple[str, ...]:
    """Up to ``limit`` orders worth inspecting, by one fixed rule in every packet: the newest of each kind.

    The kinds are captured but unprocessed, late-authorised or conflicting (even if fulfilled), and
    failed or unresolved. Taking one of each keeps the sample balanced, so a handful of edge-case orders
    cannot crowd out the orders that make up most of the problem (or the other way round).
    """
    newest: dict[int, tuple[int, str]] = {}
    for order_id, projection in view.order_projections.items():
        order = view.orders[order_id]
        if order.created_at_ms < scope.lookback_start_ms or not projection.payments:
            continue
        risky = any(payment.late_authorization or payment.needs_reconciliation for payment in projection.payments)
        # A fulfilled order still matters when another of its payments authorised late or conflicts:
        # that is a possible double payment. Otherwise only unfulfilled orders are samples.
        if order_id in view.fulfilments and not risky:
            continue
        if risky:
            kind = 1
        elif projection.single_captured_payment_id is not None:
            kind = 0
        else:
            kind = 2
        candidate = (-order.created_at_ms, order_id)
        if kind not in newest or candidate < newest[kind]:
            newest[kind] = candidate
    return tuple(newest[kind][1] for kind in sorted(newest))[:limit]
