"""Open grouped incidents from detector results (EVALUATION.md §7).

``detect_incidents`` evaluates every detector as of a replay time. When something
fires and no incident is open for the run, it opens one whose trigger is the
highest-priority fired detector (the business symptom first), and records the result
of every detector, fired or not, as ``incident_trigger`` evidence: which detector did
not fire is evidence too. Firings during the grouping window attach to the open
incident as later evidence instead of opening a second incident (cooldown).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from pydantic import JsonValue

from recovery_workbench import metrics, storage
from recovery_workbench.contracts import EvidenceKind, canonical_json, sha256_hex

# The business symptom outranks its specific explanations as the incident's trigger. Signature rejections at an
# integration that still accepts deliveries (Q25) rank after the rejection spike, the broader receiver failure.
TRIGGER_PRIORITY: tuple[str, ...] = (
    metrics.ORDERS_NOT_COMPLETING, metrics.PROCESSING_GAP, metrics.METHOD_DROP, metrics.REJECTION_SPIKE,
    metrics.SIGNATURE_REJECTIONS, metrics.UNRESOLVED_SPIKE,
)
_SHORT_NAMES = {
    metrics.ORDERS_NOT_COMPLETING: "symptom",
    metrics.PROCESSING_GAP: "processing_gap",
    metrics.METHOD_DROP: "method_drop",
    metrics.REJECTION_SPIKE: "rejections",
    metrics.SIGNATURE_REJECTIONS: "signatures",
    metrics.UNRESOLVED_SPIKE: "unresolved",
}


@dataclass(frozen=True)
class IncidentSettings:
    detectors: metrics.IncidentDetectorSettings = metrics.DEFAULT_INCIDENT_DETECTORS
    # Cooldown: while an incident opened within this window is open, new firings attach to it
    # instead of opening another incident for the same ongoing problem.
    grouping_window_ms: int = 6 * metrics.HOUR_MS


DEFAULT_INCIDENT_SETTINGS = IncidentSettings()


@dataclass(frozen=True)
class DetectionReport:
    as_of_ms: int
    results: tuple[metrics.DetectorResult, ...]
    opened_incident_id: str | None
    attached_incident_id: str | None  # an already open incident that received the new firings

    @property
    def fired(self) -> tuple[metrics.DetectorResult, ...]:
        return tuple(result for result in self.results if result.fired)


def detect_incidents(
    conn: sqlite3.Connection, run_id: str, as_of_ms: int, settings: IncidentSettings = DEFAULT_INCIDENT_SETTINGS
) -> DetectionReport:
    """Evaluate detectors at ``as_of_ms`` and persist a grouped incident if anything fired."""
    windows = settings.detectors.windows
    view = metrics.load_run_view(conn, run_id, as_of_ms, stale_after_ms=windows.stale_after_ms)
    results = metrics.evaluate_detectors(view, settings.detectors)
    fired = [result for result in results if result.fired]
    if not fired:
        return DetectionReport(as_of_ms, results, None, None)
    with storage.write_transaction(conn):
        open_incident = _open_incident(conn, run_id, as_of_ms, settings.grouping_window_ms)
        if open_incident is not None:
            _attach(conn, open_incident, fired, as_of_ms)
            return DetectionReport(as_of_ms, results, None, open_incident.incident_id)
        incident = _open(conn, view, results, fired)
    return DetectionReport(as_of_ms, results, incident.incident_id, None)


def _open_incident(
    conn: sqlite3.Connection, run_id: str, as_of_ms: int, grouping_window_ms: int
) -> storage.IncidentRecord | None:
    recent = [
        incident
        for incident in storage.list_incidents(conn, run_id=run_id, as_of_ms=as_of_ms)
        if incident.state == "open" and as_of_ms - incident.evidence_cutoff_ms < grouping_window_ms
    ]
    return recent[-1] if recent else None


def _open(
    conn: sqlite3.Connection,
    view: metrics.RunView,
    results: tuple[metrics.DetectorResult, ...],
    fired: list[metrics.DetectorResult],
) -> storage.IncidentRecord:
    primary = min(fired, key=lambda result: (TRIGGER_PRIORITY.index(result.detector), sorted(result.cohort.items())))
    sequence = len(storage.list_incidents(conn, run_id=view.run_id)) + 1
    merchant = metrics.merchant_measures(view)
    provider = metrics.provider_measures(view, metrics.DEFAULT_WINDOWS.current(view.as_of_ms))
    incident = storage.IncidentRecord(
        run_id=view.run_id,
        incident_id=f"inc_{sequence:04d}",
        trigger=primary.detector,
        cohort=dict(primary.cohort),
        window_start_ms=max(0, primary.window_start_ms),
        window_end_ms=max(0, primary.window_end_ms),
        evidence_cutoff_ms=view.as_of_ms,
        state="open",
        measured_impact={
            "symptom": primary.detector,
            "fired_detectors": sorted({result.detector for result in fired}),
            "not_fired_detectors": sorted({r.detector for r in results} - {r.detector for r in fired}),
            "summary_counts": {
                "pending_captured_orders": merchant.pending_captured_orders,
                "pending_captured_value_paise": merchant.pending_captured_value_paise,
                "backlog_rows": merchant.backlog_rows,
                "worker_failures": merchant.worker_failures,
                "rejected_delivery_attempts": merchant.rejected_attempts,
                "mature_attempts": provider.authorization.denominator,
                "mature_attempts_authorized": provider.authorization.numerator,
                "mature_attempts_unresolved": provider.unresolved.numerator,
            },
        },
    )
    storage.insert_incident(conn, incident)
    for result in results:
        _record(conn, incident, result, view.as_of_ms, suffix="")
    return incident


def _attach(
    conn: sqlite3.Connection, incident: storage.IncidentRecord, fired: list[metrics.DetectorResult], as_of_ms: int
) -> None:
    """Record detectors that newly fire after the incident opened (known later than its cutoff)."""
    offset_minutes = (as_of_ms - incident.evidence_cutoff_ms) // metrics.MINUTE_MS
    if offset_minutes == 0:
        return  # the same evaluation already recorded every result when the incident opened
    already_fired = {
        (str(item.finding.get("detector")), canonical_json(item.finding.get("cohort")))
        for item in storage.list_evidence(conn, run_id=incident.run_id, source_record_id=incident.incident_id)
        if item.finding.get("fired") is True
    }
    for result in fired:
        if (result.detector, canonical_json(dict(result.cohort))) not in already_fired:
            _record(conn, incident, result, as_of_ms, suffix=f"_{offset_minutes}m")


def _evidence_id(incident: storage.IncidentRecord, result: metrics.DetectorResult, suffix: str) -> str:
    cohort = "_".join(re.sub(r"[^a-z0-9]+", "_", str(value).lower()) for _, value in sorted(result.cohort.items()))
    return f"ev_trig_{incident.incident_id.removeprefix('inc_')}_{_SHORT_NAMES[result.detector]}_{cohort}{suffix}"[:63]


def _record(
    conn: sqlite3.Connection,
    incident: storage.IncidentRecord,
    result: metrics.DetectorResult,
    known_at_ms: int,
    *,
    suffix: str,
) -> None:
    finding: dict[str, JsonValue] = result.as_json()
    finding["evaluated_at_ms"] = known_at_ms
    storage.insert_evidence(
        conn,
        storage.EvidenceRecord(
            run_id=incident.run_id,
            evidence_id=_evidence_id(incident, result, suffix),
            kind=EvidenceKind.INCIDENT_TRIGGER,
            source_table="incidents",
            source_record_id=incident.incident_id,
            known_at_ms=known_at_ms,
            content_hash=sha256_hex(canonical_json(finding)),
            finding=finding,
        ),
    )
