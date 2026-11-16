"""The val-7 repair measurement's summary, offline: report.json from the live stage's answers.json.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<work>/app/src .venv/bin/python summarize.py \\
        --code-export <work>/app --val6-diagnosis <val-6 diagnosis.py> --output-dir <out>

Every verdict is recomputed from the stored reply texts, val-7's with the package under test and val-6's with val-6's
own diagnosis.py, and every correctness from the stored expected answers; any disagreement with what the stage
recorded stops the summary. The metric and the decision rule are PREREGISTRATION.md's, applied as written:

* a **trigger** is a first answer val-6 accepts and val-7 refuses;
* it is **recovered** when its repaired answer passes val-7 and its outcome correctness is not worse than the first's;
* **R** is the share of triggers recovered over S and C together, identical outputs counted once: two diagnoses of one
  packet whose first replies and repair replies are byte-identical are one output, within S and across S and C.
  The same figures counted without that rule are reported beside them, with whether the decision would differ.

Writes report.json and never overwrites it. It calls no model and opens no database.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import diagnosis
from recovery_workbench.contracts import sha256_hex

import val7_repair_design as design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

DECISION_ROWS = {
    1: "val-7 as built: both rules refuse, with one repair",
    2: "rule (a) refuses; rule (b) becomes a visible flag on an accepted answer, not a refusal",
    3: "val-7 as built",
    4: "as in the second row: rule (a) refuses; rule (b) becomes a visible flag on an accepted answer, not a refusal",
}
MIN_TRIGGERS = 10
MIN_R = 0.9


def decide(triggers: int, recovered: int) -> dict[str, JsonValue]:
    """The pre-registration's table, row by row."""
    share = recovered / triggers if triggers else None
    if triggers >= MIN_TRIGGERS:
        row = 1 if share is not None and share >= MIN_R else 2
    else:
        row = 3 if recovered == triggers else 4  # with no trigger at all, every trigger is (vacuously) recovered
    return {"triggers": triggers, "recovered": recovered, "R": share, "row": row, "ship": DECISION_ROWS[row]}


def _content(call: Mapping[str, JsonValue] | None) -> str | None:
    reply = None if call is None else call.get("reply")
    return None if not isinstance(reply, dict) else str(reply["content"])


def identity(answer: Mapping[str, JsonValue]) -> str:
    """One output: the packet, its first reply and its repair reply, byte for byte."""
    first = _content(answer["first"]["call"])  # type: ignore[index]
    repair = None if answer["repair"] is None else _content(answer["repair"]["call"])  # type: ignore[index]
    return sha256_hex(json.dumps([answer["packet_hash"], first, repair]).encode("utf-8"))


def rechecked(answers: Sequence[Mapping[str, JsonValue]], items: Mapping[str, design.SampleCase], val6) -> list[str]:
    """Where a recorded verdict or score differs from one recomputed now; an empty list means none does."""
    problems = []
    for answer in answers:
        item = items[str(answer["packet_hash"])]
        first = answer["first"]
        content = _content(first["call"])  # type: ignore[arg-type, index]
        old = design.verdict(val6, content, item.packet)
        if (None if old is None else old.as_json()) != first["val6"]:  # type: ignore[index]
            problems.append(f"{answer['order']}: val-6's verdict on the first answer")
        for name, part in (("first", first), ("repair", answer["repair"])):
            if part is None or part["call"] is None or part["call"]["reply"] is None:  # type: ignore[index]
                continue
            text = _content(part["call"])  # type: ignore[arg-type, index]
            reasons = tuple(part["call"]["rejection_reasons"])  # type: ignore[index]
            try:
                json.loads(str(text))
            except json.JSONDecodeError:
                continue
            new = design.verdict(diagnosis, text, item.packet)
            if new is None or new.reasons != reasons or part["val7"] != new.as_json():  # type: ignore[index]
                problems.append(f"{answer['order']}: val-7's verdict on the {name} answer")
            if part["score"] != design.scored(item.case, design.output_of(text)):  # type: ignore[index]
                problems.append(f"{answer['order']}: the {name} answer's score")
    return problems


def _latency(values: Sequence[int]) -> dict[str, JsonValue]:
    if not values:
        return {"count": 0}
    return {"count": len(values), "median_ms": statistics.median(values), "mean_ms": round(statistics.fmean(values)),
            "min_ms": min(values), "max_ms": max(values)}


def _outcome(score: Mapping[str, JsonValue] | None) -> bool:
    return bool(score and score["outcome_correct"])


def summarize(document: Mapping[str, JsonValue], items: Mapping[str, design.SampleCase], val6) -> dict[str, JsonValue]:
    answers: list[Mapping[str, JsonValue]] = document["answers"]  # type: ignore[assignment]
    problems = rechecked(answers, items, val6)
    if problems:
        raise SystemExit(f"recorded verdicts or scores differ from recomputed ones: {problems[:10]}")
    triggers = [answer for answer in answers if answer["trigger"]]
    distinct: dict[str, Mapping[str, JsonValue]] = {}
    for answer in triggers:
        distinct.setdefault(identity(answer), answer)
    by_sample = Counter(str(answer["sample"]) for answer in triggers)
    s_pairs: dict[str, list[str]] = {}
    for answer in answers:
        if answer["sample"] == "S":
            s_pairs.setdefault(str(answer["packet_hash"]), []).append(identity(answer))
    c_answers = [answer for answer in answers if answer["sample"] == "C"]
    repaired_triggers = [int(answer["repair"]["call"]["wall_ms"]) for answer in triggers  # type: ignore[index]
                         if answer["repair"] is not None]
    every_repair = [int(answer["repair"]["call"]["wall_ms"]) for answer in answers  # type: ignore[index]
                    if answer["repair"] is not None]
    finals: dict[str, Counter[str]] = {}
    for sample in ("S", "C"):
        group = [answer for answer in answers if answer["sample"] == sample and answer["val6_run"] is not None]
        tally: Counter[str] = Counter()
        for answer in group:
            val7_ok = _outcome(answer["final"]["score"])  # type: ignore[index]
            val6_ok = _outcome(answer["val6_run"]["score"])  # type: ignore[index]
            tally["val-7 outcome correct"] += val7_ok
            tally["val-6 run outcome correct"] += val6_ok
            tally[f"val-6 {'right' if val6_ok else 'wrong'} -> val-7 {'right' if val7_ok else 'wrong'}"] += 1
        tally["diagnoses with a first answer"] = len(group)
        finals[sample] = tally
    decided, raw = (decide(len(distinct), sum(bool(answer["recovered"]) for answer in distinct.values())),
                    decide(len(triggers), sum(bool(answer["recovered"]) for answer in triggers)))
    return {
        "preregistration": document["preregistration"],
        "answers_sha256": None,  # filled in by main, from the file's bytes
        "code_under_test": document["code_under_test"], "model": document["model"],
        "application_settings": document["application_settings"],
        "diagnoses": {"S": sum(answer["sample"] == "S" for answer in answers), "C": len(c_answers),
                      "final_status": dict(Counter(f"{answer['sample']} {answer['final']['status']}"  # type: ignore[index]
                                                   for answer in answers)),
                      "model_call_errors": sum(1 for answer in answers for part in (answer["first"], answer["repair"])
                                               if part is not None and part["call"] is not None  # type: ignore[index]
                                               and part["call"]["error"]),  # type: ignore[index]
                      "first_answer": dict(Counter(
                          f"{answer['sample']} val-6 {'accepts' if (answer['first']['val6'] or {}).get('accepted') else 'refuses'}, "  # type: ignore[index, union-attr]
                          f"val-7 {'accepts' if (answer['first']['val7'] or {}).get('accepted') else 'refuses'}"  # type: ignore[index, union-attr]
                          for answer in answers))},
        "triggers": {"counted_once": len(distinct), "every_diagnosis": len(triggers), "by_sample": dict(by_sample),
                     "by_rule": dict(Counter("+".join(answer["first"]["val7_rules"]) for answer in distinct.values())),  # type: ignore[index, arg-type]
                     "list": [{"order": answer["order"], "sample": answer["sample"], "pass": answer["pass"],
                               "label": answer["label"], "packet_hash": answer["packet_hash"],
                               "rules": answer["first"]["val7_rules"],  # type: ignore[index]
                               "first_outcome_correct": _outcome(answer["first"]["score"]),  # type: ignore[index]
                               "first_change_correct": (answer["first"]["score"] or {}).get("change_correct"),  # type: ignore[index, union-attr]
                               "repair_passes_val7": bool(answer["repair"] and (answer["repair"]["val7"] or {}).get("accepted")),  # type: ignore[index, union-attr]
                               "repair_outcome_correct": None if answer["repair"] is None
                               else _outcome(answer["repair"]["score"]),  # type: ignore[index]
                               "recovered": answer["recovered"], "counted": identity(answer) in distinct
                               and distinct[identity(answer)] is answer,
                               "repair_call_ms": None if answer["repair"] is None
                               else answer["repair"]["call"]["wall_ms"]}  # type: ignore[index]
                              for answer in triggers]},
        "decision": {**decided, "rule": "PREREGISTRATION.md, fixed by the owner on 2026-11-13",
                     "counted_without_the_identical_output_rule": raw,
                     "same_decision_either_way": raw["row"] == decided["row"]},
        "reported_not_decided": {
            "trigger_rate_in_C": {"triggers": by_sample.get("C", 0), "diagnoses": len(c_answers),
                                  "share": by_sample.get("C", 0) / len(c_answers) if c_answers else None},
            "repair_latency_triggers": _latency(repaired_triggers),
            "repair_latency_every_repair": _latency(every_repair),
            "final_correctness_against_a_val6_run_of_the_same_first_answers": {k: dict(v) for k, v in finals.items()},
            "val6_run_live_repair_calls": sum(len(answer["val6_run"]["live_calls"]) for answer in answers  # type: ignore[index]
                                              if answer["val6_run"] is not None),
        },
        "determinism": {"S_packets": len(s_pairs),
                        "S_packets_with_identical_outputs_in_both_passes": sum(1 for ids in s_pairs.values()
                                                                               if len(set(ids)) == 1)},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise the val-7 repair measurement.")
    parser.add_argument("--code-export", type=Path, required=True)
    parser.add_argument("--val6-diagnosis", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repository-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    root: Path = (args.repository_dir or args.code_export).resolve()
    answers_path, report_path = args.output_dir / "answers.json", args.output_dir / "report.json"
    if report_path.exists():
        parser.error(f"{report_path} exists; it is never overwritten")
    val6 = design.val6_module(args.val6_diagnosis)
    raw = answers_path.read_bytes()
    document = json.loads(raw)
    items = {item.packet_hash: item for item in (*design.sample_s(root, val6), *design.sample_c(root))}
    report = summarize(document, items, val6) | {"answers_sha256": sha256_hex(raw)}
    with report_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"decision": report["decision"], "triggers": {k: v for k, v in report["triggers"].items() if k != "list"},
                      "trigger_rate_in_C": report["reported_not_decided"]["trigger_rate_in_C"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
