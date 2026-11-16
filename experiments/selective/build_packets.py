"""Stage 1, in the test container only: every test world's packet and its expectations. No system runs here.

    docker run --rm --init --network none --read-only --tmpfs /tmp --entrypoint sh recovery-workbench-test:g -c \\
        'python experiments/selective/build_packets.py --output-dir /tmp/g_build >&2 &&
         tar -C /tmp/g_build -cf - .' > <host scratch>/g_build.tar

The container keeps the suite's isolation flags and mounts nothing: the build writes under the in-memory /tmp, and the
finished build leaves as a tar stream on stdout. Each world is seeded into its own throwaway database there, and its
packet is built at the cutoff exactly as M4 builds it (``evaluation.run.build_case_packet``).

Unlike rounds 1-3, the build runs no system on a packet: not rules-v2, rules-v3, the router, the comparator or the
model. Their answers on these packets are results, and none may exist before the pre-registration is committed; the
summariser computes them afterwards, with the code under test. The build checks inputs only: each world opens exactly
one incident, and a held-out world's packet offers its faulting deployment and its innocent one as candidates, so a
correct answer exists and the wrong one can be given.

Writes ``packets/<case>.json`` and ``cases.json``, records the pre-registration's SHA-256 and whether it is still a
draft, and refuses to overwrite either. No wall-clock value enters the output, so a second build is byte-identical.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import __version__, packet, settings, storage
from recovery_workbench.contracts import EvidencePacket, ModelMode, sha256_hex
from recovery_workbench.evaluation import run as evaluation_run
from recovery_workbench.simulator import driver, families
from recovery_workbench.simulator.world import GENERATOR_VERSION, generate_world

import selective_design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

round1 = selective_design.round1_design


@dataclass(frozen=True)
class BuiltWorld:
    case: selective_design.Case
    built: EvidencePacket
    truth: Mapping[str, JsonValue]
    seeded: driver.SeedSummary


def build_world(case: selective_design.Case, work_dir: Path, stale_after_ms: int) -> BuiltWorld:
    """Seed one world into its own database and build its packet at the cutoff; nothing answers it."""
    world = generate_world(families.spec_for(case.run), case.run.seed)
    directory = work_dir / case.case_id
    directory.mkdir()
    with closing(storage.connect(directory / "workbench.sqlite")) as conn:
        seeded = driver.seed_world(conn, world, oracle_dir=directory / "oracle", provider_dir=directory / "provider",
                                   model_mode=ModelMode.RULES, stale_after_ms=stale_after_ms)
        if len(seeded.incident_ids) != 1:
            raise ValueError(f"{case.case_id} (seed {case.run.seed}) opened {len(seeded.incident_ids)} incidents; every "
                             "test world needs exactly one, and the pre-registration must say how a world that does not "
                             "is handled before any rebuild")
        truth = round1.read_json(Path(str(seeded.oracle_path)))
        scored = evaluation_run.Case(
            run_id=world.run_id, seed=case.run.seed, family=case.run.family, variant=case.run.variant,
            params=case.run.params, cutoff_ms=world.cutoff_ms, expected=truth["expected"], oracle=truth,  # type: ignore[arg-type]
            provider_records=Path(str(seeded.provider_records_path)),
        )
        built = evaluation_run.build_case_packet(conn, scored)
    shutil.rmtree(directory)  # the container's /tmp is memory; keep one world at a time
    return BuiltWorld(case, built, truth, seeded)


def input_problems(world: BuiltWorld) -> list[str]:
    """What makes a built world unfit for the test, judged on its inputs alone; empty when it is fit."""
    if world.case.role != selective_design.HELDOUT:
        return []
    truth = world.truth
    harmless: Sequence[str] = truth["harmless_deploy_ids"]  # type: ignore[assignment]
    missing = sorted({str(truth["faulting_deploy_id"]), *harmless} - set(world.built.candidate_change_ids))
    if truth["faulting_deploy_id"] is None or len(harmless) != 1 or missing:
        return [f"a held-out world needs its faulting deployment and its one innocent deployment among the packet's "
                f"candidates; missing {missing}, innocent {list(harmless)}"]
    return []


def case_record(world: BuiltWorld) -> dict[str, JsonValue]:
    case, built, truth = world.case, world.built, world.truth
    return {
        "case_id": case.case_id, "role": case.role, "family": case.run.family, "variant": case.run.variant,
        "seed": case.run.seed, "split": case.run.split, "params": dict(case.run.params),
        "source": {"manifest": case.source_manifest, "run_key": case.source_run_key, "replicate": case.replicate},
        "run_id": world.seeded.run_id, "cutoff_ms": built.incident.evidence_cutoff_ms,
        "observable_sha256": world.seeded.observable_sha256, "oracle_sha256": world.seeded.oracle_sha256,
        "packet_file": selective_design.packet_file(case.case_id), "packet_hash": built.packet_hash(),
        "candidate_change_ids": list(built.candidate_change_ids),
        "expected": truth["expected"], "faulting_deploy_id": truth["faulting_deploy_id"],
        "harmless_deploy_ids": truth["harmless_deploy_ids"],
    }


def build_all(cases: Sequence[selective_design.Case], output_dir: Path) -> list[JsonValue]:
    """Build every world in run order; write its packet and return its record. Stops on a world unfit for the test."""
    stale_after_ms = settings.load_settings().stale_after_seconds * 1_000
    records: list[JsonValue] = []
    with tempfile.TemporaryDirectory(prefix="g_selective_build_") as scratch:
        for case in cases:
            world = build_world(case, Path(scratch), stale_after_ms)
            problems = input_problems(world)
            if problems:
                raise ValueError(f"{case.case_id} (seed {case.run.seed}) is unfit for the test: {problems}")
            round1.write_packet(output_dir / selective_design.packet_file(case.case_id), world.built)
            records.append(case_record(world))
            print(f"{case.case_id}: packet {world.built.packet_hash()[:16]}", flush=True)
    return records


def _file_record(path: Path) -> dict[str, JsonValue]:
    return {"file": str(path.relative_to(selective_design.REPOSITORY_DIR)), "sha256": sha256_hex(path.read_bytes())}


def design_record() -> dict[str, JsonValue]:
    """The design as the build saw it: worlds, systems, arms, the code to be named, and the decision constants."""
    return {
        "split": selective_design.SPLIT, "seed_block": [selective_design.FIRST_SEED, selective_design.LAST_SEED],
        "heldout": {"entries": selective_design.HELDOUT_ENTRIES, "worlds_per_entry": selective_design.WORLDS_PER_ENTRY,
                    "seed_rule": "entry i (0-based, manifest order) takes 13101 + 2i and 13102 + 2i"},
        "controls": {"families": list(selective_design.CONTROL_FAMILIES),
                     "per_family": selective_design.CONTROLS_PER_FAMILY,
                     "seed_rule": "13149 onwards, in family order, each family's first TEST runs in manifest order"},
        "systems": {"R": "rules_v3 on every world", "S": "route; MODEL: the live local diagnosis; RULES: rules_v3",
                    "R2": "rules.diagnose (rules-v2)", "M-all": "the model on every held-out world (descriptive)"},
        "arms": {name: {"system": arm.system, "model_seed": arm.model_seed, "answers_file": arm.answers_file,
                        "trace_file": arm.trace_file, "decides": arm.decides}
                 for name, arm in selective_design.ARMS.items()},
        "code_under_test": {"versions": dict(selective_design.CODE_VERSIONS),
                            "frozen_sources": dict(selective_design.FROZEN_SOURCES),
                            "commit": selective_design.CODE_COMMIT,
                            "package_sources_sha256": selective_design.CODE_PACKAGE_SOURCES_SHA256},
        "model": {"think": selective_design.THINK, "deadline_s": selective_design.DEADLINE_S,
                  "max_repairs": selective_design.MAX_REPAIRS, "model_seeds": list(selective_design.MODEL_SEEDS),
                  "settings": str(selective_design.FROZEN_SETTINGS_PATH.relative_to(selective_design.REPOSITORY_DIR))},
        "decision_1": {"unit": "held-out manifest entry: 2 worlds x 2 model seeds", "units": selective_design.HELDOUT_ENTRIES,
                       "wrong_attributions_over": "all 58 worlds, controls included"},
        "alpha": str(selective_design.ALPHA), "max_operational_failures": selective_design.MAX_OPERATIONAL_FAILURES,
        "max_control_model_routes": selective_design.MAX_CONTROL_MODEL_ROUTES,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the phase-11b test packets (run in the test container).")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output_dir: Path = args.output_dir
    if (output_dir / selective_design.CASES_FILE).exists() or (output_dir / selective_design.PACKETS_DIR).exists():
        parser.error(f"{output_dir} already holds a build; use an empty output directory")
    preregistration = selective_design.preregistration_record()  # before any world: no build without it
    heldout = selective_design.load_heldout_manifest()
    evaluation = families.load_manifest()
    cases = selective_design.experiment_cases(heldout, evaluation)
    seed_check = selective_design.seed_report(cases, evaluation)
    records = build_all(cases, output_dir)
    round1.write_new_json(output_dir / selective_design.CASES_FILE, {
        "experiment": ("Selective AI for deployment attribution (phase 11b): the one test. R (rules_v3), S (the "
                       "router, then the local model or rules_v3), R2 (rules-v2) and M-all (the model everywhere), on "
                       "48 held-out worlds and 10 control worlds"),
        "notice": ("Built in the test container; no system was run on these packets. expected, faulting_deploy_id "
                   "and harmless_deploy_ids are oracle extracts used only for scoring; the arms send the packet files "
                   "alone to the model."),
        "preregistration": preregistration,
        "preregistration_draft": selective_design.preregistration_is_draft(),
        "versions": {"workbench": __version__, "world_generator": GENERATOR_VERSION, "packet": packet.PACKET_VERSION,
                     "family_generator": evaluation.generator_version, "evaluation_manifest": evaluation.manifest_version,
                     "heldout_manifest": heldout.manifest_version},
        "inputs": {"heldout_manifest": _file_record(selective_design.HELDOUT_MANIFEST_PATH),
                   "heldout_variant_truth": _file_record(selective_design.HELDOUT_TRUTH_PATH),
                   "evaluation_manifest": _file_record(selective_design.EVALUATION_MANIFEST_PATH)},
        "design": design_record(),
        "seed_check": seed_check,
        "cases": records,
    })
    print(f"wrote {len(records)} cases to {output_dir}; pre-registration sha256 {preregistration['sha256']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
