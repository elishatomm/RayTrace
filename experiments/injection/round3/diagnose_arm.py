"""Stage 2 of round 3, on the host with the live local model: diagnose every round-3 packet under one arm.

    git archive 227e806 | tar -x -C <scratch>/a_p9_export_227e806     # the arm's code, exactly as committed
    OLLAMA_HOST=127.0.0.1:11434 ollama serve &                         # loopback only; stopped right after the stage
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<scratch>/a_p9_export_227e806/src .venv/bin/python \\
        experiments/injection/round3/diagnose_arm.py --arm diag-p2_val-5_20261111 \\
        --code-export <scratch>/a_p9_export_227e806

An arm is one code under test (a prompt and a validator) at one model seed: the eight arms of the chain,
or a conditional confirmation arm. Before any model call the stage checks that:

* the package was imported from the export, and it reports the arm's prompt and validator versions;
* PREREGISTRATION.md is the file the build hashed, and it is no longer a draft;
* every dated addendum is recorded in ``addenda.json`` with its SHA-256;
* every packet still has its build-time hash; the code renders the build's (``diag-p2``) system prompt;
  it renders the build's user message wherever the arm's prompt is the build's, or the packet holds no
  deployment summary; and under ``diag-p4`` it renders each planted case exactly as its clean twin. An
  export that fails one of these would fail validity check 2, 4 or 6 whatever the model answered, so it
  is stopped here, before it spends a model call;
* the frozen M4 settings match the code and the model digest, with the arm's prompt, validator and model
  seed as the only differences; no model is ever pulled.

Round 1's diagnose loop then does the work (``experiments/injection/diagnose.py``): the model is unloaded
first, so the first case is a cold start, as in M4, and every answer is live inference. Each answer then
gets its run record: the model, its digest, the full generation options and seed, and the SHA-256 of the
first request (messages plus response schema) and of its schema alone. Writes ``answers_<arm>.json`` and
``traces/<arm>.jsonl`` and never overwrites them. It opens no database.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

import recovery_workbench
from recovery_workbench import diagnosis, packet
from recovery_workbench.contracts import canonical_json, sha256_hex
from recovery_workbench.evaluation import run as evaluation_run
from recovery_workbench.evaluation.environment import describe_environment

import round3_design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

round1 = round3_design.round1_design
round1_diagnose = round3_design.round1_module("diagnose")
round2_diagnose_arm = round3_design.round2_module("diagnose_arm")


def code_under_test(arm: round3_design.Arm, code_export: Path) -> dict[str, JsonValue]:
    """The package this process diagnoses with, after checking it is the arm's: imported from the export, right versions."""
    package_dir = Path(str(recovery_workbench.__file__)).resolve().parent
    exported = (code_export / "src" / "recovery_workbench").resolve()
    if package_dir != exported:
        raise SystemExit(f"recovery_workbench was imported from {package_dir}, not from the export {exported}; "
                         f"run with PYTHONPATH={code_export / 'src'}")
    found = {"prompt": diagnosis.PROMPT_VERSION, "validator": diagnosis.VALIDATOR_VERSION}
    wanted = {"prompt": arm.code.prompt_version, "validator": arm.code.validator_version}
    if found != wanted:
        raise SystemExit(f"the {arm.name} arm needs {wanted}, but the code under test reports {found}; refusing to run")
    return {
        "source": arm.code.source, "code_export": str(code_export), "package_dir": str(package_dir),
        "prompt_version": diagnosis.PROMPT_VERSION, "validator_version": diagnosis.VALIDATOR_VERSION,
        "diagnosis_py_sha256": sha256_hex((package_dir / "diagnosis.py").read_bytes()),
        "package_sources_sha256": round2_diagnose_arm.package_sources_sha256(package_dir),
    }


def frozen_settings_for(arm: round3_design.Arm, frozen: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The frozen M4 settings with the arm's prompt, validator and model seed, the only settings the arms differ in."""
    model: Mapping[str, JsonValue] = frozen["model"]  # type: ignore[assignment]
    options: Mapping[str, JsonValue] = model["options"]  # type: ignore[assignment]
    versions: Mapping[str, JsonValue] = frozen["versions"]  # type: ignore[assignment]
    return {**frozen,
            "model": {**model, "prompt_version": arm.code.prompt_version, "options": {**options, "seed": arm.model_seed}},
            "versions": {**versions, "validator": arm.code.validator_version}}


def arm_client(arm: round3_design.Arm, frozen: Mapping[str, JsonValue]) -> diagnosis.OllamaClient:
    """A client with the arm's settings, after checking the code matches them and the model digest (round 1's checks)."""
    settings = frozen_settings_for(arm, frozen)
    model: dict[str, JsonValue] = settings["model"]  # type: ignore[assignment]
    versions: dict[str, JsonValue] = settings["versions"]  # type: ignore[assignment]
    frozen_options: dict[str, JsonValue] = frozen["model"]["options"]  # type: ignore[index, assignment]
    checks: dict[str, tuple[JsonValue, JsonValue]] = {
        "chosen_think": (settings["chosen_think"], "off"),
        "think": (model["think"], False),
        "prompt_version": (model["prompt_version"], diagnosis.PROMPT_VERSION),
        "validator": (versions["validator"], diagnosis.VALIDATOR_VERSION),
        "packet": (versions["packet"], packet.PACKET_VERSION),
        "deadline_s": (model["deadline_s"], evaluation_run.DEADLINE_S),
        "max_repairs": (model["max_repairs"], 1),
        "frozen_seed": (frozen_options["seed"], diagnosis.DEFAULT_OPTIONS["seed"]),
        "options": (model["options"], {**diagnosis.DEFAULT_OPTIONS, **evaluation_run.QWEN_SAMPLING, "seed": arm.model_seed}),
    }
    drifted = {name: {"settings": pair[0], "code": pair[1]} for name, pair in checks.items() if pair[0] != pair[1]}
    if drifted or arm.model_seed not in round3_design.MODEL_SEEDS:
        raise SystemExit(f"the code no longer matches {round1.FROZEN_SETTINGS_PATH} for the {arm.name} arm: {drifted}; "
                         "refusing to run")
    client = diagnosis.OllamaClient(model=str(model["model"]), options=model["options"],  # type: ignore[arg-type]
                                    keep_alive=str(model["keep_alive"]))
    digest = client.model_digest()
    if digest != model["model_digest"]:
        client.close()
        raise SystemExit(f"{model['model']} has digest {digest}, not the frozen {model['model_digest']}; "
                         "refusing to run (never pull a model for this stage)")
    return client


def arm_cases(cases_document: Mapping[str, JsonValue], arm: round3_design.Arm,
              experiment_dir: Path) -> tuple[list[dict[str, JsonValue]], dict[str, JsonValue]]:
    """The cases for round 1's diagnose loop, each with the prompt this code renders, and those renderings' SHA-256.

    Refuses a changed packet, a system prompt other than the build's, and a user message other than the
    build's where the two must agree (see the module docstring).
    """
    versions: Mapping[str, JsonValue] = cases_document["versions"]  # type: ignore[assignment]
    if sha256_hex(diagnosis.SYSTEM_PROMPT.encode("utf-8")) != versions["system_prompt_sha256"]:
        raise SystemExit(f"the code under test renders a system prompt other than the build's {versions['prompt']}; "
                         "the validity checks need it unchanged, so no model call is made")
    same_prompt_as_build = arm.code.prompt_version == versions["prompt"]
    cases: list[Mapping[str, JsonValue]] = cases_document["cases"]  # type: ignore[assignment]
    prepared: list[dict[str, JsonValue]] = []
    rendered: dict[str, JsonValue] = {}
    for case in cases:
        built = round1.read_packet(experiment_dir / str(case["packet_file"]))
        if built.packet_hash() != case["packet_hash"]:
            raise SystemExit(f"{case['case_id']}: the packet file changed after the build (hash {built.packet_hash()})")
        prompt_sha256 = sha256_hex(diagnosis.render_packet(built).encode("utf-8"))
        must_match = same_prompt_as_build or case["rendering_group"] == round3_design.WITHOUT_SUMMARY
        if must_match and prompt_sha256 != case["prompt_sha256"]:
            raise SystemExit(f"{case['case_id']}: the code under test renders a user message other than the build's "
                             f"{versions['prompt']} one, where the two must agree; no model call is made")
        rendered[str(case["case_id"])] = prompt_sha256
        prepared.append({**case, "prompt_sha256": prompt_sha256})
    if arm.code.prompt_version == round3_design.STRIP_PROMPT:
        _require_planted_as_twin(cases, rendered)
    return prepared, rendered


def _require_planted_as_twin(cases: Sequence[Mapping[str, JsonValue]], rendered: Mapping[str, JsonValue]) -> None:
    """Strip removes every copy of the summary, and a planted world differs from its twin only there."""
    unlike = [str(case["case_id"]) for case in cases
              if case["attack"] is not None and rendered[str(case["case_id"])] != rendered[str(case["twin_id"])]]
    if unlike:
        raise SystemExit(f"the code under test renders {unlike} unlike their clean twins, so it does not remove the note; "
                         "validity check 6 needs them identical, so no model call is made")


def run_records(cases: Sequence[Mapping[str, JsonValue]], experiment_dir: Path, trace_path: Path,
                model: Mapping[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    """Each run's record: the model, digest, generation options and seed, and the hashes of its first request.

    The request is rebuilt with the code under test's own builders, the ones ``diagnose`` calls, and kept
    only when its messages are the trace's first call, so a hash always describes what was sent. A run whose
    first call differs, or never happened, gets no request hash, and every equality check then fails for it.
    """
    with trace_path.open(encoding="utf-8") as handle:
        first_sent = {str(line["case_id"]): line["calls"][0]["messages"] if line["calls"] else None
                      for line in map(json.loads, handle) if line}
    records: dict[str, dict[str, JsonValue]] = {}
    for case in cases:
        case_id = str(case["case_id"])
        built = round1.read_packet(experiment_dir / str(case["packet_file"]))
        messages = [message.model_dump() for message in diagnosis.initial_messages(built)]
        schema = diagnosis.build_response_schema(built)
        sent_as_built = first_sent.get(case_id) == messages
        records[case_id] = {
            "model": model["model"], "model_digest": model["model_digest"], "options": model["options"],
            "seed": model["options"]["seed"], "think": model["think"],  # type: ignore[index, call-overload]
            "first_request_sha256": sha256_hex(canonical_json({"messages": messages, "format": schema}))
            if sent_as_built else None,
            "response_schema_sha256": sha256_hex(canonical_json(schema)),
            "first_call_sent_as_built": sent_as_built,
        }
    return records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose the round-3 packets with the live local model, one arm.")
    parser.add_argument("--arm", required=True, help="an arm of the chain, or a confirmation arm the adoption rules call for")
    parser.add_argument("--code-export", type=Path, required=True, help="the git archive export whose src/ is on PYTHONPATH")
    parser.add_argument("--experiment-dir", type=Path, default=round3_design.ROUND3_DIR)
    args = parser.parse_args(argv)
    arm = round3_design.arm_named(args.arm)
    experiment_dir: Path = args.experiment_dir
    code = code_under_test(arm, args.code_export.resolve())
    answers_path, trace_path = experiment_dir / arm.answers_file, experiment_dir / arm.trace_file
    if answers_path.exists() or trace_path.exists():
        parser.error(f"{answers_path} or {trace_path} exists; live answers are never overwritten (move both away to run again)")
    cases_path = experiment_dir / round3_design.CASES_FILE
    cases_document = round1.read_json(cases_path)
    preregistration = round3_design.checked_final_preregistration(cases_document, experiment_dir)
    addenda = round3_design.checked_addenda(experiment_dir)
    cases, rendered = arm_cases(cases_document, arm, experiment_dir)
    frozen_path: Path = round1.FROZEN_SETTINGS_PATH
    client = arm_client(arm, round1.read_json(frozen_path))
    try:
        client.unload()  # the first case starts cold, as in M4 and rounds 1 and 2
        started = datetime.now(UTC).isoformat()
        answers = round1_diagnose.diagnose_all(client, cases, experiment_dir, trace_path)
        environment = describe_environment(client, think=False, deadline_s=evaluation_run.DEADLINE_S, systems=("model",))
    finally:
        client.close()
    model: Mapping[str, JsonValue] = environment["model"]  # type: ignore[assignment]
    records = run_records(cases, experiment_dir, trace_path, model)
    answers = [answer | {"run_record": records[str(answer["case_id"])]} for answer in answers]  # type: ignore[operator]
    round1.write_new_json(answers_path, {
        "notice": "Live local inference against Ollama on 127.0.0.1; nothing here is scripted or replayed.",
        "arm": arm.name, "model_seed": arm.model_seed, "code_under_test": code,
        "preregistration": preregistration, "addenda": addenda,
        "frozen_settings": str(frozen_path.relative_to(round1.REPOSITORY_DIR)),
        "frozen_settings_sha256": sha256_hex(frozen_path.read_bytes()),
        "cases_sha256": sha256_hex(cases_path.read_bytes()), "rendered_prompt_sha256": rendered,
        "started_wall_utc": started, "finished_wall_utc": datetime.now(UTC).isoformat(),
        "environment": environment, "answers": answers,
    })
    print(f"wrote {len(answers)} answers to {answers_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
