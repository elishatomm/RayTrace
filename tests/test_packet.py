"""The pkt-1 evidence packet (docs/PACKET_SCHEMA.md): every core key, deterministic, bounded, leak-free."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue
from test_diagnose_incident import FakeModel
from test_tools import seed

from recovery_workbench import diagnosis, packet, storage
from recovery_workbench.contracts import (
    ContractError,
    EvidenceKind,
    EvidencePacket,
    ScopeError,
    canonical_json,
    sha256_hex,
)
from recovery_workbench.simulator import driver, families, merchant, runtime, scenarios

SCENARIOS = ("worker_backlog", "method_degradation", "late_authorization")

# Core keys per kind, copied from docs/PACKET_SCHEMA.md (pkt-1).
CORE_KEYS: dict[str, set[str]] = {
    "incident_trigger": {"fired", "not_fired", "fired_since_open", "first_breach_ms"},
    "metric_cohort": {
        "method", "mature_cohort_start_ms", "mature_cohort_end_ms", "attempts", "authorised", "captured", "unresolved",
        "authorisation_ratio", "authorisation_ratio_95ci", "baseline_attempts", "baseline_authorisation_ratio",
        "baseline_95ci", "share_of_attempts", "baseline_share_of_attempts",
    },
    "metric_processing": {
        "captured_orders", "processed", "pending_captured", "held_for_review", "pending_captured_value_paise",
        "processing_completion_ratio", "baseline_processing_completion_ratio", "grace_ms",
    },
    "provider_state": {
        "snapshot_age_ms", "observation_coverage", "payments_by_status", "failed_by_reason",
        "failed_by_method_and_reason", "failed_by_error_source", "failed_then_authorised_later",
        "authorised_not_captured", "captured_without_received_event", "payments_needing_reconciliation",
        "reconciliation_reasons", "stale_payments",
    },
    "delivery_health": {
        "window_start_ms", "delivery_attempts", "accepted", "duplicates", "conflicting_ids", "rejected_signature",
        "rejected_schema", "rejected_size", "distinct_events_rejected", "first_rejection_ms",
        "accepted_since_first_rejection",
    },
    "worker_health": {
        "inbox_pending", "inbox_failed", "inbox_manual_review", "oldest_unprocessed_age_ms", "processed_last_30m",
        "failed_attempts_last_60m", "last_event_received_ms", "retry_policy",
    },
    "worker_error": {
        "exception_category", "failed_events", "failed_by_event_type", "first_failure_ms", "last_failure_ms",
        "worker_versions", "distinct_trace_signatures",
    },
    "fixture_check": {"deployed_version", "tests_run", "tests_failed", "results"},
    "deployment": {"deploy_id", "service", "kind", "deployed_at_ms", "files_changed", "summary"},
    "log": {"source", "level", "occurred_at_ms"},
    "order_journey": {"order_id", "order_amount_paise", "attempts", "projection"},
}
DETECTOR_NAME = re.compile(
    r"^(orders_not_completing|merchant_processing_gap|delivery_rejection_spike|unresolved_spike|"
    r"method_authorization_drop\[[a-z]+\])$"
)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, tuple[sqlite3.Connection, driver.SeedSummary]]]:
    runs = {name: seed(tmp_path_factory.mktemp(f"packet_{name}"), name) for name in SCENARIOS}
    yield runs
    for conn, _ in runs.values():
        conn.close()


def build(conn: sqlite3.Connection, summary: driver.SeedSummary, *, cutoff_ms: int | None = None,
          persist: bool = False) -> EvidencePacket:
    cutoff = summary.cutoff_ms if cutoff_ms is None else cutoff_ms
    return packet.build_packet(
        conn, run_id=summary.run_id, incident_id=summary.incident_ids[0], cutoff_ms=cutoff,
        deployed=runtime.active_merchant(conn, run_id=summary.run_id, at_ms=cutoff), persist=persist,
    )


def kinds(built: EvidencePacket, kind: EvidenceKind) -> list:
    return [item for item in built.items if item.kind is kind]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_core_key_is_present_for_every_kind(seeded, scenario: str) -> None:
    conn, summary = seeded[scenario]
    built = build(conn, summary)
    by_id = {item.evidence_id: item for item in built.items}
    assert by_id["ev_trigger"].kind is EvidenceKind.INCIDENT_TRIGGER
    assert CORE_KEYS["incident_trigger"] <= set(by_id["ev_trigger"].facts)
    for method in (*built.candidate_methods, "all"):
        assert CORE_KEYS["metric_cohort"] <= set(by_id[f"ev_metric_{method}"].facts), method
    assert CORE_KEYS["metric_processing"] <= set(by_id["ev_metric_processing"].facts)
    for evidence_id, kind in (("ev_provider_state", "provider_state"), ("ev_delivery", "delivery_health"),
                              ("ev_worker", "worker_health"), ("ev_fixture", "fixture_check")):
        assert CORE_KEYS[kind] <= set(by_id[evidence_id].facts), evidence_id
    for kind in ("worker_error", "deployment", "log", "order_journey"):
        for item in kinds(built, EvidenceKind(kind)):
            assert CORE_KEYS[kind] <= set(item.facts), item.evidence_id
    for result in by_id["ev_fixture"].facts["results"]:
        assert set(result) >= {"test_id", "passed", "detail", "top_frame"}
    assert set(built.incident.summary_counts) == {"orders_with_attempts", "orders_completed", "payment_attempts"}
    assert set(built.incident.cohort) == {"merchant_id", "integration_id", "methods"}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_detector_state_uses_the_real_detector_names(seeded, scenario: str) -> None:
    conn, summary = seeded[scenario]
    facts = next(item for item in build(conn, summary).items if item.evidence_id == "ev_trigger").facts
    assert facts["fired"] == sorted(facts["fired"])
    assert facts["not_fired"] == sorted(facts["not_fired"])
    assert not set(facts["fired"]) & set(facts["not_fired"])
    assert all(DETECTOR_NAME.match(name) for name in [*facts["fired"], *facts["not_fired"], *facts["fired_since_open"]])
    assert isinstance(facts["first_breach_ms"], int)
    assert facts["first_breach_ms"] <= summary.cutoff_ms
    trigger = build(conn, summary).incident.trigger
    assert DETECTOR_NAME.match(trigger)
    assert trigger in facts["fired_since_open"]
    assert facts["fired_since_open"] == sorted(facts["fired_since_open"])


def test_candidates_follow_the_contract(seeded) -> None:
    conn, summary = seeded["worker_backlog"]
    built = build(conn, summary)
    deployments = kinds(built, EvidenceKind.DEPLOYMENT)
    assert list(built.candidate_change_ids) == [item.facts["deploy_id"] for item in deployments]
    assert [item.facts["deployed_at_ms"] for item in deployments] == sorted(item.facts["deployed_at_ms"] for item in deployments)
    assert list(built.candidate_methods) == sorted(built.candidate_methods) == ["card", "netbanking", "upi"]
    assert all(item.title == f"Deployment {item.facts['deploy_id']} to {item.facts['service']}" for item in deployments)


def test_worker_errors_appear_only_when_the_worker_failed(seeded) -> None:
    assert kinds(build(*seeded["worker_backlog"]), EvidenceKind.WORKER_ERROR)
    for scenario in ("method_degradation", "late_authorization"):
        assert not kinds(build(*seeded[scenario]), EvidenceKind.WORKER_ERROR)


def test_free_text_runs_carry_a_null_category(tmp_path: Path) -> None:
    from recovery_workbench.simulator import scenarios
    from recovery_workbench.simulator.world import generate_world

    spec = scenarios.WORKER_BACKLOG.model_copy(update={"emits_categories": False, "name": "worker_backlog_free_text"})
    conn = storage.connect(tmp_path / "workbench.sqlite")
    try:
        summary = driver.seed_world(conn, generate_world(spec, 7), oracle_dir=tmp_path / "oracle")
        errors = kinds(build(conn, summary), EvidenceKind.WORKER_ERROR)[0]
        assert errors.facts["exception_category"] is None
        assert errors.text
        assert "Traceback" in errors.text
    finally:
        conn.close()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_same_inputs_give_a_byte_identical_packet(seeded, scenario: str) -> None:
    conn, summary = seeded[scenario]
    first, second = build(conn, summary), build(conn, summary)
    assert canonical_json(first.model_dump(mode="json")) == canonical_json(second.model_dump(mode="json"))
    assert first.packet_hash() == second.packet_hash()


def test_an_independently_seeded_copy_gives_the_same_packet(seeded, tmp_path: Path) -> None:
    conn, summary = seeded["worker_backlog"]
    other_conn, other_summary = seed(tmp_path, "worker_backlog")
    try:
        assert build(other_conn, other_summary).packet_hash() == build(conn, summary).packet_hash()
    finally:
        other_conn.close()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_packet_stays_inside_its_size_budget(seeded, scenario: str) -> None:
    built = build(*seeded[scenario])
    assert len(diagnosis.render_packet(built)) <= 16_000  # about 4k tokens at 4 characters a token
    for item in kinds(built, EvidenceKind.DEPLOYMENT):
        assert len(item.text.splitlines()) <= 61
        assert len(item.text) <= 2_600
    for item in kinds(built, EvidenceKind.WORKER_ERROR):
        assert len(item.text or "") <= 4_000
    assert len(kinds(built, EvidenceKind.LOG)) <= 8
    assert len(kinds(built, EvidenceKind.ORDER_JOURNEY)) <= 3
    assert len(kinds(built, EvidenceKind.FIXTURE_CHECK)) == 1


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_packet_leaks_no_answer(seeded, scenario: str) -> None:
    observable = json.dumps(build(*seeded[scenario]).model_dump(mode="json"))
    assert "sim_" not in observable
    assert "oracle" not in observable
    assert not re.search(r"\b(config|payload|orders|handler|receiver|checkout|notifications)@\d+\b", observable)
    assert not re.search(r"\bD[1-6]\b", observable)
    assert "family" not in observable
    assert "variant" not in observable
    assert not re.search(r"\b(bug|faulty|broken|fix|fixed|culprit|root cause|innocent|harmless)\b", observable, re.I)
    assert "latency" not in observable
    assert "_wall_ms" not in observable


def _labels_the_model_must_not_see() -> set[str]:
    runs = families.load_manifest().runs
    return set(scenarios.DEMO_SCENARIOS) | {run.family for run in runs} | {run.variant for run in runs}


def _exact_mentions(value: JsonValue) -> Iterator[str]:
    """Every dict key and every string value, recursively."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _exact_mentions(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _exact_mentions(inner)
    elif isinstance(value, str):
        yield value


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_model_never_sees_the_run_the_incident_or_a_scenario_label(seeded, scenario: str) -> None:
    conn, summary = seeded[scenario]
    built = build(conn, summary)
    client = FakeModel("not json", "not json")  # only the request matters here
    diagnosis.diagnose(built, client, think=False)
    sent = client.requests[0]
    seen = "\n".join(message.content for message in sent.messages) + json.dumps(sent.response_schema)
    tokens = Counter(re.findall(r"[A-Za-z0-9_]+", seen))
    for identifier in (summary.run_id, *summary.incident_ids, "run_id", "incident_id"):
        assert tokens[identifier] == 0, identifier
    # Two labels are also pkt-1 vocabulary: late_authorization (a projection key and an exclusion reason)
    # and stale_snapshot (an exclusion reason). They may appear only as exact fact keys or values, as they
    # do in every scenario; never in the incident card, a title, a text block or inside an identifier.
    structured = Counter(
        mention for value in [*(item.facts for item in built.items), built.incident.cohort, built.incident.summary_counts]
        for mention in _exact_mentions(value)
    )
    leaked = {label: tokens[label] for label in _labels_the_model_must_not_see() if tokens[label] != structured[label]}
    assert not leaked
    assert tokens["late_authorization"] > 0 or scenario != "worker_backlog"  # the vocabulary case is exercised


def test_fixture_results_are_in_every_packet_and_run_on_the_deployed_handler(seeded) -> None:
    for scenario in SCENARIOS:
        conn, summary = seeded[scenario]
        fixture = kinds(build(conn, summary), EvidenceKind.FIXTURE_CHECK)[0]
        deployed = runtime.active_merchant(conn, run_id=summary.run_id, at_ms=summary.cutoff_ms)
        assert fixture.facts["deployed_version"] == deployed.version
        assert fixture.facts["tests_run"] == len(merchant.FIXTURE_TEST_IDS)
    backlog_fixture = kinds(build(*seeded["worker_backlog"]), EvidenceKind.FIXTURE_CHECK)[0]
    assert backlog_fixture.facts["tests_failed"] == 3
    assert "merchant_app/payload.py" in backlog_fixture.text


def test_an_earlier_cutoff_sees_less_and_never_the_future(seeded) -> None:
    conn, summary = seeded["worker_backlog"]
    late = build(conn, summary)
    incident = storage.list_incidents(conn, run_id=summary.run_id)[0]
    early = build(conn, summary, cutoff_ms=incident.evidence_cutoff_ms)
    assert all(item.known_at_ms <= early.incident.evidence_cutoff_ms for item in early.items)
    early_errors = kinds(early, EvidenceKind.WORKER_ERROR)[0].facts["failed_events"]
    assert early_errors < kinds(late, EvidenceKind.WORKER_ERROR)[0].facts["failed_events"]
    with pytest.raises(ScopeError, match="opened by"):
        build(conn, summary, cutoff_ms=incident.evidence_cutoff_ms - 1)


def test_packet_items_are_persisted_once_with_content_hashes(seeded, tmp_path: Path) -> None:
    conn, summary = seed(tmp_path, "worker_backlog")
    try:
        built = build(conn, summary, persist=True)
        rows = storage.list_evidence(conn, run_id=summary.run_id, source_record_id=packet.packet_ref(built))
        assert len(rows) == len(built.items)
        by_item = {row.finding["packet_item_id"]: row for row in rows}
        for item in built.items:
            assert by_item[item.evidence_id].content_hash == sha256_hex(canonical_json(item.model_dump(mode="json")))
        build(conn, summary, persist=True)  # same packet: nothing new is stored
        assert len(storage.list_evidence(conn, run_id=summary.run_id, source_record_id=packet.packet_ref(built))) == len(rows)
        assert packet.stored_packet_items(conn, run_id=summary.run_id, reference=packet.packet_ref(built))
        with storage.write_transaction(conn):  # a record known by the cutoff appears after the packet was cut
            storage.insert_merchant_log(conn, storage.MerchantLogRecord(
                run_id=summary.run_id, log_id="log_late_arrival", service_id="storefront", level="info",
                message="late arriving line", occurred_at_ms=summary.cutoff_ms, known_at_ms=summary.cutoff_ms,
            ))
        with pytest.raises(ContractError, match="differs from a fresh build"):
            build(conn, summary, persist=True)
    finally:
        conn.close()
