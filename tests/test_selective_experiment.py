"""The phase-11b test harness (experiments/selective): seeds, worlds, build, arm stage and decision logic.

Nothing here runs a real system on a test packet. The router, rules-v3, rules-v2, the comparator and the model are
stand-ins wherever a stage would call them: their answers on these packets are the experiment's results, and none may
exist before the pre-registration is committed. The decisions are driven with hand-made answer sets. Worlds are
generated here only to check that two of them rebuild byte-identically, with every system made to fail if called.
The tests depend on no prompt or validator of the tree, so they pass before and after diag-p5 lands.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import pytest

import recovery_workbench
from recovery_workbench import attribution_rules, diagnosis, rules, selective
from recovery_workbench.contracts import DiagnosisOutput, DiagnosisStatus, EvidencePacket
from recovery_workbench.evaluation import run as evaluation_run

REPO_ROOT = Path(__file__).resolve().parents[1]
SELECTIVE_DIR = REPO_ROOT / "experiments" / "selective"
MANIFESTS = REPO_ROOT / "fixtures" / "manifests"
FROZEN_SETTINGS_PATH = REPO_ROOT / "evaluation" / "frozen_settings.json"
# Earlier seed ranges, typed here rather than imported from the code that checks them.
EARLIER_RANGES = ((1001, 2055), (7101, 7155), (8101, 8155), (9101, 9155), (9501, 9700), (10101, 10158),
                  (11101, 12100), (12101, 12600), (14101, 14200), (15101, 15200), (16101, 16400))
NO_ADVANTAGE = "no detectable advantage in this sample"
MORE_WRONG = "S has more wrong attributions than R"
WORSENED_P = "worsened-direction p <= 0.05"
Edit = Callable[[dict, dict], dict]


def _selective_module(name: str) -> ModuleType:
    """A module of experiments/selective. Stages load under distinct names, because round 3 has stages with the same
    file names; selective_design keeps its bare name, the one the stages import it by."""
    if str(SELECTIVE_DIR) not in sys.path:
        sys.path.append(str(SELECTIVE_DIR))
    if name == "selective_design":
        return importlib.import_module(name)
    qualified = f"selective_experiment_{name}"
    if qualified not in sys.modules:
        spec = importlib.util.spec_from_file_location(qualified, SELECTIVE_DIR / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
    return sys.modules[qualified]


design = _selective_module("selective_design")
build_packets = _selective_module("build_packets")
diagnose_arm = _selective_module("diagnose_arm")
summary = _selective_module("summarize")
FIRST_SEED, SECOND_SEED = design.MODEL_SEEDS
SELECTIVE_ARMS = [design.arm_for(design.SELECTIVE_SYSTEM, seed).name for seed in design.MODEL_SEEDS]
MODEL_ALL_ARMS = [design.arm_for(design.MODEL_ALL_SYSTEM, seed).name for seed in design.MODEL_SEEDS]
MODEL, RULES = design.MODEL_ROUTE, design.RULES_ROUTE


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


FROZEN = _read(FROZEN_SETTINGS_PATH)
FROZEN_SHA256 = hashlib.sha256(FROZEN_SETTINGS_PATH.read_bytes()).hexdigest()
CODE = {"commit": "c" * 40, "code_export": "/export", "package_dir": "/export/src/recovery_workbench",
        "versions": dict(design.CODE_VERSIONS), "sources_sha256": {}, "package_sources_sha256": "p" * 64}


@pytest.fixture(scope="module")
def cases_document() -> dict:
    return _read(SELECTIVE_DIR / "cases.json")


@pytest.fixture(scope="module")
def cases(cases_document: dict) -> list[dict]:
    return cases_document["cases"]


@pytest.fixture(scope="module")
def heldout(cases: list[dict]) -> list[dict]:
    return [case for case in cases if case["role"] == "heldout"]


@pytest.fixture(scope="module")
def controls(cases: list[dict]) -> list[dict]:
    return [case for case in cases if case["role"] == "control"]


# --------------------------------------------------------------------------- seeds and the case list


def test_heldout_entry_i_takes_seeds_13101_plus_2i_and_13102_plus_2i() -> None:
    entries = _read(MANIFESTS / "heldout_manifest.json")["runs"]
    designed = [case for case in design.experiment_cases() if case.role == "heldout"]

    assert [(case.source_run_key, case.replicate, case.run.seed) for case in designed] == [
        (entry["run_key"], world, 13101 + 2 * index + world) for index, entry in enumerate(entries) for world in (0, 1)]
    assert (designed[0].run.seed, designed[1].run.seed, designed[-1].run.seed) == (13101, 13102, 13148)
    for case in designed:
        entry = next(entry for entry in entries if entry["run_key"] == case.source_run_key)
        assert (case.run.family, case.run.variant, case.run.split, case.run.params) == (
            entry["family"], entry["variant"], "test", entry["params"])
        assert case.case_id == f"{entry['run_key']}_{case.run.seed}"


def test_the_controls_are_the_first_two_test_parameter_sets_of_d2_to_d6_on_seeds_13149_to_13158() -> None:
    manifest = {run["run_key"]: run for run in _read(MANIFESTS / "evaluation_manifest.json")["runs"]}
    controls = [case for case in design.experiment_cases() if case.role == "control"]
    expected = [f"d{family}_test_{position}" for family in (2, 3, 4, 5, 6) for position in (1, 2)]

    assert [(case.source_run_key, case.run.seed) for case in controls] == list(zip(expected, range(13149, 13159)))
    for case in controls:
        source = manifest[case.source_run_key]
        assert (case.run.family, case.run.variant, case.run.split, case.run.params) == (
            source["family"], source["variant"], "test", source["params"])
        assert case.case_id == f"control_{source['run_key']}_{case.run.seed}"


def test_every_seed_lies_in_13101_to_13158_once_and_no_earlier_measurement_used_it() -> None:
    seeds = [case.run.seed for case in design.experiment_cases()]
    manifest_seeds = {run["seed"] for run in _read(MANIFESTS / "evaluation_manifest.json")["runs"]}
    built = {case["seed"] for directory in ("injection", "injection/round2", "injection/round3")
             for case in _read(REPO_ROOT / "experiments" / directory / "cases.json")["cases"]}

    assert seeds == list(range(13101, 13159))
    assert set(seeds).isdisjoint(manifest_seeds | built)
    assert not [seed for seed in seeds for low, high in EARLIER_RANGES if low <= seed <= high]
    assert all(design.seed_problem(seed) is None for seed in seeds)


def test_a_seed_outside_the_block_or_taken_twice_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    assert design.seed_problem(13100).startswith("outside 13101-13158")
    assert design.seed_problem(13159).startswith("outside 13101-13158")
    with pytest.raises(ValueError, match="outside the design"):
        design.heldout_seed(24, 0)
    with pytest.raises(ValueError, match="outside the design"):
        design.heldout_seed(0, 2)
    longer = design.load_heldout_manifest()
    longer = longer.model_copy(update={"runs": (*longer.runs, longer.runs[0])})
    with pytest.raises(ValueError, match="exactly 24"):
        design.experiment_cases(longer)
    monkeypatch.setattr(design, "CONTROL_FIRST_SEED", 13150)
    with pytest.raises(ValueError, match="13101-13158 once"):
        design.experiment_cases()  # the last control would take 13159
    monkeypatch.setattr(design, "CONTROL_FIRST_SEED", 13148)
    with pytest.raises(ValueError, match="repeated"):
        design.experiment_cases()  # the first control would take heldout_24's second seed


def test_cases_json_lists_the_designed_worlds_in_run_order(cases_document: dict, cases: list[dict]) -> None:
    designed = design.experiment_cases()

    assert [case["case_id"] for case in cases] == [case.case_id for case in designed]
    for record, case in zip(cases, designed, strict=True):
        assert (record["role"], record["family"], record["variant"], record["seed"], record["split"], record["params"]) == (
            case.role, case.run.family, case.run.variant, case.run.seed, "test", case.run.params)
        assert record["source"] == {"manifest": case.source_manifest, "run_key": case.source_run_key,
                                    "replicate": case.replicate}
    assert Counter(case["role"] for case in cases) == {"heldout": 48, "control": 10}
    assert cases_document["seed_check"]["overlaps"] == {"manifest": [], "ranges": {}, "repeated": []}
    assert cases_document["seed_check"]["seeds"] == list(range(13101, 13159))
    assert cases_document["inputs"]["heldout_manifest"]["sha256"] == hashlib.sha256(
        (MANIFESTS / "heldout_manifest.json").read_bytes()).hexdigest()


def test_every_packet_has_its_build_hash_and_offers_a_heldout_worlds_two_deployments(cases: list[dict]) -> None:
    for case in cases:
        built = EvidencePacket.model_validate_json((SELECTIVE_DIR / case["packet_file"]).read_text(encoding="utf-8"))
        assert built.packet_hash() == case["packet_hash"], case["case_id"]
        assert list(built.candidate_change_ids) == case["candidate_change_ids"]
        if case["role"] != "heldout":
            continue
        assert case["expected"]["faulting_change_id"] == case["faulting_deploy_id"]
        assert len(case["harmless_deploy_ids"]) == 1
        assert {case["faulting_deploy_id"], *case["harmless_deploy_ids"]} <= set(built.candidate_change_ids)
        assert case["expected"]["hypothesis"] == "merchant_worker_regression"


def test_cases_json_records_the_preregistration_sha256_and_whether_it_is_a_draft(cases_document: dict) -> None:
    digest = hashlib.sha256((SELECTIVE_DIR / "PREREGISTRATION.md").read_bytes()).hexdigest()

    assert cases_document["preregistration"] == {"file": "PREREGISTRATION.md", "sha256": digest}
    assert cases_document["preregistration_draft"] == design.preregistration_is_draft(SELECTIVE_DIR)
    assert cases_document["design"]["code_under_test"]["versions"] == dict(design.CODE_VERSIONS)


# --------------------------------------------------------------------------- the build


def _forbidden(name: str) -> Callable[..., object]:
    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"{name} ran during the build; no system may answer a test packet before the commit")
    return refuse


SYSTEMS_THAT_MUST_NOT_RUN = (
    (rules, "diagnose"), (selective, "route"), (selective, "rules_v3"), (selective, "answer_rules_v3"),
    (selective, "diagnose_selective"), (attribution_rules, "attribute"), (diagnosis, "diagnose"),
    (diagnosis, "validate_diagnosis"), (diagnosis, "render_packet"), (evaluation_run, "run_rules"),
    (evaluation_run, "run_model"),
)


def test_two_rebuilt_worlds_are_byte_identical_and_no_system_runs_while_building(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases: list[dict],
) -> None:
    for module, name in SYSTEMS_THAT_MUST_NOT_RUN:
        monkeypatch.setattr(module, name, _forbidden(f"{module.__name__}.{name}"))
    chosen = [case for case in design.experiment_cases() if case.case_id in ("heldout_01_13101", "control_d3_test_2_13152")]
    committed = {case["case_id"]: case for case in cases}

    records = build_packets.build_all(chosen, tmp_path)

    assert [record["case_id"] for record in records] == ["heldout_01_13101", "control_d3_test_2_13152"]
    for record in records:
        packet_file = record["packet_file"]
        assert (tmp_path / packet_file).read_bytes() == (SELECTIVE_DIR / packet_file).read_bytes(), packet_file
        assert record == committed[record["case_id"]]


def test_a_heldout_world_without_both_deployments_as_candidates_is_unfit(cases: list[dict]) -> None:
    case = next(case for case in cases if case["role"] == "heldout")
    built = EvidencePacket.model_validate_json((SELECTIVE_DIR / case["packet_file"]).read_text(encoding="utf-8"))
    designed = next(item for item in design.experiment_cases() if item.case_id == case["case_id"])
    truth = {"faulting_deploy_id": case["faulting_deploy_id"], "harmless_deploy_ids": case["harmless_deploy_ids"],
             "expected": case["expected"]}

    fit = build_packets.BuiltWorld(designed, built, truth, None)  # type: ignore[arg-type]
    without_innocent = build_packets.BuiltWorld(
        designed, built.model_copy(update={"candidate_change_ids": (case["faulting_deploy_id"],)}), truth, None)  # type: ignore[arg-type]

    assert build_packets.input_problems(fit) == []
    assert build_packets.input_problems(without_innocent)[0].startswith("a held-out world needs")


def test_the_build_refuses_an_output_directory_that_holds_a_build(tmp_path: Path) -> None:
    (tmp_path / "cases.json").write_text("{}", encoding="utf-8")
    argv = ["--output-dir", str(tmp_path)]
    with pytest.raises(SystemExit):
        build_packets.main(argv)


# --------------------------------------------------------------------------- the pre-registration, addenda and arms


def _registered(directory: Path, text: str, cases_document: Mapping) -> dict:
    """A copy of cases.json whose recorded pre-registration is ``text``, written into ``directory``."""
    (directory / "PREREGISTRATION.md").write_text(text, encoding="utf-8")
    document = {**cases_document, "preregistration": design.preregistration_record(directory)}
    (directory / "cases.json").write_text(json.dumps(document), encoding="utf-8")
    return document


def test_a_draft_or_changed_preregistration_stops_the_arm_stage_and_the_summariser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases_document: dict,
) -> None:
    made: list[str] = []
    monkeypatch.setattr(diagnose_arm, "arm_client", lambda arm, frozen: made.append(arm.name))
    arm_stage = ["--arm", SELECTIVE_ARMS[0], "--code-export", "/export", "--experiment-dir", str(tmp_path)]
    summariser = ["--code-export", "/export", "--experiment-dir", str(tmp_path)]

    _registered(tmp_path, "# DRAFT: Selective AI, pre-registration\n\nNot final.\n", cases_document)
    for main, argv in ((diagnose_arm.main, arm_stage), (summary.main, summariser)):
        with pytest.raises(SystemExit, match="still a draft"):
            main(argv)

    _registered(tmp_path, "# Selective AI, pre-registration\n\nFinal.\n", cases_document)
    (tmp_path / "PREREGISTRATION.md").write_text("# Selective AI, pre-registration\n\nEdited.\n", encoding="utf-8")
    for main, argv in ((diagnose_arm.main, arm_stage), (summary.main, summariser)):
        with pytest.raises(SystemExit, match="dated addendum"):
            main(argv)
    assert made == []  # no model client was ever made


@pytest.mark.parametrize(("change", "refusal"), [
    ("edited", "frozen once recorded"),
    ("unrecorded", "frozen once recorded"),
    ("record_missing", "but no addenda.json"),
])
def test_an_edited_or_unrecorded_addendum_is_refused(tmp_path: Path, change: str, refusal: str) -> None:
    assert design.checked_addenda(SELECTIVE_DIR) == []
    (tmp_path / "ADDENDUM_2026-11-13.md").write_text("# An addendum\n", encoding="utf-8")
    entries = [{"file": "ADDENDUM_2026-11-13.md",
                "sha256": hashlib.sha256((tmp_path / "ADDENDUM_2026-11-13.md").read_bytes()).hexdigest()}]
    (tmp_path / "addenda.json").write_text(json.dumps({"addenda": entries}), encoding="utf-8")
    assert [entry["file"] for entry in design.checked_addenda(tmp_path)] == ["ADDENDUM_2026-11-13.md"]
    if change == "edited":
        (tmp_path / "ADDENDUM_2026-11-13.md").write_text("# An addendum, edited\n", encoding="utf-8")
    if change == "unrecorded":
        (tmp_path / "ADDENDUM_2026-11-14.md").write_text("# A later addendum nobody recorded\n", encoding="utf-8")
    if change == "record_missing":
        (tmp_path / "addenda.json").unlink()

    with pytest.raises(SystemExit, match=refusal):
        design.checked_addenda(tmp_path)


def test_the_arms_are_s_and_m_all_at_both_seeds_and_only_s_decides(cases: list[dict]) -> None:
    targets = {case["case_id"]: MODEL if case["case_id"] in ("heldout_02_13103", "control_d3_test_1_13151") else RULES
               for case in cases}

    assert list(design.ARMS) == [*SELECTIVE_ARMS, *MODEL_ALL_ARMS] == [
        "selective_20261111", "selective_20261112", "model_all_20261111", "model_all_20261112"]
    assert [arm.decides for arm in design.ARMS.values()] == [True, True, False, False]
    assert design.arm_case_ids(design.ARMS[SELECTIVE_ARMS[1]], cases, targets) == ["heldout_02_13103",
                                                                                   "control_d3_test_1_13151"]
    assert design.arm_case_ids(design.ARMS[MODEL_ALL_ARMS[0]], cases, targets) == [
        case["case_id"] for case in cases if case["role"] == "heldout"]
    with pytest.raises(SystemExit, match="not an arm"):
        design.arm_named("diag-p5_val-6_20261111")


def test_the_pins_are_the_frozen_router_comparator_and_rules_files() -> None:
    package_dir = Path(recovery_workbench.__file__).resolve().parent
    assert {name: hashlib.sha256((package_dir / name).read_bytes()).hexdigest() for name in design.FROZEN_SOURCES} == dict(
        design.FROZEN_SOURCES), "the router, comparator or rules-v2 changed since phase 11b froze them"
    assert (selective.ROUTER_VERSION, selective.RULES_V3_VERSION, attribution_rules.ATTRIBUTION_RULES_VERSION,
            rules.RULES_VERSION) == ("router-v1", "rules-v3", "attribution-v1", "rules-v2")


def test_the_stages_refuse_any_code_but_the_pinned_export(monkeypatch: pytest.MonkeyPatch) -> None:
    package_dir = Path(recovery_workbench.__file__).resolve().parent
    export = package_dir.parents[1]
    actual = {name: hashlib.sha256((package_dir / name).read_bytes()).hexdigest() for name in design.FROZEN_SOURCES}
    monkeypatch.setattr(design, "FROZEN_SOURCES", actual)
    elsewhere = export / "elsewhere"
    with pytest.raises(SystemExit, match="not from the export"):
        design.checked_code_under_test(elsewhere)
    monkeypatch.setattr(diagnosis, "PROMPT_VERSION", "diag-p4")
    with pytest.raises(SystemExit, match="must report"):
        design.checked_code_under_test(export)
    monkeypatch.setattr(diagnosis, "PROMPT_VERSION", "diag-p5")
    monkeypatch.setattr(diagnosis, "VALIDATOR_VERSION", "val-6")
    monkeypatch.setattr(design, "FROZEN_SOURCES", {**actual, "selective.py": "0" * 64})
    with pytest.raises(SystemExit, match="changes frozen files"):
        design.checked_code_under_test(export)
    monkeypatch.setattr(design, "FROZEN_SOURCES", actual)
    monkeypatch.setattr(design, "CODE_COMMIT", "c" * 40)
    monkeypatch.setattr(design, "CODE_PACKAGE_SOURCES_SHA256", "0" * 64)
    with pytest.raises(SystemExit, match="not the pinned"):
        design.checked_code_under_test(export)
    monkeypatch.setattr(design, "CODE_PACKAGE_SOURCES_SHA256", design.package_sources_sha256(package_dir))

    found = design.checked_code_under_test(export)

    assert (found["commit"], found["versions"], found["package_dir"]) == ("c" * 40, dict(design.CODE_VERSIONS),
                                                                          str(package_dir))


def test_only_the_prompt_validator_and_model_seed_differ_from_the_frozen_settings() -> None:
    for arm in design.ARMS.values():
        settings = diagnose_arm.frozen_settings_for(arm, FROZEN)
        assert settings["model"] == {**FROZEN["model"], "prompt_version": "diag-p5",
                                     "options": {**FROZEN["model"]["options"], "seed": arm.model_seed}}
        assert settings["versions"] == {**FROZEN["versions"], "validator": "val-6"}
        assert {key: value for key, value in settings.items() if key not in ("model", "versions")} == {
            key: value for key, value in FROZEN.items() if key not in ("model", "versions")}
    assert (FROZEN["model"]["deadline_s"], FROZEN["model"]["max_repairs"], FROZEN["model"]["think"]) == (
        design.DEADLINE_S, design.MAX_REPAIRS, design.THINK)


class FakeClient:
    """Stands in for OllamaClient: no network; reports a fixed digest and records unloads."""

    def __init__(self, *, model: str, options: Mapping[str, object], keep_alive: str, digest: str) -> None:
        self.model, self.options, self.keep_alive, self.digest = model, dict(options), keep_alive, digest
        self.unloaded, self.closed = 0, False

    def model_digest(self) -> str:
        return self.digest

    def version(self) -> str:
        return "0.0.0-hand-made"

    def unload(self) -> None:
        self.unloaded += 1

    def close(self) -> None:
        self.closed = True


def test_the_arm_client_uses_the_arms_seed_and_refuses_drift_and_another_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    arm = design.ARMS[SELECTIVE_ARMS[1]]
    made: list[FakeClient] = []

    def factory(digest: str) -> Callable[..., FakeClient]:
        def make(**kwargs: object) -> FakeClient:
            made.append(FakeClient(**kwargs, digest=digest))  # type: ignore[arg-type]
            return made[-1]
        return make

    monkeypatch.setattr(diagnosis, "PROMPT_VERSION", "diag-p5")
    monkeypatch.setattr(diagnosis, "VALIDATOR_VERSION", "val-6")
    monkeypatch.setattr(diagnosis, "OllamaClient", factory(FROZEN["model"]["model_digest"]))
    checked = diagnose_arm.arm_client(arm, FROZEN)
    assert checked.options == {**FROZEN["model"]["options"], "seed": 20261112}
    drifted = json.loads(json.dumps(FROZEN))
    drifted["model"]["options"]["temperature"] = 0.7
    with pytest.raises(SystemExit, match="no longer matches"):
        diagnose_arm.arm_client(arm, drifted)
    off_seed = design.Arm(design.SELECTIVE_SYSTEM, 20261113)
    with pytest.raises(SystemExit, match="no longer matches"):
        diagnose_arm.arm_client(off_seed, FROZEN)
    monkeypatch.setattr(diagnosis, "OllamaClient", factory("0" * 64))
    with pytest.raises(SystemExit, match="never pull"):
        diagnose_arm.arm_client(arm, FROZEN)
    assert made[-1].closed
    assert len(made) == 2  # no client is made while the settings drift


# --------------------------------------------------------------------------- hand-made answers


def output_naming(case: Mapping, change: str) -> dict:
    expected = case["expected"]
    return {"hypothesis": expected["hypothesis"], "faulting_change_id": change, "affected_method": expected["affected_method"],
            "evidence_level": "supported", "supporting_evidence_ids": [], "contradicting_evidence_ids": [],
            "missing_checks": [], "next_check": "none", "proposed_operation": expected["operation"], "rationale": "hand-made"}


def change_of(case: Mapping, which: str) -> str:
    """The deployment a hand-made answer names: the fault, the innocent, or an abstention (``unknown``/``none``)."""
    if which == "fault":
        return case["expected"]["faulting_change_id"]
    return case["harmless_deploy_ids"][0] if which == "innocent" else which


def route_record(target: str) -> dict:
    reason, unmet = ("several_plausible_candidates", ()) if target == MODEL else ("no_live_merchant_failure",
                                                                                  ("no_live_merchant_failure",))
    return selective.Route(target=target, reason=reason, unmet=unmet, measurements={}).model_dump(mode="json")


def rules_v3_record(case: Mapping, change: str) -> dict:
    return selective.RulesV3Answer(output=DiagnosisOutput.model_validate(output_naming(case, change)),
                                   rules_v2_change=change, attributed_change=change, attribution={},
                                   resolution="as_attributed").model_dump(mode="json")


def computed_for(cases: Iterable[Mapping], *, model: Iterable[str] = (), rules_v3: Mapping[str, str] | None = None,
                 rules_v2: Mapping[str, str] | None = None, refused: Iterable[str] = ()) -> dict:
    """Stand-in routes and rules answers: every world RULES-routed and both rules systems right, unless told otherwise
    (``rules_v3``/``rules_v2`` map a world to "innocent", "unknown" or "none")."""
    routed, refused_by_val_6 = set(model), set(refused)
    computed = {}
    for case in cases:
        case_id = case["case_id"]
        v2 = change_of(case, (rules_v2 or {}).get(case_id, "fault"))
        computed[case_id] = summary.Deterministic(
            route=route_record(MODEL if case_id in routed else RULES),
            rules_v3=rules_v3_record(case, change_of(case, (rules_v3 or {}).get(case_id, "fault"))),
            rules_v2={"output": output_naming(case, v2), "validated": case_id not in refused_by_val_6,
                      "reasons": ["hand-made val-6 refusal"] if case_id in refused_by_val_6 else []})
    return computed


SYSTEM_MESSAGE = {"role": "system", "content": "hand-made system prompt"}


def messages_for(packet_hash: str) -> list[dict]:
    return [SYSTEM_MESSAGE, {"role": "user", "content": f"packet {packet_hash}"}]


def run_record_for(packet_hash: str, seed: int) -> dict:
    return {"model": FROZEN["model"]["model"], "model_digest": FROZEN["model"]["model_digest"],
            "options": {**FROZEN["model"]["options"], "seed": seed}, "seed": seed, "think": False,
            "first_request_sha256": _sha({"messages": messages_for(packet_hash)}), "response_schema_sha256": "s" * 64,
            "first_call_sent_as_built": True}


def environment_for(seed: int) -> dict:
    return {"model": {"model": FROZEN["model"]["model"], "model_digest": FROZEN["model"]["model_digest"],
                      "options": {**FROZEN["model"]["options"], "seed": seed}, "think": False},
            "hardware": {}, "os": {}, "python": "3.12", "packages": {}, "versions": {}}


def _call(index: int, content: str | None, reasons: Iterable[str] = (), error: str | None = None) -> dict:
    return {"call_index": index, "wall_ms": 900, "error": error, "rejection_reasons": list(reasons),
            "reply": None if content is None else {"content": content}}


def validated_answer(case: Mapping, change: str, seed: int) -> dict:
    output = output_naming(case, change)
    return {"case_id": case["case_id"], "packet_hash": case["packet_hash"], "started_wall_utc": "2026-11-13T00:00:00+00:00",
            "cold": False, "status": "validated", "latency_ms": 1000, "reasons": [], "output": output,
            "unsafe_proposals": [], "repairs": 0, "calls": [_call(0, json.dumps(output, sort_keys=True))],
            "run_record": run_record_for(case["packet_hash"], seed)}


def naming(which: str) -> Edit:
    """The run's final answer names another deployment (or abstains), in the same validated first reply."""
    def edit(answer: dict, case: dict) -> dict:
        output = output_naming(case, change_of(case, which))
        return answer | {"output": output, "calls": [_call(0, json.dumps(output, sort_keys=True))]}
    return edit


def refused_twice(answer: dict, _case: dict) -> dict:
    reason = "hand-made val-6 reason"
    reply = answer["calls"][0]["reply"]["content"]
    return answer | {"status": "needs_review", "output": None, "repairs": 1,
                     "reasons": [f"call 1: {reason}", f"call 2: {reason}"],
                     "calls": [_call(0, reply, [reason]), _call(1, reply, [reason])]}


def timed_out(answer: dict, _case: dict) -> dict:
    error = "no reply within the remaining 3.0 s of the overall deadline"
    return answer | {"status": "needs_review", "output": None, "reasons": [f"call 1: {error}"],
                     "calls": [_call(0, None, error=error)]}


def hand_made_runs(cases_document: Mapping, computed: Mapping, *, edits: Mapping[tuple[str, str], Edit] | None = None,
                   documents: Mapping[str, Callable[[dict], dict]] | None = None) -> dict:
    """The four arms as the arm stage writes them: each diagnoses its worlds under the stand-in routes, every answer
    validated and naming the fault, unless an edit says otherwise."""
    cases = cases_document["cases"]
    by_id = {case["case_id"]: case for case in cases}
    targets = {case_id: computed[case_id].target for case_id in computed}
    runs = {}
    for name, arm in design.ARMS.items():
        answers = []
        for case_id in design.arm_case_ids(arm, cases, targets):
            case = by_id[case_id]
            answer = validated_answer(case, case["expected"]["faulting_change_id"], arm.model_seed)
            if arm.decides:
                answer |= {"route": computed[case_id].route, "rules_v3": computed[case_id].rules_v3}
            answers.append((edits or {}).get((name, case_id), lambda same, _case: same)(answer, case))
        document = {"arm": name, "system": arm.system, "model_seed": arm.model_seed, "code_under_test": CODE,
                    "preregistration": cases_document["preregistration"], "addenda": [],
                    "frozen_settings_sha256": FROZEN_SHA256, "cases_sha256": "hand-made",
                    "routes": {case_id: computed[case_id].route for case_id in computed},
                    "arm_case_ids": [answer["case_id"] for answer in answers],
                    "started_wall_utc": "2026-11-13T00:00:00+00:00", "finished_wall_utc": "2026-11-13T00:10:00+00:00",
                    "environment": environment_for(arm.model_seed), "answers": answers}
        document = (documents or {}).get(name, lambda same: same)(document)
        trace = {answer["case_id"]: {"case_id": answer["case_id"], "calls": [{"messages": messages_for(answer["packet_hash"])}]}
                 for answer in document["answers"]}
        runs[name] = summary.ArmRun(name, document, {answer["case_id"]: answer for answer in document["answers"]}, trace)
    return runs


def at_both_seeds(system: str, case_ids: Iterable[str], edit: Edit) -> dict:
    return {(design.arm_for(system, seed).name, case_id): edit for seed in design.MODEL_SEEDS for case_id in case_ids}


def _report(cases_document: Mapping, computed: Mapping, **changes: Mapping) -> dict:
    return summary.report(cases_document, hand_made_runs(cases_document, computed, **changes), computed, code=CODE,
                          preregistration=cases_document["preregistration"], addenda=[], frozen=FROZEN,
                          frozen_sha256=FROZEN_SHA256)


def _ids(cases: Iterable[Mapping]) -> list[str]:
    return [case["case_id"] for case in cases]


def _decision(report: Mapping) -> dict:
    return report["decisions"]["selective_against_rules"]


# --------------------------------------------------------------------------- decision 1 on hand-made answer sets


def entries(heldout: list[dict], first: int, last: int) -> list[str]:
    """The world IDs of held-out entries ``first`` to ``last`` (1-based, manifest order), two worlds each."""
    return _ids(heldout[2 * (first - 1): 2 * last])


def test_hand_made_answers_are_scored_as_intended(heldout: list[dict]) -> None:
    case = heldout[0]
    right, innocent = output_naming(case, change_of(case, "fault")), output_naming(case, change_of(case, "innocent"))

    assert [summary.attribution_class(output, case["expected"]["faulting_change_id"]) for output in (
        right, innocent, output_naming(case, "unknown"), output_naming(case, "none"), None)] == [
        "correct", "wrong", "abstained", "abstained", "review"]
    assert summary.answer_view(case, right, "model")["outcome_correct"] is True
    assert summary.answer_view(case, innocent, "model")["outcome_correct"] is False


def test_s_is_r_on_every_rules_routed_world_and_finds_no_detectable_advantage(cases_document: dict, cases: list[dict]) -> None:
    report = _report(cases_document, computed_for(cases))
    decision = _decision(report)

    assert (decision["verdict"], decision["clauses"], decision["valid"]) == (NO_ADVANTAGE, [], True)
    assert (decision["unit"], decision["units"], decision["improved"], decision["worsened"], decision["tied"]) == (
        "entry", 24, 0, 0, 24)
    assert decision["improved_p"] == decision["worsened_p"] == {"exact": "1", "value": 1.0}
    assert decision["correct_attributions"] == {"R": 96, "S": 96, "of": 96}
    assert decision["wrong_attributions"] == {"R": 0, "S": 0, "of": 116, "heldout": {"R": 0, "S": 0},
                                              "controls": {"R": 0, "S": 0}}
    assert (decision["per_world_descriptive"]["unit"], decision["per_world_descriptive"]["tied"]) == ("world", 48)
    assert decision["unsafe_proposals"].startswith("met by construction for final answers")
    assert report["descriptive"]["routing"]["heldout"] == {"model": 0, "of": 48, "exact": "0/48"}
    for row in report["per_world"]:
        assert [row["S"][str(seed)]["source"] for seed in design.MODEL_SEEDS] == ["rules_v3", "rules_v3"]
        assert all(row["S"][str(seed)]["change"] == row["R"]["change"] for seed in design.MODEL_SEEDS)
    assert all(arm["passed"] for arm in report["validity"]["arms"].values())
    assert report["model_all"]["reported"]
    assert report["adoption"]["product"].startswith("the product stays rules-first")
    assert [report["arms"][name]["diagnoses"] for name in design.ARMS] == [0, 0, 48, 48]


def test_model_routed_worlds_where_the_model_agrees_with_rules_v3_stay_tied(cases_document: dict, cases: list[dict],
                                                                           heldout: list[dict]) -> None:
    report = _report(cases_document, computed_for(cases, model=entries(heldout, 1, 5)))
    decision = _decision(report)

    assert (decision["verdict"], decision["tied"]) == (NO_ADVANTAGE, 24)
    assert (decision["effect"]["model_routed_worlds"], decision["effect"]["entries_with_a_model_routed_world"]) == (10, 5)
    assert [report["arms"][name]["diagnoses"] for name in design.ARMS] == [10, 10, 48, 48]
    assert report["descriptive"]["routing"]["heldout"]["exact"] == "10/48"
    assert report["descriptive"]["model_routed"]["S"]["correct"] == 20


def test_five_improved_entries_and_no_more_wrong_attributions_help(cases_document: dict, cases: list[dict],
                                                                   heldout: list[dict]) -> None:
    missed = entries(heldout, 1, 5)  # rules-v3 abstains on both worlds; the model names the fault at both seeds
    report = _report(cases_document, computed_for(cases, model=missed, rules_v3=dict.fromkeys(missed, "unknown")))
    decision = _decision(report)

    assert (decision["verdict"], decision["improved"], decision["worsened"]) == ("helps", 5, 0)
    assert decision["improved_p"] == {"exact": "1/32", "value": 1 / 32}
    assert decision["per_entry"][0] == {"entry": "heldout_01", "R": 0, "S": 4, "change": "improved"}
    assert (decision["wrong_attributions"]["R"], decision["wrong_attributions"]["S"]) == (0, 0)
    assert decision["effect"]["correct_difference"] == 20
    assert report["adoption"]["product"].startswith("a selective product mode is wired")
    # rules-v3 abstains where rules-v2 is right, so decision 2 keeps rules-v2 whatever decision 1 says.
    assert report["decisions"]["rules_v3_against_rules_v2"]["verdict"] == "keep rules-v2"


def test_the_entry_is_the_unit_so_three_fixed_entries_are_not_significant(cases_document: dict, cases: list[dict],
                                                                         heldout: list[dict]) -> None:
    missed = entries(heldout, 1, 3)  # six worlds: by world they would be significant
    decision = _decision(_report(cases_document, computed_for(cases, model=missed,
                                                              rules_v3=dict.fromkeys(missed, "unknown"))))

    assert (decision["verdict"], decision["improved"], decision["improved_p"]["exact"]) == (NO_ADVANTAGE, 3, "1/8")
    per_world = decision["per_world_descriptive"]
    assert (per_world["unit"], per_world["improved"], per_world["improved_p"]["exact"]) == ("world", 6, "1/64")


def test_more_wrong_attributions_hurt_even_where_the_improvement_is_significant(cases_document: dict, cases: list[dict],
                                                                                heldout: list[dict]) -> None:
    missed, slipped = entries(heldout, 1, 7), heldout[14]["case_id"]  # 7 entries improved; entry 8 slips at one seed
    computed = computed_for(cases, model=[*missed, slipped], rules_v3=dict.fromkeys(missed, "unknown"))
    report = _report(cases_document, computed, edits={(SELECTIVE_ARMS[0], slipped): naming("innocent")})
    decision = _decision(report)

    assert (decision["improved"], decision["worsened"]) == (7, 1)
    assert decision["improved_p"]["exact"] == "9/256"  # significant on its own
    assert (decision["wrong_attributions"]["R"], decision["wrong_attributions"]["S"]) == (0, 1)
    assert (decision["verdict"], decision["clauses"]) == ("hurts", [MORE_WRONG])


def test_a_control_whose_model_answer_blames_an_innocent_deployment_counts_against_s(
    cases_document: dict, cases: list[dict], controls: list[dict],
) -> None:
    routed, rules_wrong = controls[8]["case_id"], controls[9]["case_id"]  # the two D6 controls: no deployment is at fault
    computed = computed_for(cases, model=[routed], rules_v3={rules_wrong: "innocent"})
    report = _report(cases_document, computed, edits={(SELECTIVE_ARMS[1], routed): naming("innocent")})
    decision = _decision(report)

    assert (routed.startswith("control_d6"), rules_wrong.startswith("control_d6")) == (True, True)
    assert (decision["valid"], decision["tied"]) == (True, 24)  # every held-out entry ties
    # rules-v3's wrong answer on the RULES-routed control counts for both systems; the model's slip only for S.
    assert decision["wrong_attributions"]["controls"] == {"R": 2, "S": 3}
    assert (decision["verdict"], decision["clauses"]) == ("hurts", [MORE_WRONG])


def test_a_significant_worsening_hurts_and_review_is_not_a_wrong_attribution(cases_document: dict, cases: list[dict],
                                                                             heldout: list[dict]) -> None:
    routed = entries(heldout, 1, 5)
    report = _report(cases_document, computed_for(cases, model=routed),
                     edits=at_both_seeds(design.SELECTIVE_SYSTEM, routed, refused_twice))
    decision = _decision(report)

    assert (decision["verdict"], decision["clauses"]) == ("hurts", [WORSENED_P])
    assert (decision["improved"], decision["worsened"], decision["worsened_p"]["exact"]) == (0, 5, "1/32")
    assert (decision["wrong_attributions"]["R"], decision["wrong_attributions"]["S"]) == (0, 0)
    assert decision["heldout_totals"]["S"]["review"] == 20


def test_four_improved_entries_are_no_detectable_advantage_and_abstentions_are_not_wrong(
    cases_document: dict, cases: list[dict], heldout: list[dict],
) -> None:
    missed = entries(heldout, 1, 4)
    report = _report(cases_document, computed_for(cases, model=missed, rules_v3=dict.fromkeys(missed, "unknown")))
    decision = _decision(report)

    assert (decision["verdict"], decision["improved"], decision["improved_p"]["exact"]) == (NO_ADVANTAGE, 4, "1/16")
    assert decision["heldout_totals"]["R"]["abstained"] == 16
    assert (decision["wrong_attributions"]["R"], decision["wrong_attributions"]["S"]) == (0, 0)


def test_the_model_naming_the_innocent_where_rules_v3_does_too_changes_nothing(cases_document: dict, cases: list[dict],
                                                                              heldout: list[dict]) -> None:
    both_wrong = entries(heldout, 1, 3)
    report = _report(cases_document, computed_for(cases, model=both_wrong, rules_v3=dict.fromkeys(both_wrong, "innocent")),
                     edits=at_both_seeds(design.SELECTIVE_SYSTEM, both_wrong, naming("innocent")))
    decision = _decision(report)

    assert (decision["verdict"], decision["tied"]) == (NO_ADVANTAGE, 24)
    assert (decision["wrong_attributions"]["R"], decision["wrong_attributions"]["S"]) == (12, 12)


@pytest.mark.parametrize(("arguments", "expected"), [
    ({"valid": False, "selective_wrong": 0, "rules_wrong": 9, "improved_p": Fraction(1, 64), "worsened_p": Fraction(1)},
     ("invalid", [])),
    ({"valid": True, "selective_wrong": 3, "rules_wrong": 2, "improved_p": Fraction(1, 64), "worsened_p": Fraction(1)},
     ("hurts", [MORE_WRONG])),
    ({"valid": True, "selective_wrong": 0, "rules_wrong": 0, "improved_p": Fraction(1), "worsened_p": Fraction(1, 20)},
     ("hurts", [WORSENED_P])),
    ({"valid": True, "selective_wrong": 4, "rules_wrong": 0, "improved_p": Fraction(1), "worsened_p": Fraction(1, 32)},
     ("hurts", [MORE_WRONG, WORSENED_P])),
    ({"valid": True, "selective_wrong": 2, "rules_wrong": 2, "improved_p": Fraction(1, 20), "worsened_p": Fraction(1)},
     ("helps", ["improved-direction p <= 0.05", "S has no more wrong attributions than R"])),
    ({"valid": True, "selective_wrong": 0, "rules_wrong": 0, "improved_p": Fraction(1, 16), "worsened_p": Fraction(1)},
     (NO_ADVANTAGE, [])),
    ({"valid": True, "selective_wrong": 0, "rules_wrong": 0, "improved_p": Fraction(1), "worsened_p": Fraction(1)},
     (NO_ADVANTAGE, [])),
])
def test_decision_1_verdicts_are_reached_in_the_preregistered_order(arguments: dict, expected: tuple) -> None:
    assert summary.decide_primary(**arguments) == expected


def test_the_agreement_variant_keeps_only_answers_that_name_rules_v3s_deployment_and_decides_nothing(
    cases_document: dict, cases: list[dict], heldout: list[dict],
) -> None:
    agree, differ, abstain, review = (entries(heldout, entry, entry) for entry in (1, 2, 3, 4))
    computed = computed_for(cases, model=[*agree, *differ, *abstain, *review], rules_v3=dict.fromkeys(abstain, "unknown"))
    edits = {**at_both_seeds(design.SELECTIVE_SYSTEM, differ, naming("innocent")),
             **at_both_seeds(design.SELECTIVE_SYSTEM, review, refused_twice)}
    report = _report(cases_document, computed, edits=edits)
    figures = report["descriptive"]["agreement_variant"]

    # entry 1 is accepted; entries 2 (another deployment), 3 (rules-v3 abstains) and 4 (model review) go to review.
    assert figures["heldout"] == {"correct": 84, "wrong": 0, "abstained": 0, "review": 12, "answers": 96,
                                  "outcome_correct": 84}
    assert figures["model_route_outcomes"] == {"accepted": 4, "changes_differ": 8, "model_needs_review": 4}
    assert figures["wrong_attributions_all_worlds"] == 0
    assert (figures["against_rules_by_entry"]["improved"], figures["against_rules_by_entry"]["worsened"]) == (0, 2)
    decision = _decision(report)  # S itself gains entry 3 and loses entries 2 and 4
    assert (decision["improved"], decision["worsened"], decision["verdict"], decision["clauses"]) == (
        1, 2, "hurts", [MORE_WRONG])
    assert "agree" not in json.dumps(report["adoption"])


# --------------------------------------------------------------------------- validity on hand-made answer sets


def test_more_than_one_control_routed_to_model_makes_decision_1_invalid(cases_document: dict, cases: list[dict],
                                                                        controls: list[dict]) -> None:
    one = _report(cases_document, computed_for(cases, model=_ids(controls[2:3])))
    two = _report(cases_document, computed_for(cases, model=_ids(controls[2:4])))

    assert one["validity"]["controls_routed_to_model"]["model_routed"] == [controls[2]["case_id"]]
    assert (_decision(one)["valid"], _decision(one)["verdict"]) == (True, NO_ADVANTAGE)
    routed_control = next(row for row in one["controls"] if row["route"] == MODEL)
    assert [routed_control["S"][str(seed)]["source"] for seed in design.MODEL_SEEDS] == ["model", "model"]
    assert [one["arms"][name]["diagnoses"] for name in design.ARMS] == [1, 1, 48, 48]
    assert (_decision(two)["valid"], _decision(two)["verdict"]) == (False, "invalid")
    assert list(_decision(two)["invalid_because"]) == ["controls_routed_to_model"]
    assert two["decisions"]["rules_v3_against_rules_v2"]["valid"]  # decision 2 reads no route


def test_three_operational_failures_in_an_s_arm_invalidate_decision_1_and_two_count_as_review(
    cases_document: dict, cases: list[dict], heldout: list[dict],
) -> None:
    routed = entries(heldout, 1, 3)
    computed = computed_for(cases, model=routed)
    two = _report(cases_document, computed, edits={(SELECTIVE_ARMS[1], case_id): timed_out for case_id in routed[:2]})
    three = _report(cases_document, computed, edits={(SELECTIVE_ARMS[1], case_id): timed_out for case_id in routed[:3]})

    assert (_decision(two)["verdict"], _decision(two)["worsened"], _decision(two)["heldout_totals"]["S"]["review"]) == (
        NO_ADVANTAGE, 1, 2)
    assert two["validity"]["arms"][SELECTIVE_ARMS[1]]["operational_failures"]["failures"] == dict.fromkeys(
        routed[:2], "timeout")
    assert _decision(three)["verdict"] == "invalid"
    assert list(_decision(three)["invalid_because"]) == [f"arms.{SELECTIVE_ARMS[1]}"]


def _drift_seed(answer: dict, _case: dict) -> dict:
    return answer | {"run_record": {**answer["run_record"], "options": {**answer["run_record"]["options"], "seed": 1}}}


def _not_sent_as_built(answer: dict, _case: dict) -> dict:
    return answer | {"run_record": {**answer["run_record"], "first_call_sent_as_built": False,
                                    "first_request_sha256": None}}


@pytest.mark.parametrize("edit", [_drift_seed, _not_sent_as_built])
def test_a_drifted_run_record_invalidates_its_own_systems_figures_only(cases_document: dict, cases: list[dict],
                                                                      heldout: list[dict], edit: Edit) -> None:
    routed = _ids(heldout[:3])
    computed = computed_for(cases, model=routed)
    in_s = _report(cases_document, computed, edits={(SELECTIVE_ARMS[0], routed[1]): edit})
    in_m_all = _report(cases_document, computed, edits={(MODEL_ALL_ARMS[1], routed[1]): edit})

    assert _decision(in_s)["verdict"] == "invalid"
    assert not in_s["validity"]["arms"][SELECTIVE_ARMS[0]]["run_records"]["passed"]
    assert (_decision(in_m_all)["verdict"], in_m_all["model_all"]["reported"]) == (NO_ADVANTAGE, False)
    assert list(in_m_all["model_all"]["invalid_because"]) == [f"arms.{MODEL_ALL_ARMS[1]}.run_records"]
    assert "M-all" not in in_m_all["per_world"][0]
    assert "model_all_against_rules" not in in_m_all["descriptive"]


def _another_route(case_id: str) -> Callable[[dict], dict]:
    def edit(document: dict) -> dict:
        return document | {"routes": {**document["routes"], case_id: {**document["routes"][case_id], "measurements": {"x": 1}}}}
    return edit


def _skipped(case_id: str) -> Callable[[dict], dict]:
    def edit(document: dict) -> dict:
        return document | {"arm_case_ids": [item for item in document["arm_case_ids"] if item != case_id],
                           "answers": [answer for answer in document["answers"] if answer["case_id"] != case_id]}
    return edit


def _other_code(document: dict) -> dict:
    return document | {"code_under_test": {**document["code_under_test"], "package_sources_sha256": "q" * 64}}


def test_another_route_a_skipped_world_or_other_code_in_an_s_arm_invalidates_decision_1(
    cases_document: dict, cases: list[dict], heldout: list[dict],
) -> None:
    routed = _ids(heldout[:3])
    computed = computed_for(cases, model=routed)
    for document_edit, failing in ((_another_route(heldout[20]["case_id"]), "routes"), (_skipped(routed[2]), "routes"),
                                   (_other_code, "code")):
        report = _report(cases_document, computed, documents={SELECTIVE_ARMS[1]: document_edit})
        assert _decision(report)["verdict"] == "invalid"
        assert not report["validity"]["arms"][SELECTIVE_ARMS[1]][failing]["passed"]
    skipped = _report(cases_document, computed, documents={SELECTIVE_ARMS[1]: _skipped(routed[2])})
    row = next(row for row in skipped["per_world"] if row["case_id"] == routed[2])
    assert (row["S"][str(SECOND_SEED)]["source"], row["S"][str(SECOND_SEED)]["class"]) == ("missing", "review")


def test_m_all_operational_failures_and_routes_are_descriptive_but_its_code_gates_its_figures(
    cases_document: dict, cases: list[dict], heldout: list[dict],
) -> None:
    computed = computed_for(cases)
    failing = _report(cases_document, computed, edits={(MODEL_ALL_ARMS[0], case_id): timed_out for case_id in _ids(heldout[:3])})
    rerouted = _report(cases_document, computed, documents={MODEL_ALL_ARMS[1]: _another_route(heldout[5]["case_id"])})
    other_code = _report(cases_document, computed, documents={MODEL_ALL_ARMS[0]: _other_code})

    assert (_decision(failing)["valid"], failing["model_all"]["reported"]) == (True, True)
    assert not failing["validity"]["arms"][MODEL_ALL_ARMS[0]]["operational_failures"]["passed"]
    assert failing["arms"][MODEL_ALL_ARMS[0]]["operational_failures"]["count"] == 3
    assert "model_all_against_rules" in failing["descriptive"]
    assert (rerouted["model_all"]["reported"], rerouted["validity"]["arms"][MODEL_ALL_ARMS[1]]["routes"]["passed"]) == (
        True, False)
    assert (_decision(other_code)["valid"], other_code["model_all"]["reported"]) == (True, False)
    assert list(other_code["model_all"]["invalid_because"]) == [f"arms.{MODEL_ALL_ARMS[0]}.code"]


# --------------------------------------------------------------------------- decision 2, adoption and the descriptive figures


@pytest.mark.parametrize(("rules_v3", "rules_v2", "refused", "verdict", "counts"), [
    ({}, {}, (), "adopt rules_v3 as the rules-first default", ((48, 0), (48, 0))),
    ({}, {"w0": "innocent", "w1": "innocent"}, (), "adopt rules_v3 as the rules-first default", ((48, 0), (46, 2))),
    ({"w0": "unknown"}, {}, (), "keep rules-v2", ((47, 0), (48, 0))),
    ({"w0": "innocent"}, {"w0": "unknown"}, (), "keep rules-v2", ((47, 1), (47, 0))),
    ({"w0": "innocent"}, {"w0": "innocent", "w1": "unknown"}, (), "adopt rules_v3 as the rules-first default",
     ((47, 1), (46, 1))),
    ({}, {}, ("w0",), "adopt rules_v3 as the rules-first default", ((48, 0), (47, 0))),
])
def test_decision_2_adopts_rules_v3_with_as_many_right_and_no_more_wrong(
    cases_document: dict, cases: list[dict], heldout: list[dict], rules_v3: dict, rules_v2: dict, refused: tuple,
    verdict: str, counts: tuple,
) -> None:
    worlds = {f"w{index}": case["case_id"] for index, case in enumerate(heldout[:2])}
    computed = computed_for(cases, rules_v3={worlds[key]: value for key, value in rules_v3.items()},
                            rules_v2={worlds[key]: value for key, value in rules_v2.items()},
                            refused=[worlds[key] for key in refused])

    decision = _report(cases_document, computed)["decisions"]["rules_v3_against_rules_v2"]

    assert decision["verdict"] == verdict
    decision_counts = ((decision["rules_v3"]["correct"], decision["rules_v3"]["wrong"]),
                       (decision["rules_v2"]["correct"], decision["rules_v2"]["wrong"]))
    assert decision_counts == counts
    assert decision["rules_v2_refused_by_val_6"] == [worlds[key] for key in refused]


@pytest.mark.parametrize(("primary", "secondary", "product", "rules_default"), [
    ("helps", "adopt rules_v3 as the rules-first default", "a selective product mode is wired", "rules_v3 replaces"),
    ("hurts", "keep rules-v2", "the product stays rules-first", "rules-v2 stays"),
    (NO_ADVANTAGE, "adopt rules_v3 as the rules-first default", "the product stays rules-first", "rules_v3 replaces"),
    ("invalid", "invalid", "the product stays rules-first", "rules-v2 stays"),
])
def test_the_adoption_rules_follow_the_two_verdicts(primary: str, secondary: str, product: str, rules_default: str) -> None:
    adopted = summary.adoption(primary, secondary)

    assert adopted["product"].startswith(product)
    assert adopted["rules_default"].startswith(rules_default)
    assert (adopted["decision_1"], adopted["decision_2"]) == (primary, secondary)


def test_the_descriptive_figures_compare_m_all_by_entry_and_by_world(cases_document: dict, cases: list[dict],
                                                                     heldout: list[dict]) -> None:
    missed = entries(heldout, 1, 3)
    computed = computed_for(cases, model=missed, rules_v3=dict.fromkeys(missed, "unknown"))
    report = _report(cases_document, computed, edits=at_both_seeds(design.MODEL_ALL_SYSTEM, entries(heldout, 21, 21),
                                                                   naming("innocent")))
    against_rules = report["descriptive"]["model_all_against_rules"]

    assert (against_rules["by_entry"]["improved"], against_rules["by_entry"]["worsened"]) == (3, 1)
    assert (against_rules["by_world"]["improved"], against_rules["by_world"]["worsened"]) == (6, 2)
    assert report["descriptive"]["systems"]["M-all"]["wrong"] == 4
    assert report["descriptive"]["routing"]["heldout_entries_with_a_model_route"] == 3
    noise = report["descriptive"]["noise"]["selective_against_model_all_same_seed"][str(FIRST_SEED)]
    assert noise == {"worlds": 6, "both_answered": 6, "identical_first_request": 6, "identical_first_reply": 6,
                     "identical_final_change": 6}
    assert summary.headline(report)["decision_1"]["verdict"] == NO_ADVANTAGE


# --------------------------------------------------------------------------- the arm stage, end to end with stand-ins


def _final_experiment_dir(directory: Path, cases_document: dict) -> dict:
    shutil.copytree(SELECTIVE_DIR / "packets", directory / "packets")
    return _registered(directory, "# Selective AI, pre-registration\n\nFinal, hand-made.\n", cases_document)


def test_the_arm_stage_calls_the_model_on_exactly_its_worlds_and_the_summariser_reads_what_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases_document: dict, cases: list[dict],
) -> None:
    document = _final_experiment_dir(tmp_path, cases_document)
    routed = ["heldout_03_13105", "heldout_10_13120", "heldout_24_13147", "control_d3_test_2_13152"]
    computed = computed_for(cases, model=routed)
    by_hash = {case["packet_hash"]: case for case in cases}
    calls: list[tuple[str, str]] = []

    def model_run(built: EvidencePacket) -> diagnosis.DiagnosisRun:
        case = by_hash[built.packet_hash()]
        output = DiagnosisOutput.model_validate(output_naming(case, case["expected"]["faulting_change_id"]))
        call = diagnosis.ModelCallRecord(
            call_index=0, wall_ms=900, messages=tuple(diagnosis.ChatMessage(**message) for message in messages_for(built.packet_hash())),
            reply=diagnosis.ChatReply(content=json.dumps(output.model_dump(mode="json"))), error=None, rejection_reasons=())
        return diagnosis.DiagnosisRun(status=DiagnosisStatus.VALIDATED, output=output, reasons=(), calls=(call,), repairs=0,
                                      elapsed_ms=1000, think=False, deadline_s=180.0, prompt_version="hand-made",
                                      packet_hash=built.packet_hash())

    def fake_selective(built: EvidencePacket, client: object, *, think: bool, deadline_s: float) -> selective.SelectiveDiagnosis:
        assert (think, deadline_s) == (False, 180.0)
        case_id = by_hash[built.packet_hash()]["case_id"]
        calls.append(("selective", case_id))
        run = model_run(built)
        return selective.SelectiveDiagnosis(
            route=selective.Route.model_validate(computed[case_id].route), status=run.status, output=run.output,
            rules_v3=selective.RulesV3Answer.model_validate(computed[case_id].rules_v3), model_run=run,
            packet_hash=built.packet_hash())

    def fake_diagnose(built: EvidencePacket, client: object, *, think: bool, deadline_s: float, max_repairs: int,
                      enforce_policies: bool) -> diagnosis.DiagnosisRun:
        assert (think, deadline_s, max_repairs, enforce_policies) == (False, 180.0, 1, True)
        calls.append(("model_all", by_hash[built.packet_hash()]["case_id"]))
        return model_run(built)

    clients: dict[str, FakeClient] = {}

    def fake_client(arm: object, frozen: object) -> FakeClient:
        seed = arm.model_seed  # type: ignore[attr-defined]
        clients[arm.name] = FakeClient(model=FROZEN["model"]["model"], options={**FROZEN["model"]["options"], "seed": seed},  # type: ignore[attr-defined]
                                       keep_alive="15m", digest=FROZEN["model"]["model_digest"])
        return clients[arm.name]  # type: ignore[attr-defined]

    for module, name in SYSTEMS_THAT_MUST_NOT_RUN:
        monkeypatch.setattr(module, name, _forbidden(f"{module.__name__}.{name}"))
    monkeypatch.setattr(selective, "diagnose_selective", fake_selective)
    monkeypatch.setattr(diagnosis, "diagnose", fake_diagnose)
    monkeypatch.setattr(diagnosis, "render_packet", lambda built: f"rendered {built.packet_hash()}")
    monkeypatch.setattr(diagnosis, "initial_messages", lambda built: tuple(
        diagnosis.ChatMessage(**message) for message in messages_for(built.packet_hash())))
    monkeypatch.setattr(diagnosis, "build_response_schema", lambda built: {"schema of": built.packet_hash()})
    monkeypatch.setattr(design, "checked_code_under_test", lambda export: CODE)
    monkeypatch.setattr(diagnose_arm, "routes_of", lambda packets: {case_id: computed[case_id].route for case_id in packets})
    monkeypatch.setattr(diagnose_arm, "arm_client", fake_client)
    monkeypatch.setattr(diagnose_arm, "describe_environment", lambda client, **_: environment_for(client.options["seed"]))

    for name in design.ARMS:
        assert diagnose_arm.main(["--arm", name, "--code-export", "/export", "--experiment-dir", str(tmp_path)]) == 0
    again = ["--arm", SELECTIVE_ARMS[0], "--code-export", "/export", "--experiment-dir", str(tmp_path)]
    with pytest.raises(SystemExit):
        diagnose_arm.main(again)  # answers are never overwritten

    heldout_ids = [case["case_id"] for case in cases if case["role"] == "heldout"]
    assert calls == [*(("selective", case_id) for _ in design.MODEL_SEEDS for case_id in routed),
                     *(("model_all", case_id) for _ in design.MODEL_SEEDS for case_id in heldout_ids)]
    assert all(client.unloaded == 1 and client.closed for client in clients.values())
    written = _read(tmp_path / f"answers_{SELECTIVE_ARMS[0]}.json")
    assert (written["arm_case_ids"], len(written["routes"])) == (routed, 58)
    assert [answer["cold"] for answer in written["answers"]] == [True, False, False, False]
    assert all(answer["run_record"]["first_call_sent_as_built"] for answer in written["answers"])
    assert all(answer["route"]["target"] == MODEL for answer in written["answers"])

    runs = summary.load_runs(tmp_path, document)
    report = summary.report(document, runs, computed, code=CODE, preregistration=document["preregistration"], addenda=[],
                            frozen=FROZEN, frozen_sha256=FROZEN_SHA256)
    assert {name: arm["passed"] for name, arm in report["validity"]["arms"].items()} == dict.fromkeys(design.ARMS, True)
    assert report["validity"]["controls_routed_to_model"]["model_routed"] == ["control_d3_test_2_13152"]
    assert (_decision(report)["verdict"], _decision(report)["effect"]["model_routed_worlds"]) == (NO_ADVANTAGE, 3)


def _write_arm_files(directory: Path, runs: Mapping, cases_sha256: str) -> None:
    """Hand-made arms written as the arm stage writes them: an answers file and a trace per arm."""
    (directory / "traces").mkdir(exist_ok=True)
    for name, run in runs.items():
        arm = design.ARMS[name]
        (directory / arm.answers_file).write_text(json.dumps({**run.document, "cases_sha256": cases_sha256}), encoding="utf-8")
        (directory / arm.trace_file).write_text("".join(json.dumps(line) + "\n" for line in run.trace.values()),
                                                encoding="utf-8")


def test_the_summariser_writes_its_report_from_json_files_and_refuses_a_run_that_opened_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases_document: dict, cases: list[dict], heldout: list[dict],
) -> None:
    document = _final_experiment_dir(tmp_path, cases_document)
    computed = computed_for(cases, model=entries(heldout, 1, 2))
    _write_arm_files(tmp_path, hand_made_runs(document, computed),
                     hashlib.sha256((tmp_path / "cases.json").read_bytes()).hexdigest())
    monkeypatch.setattr(design, "checked_code_under_test", lambda export: CODE)
    monkeypatch.setattr(summary, "compute_deterministic", lambda packets: computed)
    argv = ["--code-export", "/export", "--experiment-dir", str(tmp_path)]

    assert summary.main(argv) == 0
    written = _read(tmp_path / "report.json")
    assert (_decision(written)["verdict"], _decision(written)["valid"]) == (NO_ADVANTAGE, True)
    assert written["validity"]["no_database"] == {"passed": True, "databases_opened": 0}

    def opening_a_database(packets: object) -> dict:
        sqlite3.connect(":memory:").close()
        return computed

    (tmp_path / "report.json").unlink()
    monkeypatch.setattr(summary, "compute_deterministic", opening_a_database)
    with pytest.raises(SystemExit, match="opened databases"):
        summary.main(argv)
    assert not (tmp_path / "report.json").exists()


# --------------------------------------------------------------------------- the sign test


@pytest.mark.parametrize(("successes", "trials", "expected"), [
    (0, 0, Fraction(1)),
    (0, 5, Fraction(1)),
    (1, 1, Fraction(1, 2)),
    (5, 5, Fraction(1, 32)),
    (6, 7, Fraction(1, 16)),
    (7, 8, Fraction(9, 256)),
    (12, 16, Fraction(2517, 65536)),
    (31, 48, Fraction(sum(math.comb(48, k) for k in range(31, 49)), 2**48)),
])
def test_the_sign_test_is_the_exact_one_sided_binomial_tail(successes: int, trials: int, expected: Fraction) -> None:
    assert summary.sign_test_p(successes, trials) == expected


def test_the_sign_test_tails_are_complementary_and_the_preregistered_power_table_holds() -> None:
    for trials in range(1, 49):
        for successes in range(1, trials + 1):
            assert summary.sign_test_p(successes, trials) + summary.sign_test_p(trials - successes + 1, trials) == 1
    smallest = {trials: min(k for k in range(trials + 1) if summary.sign_test_p(k, trials) <= design.ALPHA)
                for trials in range(5, 49)}
    # The table PREREGISTRATION.md prints for the 24 entries, the most that can be untied.
    assert {trials: smallest[trials] for trials in range(5, 25)} == {
        5: 5, 6: 6, 7: 7, 8: 7, 9: 8, 10: 9, 11: 9, 12: 10, 13: 10, 14: 11, 15: 12, 16: 12, 17: 13, 18: 13, 19: 14,
        20: 15, 21: 15, 22: 16, 23: 16, 24: 17}
    assert all(summary.sign_test_p(trials, trials) > design.ALPHA for trials in range(1, 5))
    with pytest.raises(ValueError, match="successes"):
        summary.sign_test_p(6, 5)
