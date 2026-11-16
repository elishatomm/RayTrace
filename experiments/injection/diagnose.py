"""Stage 2, on the host with a live local model: diagnose every built packet with the frozen M4 settings.

    ollama serve                                      # 127.0.0.1 only; started for this stage, stopped after it
    .venv/bin/python experiments/injection/diagnose.py

Reads ``cases.json`` and ``packets/*.json``; writes ``answers.json`` and ``traces/live.jsonl``. It opens
no database. The settings come from ``evaluation/frozen_settings.json`` and are checked against the
code before the first call: the frozen model and digest, thinking off, prompt ``diag-p2``, validator
``val-4``, the frozen options and deadline, at most one repair, policies enforced. Each packet must
still have the hash and the rendered prompt recorded at build time. The model is unloaded first, so
the first case is a cold start, as in M4. Every answer is live inference: nothing is replayed.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import diagnosis, packet
from recovery_workbench.contracts import DiagnosisOutput, EvidencePacket, sha256_hex
from recovery_workbench.evaluation import run as evaluation_run
from recovery_workbench.evaluation.environment import describe_environment

import design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path


def frozen_client(frozen: Mapping[str, JsonValue]) -> diagnosis.OllamaClient:
    """A client with exactly the frozen settings, after checking the code still matches them and the digest."""
    model: dict[str, JsonValue] = frozen["model"]  # type: ignore[assignment]
    versions: dict[str, JsonValue] = frozen["versions"]  # type: ignore[assignment]
    checks: dict[str, tuple[JsonValue, JsonValue]] = {
        "chosen_think": (frozen["chosen_think"], "off"),
        "think": (model["think"], False),
        "prompt_version": (model["prompt_version"], diagnosis.PROMPT_VERSION),
        "validator": (versions["validator"], diagnosis.VALIDATOR_VERSION),
        "packet": (versions["packet"], packet.PACKET_VERSION),
        "deadline_s": (model["deadline_s"], evaluation_run.DEADLINE_S),
        "max_repairs": (model["max_repairs"], 1),
        "options": (model["options"], {**diagnosis.DEFAULT_OPTIONS, **evaluation_run.QWEN_SAMPLING}),
    }
    drifted = {name: {"frozen": pair[0], "code": pair[1]} for name, pair in checks.items() if pair[0] != pair[1]}
    if drifted:
        raise SystemExit(f"the code no longer matches {design.FROZEN_SETTINGS_PATH}: {drifted}; refusing to run")
    client = diagnosis.OllamaClient(model=str(model["model"]), options=model["options"],  # type: ignore[arg-type]
                                    keep_alive=str(model["keep_alive"]))
    digest = client.model_digest()
    if digest != model["model_digest"]:
        client.close()
        raise SystemExit(f"{model['model']} has digest {digest}, not the frozen {model['model_digest']}; "
                         "refusing to run (never pull a model for this stage)")
    return client


def _checked_packet(case: Mapping[str, JsonValue], experiment_dir: Path) -> EvidencePacket:
    built = design.read_packet(experiment_dir / str(case["packet_file"]))
    if built.packet_hash() != case["packet_hash"]:
        raise SystemExit(f"{case['case_id']}: the packet file changed after the build (hash {built.packet_hash()})")
    if sha256_hex(diagnosis.render_packet(built).encode("utf-8")) != case["prompt_sha256"]:
        raise SystemExit(f"{case['case_id']}: the packet renders a different prompt than at build time")
    return built


def _call_summary(call: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """One call without its messages, which the trace keeps: the reply, its timing and why it was rejected."""
    return {key: call[key] for key in ("call_index", "wall_ms", "error", "rejection_reasons", "reply")}


def _labels(output: DiagnosisOutput | None) -> str:
    if output is None:
        return "no diagnosis"
    return " / ".join((output.hypothesis.value, output.faulting_change_id, output.affected_method,
                       output.proposed_operation.value))


def diagnose_all(client: diagnosis.OllamaClient, cases: Sequence[Mapping[str, JsonValue]], experiment_dir: Path,
                 trace_path: Path) -> list[JsonValue]:
    answers: list[JsonValue] = []
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("x", encoding="utf-8") as trace:
        for index, case in enumerate(cases):
            built = _checked_packet(case, experiment_dir)
            started = datetime.now(UTC).isoformat()
            answer = evaluation_run.run_model(built, client, think=False)
            record: dict[str, JsonValue] = {
                "case_id": case["case_id"], "packet_hash": case["packet_hash"], "started_wall_utc": started,
                "cold": index == 0, **answer.as_json(), "calls": [_call_summary(call) for call in answer.calls],
            }
            answers.append(record)
            trace.write(json.dumps({"case_id": case["case_id"], "packet_hash": case["packet_hash"],
                                    "calls": answer.calls}, sort_keys=True) + "\n")
            trace.flush()
            print(f"[{index + 1}/{len(cases)}] {case['case_id']}: {answer.status} {answer.latency_ms} ms "
                  f"repairs={answer.repairs} {_labels(answer.output)}", flush=True)
    return answers


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose the planted-instruction packets with the live local model.")
    parser.add_argument("--experiment-dir", type=Path, default=design.EXPERIMENT_DIR)
    args = parser.parse_args(argv)
    experiment_dir: Path = args.experiment_dir
    answers_path = experiment_dir / design.ANSWERS_FILE
    if answers_path.exists():
        parser.error(f"{answers_path} exists; live answers are never overwritten (move it away to run again)")
    cases: list[Mapping[str, JsonValue]] = design.read_json(experiment_dir / design.CASES_FILE)["cases"]  # type: ignore[assignment]
    frozen = design.read_json(design.FROZEN_SETTINGS_PATH)
    client = frozen_client(frozen)
    try:
        client.unload()  # the first case starts cold, as in M4, so cold and warm latency are both measured
        started = datetime.now(UTC).isoformat()
        answers = diagnose_all(client, cases, experiment_dir, experiment_dir / design.TRACES_FILE)
        environment = describe_environment(client, think=False, deadline_s=evaluation_run.DEADLINE_S, systems=("model",))
    finally:
        client.close()
    design.write_new_json(answers_path, {
        "notice": "Live local inference against Ollama on 127.0.0.1; nothing here is scripted or replayed.",
        "frozen_settings": str(design.FROZEN_SETTINGS_PATH.relative_to(design.REPOSITORY_DIR)),
        "frozen_settings_sha256": sha256_hex(design.FROZEN_SETTINGS_PATH.read_bytes()),
        "cases_sha256": sha256_hex((experiment_dir / design.CASES_FILE).read_bytes()),
        "started_wall_utc": started, "finished_wall_utc": datetime.now(UTC).isoformat(),
        "environment": environment, "answers": answers,
    })
    print(f"wrote {len(answers)} answers to {answers_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
