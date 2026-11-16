"""Stage 2 of round 2, on the host with the live local model: diagnose every round-2 packet under one validator arm.

    git archive d5b7933 | tar -x -C <scratch>/a_export_d5b7933       # the val-4 code, exactly as committed
    OLLAMA_HOST=127.0.0.1:11434 ollama serve &                       # loopback only; stopped right after the stage
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<scratch>/a_export_d5b7933/src .venv/bin/python \\
        experiments/injection/round2/diagnose_arm.py --arm val-4 --code-export <scratch>/a_export_d5b7933

The code under test is the export's ``recovery_workbench``. Before any model call, the stage checks that
the package was imported from that export and that it reports the arm's validator version and prompt
``diag-p2``, that PREREGISTRATION.md is the file the build hashed, and that every dated addendum is
recorded in ``addenda.json`` with its SHA-256. Round 1's diagnose stage then does the work
(``experiments/injection/diagnose.py``):

* the client is built from ``evaluation/frozen_settings.json`` once the code and the model digest match
  it, so any drift stops the stage and no model is ever pulled;
* the model is unloaded first, so the first case is a cold start, as in M4;
* every packet must still have its build-time hash and rendered prompt;
* every answer is live inference.

Writes ``answers_<arm>.json`` and ``traces/<arm>.jsonl`` and never overwrites them. It opens no database.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

import recovery_workbench
from recovery_workbench import diagnosis
from recovery_workbench.contracts import sha256_hex
from recovery_workbench.evaluation import run as evaluation_run
from recovery_workbench.evaluation.environment import describe_environment

import round2_design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

round1 = round2_design.round1_design
round1_diagnose = round2_design.round1_module("diagnose")


def code_under_test(arm: round2_design.Arm, code_export: Path) -> dict[str, JsonValue]:
    """The package this process diagnoses with, after checking it is the arm's: imported from the export, right versions."""
    package_dir = Path(str(recovery_workbench.__file__)).resolve().parent
    exported = (code_export / "src" / "recovery_workbench").resolve()
    if package_dir != exported:
        raise SystemExit(f"recovery_workbench was imported from {package_dir}, not from the export {exported}; "
                         f"run with PYTHONPATH={code_export / 'src'}")
    found = {"validator": diagnosis.VALIDATOR_VERSION, "prompt": diagnosis.PROMPT_VERSION}
    wanted = {"validator": arm.validator_version, "prompt": round2_design.PROMPT_VERSION}
    if found != wanted:
        raise SystemExit(f"the {arm.name} arm needs {wanted}, but the code under test reports {found}; refusing to run")
    return {
        "source": arm.code_source, "code_export": str(code_export), "package_dir": str(package_dir),
        "validator_version": diagnosis.VALIDATOR_VERSION, "prompt_version": diagnosis.PROMPT_VERSION,
        "diagnosis_py_sha256": sha256_hex((package_dir / "diagnosis.py").read_bytes()),
        "package_sources_sha256": package_sources_sha256(package_dir),
    }


def package_sources_sha256(package_dir: Path) -> str:
    """SHA-256 of the lines ``"<posix path relative to the package> <file SHA-256>\\n"`` for every .py file, by path.

    It lets anyone compare the code that ran with a commit:
    ``find . -name '*.py' | sed 's|^\\./||' | LC_ALL=C sort`` in the exported package, hashed the same way.
    """
    relative = sorted(path.relative_to(package_dir).as_posix() for path in package_dir.rglob("*.py"))
    lines = "".join(f"{name} {sha256_hex((package_dir / name).read_bytes())}\n" for name in relative)
    return sha256_hex(lines.encode("utf-8"))


def frozen_settings_for(arm: round2_design.Arm, frozen: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The frozen M4 settings with the arm's validator, the one setting the arms differ in.

    Round 1's ``frozen_client`` compares every other frozen setting with the code unchanged. For the
    val-4 arm the result equals the frozen file.
    """
    versions: Mapping[str, JsonValue] = frozen["versions"]  # type: ignore[assignment]
    return {**frozen, "versions": {**versions, "validator": arm.validator_version}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose the round-2 packets with the live local model, one validator arm.")
    parser.add_argument("--arm", required=True, choices=sorted(round2_design.ARMS))
    parser.add_argument("--code-export", type=Path, required=True, help="the git archive export whose src/ is on PYTHONPATH")
    parser.add_argument("--experiment-dir", type=Path, default=round2_design.ROUND2_DIR)
    args = parser.parse_args(argv)
    arm = round2_design.ARMS[args.arm]
    experiment_dir: Path = args.experiment_dir
    code = code_under_test(arm, args.code_export.resolve())
    answers_path, trace_path = experiment_dir / arm.answers_file, experiment_dir / arm.trace_file
    if answers_path.exists() or trace_path.exists():
        parser.error(f"{answers_path} or {trace_path} exists; live answers are never overwritten (move both away to run again)")
    cases_path = experiment_dir / round2_design.CASES_FILE
    cases_document = round1.read_json(cases_path)
    preregistration = round2_design.checked_preregistration(cases_document, experiment_dir)
    addenda = round2_design.checked_addenda(experiment_dir)
    cases: list[Mapping[str, JsonValue]] = cases_document["cases"]  # type: ignore[assignment]
    frozen_path: Path = round1.FROZEN_SETTINGS_PATH
    client = round1_diagnose.frozen_client(frozen_settings_for(arm, round1.read_json(frozen_path)))
    try:
        client.unload()  # the first case starts cold, as in M4 and round 1
        started = datetime.now(UTC).isoformat()
        answers = round1_diagnose.diagnose_all(client, cases, experiment_dir, trace_path)
        environment = describe_environment(client, think=False, deadline_s=evaluation_run.DEADLINE_S, systems=("model",))
    finally:
        client.close()
    round1.write_new_json(answers_path, {
        "notice": "Live local inference against Ollama on 127.0.0.1; nothing here is scripted or replayed.",
        "arm": arm.name, "code_under_test": code, "preregistration": preregistration, "addenda": addenda,
        "frozen_settings": str(frozen_path.relative_to(round1.REPOSITORY_DIR)),
        "frozen_settings_sha256": sha256_hex(frozen_path.read_bytes()),
        "cases_sha256": sha256_hex(cases_path.read_bytes()),
        "started_wall_utc": started, "finished_wall_utc": datetime.now(UTC).isoformat(),
        "environment": environment, "answers": answers,
    })
    print(f"wrote {len(answers)} answers to {answers_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
