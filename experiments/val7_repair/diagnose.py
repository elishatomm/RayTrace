"""The val-7 repair measurement's live stage, on the host: diagnose samples S and C with the application's own path.

    git archive 3ee7e78 | tar -x -C <work>/app              # then the nine val-7 files, as recorded beside the answers
    OLLAMA_NO_CLOUD=1 OLLAMA_HOST=127.0.0.1:11434 ollama serve &   # loopback only; stopped right after the stage
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<work>/app/src .venv/bin/python diagnose.py \\
        --code-export <work>/app --val6-diagnosis <val-6 diagnosis.py> --output-dir <out>

PREREGISTRATION.md fixes what is measured. Round 3's stage (experiments/injection/round3/diagnose_arm.py) is the
pattern: the package must be the export's, at the expected versions; the model's digest must be the recorded one, and
no model is ever pulled; the model is unloaded first, so the first case starts cold; every packet keeps its stored
hash; each answer gets a run record with the SHA-256 of its first request; answers are never overwritten; no database
is opened.

What differs from round 3 is the code path, because the pre-registration measures the application "run as the
application runs it". A live diagnosis there is ``diagnosis_store._diagnose_with_model`` with ``load_settings()``'s
defaults: the client is ``diagnosis.OllamaClient(base_url=settings.ollama_url, model=settings.model)``, so it decodes
with the client's default options (greedy, seed 20261111), not the evaluation's Qwen sampling, and the call is
``diagnosis.diagnose(packet, client, think=settings.model_think, deadline_s=settings.run_timeout_seconds,
enforce_policies=True)``: one call, at most one repair. That call is made here as written.

For each diagnosis it records what the pre-registration lists. val-6's verdict comes from val-6's own diagnosis.py,
and so does "a val-6 run of the same first answers": val-6's diagnose loop is run with a client that answers every
request val-7's run already sent with that run's reply, and sends any other request (a repair carrying val-6's
reasons rather than val-7's) to the live model, recorded as such.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from pydantic import JsonValue

import recovery_workbench
from recovery_workbench import diagnosis
from recovery_workbench.contracts import EvidencePacket, canonical_json, sha256_hex
from recovery_workbench.evaluation.environment import describe_environment
from recovery_workbench.settings import Settings, load_settings

import val7_repair_design as design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

VAL7_DIAGNOSIS_SHA256 = "05f06d7b5c3d2025acaf47d7c0f392406358f73c47f8cdf6792b7d656e9a4163"
MODEL_DIGEST_PREFIX = "500a1f067a9f"  # the digest the README records
S_PASSES = 2
APPLICATION_DEFAULTS = {"model": "qwen3:8b", "model_think": False, "ollama_url": "http://127.0.0.1:11434",
                        "run_timeout_seconds": 90}


# --------------------------------------------------------------------------- the code and the settings under test


def package_sources_sha256(package_dir: Path) -> str:
    """Every .py file of the package, path and SHA-256 one per line, sorted by path, hashed."""
    lines = [f"{path.relative_to(package_dir).as_posix()} {sha256_hex(path.read_bytes())}"
             for path in sorted(package_dir.rglob("*.py"))]
    return sha256_hex("\n".join(lines).encode("utf-8"))


def code_under_test(code_export: Path) -> dict[str, JsonValue]:
    package_dir = Path(str(recovery_workbench.__file__)).resolve().parent
    exported = (code_export / "src" / "recovery_workbench").resolve()
    if package_dir != exported:
        raise SystemExit(f"recovery_workbench was imported from {package_dir}, not from the export {exported}; "
                         f"run with PYTHONPATH={code_export / 'src'}")
    found = {"prompt": diagnosis.PROMPT_VERSION, "validator": diagnosis.VALIDATOR_VERSION}
    if found != {"prompt": "diag-p5", "validator": "val-7"}:
        raise SystemExit(f"the measurement needs diag-p5 and val-7, but the code under test reports {found}")
    diagnosis_sha256 = sha256_hex((package_dir / "diagnosis.py").read_bytes())
    if diagnosis_sha256 != VAL7_DIAGNOSIS_SHA256:
        raise SystemExit(f"diagnosis.py has SHA-256 {diagnosis_sha256}, not the val-7 file {VAL7_DIAGNOSIS_SHA256}")
    return {"package_dir": "app/src/recovery_workbench", "prompt_version": diagnosis.PROMPT_VERSION,
            "validator_version": diagnosis.VALIDATOR_VERSION, "diagnosis_py_sha256": diagnosis_sha256,
            "package_sources_sha256": package_sources_sha256(package_dir)}


def application_settings() -> Settings:
    """``load_settings()`` with no WORKBENCH_* variable set: how the application runs out of the box."""
    settings = load_settings({})
    found = {name: getattr(settings, name) for name in APPLICATION_DEFAULTS}
    if found != APPLICATION_DEFAULTS:
        raise SystemExit(f"the application's default settings are {found}, not {APPLICATION_DEFAULTS}")
    return settings


def application_client(settings: Settings) -> tuple[diagnosis.OllamaClient, dict[str, JsonValue]]:
    """The client diagnosis_store._diagnose_with_model builds, after checking the model's digest."""
    client = diagnosis.OllamaClient(base_url=settings.ollama_url, model=settings.model)
    digest = client.model_digest()  # the application asks for it before diagnosing, too
    if not digest.startswith(MODEL_DIGEST_PREFIX):
        client.close()
        raise SystemExit(f"{settings.model} has digest {digest}, not {MODEL_DIGEST_PREFIX}...; refusing to run "
                         "(never pull a model for this measurement)")
    return client, {"model": client.model, "model_digest": digest, "ollama_version": client.version(),
                    "options": dict(client.options), "keep_alive": client.keep_alive, "think": settings.model_think,
                    "deadline_s": float(settings.run_timeout_seconds), "max_repairs": 1}


# --------------------------------------------------------------------------- val-6's run of the same first answer


def request_key(messages: Sequence[Mapping[str, JsonValue]], schema: Mapping[str, JsonValue], think: bool) -> str:
    return canonical_json({"messages": list(messages), "format": dict(schema), "think": think})


class ReplayingClient:
    """Answers a request val-7's run sent with that run's reply (or its error); sends any other request live."""

    def __init__(self, run: diagnosis.DiagnosisRun, built: EvidencePacket, live: diagnosis.OllamaClient,
                 target: ModuleType) -> None:
        schema = diagnosis.build_response_schema(built)
        self._sent = {request_key([m.model_dump() for m in call.messages], schema, run.think): call for call in run.calls}
        self._live, self._target = live, target
        self.replayed = 0
        self.live_calls: list[dict[str, JsonValue]] = []

    def chat(self, request: object, *, timeout_s: float) -> object:
        dumped = request.model_dump(mode="json")  # type: ignore[attr-defined]
        earlier = self._sent.get(request_key(dumped["messages"], dumped["response_schema"], dumped["think"]))
        if earlier is not None:
            self.replayed += 1
            if earlier.reply is None:
                raise self._target.ModelCallError(earlier.error or "the replayed call had no reply")
            return self._target.ChatReply.model_validate(earlier.reply.model_dump())
        started = datetime.now(UTC)
        try:
            reply = self._live.chat(diagnosis.ChatRequest.model_validate(dumped), timeout_s=timeout_s)
        except diagnosis.ModelCallError as failed:
            self.live_calls.append({"messages": dumped["messages"], "error": str(failed), "reply": None})
            raise self._target.ModelCallError(str(failed)) from failed
        wall_ms = round((datetime.now(UTC) - started).total_seconds() * 1000)
        self.live_calls.append({"messages": dumped["messages"], "error": None, "wall_ms": wall_ms,
                                "reply": reply.model_dump(mode="json")})
        return self._target.ChatReply.model_validate(reply.model_dump())


# --------------------------------------------------------------------------- one diagnosis


def _content(call: diagnosis.ModelCallRecord | None) -> str | None:
    return None if call is None or call.reply is None else call.reply.content


def _call_summary(call: diagnosis.ModelCallRecord | None) -> dict[str, JsonValue] | None:
    if call is None:
        return None
    return {"call_index": call.call_index, "wall_ms": call.wall_ms, "error": call.error,
            "rejection_reasons": list(call.rejection_reasons),
            "reply": None if call.reply is None else call.reply.model_dump(mode="json")}


def run_verdict(call: diagnosis.ModelCallRecord | None, built: EvidencePacket) -> design.Verdict | None:
    """val-7's verdict on one call's reply: the run's own reasons, re-checked wherever the reply is JSON."""
    if call is None or call.reply is None:
        return None
    reasons = tuple(call.rejection_reasons)
    try:
        json.loads(call.reply.content)
    except json.JSONDecodeError:
        return design.Verdict(False, reasons)
    rechecked = design.verdict(diagnosis, call.reply.content, built)
    if rechecked is None or rechecked.reasons != reasons:
        raise SystemExit(f"call {call.call_index}: the run's reasons {reasons} differ from val-7's re-check {rechecked}")
    return rechecked


def diagnose_one(item: design.SampleCase, client: diagnosis.OllamaClient, settings: Settings,
                 val6: ModuleType) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """One diagnosis as the application makes it, with everything the pre-registration records; and its trace."""
    built, case = item.packet, item.case
    if built.packet_hash() != item.packet_hash:
        raise SystemExit(f"{item.label}: the packet no longer hashes to {item.packet_hash}")
    deadline_s = float(settings.run_timeout_seconds)
    run = diagnosis.diagnose(built, client, think=settings.model_think, deadline_s=deadline_s, enforce_policies=True)
    first = run.calls[0] if run.calls else None
    repair = run.calls[1] if len(run.calls) > 1 else None
    first_content, repair_content = _content(first), _content(repair)
    first_val6 = design.verdict(val6, first_content, built)
    first_val7, repair_val7 = run_verdict(first, built), run_verdict(repair, built)
    trigger = bool(first_val6 and first_val7 and first_val6.accepted and not first_val7.accepted)
    first_score = design.scored(case, design.output_of(first_content)) if first_content is not None else None
    repair_score = design.scored(case, design.output_of(repair_content)) if repair_content is not None else None
    recovered = None
    if trigger:
        recovered = bool(repair_val7 is not None and repair_val7.accepted and repair_score is not None
                         and first_score is not None
                         and int(bool(repair_score["outcome_correct"])) >= int(bool(first_score["outcome_correct"])))
    counterfactual = None
    replaying = None
    if first_content is not None:
        replaying = ReplayingClient(run, built, client, val6)
        val6_run = val6.diagnose(built, replaying, think=settings.model_think, deadline_s=deadline_s,
                                 enforce_policies=True)
        output = None if val6_run.output is None else json.loads(val6_run.output.model_dump_json())
        counterfactual = {
            "status": val6_run.status.value, "output": output, "reasons": list(val6_run.reasons),
            "repairs": val6_run.repairs, "replayed_calls": replaying.replayed,
            "live_calls": [{key: value for key, value in call.items() if key != "messages"}
                           for call in replaying.live_calls],
            "score": design.scored(case, design.output_of(json.dumps(output)) if output is not None else None),
        }
    schema = diagnosis.build_response_schema(built)
    initial = [message.model_dump() for message in diagnosis.initial_messages(built)]
    sent_as_built = first is not None and [m.model_dump() for m in first.messages] == initial
    record: dict[str, JsonValue] = {
        "packet_hash": item.packet_hash, "label": item.label, "stored_answers": list(item.stored_answers),
        "first": {"call": _call_summary(first), "val6": None if first_val6 is None else first_val6.as_json(),
                  "val7": None if first_val7 is None else first_val7.as_json(),
                  "val7_rules": design.val7_rules_in(first_val7.reasons) if first_val7 else [], "score": first_score},
        "trigger": trigger,
        "repair": None if repair is None else {"call": _call_summary(repair),
                                               "val7": None if repair_val7 is None else repair_val7.as_json(),
                                               "score": repair_score},
        "final": {"status": run.status.value, "reasons": list(run.reasons),
                  "output": None if run.output is None else json.loads(run.output.model_dump_json()),
                  "score": design.scored(case, run.output)},
        "recovered": recovered,
        "latency": {"elapsed_ms": run.elapsed_ms, "first_call_ms": None if first is None else first.wall_ms,
                    "repair_call_ms": None if repair is None else repair.wall_ms,
                    "added_ms": None if first is None else run.elapsed_ms - first.wall_ms},
        "val6_run": counterfactual,
        "request": {"first_request_sha256": sha256_hex(canonical_json({"messages": initial, "format": schema}))
                    if sent_as_built else None,
                    "response_schema_sha256": sha256_hex(canonical_json(schema)), "first_call_sent_as_built": sent_as_built},
    }
    trace: dict[str, JsonValue] = {
        "packet_hash": item.packet_hash, "label": item.label,
        "calls": [{**_call_summary(call), "messages": [m.model_dump() for m in call.messages]}  # type: ignore[dict-item]
                  for call in run.calls],
        "val6_live_calls": [] if replaying is None else replaying.live_calls,
    }
    return record, trace


def _labels(record: Mapping[str, JsonValue]) -> str:
    output = record["final"]["output"]  # type: ignore[index]
    if not isinstance(output, dict):
        return "no diagnosis"
    return " / ".join(str(output[key]) for key in ("hypothesis", "faulting_change_id", "proposed_operation"))


# --------------------------------------------------------------------------- the stage


def run_plan(sample_s: Sequence[design.SampleCase], sample_c: Sequence[design.SampleCase]) -> list[tuple[str, int, design.SampleCase]]:
    """S twice, as two passes in the same order, then C once."""
    return [*(("S", number, item) for number in range(1, S_PASSES + 1) for item in sample_s),
            *(("C", 1, item) for item in sample_c)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose the val-7 repair measurement's samples with the live model.")
    parser.add_argument("--code-export", type=Path, required=True, help="the export whose src/ is on PYTHONPATH")
    parser.add_argument("--val6-diagnosis", type=Path, required=True, help="val-6's diagnosis.py (SHA-256 pinned)")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repository-dir", type=Path, default=None, help="where the stored packets are (default: the export)")
    parser.add_argument("--plan-only", action="store_true", help="print the samples and stop before any model call")
    args = parser.parse_args(argv)
    code_export: Path = args.code_export.resolve()
    root: Path = (args.repository_dir or code_export).resolve()
    answers_path, trace_path = args.output_dir / "answers.json", args.output_dir / "traces.jsonl"
    if answers_path.exists() or trace_path.exists():
        parser.error(f"{answers_path} or {trace_path} exists; live answers are never overwritten")
    code = code_under_test(code_export)
    preregistration = design.checked_preregistration(root / "experiments" / "val7_repair")
    val6 = design.val6_module(args.val6_diagnosis)
    settings = application_settings()
    sample_s, sample_c = design.sample_s(root, val6), design.sample_c(root)
    plan = run_plan(sample_s, sample_c)
    print(f"sample S: {len(sample_s)} packets x {S_PASSES}; sample C: {len(sample_c)} packets; {len(plan)} diagnoses",
          flush=True)
    if args.plan_only:
        for sample, number, item in plan:
            print(f"{sample}{number} {item.packet_hash[:16]} {item.label} ({len(item.stored_answers)} stored answers)")
        return 0
    client, model = application_client(settings)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    answers: list[JsonValue] = []
    try:
        client.unload()  # the first case starts cold, as in M4 and rounds 1-3
        started = datetime.now(UTC).isoformat()
        with trace_path.open("x", encoding="utf-8") as traces:
            for index, (sample, number, item) in enumerate(plan):
                case_started = datetime.now(UTC).isoformat()
                record, trace = diagnose_one(item, client, settings, val6)
                answers.append({"order": index, "sample": sample, "pass": number, "cold": index == 0,
                                "started_wall_utc": case_started, **record})
                traces.write(json.dumps({"order": index, "sample": sample, "pass": number, **trace}, sort_keys=True) + "\n")
                traces.flush()
                print(f"[{index + 1}/{len(plan)}] {sample}{number} {item.label}: {record['final']['status']} "  # type: ignore[index]
                      f"trigger={record['trigger']} recovered={record['recovered']} "
                      f"{record['latency']['elapsed_ms']} ms {_labels(record)}", flush=True)  # type: ignore[index]
        finished = datetime.now(UTC).isoformat()
        environment = describe_environment(client, think=settings.model_think,
                                           deadline_s=float(settings.run_timeout_seconds), systems=("model",))
    finally:
        client.close()
    document = {
        "notice": "Live local inference against Ollama on 127.0.0.1; nothing here is scripted or replayed, except that "
                  "each val6_run replays the val-7 run's own requests and replies, as its docstring says.",
        "preregistration": preregistration, "code_under_test": code,
        "val6_diagnosis_sha256": design.VAL6_DIAGNOSIS_SHA256,
        "application_settings": {name: getattr(settings, name) for name in APPLICATION_DEFAULTS},
        "model": model, "samples": {"S": [{"packet_hash": item.packet_hash, "label": item.label,
                                           "stored_answers": list(item.stored_answers)} for item in sample_s],
                                    "C": [{"packet_hash": item.packet_hash, "label": item.label} for item in sample_c]},
        "started_wall_utc": started, "finished_wall_utc": finished, "environment": environment, "answers": answers,
    }
    with answers_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=1, sort_keys=True, default=str) + "\n")
    print(f"wrote {len(answers)} answers to {answers_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
