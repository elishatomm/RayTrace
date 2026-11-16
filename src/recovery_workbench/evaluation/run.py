"""M4 evaluation runner (PLAN.md §5 M4, EVALUATION.md §9-10).

    python -m recovery_workbench.evaluation.run --split dev|test --data-dir DIR --systems rules,model \\
        --model qwen3:8b --think off|on --output-dir evaluation/ [--label NAME]

For every run of a generated split (``evaluation.generate``): build the pkt-2 packet at the run's
cutoff with the merchant code live then, diagnose it with the rules baseline and/or the local
model, score each answer against the run's oracle, and, when the final proposal is
``reprocess_inbox``, act as the operator end to end on a private copy of the database. The
operator picks the allowlisted fix for the module the BLAMED deploy changed, so a wrong blame
fails fix validation and blocks recovery (a safe failure). Writes ``results.json``,
``recovery_ledger.json``, ``environment.json`` and a copy of ``data_manifest.json`` to
``<output-dir>/runs/<label>/`` and one trace line per run to ``<output-dir>/traces/<label>.jsonl``.
One model inference runs at a time.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import diagnosis, packet, rules, storage
from recovery_workbench.contracts import (
    CHOICE_NONE,
    CHOICE_UNKNOWN,
    DiagnosisOutput,
    EvidencePacket,
    Hypothesis,
    Operation,
)
from recovery_workbench.evaluation import recovery
from recovery_workbench.evaluation.environment import describe_environment
from recovery_workbench.evaluation.generate import DATA_MANIFEST_FILE
from recovery_workbench.simulator import families, runtime

DEADLINE_S = 180.0
# Qwen3's recommended sampling for this model family (temperature 0.6, top_p 0.95, top_k 20, min_p 0);
# context, output budget and seed stay as diagnosis.DEFAULT_OPTIONS sets them.
QWEN_SAMPLING: Mapping[str, JsonValue] = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0}
SYSTEMS = ("rules", "model")
_CHANGE_SCORED = frozenset({"D1", "D3", "D6"})
_METHOD_SCORED = frozenset({"D2", "D6"})


@dataclass(frozen=True)
class Case:
    """One generated run with what scoring needs from the manifest and its oracle."""

    run_id: str
    seed: int
    family: str
    variant: str
    params: Mapping[str, JsonValue]
    cutoff_ms: int
    expected: Mapping[str, JsonValue]
    oracle: Mapping[str, JsonValue]
    provider_records: Path

    @property
    def base_family(self) -> str:
        """D6 is scored like the incident it sits next to: a method drop (D2) or no incident (D5)."""
        if self.family != "D6":
            return self.family
        return "D2" if self.expected["hypothesis"] == Hypothesis.PROVIDER_METHOD_DEGRADATION.value else "D5"


def load_cases(data_dir: Path, manifest: families.EvaluationManifest, split: str) -> list[Case]:
    data_manifest = json.loads((data_dir / DATA_MANIFEST_FILE).read_text(encoding="utf-8"))
    if data_manifest["split"] != split:
        raise ValueError(f"{data_dir} holds the {data_manifest['split']!r} split, not {split!r}")
    by_seed = {run.seed: run for run in manifest.runs if run.split == split}
    cases = []
    for entry in data_manifest["runs"]:
        run = by_seed[entry["seed"]]
        oracle = json.loads((data_dir / entry["oracle_file"]).read_text(encoding="utf-8"))
        recovery.require_current_oracle(oracle)  # here, before any work: _recover records exceptions instead of stopping
        cases.append(Case(
            run_id=entry["run_id"], seed=run.seed, family=run.family, variant=run.variant, params=run.params,
            cutoff_ms=entry["cutoff_ms"], expected=oracle["expected"], oracle=oracle,
            provider_records=data_dir / entry["provider_records_file"],
        ))
    return cases


def build_case_packet(conn: sqlite3.Connection, case: Case) -> EvidencePacket:
    """The packet an operator gets at the run's cutoff, with the merchant code deployed then."""
    [incident] = storage.list_incidents(conn, run_id=case.run_id)
    deployed = runtime.active_merchant(conn, run_id=case.run_id, at_ms=case.cutoff_ms)
    return packet.build_packet(conn, run_id=case.run_id, incident_id=incident.incident_id,
                               cutoff_ms=case.cutoff_ms, deployed=deployed, persist=False)


# --------------------------------------------------------------------------- scoring


def score(case: Case, output: DiagnosisOutput | None) -> dict[str, JsonValue]:
    """Score one final answer against the oracle; None (needs_review) scores as not correct."""
    family = case.family
    if output is None:
        return _unanswered_score(family)
    expected = case.expected
    checks = _Checks(
        hypothesis=output.hypothesis.value == expected["hypothesis"],
        change=output.faulting_change_id == expected["faulting_change_id"] if family in _CHANGE_SCORED else None,
        method=output.affected_method == expected["affected_method"] if family in _METHOD_SCORED else None,
        operation=output.proposed_operation.value in expected["acceptable_operations"],  # type: ignore[operator]
        abstained=output.hypothesis is Hypothesis.UNKNOWN,
    )
    return {
        "answered": True, "hypothesis_correct": checks.hypothesis, "change_correct": checks.change,
        "method_correct": checks.method, "operation_acceptable": checks.operation, "abstained": checks.abstained,
        "outcome_correct": _outcome_correct(case, output, checks),
        "wrong_recovery_proposal": (output.proposed_operation is Operation.REPROCESS_INBOX and not checks.operation),
    }


@dataclass(frozen=True)
class _Checks:
    """The component checks of one answer; change and method are None where the family does not score them."""

    hypothesis: bool
    change: bool | None
    method: bool | None
    operation: bool
    abstained: bool


def _unanswered_score(family: str) -> dict[str, JsonValue]:
    return {"answered": False, "hypothesis_correct": False, "change_correct": None if family not in _CHANGE_SCORED else False,
            "method_correct": None if family not in _METHOD_SCORED else False, "operation_acceptable": False,
            "abstained": False, "outcome_correct": False, "wrong_recovery_proposal": False}


def _outcome_correct(case: Case, output: DiagnosisOutput, checks: _Checks) -> bool:
    """The right cause and an acceptable action for the run's family, or a correct abstention."""
    base = case.base_family
    if base in ("D1", "D3"):
        return checks.hypothesis and bool(checks.change) and checks.operation
    if base == "D2":
        return checks.hypothesis and bool(checks.method) and checks.operation and _spares_innocent_deploy(case, output)
    if base == "D4":  # stopping or refreshing without naming the cause is a correct abstention here
        return (checks.hypothesis and checks.operation) or (
            checks.abstained and output.proposed_operation in (Operation.ESCALATE, Operation.REFRESH_PROVIDER_STATE))
    # D5 and D5-style D6: no degradation claim
    return ((checks.hypothesis or checks.abstained) and output.proposed_operation is Operation.NO_ACTION
            and _spares_innocent_deploy(case, output))


def _spares_innocent_deploy(case: Case, output: DiagnosisOutput) -> bool:
    """D6 runs must never blame the innocent deploy; other families have no such constraint."""
    return case.family != "D6" or output.faulting_change_id in (CHOICE_NONE, CHOICE_UNKNOWN)


# --------------------------------------------------------------------------- systems


@dataclass
class SystemAnswer:
    system: str
    output: DiagnosisOutput | None
    status: str  # validated | needs_review | invalid
    latency_ms: int
    reasons: tuple[str, ...] = ()
    unsafe_proposals: list[dict[str, JsonValue]] = field(default_factory=list)
    repairs: int = 0
    calls: list[dict[str, JsonValue]] = field(default_factory=list)
    run: diagnosis.DiagnosisRun | None = None

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "status": self.status, "latency_ms": self.latency_ms, "reasons": list(self.reasons),
            "output": None if self.output is None else self.output.model_dump(mode="json"),
            "unsafe_proposals": self.unsafe_proposals, "repairs": self.repairs,
        }


def run_rules(built: EvidencePacket) -> SystemAnswer:
    started = time.perf_counter()
    output = rules.diagnose(built)
    elapsed = int((time.perf_counter() - started) * 1000)
    try:
        diagnosis.validate_diagnosis(output.model_dump(mode="json"), built, enforce_policies=True)
    except diagnosis.DiagnosisContractError as invalid:
        return SystemAnswer("rules", None, "invalid", elapsed, reasons=tuple(invalid.reasons),
                            unsafe_proposals=[v.model_dump(mode="json") for v in invalid.violations])
    return SystemAnswer("rules", output, "validated", elapsed)


def run_model(built: EvidencePacket, client: diagnosis.OllamaClient, *, think: bool) -> SystemAnswer:
    result = diagnosis.diagnose(built, client, think=think, deadline_s=DEADLINE_S)
    calls = [
        {"call_index": call.call_index, "wall_ms": call.wall_ms, "error": call.error,
         "rejection_reasons": list(call.rejection_reasons),
         "reply": None if call.reply is None else call.reply.model_dump(mode="json"),
         "messages": [message.model_dump() for message in call.messages]}
        for call in result.calls
    ]
    return SystemAnswer(
        "model", result.output, result.status.value, result.elapsed_ms, reasons=result.reasons,
        unsafe_proposals=[proposal.model_dump(mode="json") for proposal in result.unsafe_proposals],
        repairs=result.repairs, calls=calls, run=result,
    )


# --------------------------------------------------------------------------- the run


@dataclass(frozen=True)
class RunnerConfig:
    split: str
    data_dir: Path
    systems: tuple[str, ...]
    model: str
    think: bool
    output_dir: Path
    label: str
    limit: int | None = None
    manifest_path: Path = families.MANIFEST_PATH


def evaluate(config: RunnerConfig) -> dict[str, JsonValue]:
    """Diagnose, score and (for reprocess proposals) recover every run of the split; write the outputs."""
    manifest = families.load_manifest(config.manifest_path)
    cases = load_cases(config.data_dir, manifest, config.split)[: config.limit]
    run_dir = config.output_dir / "runs" / config.label
    if (run_dir / "results.json").exists():
        raise FileExistsError(f"{run_dir} already holds results; evaluation outputs are never overwritten")
    run_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / "traces").mkdir(parents=True, exist_ok=True)
    client = _model_client(config)
    environment = describe_environment(client, think=config.think if client else None,
                                       deadline_s=DEADLINE_S if client else None, systems=config.systems)
    model_digest = environment["model"]["model_digest"] if client else None  # type: ignore[index]
    data_db = config.data_dir / "workbench.sqlite"
    results: list[JsonValue] = []
    ledger: list[JsonValue] = []
    with closing(storage.connect(data_db)) as conn, \
            (config.output_dir / "traces" / f"{config.label}.jsonl").open("x", encoding="utf-8") as trace:
        for index, case in enumerate(cases):
            built = build_case_packet(conn, case)
            answers = _answers(config, built, client)
            case_result, entries = _scored_case(config, case, index, built, answers, data_db, model_digest)
            results.append(case_result)
            ledger.extend(entries)
            trace.write(_trace_line(config.label, case, built, answers))
            trace.flush()
            print(_progress(index, len(cases), case, case_result), file=sys.stderr, flush=True)
    if client is not None:
        client.close()
    document: dict[str, JsonValue] = {
        "label": config.label, "split": config.split, "systems": list(config.systems),
        "model": config.model if client else None, "think": config.think if client else None,
        "cases": results,
    }
    _write(run_dir / "results.json", document)
    _write(run_dir / "recovery_ledger.json", {"label": config.label, "entries": ledger})
    _write(run_dir / "environment.json", environment)
    _write(run_dir / DATA_MANIFEST_FILE, json.loads((config.data_dir / DATA_MANIFEST_FILE).read_text(encoding="utf-8")))
    return document


def _model_client(config: RunnerConfig) -> diagnosis.OllamaClient | None:
    """The local model's client when the model is evaluated, unloaded so the first case starts cold."""
    if "model" not in config.systems:
        return None
    client = diagnosis.OllamaClient(model=config.model, options={**diagnosis.DEFAULT_OPTIONS, **QWEN_SAMPLING})
    client.unload()  # the first case starts cold, so cold and warm latency are both measured
    return client


def _answers(config: RunnerConfig, built: EvidencePacket, client: diagnosis.OllamaClient | None) -> dict[str, SystemAnswer]:
    answers: dict[str, SystemAnswer] = {}
    if "rules" in config.systems:
        answers["rules"] = run_rules(built)
    if client is not None:
        answers["model"] = run_model(built, client, think=config.think)
    return answers


def _scored_case(
    config: RunnerConfig, case: Case, index: int, built: EvidencePacket, answers: Mapping[str, SystemAnswer],
    data_db: Path, model_digest: object,
) -> tuple[dict[str, JsonValue], list[JsonValue]]:
    """The case's result record and its recovery ledger entries, one per final reprocess_inbox proposal."""
    case_result: dict[str, JsonValue] = {
        "run_id": case.run_id, "seed": case.seed, "family": case.family, "variant": case.variant,
        "params": dict(case.params), "expected": dict(case.expected), "order": index,
    }
    entries: list[JsonValue] = []
    for system, answer in answers.items():
        record = {**answer.as_json(), "score": score(case, answer.output)}
        if answer.output is not None and answer.output.proposed_operation is Operation.REPROCESS_INBOX:
            entry = _recover(config, case, built, answer, data_db, model_digest)
            entries.append(entry)
            record["recovery_status"] = entry["status"]
        case_result[system] = record
    return case_result, entries


def _trace_line(label: str, case: Case, built: EvidencePacket, answers: Mapping[str, SystemAnswer]) -> str:
    return json.dumps({
        "label": label, "run_id": case.run_id, "packet_hash": built.packet_hash(),
        "packet": built.model_dump(mode="json"),
        "answers": {system: {**answer.as_json(), "calls": answer.calls} for system, answer in answers.items()},
    }, sort_keys=True) + "\n"


def _recover(config: RunnerConfig, case: Case, built: EvidencePacket, answer: SystemAnswer, data_db: Path,
             model_digest: object) -> dict[str, JsonValue]:
    """Recovery outcome for one proposal; an unexpected exception is recorded as an executed error."""
    assert answer.output is not None
    try:
        return recovery.recover(
            data_db=data_db, work_dir=config.data_dir / "recovery" / config.label, run_id=case.run_id,
            system=answer.system, output=answer.output, model_run=answer.run,
            model_id=config.model if answer.system == "model" else None,
            model_digest=str(model_digest) if answer.system == "model" else None,
            packet_hash=built.packet_hash(), oracle=case.oracle, provider_records=case.provider_records,
        )
    except Exception as error:  # the evaluation measures executed errors, so it records them instead of stopping
        return {"run_id": case.run_id, "system": answer.system, "blamed_change": answer.output.faulting_change_id,
                "status": "error", "error": f"{type(error).__name__}: {error}"}


def _progress(index: int, total: int, case: Case, result: Mapping[str, JsonValue]) -> str:
    parts = [f"[{index + 1}/{total}] {case.run_id} {case.family} {case.variant}"]
    for system in SYSTEMS:
        record = result.get(system)
        if isinstance(record, dict):
            parts.append(f"{system}: {'ok' if record['score']['outcome_correct'] else 'MISS'} "  # type: ignore[index]
                         f"{record['status']} {record['latency_ms']} ms")
    return " | ".join(parts)


def _write(path: Path, document: JsonValue) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recovery_workbench.evaluation.run",
                                     description="Run the M4 evaluation on one generated split.")
    parser.add_argument("--split", required=True, choices=("dev", "test"))
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--systems", default="rules,model")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--think", choices=("off", "on"), default="off")
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation"))
    parser.add_argument("--label", default=None)
    parser.add_argument("--limit", type=int, default=None, help="first N runs only (smoke checks)")
    args = parser.parse_args(argv)
    systems = tuple(item.strip() for item in args.systems.split(",") if item.strip())
    unknown = sorted(set(systems) - set(SYSTEMS))
    if not systems or unknown:
        parser.error(f"--systems takes a comma list of {SYSTEMS}, got {args.systems!r}")
    label = args.label or (f"{args.split}_think_{args.think}" if "model" in systems else f"{args.split}_rules")
    document = evaluate(RunnerConfig(split=args.split, data_dir=args.data_dir, systems=systems, model=args.model,
                                     think=args.think == "on", output_dir=args.output_dir, label=label, limit=args.limit))
    correct = {system: sum(bool(case[system]["score"]["outcome_correct"]) for case in document["cases"]  # type: ignore[index]
                           if system in case) for system in systems}  # type: ignore[operator]
    print(json.dumps({"label": label, "cases": len(document["cases"]), "outcome_correct": correct}))  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
