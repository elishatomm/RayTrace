"""Signature rejections at an integration that still accepts deliveries open an incident.

The rejection spike needs a fifth of an integration's deliveries rejected. During a provider secret rotation the
retries of events first sent before it keep the old signature and are refused while new events verify, so at a busy
integration they stay far under that share and opened no incident. ``metrics.detect_signature_rejections`` covers
that case. It reports an integration only when the latest window holds a signature rejection and, at or after the
first one, an accepted delivery; it fires at the spike's minimum count, 10, whatever their share. Elsewhere it
reports nothing, so ordinary worlds, the frozen evaluation among them, are evaluated exactly as before.

Seeds 19601-19700 are reserved for Q22 and Q25 (agent B); this file uses 19602.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from conftest import RUN_A, START_MS, SECRET_B, SeededRun, deliver, seed_run
from test_incidents import EXPECTED_TRIGGER
from test_webhook_conformance import BUSY, COORDINATED_ROTATION

from recovery_workbench import incidents, metrics, storage
from recovery_workbench.contracts import EvidenceKind, ReceiveOutcome
from recovery_workbench.simulator import driver, families, scenarios, signing
from recovery_workbench.simulator.world import INTEGRATION_ID, World, generate_world

MINUTE = 60_000
T = START_MS + 60 * MINUTE  # the window below never reaches back before the run started
SETTINGS = metrics.DEFAULT_INCIDENT_DETECTORS
FRESH_ROTATION_SEED = 19602
SECOND_INTEGRATION = "int_SYNTHETIC_A2"


def send(conn: sqlite3.Connection, run: SeededRun, number: int, at_ms: int, **fields: object) -> ReceiveOutcome:
    """Delivery ``number`` received at ``at_ms``; one distinct body per number, signed with the run's secret unless
    ``secret_ref`` says otherwise."""
    response = deliver(
        conn, run, f"evt_SYNTHETIC_{number:04d}", received_at_ms=at_ms, occurred_at_ms=at_ms - 50,
        payment_id=signing.synthetic_id("pay", number), order_id=signing.synthetic_id("order", number),
        **fields,  # type: ignore[arg-type]
    )
    return response.outcome


def busy_integration(conn: sqlite3.Connection, run: SeededRun, *, rejected: int, accepted: int = 50) -> int:
    """``accepted`` deliveries every 20 s from T, and ``rejected`` signed with another secret every minute from T + 1
    minute; returns the time of the last delivery. 10 of 60 is a sixth: under the spike's fifth."""
    for number in range(accepted):
        assert send(conn, run, number, T + number * 20_000) is ReceiveOutcome.ACCEPTED
    for offset in range(rejected):
        outcome = send(conn, run, 1_000 + offset, T + (offset + 1) * MINUTE, secret_ref=SECRET_B)
        assert outcome is ReceiveOutcome.REJECTED_SIGNATURE
    return T + max((accepted - 1) * 20_000, rejected * MINUTE)


def detected(conn: sqlite3.Connection, as_of_ms: int) -> list[metrics.DetectorResult]:
    return metrics.detect_signature_rejections(metrics.load_run_view(conn, RUN_A, as_of_ms), SETTINGS)


# --------------------------------------------------------------------------- the rule


def test_ten_signature_rejections_while_others_are_accepted_open_an_incident(conn, run_a: SeededRun) -> None:
    last = busy_integration(conn, run_a, rejected=10)
    report = incidents.detect_incidents(conn, RUN_A, last)
    assert report.opened_incident_id == "inc_0001"
    [incident] = storage.list_incidents(conn, run_id=RUN_A)
    assert incident.trigger == metrics.SIGNATURE_REJECTIONS
    assert incident.cohort == {"integration_id": run_a.integration_id}
    findings = {item.evidence_id: item.finding for item in storage.list_evidence(
        conn, run_id=RUN_A, kind=EvidenceKind.INCIDENT_TRIGGER, source_record_id="inc_0001")}
    spike = findings["ev_trig_0001_rejections_int_synthetic_a"]
    assert (spike["fired"], spike["reason"], spike["measurements"]) == (
        False, "share_normal", {"rejected": {"numerator": 10, "denominator": 60, "value": 0.1667}})
    signature = findings["ev_trig_0001_signatures_int_synthetic_a"]
    assert (signature["fired"], signature["reason"]) == (True, "rejections_high")
    assert signature["measurements"] == {
        "rejected_signature": {"numerator": 10, "denominator": 60, "value": 0.1667},
        "distinct_bodies_rejected": 10,
        "accepted_since_first_rejection": 47,  # the first rejection came at T + 1 min, after three acceptances
    }


def test_nine_signature_rejections_are_reported_without_firing(conn, run_a: SeededRun) -> None:
    last = busy_integration(conn, run_a, rejected=9)
    [result] = detected(conn, last)
    assert (result.detector, result.fired, result.reason) == (metrics.SIGNATURE_REJECTIONS, False, "too_few_rejections")
    assert incidents.detect_incidents(conn, RUN_A, last).opened_incident_id is None


def test_a_receiver_that_refuses_everything_is_left_to_the_rejection_spike(conn, run_a: SeededRun) -> None:
    """D3's shape: accepted until a bad deploy, refused from then on. Nothing is accepted after the first rejection."""
    for number in range(30):
        assert send(conn, run_a, number, T + number * 20_000) is ReceiveOutcome.ACCEPTED
    for offset in range(15):
        assert send(conn, run_a, 100 + offset, T + 10 * MINUTE + offset * MINUTE,
                    secret_ref=SECRET_B) is ReceiveOutcome.REJECTED_SIGNATURE
    as_of = T + 25 * MINUTE
    assert detected(conn, as_of) == []
    report = incidents.detect_incidents(conn, RUN_A, as_of)
    assert report.opened_incident_id == "inc_0001"
    [incident] = storage.list_incidents(conn, run_id=RUN_A)
    assert incident.trigger == metrics.REJECTION_SPIKE
    assert metrics.SIGNATURE_REJECTIONS not in {result.detector for result in report.results}


def test_only_signature_rejections_inside_the_window_and_at_their_own_integration_count(conn, run_a: SeededRun) -> None:
    # Schema rejections are not signature rejections.
    for offset in range(12):
        assert send(conn, run_a, 100 + offset, T + offset * MINUTE,
                    event_type="payment.unlisted") is ReceiveOutcome.REJECTED_SCHEMA
    for number in range(20):
        assert send(conn, run_a, number, T + 15 * MINUTE + number * 20_000) is ReceiveOutcome.ACCEPTED
    assert detected(conn, T + 25 * MINUTE) == []
    # Signature rejections older than the window are not in it.
    for offset in range(12):
        assert send(conn, run_a, 200 + offset, T + 26 * MINUTE + offset * 1_000,
                    secret_ref=SECRET_B) is ReceiveOutcome.REJECTED_SIGNATURE
    for number in range(20, 25):
        assert send(conn, run_a, number, T + 27 * MINUTE + number * 1_000) is ReceiveOutcome.ACCEPTED
    assert [result.fired for result in detected(conn, T + 28 * MINUTE)] == [True]
    assert detected(conn, T + 57 * MINUTE) == []  # the window starts 30 minutes back, after every rejection
    # Rejections at one integration and acceptances at another are two integrations, neither showing the pattern.
    with storage.write_transaction(conn):
        storage.add_integration(conn, storage.IntegrationRecord(
            run_id=RUN_A, integration_id=SECOND_INTEGRATION, secret_ref=run_a.secret_ref, max_body_bytes=65_536))
    for offset in range(12):
        assert send(conn, run_a, 300 + offset, T + 60 * MINUTE + offset * 1_000, integration_id=SECOND_INTEGRATION,
                    secret_ref=SECRET_B) is ReceiveOutcome.REJECTED_SIGNATURE
    for number in range(30, 40):
        assert send(conn, run_a, number, T + 61 * MINUTE + number * 1_000) is ReceiveOutcome.ACCEPTED
    assert detected(conn, T + 62 * MINUTE) == []


def test_the_rule_reports_nothing_for_a_run_without_deliveries(conn) -> None:
    other = seed_run(conn, "run_unit_quiet", "merchant_SYNTHETIC_A", "int_SYNTHETIC_Q", "fixture_secret_a")
    view = metrics.load_run_view(conn, other.run_id, T)
    assert metrics.detect_signature_rejections(view, SETTINGS) == []
    assert metrics.SIGNATURE_REJECTIONS not in {result.detector for result in metrics.evaluate_detectors(view)}


# --------------------------------------------------------------------------- worlds


def signature_reports(conn: sqlite3.Connection, world: World) -> list[tuple[int, list[metrics.DetectorResult]]]:
    """What the signature detector reports at each of the replay's detector evaluations and at the cutoff."""
    interval = world.spec.detector_interval_minutes * MINUTE
    times = sorted({*range(world.start_ms + interval, world.cutoff_ms + 1, interval), world.cutoff_ms})
    found = []
    for at in times:
        results = metrics.detect_signature_rejections(metrics.load_run_view(conn, world.run_id, at), SETTINGS)
        if results:
            found.append(((at - world.start_ms) // MINUTE, results))
    return found


def test_a_rotation_on_a_fresh_seed_opens_one_incident_at_the_next_evaluation(tmp_path: Path) -> None:
    world = generate_world(BUSY, FRESH_ROTATION_SEED, conformance=COORDINATED_ROTATION)
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        driver.seed_world(conn, world, oracle_dir=None)
        opened = storage.list_incidents(conn, run_id=world.run_id)
        assert len(opened) == 1
        [incident] = opened
        rejected = [row for row in storage.list_delivery_attempts(conn, run_id=world.run_id)
                    if row.outcome is ReceiveOutcome.REJECTED_SIGNATURE]
        first = min(row.received_at_ms for row in rejected)
        assert first < incident.evidence_cutoff_ms <= first + world.spec.detector_interval_minutes * MINUTE
        assert (incident.trigger, incident.cohort) == (metrics.SIGNATURE_REJECTIONS, {"integration_id": INTEGRATION_ID})
        # It fires at every evaluation from then to the cutoff: one retry per stranded event is already 10 or more.
        reports = signature_reports(conn, world)
        assert reports[0][0] == (incident.evidence_cutoff_ms - world.start_ms) // MINUTE
        assert all(result.fired for _, results in reports for result in results)
        assert {result.measurements["distinct_bodies_rejected"] for _, results in reports for result in results} == {
            len({row.body_sha256 for row in rejected})}


@pytest.fixture(scope="module", params=sorted(scenarios.DEMO_SCENARIOS))
def demo(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[sqlite3.Connection, World]]:
    directory = tmp_path_factory.mktemp(request.param)
    world = generate_world(scenarios.demo_scenario(request.param), 101)
    with closing(storage.connect(directory / "workbench.sqlite")) as conn:
        driver.seed_world(conn, world, oracle_dir=None)
        yield conn, world


def test_a_demo_world_has_no_signature_rejection_and_the_rule_never_reports(demo) -> None:
    conn, world = demo
    outcomes = {row.outcome for row in storage.list_delivery_attempts(conn, run_id=world.run_id)}
    assert ReceiveOutcome.REJECTED_SIGNATURE not in outcomes
    assert signature_reports(conn, world) == []
    [incident] = storage.list_incidents(conn, run_id=world.run_id)
    trigger_and_cohort = (incident.trigger, incident.cohort)
    assert trigger_and_cohort == EXPECTED_TRIGGER[world.spec.name]


@pytest.mark.parametrize("run_key", ["d3_dev_1", "d3_dev_2"])  # receiver@2 and config@3, the frozen D3 faults
def test_a_receiver_fault_world_is_still_the_rejection_spikes_and_the_rule_never_reports(
    tmp_path: Path, run_key: str
) -> None:
    [run] = [run for run in families.load_manifest().runs if run.run_key == run_key]
    world = generate_world(families.spec_for(run), run.seed)
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        driver.seed_world(conn, world, oracle_dir=None)
        attempts = storage.list_delivery_attempts(conn, run_id=world.run_id)
        first = min(row.received_at_ms for row in attempts if row.outcome is ReceiveOutcome.REJECTED_SIGNATURE)
        assert not [row for row in attempts if row.outcome is ReceiveOutcome.ACCEPTED and row.received_at_ms >= first]
        assert signature_reports(conn, world) == []
        [incident] = storage.list_incidents(conn, run_id=world.run_id)
        assert incident.trigger == metrics.REJECTION_SPIKE
