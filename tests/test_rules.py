"""Rules baseline on hand-built pkt-1 packets and on development-split runs (never the test split)."""
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from recovery_workbench import diagnosis, packet, rules, storage
from recovery_workbench.contracts import EvidencePacket, Hypothesis, Operation
from recovery_workbench.simulator import driver, families, runtime
from recovery_workbench.simulator.world import generate_world

T = 1_793_620_000_000
MIN = 60_000
TRACE = (
    "Traceback (most recent call last):\n"
    '  File "merchant_app/handler.py", line 22, in handle_event\n    parsed = payload.parse_event(event)\n'
    '  File "merchant_app/payload.py", line 23, in parse_event\n    "payment_id": entity["id"],\nKeyError: \'id\'\n'
)


def metric(method: str, *, attempts: int = 60, ratio: float = 0.93, baseline: float = 0.93, unresolved: int = 0,
           share: float | None = 0.33, baseline_share: float | None = 0.33) -> dict:
    return {"evidence_id": f"ev_metric_{method}", "kind": "metric", "known_at_ms": T, "title": f"Provider {method}",
            "facts": {"method": method, "mature_cohort_start_ms": T - 40 * MIN, "mature_cohort_end_ms": T - 10 * MIN,
                      "attempts": attempts, "authorised": round(attempts * ratio), "captured": round(attempts * ratio),
                      "unresolved": unresolved, "authorisation_ratio": ratio, "authorisation_ratio_95ci": [ratio - 0.05, ratio + 0.05],
                      "baseline_attempts": 400, "baseline_authorisation_ratio": baseline,
                      "baseline_95ci": [baseline - 0.02, baseline + 0.02], "share_of_attempts": share,
                      "baseline_share_of_attempts": baseline_share}, "text": None}


def healthy_packet() -> dict:
    """A pkt-1 packet with every core key and nothing wrong; tests change what they need."""
    return {
        "packet_version": "pkt-2",
        "incident": {"run_id": "run_eval_9999", "incident_id": "inc_0001", "symptom": "Fewer orders are completing than usual",
                     "trigger": "orders_not_completing", "cohort": {"merchant_id": "merchant_SYNTHETIC_A",
                     "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]},
                     "window_start_ms": T - 70 * MIN, "window_end_ms": T - 10 * MIN, "evidence_cutoff_ms": T,
                     "summary_counts": {"orders_with_attempts": 400, "orders_completed": 350, "payment_attempts": 420}},
        "items": [
            {"evidence_id": "ev_trigger", "kind": "incident_trigger", "known_at_ms": T, "title": "Detectors",
             "facts": {"fired": ["orders_not_completing"], "not_fired": ["delivery_rejection_spike", "merchant_processing_gap",
                       "method_authorization_drop[card]", "method_authorization_drop[netbanking]",
                       "method_authorization_drop[upi]", "unresolved_spike"],
                       "fired_since_open": ["orders_not_completing"], "first_breach_ms": T}, "text": None},
            metric("card"), metric("netbanking"), metric("upi"), metric("all", attempts=180, share=None, baseline_share=None),
            {"evidence_id": "ev_metric_processing", "kind": "metric", "known_at_ms": T, "title": "Processing",
             "facts": {"captured_orders": 90, "processed": 90, "pending_captured": 0, "held_for_review": 0,
                       "pending_captured_value_paise": 0, "processing_completion_ratio": 1.0,
                       "baseline_processing_completion_ratio": 1.0, "grace_ms": 300_000}, "text": None},
            {"evidence_id": "ev_provider_state", "kind": "provider_state", "known_at_ms": T, "title": "Provider state",
             "facts": {"snapshot_age_ms": 0, "observation_coverage": 0.99, "payments_by_status": {"captured": 380, "failed": 40},
                       "failed_by_reason": {"insufficient_balance": 40}, "failed_by_method_and_reason": {},
                       "failed_by_error_source": {"customer": 40}, "failed_then_authorised_later": 0,
                       "authorised_not_captured": 0, "captured_without_received_event": 0,
                       "payments_needing_reconciliation": 0, "reconciliation_reasons": {}, "stale_payments": 0}, "text": None},
            {"evidence_id": "ev_delivery", "kind": "delivery_health", "known_at_ms": T, "title": "Delivery",
             "facts": {"window_start_ms": T - 60 * MIN, "delivery_attempts": 300, "accepted": 300, "duplicates": 0,
                       "conflicting_ids": 0, "rejected_signature": 0, "rejected_schema": 0, "rejected_size": 0,
                       "distinct_events_rejected": 0, "first_rejection_ms": None, "accepted_since_first_rejection": None},
             "text": None},
            {"evidence_id": "ev_worker", "kind": "worker_health", "known_at_ms": T, "title": "Worker",
             "facts": {"inbox_pending": 0, "inbox_failed": 0, "inbox_manual_review": 0, "oldest_unprocessed_age_ms": None,
                       "processed_last_30m": 90, "failed_attempts_last_60m": 0, "last_event_received_ms": T,
                       "retry_policy": "3 attempts"}, "text": None},
            {"evidence_id": "ev_fixture", "kind": "fixture_check", "known_at_ms": T, "title": "Fixture checks",
             "facts": {"deployed_version": "baseline", "tests_run": 4, "tests_failed": 0, "results": [
                 {"test_id": test_id, "passed": True, "detail": None, "top_frame": None}
                 for test_id in ("captured_payment_builds_fulfilment", "authorized_payment_changes_nothing",
                                 "failed_payment_changes_nothing", "signed_delivery_verifies")]}, "text": None},
        ],
        "candidate_change_ids": [],
        "candidate_methods": ["card", "netbanking", "upi"],
    }


def item(document: dict, evidence_id: str) -> dict:
    return next(entry for entry in document["items"] if entry["evidence_id"] == evidence_id)


def add_deployment(document: dict, deploy_id: str, at_ms: int, files: list[str], service: str = "order_worker") -> None:
    document["items"].append({
        "evidence_id": f"ev_deploy_{deploy_id.removeprefix('deploy_')}", "kind": "deployment", "known_at_ms": at_ms,
        "title": f"Deployment {deploy_id} to {service}", "facts": {"deploy_id": deploy_id, "service": service,
        "kind": "code", "deployed_at_ms": at_ms, "files_changed": files, "summary": "Routine change"}, "text": "diff"})
    document["candidate_change_ids"].append(deploy_id)


def worker_failing(document: dict, *, first_failure_ms: int, category: str | None = "event_parse_error",
                   fixture_frame: str | None = "merchant_app/payload.py:23 in parse_event") -> None:
    item(document, "ev_trigger")["facts"].update(fired=["merchant_processing_gap"],
                                                 fired_since_open=["merchant_processing_gap"])
    document["items"].append({
        "evidence_id": "ev_worker_errors", "kind": "worker_error", "known_at_ms": T, "title": "Worker errors",
        "facts": {"exception_category": category, "failed_events": 30, "failed_by_event_type": {"payment.captured": 30},
                  "first_failure_ms": first_failure_ms, "last_failure_ms": T, "worker_versions": {"deploy_a": 90},
                  "distinct_trace_signatures": 1}, "text": TRACE})
    fixture = item(document, "ev_fixture")["facts"]
    fixture["tests_failed"] = 3
    for result in fixture["results"][:3]:
        result.update(passed=False, detail="KeyError: 'id'", top_frame=fixture_frame)


def diagnose(document: dict) -> diagnosis.DiagnosisOutput:
    built = EvidencePacket.model_validate(document)
    output = rules.diagnose(built)
    assert diagnosis.validate_diagnosis(output.model_dump(mode="json"), built, enforce_policies=True) == output
    return output


# --------------------------------------------------------------------------- hand-built packets


def test_a_worker_failure_blames_the_latest_change_before_the_first_failure():
    document = healthy_packet()
    add_deployment(document, "deploy_early", T - 120 * MIN, ["merchant_app/config.py"], "merchant_config")
    add_deployment(document, "deploy_fault", T - 40 * MIN, ["merchant_app/payload.py"])
    worker_failing(document, first_failure_ms=T - 39 * MIN)
    output = diagnose(document)
    assert (output.hypothesis, output.proposed_operation, output.faulting_change_id) == (
        Hypothesis.MERCHANT_WORKER_REGRESSION, Operation.REPROCESS_INBOX, "deploy_fault")
    assert output.evidence_level.value == "reproduced_in_fixture"
    assert "ev_fixture" in output.supporting_evidence_ids


def test_an_innocent_latest_deploy_touching_the_traced_file_is_not_blamed():
    document = healthy_packet()
    add_deployment(document, "deploy_fault", T - 40 * MIN, ["merchant_app/config.py"], "merchant_config")
    add_deployment(document, "deploy_innocent", T - 36 * MIN, ["merchant_app/payload.py"])  # after the first failure
    worker_failing(document, first_failure_ms=T - 39 * MIN, category=None)  # free text only
    assert diagnose(document).faulting_change_id == "deploy_fault"


def test_top_frame_blame_breaks_a_tie_between_close_deploys():
    document = healthy_packet()
    add_deployment(document, "deploy_traced", T - 45 * MIN, ["merchant_app/payload.py"])
    add_deployment(document, "deploy_other", T - 42 * MIN, ["merchant_app/notifications.py"], "storefront")
    worker_failing(document, first_failure_ms=T - 40 * MIN)
    assert diagnose(document).faulting_change_id == "deploy_traced"


def test_rejected_deliveries_point_at_the_receiver_change_and_replay():
    document = healthy_packet()
    add_deployment(document, "deploy_receiver", T - 30 * MIN, ["merchant_app/receiver.py"], "webhook_receiver")
    add_deployment(document, "deploy_later", T - 20 * MIN, ["merchant_app/checkout.py"], "storefront")
    item(document, "ev_trigger")["facts"].update(fired=["delivery_rejection_spike"],
                                                 fired_since_open=["delivery_rejection_spike"])
    item(document, "ev_delivery")["facts"].update(rejected_signature=40, distinct_events_rejected=25,
                                                  first_rejection_ms=T - 29 * MIN, accepted_since_first_rejection=0)
    fixture = item(document, "ev_fixture")["facts"]
    fixture["tests_failed"] = 1
    fixture["results"][3].update(passed=False, detail="signature verification failed")
    output = diagnose(document)
    assert (output.hypothesis, output.proposed_operation, output.faulting_change_id) == (
        Hypothesis.RECEIVER_REJECTING_DELIVERIES, Operation.PREPARE_PROVIDER_REPLAY, "deploy_receiver")
    assert output.evidence_level.value == "reproduced_in_fixture"


def test_a_fired_method_detector_names_the_method_and_escalates():
    document = healthy_packet()
    add_deployment(document, "deploy_near", T - 80 * MIN, ["merchant_app/checkout.py"], "storefront")
    item(document, "ev_trigger")["facts"].update(
        fired=["method_authorization_drop[card]", "orders_not_completing"],
        fired_since_open=["method_authorization_drop[card]", "orders_not_completing"])
    document["items"][1] = metric("card", ratio=0.5, baseline=0.92)
    output = diagnose(document)
    assert (output.hypothesis, output.affected_method, output.faulting_change_id, output.proposed_operation) == (
        Hypothesis.PROVIDER_METHOD_DEGRADATION, "card", "none", Operation.ESCALATE)


def test_contradictory_provider_evidence_asks_for_a_refresh():
    document = healthy_packet()
    item(document, "ev_provider_state")["facts"].update(
        payments_needing_reconciliation=30, reconciliation_reasons={"same_time_contradiction": 30})
    output = diagnose(document)
    assert (output.hypothesis, output.proposed_operation) == (Hypothesis.AMBIGUOUS_PAYMENT_STATE,
                                                              Operation.REFRESH_PROVIDER_STATE)


def test_provider_confirmed_pending_payments_are_ambiguous_state():
    document = healthy_packet()
    document["items"][4] = metric("all", attempts=80, ratio=0.55, unresolved=30, share=None, baseline_share=None)
    item(document, "ev_provider_state")["facts"].update(
        payments_by_status={"captured": 300, "created": 45, "failed": 20}, failed_then_authorised_later=5)
    assert diagnose(document).hypothesis is Hypothesis.AMBIGUOUS_PAYMENT_STATE


def test_unresolved_attempts_with_no_provider_evidence_are_an_observation_gap():
    document = healthy_packet()
    document["items"][4] = metric("all", attempts=26, ratio=0.0, unresolved=26, share=None, baseline_share=None)
    item(document, "ev_provider_state")["facts"].update(
        snapshot_age_ms=None, observation_coverage=0.7, payments_by_status={"captured": 99, "unknown": 47})
    output = diagnose(document)
    assert (output.hypothesis, output.proposed_operation, output.evidence_level.value) == (
        Hypothesis.NO_INCIDENT, Operation.NO_ACTION, "insufficient")


@pytest.mark.parametrize(
    ("change", "next_check"),
    [
        (lambda d: d["items"].__setitem__(2, metric("netbanking", ratio=0.45, baseline=0.45, share=0.7, baseline_share=0.1)),
         "compare_method_cohorts"),
        (lambda d: d["items"].__setitem__(4, metric("all", attempts=12, share=None, baseline_share=None)),
         "wait_for_mature_cohort"),
    ],
)
def test_a_lone_symptom_with_healthy_payments_is_no_incident(change: Callable[[dict], None], next_check: str):
    document = healthy_packet()
    add_deployment(document, "deploy_near", T - 90 * MIN, ["merchant_app/config.py"], "merchant_config")
    change(document)
    output = diagnose(document)
    assert (output.hypothesis, output.faulting_change_id, output.next_check.value) == (
        Hypothesis.NO_INCIDENT, "none", next_check)


def test_a_method_alert_that_stopped_firing_still_counts():
    """pkt-2: the detector that opened the incident is quiet at the cutoff; its alert history is not."""
    document = healthy_packet()
    document["incident"]["trigger"] = "method_authorization_drop[card]"
    item(document, "ev_trigger")["facts"].update(fired=[], fired_since_open=["method_authorization_drop[card]"])
    document["items"][1] = metric("card", ratio=0.8, baseline=0.92)
    output = diagnose(document)
    assert (output.hypothesis, output.affected_method) == (Hypothesis.PROVIDER_METHOD_DEGRADATION, "card")


def test_no_matching_pattern_escalates_as_unknown():
    document = healthy_packet()
    document["incident"]["trigger"] = "merchant_processing_gap"
    item(document, "ev_trigger")["facts"].update(fired=["merchant_processing_gap"],
                                                 fired_since_open=["merchant_processing_gap"])
    item(document, "ev_metric_processing")["facts"].update(pending_captured=12)
    output = diagnose(document)
    assert (output.hypothesis, output.proposed_operation) == (Hypothesis.UNKNOWN, Operation.ESCALATE)


def test_keys_outside_the_core_schema_never_change_the_answer():
    document = healthy_packet()
    add_deployment(document, "deploy_fault", T - 40 * MIN, ["merchant_app/payload.py"])
    worker_failing(document, first_failure_ms=T - 39 * MIN)
    enriched = copy.deepcopy(document)
    for entry in enriched["items"]:
        entry["facts"]["extra_hint"] = "merchant_worker_regression"
    item(enriched, "ev_trigger")["facts"]["reasons"] = {"orders_not_completing": "degraded"}
    assert diagnose(enriched) == diagnose(document)


# --------------------------------------------------------------------------- development-split runs


@pytest.fixture(scope="module")
def dev_runs(tmp_path_factory: pytest.TempPathFactory):
    """One development-split run per family, built with the real generator and packet builder."""
    directory = tmp_path_factory.mktemp("rules_dev")
    conn = storage.connect(directory / "workbench.sqlite")
    picked = {}
    for run in families.load_manifest().runs:
        if run.split == "dev" and run.family not in picked:
            picked[run.family] = run
    assert all(run.split == "dev" for run in picked.values()), "rules never run on test-split runs"
    yield conn, directory, list(picked.values())
    conn.close()


def test_rules_diagnose_one_development_run_per_family_correctly(dev_runs):
    conn, directory, runs = dev_runs
    for run in runs:
        world = generate_world(families.spec_for(run), run.seed)
        summary = driver.seed_world(conn, world, oracle_dir=directory / "oracle", provider_dir=directory / "provider")
        expected = json.loads(Path(str(summary.oracle_path)).read_text(encoding="utf-8"))["expected"]
        [incident] = storage.list_incidents(conn, run_id=world.run_id)
        built = packet.build_packet(  # diagnosed at the run's cutoff, as the evaluation runner does
            conn, run_id=world.run_id, incident_id=incident.incident_id, cutoff_ms=summary.cutoff_ms,
            deployed=runtime.active_merchant(conn, run_id=world.run_id, at_ms=summary.cutoff_ms), persist=False,
        )
        output = rules.diagnose(built)
        diagnosis.validate_diagnosis(output.model_dump(mode="json"), built, enforce_policies=True)
        assert (output.hypothesis.value, output.faulting_change_id, output.affected_method) == (
            expected["hypothesis"], expected["faulting_change_id"], expected["affected_method"]), run.run_key
        assert output.proposed_operation.value in expected["acceptable_operations"], run.run_key
