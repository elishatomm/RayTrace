"""Choose and freeze the model setting from the DEV runs, before the TEST split exists.

    python -m recovery_workbench.evaluation.freeze --output-dir evaluation --off dev_think_off --on dev_think_on

The rule was fixed before any DEV result was seen (PLAN.md §5 M4): the setting with more DEV
runs whose outcome is correct wins; on a tie, the one with fewer unsafe proposals; then the one
with the lower warm median latency. ``frozen_settings.json`` records every version, digest and
option of the winner, and is written exactly once.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench.evaluation.run import DEADLINE_S

SELECTION_RULE = ("higher DEV outcome-correct count; on a tie, fewer unsafe proposals (blocked included); "
                  "then lower warm median latency")


def load_run(output_dir: Path, label: str) -> tuple[dict, dict]:
    run_dir = output_dir / "runs" / label
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    return results, environment


def model_figures(results: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Outcome, unsafe and latency figures of the model in one run (case order is run order)."""
    cases = [case for case in results["cases"] if "model" in case]  # type: ignore[union-attr]
    latencies = [int(case["model"]["latency_ms"]) for case in cases]  # type: ignore[index]
    warm = latencies[1:]
    return {
        "cases": len(cases),
        "outcome_correct": sum(bool(case["model"]["score"]["outcome_correct"]) for case in cases),  # type: ignore[index]
        "unsafe_proposals": sum(len(case["model"]["unsafe_proposals"]) for case in cases),  # type: ignore[index]
        "needs_review": sum(case["model"]["status"] != "validated" for case in cases),  # type: ignore[index]
        "cold_latency_ms": latencies[0] if latencies else None,
        "warm_median_latency_ms": int(statistics.median(warm)) if warm else None,
    }


def choose(off: Mapping[str, JsonValue], on: Mapping[str, JsonValue]) -> str:
    """Apply the pre-registered rule; returns "off" or "on"."""
    def key(figures: Mapping[str, JsonValue]) -> tuple[int, int, int]:
        warm = figures["warm_median_latency_ms"]
        return (-int(figures["outcome_correct"]), int(figures["unsafe_proposals"]),  # type: ignore[arg-type]
                int(warm) if warm is not None else sys.maxsize)  # type: ignore[arg-type]
    return "off" if key(off) <= key(on) else "on"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recovery_workbench.evaluation.freeze")
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation"))
    parser.add_argument("--off", default="dev_think_off")
    parser.add_argument("--on", default="dev_think_on")
    args = parser.parse_args(argv)
    off_results, off_env = load_run(args.output_dir, args.off)
    on_results, on_env = load_run(args.output_dir, args.on)
    figures = {"off": model_figures(off_results), "on": model_figures(on_results)}
    chosen = choose(figures["off"], figures["on"])
    environment = off_env if chosen == "off" else on_env
    frozen: dict[str, JsonValue] = {
        "selection_rule": SELECTION_RULE,
        "dev_figures": figures,
        "chosen_think": chosen,
        "model": environment["model"],
        "deadline_s": DEADLINE_S,
        "versions": environment["versions"],
        "policy": environment["policy"],
        "frozen_before_test_generation": True,
        "note": "No prompt or validator change in M4 (prompt diag-p2). Rules had one dev tuning pass; the model none.",
    }
    path = args.output_dir / "frozen_settings.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(frozen, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"chosen_think": chosen, "dev_figures": figures}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
