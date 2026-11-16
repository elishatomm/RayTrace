"""The receiver fixture check after a provider secret rotation, end to end on simulator-generated worlds.

The simulator now records the provider's rotation as private state when it creates the run
(``storage.record_provider_rotation``), and ``runtime.active_merchant`` hands the receiver check the secret the
provider signs with at the check's time. Before, the check always signed with the provider's first secret: after a
coordinated rotation it reproduced a failure that was not there, and both rules versions blamed the rotation's own
config deploy with ``reproduced_in_fixture``; after a rotation the merchant missed, it passed while every new delivery
was refused. Seeds 19601-19700 are reserved for agent B; this file uses 19601, 19602 and 19604, and P12a's 17105.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from test_webhook_conformance import BUSY, COORDINATED_ROTATION, ROTATION_SEED

from recovery_workbench import packet, rules, selective, storage
from recovery_workbench.contracts import DiagnosisOutput, EvidenceLevel, ModelMode
from recovery_workbench.simulator import driver, families, merchant, runtime
from recovery_workbench.simulator.scenarios import (
    Deploy,
    LostDeliveries,
    ScenarioSpec,
    SecretRotation,
    WebhookConformance,
)
from recovery_workbench.simulator.world import ROTATED_SIGNING_REF, World, generate_world

OLD = merchant.PROVIDER_SIGNING_REF
NEXT = ROTATED_SIGNING_REF
RECEIVER_TEST = "signed_delivery_verifies"
FRESH_ROTATION_SEED = 19602
STAGED_ROTATION_SEED = 19601
PROVIDER_ONLY_SEED = 19604
RULES = {"rules_v2": rules.diagnose, "rules_v3": selective.rules_v3}


def seeded(tmp_path: Path, spec: ScenarioSpec, seed: int, overlay: WebhookConformance | None) -> tuple[
    sqlite3.Connection, World
]:
    world = generate_world(spec, seed, conformance=overlay)
    conn = storage.connect(tmp_path / f"world_{seed}.sqlite")
    driver.seed_world(conn, world, oracle_dir=None)
    return conn, world


def receiver_check_at(conn: sqlite3.Connection, world: World, at_ms: int) -> merchant.FixtureCheckResult:
    return merchant.run_fixture_check(RECEIVER_TEST, runtime.active_merchant(conn, run_id=world.run_id, at_ms=at_ms))


def answers(conn: sqlite3.Connection, world: World) -> dict[str, DiagnosisOutput]:
    """Both rules versions on the packet of the world's one incident, at its cutoff, as the evaluation builds it."""
    [incident] = storage.list_incidents(conn, run_id=world.run_id)
    deployed = runtime.active_merchant(conn, run_id=world.run_id, at_ms=world.cutoff_ms)
    built = packet.build_packet(conn, run_id=world.run_id, incident_id=incident.incident_id, cutoff_ms=world.cutoff_ms,
                                deployed=deployed, persist=False)
    return {name: diagnose(built) for name, diagnose in RULES.items()}


@pytest.fixture(params=[ROTATION_SEED, FRESH_ROTATION_SEED])
def coordinated(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, World]]:
    conn, world = seeded(tmp_path, BUSY, request.param, COORDINATED_ROTATION)
    with closing(conn):
        yield conn, world


def test_after_a_coordinated_rotation_the_receiver_check_signs_with_the_new_secret_and_passes(coordinated) -> None:
    conn, world = coordinated
    rotation_at = world.signing_rotation_at_ms
    assert rotation_at is not None
    before, after = (runtime.active_merchant(conn, run_id=world.run_id, at_ms=at)
                     for at in (rotation_at - 1, rotation_at))
    assert (before.provider_signing_ref, after.provider_signing_ref) == (OLD, NEXT)
    assert receiver_check_at(conn, world, rotation_at - 1).passed  # the baseline config and the provider's first secret
    assert receiver_check_at(conn, world, world.cutoff_ms).passed  # config@3 and the provider both on the new secret


def test_after_a_coordinated_rotation_no_rules_version_blames_the_rotation_with_a_fixture_reproduction(
    coordinated,
) -> None:
    conn, world = coordinated
    [rotation_deploy] = storage.list_deployments(conn, run_id=world.run_id, as_of_ms=world.cutoff_ms)
    assert rotation_deploy.deployed_at_ms == world.signing_rotation_at_ms
    for name, output in answers(conn, world).items():
        assert output.evidence_level is not EvidenceLevel.REPRODUCED_IN_FIXTURE, name
        assert output.faulting_change_id != rotation_deploy.deploy_id, name


def test_a_rotation_the_merchant_misses_is_reproduced_by_the_receiver_check(tmp_path: Path) -> None:
    """The provider rotates and the merchant deploys nothing: the old check passed while new deliveries were refused."""
    conn, world = seeded(tmp_path, BUSY, PROVIDER_ONLY_SEED, WebhookConformance(secret_rotation=SecretRotation(
        at_fraction=0.5)))
    with closing(conn):
        assert storage.list_deployments(conn, run_id=world.run_id, as_of_ms=world.cutoff_ms) == ()
        check = receiver_check_at(conn, world, world.cutoff_ms)
        assert (check.passed, check.detail) == (
            False, "signature verification failed for a delivery signed by the provider")
        for name, output in answers(conn, world).items():
            assert output.evidence_level is EvidenceLevel.REPRODUCED_IN_FIXTURE, name


def test_a_rotation_the_merchant_staged_passes_the_check_with_the_staged_secret(tmp_path: Path) -> None:
    spec = BUSY.model_copy(update={"harmless": (Deploy(variant="config@6", at_fraction=0.25),)})
    overlay = WebhookConformance(
        secret_rotation=SecretRotation(at_fraction=0.5),
        lost_deliveries=LostDeliveries(start_fraction=0.47, end_fraction=0.5, rate=1.0, lost="request"),
    )
    conn, world = seeded(tmp_path, spec, STAGED_ROTATION_SEED, overlay)
    with closing(conn):
        deployed = runtime.active_merchant(conn, run_id=world.run_id, at_ms=world.cutoff_ms)
        assert (deployed.provider_signing_ref, deployed.secret_ref, deployed.staged_secret_ref) == (NEXT, OLD, NEXT)
        assert receiver_check_at(conn, world, world.cutoff_ms).passed


@pytest.mark.parametrize("run_key", ["d3_dev_2", "d3_test_2"])  # config@3 faults: the provider never rotates
def test_a_frozen_receiver_config_fault_is_still_reproduced(tmp_path: Path, run_key: str) -> None:
    [run] = [run for run in families.load_manifest().runs if run.run_key == run_key]
    world = generate_world(families.spec_for(run), run.seed)
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        driver.seed_world(conn, world, oracle_dir=None, model_mode=ModelMode.RULES)
        assert storage.provider_secret_at(conn, run_id=world.run_id, at_ms=world.cutoff_ms) is None
        assert runtime.active_merchant(conn, run_id=world.run_id, at_ms=world.cutoff_ms).provider_signing_ref == OLD
        assert not receiver_check_at(conn, world, world.cutoff_ms).passed
        for name, output in answers(conn, world).items():
            assert output.evidence_level is EvidenceLevel.REPRODUCED_IN_FIXTURE, name


def test_a_database_migrated_before_rotations_were_recorded_signs_with_the_first_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "SIMULATOR_MIGRATIONS", storage.SIMULATOR_MIGRATIONS[:1])  # 1001 only
    with closing(storage.connect(tmp_path / "older.sqlite")) as conn:
        storage.migrate_simulator(conn)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sim_provider_signing'").fetchone()
        with storage.write_transaction(conn):
            storage.create_run(conn, storage.RunRecord(
                run_id="run_older_unit", seed=1, scenario="unit", split="unit", merchant_id="merchant_SYNTHETIC_A",
                generator_version="unit", clock_ms=0, model_mode=ModelMode.RULES))
            storage.set_merchant_profile(conn, run_id="run_older_unit", emits_categories=True)
        assert runtime.active_merchant(conn, run_id="run_older_unit", at_ms=0).provider_signing_ref == OLD
