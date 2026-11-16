"""Generate the frozen evaluation runs of one split.

    python -m recovery_workbench.evaluation.generate --split dev|test --data-dir DIR [--manifest PATH]

Seeds every manifest run of the split into ``<data_dir>/workbench.sqlite``. Each run's oracle goes
to ``<data_dir>/oracle/`` and its provider records to ``<data_dir>/provider/``. Writes
``<data_dir>/data_manifest.json`` with versions, seeds, sizes, the split and the hashes of the
observable data and of each oracle. Generation only: no diagnosis runs here. The exit code is 1
if any run fails to open an incident.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import __version__, settings, storage
from recovery_workbench.contracts import ModelMode, sha256_hex
from recovery_workbench.simulator import driver, families
from recovery_workbench.simulator.world import generate_world

DATA_MANIFEST_VERSION = 1
DATABASE_FILE = "workbench.sqlite"
DATA_MANIFEST_FILE = "data_manifest.json"
SIZE_TABLES = ("orders", "payment_attempts", "payment_observations", "webhook_inbox", "delivery_attempts",
               "worker_attempts", "deployments", "merchant_logs", "incidents")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recovery_workbench.evaluation.generate",
                                     description="Generate the evaluation runs of one split (synthetic only).")
    parser.add_argument("--split", required=True, choices=("dev", "test"))
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=families.MANIFEST_PATH)
    args = parser.parse_args(argv)
    report = generate_split(args.split, args.data_dir, manifest_path=args.manifest)
    print(json.dumps({key: report[key] for key in ("split", "runs", "runs_without_incident", "data_manifest")},
                     indent=2, sort_keys=True))
    return 0 if not report["runs_without_incident"] else 1


def generate_split(split: str, data_dir: Path, *, manifest_path: Path = families.MANIFEST_PATH) -> dict[str, JsonValue]:
    """Seed every run of ``split`` and write ``data_manifest.json``; returns the manifest content."""
    manifest_bytes = manifest_path.read_bytes()
    manifest = families.EvaluationManifest.model_validate(json.loads(manifest_bytes))
    runs = [run for run in manifest.runs if run.split == split]
    if not runs:
        raise ValueError(f"{manifest_path} has no {split!r} runs")
    stale_after_ms = settings.load_settings().stale_after_seconds * 1_000
    data_dir.mkdir(parents=True, exist_ok=True)
    entries: list[JsonValue] = []
    with closing(storage.connect(data_dir / DATABASE_FILE)) as conn:
        for run in runs:
            world = generate_world(families.spec_for(run), run.seed)
            # The run's recorded mode is fixed so the observable hash does not depend on local settings;
            # the evaluation runs both systems on every run regardless.
            summary = driver.seed_world(
                conn, world, oracle_dir=data_dir / "oracle", provider_dir=data_dir / "provider",
                model_mode=ModelMode.RULES, stale_after_ms=stale_after_ms,
            )
            entries.append({
                "run_id": summary.run_id,
                "seed": run.seed,
                "split": split,
                "cutoff_ms": summary.cutoff_ms,
                "sizes": {table: summary.counts[table] for table in SIZE_TABLES},
                "incident_ids": list(summary.incident_ids),
                "observable_sha256": summary.observable_sha256,
                "oracle_sha256": summary.oracle_sha256,
                "oracle_file": str(Path(str(summary.oracle_path)).relative_to(data_dir)),
                "provider_records_file": str(Path(str(summary.provider_records_path)).relative_to(data_dir)),
            })
    content: dict[str, JsonValue] = {
        "data_manifest_version": DATA_MANIFEST_VERSION,
        "split": split,
        "evaluation_manifest_version": manifest.manifest_version,
        "evaluation_manifest_sha256": sha256_hex(manifest_bytes),
        "family_generator_version": manifest.generator_version,
        "world_generator_version": driver.GENERATOR_VERSION,
        "workbench_version": __version__,
        "stale_after_ms": stale_after_ms,
        "runs": entries,
    }
    path = data_dir / DATA_MANIFEST_FILE
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(content, indent=2, sort_keys=True) + "\n")
    content["data_manifest"] = str(path)
    content["runs_without_incident"] = [
        entry["run_id"] for entry in entries if not entry["incident_ids"]  # type: ignore[index]
    ]
    return content


if __name__ == "__main__":
    sys.exit(main())
