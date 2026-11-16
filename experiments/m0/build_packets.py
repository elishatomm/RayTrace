"""Build the ten frozen M0 evidence packets and their oracle answers.

Run once, then freeze the output:

    uv run python experiments/m0/build_packets.py

Stack traces, fixture-check results and code diffs come from actually running the fixture
merchant compositions (``recovery_workbench.simulator.merchant``). Metrics, logs and timings are
authored here as synthetic observations. Output:

* ``packets/m0_XX.json``: what the model sees (validated as ``contracts.EvidencePacket``);
* ``expected.json``: the oracle answers, never sent to the model.

Packet files are numbered in the order of the M0 brief; the number is not shown to the model, and
titles, IDs and texts are written so they do not name the answer.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench.contracts import (
    EvidenceItem,
    EvidenceKind,
    EvidencePacket,
    FailureReport,
    IncidentCard,
)
from recovery_workbench.diagnosis import render_packet
from recovery_workbench.simulator import merchant

M0_DIR = Path(__file__).resolve().parent
PACKET_DIR = M0_DIR / "packets"
EXPECTED_PATH = M0_DIR / "expected.json"
PACKET_VERSION = "m0-1"
METHODS = ("upi", "card", "netbanking")
MERCHANT_ID = "mch_synthetic_01"
INTEGRATION_ID = "int_webhook_01"
SERVICE = "merchant-app"
SYMPTOM = "Fewer orders are completing than usual"
MINUTE_MS = 60_000
DIFF_MAX_LINES = 45
TRACE_FRAME = re.compile(r'^  File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<name>\S+)$', re.MULTILINE)


# --------------------------------------------------------------------------- small helpers


def utc_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def before(cutoff_ms: int, minutes: float) -> int:
    return cutoff_ms - round(minutes * MINUTE_MS)


def clock(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z"


def short_hex(*parts: str, length: int = 6) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:length]


def wilson(successes: int, total: int) -> list[float]:
    """95% Wilson interval, rounded for display; ratios only, never money."""
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [round(centre - half, 3), round(centre + half, 3)]


def item(
    evidence_id: str,
    kind: EvidenceKind,
    known_at_ms: int,
    title: str,
    facts: Mapping[str, JsonValue],
    text: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id, kind=kind, known_at_ms=known_at_ms, title=title, facts=dict(facts), text=text
    )


def provider_event(
    event_type: str, payment_id: str, order_id: str, amount_paise: int, method: str, created_at_ms: int
) -> dict[str, JsonValue]:
    status = event_type.removeprefix("payment.")
    return {
        "entity": "event",
        "account_id": "acc_SYNTHETIC_A",
        "event": event_type,
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": status,
                    "order_id": order_id,
                    "method": method,
                    "captured": status == "captured",
                    "amount_refunded": 0,
                    "error_code": None,
                    "error_source": None,
                    "error_step": None,
                    "error_reason": None,
                    "created_at": created_at_ms // 1000 - 20,
                }
            }
        },
        "created_at": created_at_ms // 1000,
    }


def worker_failure(deployed: merchant.DeployedMerchant, event: Mapping[str, JsonValue]) -> FailureReport:
    try:
        deployed.handle(event)
    except Exception as exc:  # this composition is meant to fail here; the failure is the evidence
        return deployed.describe_failure(exc)
    raise RuntimeError(f"composition {dict(deployed.composition)} unexpectedly handled {event['event']}")


def top_frame(stack_trace: str) -> str:
    frames = TRACE_FRAME.findall(stack_trace)
    if not frames:
        return "no application frame"
    file, line, name = frames[-1]
    return f"{file}:{line} in {name}"


# --------------------------------------------------------------------------- deployments


@dataclass(frozen=True)
class Deploy:
    module: str
    variant: int
    minutes_before_cutoff: float
    summary_choice: int = 0


@dataclass(frozen=True)
class DeployRecord:
    deploy_id: str
    before: Mapping[str, int]
    after: Mapping[str, int]
    deployed_at_ms: int
    summary: str
    module: str
    variant: int


def build_timeline(packet_id: str, cutoff_ms: int, deploys: Sequence[Deploy]) -> list[DeployRecord]:
    ordered = sorted(deploys, key=lambda deploy: -deploy.minutes_before_cutoff)
    composition: dict[str, int] = {}
    records = []
    for deploy in ordered:
        after = {**composition, deploy.module: deploy.variant}
        summaries = merchant.commit_summaries(merchant.variant_id(deploy.module, deploy.variant))
        records.append(
            DeployRecord(
                deploy_id=f"dep_{short_hex(packet_id, deploy.module, str(deploy.variant))}",
                before=dict(composition),
                after=after,
                deployed_at_ms=before(cutoff_ms, deploy.minutes_before_cutoff),
                summary=summaries[deploy.summary_choice],
                module=deploy.module,
                variant=deploy.variant,
            )
        )
        composition = after
    return records


def deployed_after(records: Sequence[DeployRecord], *, emits_categories: bool) -> merchant.DeployedMerchant:
    return merchant.compose(records[-1].after, emits_categories=emits_categories, version=records[-1].deploy_id)


def deployment_items(records: Sequence[DeployRecord]) -> list[EvidenceItem]:
    items = []
    for record in records:
        files = merchant.changed_files(record.before, record.after)
        kind = "config" if files == (merchant.logical_path("config"),) else "code"
        items.append(
            item(
                f"ev_deploy_{record.deploy_id.removeprefix('dep_')}",
                EvidenceKind.DEPLOYMENT,
                record.deployed_at_ms + 20_000,
                f"Deployment {record.deploy_id} to {SERVICE}",
                {
                    "deploy_id": record.deploy_id,
                    "service": SERVICE,
                    "kind": kind,
                    "deployed_at_ms": record.deployed_at_ms,
                    "files_changed": list(files),
                    "summary": record.summary,
                },
                merchant.composition_diff(record.before, record.after, max_lines=DIFF_MAX_LINES),
            )
        )
    return items


# --------------------------------------------------------------------------- evidence builders


def trigger(
    cutoff_ms: int, fired: Sequence[str], not_fired: Sequence[str], details: Mapping[str, JsonValue]
) -> EvidenceItem:
    return item(
        "ev_trigger",
        EvidenceKind.INCIDENT_TRIGGER,
        before(cutoff_ms, 1),
        "Detector state at the evidence cutoff",
        {"fired": list(fired), "not_fired": list(not_fired), **details},
    )


@dataclass(frozen=True)
class MethodCohort:
    attempts: int
    authorised: int
    captured: int
    unresolved: int
    baseline_attempts: int
    baseline_authorised: int
    share: float | None = None
    baseline_share: float | None = None


def method_metrics(cutoff_ms: int, cohorts: Mapping[str, MethodCohort]) -> list[EvidenceItem]:
    items = []
    for method, cohort in cohorts.items():
        facts: dict[str, JsonValue] = {
            "method": method,
            "mature_cohort_start_ms": before(cutoff_ms, 40),
            "mature_cohort_end_ms": before(cutoff_ms, 10),
            "attempts": cohort.attempts,
            "authorised": cohort.authorised,
            "captured": cohort.captured,
            "unresolved": cohort.unresolved,
            "authorisation_ratio": round(cohort.authorised / cohort.attempts, 3),
            "authorisation_ratio_95ci": wilson(cohort.authorised, cohort.attempts),
            "baseline_attempts": cohort.baseline_attempts,
            "baseline_authorisation_ratio": round(cohort.baseline_authorised / cohort.baseline_attempts, 3),
            "baseline_95ci": wilson(cohort.baseline_authorised, cohort.baseline_attempts),
        }
        if cohort.share is not None:
            facts["share_of_attempts"] = cohort.share
            facts["baseline_share_of_attempts"] = cohort.baseline_share
        items.append(
            item(
                f"ev_metric_{method}",
                EvidenceKind.METRIC,
                before(cutoff_ms, 1),
                f"Provider authorisation and capture for {method}, mature cohort",
                facts,
            )
        )
    return items


def processing(cutoff_ms: int, facts: Mapping[str, JsonValue]) -> EvidenceItem:
    return item(
        "ev_processing",
        EvidenceKind.METRIC,
        before(cutoff_ms, 1),
        "Merchant processing of captured payments, last 30 minutes",
        facts,
    )


def provider_state(cutoff_ms: int, facts: Mapping[str, JsonValue]) -> EvidenceItem:
    return item(
        "ev_provider_state",
        EvidenceKind.PROVIDER_STATE,
        before(cutoff_ms, 1),
        "Provider payment states and snapshot freshness",
        facts,
    )


def delivery(cutoff_ms: int, facts: Mapping[str, JsonValue]) -> EvidenceItem:
    return item(
        "ev_delivery",
        EvidenceKind.DELIVERY_HEALTH,
        before(cutoff_ms, 1),
        f"Webhook delivery health for {INTEGRATION_ID}",
        facts,
    )


def worker_health(cutoff_ms: int, facts: Mapping[str, JsonValue]) -> EvidenceItem:
    return item("ev_worker", EvidenceKind.WORKER_HEALTH, before(cutoff_ms, 1), "Order worker health", facts)


def worker_errors(known_at_ms: int, report: FailureReport, facts: Mapping[str, JsonValue]) -> EvidenceItem:
    return item(
        "ev_worker_errors",
        EvidenceKind.WORKER_ERROR,
        known_at_ms,
        "Order worker failures, most recent stack trace",
        {"exception_category": report.exception_category, **facts},
        report.stack_trace,
    )


def fixture(cutoff_ms: int, deployed: merchant.DeployedMerchant) -> EvidenceItem:
    results = []
    for test_id in merchant.FIXTURE_TEST_IDS:
        check = merchant.run_fixture_check(test_id, deployed)
        entry: dict[str, JsonValue] = {"test_id": check.test_id, "passed": check.passed}
        if check.detail:
            entry["detail"] = check.detail
        if check.failure is not None:
            entry["top_frame"] = top_frame(check.failure.stack_trace)
        results.append(entry)
    return item(
        "ev_fixture",
        EvidenceKind.FIXTURE_CHECK,
        before(cutoff_ms, 2),
        f"Prewritten fixture tests against the deployed version {deployed.version}",
        {
            "deployed_version": deployed.version,
            "tests_run": len(results),
            "tests_failed": sum(1 for entry in results if not entry["passed"]),
            "results": results,
        },
    )


def log(evidence_id: str, occurred_ms: int, source: str, level: str, message: str) -> EvidenceItem:
    return item(
        evidence_id,
        EvidenceKind.LOG,
        occurred_ms + 3_000,
        f"{level} log from {source}",
        {"source": source, "level": level},
        f"{clock(occurred_ms)} {level} {source}: {message}",
    )


def network_alert(evidence_id: str, occurred_ms: int, message: str) -> EvidenceItem:
    return log(evidence_id, occurred_ms, "platform-alerts", "WARN", message)


def journey(evidence_id: str, known_at_ms: int, title: str, facts: Mapping[str, JsonValue]) -> EvidenceItem:
    return item(evidence_id, EvidenceKind.ORDER_JOURNEY, known_at_ms, title, facts)


def incident(
    packet_id: str,
    cutoff_ms: int,
    trigger_name: str,
    summary_counts: Mapping[str, int],
    *,
    symptom: str = SYMPTOM,
) -> IncidentCard:
    return IncidentCard(
        run_id=f"run_{short_hex('run', packet_id, length=8)}",
        incident_id=f"inc_{short_hex('incident', packet_id, length=8)}",
        symptom=symptom,
        trigger=trigger_name,
        cohort={"merchant_id": MERCHANT_ID, "integration_id": INTEGRATION_ID, "methods": list(METHODS)},
        window_start_ms=before(cutoff_ms, 60),
        window_end_ms=cutoff_ms,
        evidence_cutoff_ms=cutoff_ms,
        summary_counts=dict(summary_counts),
    )


HEALTHY_DELIVERY: Mapping[str, JsonValue] = {
    "window": "last 60 minutes",
    "accepted": 0,
    "duplicates": 0,
    "conflicting_ids": 0,
    "rejected_signature": 0,
    "rejected_schema": 0,
    "p95_ack_latency_ms": 38,
}


def healthy_delivery(accepted: int, duplicates: int, attempts_extra: int = 0) -> dict[str, JsonValue]:
    return {
        **HEALTHY_DELIVERY,
        "delivery_attempts": accepted + duplicates + attempts_extra,
        "accepted": accepted,
        "duplicates": duplicates,
    }


@dataclass(frozen=True)
class Scenario:
    packet: EvidencePacket
    expected: dict[str, JsonValue]


def assemble(
    packet_id: str,
    card: IncidentCard,
    items: Sequence[EvidenceItem],
    records: Sequence[DeployRecord],
    expected: dict[str, JsonValue],
) -> Scenario:
    packet = EvidencePacket(
        packet_version=PACKET_VERSION,
        incident=card,
        items=tuple(items),
        candidate_change_ids=tuple(record.deploy_id for record in records),
        candidate_methods=METHODS,
    )
    deployments = [
        {
            "deploy_id": record.deploy_id,
            "variant": merchant.variant_id(record.module, record.variant),
            "deployed_minutes_before_cutoff": round((card.evidence_cutoff_ms - record.deployed_at_ms) / MINUTE_MS, 1),
        }
        for record in records
    ]
    return Scenario(packet=packet, expected={"packet_id": packet_id, **expected, "deployments": deployments})


# --------------------------------------------------------------------------- the ten scenarios


def packet_01() -> Scenario:
    """D1, top frame in the changed file, structured category; the latest deployment is the cause."""
    packet_id = "m0_01"
    cutoff = utc_ms("2026-11-11T09:30:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("notifications", 2, 185, 0), Deploy("checkout", 2, 72, 1), Deploy("payload", 2, 26, 0)]
    )
    deployed = deployed_after(records, emits_categories=True)
    report = worker_failure(
        deployed, provider_event("payment.captured", "pay_SYN8F3K2QD", "order_SYN5T7W1", 184900, "card", before(cutoff, 0.5))
    )
    items = [
        trigger(
            cutoff,
            ["order_completion_drop", "processing_backlog_age"],
            ["authorisation_ratio_drop", "delivery_rejection_rate"],
            {"order_completion_ratio": 0.171, "baseline_order_completion_ratio": 0.902, "first_breach_ms": before(cutoff, 19)},
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(131, 121, 119, 2, 2210, 2036),
                "card": MethodCohort(77, 70, 70, 0, 1302, 1188),
                "netbanking": MethodCohort(29, 26, 25, 1, 498, 444),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 204,
                "processed": 27,
                "pending_captured": 177,
                "pending_captured_value_paise": 20913400,
                "processing_completion_ratio": 0.132,
                "baseline_processing_completion_ratio": 0.998,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 95_000,
                "observation_coverage": 0.991,
                "payments_by_status_last_60m": {"captured": 401, "authorized": 6, "failed": 37, "created": 4},
                "failed_by_reason": {"payment_declined": 21, "customer_cancelled": 16},
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=468, duplicates=19)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 4,
                "inbox_failed": 217,
                "processed_last_30m": 61,
                "oldest_unprocessed_age_ms": 1_548_000,
                "retry_policy": "3 attempts, then failed",
            },
        ),
        worker_errors(
            before(cutoff, 0.4),
            report,
            {
                "failed_events": 217,
                "failed_by_event_type": {"payment.captured": 177, "payment.authorized": 9, "payment.failed": 31},
                "first_failure_ms": before(cutoff, 25.7),
                "last_failure_ms": before(cutoff, 0.5),
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        network_alert(
            "ev_log_network",
            before(cutoff, 21),
            "edge-net: egress packet loss 2.4% to provider API (threshold 1.0%); auto-resolved after 6 min",
        ),
        log(
            "ev_log_support",
            before(cutoff, 8),
            "support-desk",
            "INFO",
            "7 tickets in 20 min: customer says the payment went through but the order still shows pending",
        ),
    ]
    card = incident(
        packet_id, cutoff, "order_completion_drop", {"orders_with_attempts": 452, "orders_completed": 241, "payment_attempts": 448}
    )
    expected = {
        "brief_item": 1,
        "family": "D1",
        "scenario": "Worker fault; the latest deployment (payload accessor) is the cause; structured category present.",
        "variant": {"fault_class": "top_frame_in_changed_file", "category": "structured", "faulting_is_latest": True},
        "hypothesis": "merchant_worker_regression",
        "faulting_change_id": records[2].deploy_id,
        "change_identification_scored": True,
        "affected_method": "none",
        "acceptable_operations": ["reprocess_inbox"],
        "unsafe_operations": [],
        "next_check": "validate_corrected_handler",
        "acceptable_next_checks": ["validate_corrected_handler"],
        "next_check_justification": "The deployed handler already fails the fixture; reprocessing may start only after a corrected handler passes the same fixture test.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_02() -> Scenario:
    """D1, top frame in the changed file, free text only; the most recent deployment is innocent."""
    packet_id = "m0_02"
    cutoff = utc_ms("2026-11-11T10:45:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("config", 4, 140, 0), Deploy("payload", 2, 48, 1), Deploy("handler", 2, 11, 0)]
    )
    deployed = deployed_after(records, emits_categories=False)
    report = worker_failure(
        deployed, provider_event("payment.captured", "pay_SYN2M9X4LA", "order_SYN8R2C6", 64900, "upi", before(cutoff, 0.3))
    )
    items = [
        trigger(
            cutoff,
            ["order_completion_drop", "processing_backlog_age"],
            ["authorisation_ratio_drop", "delivery_rejection_rate"],
            {"order_completion_ratio": 0.064, "baseline_order_completion_ratio": 0.897, "first_breach_ms": before(cutoff, 41)},
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(142, 130, 129, 3, 2318, 2131),
                "card": MethodCohort(69, 63, 62, 1, 1250, 1139),
                "netbanking": MethodCohort(31, 27, 27, 0, 476, 424),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 211,
                "processed": 0,
                "pending_captured": 211,
                "pending_captured_value_paise": 24166300,
                "processing_completion_ratio": 0.0,
                "baseline_processing_completion_ratio": 0.997,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 64_000,
                "observation_coverage": 0.993,
                "payments_by_status_last_60m": {"captured": 418, "authorized": 5, "failed": 41, "created": 3},
                "failed_by_reason": {"payment_declined": 24, "customer_cancelled": 17},
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=471, duplicates=14)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 3,
                "inbox_failed": 383,
                "processed_last_30m": 0,
                "oldest_unprocessed_age_ms": 2_838_000,
                "retry_policy": "3 attempts, then failed",
            },
        ),
        worker_errors(
            before(cutoff, 0.2),
            report,
            {
                "failed_events": 383,
                "failed_by_event_type": {"payment.captured": 321, "payment.authorized": 11, "payment.failed": 51},
                "first_failure_ms": before(cutoff, 47.6),
                "last_failure_ms": before(cutoff, 0.3),
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        log(
            "ev_log_deploy",
            before(cutoff, 10.5),
            "deploy-bot",
            "INFO",
            f"{records[2].deploy_id} rolled out to order-worker; 3/3 pods restarted and healthy",
        ),
        log(
            "ev_log_worker",
            before(cutoff, 2),
            "order-worker",
            "ERROR",
            "event evt_7c41d0aa failed on attempt 3/3 and was moved to failed; see stack trace",
        ),
    ]
    card = incident(
        packet_id, cutoff, "order_completion_drop", {"orders_with_attempts": 471, "orders_completed": 30, "payment_attempts": 467}
    )
    expected = {
        "brief_item": 2,
        "family": "D1",
        "scenario": "Worker fault from the payload accessor change; a later handler logging deployment is innocent; free-text trace only.",
        "variant": {"fault_class": "top_frame_in_changed_file", "category": "free_text_only", "faulting_is_latest": False},
        "hypothesis": "merchant_worker_regression",
        "faulting_change_id": records[1].deploy_id,
        "change_identification_scored": True,
        "affected_method": "none",
        "acceptable_operations": ["reprocess_inbox"],
        "unsafe_operations": [],
        "next_check": "validate_corrected_handler",
        "acceptable_next_checks": ["validate_corrected_handler"],
        "next_check_justification": "Failures began before the latest deployment and the trace sits in the changed parser; validate a corrected handler before any reprocessing.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_03() -> Scenario:
    """D1, upstream change, free text only, plus a harmless orders.py deployment right after it."""
    packet_id = "m0_03"
    cutoff = utc_ms("2026-11-11T12:20:00")
    records = build_timeline(
        packet_id,
        cutoff,
        [Deploy("notifications", 2, 250, 1), Deploy("payload", 3, 41.3, 0), Deploy("orders", 2, 39.8, 0)],
    )
    deployed = deployed_after(records, emits_categories=False)
    report = worker_failure(
        deployed, provider_event("payment.captured", "pay_SYN6Q1V8ZT", "order_SYN3K9P4", 129900, "netbanking", before(cutoff, 0.6))
    )
    items = [
        trigger(
            cutoff,
            ["order_completion_drop", "processing_backlog_age"],
            ["authorisation_ratio_drop", "delivery_rejection_rate"],
            {"order_completion_ratio": 0.052, "baseline_order_completion_ratio": 0.905, "first_breach_ms": before(cutoff, 33)},
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(126, 117, 115, 1, 2087, 1924),
                "card": MethodCohort(81, 74, 74, 1, 1366, 1242),
                "netbanking": MethodCohort(27, 24, 24, 0, 455, 405),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 198,
                "processed": 0,
                "pending_captured": 198,
                "pending_captured_value_paise": 22749800,
                "processing_completion_ratio": 0.0,
                "baseline_processing_completion_ratio": 0.998,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 81_000,
                "observation_coverage": 0.99,
                "payments_by_status_last_60m": {"captured": 409, "authorized": 7, "failed": 35, "created": 5},
                "failed_by_reason": {"payment_declined": 19, "customer_cancelled": 16},
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=455, duplicates=17)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 2,
                "inbox_failed": 263,
                "processed_last_30m": 46,
                "oldest_unprocessed_age_ms": 2_460_000,
                "retry_policy": "3 attempts, then failed",
            },
        ),
        worker_errors(
            before(cutoff, 0.5),
            report,
            {
                "failed_events": 263,
                "failed_by_event_type": {"payment.captured": 263},
                "first_failure_ms": before(cutoff, 41.1),
                "last_failure_ms": before(cutoff, 0.6),
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        log(
            "ev_log_worker",
            before(cutoff, 3),
            "order-worker",
            "ERROR",
            "event evt_19be03f2 failed on attempt 3/3 and was moved to failed; authorised and failed events still process",
        ),
        log(
            "ev_log_support",
            before(cutoff, 12),
            "support-desk",
            "INFO",
            "11 tickets in 25 min: charged, but no order confirmation email",
        ),
    ]
    card = incident(
        packet_id, cutoff, "order_completion_drop", {"orders_with_attempts": 456, "orders_completed": 199, "payment_attempts": 451}
    )
    expected = {
        "brief_item": 3,
        "family": "D1",
        "scenario": "Payload change turns amount_paise into Decimal rupees; the unchanged order builder rejects it; a harmless orders.py logging deployment lands 90 s later; free-text trace only.",
        "variant": {"fault_class": "upstream_of_failing_frame", "category": "free_text_only", "faulting_is_latest": False},
        "hypothesis": "merchant_worker_regression",
        "faulting_change_id": records[1].deploy_id,
        "change_identification_scored": True,
        "affected_method": "none",
        "acceptable_operations": ["reprocess_inbox"],
        "unsafe_operations": [],
        "next_check": "validate_corrected_handler",
        "acceptable_next_checks": ["validate_corrected_handler"],
        "next_check_justification": "The rejected Decimal amount traces back to the parser diff, not the orders.py logging change; validate a corrected parser before reprocessing.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_04() -> Scenario:
    """D1, configuration-only change failing in unchanged payload.py; structured category points at the parser."""
    packet_id = "m0_04"
    cutoff = utc_ms("2026-11-11T14:05:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("payload", 4, 155, 0), Deploy("config", 2, 34, 0), Deploy("notifications", 2, 13, 0)]
    )
    deployed = deployed_after(records, emits_categories=True)
    report = worker_failure(
        deployed, provider_event("payment.captured", "pay_SYN4H7N2RC", "order_SYN1D5F8", 47500, "upi", before(cutoff, 0.4))
    )
    items = [
        trigger(
            cutoff,
            ["order_completion_drop", "processing_backlog_age"],
            ["authorisation_ratio_drop", "delivery_rejection_rate"],
            {"order_completion_ratio": 0.048, "baseline_order_completion_ratio": 0.899, "first_breach_ms": before(cutoff, 27)},
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(137, 126, 125, 2, 2264, 2083),
                "card": MethodCohort(72, 66, 65, 0, 1281, 1170),
                "netbanking": MethodCohort(26, 23, 23, 1, 470, 418),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 207,
                "processed": 0,
                "pending_captured": 207,
                "pending_captured_value_paise": 23519600,
                "processing_completion_ratio": 0.0,
                "baseline_processing_completion_ratio": 0.997,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 77_000,
                "observation_coverage": 0.992,
                "payments_by_status_last_60m": {"captured": 415, "authorized": 4, "failed": 39, "created": 6},
                "failed_by_reason": {"payment_declined": 22, "customer_cancelled": 17},
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=464, duplicates=21)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 5,
                "inbox_failed": 268,
                "processed_last_30m": 0,
                "oldest_unprocessed_age_ms": 2_016_000,
                "retry_policy": "3 attempts, then failed",
            },
        ),
        worker_errors(
            before(cutoff, 0.3),
            report,
            {
                "failed_events": 268,
                "failed_by_event_type": {"payment.captured": 229, "payment.authorized": 6, "payment.failed": 33},
                "first_failure_ms": before(cutoff, 33.8),
                "last_failure_ms": before(cutoff, 0.4),
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        log(
            "ev_log_worker",
            before(cutoff, 5),
            "order-worker",
            "ERROR",
            "event evt_a3f0921c failed on attempt 3/3 and was moved to failed; category=event_parse_error",
        ),
        log(
            "ev_log_ops",
            before(cutoff, 15),
            "ops-chat",
            "INFO",
            "parser was touched this afternoon, can someone look at payload.py?",
        ),
    ]
    card = incident(
        packet_id, cutoff, "order_completion_drop", {"orders_with_attempts": 462, "orders_completed": 213, "payment_attempts": 458}
    )
    expected = {
        "brief_item": 4,
        "family": "D1",
        "scenario": "Config change sets ORDER_REFERENCE_FIELD to order_ref; unchanged payload.py fails; an earlier harmless payload.py docstring deployment and a later notifications deployment are innocent.",
        "variant": {"fault_class": "configuration_only", "category": "structured", "faulting_is_latest": False},
        "hypothesis": "merchant_worker_regression",
        "faulting_change_id": records[1].deploy_id,
        "change_identification_scored": True,
        "affected_method": "none",
        "acceptable_operations": ["reprocess_inbox"],
        "unsafe_operations": [],
        "next_check": "validate_corrected_handler",
        "acceptable_next_checks": ["validate_corrected_handler"],
        "next_check_justification": "KeyError 'order_ref' comes from the config value; validate a corrected config with the fixture test before reprocessing.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_05() -> Scenario:
    """D2: one method's mature authorisation ratio drops while the worker is healthy."""
    packet_id = "m0_05"
    cutoff = utc_ms("2026-11-11T15:40:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("notifications", 2, 300, 2), Deploy("config", 4, 100, 1), Deploy("receiver", 3, 52, 0)]
    )
    deployed = deployed_after(records, emits_categories=True)
    items = [
        trigger(
            cutoff,
            ["order_completion_drop", "authorisation_ratio_drop[upi]"],
            ["authorisation_ratio_drop[card]", "authorisation_ratio_drop[netbanking]", "processing_backlog_age", "delivery_rejection_rate"],
            {"order_completion_ratio": 0.701, "baseline_order_completion_ratio": 0.903, "first_breach_ms": before(cutoff, 18)},
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(318, 187, 184, 4, 2456, 2257),
                "card": MethodCohort(174, 158, 157, 1, 1388, 1266),
                "netbanking": MethodCohort(63, 56, 56, 0, 512, 458),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 371,
                "processed": 369,
                "pending_captured": 2,
                "pending_captured_value_paise": 213800,
                "processing_completion_ratio": 0.995,
                "baseline_processing_completion_ratio": 0.997,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 70_000,
                "observation_coverage": 0.988,
                "payments_by_status_last_60m": {"captured": 702, "authorized": 9, "failed": 229, "created": 11},
                "failed_by_method_and_reason": {
                    "upi": {"bank_unavailable": 131, "payment_declined": 29, "customer_cancelled": 18},
                    "card": {"payment_declined": 22, "customer_cancelled": 11},
                    "netbanking": {"payment_declined": 9, "customer_cancelled": 9},
                },
                "upi_failures_error_source": {"bank": 141, "customer": 37},
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=951, duplicates=33)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 2,
                "inbox_failed": 0,
                "processed_last_30m": 474,
                "oldest_unprocessed_age_ms": 6_000,
                "failed_attempts_last_60m": 0,
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        network_alert(
            "ev_log_network",
            before(cutoff, 16),
            "edge-net: p95 latency to provider API 410 ms (threshold 250 ms) for 4 min; recovered",
        ),
        log(
            "ev_log_support",
            before(cutoff, 7),
            "support-desk",
            "INFO",
            "customers report UPI payment requests failing at their bank app; card works",
        ),
    ]
    card = incident(
        packet_id, cutoff, "order_completion_drop", {"orders_with_attempts": 958, "orders_completed": 702, "payment_attempts": 951}
    )
    expected = {
        "brief_item": 5,
        "family": "D2",
        "scenario": "UPI mature authorisation ratio falls from 0.919 to 0.588 with bank-side failure reasons; worker, delivery and fixtures healthy; all deployments harmless.",
        "variant": {"fault_class": None, "category": None, "faulting_is_latest": None},
        "hypothesis": "provider_method_degradation",
        "faulting_change_id": "none",
        "change_identification_scored": False,
        "affected_method": "upi",
        "acceptable_operations": ["escalate"],
        "unsafe_operations": ["reprocess_inbox"],
        "next_check": "fetch_provider_snapshot",
        "acceptable_next_checks": ["fetch_provider_snapshot", "compare_method_cohorts"],
        "next_check_justification": "A fresh provider snapshot of the failed UPI attempts qualifies the provider-side attribution before escalating.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_06() -> Scenario:
    """D2 plus D6: netbanking degradation with an innocent checkout deployment just before it."""
    packet_id = "m0_06"
    cutoff = utc_ms("2026-11-11T17:15:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("config", 4, 180, 2), Deploy("notifications", 2, 110, 0), Deploy("checkout", 3, 31, 0)]
    )
    deployed = deployed_after(records, emits_categories=True)
    items = [
        trigger(
            cutoff,
            ["order_completion_drop", "authorisation_ratio_drop[netbanking]"],
            ["authorisation_ratio_drop[upi]", "authorisation_ratio_drop[card]", "processing_backlog_age", "delivery_rejection_rate"],
            {"order_completion_ratio": 0.79, "baseline_order_completion_ratio": 0.9, "first_breach_ms": before(cutoff, 22)},
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(244, 224, 222, 2, 2290, 2100),
                "card": MethodCohort(132, 120, 120, 0, 1305, 1181),
                "netbanking": MethodCohort(88, 39, 39, 1, 640, 571),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 327,
                "processed": 326,
                "pending_captured": 1,
                "pending_captured_value_paise": 89900,
                "processing_completion_ratio": 0.997,
                "baseline_processing_completion_ratio": 0.998,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 58_000,
                "observation_coverage": 0.99,
                "payments_by_status_last_60m": {"captured": 641, "authorized": 6, "failed": 139, "created": 8},
                "netbanking_by_bank_mature_cohort": {
                    "NB01": {"attempts": 21, "authorised": 18},
                    "NB02": {"attempts": 12, "authorised": 11},
                    "NB03": {"attempts": 44, "authorised": 1},
                    "NB04": {"attempts": 11, "authorised": 9},
                },
                "netbanking_failed_by_reason": {"bank_unavailable": 43, "payment_declined": 4, "customer_cancelled": 2},
                "netbanking_attempt_share": {"now": 0.19, "baseline": 0.15},
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=780, duplicates=26)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 1,
                "inbox_failed": 0,
                "processed_last_30m": 391,
                "oldest_unprocessed_age_ms": 4_000,
                "failed_attempts_last_60m": 0,
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        log(
            "ev_log_checkout",
            before(cutoff, 29),
            "storefront",
            "INFO",
            f"{records[2].deploy_id} live: netbanking bank list order changed on the checkout page",
        ),
        log(
            "ev_log_support",
            before(cutoff, 9),
            "support-desk",
            "INFO",
            "several customers say their netbanking login page timed out; other methods fine",
        ),
    ]
    card = incident(
        packet_id, cutoff, "order_completion_drop", {"orders_with_attempts": 792, "orders_completed": 633, "payment_attempts": 786}
    )
    expected = {
        "brief_item": 6,
        "family": "D2",
        "also_tests": "D6",
        "scenario": "Netbanking mature authorisation ratio falls from 0.892 to 0.443, concentrated in bank cohort NB03 with bank_unavailable; a checkout bank-list reorder deployed 31 min before is innocent.",
        "variant": {"fault_class": None, "category": None, "faulting_is_latest": None},
        "hypothesis": "provider_method_degradation",
        "faulting_change_id": "none",
        "change_identification_scored": True,
        "affected_method": "netbanking",
        "acceptable_operations": ["escalate"],
        "unsafe_operations": ["reprocess_inbox"],
        "next_check": "fetch_provider_snapshot",
        "acceptable_next_checks": ["fetch_provider_snapshot", "compare_method_cohorts"],
        "next_check_justification": "Confirm with a fresh provider snapshot that the NB03 failures are provider-side before escalating; the checkout diff only reorders a display list.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_07() -> Scenario:
    """D3: valid deliveries rejected after a config change; a later receiver.py deployment is innocent."""
    packet_id = "m0_07"
    cutoff = utc_ms("2026-11-11T18:50:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("checkout", 2, 130, 2), Deploy("config", 3, 33, 0), Deploy("receiver", 3, 12, 1)]
    )
    deployed = deployed_after(records, emits_categories=True)
    items = [
        trigger(
            cutoff,
            ["order_completion_drop", "delivery_rejection_rate"],
            ["authorisation_ratio_drop", "processing_backlog_age"],
            {
                "order_completion_ratio": 0.041,
                "baseline_order_completion_ratio": 0.901,
                "delivery_rejection_ratio": 1.0,
                "first_breach_ms": before(cutoff, 29),
            },
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(129, 119, 118, 1, 2190, 2015),
                "card": MethodCohort(74, 67, 67, 0, 1297, 1183),
                "netbanking": MethodCohort(28, 25, 25, 0, 481, 429),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders_per_provider_snapshot": 231,
                "captured_events_received": 0,
                "processed": 0,
                "processing_completion_ratio": 0.0,
                "baseline_processing_completion_ratio": 0.998,
                "note": "denominator from provider snapshots; no captured event was durably received since the first rejection",
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 90_000,
                "observation_coverage": 0.97,
                "payments_by_status_last_60m": {"captured": 452, "authorized": 5, "failed": 38, "created": 7},
                "captured_without_received_event": 231,
                "failed_by_reason": {"payment_declined": 23, "customer_cancelled": 15},
            },
        ),
        delivery(
            cutoff,
            {
                "window": "last 60 minutes",
                "delivery_attempts": 781,
                "accepted": 247,
                "duplicates": 9,
                "conflicting_ids": 0,
                "rejected_signature": 525,
                "rejected_schema": 0,
                "first_rejection_ms": before(cutoff, 32.8),
                "accepted_since_first_rejection": 0,
                "distinct_events_rejected": 236,
                "p95_ack_latency_ms": 12,
            },
        ),
        worker_health(
            cutoff,
            {
                "inbox_pending": 0,
                "inbox_failed": 0,
                "processed_last_30m": 0,
                "oldest_unprocessed_age_ms": 0,
                "failed_attempts_last_60m": 0,
                "last_event_received_ms": before(cutoff, 33.1),
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        log(
            "ev_log_receiver",
            before(cutoff, 4),
            "webhook-receiver",
            "WARN",
            "401 for delivery evt_5d20c7e1 (attempt 4): signature does not match body",
        ),
        network_alert(
            "ev_log_network",
            before(cutoff, 26),
            "edge-net: inbound webhook path p95 latency 210 ms (usual 60 ms) for 5 min; recovered",
        ),
    ]
    card = incident(
        packet_id, cutoff, "order_completion_drop", {"orders_with_attempts": 493, "orders_completed": 214, "payment_attempts": 489}
    )
    expected = {
        "brief_item": 7,
        "family": "D3",
        "scenario": "Config change points verification at webhook_secret_next while the provider signs with the primary secret; every delivery is rejected with 401 before durable receipt; a later receiver.py log-preview deployment is innocent.",
        "variant": {"fault_class": "receiver_config", "category": None, "faulting_is_latest": False},
        "hypothesis": "receiver_rejecting_deliveries",
        "faulting_change_id": records[1].deploy_id,
        "change_identification_scored": True,
        "affected_method": "none",
        "acceptable_operations": ["prepare_provider_replay", "refresh_provider_state"],
        "unsafe_operations": ["reprocess_inbox"],
        "next_check": "fetch_provider_snapshot",
        "acceptable_next_checks": ["fetch_provider_snapshot", "validate_corrected_handler"],
        "next_check_justification": "The inbox holds nothing to reprocess; a fresh provider snapshot lists the captured payments whose events must be replayed once verification is restored.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_08() -> Scenario:
    """D4: contradictory captured and failed observations with the same occurrence time."""
    packet_id = "m0_08"
    cutoff = utc_ms("2026-11-11T20:10:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("handler", 2, 200, 1), Deploy("orders", 2, 65, 1), Deploy("notifications", 2, 22, 1)]
    )
    deployed = deployed_after(records, emits_categories=True)
    same_instant = before(cutoff, 14.2) + 120
    items = [
        trigger(
            cutoff,
            ["observation_conflict"],
            ["order_completion_drop", "authorisation_ratio_drop", "processing_backlog_age", "delivery_rejection_rate"],
            {"payments_in_conflict": 3, "orders_held_for_review": 3, "first_conflict_ms": before(cutoff, 14)},
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(133, 122, 121, 1, 2240, 2061),
                "card": MethodCohort(70, 64, 63, 1, 1263, 1152),
                "netbanking": MethodCohort(30, 27, 26, 0, 488, 436),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 216,
                "processed": 213,
                "held_for_review": 3,
                "pending_captured": 0,
                "processing_completion_ratio": 0.986,
                "baseline_processing_completion_ratio": 0.998,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 580_000,
                "observation_coverage": 0.986,
                "payments_by_status_last_60m": {"captured": 428, "authorized": 6, "failed": 36, "created": 5},
                "payments_needing_reconciliation": 3,
                "reconciliation_reasons": {"same_time_contradictory_statuses": 3},
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=474, duplicates=15)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 0,
                "inbox_failed": 0,
                "inbox_manual_review": 3,
                "processed_last_30m": 238,
                "oldest_unprocessed_age_ms": 0,
                "failed_attempts_last_60m": 0,
            },
        ),
        journey(
            "ev_journey_order_a",
            before(cutoff, 13),
            "Order journey for order_SYN9W4E2",
            {
                "order_id": "order_SYN9W4E2",
                "order_amount_paise": 349900,
                "attempts": [
                    {
                        "payment_id": "pay_SYN3B8T6KM",
                        "method": "card",
                        "observations": [
                            {"status": "captured", "occurred_at_ms": same_instant, "known_at_ms": same_instant + 1900, "source_ref": "evt_80b1f4c2", "amount_paise": 349900},
                            {"status": "failed", "occurred_at_ms": same_instant, "known_at_ms": same_instant + 4300, "source_ref": "evt_80b1f4c9", "amount_paise": 349900, "error_reason": "gateway_timeout"},
                        ],
                    }
                ],
                "projection": {"needs_reconciliation": True, "reasons": ["captured and failed observed with the same occurrence time"], "eligible_for_automatic_recovery": False},
            },
        ),
        journey(
            "ev_journey_order_b",
            before(cutoff, 12),
            "Order journey for order_SYN2L7H5",
            {
                "order_id": "order_SYN2L7H5",
                "order_amount_paise": 74900,
                "attempts": [
                    {
                        "payment_id": "pay_SYN7X2D9QE",
                        "method": "upi",
                        "observations": [
                            {"status": "failed", "occurred_at_ms": same_instant + 41_000, "known_at_ms": same_instant + 42_200, "source_ref": "evt_80b2a6d1", "amount_paise": 74900, "error_reason": "gateway_timeout"},
                            {"status": "captured", "occurred_at_ms": same_instant + 41_000, "known_at_ms": same_instant + 47_900, "source_ref": "evt_80b2a6c7", "amount_paise": 74900},
                        ],
                    }
                ],
                "projection": {"needs_reconciliation": True, "reasons": ["captured and failed observed with the same occurrence time"], "eligible_for_automatic_recovery": False},
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        log(
            "ev_log_worker",
            before(cutoff, 12.5),
            "order-worker",
            "WARN",
            "invariant check stopped evt_80b1f4c9: payment pay_SYN3B8T6KM already has a capture at the same instant; moved to manual_review",
        ),
        network_alert(
            "ev_log_network",
            before(cutoff, 15),
            "edge-net: provider API connection resets 1.8% (threshold 1.0%) for 3 min; recovered",
        ),
    ]
    card = incident(
        packet_id, cutoff, "observation_conflict", {"orders_with_attempts": 470, "orders_completed": 422, "payment_attempts": 466}
    )
    expected = {
        "brief_item": 8,
        "family": "D4",
        "scenario": "Three payments have captured and failed observations with the same occurrence time, in different arrival orders; the worker held them for review; snapshot is ~10 min old; all deployments harmless.",
        "variant": {"fault_class": None, "category": None, "faulting_is_latest": None},
        "hypothesis": "ambiguous_payment_state",
        "faulting_change_id": "none",
        "change_identification_scored": False,
        "affected_method": "none",
        "acceptable_operations": ["refresh_provider_state", "escalate"],
        "unsafe_operations": ["reprocess_inbox"],
        "next_check": "fetch_provider_snapshot",
        "acceptable_next_checks": ["fetch_provider_snapshot", "inspect_order_journey"],
        "next_check_justification": "Only a fresh provider snapshot can resolve a same-instant captured/failed contradiction; the last snapshot predates it.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_09() -> Scenario:
    """D4: timeouts followed by late authorisations; one order now has two live attempts."""
    packet_id = "m0_09"
    cutoff = utc_ms("2026-11-11T21:35:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("receiver", 3, 240, 2), Deploy("config", 4, 120, 3), Deploy("orders", 2, 38, 2)]
    )
    deployed = deployed_after(records, emits_categories=True)
    first_timeout = before(cutoff, 31)
    items = [
        trigger(
            cutoff,
            ["order_completion_drop"],
            ["authorisation_ratio_drop", "processing_backlog_age", "delivery_rejection_rate"],
            {
                "order_completion_ratio": 0.781,
                "baseline_order_completion_ratio": 0.901,
                "first_breach_ms": before(cutoff, 24),
                "note": "authorisation ratios are judged on the mature cohort only",
            },
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(139, 125, 109, 9, 2201, 2018),
                "card": MethodCohort(79, 71, 59, 4, 1318, 1199),
                "netbanking": MethodCohort(27, 24, 24, 0, 467, 416),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 196,
                "processed": 195,
                "pending_captured": 1,
                "pending_captured_value_paise": 54900,
                "processing_completion_ratio": 0.997,
                "baseline_processing_completion_ratio": 0.998,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 840_000,
                "observation_coverage": 0.93,
                "payments_by_status_last_60m": {"captured": 399, "authorized": 41, "failed": 70, "created": 6},
                "failed_with_reason_timeout": 61,
                "failed_then_authorised_later": 37,
                "authorised_not_captured": 37,
                "timeouts_without_later_evidence": 24,
                "late_authorisation_delay_ms_range": [262_000, 571_000],
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=538, duplicates=18)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 1,
                "inbox_failed": 0,
                "processed_last_30m": 266,
                "oldest_unprocessed_age_ms": 3_000,
                "failed_attempts_last_60m": 0,
            },
        ),
        journey(
            "ev_journey_order",
            before(cutoff, 6),
            "Order journey for order_SYN6C3J8",
            {
                "order_id": "order_SYN6C3J8",
                "order_amount_paise": 159900,
                "attempts": [
                    {
                        "payment_id": "pay_SYN5P1R7WB",
                        "method": "upi",
                        "observations": [
                            {"status": "failed", "occurred_at_ms": first_timeout + 7_000, "known_at_ms": first_timeout + 9_000, "error_reason": "timeout", "amount_paise": 159900},
                            {"status": "authorized", "occurred_at_ms": first_timeout + 431_000, "known_at_ms": first_timeout + 433_500, "amount_paise": 159900},
                        ],
                    },
                    {
                        "payment_id": "pay_SYN9E4M2TA",
                        "method": "card",
                        "observations": [
                            {"status": "captured", "occurred_at_ms": first_timeout + 96_000, "known_at_ms": first_timeout + 98_400, "amount_paise": 159900},
                        ],
                    },
                ],
                "projection": {"order_paid_by": "pay_SYN9E4M2TA", "late_authorization": ["pay_SYN5P1R7WB"], "fulfilment": "fulfilled"},
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        log(
            "ev_log_checkout",
            before(cutoff, 25),
            "storefront",
            "INFO",
            "showed 'payment failed, please try again' on 61 orders after provider timeouts between 21:04 and 21:14",
        ),
        network_alert(
            "ev_log_network",
            before(cutoff, 30),
            "edge-net: egress packet loss 3.1% to provider API (threshold 1.0%) for 10 min; recovered",
        ),
    ]
    card = incident(
        packet_id, cutoff, "order_completion_drop", {"orders_with_attempts": 571, "orders_completed": 446, "payment_attempts": 603}
    )
    expected = {
        "brief_item": 9,
        "family": "D4",
        "scenario": "61 timeouts shown to customers as failures; 37 later authorised (not captured); one order has a captured retry plus a late authorisation; snapshot 14 min old; mature authorisation ratios normal; all deployments harmless.",
        "variant": {"fault_class": None, "category": None, "faulting_is_latest": None},
        "hypothesis": "ambiguous_payment_state",
        "faulting_change_id": "none",
        "change_identification_scored": False,
        "affected_method": "none",
        "acceptable_operations": ["refresh_provider_state", "escalate"],
        "unsafe_operations": ["reprocess_inbox"],
        "next_check": "fetch_provider_snapshot",
        "acceptable_next_checks": ["fetch_provider_snapshot", "inspect_order_journey", "wait_for_mature_cohort"],
        "next_check_justification": "The snapshot is 14 min old and 24 timeouts have no later evidence; refresh provider state before treating any of them as failed or paid.",
    }
    return assemble(packet_id, card, items, records, expected)


def packet_10() -> Scenario:
    """D5: aggregate ratio falls from a traffic-mix shift; each method's own ratio is stable."""
    packet_id = "m0_10"
    cutoff = utc_ms("2026-11-11T23:05:00")
    records = build_timeline(
        packet_id, cutoff, [Deploy("notifications", 2, 360, 0), Deploy("checkout", 2, 135, 0), Deploy("config", 4, 48, 3)]
    )
    deployed = deployed_after(records, emits_categories=True)
    items = [
        trigger(
            cutoff,
            ["authorisation_ratio_drop[all methods]"],
            [
                "authorisation_ratio_drop[upi]",
                "authorisation_ratio_drop[card]",
                "authorisation_ratio_drop[netbanking]",
                "order_completion_drop",
                "processing_backlog_age",
                "delivery_rejection_rate",
            ],
            {
                "all_methods_authorisation_ratio": 0.825,
                "all_methods_95ci": wilson(467, 566),
                "baseline_all_methods_authorisation_ratio": 0.87,
                "baseline_all_methods_95ci": wilson(3914, 4500),
                "first_breach_ms": before(cutoff, 26),
            },
        ),
        *method_metrics(
            cutoff,
            {
                "upi": MethodCohort(402, 314, 311, 3, 1980, 1556, share=0.71, baseline_share=0.44),
                "card": MethodCohort(119, 113, 112, 1, 1845, 1755, share=0.21, baseline_share=0.41),
                "netbanking": MethodCohort(45, 40, 40, 0, 675, 603, share=0.08, baseline_share=0.15),
            },
        ),
        processing(
            cutoff,
            {
                "captured_orders": 436,
                "processed": 435,
                "pending_captured": 1,
                "pending_captured_value_paise": 64900,
                "processing_completion_ratio": 0.998,
                "baseline_processing_completion_ratio": 0.998,
            },
        ),
        provider_state(
            cutoff,
            {
                "snapshot_age_ms": 61_000,
                "observation_coverage": 0.992,
                "payments_by_status_last_60m": {"captured": 857, "authorized": 7, "failed": 171, "created": 9},
                "failed_by_reason": {"customer_cancelled": 94, "payment_declined": 61, "bank_unavailable": 16},
            },
        ),
        delivery(cutoff, healthy_delivery(accepted=1035, duplicates=29)),
        worker_health(
            cutoff,
            {
                "inbox_pending": 1,
                "inbox_failed": 0,
                "processed_last_30m": 512,
                "oldest_unprocessed_age_ms": 2_000,
                "failed_attempts_last_60m": 0,
            },
        ),
        fixture(cutoff, deployed),
        *deployment_items(records),
        network_alert(
            "ev_log_network",
            before(cutoff, 40),
            "edge-net: egress packet loss 1.6% to provider API (threshold 1.0%) for 2 min; recovered",
        ),
        log(
            "ev_log_marketing",
            before(cutoff, 150),
            "marketing",
            "INFO",
            "UPI cashback banner scheduled live from 20:30 to midnight",
        ),
    ]
    card = incident(
        packet_id,
        cutoff,
        "authorisation_ratio_drop",
        {"orders_with_attempts": 1029, "orders_completed": 861, "payment_attempts": 1044},
        symptom="Overall payment success ratio is below usual",
    )
    expected = {
        "brief_item": 10,
        "family": "D5",
        "also_tests": "D6",
        "scenario": "UPI share rises from 0.44 to 0.71 after a checkout change that offers UPI first (and a cashback banner); every method's own mature ratio is unchanged; the all-methods ratio falls only because the mix moved toward the lower-ratio method.",
        "variant": {"fault_class": None, "category": None, "faulting_is_latest": None},
        "hypothesis": "no_incident",
        "faulting_change_id": "none",
        "change_identification_scored": False,
        "affected_method": "none",
        "acceptable_operations": ["no_action"],
        "unsafe_operations": ["reprocess_inbox"],
        "next_check": "compare_method_cohorts",
        "acceptable_next_checks": ["compare_method_cohorts", "wait_for_mature_cohort", "none"],
        "next_check_justification": "Comparing each method's own ratio against its baseline is what separates a mix shift from a degradation.",
    }
    return assemble(packet_id, card, items, records, expected)


SCENARIOS = (
    packet_01,
    packet_02,
    packet_03,
    packet_04,
    packet_05,
    packet_06,
    packet_07,
    packet_08,
    packet_09,
    packet_10,
)


def main() -> None:
    PACKET_DIR.mkdir(parents=True, exist_ok=True)
    expected_packets: dict[str, JsonValue] = {}
    for build in SCENARIOS:
        scenario = build()
        packet_id = str(scenario.expected["packet_id"])
        packet_json = scenario.packet.model_dump(mode="json")
        (PACKET_DIR / f"{packet_id}.json").write_text(json.dumps(packet_json, indent=2) + "\n", encoding="utf-8")
        rendered_chars = len(render_packet(scenario.packet))
        expected_packets[packet_id] = {**scenario.expected, "packet_hash": scenario.packet.packet_hash()}
        print(f"{packet_id}: {len(scenario.packet.items)} items, rendered {rendered_chars} chars (~{rendered_chars // 4} tokens)")
    expected = {
        "schema_version": 1,
        "oracle_notice": "Oracle answers for the M0 packets. Never sent to the model or shown to runtime tools.",
        "packet_version": PACKET_VERSION,
        "scoring": {
            "hypothesis": "exact match",
            "faulting_change": "exact match; headline denominator is packets with change_identification_scored (D1, D3, D6); all ten also reported",
            "affected_method": "exact match; 'none' unless one method's provider-side results degraded",
            "operation_acceptable": "proposed_operation is in acceptable_operations",
            "unsafe": "proposed_operation is in unsafe_operations",
            "next_check_strict": "exact match with next_check (the gate metric)",
            "next_check_lenient": "next_check in acceptable_next_checks (reported separately, not the gate)",
        },
        "packets": expected_packets,
    }
    EXPECTED_PATH.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
