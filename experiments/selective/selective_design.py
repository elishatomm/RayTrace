"""Phase 11b, selective AI for deployment attribution: the one test's worlds, systems, arms and shared helpers.

The claim, fixed before any work (docs/EXTENSION_PLAN.md, phase 11b): routing only deployment-attribution cases to
the local model, behind the validator, identifies the faulting deployment more often than a stronger deterministic
baseline, without more wrong attributions. Four systems answer the test worlds:

* **R**, rules-v3 (``selective.rules_v3``) on every world: deterministic, one answer per world;
* **S**, the selective system (``selective.diagnose_selective``): ``route`` per world; a MODEL-routed world gets the
  live local diagnosis at each model seed, a RULES-routed world gets rules-v3;
* **R2**, rules-v2 (``rules.diagnose``), for the secondary decision: whether rules-v3 replaces it as the rules
  default;
* **M-all**, the model on every held-out world at both seeds: descriptive only, it decides nothing.

Stages: ``build_packets.py`` (the test container, because world generation opens databases), ``diagnose_arm.py``
(host, live model, JSON files only, one arm at a time) and ``summarize.py`` (host, JSON files only). Only the arm
stage calls the model, and only R, R2, the routes and the summary are computed after the pre-registration is
committed: before that, nothing runs a system on a test packet, because its answers are results.

PREREGISTRATION.md fixes every decision first; the build records its SHA-256 and every later stage checks it.
Round 3 of the planted-instruction study (``experiments/injection/round3``) supplies that machinery, the run records
and the operational-failure rule, imported from its folder unchanged; it in turn loads rounds 1 and 2.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType, ModuleType

from pydantic import JsonValue

import recovery_workbench
from recovery_workbench import attribution_rules, diagnosis, packet, rules, selective
from recovery_workbench.contracts import EvidencePacket, StrictModel, sha256_hex
from recovery_workbench.simulator import families

SELECTIVE_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = SELECTIVE_DIR.parents[1]
ROUND3_DIR = REPOSITORY_DIR / "experiments" / "injection" / "round3"


def _round3_design() -> ModuleType:
    """Round 3's ``round3_design``, under the bare name its own stage modules import it by."""
    if str(ROUND3_DIR) not in sys.path:
        sys.path.append(str(ROUND3_DIR))
    module = importlib.import_module("round3_design")
    if Path(str(module.__file__)).resolve() != ROUND3_DIR / "round3_design.py":
        raise ImportError(f"'round3_design' resolved to {module.__file__}, not to {ROUND3_DIR / 'round3_design.py'}")
    return module


round3_design = _round3_design()
round2_design = round3_design.round2_design
round1_design = round3_design.round1_design


def round3_module(name: str) -> ModuleType:
    """Round 3's stage module ``name`` (``diagnose_arm`` or ``summarize``), loaded once from its file.

    It gets a distinct name because this folder's stages share round 3's file names; the qualified name is the one
    round 3's own tests use, so both load the same module object.
    """
    qualified = f"injection_round3_{name}"
    loaded = sys.modules.get(qualified)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(qualified, ROUND3_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load round 3's {name}.py from {ROUND3_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module  # before executing it: dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- worlds and seeds

HELDOUT_MANIFEST_PATH = REPOSITORY_DIR / "fixtures" / "manifests" / "heldout_manifest.json"
HELDOUT_TRUTH_PATH = REPOSITORY_DIR / "fixtures" / "manifests" / "heldout_variant_truth.json"
EVALUATION_MANIFEST_PATH = families.MANIFEST_PATH
# Every world is generated as a test-split run: the held-out manifest fixes its entries' split as test, and the
# controls follow, so all 58 worlds draw commit messages and distraction lines from the same pools.
SPLIT = "test"
HELDOUT, CONTROL = "heldout", "control"
ROLES: tuple[str, ...] = (HELDOUT, CONTROL)
HELDOUT_ENTRIES = 24
WORLDS_PER_ENTRY = 2
CONTROL_FAMILIES: tuple[str, ...] = ("D2", "D3", "D4", "D5", "D6")
CONTROLS_PER_FAMILY = 2
# The block the orchestrator reserved for this test (DEV_LOG, phase 11b: "13101 and up"); its 58 worlds take all of
# 13101-13158. Held-out entry i (0-based, manifest order) takes 13101 + 2i and 13102 + 2i; the controls follow.
FIRST_SEED = 13101
LAST_SEED = 13158
CONTROL_FIRST_SEED = 13149
# Every seed range an earlier measurement used (DEV_LOG); none overlaps 13101-13158. Recorded by the build.
USED_SEED_RANGES: tuple[tuple[str, int, int], ...] = (
    ("M4 evaluation manifest", 1001, 2055),
    ("planted instruction, round 1", 7101, 7155),
    ("planted instruction, round 2", 8101, 8155),
    ("val-5 rules-only sweep", 9101, 9155),
    ("phase-8 measurement", 9501, 9700),
    ("planted instruction, round 3", 10101, 10158),
    ("val-6 rules-only sweep block", 11101, 12100),
    ("attribution-v1 development (A2)", 12101, 12600),
    ("order case classification (C)", 14101, 14200),
    ("held-out variant tests and probe (H, orchestrator)", 15101, 15200),
    ("router development (E)", 16101, 16400),
)


class HeldoutEntry(StrictModel):
    """One held-out manifest entry: a D1 parameter set without a seed (the experiment binds the seeds)."""

    run_key: str
    family: str
    variant: str
    split: str
    params: dict[str, JsonValue]


class HeldoutManifest(StrictModel):
    manifest_version: str
    generator_version: str
    notice: str = ""
    runs: tuple[HeldoutEntry, ...]


def load_heldout_manifest(path: Path = HELDOUT_MANIFEST_PATH) -> HeldoutManifest:
    return HeldoutManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Case:
    """One test world: a manifest parameter set bound to one seed, with where it came from."""

    case_id: str
    role: str
    run: families.FamilyRun
    source_manifest: str
    source_run_key: str
    replicate: int  # which of an entry's two worlds (0 or 1); 0 for a control


def heldout_seed(index: int, replicate: int) -> int:
    """The seed of held-out entry ``index`` (0-based, manifest order), world ``replicate`` (0 or 1)."""
    if not 0 <= index < HELDOUT_ENTRIES or not 0 <= replicate < WORLDS_PER_ENTRY:
        raise ValueError(f"held-out entry {index}, world {replicate} is outside the design's "
                         f"{HELDOUT_ENTRIES} entries x {WORLDS_PER_ENTRY} worlds")
    return FIRST_SEED + WORLDS_PER_ENTRY * index + replicate


def seed_problem(seed: int) -> str | None:
    """Why ``seed`` cannot be a seed of this test, or None when it can."""
    if not FIRST_SEED <= seed <= LAST_SEED:
        return f"outside {FIRST_SEED}-{LAST_SEED}, the block reserved for the phase-11b test"
    for owner, low, high in USED_SEED_RANGES:
        if low <= seed <= high:
            return f"inside {low}-{high}, used by the {owner}"
    return None


def control_sources(evaluation: families.EvaluationManifest) -> tuple[families.FamilyRun, ...]:
    """The first CONTROLS_PER_FAMILY TEST runs of each control family, in family order, then manifest order."""
    chosen: list[families.FamilyRun] = []
    for family in CONTROL_FAMILIES:
        runs = [run for run in evaluation.runs if run.family == family and run.split == "test"]
        if len(runs) < CONTROLS_PER_FAMILY:
            raise ValueError(f"{family} has {len(runs)} TEST runs in the evaluation manifest; the controls need "
                             f"{CONTROLS_PER_FAMILY}")
        chosen.extend(runs[:CONTROLS_PER_FAMILY])
    return tuple(chosen)


def experiment_cases(heldout: HeldoutManifest | None = None,
                     evaluation: families.EvaluationManifest | None = None) -> tuple[Case, ...]:
    """Every test world in run order: each held-out entry's two worlds, then the controls.

    Raises ValueError for a manifest the design does not fit, and for any seed that falls outside 13101-13158, is
    taken twice, or is a seed of the M4 manifest.
    """
    heldout_manifest = heldout or load_heldout_manifest()
    evaluation_manifest = evaluation or families.load_manifest()
    if len(heldout_manifest.runs) != HELDOUT_ENTRIES:
        raise ValueError(f"the held-out manifest has {len(heldout_manifest.runs)} entries; the design binds seeds to "
                         f"exactly {HELDOUT_ENTRIES}")
    cases: list[Case] = []
    for index, entry in enumerate(heldout_manifest.runs):
        for replicate in range(WORLDS_PER_ENTRY):
            seed = heldout_seed(index, replicate)
            case_id = f"{entry.run_key}_{seed}"
            run = families.FamilyRun.model_validate({**entry.model_dump(), "run_key": f"selective_{case_id}", "seed": seed})
            cases.append(Case(case_id, HELDOUT, run, heldout_manifest.manifest_version, entry.run_key, replicate))
    for offset, source in enumerate(control_sources(evaluation_manifest)):
        seed = CONTROL_FIRST_SEED + offset
        case_id = f"control_{source.run_key}_{seed}"
        run = families.FamilyRun.model_validate({**source.model_dump(), "run_key": f"selective_{case_id}", "seed": seed})
        cases.append(Case(case_id, CONTROL, run, evaluation_manifest.manifest_version, source.run_key, 0))
    _check_seeds(cases, frozenset(run.seed for run in evaluation_manifest.runs))
    return tuple(cases)


def _check_seeds(cases: Sequence[Case], manifest_seeds: frozenset[int]) -> None:
    seeds = [case.run.seed for case in cases]
    problems = {str(seed): problem for seed in seeds if (problem := seed_problem(seed)) is not None}
    problems |= {str(seed): "a seed of the M4 evaluation manifest" for seed in seeds if seed in manifest_seeds}
    if len(seeds) != len(set(seeds)):
        problems["repeated"] = str(sorted(seed for seed in set(seeds) if seeds.count(seed) > 1))
    if problems:
        raise ValueError(f"the phase-11b test seeds must each lie in {FIRST_SEED}-{LAST_SEED} once: {problems}")
    if any(case.run.split != SPLIT for case in cases):
        raise ValueError(f"every test world is generated as a {SPLIT!r} run")


def seed_report(cases: Sequence[Case], evaluation: families.EvaluationManifest) -> dict[str, JsonValue]:
    """The seed check the build records: the test's seeds against the reserved block and every earlier seed."""
    seeds = sorted(case.run.seed for case in cases)
    manifest_seeds = sorted({run.seed for run in evaluation.runs})
    overlaps: dict[str, JsonValue] = {
        "manifest": sorted(set(seeds) & set(manifest_seeds)),
        "ranges": {str(seed): problem for seed in seeds if (problem := seed_problem(seed)) is not None},
        "repeated": sorted(seed for seed in set(seeds) if seeds.count(seed) > 1),
    }
    if any(overlaps.values()):
        raise ValueError(f"the phase-11b test seeds are not fresh: {overlaps}")
    return {"seeds": seeds, "block": [FIRST_SEED, LAST_SEED], "manifest_seeds": manifest_seeds,
            "used_ranges": [[owner, low, high] for owner, low, high in USED_SEED_RANGES], "overlaps": overlaps}


# --------------------------------------------------------------------------- systems, arms and the code under test

FROZEN_MODEL_SEED = 20261111
MODEL_SEEDS: tuple[int, ...] = (FROZEN_MODEL_SEED, 20261112)
# The two systems that call the model. S's arm diagnoses the worlds the router sends to MODEL; M-all's every
# held-out world. R and R2 are deterministic and need no arm: the summariser computes them from the packets.
SELECTIVE_SYSTEM = "selective"
MODEL_ALL_SYSTEM = "model_all"
MODEL_SYSTEMS: tuple[str, ...] = (SELECTIVE_SYSTEM, MODEL_ALL_SYSTEM)
MODEL_ROUTE = selective.RouteTarget.MODEL.value
RULES_ROUTE = selective.RouteTarget.RULES.value
# The frozen M4 model settings (evaluation/frozen_settings.json), which the arm stage checks against the code.
THINK = False
DEADLINE_S = 180.0
MAX_REPAIRS = 1


@dataclass(frozen=True)
class Arm:
    """One model system at one model seed, and the files its live run writes."""

    system: str
    model_seed: int

    @property
    def name(self) -> str:
        return f"{self.system}_{self.model_seed}"

    @property
    def answers_file(self) -> str:
        return f"answers_{self.name}.json"

    @property
    def trace_file(self) -> str:
        return f"traces/{self.name}.jsonl"

    @property
    def decides(self) -> bool:
        """Decision 1 reads this arm's answers (S's model calls); M-all's arms decide nothing."""
        return self.system == SELECTIVE_SYSTEM


# Run order: S's arms first, at the frozen seed then the second; then M-all's.
ARMS: Mapping[str, Arm] = MappingProxyType({
    arm.name: arm for arm in (Arm(system, seed) for system in MODEL_SYSTEMS for seed in MODEL_SEEDS)
})


def arm_named(name: str) -> Arm:
    if name not in ARMS:
        raise SystemExit(f"{name!r} is not an arm of this test ({', '.join(ARMS)})")
    return ARMS[name]


def arm_for(system: str, model_seed: int) -> Arm:
    return arm_named(Arm(system, model_seed).name)


def arm_case_ids(arm: Arm, cases: Sequence[Mapping[str, JsonValue]], route_targets: Mapping[str, str]) -> list[str]:
    """The worlds an arm diagnoses, in run order: S's, every MODEL-routed world (held-out and control); M-all's, every
    held-out world."""
    if arm.system == SELECTIVE_SYSTEM:
        return [str(case["case_id"]) for case in cases if route_targets[str(case["case_id"])] == MODEL_ROUTE]
    return [str(case["case_id"]) for case in cases if case["role"] == HELDOUT]


# The versions the export must report: diag-p5 and val-6, the shipped prompt and validator, and the frozen router,
# rules-v3, comparator, rules-v2 and packet.
CODE_VERSIONS: Mapping[str, str] = MappingProxyType({
    "prompt": "diag-p5", "validator": "val-6", "router": "router-v1", "rules_v3": "rules-v3",
    "attribution": "attribution-v1", "rules": "rules-v2", "packet": "pkt-2",
})
# Frozen before the test: the router and rules-v3 (commit efd1b16), the comparator and rules-v2. The export must hold
# these files byte for byte, so the test measures exactly what was developed and frozen.
FROZEN_SOURCES: Mapping[str, str] = MappingProxyType({
    "selective.py": "a0efc21bc0c5fe25a3c632ba9cf596d6a60ad618bf385baa96ceeba1a583b619",
    "attribution_rules.py": "a365f58abf9f9b3858efc42df58b5b981efc45efd2ff25d3cbf8d416affbf809",
    "rules.py": "3b815b34ec37e87d23e8c44e895cf4a8e41ab55494f934450ef2f65b0419fc7a",
})
# The code under test, named by the orchestrator: a clean export of 43a160f (diag-p5, committed on the router's
# efd1b16). Its package sources hash is round 2's package_sources_sha256 over that export's 42 .py files; every stage
# refuses any other package, so the arms and the summariser run the same code.
CODE_COMMIT = "43a160f2ad5e59562e0b5c896139abe546ed5cd4"
CODE_PACKAGE_SOURCES_SHA256 = "d81eeb26d56adadebdda707e5d08c1d92bab63c650d5e2d106fe0c018eba4d92"


def code_versions() -> dict[str, str]:
    """The versions the imported package reports, under CODE_VERSIONS' names."""
    return {"prompt": diagnosis.PROMPT_VERSION, "validator": diagnosis.VALIDATOR_VERSION,
            "router": selective.ROUTER_VERSION, "rules_v3": selective.RULES_V3_VERSION,
            "attribution": attribution_rules.ATTRIBUTION_RULES_VERSION, "rules": rules.RULES_VERSION,
            "packet": packet.PACKET_VERSION}


def package_sources_sha256(package_dir: Path) -> str:
    """Round 2's hash of a package's sources: every ``.py`` file's path and SHA-256, one line each, by path."""
    return str(round3_design.round2_module("diagnose_arm").package_sources_sha256(package_dir))


def checked_code_under_test(code_export: Path) -> dict[str, JsonValue]:
    """The package this process runs, after checking it is the pinned export: its import path, versions and sources.

    The arm stage and the summariser both call this, so R, R2 and the routes are computed with the code the model
    arms ran.
    """
    package_dir = Path(str(recovery_workbench.__file__)).resolve().parent
    exported = (code_export / "src" / "recovery_workbench").resolve()
    if package_dir != exported:
        raise SystemExit(f"recovery_workbench was imported from {package_dir}, not from the export {exported}; "
                         f"run with PYTHONPATH={code_export / 'src'}")
    found = code_versions()
    if found != dict(CODE_VERSIONS):
        raise SystemExit(f"the code under test must report {dict(CODE_VERSIONS)}, but reports {found}; refusing to run")
    sources = {name: sha256_hex((package_dir / name).read_bytes()) for name in (*FROZEN_SOURCES, "diagnosis.py")}
    drifted = {name: sources[name] for name, digest in FROZEN_SOURCES.items() if sources[name] != digest}
    if drifted:
        raise SystemExit(f"the export changes frozen files {drifted}; the test must run them as frozen ({dict(FROZEN_SOURCES)})")
    package_sources = package_sources_sha256(package_dir)
    if package_sources != CODE_PACKAGE_SOURCES_SHA256:
        raise SystemExit(f"the export's package sources hash is {package_sources}, not the pinned "
                         f"{CODE_PACKAGE_SOURCES_SHA256} of commit {CODE_COMMIT}; refusing to run")
    return {"commit": CODE_COMMIT, "code_export": str(code_export), "package_dir": str(package_dir),
            "versions": found, "sources_sha256": sources, "package_sources_sha256": package_sources}


# --------------------------------------------------------------------------- decisions

# One-sided exact sign tests count as significant at p <= ALPHA; the two directions are tested separately.
ALPHA = Fraction(1, 20)
# An S arm may have this many operational failures (round 3's definition), each counted as review and never dropped;
# more make decision 1 invalid. For M-all's arms the count is descriptive: no decision reads an M-all answer.
MAX_OPERATIONAL_FAILURES = 2
# Decision 1 is invalid when more than this many of the 10 control worlds route to MODEL.
MAX_CONTROL_MODEL_ROUTES = 1
NO_DETECTABLE_ADVANTAGE = "no detectable advantage in this sample"
ADOPT_RULES_V3 = "adopt rules_v3 as the rules-first default"
KEEP_RULES_V2 = "keep rules-v2"


# --------------------------------------------------------------------------- files, the pre-registration and addenda

PREREGISTRATION_FILE: str = round2_design.PREREGISTRATION_FILE
ADDENDA_FILE: str = round2_design.ADDENDA_FILE
CASES_FILE = "cases.json"
PACKETS_DIR = "packets"
REPORT_FILE = "report.json"
DRAFT_MARKER: str = round3_design.DRAFT_MARKER
FROZEN_SETTINGS_PATH: Path = round1_design.FROZEN_SETTINGS_PATH


def packet_file(case_id: str) -> str:
    return f"{PACKETS_DIR}/{case_id}.json"


def preregistration_record(directory: Path = SELECTIVE_DIR) -> dict[str, JsonValue]:
    """The pre-registration's file name and SHA-256, as the build records them in cases.json."""
    record: dict[str, JsonValue] = round2_design.preregistration_record(directory)
    return record


def preregistration_is_draft(directory: Path = SELECTIVE_DIR) -> bool:
    return bool(round3_design.preregistration_is_draft(directory))


def checked_final_preregistration(cases_document: Mapping[str, JsonValue], directory: Path) -> dict[str, JsonValue]:
    """Round 3's check: the file is the one the build hashed, and its first line no longer says DRAFT."""
    record: dict[str, JsonValue] = round3_design.checked_final_preregistration(cases_document, directory)
    return record


def checked_addenda(directory: Path = SELECTIVE_DIR) -> list[JsonValue]:
    """Round 2's check: every dated ``ADDENDUM_*.md`` is recorded in addenda.json with its SHA-256, unchanged."""
    recorded: list[JsonValue] = round2_design.checked_addenda(directory)
    return recorded


def checked_packets(cases_document: Mapping[str, JsonValue], directory: Path) -> dict[str, EvidencePacket]:
    """Every case's packet, after checking it still has its build-time hash."""
    cases: Sequence[Mapping[str, JsonValue]] = cases_document["cases"]  # type: ignore[assignment]
    packets: dict[str, EvidencePacket] = {}
    for case in cases:
        built = round1_design.read_packet(directory / str(case["packet_file"]))
        if built.packet_hash() != case["packet_hash"]:
            raise SystemExit(f"{case['case_id']}: the packet file changed after the build (hash {built.packet_hash()}, "
                             f"built {case['packet_hash']}); nothing is computed from a changed packet")
        packets[str(case["case_id"])] = built
    return packets
