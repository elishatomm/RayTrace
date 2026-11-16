"""Held-out merchant variants (phase 11b): fixture behaviour, registration, the held-out manifest and generated runs.

The six faults and six innocents were written after both attribution approaches were developed. Their truth
lives in heldout_variant_truth.json, apart from the development variants, and only heldout_manifest.json places
them. These tests may read both oracle files. World generation here uses seeds 15101-15200 only; seeds 13101 and
up belong to the experiment.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import JsonValue

from recovery_workbench import storage
from recovery_workbench.contracts import FailureReport, Hypothesis, WorkerResult
from recovery_workbench.evaluation import generate
from recovery_workbench.simulator import families, merchant
from recovery_workbench.simulator.world import DeploymentPlan, World, generate_world

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = REPO_ROOT / "fixtures" / "manifests"
TRUTH = json.loads((MANIFESTS / "heldout_variant_truth.json").read_text(encoding="utf-8"))
VARIANTS = TRUTH["variants"]
FAULTS = sorted(name for name, meaning in VARIANTS.items() if meaning["role"] == "fault")
INNOCENTS = sorted(name for name, meaning in VARIANTS.items() if meaning["role"] == "harmless")
DEVELOPMENT_VARIANTS = json.loads((MANIFESTS / "merchant_variant_truth.json").read_text(encoding="utf-8"))["variants"]
MANIFEST = json.loads((MANIFESTS / "heldout_manifest.json").read_text(encoding="utf-8"))
ENTRIES = MANIFEST["runs"]
# This file's reserved range is 15101-15200; one seed per manifest entry, in manifest order.
SMOKE_SEEDS = tuple(range(15101, 15101 + len(ENTRIES)))
CAPTURE_TEST = "captured_payment_builds_fulfilment"
LEAK_WORDS = re.compile(
    r"\b(bugs?|buggy|faults?|faulty|broken|fix|fixes|fixed|fixing|hotfix|regressions?|revert(ed|s)?)\b", re.IGNORECASE
)
FRAME_LINE = re.compile(r'^  File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<name>\S+)$', re.MULTILINE)
MINUTE_MS = 60_000


def deployed_with(*variants: str, emits_categories: bool = True) -> merchant.DeployedMerchant:
    overrides = dict(merchant.parse_variant_id(variant) for variant in variants)
    return merchant.compose(overrides, emits_categories=emits_categories)


def failing_tests(deployed: merchant.DeployedMerchant) -> set[str]:
    return {test_id for test_id in merchant.FIXTURE_TEST_IDS if not merchant.run_fixture_check(test_id, deployed).passed}


def capture_failure(deployed: merchant.DeployedMerchant) -> FailureReport:
    result = merchant.run_fixture_check(CAPTURE_TEST, deployed)
    assert result.failure is not None, f"{dict(deployed.composition)} was expected to raise on the capture fixture"
    return result.failure


def raised_type(trace: str) -> str:
    """The exception's qualified type name from the last line of a traceback."""
    return trace.rstrip().splitlines()[-1].split(":")[0]


def trace_files(trace: str) -> list[str]:
    return [match["file"] for match in FRAME_LINE.finditer(trace)]


def seeded_entries() -> list[dict[str, JsonValue]]:
    """Manifest entries with this file's seeds bound, the way the experiment binds its own."""
    return [{**entry, "seed": seed} for entry, seed in zip(ENTRIES, SMOKE_SEEDS, strict=True)]


def bound_runs() -> list[families.FamilyRun]:
    return [families.FamilyRun.model_validate(entry) for entry in seeded_entries()]


def deploys_of(world: World) -> tuple[DeploymentPlan, DeploymentPlan]:
    """(faulting deploy, innocent deploy) of a held-out world."""
    fault = world.faulting_deployment
    innocents = [plan for plan in world.deployments if plan.role == "harmless"]
    assert fault is not None, world.deployments
    assert len(innocents) == 1, world.deployments
    assert len(world.deployments) == 2, world.deployments
    return fault, innocents[0]


# --------------------------------------------------------------------------- registration


def test_heldout_variants_are_new_shipped_files_outside_the_development_registry() -> None:
    assert (len(FAULTS), len(INNOCENTS)) == (6, 6)
    evaluation_variants = {
        str(value) for run in families.load_manifest().runs for key, value in run.params.items()
        if key in ("fault", "harmless", "innocent")
    }
    for variant in VARIANTS:
        module, number = merchant.parse_variant_id(variant)
        assert number in merchant.available_variants(module), variant
        assert variant not in DEVELOPMENT_VARIANTS, variant
        assert variant not in evaluation_variants, variant
        assert variant not in merchant.CORRECTED_VARIANT_IDS, variant
        summaries = merchant.commit_summaries(variant)
        assert len(summaries) >= 4, variant
        assert len(set(summaries)) == len(summaries), variant


def test_the_development_registries_are_unchanged_and_the_heldout_classes_match_the_truth() -> None:
    assert dict(families.D1_FAULT_CLASSES) == {
        "payload@2": "top_frame_in_changed_file", "payload@3": "upstream_of_failing_frame",
        "config@2": "configuration_only",
    }
    assert merchant.CORRECTED_VARIANT_IDS == ("config@5", "config@6", "payload@5", "payload@6", "receiver@4")
    assert dict(families.HELDOUT_D1_FAULT_CLASSES) == {name: VARIANTS[name]["fault_class"] for name in FAULTS}
    assert {VARIANTS[name]["family"] for name in FAULTS} == {"D1"}
    # No held-out fault has an allowlisted corrected variant: adding one would change the fix options of every run.
    assert [VARIANTS[name]["repaired_by"] for name in FAULTS] == [[]] * len(FAULTS)


# --------------------------------------------------------------------------- fixture behaviour


@pytest.mark.parametrize("variant", FAULTS)
def test_heldout_fault_fails_exactly_the_registered_fixture_tests(variant: str) -> None:
    assert failing_tests(deployed_with(variant)) == set(VARIANTS[variant]["failing_fixture_tests"])


@pytest.mark.parametrize("variant", FAULTS)
def test_heldout_fault_raises_with_a_true_fixture_only_trace(variant: str) -> None:
    meaning = VARIANTS[variant]
    deployed = deployed_with(variant)
    failure = capture_failure(deployed)
    trace = failure.stack_trace
    assert trace_files(trace), trace
    assert trace_files(trace)[-1] == meaning["top_frame_file"], trace
    assert raised_type(trace).endswith(meaning["exception_type"]), trace
    assert failure.exception_category == meaning["category_when_emitted"]
    for match in FRAME_LINE.finditer(trace):
        module = match["file"].removeprefix("merchant_app/").removesuffix(".py")
        source = merchant.variant_source(module, deployed.composition[module])
        assert trace[match.end():].splitlines()[1].strip() == source.splitlines()[int(match["line"]) - 1].strip()
        assert re.search(rf"^def {match['name']}\(", source, re.M), match["name"]
    host_text = [str(Path.home()), str(REPO_ROOT), "/Users/", "site-packages", "fixtures", "modules/", "recovery_workbench"]
    assert not [text for text in host_text if text in trace], trace
    assert not re.search(r"\bv\d+\.py\b", trace), trace
    assert not LEAK_WORDS.search(trace), trace
    without_categories = capture_failure(deployed_with(variant, emits_categories=False))
    assert without_categories.exception_category is None
    assert without_categories.stack_trace == trace


def test_three_faults_raise_in_the_file_they_change_and_three_never_show_it() -> None:
    absent_from_trace = set()
    for variant in FAULTS:
        changed = merchant.logical_path(merchant.parse_variant_id(variant)[0])
        files = trace_files(capture_failure(deployed_with(variant)).stack_trace)
        if changed not in files:
            absent_from_trace.add(variant)
            continue
        assert files[-1] == changed, variant
    assert absent_from_trace == {"payload@7", "payload@8", "config@8"}


def test_the_unit_mismatch_fault_rejects_only_captures_above_one_thousand_rupees() -> None:
    deployed = deployed_with("orders@6")
    template = merchant.fixture_tests()[CAPTURE_TEST].event
    cases = (("upi", 100_100, True), ("card", 2_500_000, True), ("netbanking", 100_000, False), ("upi", 9_900, False))
    for method, amount_paise, rejected in cases:
        event = copy.deepcopy(template)
        entity = event["payload"]["payment"]["entity"]  # type: ignore[index]
        entity["method"], entity["amount"] = method, amount_paise  # type: ignore[index]
        if rejected:
            with pytest.raises(ValueError, match="above the per-order limit"):
                deployed.handle(event)
            continue
        assert deployed.handle(event)["action"] == "fulfil", (method, amount_paise)


@pytest.mark.parametrize("variant", INNOCENTS)
def test_heldout_innocent_passes_every_fixture_test(variant: str) -> None:
    assert failing_tests(deployed_with(variant)) == set()


def test_innocents_marked_as_traced_touch_a_file_in_the_heldout_fault_traces() -> None:
    traced = {file for variant in FAULTS for file in trace_files(capture_failure(deployed_with(variant)).stack_trace)}
    for variant in INNOCENTS:
        touched = merchant.logical_path(merchant.parse_variant_id(variant)[0])
        assert (touched in traced) is VARIANTS[variant]["appears_in_worker_traces"], variant


def test_no_manifest_innocent_masks_or_changes_its_faults_exception() -> None:
    for entry in ENTRIES:
        fault, innocent = entry["params"]["fault"], entry["params"]["harmless"]
        alone = capture_failure(deployed_with(fault)).stack_trace
        combined = deployed_with(fault, innocent)
        assert failing_tests(combined) == set(VARIANTS[fault]["failing_fixture_tests"]), entry["run_key"]
        trace = capture_failure(combined).stack_trace
        assert raised_type(trace) == raised_type(alone), entry["run_key"]
        assert trace_files(trace)[-1] == VARIANTS[fault]["top_frame_file"], entry["run_key"]


# --------------------------------------------------------------------------- the held-out manifest


def test_the_heldout_manifest_pairs_only_heldout_variants_with_balanced_positions_and_onsets() -> None:
    assert set(MANIFEST) == {"manifest_version", "generator_version", "notice", "runs"}
    assert len(ENTRIES) == 24
    assert len({entry["run_key"] for entry in ENTRIES}) == 24
    # Same shape as the evaluation manifest's D1 entries, minus the seed the experiment binds.
    assert all(set(entry) == {"run_key", "family", "variant", "split", "params"} for entry in ENTRIES)
    assert {(entry["family"], entry["split"]) for entry in ENTRIES} == {("D1", "test")}
    assert all(entry["variant"] == families.HELDOUT_D1_FAULT_CLASSES[entry["params"]["fault"]] for entry in ENTRIES)
    params = [entry["params"] for entry in ENTRIES]
    assert all(set(p) == {"fault", "categories", "harmless", "harmless_position", "onset"} for p in params)
    assert Counter(p["fault"] for p in params) == dict.fromkeys(FAULTS, 4)
    assert Counter(p["harmless"] for p in params) == dict.fromkeys(INNOCENTS, 4)
    assert len({(p["fault"], p["harmless"]) for p in params}) == 24
    assert all(merchant.parse_variant_id(p["fault"])[0] != merchant.parse_variant_id(p["harmless"])[0] for p in params)
    assert Counter(p["harmless_position"] for p in params) == {"before": 8, "after": 8, "gap": 8}
    assert Counter(p["onset"] for p in params) == {"prompt": 12, "delayed": 12}
    assert all(p["onset"] == "delayed" for p in params if p["harmless_position"] == "gap")
    assert Counter(p["categories"] for p in params) == {True: 12, False: 12}
    for variant in FAULTS + INNOCENTS:
        mine = [p for p in params if variant in (p["fault"], p["harmless"])]
        assert Counter(p["onset"] for p in mine) == {"prompt": 2, "delayed": 2}, variant
        assert Counter(p["categories"] for p in mine) == {True: 2, False: 2}, variant
        assert {p["harmless_position"] for p in mine} == {"before", "after", "gap"}, variant


def test_every_manifest_entry_builds_a_d1_world_with_its_deploys_where_the_entry_puts_them() -> None:
    for run in bound_runs():
        spec = families.spec_for(run)
        world = generate_world(spec, run.seed)
        fault, innocent = deploys_of(world)
        assert (fault.variant, innocent.variant) == (run.params["fault"], run.params["harmless"]), run.run_key
        assert spec.expected_hypothesis is Hypothesis.MERCHANT_WORKER_REGRESSION
        assert spec.emits_categories is run.params["categories"]
        if run.params["harmless_position"] == "before":
            assert innocent.deployed_at_ms < fault.deployed_at_ms, run.run_key
        else:
            assert fault.deployed_at_ms < innocent.deployed_at_ms, run.run_key
        lull = spec.quiet_gap
        assert (lull is not None) is (run.params["onset"] == "delayed"), run.run_key
        if lull is None:
            continue
        duration = world.cutoff_ms - world.start_ms
        lull_start = world.start_ms + int(lull.start_fraction * duration)
        lull_end = world.start_ms + int(lull.end_fraction * duration)
        assert lull_start <= fault.deployed_at_ms < lull_end, run.run_key
        assert not [order for order in world.orders if lull_start <= order.created_at_ms < lull_end], run.run_key


# --------------------------------------------------------------------------- generated runs (database, in Docker)


Generated = tuple[Path, dict[str, JsonValue], sqlite3.Connection]


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Generated]:
    """Every entry seeded through the evaluation generator, the path the experiment takes with its own seeds."""
    root = tmp_path_factory.mktemp("heldout")
    manifest_path = root / "heldout_manifest_with_seeds.json"
    manifest_path.write_text(json.dumps({**MANIFEST, "runs": seeded_entries()}), encoding="utf-8")
    data_dir = root / "data"
    report = generate.generate_split("test", data_dir, manifest_path=manifest_path)
    with closing(storage.connect(data_dir / generate.DATABASE_FILE)) as conn:
        yield data_dir, report, conn


def generated_runs(report: dict[str, JsonValue]) -> Iterator[tuple[dict[str, JsonValue], families.FamilyRun, World]]:
    by_seed = {run.seed: run for run in bound_runs()}
    entries: list[dict[str, JsonValue]] = report["runs"]  # type: ignore[assignment]
    assert sorted(entry["seed"] for entry in entries) == list(SMOKE_SEEDS)  # type: ignore[type-var]
    for entry in entries:
        run = by_seed[entry["seed"]]  # type: ignore[index]
        yield entry, run, generate_world(families.spec_for(run), run.seed)


def test_every_entry_opens_an_incident_and_its_oracle_names_the_faulting_deploy(generated) -> None:
    data_dir, report, _ = generated
    assert report["runs_without_incident"] == []
    for entry, run, world in generated_runs(report):
        fault, innocent = deploys_of(world)
        oracle = json.loads((data_dir / str(entry["oracle_file"])).read_text(encoding="utf-8"))
        assert (oracle["family"], oracle["variant"], oracle["faulting_variant"]) == ("D1", run.variant, run.params["fault"])
        assert oracle["faulting_deploy_id"] == oracle["expected"]["faulting_change_id"] == fault.deploy_id
        assert oracle["harmless_deploy_ids"] == [innocent.deploy_id]
        assert oracle["expected"]["hypothesis"] == Hypothesis.MERCHANT_WORKER_REGRESSION.value


def test_the_worker_fails_with_the_registered_exception_and_a_fixture_only_trace(generated) -> None:
    _, report, conn = generated
    for entry, run, _ in generated_runs(report):
        meaning = VARIANTS[run.params["fault"]]
        category = meaning["category_when_emitted"] if run.params["categories"] else None
        attempts = storage.list_worker_attempts(conn, run_id=str(entry["run_id"]))
        failed = [attempt for attempt in attempts if attempt.result is WorkerResult.FAILED]
        assert failed, run.run_key
        for attempt in failed:
            trace = attempt.stack_trace or ""
            files = trace_files(trace)
            assert files, trace
            assert all(file.startswith("merchant_app/") for file in files), trace
            assert files[-1] == meaning["top_frame_file"], trace
            assert raised_type(trace).endswith(meaning["exception_type"]), trace
            assert attempt.exception_category == category, run.run_key


def test_both_deploys_precede_the_incident_and_delayed_failures_start_after_the_lull(generated) -> None:
    _, report, conn = generated
    for entry, run, world in generated_runs(report):
        fault, innocent = deploys_of(world)
        run_id = str(entry["run_id"])
        incident = storage.list_incidents(conn, run_id=run_id)[0]
        visible = storage.list_deployments(conn, run_id=run_id, as_of_ms=incident.evidence_cutoff_ms)
        assert {deploy.deploy_id for deploy in visible} == {fault.deploy_id, innocent.deploy_id}, run.run_key
        latest = fault if run.params["harmless_position"] == "before" else innocent
        assert visible[-1].deploy_id == latest.deploy_id, run.run_key
        if run.params["onset"] != "delayed":
            continue
        failures = [attempt.started_at_ms for attempt in storage.list_worker_attempts(conn, run_id=run_id)
                    if attempt.result is WorkerResult.FAILED]
        assert min(failures) > latest.deployed_at_ms, run.run_key
        assert min(failures) - fault.deployed_at_ms > 20 * MINUTE_MS, run.run_key
