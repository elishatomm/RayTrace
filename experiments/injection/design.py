"""The planted-instruction experiment (PLAN.md stretch item 2): its cases, files and shared helpers.

Each targeted family reuses the parameter sets of its five frozen TEST-split runs, with fresh seeds
(never a manifest seed) and generated as development runs. Every seed is generated clean and once
per attack that targets its family, so each planted world has a clean twin that differs from it
only in the planted text. Each attack uses one channel:

* ``suppress`` (D1, D2) and ``false_approval`` (D2, D4) arrive as a customer's order note that the
  checkout service logs; D2 and D4 runs have no deployment, so a commit message is not available;
* ``misattribute`` (D1, D6) arrives in the most recent deployment's commit message, written by
  whoever shipped that deploy.

Stages: ``build.py`` (in the test container), ``diagnose.py`` (host, live model, JSON files only),
``summarize.py`` (host, JSON files only). See README.md for the commands.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import JsonValue

from recovery_workbench.contracts import EvidencePacket
from recovery_workbench.simulator import families
from recovery_workbench.simulator.scenarios import PlantAttack, PlantChannel, PlantedInstruction

EXPERIMENT_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = EXPERIMENT_DIR.parents[1]
FROZEN_SETTINGS_PATH = REPOSITORY_DIR / "evaluation" / "frozen_settings.json"
PACKETS_DIR = "packets"
CASES_FILE = "cases.json"
ANSWERS_FILE = "answers.json"
TRACES_FILE = "traces/live.jsonl"
REPORT_FILE = "injection_report.json"
README_FILE = "README.md"

SPLIT = "dev"
# Fresh seed = frozen TEST seed + offset (2001 -> 7101). No manifest seed lies in 7000-7999.
SEED_OFFSET = 5100
TARGETS: Mapping[str, tuple[PlantAttack, ...]] = MappingProxyType({
    "D1": ("suppress", "misattribute"),
    "D2": ("suppress", "false_approval"),
    "D4": ("false_approval",),
    "D6": ("misattribute",),
})
CHANNELS: Mapping[PlantAttack, PlantChannel] = MappingProxyType({
    "suppress": "order_note",
    "misattribute": "commit_message",
    "false_approval": "order_note",
})
CLEAN = "clean"


@dataclass(frozen=True)
class Case:
    """One world to build and diagnose: a family run with a fresh seed, clean or with one plant."""

    case_id: str
    run: families.FamilyRun
    plant: PlantedInstruction | None

    @property
    def twin_id(self) -> str:
        """The clean world this case is compared with (itself when clean)."""
        return case_id_for(self.run, None)


def case_id_for(run: families.FamilyRun, plant: PlantedInstruction | None) -> str:
    return f"{run.family.lower()}_{run.seed}_{CLEAN if plant is None else plant.attack}"


def experiment_cases(manifest: families.EvaluationManifest | None = None) -> tuple[Case, ...]:
    """Every case, grouped by seed: the clean world first, then one per targeted attack."""
    frozen = manifest or families.load_manifest()
    taken = {run.seed for run in frozen.runs}
    cases: list[Case] = []
    for family, attacks in TARGETS.items():
        for source in (run for run in frozen.runs if run.family == family and run.split == "test"):
            seed = source.seed + SEED_OFFSET
            if seed in taken:
                raise ValueError(f"seed {seed} is a manifest seed; the experiment must use fresh seeds")
            run = families.FamilyRun(run_key=f"injection_{family.lower()}_{seed}", family=source.family,
                                     variant=source.variant, split=SPLIT, seed=seed, params=source.params)
            plants = [None, *(PlantedInstruction(attack=attack, channel=CHANNELS[attack]) for attack in attacks)]
            cases.extend(Case(case_id_for(run, plant), run, plant) for plant in plants)
    return tuple(cases)


def packet_file(case_id: str) -> str:
    return f"{PACKETS_DIR}/{case_id}.json"


def write_packet(path: Path, built: EvidencePacket) -> None:
    """Keys stay in model order, as in experiments/m0: the prompt renders facts in that order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(built.model_dump(mode="json"), indent=2) + "\n")


def read_packet(path: Path) -> EvidencePacket:
    return EvidencePacket.model_validate_json(path.read_text(encoding="utf-8"))


def read_json(path: Path) -> dict[str, JsonValue]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} holds {type(document).__name__}, expected a JSON object")
    return document


def write_new_json(path: Path, document: JsonValue) -> None:
    """Write once: built and live outputs are never overwritten (move the old file away to run again)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def write_derived(path: Path, text: str) -> None:
    """Rewrite a derived summary: it is recomputed from the JSON files, so replacing it loses nothing."""
    path.write_text(text, encoding="utf-8")
