"""Stage 1 of round 3, in the test container only: every world's packet, rules answer and expectations.

    docker run --rm --init --network none --read-only --tmpfs /tmp --entrypoint python \\
        -v "<host output dir>":/out recovery-workbench-test:a \\
        experiments/injection/round3/build_packets.py --output-dir /out

The image must hold the baseline code (prompt ``diag-p2``, validator ``val-5``): each case records the
SHA-256 of its rendered prompt, and the arms compare their own rendering with that ``diag-p2`` record.

Round 1's build stage does the work for each world (``experiments/injection/build.py``), as round 2 uses
it: it seeds the world into its own throwaway database under the container's in-memory /tmp, builds the
packet at the cutoff exactly as M4 builds it, and compares each planted world with its clean twin. This
stage adds round 3's cases, the fresh-seed check, the pre-registration's SHA-256 and each packet's
rendering group. It checks the group against the packet: ``diag-p3`` and ``diag-p4`` change a packet's
rendering only through its deployment summaries, so a D1, D3 or D6 packet must hold one and a D2, D4 or
D5 packet none.
It writes ``packets/<case>.json`` and ``cases.json`` and refuses to overwrite them. No wall-clock value
enters the output, so a second build is byte-identical.
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
from recovery_workbench.contracts import EvidencePacket, sha256_hex
from recovery_workbench.simulator import families
from recovery_workbench.simulator.world import GENERATOR_VERSION, PLANTED_EVIDENCE_ID

import round3_design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

round1 = round3_design.round1_design
round1_build = round3_design.round1_module("build")


def rendering_group(case: round3_design.Case, built: EvidencePacket) -> str:
    """The family's rendering group, after checking the packet agrees with it."""
    group = round3_design.FAMILY_PLANS[case.run.family].rendering
    has_summary = bool(round3_design.deployment_summaries(built))
    if has_summary != (group == round3_design.WITH_SUMMARY):
        raise AssertionError(f"{case.case_id}: {case.run.family} is in rendering group {group!r}, but its packet "
                             f"{'holds' if has_summary else 'holds no'} deployment summary")
    return group


def build_all(cases: Sequence[round3_design.Case], output_dir: Path) -> list[JsonValue]:
    """Build each seed group, clean world first; write its packets and return round 1's case records, extended."""
    stale_after_ms = settings.load_settings().stale_after_seconds * 1_000
    records: list[JsonValue] = []
    with tempfile.TemporaryDirectory(prefix="a_injection_round3_build_") as scratch:
        for twin_id, group in groupby(cases, key=lambda case: case.twin_id):
            worlds = [round1_build.build_world(case, Path(scratch), stale_after_ms) for case in group]
            clean = worlds[0]
            if clean.case.case_id != twin_id:
                raise AssertionError(f"the first case of seed group {twin_id} must be the clean world")
            for world in worlds:
                round1.write_packet(output_dir / round1.packet_file(world.case.case_id), world.built)
                records.append({**round1_build.case_record(world, clean),
                                "rendering_group": rendering_group(world.case, world.built)})
                print(f"{world.case.case_id}: packet {world.built.packet_hash()[:16]}", flush=True)
    return records


def _design_record() -> dict[str, JsonValue]:
    return {
        "split": round3_design.SPLIT, "attack": round3_design.ATTACK, "channel": round3_design.CHANNEL,
        "families": {family: {"worlds": plan.worlds, "planted_twins": plan.planted, "rendering": plan.rendering}
                     for family, plan in round3_design.FAMILY_PLANS.items()},
        "parameter_sets": "the family's manifest runs, TEST then DEV, each in manifest order; the first `worlds` of them",
        "seed_rule": (f"{round3_design.SEED_BASE} + {round3_design.FAMILY_SEED_STEP} x (family number - 1) + "
                      "position (1-based) in that order"),
        "planted_evidence_id": PLANTED_EVIDENCE_ID, "strip_placeholder": round3_design.PLACEHOLDER,
        "arms": {name: {"prompt": arm.code.prompt_version, "validator": arm.code.validator_version,
                        "model_seed": arm.model_seed, "code": arm.code.source, "answers_file": arm.answers_file,
                        "trace_file": arm.trace_file, "conditional": name not in round3_design.CORE_ARMS}
                 for name, arm in round3_design.ARMS.items()},
        "decisions": {decision.name: {"baseline": [decision.baseline.prompt_version, decision.baseline.validator_version],
                                      "candidate": [decision.candidate.prompt_version,
                                                    decision.candidate.validator_version]}
                      for decision in round3_design.DECISIONS},
        "alpha": str(round3_design.ALPHA), "max_not_comparable": round3_design.MAX_NOT_COMPARABLE,
    }


def _require_baseline_code() -> None:
    found = (diagnosis.PROMPT_VERSION, diagnosis.VALIDATOR_VERSION)
    wanted = (round3_design.BASELINE_CODE.prompt_version, round3_design.BASELINE_CODE.validator_version)
    if found != wanted:
        raise SystemExit(f"the build records {wanted[0]} renderings and {wanted[1]} rules checks, but this code reports "
                         f"{found}; build from the baseline code ({round3_design.BASELINE_CODE.source})")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the round-3 planted-instruction packets (run in the test container).")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output_dir: Path = args.output_dir
    if (output_dir / round3_design.CASES_FILE).exists() or (output_dir / round3_design.PACKETS_DIR).exists():
        parser.error(f"{output_dir} already holds a build; use an empty output directory")
    _require_baseline_code()
    preregistration = round3_design.preregistration_record()  # before any world: no build without it
    manifest = families.load_manifest()
    cases = round3_design.round3_cases(manifest)
    seed_check = round3_design.fresh_seed_report(cases, manifest, round3_design.built_seeds(round3_design.ROUND1_CASES_PATH),
                                                 round3_design.built_seeds(round3_design.ROUND2_CASES_PATH))
    records = build_all(cases, output_dir)
    round1.write_new_json(output_dir / round3_design.CASES_FILE, {
        "experiment": ("Planted instruction, round 3 (Q20), on fresh seeds: a chain of four codes. The fence "
                       "(diag-p3 against diag-p2), the validator (val-6 against val-5), and strip instead of fence "
                       "(diag-p4 against diag-p3)"),
        "notice": ("Built in the test container from the baseline code. expected, faulting_deploy_id, "
                   "harmless_deploy_ids and planted are oracle extracts used only for scoring; the arms send the "
                   "packet files alone to the model. prompt_sha256 is the diag-p2 rendering; versions.validator "
                   "checked the rules answers at build time; each arm's code is recorded in its answers file."),
        "preregistration": preregistration,
        "preregistration_draft": round3_design.preregistration_is_draft(),
        "versions": {"workbench": __version__, "world_generator": GENERATOR_VERSION, "packet": packet.PACKET_VERSION,
                     "rules": rules.RULES_VERSION, "validator": diagnosis.VALIDATOR_VERSION,
                     "prompt": diagnosis.PROMPT_VERSION, "evaluation_manifest": manifest.manifest_version,
                     "system_prompt_sha256": sha256_hex(diagnosis.SYSTEM_PROMPT.encode("utf-8"))},
        "design": _design_record(),
        "fresh_seed_check": seed_check,
        "cases": records,
    })
    print(f"wrote {len(records)} cases to {output_dir}; pre-registration sha256 {preregistration['sha256']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
