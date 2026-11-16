"""M0 model gate: run the ten frozen packets through a local model.

    uv run python experiments/m0/run_m0.py                                  # run 1 settings
    uv run python experiments/m0/run_m0.py --model qwen3:8b --think off --sampling greedy
    uv run python experiments/m0/run_m0.py --model qwen3:8b --think on --sampling qwen-recommended \\
        --deadline 180 --num-predict 12000

Defaults reproduce run 1 exactly: qwen3:4b, thinking off then on, greedy decoding, 90 s deadline,
num_predict 6144. Prompt ``diag-p1`` and the validator are shared by every run, and the operation
policy is switched off here so results stay comparable with run 1; how many proposals the policy
WOULD have blocked is computed offline and reported in its own column.

Live inference only: every result comes from a fresh call to local Ollama. Each invocation is
appended to ``m0_model_report.json`` as a new run; earlier runs are never rewritten. Traces go to
``traces/<run label>_think_<off|on>.jsonl``. ``expected.json`` (the oracle) is read here only to score
answers after they arrive; it is never sent to the model. Preliminary gates on ten hand-made
packets, not accuracy claims.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import diagnosis
from recovery_workbench.contracts import EvidencePacket
from recovery_workbench.diagnosis import DiagnosisRun, OllamaClient, diagnose

M0_DIR = Path(__file__).resolve().parent
PACKET_DIR = M0_DIR / "packets"
EXPECTED_PATH = M0_DIR / "expected.json"
REPORT_PATH = M0_DIR / "m0_model_report.json"
TRACE_DIR = M0_DIR / "traces"
GATE_MIN_HYPOTHESES = 8
GATE_MAX_WARM_MEDIAN_S = 30.0
STRETCH_MIN_NEXT_CHECK = 8
UNLOAD_WAIT_S = 20.0
# Models this experiment loads; only these are unloaded before a cold measurement.
M0_MODELS = ("qwen3:4b", "qwen3:8b")
SEED = 20261111
NUM_CTX = 16384

# Sampling presets. "greedy" is run 1. "qwen-recommended" follows the Qwen3 model card, which
# advises against greedy decoding in thinking mode; the fixed seed is kept in both presets.
SAMPLING: Mapping[str, Mapping[bool, Mapping[str, JsonValue]]] = {
    "greedy": {False: {"temperature": 0}, True: {"temperature": 0}},
    "qwen-recommended": {
        True: {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0},
        False: {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0},
    },
}


def inference_options(*, sampling: str, think: bool, num_predict: int) -> dict[str, JsonValue]:
    return {**SAMPLING[sampling][think], "seed": SEED, "num_ctx": NUM_CTX, "num_predict": num_predict}


# --------------------------------------------------------------------------- environment


def sysctl(name: str) -> str:
    completed = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return f"unavailable ({completed.stderr.strip() or 'sysctl failed'})"
    return completed.stdout.strip()


def environment(client: OllamaClient) -> dict[str, JsonValue]:
    memsize = sysctl("hw.memsize")
    return {
        "hardware": {
            "chip": sysctl("machdep.cpu.brand_string"),
            "model": sysctl("hw.model"),
            "cpu_count": sysctl("hw.ncpu"),
            "ram_bytes": int(memsize) if memsize.isdigit() else memsize,
            "ram_gib": round(int(memsize) / 2**30, 1) if memsize.isdigit() else None,
        },
        "os": {"platform": platform.platform(), "macos_version": platform.mac_ver()[0]},
        "python": sys.version.split()[0],
        "packages": {name: importlib.metadata.version(name) for name in ("httpx", "pydantic")},
        "ollama": {"version": client.version(), "url": diagnosis.DEFAULT_OLLAMA_URL},
        "model": {"name": client.model, "digest": client.model_digest()},
    }


def unload_and_wait(client: OllamaClient) -> bool:
    """Drop every M0 model from memory so the next call is a genuine cold start; True once gone."""
    for model in M0_MODELS:
        if any(running.name == model for running in client.running_models()):
            client.unload(model)
    deadline = time.monotonic() + UNLOAD_WAIT_S
    while time.monotonic() < deadline:
        if not any(running.name in M0_MODELS for running in client.running_models()):
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- scoring


def score(run: DiagnosisRun, truth: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    output = run.output
    if output is None:
        return {
            "validated": False,
            "hypothesis_correct": False,
            "faulting_change_correct": False,
            "affected_method_correct": False,
            "operation_acceptable": False,
            "unsafe_proposal": False,
            "next_check_strict": False,
            "next_check_lenient": False,
            "operation_policy_would_block": False,
        }
    operation = output.proposed_operation.value
    return {
        "validated": True,
        "hypothesis_correct": output.hypothesis.value == truth["hypothesis"],
        "faulting_change_correct": output.faulting_change_id == truth["faulting_change_id"],
        "affected_method_correct": output.affected_method == truth["affected_method"],
        "operation_acceptable": operation in truth["acceptable_operations"],
        "unsafe_proposal": operation in truth["unsafe_operations"],
        "next_check_strict": output.next_check.value == truth["next_check"],
        "next_check_lenient": output.next_check.value in truth["acceptable_next_checks"],
        # Offline only: validation ran with the policy off, so this changes nothing in this run.
        "operation_policy_would_block": not diagnosis.operation_allowed(output.hypothesis, output.proposed_operation),
    }


def call_measurements(run: DiagnosisRun) -> list[dict[str, JsonValue]]:
    measured = []
    for call in run.calls:
        reply = call.reply
        entry: dict[str, JsonValue] = {"call_index": call.call_index, "wall_ms": call.wall_ms, "error": call.error}
        if reply is not None:
            eval_s = (reply.eval_duration_ns or 0) / 1e9
            entry.update(
                {
                    "done_reason": reply.done_reason,
                    "total_duration_ms": _ns_to_ms(reply.total_duration_ns),
                    "load_duration_ms": _ns_to_ms(reply.load_duration_ns),
                    "prompt_eval_count": reply.prompt_eval_count,
                    "prompt_eval_duration_ms": _ns_to_ms(reply.prompt_eval_duration_ns),
                    "eval_count": reply.eval_count,
                    "eval_duration_ms": _ns_to_ms(reply.eval_duration_ns),
                    "generation_tokens_per_s": round(reply.eval_count / eval_s, 1) if reply.eval_count and eval_s else None,
                    "thinking_chars": len(reply.thinking or ""),
                    "content_chars": len(reply.content),
                    "rejection_reasons": list(call.rejection_reasons),
                }
            )
        measured.append(entry)
    return measured


def _ns_to_ms(value: int | None) -> int | None:
    return None if value is None else round(value / 1e6)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear interpolation between closest ranks (the 'inclusive' method)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _count(rows: Sequence[Mapping[str, JsonValue]], key: str) -> int:
    return sum(1 for row in rows if row["score"][key])


def _latency_summary(results: Sequence[Mapping[str, JsonValue]]) -> dict[str, JsonValue]:
    warm_s = [entry["elapsed_ms"] / 1000 for entry in results if not entry["cold"]]
    if not warm_s:
        return {"n": 0}
    return {
        "n": len(warm_s),
        "median": round(statistics.median(warm_s), 1),
        "p90": round(percentile(warm_s, 0.9), 1),
        "max": round(max(warm_s), 1),
        "min": round(min(warm_s), 1),
        "percentile_method": "linear interpolation between closest ranks",
    }


def _gate(results: Sequence[Mapping[str, JsonValue]], warm: Mapping[str, JsonValue], deadline_s: float) -> dict[str, JsonValue]:
    hypotheses = _count(results, "hypothesis_correct")
    median_warm = warm.get("median")
    hypotheses_pass = hypotheses >= GATE_MIN_HYPOTHESES
    latency_30s = isinstance(median_warm, float | int) and median_warm <= GATE_MAX_WARM_MEDIAN_S
    return {
        "hypotheses_pass": hypotheses_pass,
        "verdict_30s": {
            "rule": f"hypotheses >= {GATE_MIN_HYPOTHESES}/10 and warm median <= {GATE_MAX_WARM_MEDIAN_S:g} s",
            "latency_pass": latency_30s,
            "passes": hypotheses_pass and latency_30s,
        },
        "verdict_labelled_cut": {
            "rule": (
                f"hypotheses >= {GATE_MIN_HYPOTHESES}/10 with every diagnosis bounded by the {deadline_s:g} s "
                "deadline; the video labels the cut and states the warm median"
            ),
            "passes": hypotheses_pass,
            "warm_median_s_to_label": median_warm,
        },
        "warm_p90_within_30_s": isinstance(warm.get("p90"), float | int) and warm["p90"] <= GATE_MAX_WARM_MEDIAN_S,
        "stretch_tool_loop_condition": {
            "rule": f"strict next-check agreement >= {STRETCH_MIN_NEXT_CHECK}/10",
            "met": _count(results, "next_check_strict") >= STRETCH_MIN_NEXT_CHECK,
        },
    }


def summarise(
    results: Sequence[Mapping[str, JsonValue]], expected: Mapping[str, Mapping[str, JsonValue]], deadline_s: float
) -> dict[str, JsonValue]:
    total = len(results)
    scored_change = [entry for entry in results if expected[entry["packet_id"]]["change_identification_scored"]]
    validated = _count(results, "validated")
    cold = [entry for entry in results if entry["cold"]]
    all_calls = [call for entry in results for call in entry["calls"]]
    thinking = [call["thinking_chars"] for call in all_calls if "thinking_chars" in call]
    tokens_per_s = [call["generation_tokens_per_s"] for call in all_calls if call.get("generation_tokens_per_s")]
    prompt_tokens = [call["prompt_eval_count"] for call in all_calls if call.get("prompt_eval_count") and call["call_index"] == 0]
    warm = _latency_summary(results)
    return {
        "diagnoses": total,
        "validated": f"{validated}/{total}",
        "needs_review": f"{total - validated}/{total}",
        "hypotheses_correct": f"{_count(results, 'hypothesis_correct')}/{total}",
        "faulting_change_correct_scored_packets": f"{_count(scored_change, 'faulting_change_correct')}/{len(scored_change)}",
        "faulting_change_correct_all_packets": f"{_count(results, 'faulting_change_correct')}/{total}",
        "affected_method_correct": f"{_count(results, 'affected_method_correct')}/{total}",
        "operation_acceptable": f"{_count(results, 'operation_acceptable')}/{total}",
        "unsafe_proposals": f"{_count(results, 'unsafe_proposal')}/{total}",
        "operation_policy_would_block": f"{_count(results, 'operation_policy_would_block')}/{validated} validated",
        "next_check_agreement_strict": f"{_count(results, 'next_check_strict')}/{total}",
        "next_check_agreement_lenient": f"{_count(results, 'next_check_lenient')}/{total}",
        "repairs_used": sum(entry["repairs"] for entry in results),
        "cold_latency_s": round(cold[0]["elapsed_ms"] / 1000, 1) if cold else None,
        "cold_load_duration_s": round((cold[0]["calls"][0].get("load_duration_ms") or 0) / 1000, 1) if cold else None,
        "warm_latency_s": warm,
        "thinking_chars_per_call": {"median": statistics.median(thinking) if thinking else 0, "max": max(thinking, default=0)},
        "generation_tokens_per_s_median": round(statistics.median(tokens_per_s), 1) if tokens_per_s else None,
        "prompt_tokens_first_call": {"min": min(prompt_tokens), "max": max(prompt_tokens)} if prompt_tokens else None,
        "gate": _gate(results, warm, deadline_s),
    }


# --------------------------------------------------------------------------- the experiment


def run_setting(
    client: OllamaClient,
    packets: Mapping[str, EvidencePacket],
    expected: Mapping[str, Mapping[str, JsonValue]],
    *,
    think: bool,
    deadline_s: float,
    trace_path: Path,
) -> tuple[list[dict[str, JsonValue]], dict[str, JsonValue]]:
    unloaded = unload_and_wait(client)
    memory: dict[str, JsonValue] = {"models_unloaded_before_first_call": unloaded}
    results: list[dict[str, JsonValue]] = []
    with trace_path.open("w", encoding="utf-8") as traces:
        for position, (packet_id, packet) in enumerate(packets.items()):
            run = diagnose(packet, client, think=think, deadline_s=deadline_s, enforce_policies=False)
            verdict = score(run, expected[packet_id])
            entry: dict[str, JsonValue] = {
                "packet_id": packet_id,
                "think": think,
                "cold": position == 0 and unloaded,
                "status": run.status.value,
                "elapsed_ms": run.elapsed_ms,
                "repairs": run.repairs,
                "reasons": list(run.reasons),
                "output": run.output.model_dump(mode="json") if run.output else None,
                "score": verdict,
                "calls": call_measurements(run),
            }
            results.append(entry)
            traces.write(json.dumps({**entry, "packet_hash": run.packet_hash, "run": run.model_dump(mode="json")}) + "\n")
            traces.flush()
            if position == 0:
                memory["api_ps_after_first_call"] = [model.model_dump(mode="json") for model in client.running_models()]
            print(
                f"{client.model} think={'on ' if think else 'off'} {packet_id} {run.status.value:<12} "
                f"{run.elapsed_ms / 1000:6.1f}s hyp={'Y' if verdict['hypothesis_correct'] else 'n'} "
                f"change={'Y' if verdict['faulting_change_correct'] else 'n'} op={_operation_mark(verdict)} "
                f"next={'Y' if verdict['next_check_strict'] else 'n'} repairs={run.repairs}",
                flush=True,
            )
    memory["api_ps_after_last_call"] = [model.model_dump(mode="json") for model in client.running_models()]
    return results, memory


def _operation_mark(verdict: Mapping[str, JsonValue]) -> str:
    if verdict["operation_acceptable"]:
        return "ok"
    return "UNSAFE" if verdict["unsafe_proposal"] else "no"


def load_report() -> dict[str, JsonValue]:
    if REPORT_PATH.exists():
        return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    return {
        "report_version": 1,
        "notice": (
            "Live local inference against Ollama; nothing here is scripted or replayed. Preliminary M0 gate "
            "on ten hand-made synthetic packets: small-sample results, not accuracy claims."
        ),
        "runs": [],
    }


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=diagnosis.DEFAULT_MODEL, help="Ollama model tag, e.g. qwen3:8b")
    parser.add_argument("--think", choices=("off", "on", "both"), default="both", help="thinking setting(s) to run")
    parser.add_argument("--sampling", choices=tuple(SAMPLING), default="greedy", help="decoding preset")
    parser.add_argument("--deadline", type=float, default=diagnosis.DEFAULT_DEADLINE_S, help="overall seconds per diagnosis")
    parser.add_argument(
        "--num-predict", type=int, default=6144, help="token cap for thinking plus answer; raise it with a longer deadline"
    )
    parser.add_argument("--run-label", default=None, help="label for the report entry and trace files")
    args = parser.parse_args(argv)
    if args.deadline <= 0 or args.num_predict <= 0:
        parser.error("--deadline and --num-predict must be positive")
    return args


def default_label(args: argparse.Namespace, started_wall: datetime) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{args.model}_think-{args.think}_{args.sampling}_{args.deadline:g}s")
    return f"{started_wall.strftime('%Y%m%dT%H%M%SZ')}_{diagnosis.PROMPT_VERSION}_{slug}"


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_arguments(argv)
    settings = ("off", "on") if args.think == "both" else (args.think,)
    packets = {
        path.stem: EvidencePacket.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(PACKET_DIR.glob("m0_*.json"))
    }
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))["packets"]
    stale = [packet_id for packet_id, packet in packets.items() if expected[packet_id]["packet_hash"] != packet.packet_hash()]
    if stale:
        raise SystemExit(f"packets changed after expected.json was written: {stale}; rebuild both before scoring")
    report = load_report()
    started_wall = datetime.now(UTC)
    label = args.run_label or default_label(args, started_wall)
    if any(run.get("run_label") == label for run in report["runs"]):
        raise SystemExit(f"run label {label!r} is already in {REPORT_PATH.name}; choose another --run-label")
    TRACE_DIR.mkdir(parents=True, exist_ok=True)

    run_record: dict[str, JsonValue] = {
        "run_label": label,
        "started_wall_utc": started_wall.isoformat(),
        "inference": {
            "model": args.model,
            "sampling": args.sampling,
            "deadline_s": args.deadline,
            "max_repairs": 1,
            "stream": False,
            "keep_alive": "15m",
            "format": "per-packet JSON schema (build_response_schema)",
            "options_by_setting": {},
        },
        "prompt": {
            "version": diagnosis.PROMPT_VERSION,
            "system_prompt_sha256": hashlib.sha256(diagnosis.SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "system_prompt": diagnosis.SYSTEM_PROMPT,
            "schema_property_order": list(schema_order(next(iter(packets.values())))),
        },
        "validation": {"validator_version": diagnosis.VALIDATOR_VERSION, "policies_enforced": False},
        "packets": {packet_id: packet.packet_hash() for packet_id, packet in packets.items()},
        "settings": {},
    }
    for setting in settings:
        think = setting == "on"
        options = inference_options(sampling=args.sampling, think=think, num_predict=args.num_predict)
        run_record["inference"]["options_by_setting"][f"think_{setting}"] = options
        client = OllamaClient(model=args.model, options=options, keep_alive="15m")
        try:
            run_record.setdefault("environment", environment(client))
            trace_path = TRACE_DIR / f"{label}_think_{setting}.jsonl"
            results, memory = run_setting(
                client, packets, expected, think=think, deadline_s=args.deadline, trace_path=trace_path
            )
        finally:
            client.close()
        run_record["settings"][f"think_{setting}"] = {
            "summary": summarise(results, expected, args.deadline),
            "memory": memory,
            "trace_file": str(trace_path.relative_to(M0_DIR)),
            "per_packet": results,
        }
    run_record["finished_wall_utc"] = datetime.now(UTC).isoformat()
    report = load_report()  # re-read: another invocation may have appended meanwhile
    report["runs"].append(run_record)
    report["headline_run"] = label
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for setting, block in run_record["settings"].items():
        summary = block["summary"]
        print(f"\n{label} {setting}: " + json.dumps({key: value for key, value in summary.items() if key != "gate"}))
        print(f"{label} {setting} gate: " + json.dumps(summary["gate"]))


def schema_order(packet: EvidencePacket) -> tuple[str, ...]:
    return tuple(diagnosis.build_response_schema(packet)["properties"])


if __name__ == "__main__":
    main()
