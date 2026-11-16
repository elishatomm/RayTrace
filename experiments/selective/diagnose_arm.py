"""Stage 2, on the host with the live local model: one model arm, run from a clean export of the code under test.

    git archive 43a160f | tar -x -C <scratch>/g_export_43a160f        # the code under test, exactly as committed
    OLLAMA_HOST=127.0.0.1:11434 ollama serve &                        # loopback only; stopped right after the stage
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<scratch>/g_export_43a160f/src .venv/bin/python \\
        experiments/selective/diagnose_arm.py --arm selective_20261111 --code-export <scratch>/g_export_43a160f

Four arms, each one model system at one model seed:

* ``selective_<seed>``, S's model calls: every world the router sends to MODEL, held-out or control, in run order,
  through ``selective.diagnose_selective``, the product's own entry point (one call, at most one repair, val-6,
  needs_review when refused twice, no fallback to rules). S's answers on RULES-routed worlds are rules-v3's, which
  the summariser computes; no arm is needed for them;
* ``model_all_<seed>``, M-all: every held-out world, through ``diagnosis.diagnose`` with the same settings.

Before any model call the stage refuses:

* a pre-registration that is not the file the build hashed, or whose first line still says DRAFT;
* an addendum that is not recorded in ``addenda.json`` with its SHA-256;
* code that is not the pinned export of 43a160f: imported from elsewhere, other versions, a changed frozen file
  (router, comparator, rules-v2), or other package sources;
* a packet without its build-time hash;
* settings other than the frozen M4 ones with diag-p5, val-6 and the arm's seed, and any other model digest; no model
  is ever pulled.

The routes of all 58 worlds are computed first, by the export's ``selective.route``, and recorded. The model is
unloaded, so the first case starts cold, as in M4 and rounds 1-3. Each answer gets round 3's run record: the model,
its digest, the generation options with the seed, thinking, and the SHA-256 of the first request (messages plus
response schema) when the trace shows it was sent as built. Writes ``answers_<arm>.json`` and ``traces/<arm>.jsonl``
and never overwrites them. It opens no database.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import diagnosis, packet, selective
from recovery_workbench.contracts import EvidencePacket, sha256_hex
from recovery_workbench.evaluation import run as evaluation_run
from recovery_workbench.evaluation.environment import describe_environment

import selective_design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

round1 = selective_design.round1_design
round3_diagnose_arm = selective_design.round3_module("diagnose_arm")
CALL_SUMMARY_FIELDS = ("call_index", "wall_ms", "error", "rejection_reasons", "reply")


def frozen_settings_for(arm: selective_design.Arm, frozen: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The frozen M4 settings with diag-p5, val-6 and the arm's model seed: the only settings this test changes."""
    model: Mapping[str, JsonValue] = frozen["model"]  # type: ignore[assignment]
    options: Mapping[str, JsonValue] = model["options"]  # type: ignore[assignment]
    versions: Mapping[str, JsonValue] = frozen["versions"]  # type: ignore[assignment]
    return {**frozen,
            "model": {**model, "prompt_version": selective_design.CODE_VERSIONS["prompt"],
                      "options": {**options, "seed": arm.model_seed}},
            "versions": {**versions, "validator": selective_design.CODE_VERSIONS["validator"]}}


def arm_client(arm: selective_design.Arm, frozen: Mapping[str, JsonValue]) -> diagnosis.OllamaClient:
    """A client with the arm's settings, after checking the code matches them and the model digest (round 3's checks)."""
    settings = frozen_settings_for(arm, frozen)
    model: Mapping[str, JsonValue] = settings["model"]  # type: ignore[assignment]
    versions: Mapping[str, JsonValue] = settings["versions"]  # type: ignore[assignment]
    frozen_options: Mapping[str, JsonValue] = frozen["model"]["options"]  # type: ignore[index, assignment]
    checks: dict[str, tuple[JsonValue, JsonValue]] = {
        "chosen_think": (settings["chosen_think"], "off"),
        "think": (model["think"], selective_design.THINK),
        "prompt_version": (model["prompt_version"], diagnosis.PROMPT_VERSION),
        "validator": (versions["validator"], diagnosis.VALIDATOR_VERSION),
        "packet": (versions["packet"], packet.PACKET_VERSION),
        "deadline_s": (model["deadline_s"], selective_design.DEADLINE_S),
        "evaluation_deadline_s": (evaluation_run.DEADLINE_S, selective_design.DEADLINE_S),
        "max_repairs": (model["max_repairs"], selective_design.MAX_REPAIRS),
        "frozen_seed": (frozen_options["seed"], diagnosis.DEFAULT_OPTIONS["seed"]),
        "options": (model["options"], {**diagnosis.DEFAULT_OPTIONS, **evaluation_run.QWEN_SAMPLING, "seed": arm.model_seed}),
    }
    drifted = {name: {"settings": pair[0], "code": pair[1]} for name, pair in checks.items() if pair[0] != pair[1]}
    if drifted or arm.model_seed not in selective_design.MODEL_SEEDS:
        raise SystemExit(f"the code no longer matches {selective_design.FROZEN_SETTINGS_PATH} for the {arm.name} arm: "
                         f"{drifted}; refusing to run")
    client = diagnosis.OllamaClient(model=str(model["model"]), options=model["options"],  # type: ignore[arg-type]
                                    keep_alive=str(model["keep_alive"]))
    digest = client.model_digest()
    if digest != model["model_digest"]:
        client.close()
        raise SystemExit(f"{model['model']} has digest {digest}, not the frozen {model['model_digest']}; "
                         "refusing to run (never pull a model for this stage)")
    return client


def routes_of(packets: Mapping[str, EvidencePacket]) -> dict[str, dict[str, JsonValue]]:
    """Every world's route as the code under test decides it, with its reason, unmet criteria and measurements."""
    return {case_id: selective.route(built).model_dump(mode="json") for case_id, built in packets.items()}


def run_fields(run: diagnosis.DiagnosisRun) -> tuple[dict[str, JsonValue], list[JsonValue]]:
    """One model run as M4's runner records it (``evaluation.run.run_model``): the answer, and every call in full."""
    calls: list[JsonValue] = [
        {"call_index": call.call_index, "wall_ms": call.wall_ms, "error": call.error,
         "rejection_reasons": list(call.rejection_reasons),
         "reply": None if call.reply is None else call.reply.model_dump(mode="json"),
         "messages": [message.model_dump() for message in call.messages]}
        for call in run.calls
    ]
    answer: dict[str, JsonValue] = {
        "status": run.status.value, "latency_ms": run.elapsed_ms, "reasons": list(run.reasons),
        "output": None if run.output is None else run.output.model_dump(mode="json"),
        "unsafe_proposals": [proposal.model_dump(mode="json") for proposal in run.unsafe_proposals],
        "repairs": run.repairs,
    }
    return answer, calls


def diagnose_case(arm: selective_design.Arm, built: EvidencePacket,
                  client: diagnosis.ChatClient) -> tuple[dict[str, JsonValue], list[JsonValue]]:
    """One world under the arm's system: S through the selective entry point, M-all through the plain diagnosis."""
    if arm.system == selective_design.SELECTIVE_SYSTEM:
        chosen = selective.diagnose_selective(built, client, think=selective_design.THINK,
                                              deadline_s=selective_design.DEADLINE_S)
        if chosen.model_run is None:
            raise SystemExit(f"packet {built.packet_hash()[:16]} was routed to MODEL, but diagnose_selective answered it on "
                             f"the {chosen.route.target.value} route; the route must be deterministic, so the stage stops")
        answer, calls = run_fields(chosen.model_run)
        return answer | {"route": chosen.route.model_dump(mode="json"),
                         "rules_v3": chosen.rules_v3.model_dump(mode="json")}, calls
    run = diagnosis.diagnose(built, client, think=selective_design.THINK, deadline_s=selective_design.DEADLINE_S,
                             max_repairs=selective_design.MAX_REPAIRS, enforce_policies=True)
    return run_fields(run)


def _labels(answer: Mapping[str, JsonValue]) -> str:
    output = answer["output"]
    if not isinstance(output, dict):
        return "no diagnosis"
    return " / ".join(str(output[key]) for key in ("hypothesis", "faulting_change_id", "proposed_operation"))


def diagnose_all(arm: selective_design.Arm, case_ids: Sequence[str], packets: Mapping[str, EvidencePacket],
                 client: diagnosis.ChatClient, trace_path: Path) -> list[JsonValue]:
    """Diagnose the arm's worlds in order; the trace keeps every call's messages, the answers a summary of each call."""
    answers: list[JsonValue] = []
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("x", encoding="utf-8") as trace:
        for index, case_id in enumerate(case_ids):
            built = packets[case_id]
            started = datetime.now(UTC).isoformat()
            answer, calls = diagnose_case(arm, built, client)
            answers.append({"case_id": case_id, "packet_hash": built.packet_hash(), "started_wall_utc": started,
                            "cold": index == 0, **answer,
                            "calls": [{key: call[key] for key in CALL_SUMMARY_FIELDS} for call in calls]})  # type: ignore[index]
            trace.write(json.dumps({"case_id": case_id, "packet_hash": built.packet_hash(), "calls": calls},
                                   sort_keys=True) + "\n")
            trace.flush()
            print(f"[{index + 1}/{len(case_ids)}] {case_id}: {answer['status']} {answer['latency_ms']} ms "
                  f"repairs={answer['repairs']} {_labels(answer)}", flush=True)
    return answers


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one model arm of the phase-11b test with the live local model.")
    parser.add_argument("--arm", required=True, choices=sorted(selective_design.ARMS))
    parser.add_argument("--code-export", type=Path, required=True, help="the git archive export whose src/ is on PYTHONPATH")
    parser.add_argument("--experiment-dir", type=Path, default=selective_design.SELECTIVE_DIR)
    args = parser.parse_args(argv)
    arm = selective_design.arm_named(args.arm)
    experiment_dir: Path = args.experiment_dir
    answers_path, trace_path = experiment_dir / arm.answers_file, experiment_dir / arm.trace_file
    if answers_path.exists() or trace_path.exists():
        parser.error(f"{answers_path} or {trace_path} exists; live answers are never overwritten (move both away, and "
                     "write a dated addendum, to run again)")
    cases_path = experiment_dir / selective_design.CASES_FILE
    cases_document = round1.read_json(cases_path)
    preregistration = selective_design.checked_final_preregistration(cases_document, experiment_dir)
    addenda = selective_design.checked_addenda(experiment_dir)
    code = selective_design.checked_code_under_test(args.code_export.resolve())
    packets = selective_design.checked_packets(cases_document, experiment_dir)
    cases: Sequence[Mapping[str, JsonValue]] = cases_document["cases"]  # type: ignore[assignment]
    routes = routes_of(packets)
    case_ids = selective_design.arm_case_ids(arm, cases, {case_id: str(route["target"]) for case_id, route in routes.items()})
    rendered = {case_id: sha256_hex(diagnosis.render_packet(packets[case_id]).encode("utf-8")) for case_id in case_ids}
    frozen_path = selective_design.FROZEN_SETTINGS_PATH
    client = arm_client(arm, round1.read_json(frozen_path))
    try:
        client.unload()  # the first case starts cold, as in M4 and rounds 1-3
        started = datetime.now(UTC).isoformat()
        answers = diagnose_all(arm, case_ids, packets, client, trace_path)
        environment = describe_environment(client, think=selective_design.THINK, deadline_s=selective_design.DEADLINE_S,
                                           systems=("model",))
    finally:
        client.close()
    by_id = {str(case["case_id"]): case for case in cases}
    model: Mapping[str, JsonValue] = environment["model"]  # type: ignore[assignment]
    records = round3_diagnose_arm.run_records([by_id[case_id] for case_id in case_ids], experiment_dir, trace_path, model)
    answers = [answer | {"run_record": records[str(answer["case_id"])]} for answer in answers]  # type: ignore[index, operator]
    round1.write_new_json(answers_path, {
        "notice": "Live local inference against Ollama on 127.0.0.1; nothing here is scripted or replayed.",
        "arm": arm.name, "system": arm.system, "model_seed": arm.model_seed, "code_under_test": code,
        "preregistration": preregistration, "addenda": addenda,
        "frozen_settings": str(frozen_path.relative_to(selective_design.REPOSITORY_DIR)),
        "frozen_settings_sha256": sha256_hex(frozen_path.read_bytes()),
        "cases_sha256": sha256_hex(cases_path.read_bytes()),
        "routes": routes, "arm_case_ids": case_ids,
        "system_prompt_sha256": sha256_hex(diagnosis.SYSTEM_PROMPT.encode("utf-8")), "rendered_prompt_sha256": rendered,
        "started_wall_utc": started, "finished_wall_utc": datetime.now(UTC).isoformat(),
        "environment": environment, "answers": answers,
    })
    print(f"wrote {len(answers)} answers to {answers_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
