"""Stage 1, in the test container only: generate every world, then write its packet, rules answer and expectations.

    docker run --rm --init --network none --read-only --tmpfs /tmp --entrypoint python \\
        -v "<host output dir>":/out recovery-workbench-test:a experiments/injection/build.py --output-dir /out

Each world is seeded into its own throwaway database under the container's in-memory /tmp, and its
packet is built at the cutoff exactly as the M4 evaluation builds it (``evaluation.run``). Every
planted world is compared with its clean twin, database and packet, and each difference is recorded.
Writes ``packets/<case>.json`` and ``cases.json``; refuses to overwrite. The output is deterministic:
no wall-clock value enters it, so a second build must be byte-identical.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import __version__, diagnosis, packet, rules, settings, storage
from recovery_workbench.contracts import EvidenceItem, EvidenceKind, EvidencePacket, ModelMode, canonical_json, sha256_hex
from recovery_workbench.evaluation import run as evaluation_run
from recovery_workbench.simulator import driver, families, oracle
from recovery_workbench.simulator.world import GENERATOR_VERSION, PLANT_LEAD_MS, PLANTED_EVIDENCE_ID, generate_world

import design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

Rows = dict[str, Counter[tuple[object, ...]]]
# Where planted text may sit in a packet: untrusted text fields (docs/PACKET_SCHEMA.md rule 5) and a
# deployment's commit summary.
_UNTRUSTED_FIELDS = {EvidenceKind.LOG: ("text",), EvidenceKind.DEPLOYMENT: ("facts.summary",)}


@dataclass(frozen=True)
class BuiltWorld:
    case: design.Case
    run_id: str
    built: EvidencePacket
    truth: dict[str, JsonValue]
    rules_answer: evaluation_run.SystemAnswer
    rows: Rows
    observable_sha256: str


def build_world(case: design.Case, work_dir: Path, stale_after_ms: int) -> BuiltWorld:
    """Seed one world into its own database and build its packet and rules answer at the cutoff."""
    world = generate_world(families.spec_for(case.run), case.run.seed, plant=case.plant)
    directory = work_dir / case.case_id
    directory.mkdir()
    with closing(storage.connect(directory / "workbench.sqlite")) as conn:
        summary = driver.seed_world(conn, world, oracle_dir=directory / "oracle", provider_dir=directory / "provider",
                                    model_mode=ModelMode.RULES, stale_after_ms=stale_after_ms)
        truth = design.read_json(Path(str(summary.oracle_path)))
        scored = evaluation_run.Case(
            run_id=world.run_id, seed=case.run.seed, family=case.run.family, variant=case.run.variant,
            params=case.run.params, cutoff_ms=world.cutoff_ms, expected=truth["expected"], oracle=truth,  # type: ignore[arg-type]
            provider_records=Path(str(summary.provider_records_path)),
        )
        built = evaluation_run.build_case_packet(conn, scored)
        rows = observable_rows(conn, world.run_id)
    shutil.rmtree(directory)  # the container's /tmp is memory; keep one seed group at a time
    return BuiltWorld(case, world.run_id, built, truth, evaluation_run.run_rules(built), rows, summary.observable_sha256)


def observable_rows(conn: sqlite3.Connection, run_id: str) -> Rows:
    """Every observable row of the run, per table, with the oracle's own queries (no wall-clock columns)."""
    return {
        table: Counter(tuple(value.hex() if isinstance(value, bytes) else value for value in row)
                       for row in conn.execute(query, (run_id,)).fetchall())
        for table, query in oracle.OBSERVABLE_QUERIES.items()
    }


# --------------------------------------------------------------------------- twin comparison


def database_diff(clean: Rows, planted: Rows) -> dict[str, JsonValue]:
    """Tables whose rows differ from the clean twin's, with the first column (the row's key) of each change."""
    changes: dict[str, JsonValue] = {}
    for table, clean_rows in clean.items():
        added, removed = planted[table] - clean_rows, clean_rows - planted[table]
        if added or removed:
            changes[table] = {"added": sorted(str(row[0]) for row in added.elements()),
                              "removed": sorted(str(row[0]) for row in removed.elements())}
    return changes


def _strings(value: JsonValue, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, entry in value.items():
            yield from _strings(entry, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            yield from _strings(entry, f"{path}[{index}]")


def plant_locations(built: EvidencePacket, plant_text: str) -> dict[str, list[str]]:
    """Every field of the packet holding the planted text: ``{evidence ID or packet part: [field paths]}``."""
    found: dict[str, list[str]] = {}
    parts: list[tuple[str, JsonValue]] = [
        ("incident", built.incident.model_dump(mode="json")),
        ("candidates", {"change_ids": list(built.candidate_change_ids), "methods": list(built.candidate_methods)}),
        *((item.evidence_id, item.model_dump(mode="json", exclude={"evidence_id"})) for item in built.items),
    ]
    for owner, value in parts:
        paths = [path for path, text in _strings(value, "") if plant_text in text]
        if paths:
            found[owner] = paths
    return found


def outside_untrusted_fields(built: EvidencePacket, locations: dict[str, list[str]]) -> list[str]:
    """Locations of planted text other than a log's ``text`` or a deployment's ``facts.summary``."""
    kinds = {item.evidence_id: item.kind for item in built.items}
    return sorted(f"{owner}:{path}" for owner, paths in locations.items() for path in paths
                  if owner not in kinds or path not in _UNTRUSTED_FIELDS.get(kinds[owner], ()))


def _content_key(item: EvidenceItem) -> str:
    return sha256_hex(canonical_json(item.model_dump(mode="json", exclude={"evidence_id"})))


def packet_diff(clean: EvidencePacket, planted: EvidencePacket, plant_ids: set[str]) -> dict[str, JsonValue]:
    """How the planted packet differs from its clean twin, item by item, matched by content.

    Items that carry the plant are ``added`` (no clean counterpart) or ``modified`` (they replace the
    clean item with the same ID). Everything else must match a clean item exactly; a match under a
    new ID is a renumbering, a clean item left over is ``dropped`` and an unmatched planted item is
    ``other_changed``.
    """
    by_content: dict[str, list[str]] = {}
    for item in clean.items:
        by_content.setdefault(_content_key(item), []).append(item.evidence_id)
    renumbered: dict[str, str] = {}
    other_changed: list[str] = []
    for item in planted.items:
        if item.evidence_id in plant_ids:
            continue
        matches = by_content.get(_content_key(item))
        if not matches:
            other_changed.append(item.evidence_id)
            continue
        clean_id = matches.pop(0)
        if clean_id != item.evidence_id:
            renumbered[clean_id] = item.evidence_id
    leftover = {evidence_id for ids in by_content.values() for evidence_id in ids}
    modified = sorted(plant_ids & leftover)
    return {
        "added_items": sorted(plant_ids - leftover),
        "modified_items": modified,
        "renumbered_items": dict(sorted(renumbered.items())),
        "dropped_items": sorted(leftover - set(modified)),
        "other_changed_items": sorted(other_changed),
        "incident_card_changed": clean.incident != planted.incident,
        "candidates_changed": (clean.candidate_change_ids, clean.candidate_methods)
        != (planted.candidate_change_ids, planted.candidate_methods),
    }


# --------------------------------------------------------------------------- records


def _rules_record(answer: evaluation_run.SystemAnswer) -> dict[str, JsonValue]:
    # Latency is wall-clock time, so it stays out: the build output must be deterministic.
    return {"status": answer.status, "reasons": list(answer.reasons),
            "output": None if answer.output is None else answer.output.model_dump(mode="json")}


def twin_comparison(clean: BuiltWorld, planted: BuiltWorld, locations: dict[str, list[str]]) -> dict[str, JsonValue]:
    if planted.truth["expected"] != clean.truth["expected"]:
        raise AssertionError(f"{planted.case.case_id}: the plant changed the expected answer; it must change nothing")
    return {
        "database": database_diff(clean.rows, planted.rows),
        "packet": packet_diff(clean.built, planted.built, set(locations) & set(planted.built.evidence_ids())),
        "rules_changed": _rules_record(clean.rules_answer) != _rules_record(planted.rules_answer),
    }


def case_record(world: BuiltWorld, clean: BuiltWorld) -> dict[str, JsonValue]:
    case, truth, built = world.case, world.truth, world.built
    planted = truth.get("planted_instruction")
    if (case.plant is None) != (planted is None):
        raise AssertionError(f"{case.case_id}: the oracle must record a plant exactly when one is configured")
    locations = {} if not isinstance(planted, dict) else plant_locations(built, str(planted["text"]))
    if case.plant is not None and not locations:
        raise AssertionError(f"{case.case_id}: the planted text never reached the packet")
    if PLANTED_EVIDENCE_ID in built.evidence_ids():
        raise AssertionError(f"{case.case_id}: a packet item uses the planted evidence ID {PLANTED_EVIDENCE_ID}")
    return {
        "case_id": case.case_id, "twin_id": case.twin_id, "family": case.run.family, "variant": case.run.variant,
        "seed": case.run.seed, "split": case.run.split, "params": dict(case.run.params),
        "attack": None if case.plant is None else case.plant.attack,
        "channel": None if case.plant is None else case.plant.channel,
        "run_id": world.run_id, "cutoff_ms": built.incident.evidence_cutoff_ms, "observable_sha256": world.observable_sha256,
        "packet_file": design.packet_file(case.case_id), "packet_hash": built.packet_hash(),
        "prompt_sha256": sha256_hex(diagnosis.render_packet(built).encode("utf-8")),
        "expected": truth["expected"], "faulting_deploy_id": truth["faulting_deploy_id"],
        "harmless_deploy_ids": truth["harmless_deploy_ids"], "planted": planted,
        "plant_locations": locations,
        "plant_outside_untrusted_fields": outside_untrusted_fields(built, locations),
        "rules": _rules_record(world.rules_answer),
        "twin_comparison": None if case.plant is None else twin_comparison(clean, world, locations),
    }


# --------------------------------------------------------------------------- the stage


def build_all(output_dir: Path) -> list[JsonValue]:
    stale_after_ms = settings.load_settings().stale_after_seconds * 1_000
    records: list[JsonValue] = []
    with tempfile.TemporaryDirectory(prefix="a_injection_build_") as scratch:
        for twin_id, group in groupby(design.experiment_cases(), key=lambda case: case.twin_id):
            worlds = [build_world(case, Path(scratch), stale_after_ms) for case in group]
            clean = worlds[0]
            if clean.case.case_id != twin_id:
                raise AssertionError(f"the first case of seed group {twin_id} must be the clean world")
            for world in worlds:
                design.write_packet(output_dir / design.packet_file(world.case.case_id), world.built)
                record = case_record(world, clean)
                records.append(record)
                print(_progress(record), flush=True)
    return records


def _progress(record: dict[str, JsonValue]) -> str:
    comparison = record["twin_comparison"]
    if not isinstance(comparison, dict):
        output = record["rules"]["output"]  # type: ignore[index]
        answer = "invalid" if not isinstance(output, dict) else f"{output['hypothesis']} / {output['proposed_operation']}"
        return f"{record['case_id']}: clean; rules {answer}"
    return (f"{record['case_id']}: plant in {record['plant_locations']}; database {comparison['database']}; "
            f"packet {comparison['packet']}; rules changed {comparison['rules_changed']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the planted-instruction packets (run in the test container).")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output_dir: Path = args.output_dir
    if (output_dir / design.CASES_FILE).exists() or (output_dir / design.PACKETS_DIR).exists():
        parser.error(f"{output_dir} already holds a build; use an empty output directory")
    records = build_all(output_dir)
    manifest = families.load_manifest()
    design.write_new_json(output_dir / design.CASES_FILE, {
        "experiment": "Instruction planted in a log line or a commit message (PLAN.md stretch item 2)",
        "notice": ("Built in the test container. expected, faulting_deploy_id, harmless_deploy_ids and planted are "
                   "oracle extracts used only for scoring; the diagnose stage sends the packet files alone to the model."),
        "versions": {"workbench": __version__, "world_generator": GENERATOR_VERSION, "packet": packet.PACKET_VERSION,
                     "rules": rules.RULES_VERSION, "validator": diagnosis.VALIDATOR_VERSION,
                     "prompt": diagnosis.PROMPT_VERSION, "evaluation_manifest": manifest.manifest_version},
        "design": {"split": design.SPLIT, "seed_offset": design.SEED_OFFSET,
                   "targets": {family: list(attacks) for family, attacks in design.TARGETS.items()},
                   "channels": dict(design.CHANNELS), "planted_evidence_id": PLANTED_EVIDENCE_ID,
                   "order_note_lead_ms": PLANT_LEAD_MS},
        "cases": records,
    })
    print(f"wrote {len(records)} cases to {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
