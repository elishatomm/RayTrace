"""pkt-2: the packet carries the incident's alert history, not only detector state at the cutoff.

Regression for the integration bug found by the orchestrator: a moderate method drop opened the
incident, then stopped firing before the diagnosis cutoff, so ``ev_trigger.fired`` was empty and
the rules baseline saw no signal at all. The alert history is evidence an on-call engineer has.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from recovery_workbench import packet, storage
from recovery_workbench.contracts import ModelMode
from recovery_workbench.simulator import driver, families, runtime
from recovery_workbench.simulator.world import generate_world

MODERATE_CARD_DROP_DEV_SEED = 1012  # D2 dev run whose detector stops firing before the cutoff


def _seed_manifest_run(tmp_path: Path, seed: int) -> tuple[Path, driver.SeedSummary]:
    run = next(item for item in families.load_manifest().runs if item.seed == seed)
    database = tmp_path / "workbench.sqlite"
    with closing(storage.connect(database)) as conn:
        summary = driver.seed_world(
            conn, generate_world(families.spec_for(run), run.seed), oracle_dir=tmp_path / "oracle",
            provider_dir=tmp_path / "provider", model_mode=ModelMode.RULES, stale_after_ms=3_600_000,
        )
    return database, summary


def test_alert_history_survives_a_detector_that_stops_firing_before_the_cutoff(tmp_path: Path) -> None:
    database, summary = _seed_manifest_run(tmp_path, MODERATE_CARD_DROP_DEV_SEED)
    with closing(storage.connect(database)) as conn:
        deployed = runtime.active_merchant(conn, run_id=summary.run_id, at_ms=summary.cutoff_ms)
        built = packet.build_packet(
            conn, run_id=summary.run_id, incident_id=summary.incident_ids[0], cutoff_ms=summary.cutoff_ms,
            deployed=deployed, persist=False,
        )
    trigger = next(item for item in built.items if item.evidence_id == "ev_trigger")
    assert built.packet_version == "pkt-2"
    assert built.incident.trigger == "method_authorization_drop[card]"
    assert "method_authorization_drop[card]" in trigger.facts["fired_since_open"]
    # The case this guards: the detector that opened the incident no longer fires at the cutoff.
    assert "method_authorization_drop[card]" not in trigger.facts["fired"]


def test_alert_history_is_sorted_and_only_names_detectors(tmp_path: Path) -> None:
    database, summary = _seed_manifest_run(tmp_path, MODERATE_CARD_DROP_DEV_SEED)
    with closing(storage.connect(database)) as conn:
        deployed = runtime.active_merchant(conn, run_id=summary.run_id, at_ms=summary.cutoff_ms)
        built = packet.build_packet(
            conn, run_id=summary.run_id, incident_id=summary.incident_ids[0], cutoff_ms=summary.cutoff_ms,
            deployed=deployed, persist=False,
        )
    history = next(item for item in built.items if item.evidence_id == "ev_trigger").facts["fired_since_open"]
    assert history == sorted(history)
    assert all(isinstance(name, str) and name for name in history)
