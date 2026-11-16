"""Smoke check of prompt diag-p5 (phase 10): M4's 18 DEVELOPMENT packets, live, once. Descriptive; it decides nothing.

    git archive <HEAD> | tar -x -C <scratch>/d_p10/smoke_export      # then copy in diag-p5's diagnosis.py
    OLLAMA_HOST=127.0.0.1:11434 ollama serve &                         # loopback only; stopped right after
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<export>/src .venv/bin/python \\
        evaluation/runs/dev_diag_p5_smoke/run_smoke.py --code-export <export>

Code under test: the export's package, which must report prompt diag-p5 and validator val-6. Inputs, read from the
export: ``evaluation/traces/dev_think_off.jsonl`` (the 18 development packets M4 diagnosed with diag-p2 + val-4; the
TEST traces are never opened), ``evaluation/frozen_settings.json`` and ``evaluation/runs/dev_think_off/results.json``.
The model settings are M4's frozen ones (qwen3:8b at its frozen digest, think off, temperature 0.6, seed 20261111);
only the prompt and the validator differ. The model is unloaded first, so the first case is a cold start as in M4.
Every answer is live inference. Writes ``answers.json`` and ``summary.json`` next to this file and never overwrites
them. It opens no database and runs no recovery: the comparison is diagnosis outcome only.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

import recovery_workbench
from recovery_workbench import diagnosis
from recovery_workbench.contracts import EvidencePacket, canonical_json, sha256_hex
from recovery_workbench.evaluation import run as evaluation_run
from recovery_workbench.evaluation.environment import describe_environment

OUTPUT_DIR = Path(__file__).resolve().parent
TRACE = Path("evaluation/traces/dev_think_off.jsonl")
M4_RESULTS = Path("evaluation/runs/dev_think_off/results.json")
FROZEN_SETTINGS = Path("evaluation/frozen_settings.json")
CODE = {"prompt_version": "diag-p5", "validator_version": "val-6"}


def code_under_test(code_export: Path) -> dict[str, JsonValue]:
    """The package this process diagnoses with, after checking it is the export's and reports diag-p5 and val-6."""
    package_dir = Path(str(recovery_workbench.__file__)).resolve().parent
    if package_dir != (code_export / "src" / "recovery_workbench").resolve():
        raise SystemExit(f"recovery_workbench was imported from {package_dir}, not from {code_export}; "
                         f"run with PYTHONPATH={code_export / 'src'}")
    found = {"prompt_version": diagnosis.PROMPT_VERSION, "validator_version": diagnosis.VALIDATOR_VERSION}
    if found != CODE:
        raise SystemExit(f"the smoke check needs {CODE}, but the code under test reports {found}; refusing to run")
    sources = sorted(path.relative_to(package_dir).as_posix() for path in package_dir.rglob("*.py"))
    listing = "".join(f"{name} {sha256_hex((package_dir / name).read_bytes())}\n" for name in sources)
    return {**found, "code_export": str(code_export), "package_dir": str(package_dir),
            "diagnosis_py_sha256": sha256_hex((package_dir / "diagnosis.py").read_bytes()),
            "package_sources_sha256": sha256_hex(listing.encode("utf-8"))}


def smoke_client(frozen: Mapping[str, JsonValue]) -> diagnosis.OllamaClient:
    """M4's frozen model settings, after checking the code still builds the same options; never pulls a model."""
    model: Mapping[str, JsonValue] = frozen["model"]  # type: ignore[assignment]
    expected_options = {**diagnosis.DEFAULT_OPTIONS, **evaluation_run.QWEN_SAMPLING}
    drifted = {name: pair for name, pair in {
        "chosen_think": (frozen["chosen_think"], "off"), "think": (model["think"], False),
        "deadline_s": (model["deadline_s"], evaluation_run.DEADLINE_S), "max_repairs": (model["max_repairs"], 1),
        "options": (model["options"], expected_options),
    }.items() if pair[0] != pair[1]}
    if drifted:
        raise SystemExit(f"the code no longer matches {FROZEN_SETTINGS}: {drifted}; refusing to run")
    client = diagnosis.OllamaClient(model=str(model["model"]), options=model["options"],  # type: ignore[arg-type]
                                    keep_alive=str(model["keep_alive"]))
    digest = client.model_digest()
    if digest != model["model_digest"]:
        client.close()
        raise SystemExit(f"{model['model']} has digest {digest}, not the frozen {model['model_digest']}; refusing to "
                         "run (never pull a model for this check)")
    return client


# M4's traces store each packet with sorted keys, but M4 rendered packet.py's key order; the order is read back
# from the first user message M4 sent (the same method as tests/test_prompt_p3.py::in_rendered_key_order).


def _shown_name(key: str, value: JsonValue) -> str:
    is_time = key.endswith("_ms") and isinstance(value, int) and not isinstance(value, bool)
    return key.removesuffix("_ms") if is_time else key


def _in_shown_order(value: JsonValue, shown: JsonValue) -> JsonValue:
    if isinstance(value, list) and isinstance(shown, list):
        return [_in_shown_order(entry, shown_entry) for entry, shown_entry in zip(value, shown, strict=True)]
    if not isinstance(value, dict) or not isinstance(shown, dict):
        return value
    order = list(shown)
    keys = sorted(value, key=lambda key: order.index(_shown_name(key, value[key])))
    return {key: _in_shown_order(value[key], shown[_shown_name(key, value[key])]) for key in keys}


def in_rendered_key_order(document: dict, user_message: str) -> EvidencePacket:
    lines = user_message.split("\n")
    card = lines[:lines.index("Evidence items:")]
    shown = {label: json.loads(line.removeprefix(f"{label}: ")) for line in card
             for label in ("Cohort", "Summary counts") if line.startswith(f"{label}: ")}
    incident = {**document["incident"],
                "cohort": _in_shown_order(document["incident"]["cohort"], shown["Cohort"]),
                "summary_counts": _in_shown_order(document["incident"]["summary_counts"], shown["Summary counts"])}
    items, position = [], 0
    for item in document["items"]:
        position = next(index for index in range(position, len(lines)) if lines[index].startswith(f"[{item['evidence_id']}] "))
        if item["facts"]:
            facts_line = lines[position + 1]
            item = {**item, "facts": _in_shown_order(item["facts"], json.loads(facts_line.removeprefix("facts: ")))}
        items.append(item)
    return EvidencePacket.model_validate({**document, "incident": incident, "items": items})


def development_cases(export: Path) -> list[tuple[evaluation_run.Case, EvidencePacket, dict]]:
    """M4's 18 development runs, in M4's order: the scoring case, the packet M4 diagnosed and M4's model record."""
    m4 = {case["run_id"]: case for case in json.loads((export / M4_RESULTS).read_text(encoding="utf-8"))["cases"]}
    cases = []
    for line in (export / TRACE).read_text(encoding="utf-8").splitlines():
        trace = json.loads(line)
        user = trace["answers"]["model"]["calls"][0]["messages"][1]["content"]
        built = in_rendered_key_order(trace["packet"], user)
        if built.packet_hash() != trace["packet_hash"]:
            raise SystemExit(f"{trace['run_id']}: the trace packet does not hash to the packet M4 diagnosed")
        record = m4[trace["run_id"]]
        case = evaluation_run.Case(
            run_id=record["run_id"], seed=record["seed"], family=record["family"], variant=record["variant"],
            params=record["params"], cutoff_ms=built.incident.evidence_cutoff_ms, expected=record["expected"],
            oracle={}, provider_records=Path(),  # scoring reads family and expected only; no recovery runs here
        )
        cases.append((case, built, record))
    if len(cases) != 18 or [case.run_id for case, _, _ in cases] != [record["run_id"] for record in m4.values()]:
        raise SystemExit("the development trace does not hold M4's 18 development runs in M4's order")
    return cases


def _labels(output: dict | None) -> str:
    if output is None:
        return "no diagnosis"
    return " / ".join(str(output[key]) for key in ("hypothesis", "faulting_change_id", "affected_method",
                                                   "proposed_operation"))


def _call_summary(call: dict) -> dict:
    return {key: call[key] for key in ("call_index", "wall_ms", "error", "rejection_reasons", "reply")}


def diagnose_all(client: diagnosis.OllamaClient,
                 cases: Sequence[tuple[evaluation_run.Case, EvidencePacket, dict]]) -> list[dict]:
    """One live diagnosis per case, scored with M4's own scoring, next to M4's record for the same packet."""
    answers = []
    for index, (case, built, m4) in enumerate(cases):
        messages = [message.model_dump() for message in diagnosis.initial_messages(built)]
        schema = diagnosis.build_response_schema(built)
        started = datetime.now(UTC).isoformat()
        answer = evaluation_run.run_model(built, client, think=False)
        sent_as_built = bool(answer.calls) and answer.calls[0]["messages"] == messages
        record = {
            "run_id": case.run_id, "family": case.family, "variant": case.variant, "packet_hash": built.packet_hash(),
            "prompt_sha256": sha256_hex(messages[1]["content"].encode("utf-8")),
            "first_request_sha256": sha256_hex(canonical_json({"messages": messages, "format": schema}))
            if sent_as_built else None,
            "failure_details_relocated": "failure details ---" in messages[1]["content"],
            "started_wall_utc": started, "cold": index == 0, **answer.as_json(),
            "score": evaluation_run.score(case, answer.output),
            "calls": [_call_summary(call) for call in answer.calls],
            "m4": {key: m4["model"][key] for key in ("status", "output", "score", "repairs", "unsafe_proposals")},
        }
        answers.append(record)
        print(f"[{index + 1}/{len(cases)}] {case.run_id} {case.family}: {answer.status} {answer.latency_ms} ms "
              f"repairs={answer.repairs} {_labels(record['output'])} | outcome {record['score']['outcome_correct']} "
              f"(M4 {m4['model']['score']['outcome_correct']})", flush=True)
    return answers


def _counts(records: Sequence[dict], families: Sequence[str]) -> dict[str, JsonValue]:
    return {
        "outcome_correct": sum(bool(record["score"]["outcome_correct"]) for record in records),
        "needs_review": sum(record["status"] == "needs_review" for record in records),
        "unsafe_proposals": sum(len(record["unsafe_proposals"]) for record in records),
        "repaired_runs": sum(bool(record["repairs"]) for record in records),
        "outcome_correct_by_family": dict(sorted(Counter(
            family for family, record in zip(families, records, strict=True) if record["score"]["outcome_correct"]
        ).items())),
    }


def summarise(answers: Sequence[dict]) -> dict[str, JsonValue]:
    """Counts side by side; per run, M4's diag-p2 + val-4 outcome against this run's diag-p5 + val-6 outcome."""
    families = [str(answer["family"]) for answer in answers]
    latencies = [int(answer["latency_ms"]) for answer in answers]
    return {
        "runs": len(answers),
        "families": dict(sorted(Counter(families).items())),
        "m4_diag_p2_val_4": _counts([answer["m4"] for answer in answers], families),
        "diag_p5_val_6": _counts(answers, families),
        "changed_outcome": sorted(str(answer["run_id"]) for answer in answers
                                  if answer["score"]["outcome_correct"] != answer["m4"]["score"]["outcome_correct"]),
        "cold_latency_ms": latencies[0],
        "warm_median_latency_ms": statistics.median(latencies[1:]),
        "per_run": [{
            "run_id": answer["run_id"], "family": answer["family"], "variant": answer["variant"],
            "failure_details_relocated": answer["failure_details_relocated"],
            "m4": {"outcome_correct": answer["m4"]["score"]["outcome_correct"], "status": answer["m4"]["status"],
                   "answer": _labels(answer["m4"]["output"])},
            "diag_p5": {"outcome_correct": answer["score"]["outcome_correct"], "status": answer["status"],
                        "repairs": answer["repairs"], "answer": _labels(answer["output"])},
        } for answer in answers],
    }


def _write_new(path: Path, document: JsonValue) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke check of diag-p5 on M4's 18 development packets (live).")
    parser.add_argument("--code-export", type=Path, required=True, help="the export whose src/ is on PYTHONPATH")
    args = parser.parse_args(argv)
    export: Path = args.code_export.resolve()
    answers_path, summary_path = OUTPUT_DIR / "answers.json", OUTPUT_DIR / "summary.json"
    if answers_path.exists() or summary_path.exists():
        parser.error(f"{OUTPUT_DIR} already holds a smoke check; live answers are never overwritten")
    code = code_under_test(export)
    cases = development_cases(export)
    frozen = json.loads((export / FROZEN_SETTINGS).read_text(encoding="utf-8"))
    client = smoke_client(frozen)
    try:
        client.unload()  # the first case starts cold, as in M4
        started = datetime.now(UTC).isoformat()
        answers = diagnose_all(client, cases)
        finished = datetime.now(UTC).isoformat()
        environment = describe_environment(client, think=False, deadline_s=evaluation_run.DEADLINE_S, systems=("model",))
    finally:
        client.close()
    inputs = {str(path): sha256_hex((export / path).read_bytes()) for path in (TRACE, M4_RESULTS, FROZEN_SETTINGS)}
    record: dict[str, JsonValue] = {
        "notice": ("Live local inference against Ollama on 127.0.0.1; nothing here is scripted or replayed. A smoke "
                   "check: descriptive only, one run, decides nothing. M4's development results used diag-p2 + val-4; "
                   "these use diag-p5 + val-6 with M4's frozen model settings, so the prompt and the validator both "
                   "differ, and with temperature 0.6 a changed prompt also changes the sampled path."),
        "code_under_test": code, "inputs_sha256": inputs, "started_wall_utc": started, "finished_wall_utc": finished,
        "environment": environment,
    }
    _write_new(answers_path, {**record, "answers": answers})
    _write_new(summary_path, {**record, "summary": summarise(answers)})
    print(json.dumps(summarise(answers)["diag_p5_val_6"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
