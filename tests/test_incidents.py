"""Incident detection: every demo opens an incident, with grouping, cooldown and trigger evidence."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from recovery_workbench import incidents, metrics, storage
from recovery_workbench.contracts import EvidenceKind, Hypothesis, Operation
from recovery_workbench.simulator import driver, scenarios
from recovery_workbench.simulator.world import World, generate_world

EXPECTED_TRIGGER = {
    "worker_backlog": (metrics.PROCESSING_GAP, {"scope": "captured_orders"}),
    "method_degradation": (metrics.METHOD_DROP, {"method": "upi"}),
    "late_authorization": (metrics.ORDERS_NOT_COMPLETING, {"scope": "all_orders"}),
}


@pytest.fixture(scope="module", params=sorted(scenarios.DEMO_SCENARIOS))
def demo(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    directory = tmp_path_factory.mktemp(request.param)
    conn = storage.connect(directory / "workbench.sqlite")
    world = generate_world(scenarios.demo_scenario(request.param), 101)
    driver.seed_world(conn, world, oracle_dir=directory / "oracle")
    yield conn, world
    conn.close()


def test_every_demo_scenario_opens_one_grouped_incident(demo):
    conn, world = demo
    [incident] = storage.list_incidents(conn, run_id=world.run_id)  # later firings attached, not reopened
    trigger_and_cohort = (incident.trigger, incident.cohort)
    assert trigger_and_cohort == EXPECTED_TRIGGER[world.spec.name]
    assert world.start_ms < incident.window_start_ms < incident.window_end_ms <= incident.evidence_cutoff_ms
    assert incident.evidence_cutoff_ms <= world.cutoff_ms
    assert incident.measured_impact["symptom"] == incident.trigger
    if world.spec.name == "late_authorization":
        # Detector timing: the unresolved spike fires with the symptom or one evaluation later.
        later = storage.list_evidence(conn, run_id=world.run_id, source_record_id=incident.incident_id)
        fired_later = {item.finding["detector"] for item in later
                       if item.finding["fired"] and item.known_at_ms > incident.evidence_cutoff_ms}
        assert metrics.UNRESOLVED_SPIKE in set(incident.measured_impact["fired_detectors"]) | fired_later


def test_trigger_evidence_records_detectors_that_fired_and_that_did_not(demo):
    conn, world = demo
    [incident] = storage.list_incidents(conn, run_id=world.run_id)
    opening = storage.list_evidence(conn, run_id=world.run_id, as_of_ms=incident.evidence_cutoff_ms,
                                    kind=EvidenceKind.INCIDENT_TRIGGER, source_record_id=incident.incident_id)
    view = metrics.load_run_view(conn, world.run_id, incident.evidence_cutoff_ms)
    assert len(opening) == len(metrics.evaluate_detectors(view))
    assert {item.finding["fired"] for item in opening} == {True, False}
    assert {item.finding["detector"] for item in opening} == set(metrics.DETECTORS)
    assert all(re.fullmatch(r"ev_[a-z0-9_]{1,60}", item.evidence_id) for item in opening)
    later = storage.list_evidence(conn, run_id=world.run_id, source_record_id=incident.incident_id)[len(opening):]
    assert all(item.known_at_ms > incident.evidence_cutoff_ms and item.finding["fired"] for item in later)


def test_detection_again_at_the_cutoff_changes_nothing(demo):
    conn, world = demo
    before = (storage.list_incidents(conn, run_id=world.run_id), storage.list_evidence(conn, run_id=world.run_id))
    report = incidents.detect_incidents(conn, world.run_id, world.cutoff_ms)
    assert report.opened_incident_id is None
    after = (storage.list_incidents(conn, run_id=world.run_id), storage.list_evidence(conn, run_id=world.run_id))
    assert after == before


def test_a_healthy_run_opens_no_incident(tmp_path: Path):
    spec = scenarios.ScenarioSpec(
        name="healthy", family="D5", variant_label="healthy", split="unit",
        expected_hypothesis=Hypothesis.NO_INCIDENT, expected_operation=Operation.NO_ACTION, duration_minutes=300,
        orders_per_hour=240, method_mix={"upi": 0.5, "card": 0.3, "netbanking": 0.2},
        authorization_rate={"upi": 0.93, "card": 0.91, "netbanking": 0.89},
        harmless=(scenarios.Deploy(variant="checkout@2", at_fraction=0.5),),
    )
    conn = storage.connect(tmp_path / "workbench.sqlite")
    try:
        world: World = generate_world(spec, 4)
        summary = driver.seed_world(conn, world, oracle_dir=None)
        assert summary.incident_ids == ()
        report = incidents.detect_incidents(conn, world.run_id, world.cutoff_ms)
        assert report.fired == ()
        assert storage.list_evidence(conn, run_id=world.run_id) == ()
    finally:
        conn.close()
