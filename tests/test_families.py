"""The frozen evaluation manifest, the family builders and development-split generation."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest

from recovery_workbench import diagnosis, metrics, storage
from recovery_workbench.contracts import Hypothesis, Operation
from recovery_workbench.evaluation import generate
from recovery_workbench.evaluation.generate import DATA_MANIFEST_FILE
from recovery_workbench.simulator import families, merchant
from recovery_workbench.simulator.world import generate_world

MANIFEST = families.load_manifest()


def runs_of(family: str, split: str | None = None) -> list[families.FamilyRun]:
    return [run for run in MANIFEST.runs if run.family == family and (split is None or run.split == split)]


def test_the_manifest_holds_six_families_of_three_dev_and_five_test_runs():
    assert len(MANIFEST.runs) == 48
    for family in families.FAMILIES:
        assert Counter(run.split for run in runs_of(family)) == {"dev": 3, "test": 5}, family
    assert len({run.run_key for run in MANIFEST.runs}) == 48
    dev_seeds = {run.seed for run in MANIFEST.runs if run.split == "dev"}
    test_seeds = {run.seed for run in MANIFEST.runs if run.split == "test"}
    assert len(dev_seeds) == 18
    assert len(test_seeds) == 30
    assert not dev_seeds & test_seeds


def test_variant_coverage_matches_the_evaluation_design():
    d1 = runs_of("D1")
    assert {run.params["fault"] for run in d1} == {"payload@2", "payload@3", "config@2"}
    assert {run.params["categories"] for run in d1} == {True, False}
    assert {run.params["harmless_position"] for run in d1} == {"before", "after", "gap"}
    delayed = [run for run in d1 if run.params["onset"] == "delayed"]
    assert Counter(run.split for run in delayed) == {"dev": 1, "test": 2}
    assert {run.params["fault"] for run in delayed} == {"payload@2", "payload@3", "config@2"}
    assert all(run.params["harmless_position"] == "gap" and run.params["harmless"] in ("handler@2", "orders@2")
               for run in delayed)
    assert {run.params["fault"] for run in runs_of("D1", "dev")} == {"payload@2", "payload@3", "config@2"}
    assert {(run.params["method"], run.params["magnitude"]) for run in runs_of("D2")} >= {
        ("upi", "severe"), ("upi", "moderate"), ("card", "severe"), ("card", "moderate"), ("netbanking", "severe"),
        ("netbanking", "moderate")}
    assert {run.params["fault"] for run in runs_of("D3")} == {"receiver@2", "config@3"}
    assert {run.params["mode"] for run in runs_of("D4")} == {"late_authorization", "stale_snapshot", "same_time_contradiction"}
    assert {run.params["mode"] for run in runs_of("D5")} == {"mix_shift", "sparse_cohort", "missing_observations"}
    assert {run.params["base"] for run in runs_of("D6")} == {"D2", "D5"}
    for split in ("dev", "test"):  # each split has an innocent latest deploy that touches a traced file
        assert any(run.params.get("harmless_position") == "after" and run.params["harmless"] in ("handler@2", "orders@2")
                   for run in runs_of("D1", split))


def test_every_run_builds_a_neutral_spec_with_policy_consistent_expectations():
    for run in MANIFEST.runs:
        spec = families.spec_for(run)
        assert spec.name == families.EVALUATION_SCENARIO_NAME
        assert (spec.family, spec.split, spec.variant_label) == (run.family, run.split, run.variant)
        assert spec.expected_operation in spec.acceptable_operations
        assert spec.acceptable_operations <= diagnosis.OPERATION_POLICY[spec.expected_hypothesis]
        world = generate_world(spec, run.seed)
        assert re.fullmatch(r"run_[0-9a-f]{10}", world.run_id)
        assert str(run.seed) not in world.run_id + world.orders[0].order_id + world.attempts[0].payment_id
    d6 = {run.params["base"]: families.spec_for(run).expected_hypothesis for run in runs_of("D6")}
    assert d6 == {"D2": Hypothesis.PROVIDER_METHOD_DEGRADATION, "D5": Hypothesis.NO_INCIDENT}


def test_dev_and_test_runs_share_no_ids_deploy_hashes_or_texts():
    seen: dict[str, dict[str, set[str]]] = {"dev": {}, "test": {}}
    for run in MANIFEST.runs:
        world = generate_world(families.spec_for(run), run.seed)
        bucket = seen[run.split]
        bucket.setdefault("orders", set()).update(order.order_id for order in world.orders)
        bucket.setdefault("deploy_ids", set()).update(plan.deploy_id for plan in world.deployments)
        bucket.setdefault("summaries", set()).update(plan.summary for plan in world.deployments)
        bucket.setdefault("log_texts", set()).update(log.message for log in world.logs)
    for key in ("orders", "deploy_ids", "summaries", "log_texts"):
        assert seen["dev"][key], key
        assert seen["test"][key], key
        assert not seen["dev"][key] & seen["test"][key], key


@pytest.fixture(scope="module")
def dev_split(tmp_path_factory: pytest.TempPathFactory):
    data_dir = tmp_path_factory.mktemp("dev_split")
    assert generate.main(["--split", "dev", "--data-dir", str(data_dir)]) == 0
    manifest = json.loads((data_dir / "data_manifest.json").read_text(encoding="utf-8"))
    with closing(storage.connect(data_dir / "workbench.sqlite")) as conn:
        yield data_dir, manifest, conn


FROZEN_DEV_DATA = Path(__file__).resolve().parents[1] / "evaluation" / "runs" / "dev_think_off" / DATA_MANIFEST_FILE


def test_the_dev_split_reproduces_the_observable_data_of_the_frozen_evaluation(dev_split):
    """Any generator change that moves an observable row of the committed M4 dev data fails here.

    Observable hashes only: oracle files changed on purpose with owner decision Q8.
    """
    _, manifest, _ = dev_split
    frozen = json.loads(FROZEN_DEV_DATA.read_text(encoding="utf-8"))

    def observable(document: dict) -> dict[int, tuple[str, int, str]]:
        return {entry["seed"]: (entry["run_id"], entry["cutoff_ms"], entry["observable_sha256"])
                for entry in document["runs"]}

    assert len(frozen["runs"]) == 18
    assert observable(manifest) == observable(frozen)


def test_every_dev_run_opens_an_incident_and_records_its_hashes(dev_split):
    data_dir, manifest, _ = dev_split
    assert (manifest["split"], len(manifest["runs"])) == ("dev", 18)
    assert {"world_generator_version", "family_generator_version", "evaluation_manifest_sha256",
            "workbench_version", "stale_after_ms"} <= set(manifest)
    for entry in manifest["runs"]:
        assert entry["incident_ids"], entry["run_id"]
        assert re.fullmatch(r"[0-9a-f]{64}", entry["observable_sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", entry["oracle_sha256"])
        assert entry["sizes"]["orders"] >= 100
        assert (data_dir / entry["oracle_file"]).is_file()
        assert (data_dir / entry["provider_records_file"]).is_file()
    text = (data_dir / "data_manifest.json").read_text(encoding="utf-8")
    assert not re.search(r'"D[1-6]"|family"|variant"|hypothesis', text)  # labels live in the oracle only


def test_each_oracle_holds_the_scored_expectations(dev_split):
    data_dir, manifest, _ = dev_split
    by_seed = {run.seed: run for run in MANIFEST.runs}
    for entry in manifest["runs"]:
        oracle = json.loads((data_dir / entry["oracle_file"]).read_text(encoding="utf-8"))
        run = by_seed[entry["seed"]]
        expected = oracle["expected"]
        assert (oracle["family"], oracle["variant"]) == (run.family, run.variant)
        assert {"hypothesis", "faulting_change_id", "affected_method", "acceptable_operations"} <= set(expected)
        if run.family in ("D1", "D3"):
            assert expected["faulting_change_id"] == oracle["faulting_deploy_id"] != "none"
        else:
            assert expected["faulting_change_id"] == "none"
        allowed = {op.value for op in diagnosis.OPERATION_POLICY[Hypothesis(expected["hypothesis"])]}
        assert set(expected["acceptable_operations"]) <= allowed
        assert oracle["observable_dataset_sha256"] == entry["observable_sha256"]


def _fired_anywhere(conn: sqlite3.Connection, run_id: str) -> set[str]:
    return {str(item.finding["detector"]) for item in storage.list_evidence(conn, run_id=run_id) if item.finding["fired"]}


def test_d5_runs_fire_the_symptom_and_never_a_within_method_detector(dev_split):
    _, manifest, conn = dev_split
    by_seed = {run.seed: run for run in MANIFEST.runs}
    for entry in manifest["runs"]:
        if by_seed[entry["seed"]].family != "D5":
            continue
        fired = _fired_anywhere(conn, entry["run_id"])
        assert metrics.ORDERS_NOT_COMPLETING in fired, entry["run_id"]
        assert metrics.METHOD_DROP not in fired, entry["run_id"]


def test_delayed_onset_puts_the_innocent_deploy_between_the_fault_and_the_first_failure(dev_split):
    _, manifest, conn = dev_split
    by_seed = {run.seed: run for run in MANIFEST.runs}
    checked = 0
    for entry in manifest["runs"]:
        run = by_seed[entry["seed"]]
        if run.params.get("onset") != "delayed":
            continue
        [incident] = storage.list_incidents(conn, run_id=entry["run_id"])
        fault, innocent = storage.list_deployments(conn, run_id=entry["run_id"], as_of_ms=incident.evidence_cutoff_ms)
        failures = [a.started_at_ms for a in storage.list_worker_attempts(conn, run_id=entry["run_id"])
                    if a.result.value == "failed"]
        assert fault.deployed_at_ms < innocent.deployed_at_ms < min(failures), entry["run_id"]
        assert min(failures) - fault.deployed_at_ms > 20 * 60_000
        assert innocent.summary in merchant.commit_summaries(str(run.params["harmless"]))
        checked += 1
    assert checked == 1


def test_an_innocent_deploy_is_the_latest_one_at_the_incident_cutoff_when_placed_after(dev_split):
    _, manifest, conn = dev_split
    by_seed = {run.seed: run for run in MANIFEST.runs}
    checked = 0
    for entry in manifest["runs"]:
        run = by_seed[entry["seed"]]
        if run.family not in ("D1", "D3") or run.params.get("harmless_position") != "after":
            continue
        [incident] = storage.list_incidents(conn, run_id=entry["run_id"])
        visible = storage.list_deployments(conn, run_id=entry["run_id"], as_of_ms=incident.evidence_cutoff_ms)
        assert len(visible) == 2, entry["run_id"]
        assert visible[-1].summary in merchant.commit_summaries(str(run.params["harmless"]))
        checked += 1
    assert checked >= 2


def test_unknown_parameters_are_refused():
    bad = families.FamilyRun(run_key="x", family="D4", variant="v", split="dev", seed=1, params={"mode": "chaos"})
    with pytest.raises(ValueError, match="D4 mode"):
        families.spec_for(bad)
    mismatched = families.FamilyRun(run_key="y", family="D5", variant="mix_shift", split="dev", seed=1,
                                    params={"mode": "sparse_cohort"})
    with pytest.raises(ValueError, match="describe variant"):
        families.spec_for(mismatched)
    assert Operation.NO_ACTION in families.spec_for(runs_of("D5", "dev")[0]).acceptable_operations
