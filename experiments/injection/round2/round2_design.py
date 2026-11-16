"""Round 2 of the planted-instruction study (owner answer Q19): its seeds, cases, arms and shared helpers.

Round 2 measures validator ``val-5`` against ``val-4`` on fresh worlds, so the two rules are not judged
on the 50 round-1 answers they were designed from. It reuses round 1 (``experiments/injection``)
unchanged, by importing it:

* the same targeted families, notes and channels (round 1's ``TARGETS`` and ``CHANNELS``), each planted
  world with its clean twin, plus five clean D5 runs as false-refusal controls;
* the same parameter sets (each family's five frozen TEST-split runs), generated as ``dev`` runs, with
  fresh seeds: frozen TEST seed + 6100, so 8101-8155. Round 1 used + 5100 (7101-7155), and every
  manifest seed is at most 2055.

Stages: ``build_packets.py`` (test container), ``diagnose_arm.py`` (host, live model, JSON files only, one
validator arm at a time) and ``summarize.py`` (host, JSON files only). PREREGISTRATION.md fixed the
metrics before any live call; the build records its SHA-256 and every later stage checks it.

Round 1's stage modules import each other by bare name (``import design``), so round 1's folder goes on
``sys.path`` and no round-2 module takes one of their names. The task names round 2's summariser
``summarize.py``, so round 1's stage modules are loaded from their files under distinct names.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType

from pydantic import JsonValue

from recovery_workbench.contracts import sha256_hex
from recovery_workbench.simulator import families
from recovery_workbench.simulator.scenarios import PlantedInstruction

ROUND2_DIR = Path(__file__).resolve().parent
ROUND1_DIR = ROUND2_DIR.parent


def _round1_design() -> ModuleType:
    """Round 1's ``design``, imported under the bare name round 1's other modules use for it."""
    if str(ROUND1_DIR) not in sys.path:
        sys.path.append(str(ROUND1_DIR))
    module = importlib.import_module("design")
    if Path(str(module.__file__)).resolve() != ROUND1_DIR / "design.py":
        raise ImportError(f"'design' resolved to {module.__file__}, not to round 1's {ROUND1_DIR / 'design.py'}")
    return module


def round1_module(name: str) -> ModuleType:
    """Round 1's stage module ``name`` (``build``, ``diagnose`` or ``summarize``), loaded once from its file."""
    qualified = f"injection_round1_{name}"
    loaded = sys.modules.get(qualified)
    if loaded is not None:
        return loaded
    _round1_design()  # its folder must be on sys.path first: the stage modules ``import design``
    spec = importlib.util.spec_from_file_location(qualified, ROUND1_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load round 1's {name}.py from {ROUND1_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module  # before executing it: dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


round1_design = _round1_design()
Case = round1_design.Case

SPLIT: str = round1_design.SPLIT
TARGETS = round1_design.TARGETS
CHANNELS = round1_design.CHANNELS
# Fresh seed = frozen TEST seed + offset (2001 -> 8101). Round 1 used 5100, so it sits in 7101-7155.
SEED_OFFSET = 6100
ROUND1_SEED_RANGE = (7101, 7155)
# Seeds from here up belong to Agent B's rules-only property test (orchestrator, phase 8; ADDENDUM_2026-11-12.md).
RESERVED_SEEDS_FROM = 9101
# Clean runs only: no_incident is the right answer there, so a refusal of it is a false refusal.
CONTROL_FAMILIES: tuple[str, ...] = ("D5",)
FAMILIES: tuple[str, ...] = tuple(sorted({*TARGETS, *CONTROL_FAMILIES}))
PROMPT_VERSION = "diag-p2"

PREREGISTRATION_FILE = "PREREGISTRATION.md"
ADDENDA_FILE = "addenda.json"
ADDENDUM_PATTERN = "ADDENDUM_*.md"
CASES_FILE: str = round1_design.CASES_FILE
PACKETS_DIR: str = round1_design.PACKETS_DIR
PAIRED_REPORT_FILE = "paired_report.json"
ROUND1_CASES_PATH = ROUND1_DIR / round1_design.CASES_FILE


@dataclass(frozen=True)
class Arm:
    """One validator arm: the version its code under test must report, where that code comes from, its outputs."""

    name: str
    validator_version: str
    code_source: str
    answers_file: str
    trace_file: str


BASELINE_ARM = "val-4"
CANDIDATE_ARM = "val-5"
ARMS: Mapping[str, Arm] = MappingProxyType({
    BASELINE_ARM: Arm(BASELINE_ARM, "val-4", "git archive d5b7933", "answers_val4.json", "traces/val4.jsonl"),
    CANDIDATE_ARM: Arm(CANDIDATE_ARM, "val-5", "git archive of the commit that adds val-5, once verified",
                       "answers_val5.json", "traces/val5.jsonl"),
})


# --------------------------------------------------------------------------- cases and seeds


def round2_cases(manifest: families.EvaluationManifest | None = None) -> tuple[Case, ...]:
    """Every round-2 case in run order: per family and seed, the clean world first, then one per note.

    Raises ValueError for a seed that is not fresh: a manifest seed, a round-1 seed, or one inside
    round 1's range.
    """
    frozen = manifest or families.load_manifest()
    taken = frozenset({run.seed for run in frozen.runs} | {case.run.seed for case in round1_design.experiment_cases(frozen)})
    cases: list[Case] = []
    for family in FAMILIES:
        plants = [None, *(PlantedInstruction(attack=attack, channel=CHANNELS[attack]) for attack in TARGETS.get(family, ()))]
        for source in (run for run in frozen.runs if run.family == family and run.split == "test"):
            run = _fresh_run(source, taken)
            cases.extend(Case(round1_design.case_id_for(run, plant), run, plant) for plant in plants)
    return tuple(cases)


def _fresh_run(source: families.FamilyRun, taken: frozenset[int]) -> families.FamilyRun:
    seed = source.seed + SEED_OFFSET
    low, high = ROUND1_SEED_RANGE
    if seed in taken or low <= seed <= high:
        raise ValueError(f"seed {seed} (from {source.run_key}) is a manifest or round-1 seed; round 2 must use fresh seeds")
    if seed >= RESERVED_SEEDS_FROM:
        raise ValueError(f"seed {seed} (from {source.run_key}) is reserved for the rules-only property test "
                         f"(seeds {RESERVED_SEEDS_FROM} and up); choose a smaller offset")
    return families.FamilyRun(run_key=f"injection_round2_{source.family.lower()}_{seed}", family=source.family,
                              variant=source.variant, split=SPLIT, seed=seed, params=source.params)


def round1_built_seeds(path: Path = ROUND1_CASES_PATH) -> frozenset[int]:
    """The seeds round 1 actually built, read from its cases.json."""
    cases: list[Mapping[str, JsonValue]] = round1_design.read_json(path)["cases"]  # type: ignore[assignment]
    return frozenset(int(case["seed"]) for case in cases)  # type: ignore[arg-type]


def fresh_seed_report(cases: Sequence[Case], manifest: families.EvaluationManifest,
                      round1_built: frozenset[int]) -> dict[str, JsonValue]:
    """The disjointness check the build records: round-2 seeds against the manifest and round 1, designed and built."""
    round2 = sorted({case.run.seed for case in cases})
    manifest_seeds = sorted({run.seed for run in manifest.runs})
    round1 = sorted({case.run.seed for case in round1_design.experiment_cases(manifest)} | round1_built)
    low, high = ROUND1_SEED_RANGE
    overlaps: dict[str, JsonValue] = {
        "manifest": sorted(set(round2) & set(manifest_seeds)),
        "round1": sorted(set(round2) & set(round1)),
        "round1_range": [seed for seed in round2 if low <= seed <= high],
    }
    if any(overlaps.values()):
        raise ValueError(f"round-2 seeds are not fresh: {overlaps}")
    return {"round2_seeds": round2, "manifest_seeds": manifest_seeds, "round1_seeds": round1,
            "round1_range": [low, high], "overlaps": overlaps}


# --------------------------------------------------------------------------- the pre-registration


def preregistration_record(directory: Path = ROUND2_DIR) -> dict[str, JsonValue]:
    """The pre-registration file's name and SHA-256, as the build records them in cases.json."""
    return {"file": PREREGISTRATION_FILE, "sha256": sha256_hex((directory / PREREGISTRATION_FILE).read_bytes())}


def checked_preregistration(cases_document: Mapping[str, JsonValue], directory: Path) -> dict[str, JsonValue]:
    """The pre-registration record, after checking the file is byte-identical to the one the build hashed."""
    recorded = cases_document.get("preregistration")
    current = preregistration_record(directory)
    if current != recorded:
        raise SystemExit(f"{directory / PREREGISTRATION_FILE} is not the file the build hashed ({recorded} -> {current}); "
                         "it is frozen once written: put any change in a dated addendum file instead")
    return current


def checked_addenda(directory: Path = ROUND2_DIR) -> list[JsonValue]:
    """The recorded addenda (``addenda.json``), after checking each ``ADDENDUM_*.md`` file is recorded with its SHA-256.

    Addenda come after the build, so cases.json cannot hold their hashes. An addendum edited after it
    was recorded, or never recorded, stops the stage, as an edited pre-registration does.
    """
    found = sorted(path.name for path in directory.glob(ADDENDUM_PATTERN))
    if not found:
        return []
    record_path = directory / ADDENDA_FILE
    if not record_path.exists():
        raise SystemExit(f"{directory} holds addenda {found} but no {ADDENDA_FILE} recording their SHA-256")
    recorded: list[Mapping[str, JsonValue]] = round1_design.read_json(record_path)["addenda"]  # type: ignore[assignment]
    current = [{"file": name, "sha256": sha256_hex((directory / name).read_bytes())} for name in found]
    listed = sorted(({"file": entry["file"], "sha256": entry["sha256"]} for entry in recorded), key=lambda entry: str(entry["file"]))
    if listed != current:
        raise SystemExit(f"the addenda in {directory} do not match {ADDENDA_FILE} (recorded {listed}, found {current}); "
                         "an addendum is frozen once recorded: write a new dated one instead")
    return list(recorded)  # type: ignore[arg-type]
