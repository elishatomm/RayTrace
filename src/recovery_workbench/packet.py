"""Assemble the ``pkt-2`` evidence packet for one incident (docs/PACKET_SCHEMA.md, binding).

``build_packet`` reads only what was known by the cutoff, through ``tools.py``, plus the deployed
handler the caller passes in for the fixture check. The same database, run, incident and cutoff
give a byte-identical packet, and so the same ``packet_hash()``. No wall-clock value, private
simulator table or oracle content enters a packet.

Every item is also persisted as an ``evidence`` row with its content hash. Packet item IDs are
neutral and reused by every packet (``ev_trigger``), so the stored rows get a packet-scoped ID and
keep the item ID inside the finding; building the same packet again stores nothing new.
"""
from __future__ import annotations

import re
import sqlite3

from pydantic import JsonValue

from recovery_workbench import metrics, storage, tools
from recovery_workbench.contracts import (
    ContractError,
    EvidenceItem,
    EvidenceKind,
    EvidencePacket,
    IncidentCard,
    ScopeError,
    canonical_json,
    sha256_hex,
)
from recovery_workbench.simulator import merchant

PACKET_VERSION = "pkt-2"  # pkt-2 adds ev_trigger.fired_since_open (see docs/PACKET_SCHEMA.md)
SYMPTOM = "Fewer orders are completing than usual"
MAX_LOGS = 8
MAX_JOURNEYS = 3
EVIDENCE_SOURCE_TABLE = "evidence_packets"
_ID_UNSAFE = re.compile(r"[^a-z0-9_]+")


def build_packet(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    incident_id: str,
    cutoff_ms: int,
    deployed: merchant.DeployedMerchant,
    persist: bool = True,
) -> EvidencePacket:
    """The bounded, cutoff-filtered evidence for one incident, exactly as ``pkt-2`` specifies."""
    scope = tools.scope_for(conn, run_id=run_id, cutoff_ms=cutoff_ms)
    incident = _require_incident(conn, scope, incident_id)
    view = tools.load_view(conn, scope)
    with storage.read_transaction(conn):
        changes = tools.recent_changes(conn, scope, tools.ALL)
        log_groups = tools.recent_log_groups(conn, scope, limit=MAX_LOGS)
        trigger_rows = storage.list_evidence(
            conn, run_id=run_id, as_of_ms=cutoff_ms, kind=EvidenceKind.INCIDENT_TRIGGER, source_record_id=incident_id
        )
    methods = sorted({attempt.method for attempt in view.attempts})
    items = [
        _trigger_item(scope, view, incident, trigger_rows),
        *(_metric_item(scope, view, method) for method in methods),
        _metric_item(scope, view, None),
        _item("ev_metric_processing", EvidenceKind.METRIC, scope.cutoff_ms,
              "Merchant processing of captured payments", tools.processing_facts(conn, scope, view)),
        _item("ev_provider_state", EvidenceKind.PROVIDER_STATE, scope.cutoff_ms,
              "Provider payment states and snapshot freshness", tools.provider_state_facts(view, scope)),
        _item("ev_delivery", EvidenceKind.DELIVERY_HEALTH, scope.cutoff_ms, "Webhook delivery health",
              tools.delivery_facts(view, scope, tools.ALL)),
        _item("ev_worker", EvidenceKind.WORKER_HEALTH, scope.cutoff_ms, "Order worker health",
              tools.worker_health_facts(view, scope)),
        *_worker_error_items(view, scope),
        _fixture_item(scope, deployed),
        *_deployment_items(changes),
        *_log_items(log_groups),
        *_journey_items(view, scope),
    ]
    packet = EvidencePacket(
        packet_version=PACKET_VERSION,
        incident=_card(scope, view, incident),
        items=tuple(items),
        candidate_change_ids=tuple(change.record.deploy_id for change in changes),
        candidate_methods=tuple(methods),
    )
    if persist:
        persist_packet(conn, packet)
    return packet


def _require_incident(conn: sqlite3.Connection, scope: tools.ToolScope, incident_id: str) -> storage.IncidentRecord:
    with storage.read_transaction(conn):
        known = storage.list_incidents(conn, run_id=scope.run_id, as_of_ms=scope.cutoff_ms)
    for incident in known:
        if incident.incident_id == incident_id:
            return incident
    raise ScopeError(f"incident {incident_id!r} is not an incident of run {scope.run_id!r} opened by {scope.cutoff_ms}")


def _item(
    evidence_id: str, kind: EvidenceKind, known_at_ms: int, title: str, facts: dict[str, JsonValue], text: str | None = None
) -> EvidenceItem:
    return EvidenceItem(evidence_id=evidence_id, kind=kind, known_at_ms=known_at_ms, title=title, facts=facts, text=text)


def detector_name(result: metrics.DetectorResult) -> str:
    """pkt-2 detector names: one per method for the method detector, e.g. ``method_authorization_drop[upi]``."""
    if result.detector == metrics.METHOD_DROP:
        return f"{result.detector}[{result.cohort['method']}]"
    return result.detector


def _incident_trigger_name(incident: storage.IncidentRecord) -> str:
    method = incident.cohort.get("method")
    return f"{incident.trigger}[{method}]" if incident.trigger == metrics.METHOD_DROP and method else incident.trigger


def _trigger_item(
    scope: tools.ToolScope,
    view: metrics.RunView,
    incident: storage.IncidentRecord,
    trigger_rows: tuple[storage.EvidenceRecord, ...],
) -> EvidenceItem:
    results = metrics.evaluate_detectors(view, metrics.DEFAULT_INCIDENT_DETECTORS)
    fired = sorted({detector_name(result) for result in results if result.fired})
    evaluated = sorted({detector_name(result) for result in results})
    reasons: dict[str, JsonValue] = {}
    for result in sorted(results, key=lambda item: (not item.fired, sorted(item.cohort.items()))):
        reasons.setdefault(detector_name(result), result.reason)
    breaches = [
        row.known_at_ms for row in trigger_rows
        if row.finding.get("fired") is True and row.finding.get("detector") == incident.trigger
        and row.finding.get("cohort") == incident.cohort
    ]
    # A detector can stop firing before the diagnosis cutoff (a moderate drop whose latest
    # windows overlap the baseline). The alert history is evidence too, so the packet carries
    # every detector that fired while the incident was open, not only the state at the cutoff.
    fired_since_open = sorted(
        {name for row in trigger_rows if row.finding.get("fired") is True and (name := _history_name(row.finding))}
    )
    return _item(
        "ev_trigger", EvidenceKind.INCIDENT_TRIGGER, scope.cutoff_ms, "Detector state at the evidence cutoff",
        {
            "fired": fired,
            "not_fired": [name for name in evaluated if name not in fired],
            "fired_since_open": fired_since_open,
            "first_breach_ms": min(breaches, default=None),
            "reasons": dict(sorted(reasons.items())),
        },
    )


def _history_name(finding: dict[str, JsonValue]) -> str | None:
    """pkt detector name for a stored detector finding, or None when the row names no detector."""
    detector = finding.get("detector")
    if not isinstance(detector, str):
        return None
    cohort = finding.get("cohort")
    method = cohort.get("method") if isinstance(cohort, dict) else None
    return f"{detector}[{method}]" if detector == metrics.METHOD_DROP and isinstance(method, str) else detector


def _metric_item(scope: tools.ToolScope, view: metrics.RunView, method: str | None) -> EvidenceItem:
    label = method or tools.ALL
    title = f"Provider authorisation and capture for {label}, mature cohort"
    return _item(f"ev_metric_{_id_part(label)}", EvidenceKind.METRIC, scope.cutoff_ms, title,
                 tools.provider_cohort_facts(view, method))


def _worker_error_items(view: metrics.RunView, scope: tools.ToolScope) -> list[EvidenceItem]:
    errors = tools.worker_error_facts(view, scope)
    if errors is None:
        return []
    return [_item("ev_worker_errors", EvidenceKind.WORKER_ERROR, int(errors.facts["last_failure_ms"]),  # type: ignore[arg-type]
                  "Order worker failures, most common stack trace", errors.facts, errors.trace)]


def _fixture_item(scope: tools.ToolScope, deployed: merchant.DeployedMerchant) -> EvidenceItem:
    checks = [tools.run_fixture_check(deployed, test_id=test_id) for test_id in merchant.FIXTURE_TEST_IDS]
    failing_trace = next((check.failure.stack_trace for check in checks if check.failure is not None), None)
    results: list[JsonValue] = [
        {
            "test_id": check.test_id,
            "passed": check.passed,
            "detail": check.detail,
            "top_frame": None if check.failure is None else tools.top_frame(check.failure.stack_trace),
        }
        for check in checks
    ]
    return _item(
        "ev_fixture", EvidenceKind.FIXTURE_CHECK, scope.cutoff_ms,
        f"Prewritten fixture tests against the deployed version {deployed.version}",
        {
            "deployed_version": deployed.version,
            "tests_run": len(checks),
            "tests_failed": sum(not check.passed for check in checks),
            "results": results,
        },
        failing_trace,
    )


def _deployment_items(changes: tuple[tools.Change, ...]) -> list[EvidenceItem]:
    items = []
    used: set[str] = set()
    for change in changes:
        record = change.record
        evidence_id = f"ev_deploy_{_id_part(record.deploy_id.removeprefix('deploy_'))}"[:63]
        if evidence_id in used:
            raise ContractError(f"deployments {record.deploy_id!r} and another map to the same evidence ID {evidence_id!r}")
        used.add(evidence_id)
        items.append(_item(
            evidence_id, EvidenceKind.DEPLOYMENT, record.known_at_ms, f"Deployment {record.deploy_id} to {record.service_id}",
            {
                "deploy_id": record.deploy_id,
                "service": record.service_id,
                "kind": record.kind,
                "deployed_at_ms": record.deployed_at_ms,
                "files_changed": list(record.files_changed),
                "summary": record.summary,
            },
            change.diff,
        ))
    return items


def _log_items(groups: tuple[tools.LogGroup, ...]) -> list[EvidenceItem]:
    return [
        _item(
            f"ev_log_{index:02d}", EvidenceKind.LOG, group.latest.known_at_ms,
            f"{group.latest.level} log from {group.latest.service_id}",
            {
                "source": group.latest.service_id,
                "level": group.latest.level,
                "occurred_at_ms": group.latest.occurred_at_ms,
                "occurrences": group.occurrences,  # lines of the same kind in the lookback (extra key)
            },
            group.latest.message,
        )
        for index, group in enumerate(groups, start=1)
    ]


def _journey_items(view: metrics.RunView, scope: tools.ToolScope) -> list[EvidenceItem]:
    return [
        _item(f"ev_journey_{index:02d}", EvidenceKind.ORDER_JOURNEY, scope.cutoff_ms, f"Order journey for {order_id}",
              tools.order_journey_facts(view, order_id))
        for index, order_id in enumerate(tools.sample_journey_orders(view, scope, limit=MAX_JOURNEYS), start=1)
    ]


def _card(scope: tools.ToolScope, view: metrics.RunView, incident: storage.IncidentRecord) -> IncidentCard:
    start = scope.lookback_start_ms
    recent_orders = {
        attempt.order_id for attempt in view.attempts if attempt.created_at_ms >= start and attempt.order_id in view.orders
    }
    integrations = sorted(view.integration_ids)
    return IncidentCard(
        run_id=scope.run_id,
        incident_id=incident.incident_id,
        symptom=SYMPTOM,
        trigger=_incident_trigger_name(incident),
        cohort={
            "merchant_id": scope.merchant_id,
            "integration_id": integrations[0] if len(integrations) == 1 else ",".join(integrations),
            "methods": sorted({attempt.method for attempt in view.attempts}),
        },
        window_start_ms=incident.window_start_ms,
        window_end_ms=incident.window_end_ms,
        evidence_cutoff_ms=scope.cutoff_ms,
        summary_counts={
            "orders_with_attempts": len(recent_orders),
            "orders_completed": sum(order_id in view.fulfilments for order_id in recent_orders),
            "payment_attempts": sum(attempt.created_at_ms >= start for attempt in view.attempts),
        },
    )


def _id_part(text: str) -> str:
    return _ID_UNSAFE.sub("_", text.lower()).strip("_") or "x"


# --------------------------------------------------------------------------- persistence


def packet_ref(packet: EvidencePacket) -> str:
    """Identifies one packet's stored evidence: its incident and cutoff."""
    return f"{packet.incident.incident_id}@{packet.incident.evidence_cutoff_ms}"


def stored_evidence_id(packet: EvidencePacket, item_id: str) -> str:
    token = sha256_hex(f"{packet.incident.run_id}|{packet_ref(packet)}".encode())[:10]
    return f"ev_p{token}_{item_id.removeprefix('ev_')}"[:63]


def persist_packet(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    """Store every item as an ``evidence`` row with its content hash; storing the same packet twice is a no-op.

    A second build for the same incident and cutoff that differs would mean evidence below the
    cutoff changed; that is refused loudly instead of overwriting what a diagnosis cited.
    """
    run_id, reference = packet.incident.run_id, packet_ref(packet)
    rows = []
    for item in packet.items:
        dumped = item.model_dump(mode="json")
        rows.append(storage.EvidenceRecord(
            run_id=run_id,
            evidence_id=stored_evidence_id(packet, item.evidence_id),
            kind=item.kind,
            source_table=EVIDENCE_SOURCE_TABLE,
            source_record_id=reference,
            known_at_ms=item.known_at_ms,
            content_hash=sha256_hex(canonical_json(dumped)),
            finding={"packet_version": packet.packet_version, "packet_item_id": item.evidence_id, "item": dumped},
        ))
    if len({row.evidence_id for row in rows}) != len(rows):
        raise ContractError(f"packet {reference} would store two items under one evidence ID; item IDs are too long")
    with storage.write_transaction(conn):
        existing = storage.list_evidence(conn, run_id=run_id, source_record_id=reference)
        if existing:
            if {(row.evidence_id, row.content_hash) for row in existing} == {(row.evidence_id, row.content_hash) for row in rows}:
                return
            raise ContractError(
                f"the evidence stored for {reference} differs from a fresh build; records known by the cutoff changed"
            )
        for row in rows:
            storage.insert_evidence(conn, row)


def stored_packet_items(conn: sqlite3.Connection, *, run_id: str, reference: str) -> tuple[EvidenceItem, ...]:
    """The persisted items of one packet (``incident@cutoff``), grouped by kind for display."""
    with storage.read_transaction(conn):
        rows = storage.list_evidence(conn, run_id=run_id, source_record_id=reference)
    items = [EvidenceItem.model_validate(row.finding["item"]) for row in rows if row.source_table == EVIDENCE_SOURCE_TABLE]
    order = {"incident_trigger": 0, "metric": 1, "provider_state": 2, "delivery_health": 3, "worker_health": 4,
             "worker_error": 5, "fixture_check": 6, "deployment": 7, "log": 8, "order_journey": 9}
    return tuple(sorted(items, key=lambda item: (order[item.kind.value], item.evidence_id)))
