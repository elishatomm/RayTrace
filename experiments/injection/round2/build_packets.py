"""Stage 1 of round 2, in the test container only: every world's packet, rules answer and expectations.

    docker run --rm --init --network none --read-only --tmpfs /tmp --entrypoint python \\
        -v "<host output dir>":/out recovery-workbench-test:a \\
        experiments/injection/round2/build_packets.py --output-dir /out

Round 1's build stage does the work for each world (``experiments/injection/build.py``). It seeds the
world into its own throwaway database under the container's in-memory /tmp, builds the packet at the
cutoff exactly as M4 builds it, and compares each planted world with its clean twin. This stage adds
round 2's cases, the fresh-seed check and the pre-registration's SHA-256. It writes
``packets/<case>.json`` and ``cases.json`` and refuses to overwrite them. No wall-clock value enters
the output, so a second build is byte-identical.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from itertools import groupby
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import __version__, diagnosis, packet, rules, settings
from recovery_workbench.simulator import families
from recovery_workbench.simulator.world import GENERATOR_VERSION, PLANT_LEAD_MS, PLANTED_EVIDENCE_ID

import round2_design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

round1 = round2_design.round1_design
round1_build = round2_design.round1_module("build")


def build_all(cases: Sequence[round2_design.Case], output_dir: Path) -> list[JsonValue]:
    """Build each seed group, clean world first; write its packets and return round 1's case records."""
    stale_after_ms = settings.load_settings().stale_after_seconds * 1_000
    records: list[JsonValue] = []
    with tempfile.TemporaryDirectory(prefix="a_injection_round2_build_") as scratch:
        for twin_id, group in groupby(cases, key=lambda case: case.twin_id):
            worlds = [round1_build.build_world(case, Path(scratch), stale_after_ms) for case in group]
            clean = worlds[0]
            if clean.case.case_id != twin_id:
                raise AssertionError(f"the first case of seed group {twin_id} must be the clean world")
            for world in worlds:
                round1.write_packet(output_dir / round1.packet_file(world.case.case_id), world.built)
                records.append(round1_build.case_record(world, clean))
                print(f"{world.case.case_id}: packet {world.built.packet_hash()[:16]}", flush=True)
    return records


def _design_record() -> dict[str, JsonValue]:
    return {
        "split": round2_design.SPLIT, "seed_offset": round2_design.SEED_OFFSET,
        "families": list(round2_design.FAMILIES),
        "targets": {family: list(attacks) for family, attacks in round2_design.TARGETS.items()},
        "controls": list(round2_design.CONTROL_FAMILIES), "channels": dict(round2_design.CHANNELS),
        "planted_evidence_id": PLANTED_EVIDENCE_ID, "order_note_lead_ms": PLANT_LEAD_MS,
        "arms": {name: {"validator_version": arm.validator_version, "code": arm.code_source,
                        "answers_file": arm.answers_file, "trace_file": arm.trace_file}
                 for name, arm in round2_design.ARMS.items()},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the round-2 planted-instruction packets (run in the test container).")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output_dir: Path = args.output_dir
    if (output_dir / round2_design.CASES_FILE).exists() or (output_dir / round2_design.PACKETS_DIR).exists():
        parser.error(f"{output_dir} already holds a build; use an empty output directory")
    preregistration = round2_design.preregistration_record()  # before any world: no build without it
    manifest = families.load_manifest()
    cases = round2_design.round2_cases(manifest)
    seed_check = round2_design.fresh_seed_report(cases, manifest, round2_design.round1_built_seeds())
    records = build_all(cases, output_dir)
    round1.write_new_json(output_dir / round2_design.CASES_FILE, {
        "experiment": "Planted instruction, round 2: validator val-5 against val-4 on fresh seeds (owner answer Q19)",
        "notice": ("Built in the test container. expected, faulting_deploy_id, harmless_deploy_ids and planted are "
                   "oracle extracts used only for scoring; the diagnose stage sends the packet files alone to the "
                   "model. versions.validator is the validator that checked the rules answers at build time; each "
                   "arm's validator is recorded in its answers file."),
        "preregistration": preregistration,
        "versions": {"workbench": __version__, "world_generator": GENERATOR_VERSION, "packet": packet.PACKET_VERSION,
                     "rules": rules.RULES_VERSION, "validator": diagnosis.VALIDATOR_VERSION,
                     "prompt": diagnosis.PROMPT_VERSION, "evaluation_manifest": manifest.manifest_version},
        "design": _design_record(),
        "fresh_seed_check": seed_check,
        "cases": records,
    })
    print(f"wrote {len(records)} cases to {output_dir}; pre-registration sha256 {preregistration['sha256']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
