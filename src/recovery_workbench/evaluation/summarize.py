"""Write ``summary.md``, the citation spot-check sheet and the top-level copies of the frozen TEST run.

    python -m recovery_workbench.evaluation.summarize --output-dir evaluation --frozen test_frozen \\
        [--rerun test_rerun] [--dev dev_think_off,dev_think_on]

Every table carries raw counts and denominators. Where rules beat or tie the model, the summary
says so plainly.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench.evaluation.freeze import load_run

SPOT_CHECK_SEED = 20261112
FAMILY_NAMES = {"D1": "worker regression after a deploy", "D2": "provider/method degradation",
                "D3": "receiver rejects valid deliveries", "D4": "ambiguous or late-authorised state",
                "D5": "no real incident", "D6": "innocent deploy near D2/D5"}

Case = Mapping[str, JsonValue]


def variant_key(case: Case) -> str:
    params: Mapping[str, JsonValue] = case["params"]  # type: ignore[assignment]
    family, variant = case["family"], case["variant"]
    if family == "D1":
        return f"D1 {variant}, onset {params.get('onset', 'prompt')}, categories {'on' if params.get('categories') else 'off'}"
    if family == "D2":
        return f"D2 {params['magnitude']} drop"
    if family == "D6":
        return f"D6 near {'method drop' if params['base'] == 'D2' else 'mix shift'}"
    return f"{family} {variant}"


def _correct(case: Case, system: str, metric: str = "outcome_correct") -> bool | None:
    record = case.get(system)
    if not isinstance(record, dict):
        return None
    value = record["score"][metric]
    return None if value is None else bool(value)


def _ratio(cases: Sequence[Case], system: str, metric: str = "outcome_correct") -> str:
    scored = [value for case in cases if (value := _correct(case, system, metric)) is not None]
    return f"{sum(scored)}/{len(scored)}" if scored else "-"


def _verdict(cases: Sequence[Case]) -> str:
    rules = sum(bool(_correct(case, "rules")) for case in cases)
    model = sum(bool(_correct(case, "model")) for case in cases)
    if rules > model:
        return "**rules beat the model**"
    if rules == model:
        return "**rules tie the model**"
    return "model beats rules"


def _grouped(cases: Sequence[Case], key: Callable[[Case], str]) -> dict[str, list[Case]]:
    groups: dict[str, list[Case]] = {}
    for case in cases:
        groups.setdefault(key(case), []).append(case)
    return dict(sorted(groups.items()))


def _latency(cases: Sequence[Case]) -> str:
    values = [int(case["model"]["latency_ms"]) for case in cases if isinstance(case.get("model"), dict)]  # type: ignore[index]
    if not values:
        return "no model calls"
    warm = values[1:] or values
    p90 = sorted(warm)[max(0, round(0.9 * len(warm)) - 1)]
    return (f"cold (first case) {values[0] / 1000:.1f} s; warm median {statistics.median(warm) / 1000:.1f} s, "
            f"p90 {p90 / 1000:.1f} s, max {max(warm) / 1000:.1f} s over {len(warm)} warm cases")


def _failures(cases: Sequence[Case], system: str) -> list[str]:
    lines = []
    for case in cases:
        record = case.get(system)
        if not isinstance(record, dict) or record["score"]["outcome_correct"]:
            continue
        expected = case["expected"]
        output = record["output"] or {}
        got = (f"status {record['status']}" if not output else
               f"{output['hypothesis']} / change {output['faulting_change_id']} / method {output['affected_method']} "
               f"/ {output['proposed_operation']}")
        lines.append(f"- {case['run_id']} ({variant_key(case)}): got {got}; expected {expected['hypothesis']} / change "
                     f"{expected['faulting_change_id']} / method {expected['affected_method']} / one of "
                     f"{expected['acceptable_operations']}")
    return lines or ["- none"]


def _recovery_table(ledger: Sequence[Mapping[str, JsonValue]]) -> list[str]:
    lines = ["| System | Reprocess proposals | Executed | Blocked (safe) | No targets | Errors | Fulfilled = oracle set | "
             "Restored value (paise) | New capture (paise) | Duplicate fulfilments | Retried execute changed nothing |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for system in ("rules", "model"):
        entries = [entry for entry in ledger if entry["system"] == system]
        executed = [entry for entry in entries if entry["status"] == "succeeded"]
        lines.append(
            f"| {system} | {len(entries)} | {len(executed)} | {sum(e['status'] == 'blocked' for e in entries)} | "
            f"{sum(e['status'] == 'no_targets' for e in entries)} | {sum(e['status'] == 'error' for e in entries)} | "
            f"{sum(bool(e.get('matches_oracle')) for e in executed)}/{len(executed)} | "
            f"{sum(int(e.get('restored_processing_value_paise', 0)) for e in executed)} | "  # type: ignore[arg-type]
            f"{sum(int(e.get('additional_captured_paise', 0)) for e in executed)} | "  # type: ignore[arg-type]
            f"{sum(int(e.get('duplicate_fulfilments', 0)) for e in executed)} | "  # type: ignore[arg-type]
            f"{sum(bool(e.get('retry_replayed')) for e in executed)}/{len(executed)} |")
    return lines


def build_summary(output_dir: Path, frozen: str, rerun: str | None, dev: Sequence[str]) -> str:
    results, environment = load_run(output_dir, frozen)
    ledger = json.loads((output_dir / "runs" / frozen / "recovery_ledger.json").read_text(encoding="utf-8"))["entries"]
    settings = json.loads((output_dir / "frozen_settings.json").read_text(encoding="utf-8"))
    cases = results["cases"]
    rerun_cases = load_run(output_dir, rerun)[0]["cases"] if rerun else None
    model = environment["model"]
    lines = [
        "# Evaluation summary (M4)",
        "",
        f"Frozen TEST split: {len(cases)} runs, evaluated once with the frozen setting (`{frozen}`). Rules baseline "
        f"`{environment['versions']['rules']}` versus `{model['model']}` (digest `{model['model_digest'][:12]}`), "
        f"thinking **{'on' if model['think'] else 'off'}**, prompt `{model['prompt_version']}`, validator "
        f"`{environment['versions']['validator']}`, packet `{environment['versions']['packet']}`.",
        "",
        "**Asymmetry, stated up front:** the rules baseline received one tuning pass on the DEV split; the model "
        "received none (no prompt or validator change in M4). This favours rules, so any place the model beats rules "
        "is conservative; places where rules win partly reflect that pass. The simulator, the manifest and the rules "
        "were written by the same agent, so no part of this is blind. All numbers describe this synthetic simulator only.",
        "",
        "## Headline: outcome correct on TEST, by family",
        "",
        "| Family | Runs | Rules | Model" + (" (run 1)" if rerun_cases else "") + (" | Model (run 2)" if rerun_cases else "")
        + " | Verdict |",
        "|---|---:|---:|---:|" + ("---:|" if rerun_cases else "") + "---|",
    ]
    by_family = _grouped(cases, lambda case: str(case["family"]))
    rerun_by_family = _grouped(rerun_cases, lambda case: str(case["family"])) if rerun_cases else {}
    for family, group in by_family.items():
        extra = f" | {_ratio(rerun_by_family.get(family, []), 'model')}" if rerun_cases else ""
        lines.append(f"| {family} {FAMILY_NAMES[family]} | {len(group)} | {_ratio(group, 'rules')} | "
                     f"{_ratio(group, 'model')}{extra} | {_verdict(group)} |")
    total_extra = f" | {_ratio(rerun_cases, 'model')}" if rerun_cases else ""
    lines.append(f"| **All** | {len(cases)} | {_ratio(cases, 'rules')} | {_ratio(cases, 'model')}{total_extra} | "
                 f"{_verdict(cases)} |")
    lines += ["", "Outcome correct = the right cause and an acceptable action (faulting deploy for D1/D3/D6, method "
              "for D2/D6), or a correct abstention (D4: unknown with refresh/escalate; D5: no claim with no action). "
              "A model answer that ended in needs_review counts as not correct.", "",
              "## By variant (TEST)", "", "| Variant | Runs | Rules | Model | Verdict |", "|---|---:|---:|---:|---|"]
    for key, group in _grouped(cases, variant_key).items():
        lines.append(f"| {key} | {len(group)} | {_ratio(group, 'rules')} | {_ratio(group, 'model')} | {_verdict(group)} |")
    lines += ["", "## Component scores (TEST)", "", "| Measure | Rules | Model |", "|---|---:|---:|"]
    for metric, label in (("hypothesis_correct", "Hypothesis"), ("change_correct", "Faulting change (D1/D3/D6)"),
                          ("method_correct", "Affected method (D2/D6)"), ("operation_acceptable", "Acceptable operation"),
                          ("abstained", "Abstained (unknown)")):
        lines.append(f"| {label} | {_ratio(cases, 'rules', metric)} | {_ratio(cases, 'model', metric)} |")
    model_unsafe = sum(len(case["model"]["unsafe_proposals"]) for case in cases if "model" in case)
    rules_unsafe = sum(len(case["rules"]["unsafe_proposals"]) for case in cases if "rules" in case)
    wrong = {system: sum(bool(case[system]["score"]["wrong_recovery_proposal"]) for case in cases if system in case)
             for system in ("rules", "model")}
    needs_review = sum(case["model"]["status"] != "validated" for case in cases if "model" in case)
    lines += ["", "## Safety (TEST)", "",
              f"- Unsafe proposals refused by the operation or change policy, counted even when a repair replaced them: "
              f"rules {rules_unsafe}, model {model_unsafe} (all blocked; none reached recovery).",
              f"- Final reprocess proposals the oracle does not accept (allowed by policy, wrong for the run): rules "
              f"{wrong['rules']}, model {wrong['model']}. Their recovery outcome is in the ledger below.",
              f"- Model answers that ended in needs_review: {needs_review}.",
              "", "## Recovery executed for reprocess proposals (TEST)", "", *_recovery_table(ledger),
              "", "Executed errors are reported apart from proposals. A blocked recovery means the fix for the blamed "
              "deploy's module did not validate, or no allowlisted fix exists for it: the safe failure mode.",
              "", "## Latency (TEST)", "", f"- Model: {_latency(cases)}.",
              "- Rules: under 10 ms per run.", "", "## Failures (TEST)", "", "### Rules", *_failures(cases, "rules"),
              "", "### Model", *_failures(cases, "model")]
    if rerun_cases:
        lines += _variance_lines(cases, rerun_cases)
    lines += _dev_comparison_lines(output_dir, settings, dev)
    lines += ["", "## Files", "", "`results.json` (per case, both systems), `recovery_ledger.json`, `environment.json`, "
              "`data_manifest.json`, `frozen_settings.json`, `traces/*.jsonl` (packets, model inputs and outputs, "
              "validation), `citation_spot_check.md` (for the owner to fill in), `runs/<label>/` (every run)."]
    return "\n".join(lines) + "\n"


def _variance_lines(cases: Sequence[Case], rerun_cases: Sequence[Case]) -> list[str]:
    changed = [case["run_id"] for case, again in zip(cases, rerun_cases)
               if _correct(case, "model") != _correct(again, "model")]
    return ["", "## Run-to-run variance (TEST, model only, same frozen setting)", "",
            f"- Run 1 {_ratio(cases, 'model')}, run 2 {_ratio(rerun_cases, 'model')} outcome correct; "
            f"{len(changed)} runs changed correctness between the two runs: {', '.join(changed) or 'none'}.",
            f"- Run 2 latency: {_latency(rerun_cases)}.", "- Both runs are reported; neither was selected."]


def _dev_comparison_lines(output_dir: Path, settings: dict, dev: Sequence[str]) -> list[str]:
    lines = ["", "## DEV comparison and the frozen setting", "", f"Pre-registered rule: {settings['selection_rule']}.", "",
             "| Setting | Outcome correct | Unsafe proposals | Needs review | Cold latency | Warm median |",
             "|---|---:|---:|---:|---:|---:|"]
    for think, figures in settings["dev_figures"].items():
        lines.append(f"| think {think} | {figures['outcome_correct']}/{figures['cases']} | {figures['unsafe_proposals']} | "
                     f"{figures['needs_review']} | {figures['cold_latency_ms'] / 1000:.1f} s | "
                     f"{figures['warm_median_latency_ms'] / 1000:.1f} s |")
    lines += ["", f"Chosen: thinking **{settings['chosen_think']}**, frozen in `frozen_settings.json` before TEST was generated."]
    for label in dev:
        dev_cases = load_run(output_dir, label)[0]["cases"]
        lines += ["", f"DEV `{label}` by family: " + "; ".join(
            f"{family} rules {_ratio(group, 'rules')} vs model {_ratio(group, 'model')}"
            for family, group in _grouped(dev_cases, lambda case: str(case["family"])).items())]
    return lines


def build_spot_check(output_dir: Path, frozen: str, count: int = 10) -> str:
    lines_in = (output_dir / "traces" / f"{frozen}.jsonl").read_text(encoding="utf-8").splitlines()
    traces = [json.loads(line) for line in lines_in]
    validated = [trace for trace in traces if trace["answers"].get("model", {}).get("status") == "validated"]
    sample = random.Random(SPOT_CHECK_SEED).sample(validated, min(count, len(validated)))  # NOSONAR(S2245) seeded so the owner's spot-check sample is reproducible; not a security use
    chosen = sorted(sample, key=lambda trace: trace["run_id"])
    lines = ["# Citation spot check (owner to fill in)", "",
             f"{len(chosen)} validated model diagnoses from `{frozen}`, chosen with seed {SPOT_CHECK_SEED}. For each "
             "cited item, judge whether its evidence supports the rationale's claim. EVALUATION.md §9 forbids grading "
             "this with another model, so the last column is left empty.", ""]
    for trace in chosen:
        output = trace["answers"]["model"]["output"]
        items = {item["evidence_id"]: item for item in trace["packet"]["items"]}
        lines += [f"## {trace['run_id']}: {output['hypothesis']} / {output['proposed_operation']}", "",
                  f"Rationale: {output['rationale']}", "", "| Cited | Evidence | Supports the claim? (yes/no) |", "|---|---|---|"]
        for cited in output["supporting_evidence_ids"]:
            item = items[cited]
            excerpt = (json.dumps(item["facts"], sort_keys=True)[:300] + " " + (item.get("text") or "")[:200]).replace("|", "/")
            lines.append(f"| {cited} | {item['title']}: {' '.join(excerpt.split())} |  |")
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recovery_workbench.evaluation.summarize")
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation"))
    parser.add_argument("--frozen", required=True)
    parser.add_argument("--rerun", default=None)
    parser.add_argument("--dev", default="dev_think_off,dev_think_on")
    args = parser.parse_args(argv)
    dev = [label for label in args.dev.split(",") if label]
    for name in ("results.json", "recovery_ledger.json", "environment.json", "data_manifest.json"):
        shutil.copyfile(args.output_dir / "runs" / args.frozen / name, args.output_dir / name)
    (args.output_dir / "summary.md").write_text(build_summary(args.output_dir, args.frozen, args.rerun, dev), encoding="utf-8")
    sheet = args.output_dir / "citation_spot_check.md"
    if _write_unless_filled_in(sheet, build_spot_check(args.output_dir, args.frozen)):
        print(f"wrote {args.output_dir / 'summary.md'} and {sheet}")
    else:
        print(f"wrote {args.output_dir / 'summary.md'}; kept {sheet} unchanged because it has been filled in "
              "(it differs from a freshly generated blank sheet); move it aside to regenerate a blank one")
    return 0


def _write_unless_filled_in(sheet: Path, blank: str) -> bool:
    """Write the blank spot-check sheet unless the existing one differs from it; True if written.

    The owner fills the sheet in by hand, so a re-summarise must never overwrite their judgements.
    """
    if sheet.exists() and sheet.read_text(encoding="utf-8") != blank:
        return False
    sheet.write_text(blank, encoding="utf-8")
    return True


if __name__ == "__main__":
    sys.exit(main())
