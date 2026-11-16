"""Stage 3, on the host (JSON files only): the validity checks, both decisions, the adoption rules and the figures.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<scratch>/g_export_43a160f/src .venv/bin/python \\
        experiments/selective/summarize.py --code-export <scratch>/g_export_43a160f

Reads cases.json, the packet files, and each arm's ``answers_<arm>.json`` and ``traces/<arm>.jsonl``. It computes
what no arm records: R (rules-v3) and R2 (rules-v2) on every world, and every world's route, with the pinned export,
the same code the arms ran (one package sources hash). They are pure functions of a packet. The stage opens no
database, and an audit hook makes that a checked fact: a database opened during the run stops it before
``report.json`` is written. Every rule it applies, and the order in which a verdict is reached, is
PREREGISTRATION.md's, fixed before any result existed. ``report.json`` is derived: a re-run recomputes it from the
files above.

The decision logic (``world_row``, ``paired_by_entry``, ``decide_primary``, ``decide_secondary``, ``validity``) takes
plain records, so the tests drive it with hand-made answer sets and never compute a real answer on a test packet.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import diagnosis, rules, selective
from recovery_workbench.contracts import CHOICE_NONE, CHOICE_UNKNOWN, EvidencePacket, sha256_hex

import selective_design  # this folder's shared module; the stage runs as a script, so its folder is on sys.path

round1 = selective_design.round1_design
round3_summary = selective_design.round3_module("summarize")
round2_summary = round3_summary.round2_summary
round1_summary = round3_summary.round1_summary

Record = Mapping[str, JsonValue]
ArmRun = round2_summary.ArmRun
CORRECT, WRONG, ABSTAINED, REVIEW = "correct", "wrong", "abstained", "review"
CLASSES: tuple[str, ...] = (CORRECT, WRONG, ABSTAINED, REVIEW)
ABSTENTIONS = frozenset({CHOICE_NONE, CHOICE_UNKNOWN})
IMPROVED, WORSENED, TIED = "improved", "worsened", "tied"
# The systems. S-agree is the agreement variant: descriptive only, and no adoption rule refers to it.
R, S, R2, M_ALL, S_AGREE = "R", "S", "R2", "M-all", "S-agree"
SYSTEMS: tuple[str, ...] = (R, S, R2, S_AGREE, M_ALL)
DETERMINISTIC_SYSTEMS = frozenset({R, R2})  # one answer per world, counted once per model seed
SEED_KEYS: tuple[str, ...] = tuple(str(seed) for seed in selective_design.MODEL_SEEDS)
MODEL_ROUTE, RULES_ROUTE = selective_design.MODEL_ROUTE, selective_design.RULES_ROUTE
# Checked by refusal before any figure exists: a report exists only when they held.
REFUSED_UNLESS_HELD = ("preregistration", "packets", "summariser_code")
# M-all decides nothing; these checks on its arms gate whether its own figures are reported (orchestrator, open point 3).
MODEL_ALL_GATES = ("code", "preregistration", "run_records")
STRATA = ("fault", "innocent", "onset", "position")
UNSAFE_PROPOSALS = ("met by construction for final answers: policies are enforced, so val-6 refuses an unsafe "
                    "proposal and the run repairs it or ends in review; policy-blocked runs are reported per arm as a cost")


# --------------------------------------------------------------------------- what the code under test answers without a model


@dataclass(frozen=True)
class Deterministic:
    """One packet's route, rules-v3 answer and rules-v2 answer, as the code under test gives them."""

    route: Record  # selective.Route
    rules_v3: Record  # selective.RulesV3Answer
    rules_v2: Record  # {"output": rules.diagnose's answer, "validated": whether val-6 accepts it, "reasons": val-6's}

    @property
    def target(self) -> str:
        return str(self.route["target"])

    @property
    def rules_v3_output(self) -> Record:
        output: Record = self.rules_v3["output"]  # type: ignore[assignment]
        return output

    @property
    def rules_v2_output(self) -> Record | None:
        """rules-v2's final answer: none when val-6 refuses it, as the product's rules mode refuses to store it."""
        output: Record = self.rules_v2["output"]  # type: ignore[assignment]
        return output if self.rules_v2["validated"] else None


def compute_deterministic(packets: Mapping[str, EvidencePacket]) -> dict[str, Deterministic]:
    """Route, rules-v3 and rules-v2 for every packet, with the imported (checked) code under test."""
    computed: dict[str, Deterministic] = {}
    for case_id, built in packets.items():
        produced = rules.diagnose(built)
        reasons: list[JsonValue] = []
        try:
            diagnosis.validate_diagnosis(produced.model_dump(mode="json"), built)
        except diagnosis.DiagnosisContractError as refused:
            # rules-v2 predates val-6: a refused answer is no final answer here, kept with val-6's reasons.
            reasons = list(refused.reasons)
        computed[case_id] = Deterministic(
            route=selective.route(built).model_dump(mode="json"),
            rules_v3=selective.answer_rules_v3(built).model_dump(mode="json"),
            rules_v2={"output": produced.model_dump(mode="json"), "validated": not reasons, "reasons": reasons},
        )
    return computed


# --------------------------------------------------------------------------- reading the arms


def load_arm(experiment_dir: Path, arm: selective_design.Arm, cases_document: Record) -> ArmRun:
    """One arm's answers and trace, after checking they are this build's and this arm's, and cover its own worlds."""
    document = round1.read_json(experiment_dir / arm.answers_file)
    run = ArmRun(name=arm.name, document=document,
                 answers=round2_summary.by_case(document["answers"], arm.answers_file),  # type: ignore[arg-type]
                 trace=round2_summary.by_case(round2_summary.read_trace(experiment_dir / arm.trace_file), arm.trace_file))
    check_arm_file(run, arm, cases_document,
                   sha256_hex((experiment_dir / selective_design.CASES_FILE).read_bytes()))
    return run


def check_arm_file(run: ArmRun, arm: selective_design.Arm, cases_document: Record, cases_sha256: str) -> None:
    """Refuse an answers file that is another arm's, ran on another build or pre-registration, or covers other worlds
    than it lists (M-all must list every held-out world; S's list is checked against the routes as a validity check)."""
    cases: Sequence[Record] = cases_document["cases"]  # type: ignore[assignment]
    problems: list[str] = []
    found = (run.document.get("arm"), run.document.get("system"), run.document.get("model_seed"))
    if found != (arm.name, arm.system, arm.model_seed):
        problems.append(f"its answers file is labelled {found}")
    if run.document.get("cases_sha256") != cases_sha256:
        problems.append("it ran on a different cases.json")
    if run.document.get("preregistration") != cases_document["preregistration"]:
        problems.append("it ran under a different pre-registration")
    listed = run.document.get("arm_case_ids")
    known = [str(case["case_id"]) for case in cases]
    if not isinstance(listed, list) or len(set(map(str, listed))) != len(listed) or not set(map(str, listed)) <= set(known):
        problems.append("its list of worlds is malformed or names worlds this build does not have")
    elif list(run.answers) != listed or set(run.trace) != set(listed):
        problems.append("its answers or trace do not cover exactly, and in order, the worlds it lists")
    heldout = [str(case["case_id"]) for case in cases if case["role"] == selective_design.HELDOUT]
    if arm.system == selective_design.MODEL_ALL_SYSTEM and listed != heldout:
        problems.append("M-all must diagnose every held-out world, in run order")
    if problems:
        raise SystemExit(f"the {arm.name} arm cannot be summarised: {'; '.join(problems)}")


def load_runs(experiment_dir: Path, cases_document: Record) -> dict[str, ArmRun]:
    """All four model arms; the report needs every one of them."""
    return {name: load_arm(experiment_dir, arm, cases_document) for name, arm in selective_design.ARMS.items()}


# --------------------------------------------------------------------------- one final answer


def final_output(answer: Record | None) -> Record | None:
    """A model run's final answer: its validated output; none when it ended needs_review (or never ran)."""
    if answer is None or answer["status"] != "validated" or not isinstance(answer["output"], dict):
        return None
    return answer["output"]


def attribution_class(output: Record | None, expected: str) -> str:
    """correct (names the expected faulting deployment), wrong (names another deployment), abstained (``unknown`` or
    ``none``) or review (no final answer). Only ``wrong`` counts as a wrong attribution."""
    if output is None:
        return REVIEW
    change = str(output["faulting_change_id"])
    if change == expected:
        return CORRECT
    return ABSTAINED if change in ABSTENTIONS else WRONG


def answer_view(case: Record, output: Record | None, source: str) -> dict[str, JsonValue]:
    """One final answer on one world: what it names, its class, where it came from and M4's outcome score."""
    expected: Record = case["expected"]  # type: ignore[assignment]
    return {"change": None if output is None else output["faulting_change_id"],
            "class": attribution_class(output, str(expected["faulting_change_id"])), "source": source,
            "outcome_correct": bool(output is not None and round1_summary.scored(case, output))}


def _selective_answer(case: Record, model_seed: int, runs: Mapping[str, ArmRun]) -> Record | None:
    arm = selective_design.arm_for(selective_design.SELECTIVE_SYSTEM, model_seed)
    return runs[arm.name].answers.get(str(case["case_id"]))


def selective_view(case: Record, model_seed: int, deterministic: Mapping[str, Deterministic],
                   runs: Mapping[str, ArmRun]) -> dict[str, JsonValue]:
    """S's final answer on one world at one seed: rules-v3's on the RULES route, the S arm's on the MODEL route.

    A MODEL-routed world the arm never diagnosed has no answer ("missing"); the routes check then fails."""
    computed = deterministic[str(case["case_id"])]
    if computed.target == RULES_ROUTE:
        return answer_view(case, computed.rules_v3_output, "rules_v3")
    answer = _selective_answer(case, model_seed, runs)
    return answer_view(case, final_output(answer), "model" if answer is not None else "missing")


def agreement_view(case: Record, model_seed: int, deterministic: Mapping[str, Deterministic],
                   runs: Mapping[str, ArmRun]) -> dict[str, JsonValue]:
    """S-agree's final answer (Agent E's agreement variant; descriptive only). The RULES route is rules-v3's. On the
    MODEL route the model's answer stands only when it names rules-v3's deployment; otherwise it is review.

    So S-agree names a deployment only where rules-v3 names the same one: it can never be right where rules-v3 is not."""
    computed = deterministic[str(case["case_id"])]
    if computed.target == RULES_ROUTE:
        return answer_view(case, computed.rules_v3_output, "rules_v3")
    answer = _selective_answer(case, model_seed, runs)
    output = final_output(answer)
    if answer is None or output is None:
        return answer_view(case, None, "missing" if answer is None else "model_needs_review")
    if output["faulting_change_id"] != computed.rules_v3_output["faulting_change_id"]:
        return answer_view(case, None, "changes_differ")
    return answer_view(case, output, "accepted")


def model_all_view(case: Record, model_seed: int, runs: Mapping[str, ArmRun]) -> dict[str, JsonValue]:
    arm = selective_design.arm_for(selective_design.MODEL_ALL_SYSTEM, model_seed)
    answer = runs[arm.name].answers.get(str(case["case_id"]))
    return answer_view(case, final_output(answer), "model" if answer is not None else "missing")


def _per_seed(view: Callable[[int], dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return {key: view(seed) for key, seed in zip(SEED_KEYS, selective_design.MODEL_SEEDS, strict=True)}


# --------------------------------------------------------------------------- one world


def world_row(case: Record, deterministic: Mapping[str, Deterministic], runs: Mapping[str, ArmRun], *,
              with_model_all: bool = True) -> dict[str, JsonValue]:
    """Every system's answers on one held-out world (M-all's only when its figures are reported)."""
    case_id = str(case["case_id"])
    computed = deterministic[case_id]
    params: Record = case["params"]  # type: ignore[assignment]
    source: Record = case["source"]  # type: ignore[assignment]
    row: dict[str, JsonValue] = {
        "case_id": case_id, "entry": source["run_key"], "replicate": source["replicate"],
        "fault": params["fault"], "innocent": params["harmless"], "position": params["harmless_position"],
        "onset": params["onset"], "expected": case["expected"]["faulting_change_id"],  # type: ignore[index, call-overload]
        "route": computed.target, "route_reason": computed.route["reason"],
        "rules_v3_resolution": computed.rules_v3["resolution"],
        R: answer_view(case, computed.rules_v3_output, "rules_v3"),
        R2: {**answer_view(case, computed.rules_v2_output, "rules_v2"), "validated": computed.rules_v2["validated"]},
        S: _per_seed(lambda seed: selective_view(case, seed, deterministic, runs)),
        S_AGREE: _per_seed(lambda seed: agreement_view(case, seed, deterministic, runs)),
    }
    if with_model_all:
        row[M_ALL] = _per_seed(lambda seed: model_all_view(case, seed, runs))
    row["counts"] = {system: {name: count(row, system, name) for name in (CORRECT, WRONG)} for system in systems_in(row)}
    row["change"] = direction(count(row, R, CORRECT), count(row, S, CORRECT))
    return row


def control_row(case: Record, deterministic: Mapping[str, Deterministic], runs: Mapping[str, ArmRun]) -> dict[str, JsonValue]:
    """One control world: its route, and R's, S's and S-agree's answers (S equals R on the RULES route by construction)."""
    case_id = str(case["case_id"])
    computed = deterministic[case_id]
    rules_v3 = computed.rules_v3_output
    row: dict[str, JsonValue] = {
        "case_id": case_id, "family": case["family"], "variant": case["variant"], "expected": case["expected"],
        "route": computed.target, "route_reason": computed.route["reason"],
        R: {**answer_view(case, rules_v3, "rules_v3"), "hypothesis": rules_v3["hypothesis"],
            "operation": rules_v3["proposed_operation"]},
        S: _per_seed(lambda seed: selective_view(case, seed, deterministic, runs)),
        S_AGREE: _per_seed(lambda seed: agreement_view(case, seed, deterministic, runs)),
    }
    row["counts"] = {system: {name: count(row, system, name) for name in (CORRECT, WRONG)} for system in systems_in(row)}
    return row


def systems_in(row: Record) -> list[str]:
    return [system for system in SYSTEMS if system in row]


def seed_views(row: Record, system: str) -> list[Record]:
    """A system's final answers on a world, one per model seed; a deterministic system's one answer stands for both."""
    if system in DETERMINISTIC_SYSTEMS:
        return [row[system]] * len(SEED_KEYS)  # type: ignore[list-item]
    views: Record = row[system]  # type: ignore[assignment]
    return [views[key] for key in SEED_KEYS]  # type: ignore[misc]


def count(row: Record, system: str, attribution: str) -> int:
    """How many of the two seeds' final answers on this world fall in class ``attribution`` (R and R2: 0 or 2)."""
    return sum(view["class"] == attribution for view in seed_views(row, system))


def direction(baseline: int, candidate: int) -> str:
    if candidate > baseline:
        return IMPROVED
    return WORSENED if candidate < baseline else TIED


# --------------------------------------------------------------------------- the sign test and paired comparisons


def sign_test_p(successes: int, trials: int) -> Fraction:
    """Round 3's exact one-sided sign test: P(X >= successes) for X ~ Binomial(trials, 1/2); 1 when trials is 0."""
    p_value: Fraction = round3_summary.sign_test_p(successes, trials)
    return p_value


def p_value_record(value: Fraction) -> dict[str, JsonValue]:
    return {"exact": str(value), "value": float(value)}


def _grouped(rows: Sequence[Record], key: str) -> dict[str, list[Record]]:
    groups: dict[str, list[Record]] = {}
    for row in rows:
        groups.setdefault(str(row[key]), []).append(row)
    return groups


def _tallied(units: Sequence[tuple[str, int, int]], baseline: str, candidate: str, unit: str) -> dict[str, JsonValue]:
    """The sign test over units, each ``(name, baseline correct, candidate correct)``, with every unit's row."""
    tally = Counter({IMPROVED: 0, WORSENED: 0, TIED: 0})
    per_unit: list[JsonValue] = []
    for name, before, after in units:
        change = direction(before, after)
        tally[change] += 1
        per_unit.append({unit: name, baseline: before, candidate: after, "change": change})
    untied = tally[IMPROVED] + tally[WORSENED]
    return {"baseline": baseline, "candidate": candidate, "unit": unit, "units": len(units),
            IMPROVED: tally[IMPROVED], WORSENED: tally[WORSENED], TIED: tally[TIED],
            "improved_p": p_value_record(sign_test_p(tally[IMPROVED], untied)),
            "worsened_p": p_value_record(sign_test_p(tally[WORSENED], untied)), f"per_{unit}": per_unit}


def paired_by_entry(rows: Sequence[Record], baseline: str, candidate: str) -> dict[str, JsonValue]:
    """The comparison with the held-out manifest entry as the unit: an entry's 2 worlds x 2 seeds give each system 0-4
    correct answers (R and R2 count their one answer per world at both seeds, so 0, 2 or 4)."""
    units = [(entry, sum(count(row, baseline, CORRECT) for row in group), sum(count(row, candidate, CORRECT) for row in group))
             for entry, group in _grouped(rows, "entry").items()]
    return _tallied(units, baseline, candidate, "entry")


def paired_by_world(rows: Sequence[Record], baseline: str, candidate: str) -> dict[str, JsonValue]:
    """The same comparison with the world as the unit (0-2 per world): descriptive only, since an entry's two worlds
    are not independent."""
    units = [(str(row["case_id"]), count(row, baseline, CORRECT), count(row, candidate, CORRECT)) for row in rows]
    return _tallied(units, baseline, candidate, "world")


def totals(rows: Sequence[Record], system: str) -> dict[str, int]:
    """A system's attribution classes over these worlds, counted per world and seed."""
    return {name: sum(count(row, system, name) for row in rows) for name in CLASSES}


# --------------------------------------------------------------------------- the decisions


def decide_primary(*, valid: bool, selective_wrong: int, rules_wrong: int, improved_p: Fraction,
                   worsened_p: Fraction) -> tuple[str, list[str]]:
    """Decision 1's verdict in PREREGISTRATION.md's order (the first rule that matches decides), and the clauses that
    made it."""
    if not valid:
        return "invalid", []
    hurt = [clause for clause, holds in (("S has more wrong attributions than R", selective_wrong > rules_wrong),
                                         ("worsened-direction p <= 0.05", worsened_p <= selective_design.ALPHA)) if holds]
    if hurt:
        return "hurts", hurt
    if improved_p <= selective_design.ALPHA and selective_wrong <= rules_wrong:
        return "helps", ["improved-direction p <= 0.05", "S has no more wrong attributions than R"]
    return selective_design.NO_DETECTABLE_ADVANTAGE, []


def selective_against_rules(rows: Sequence[Record], control_rows: Sequence[Record], checks: Record) -> dict[str, JsonValue]:
    """Decision 1, S against R: the sign test over the 24 held-out entries; wrong attributions over all 58 worlds."""
    by_entry = paired_by_entry(rows, R, S)
    improved, worsened = int(by_entry[IMPROVED]), int(by_entry[WORSENED])  # type: ignore[call-overload]
    every_world = [*rows, *control_rows]
    wrong = {system: totals(every_world, system)[WRONG] for system in (R, S)}
    correct = {system: totals(rows, system)[CORRECT] for system in (R, S)}
    cause = invalid_because(checks, primary_binding())
    verdict, clauses = decide_primary(valid=not cause, selective_wrong=wrong[S], rules_wrong=wrong[R],
                                      improved_p=sign_test_p(improved, improved + worsened),
                                      worsened_p=sign_test_p(worsened, improved + worsened))
    routed = [row for row in rows if row["route"] == MODEL_ROUTE]
    return {
        "verdict": verdict, "clauses": clauses, "valid": not cause, "invalid_because": cause,  # type: ignore[dict-item]
        "binding_checks": primary_binding(),  # type: ignore[dict-item]
        **by_entry,
        "correct_attributions": {**correct, "of": len(SEED_KEYS) * len(rows)},  # type: ignore[dict-item]
        "wrong_attributions": {**wrong, "of": len(SEED_KEYS) * len(every_world),  # type: ignore[dict-item]
                               "heldout": {system: totals(rows, system)[WRONG] for system in (R, S)},
                               "controls": {system: totals(control_rows, system)[WRONG] for system in (R, S)}},
        "heldout_totals": {system: totals(rows, system) for system in (R, S)},  # type: ignore[dict-item]
        "effect": {"net_entries": improved - worsened, "correct_difference": correct[S] - correct[R],
                   "wrong_difference": wrong[S] - wrong[R], "model_routed_worlds": len(routed),
                   "entries_with_a_model_routed_world": len({str(row["entry"]) for row in routed}), "worlds": len(rows)},
        "per_world_descriptive": paired_by_world(rows, R, S),
        "unsafe_proposals": UNSAFE_PROPOSALS,
    }


def decide_secondary(*, valid: bool, rules_v3: Mapping[str, int], rules_v2: Mapping[str, int]) -> str:
    """Decision 2, deterministic: rules-v3 replaces rules-v2 with at least as many correct and no more wrong answers."""
    if not valid:
        return "invalid"
    if rules_v3[CORRECT] >= rules_v2[CORRECT] and rules_v3[WRONG] <= rules_v2[WRONG]:
        return selective_design.ADOPT_RULES_V3
    return selective_design.KEEP_RULES_V2


def rules_v3_against_rules_v2(rows: Sequence[Record], checks: Record) -> dict[str, JsonValue]:
    """Decision 2 on the held-out worlds, one answer per world for each system."""
    tallies = {system: Counter(str(row[system]["class"]) for row in rows) for system in (R, R2)}  # type: ignore[index]
    counted = {system: {name: tallies[system][name] for name in CLASSES} for system in (R, R2)}
    cause = invalid_because(checks, REFUSED_UNLESS_HELD)
    verdict = decide_secondary(valid=not cause, rules_v3=counted[R], rules_v2=counted[R2])
    return {"verdict": verdict, "valid": not cause, "invalid_because": cause, "worlds": len(rows),  # type: ignore[dict-item]
            "rules_v3": counted[R], "rules_v2": counted[R2],  # type: ignore[dict-item]
            "rules_v2_refused_by_val_6": [row["case_id"] for row in rows if not row[R2]["validated"]],  # type: ignore[index]
            "per_world_disagreements": [{"case_id": row["case_id"], "rules_v3": row[R]["change"], "rules_v2": row[R2]["change"]}  # type: ignore[index]
                                        for row in rows if row[R]["change"] != row[R2]["change"]]}  # type: ignore[index]


def adoption(primary: str, secondary: str) -> dict[str, JsonValue]:
    """PREREGISTRATION.md's adoption rules: what ships after each verdict. Nothing descriptive enters them."""
    return {
        "decision_1": primary,
        "product": ("a selective product mode is wired: the orchestrator adds a ModelMode and B wires diagnosis_store; "
                    "the README states the measured advantage" if primary == "helps" else
                    "the product stays rules-first; the model is labelled experimental assistance with its measured limits"),
        "decision_2": secondary,
        "rules_default": ("rules_v3 replaces rules-v2 as the rules default; rules-v2 is kept for reproducing M4"
                          if secondary == selective_design.ADOPT_RULES_V3 else "rules-v2 stays the rules default"),
    }


# --------------------------------------------------------------------------- validity


def primary_binding() -> list[str]:
    """The checks decision 1 needs: the refusals, the controls' routes, and every check on S's two arms."""
    selective_arms = [name for name, arm in selective_design.ARMS.items() if arm.decides]
    return [*REFUSED_UNLESS_HELD, "controls_routed_to_model", *(f"arms.{name}" for name in selective_arms)]


def model_all_binding() -> list[str]:
    """What gates M-all's figures: its arms' code, pre-registration and run records. Its routes and operational
    failures are descriptive, because no decision reads an M-all answer."""
    return [f"arms.{name}.{check}" for name, arm in selective_design.ARMS.items() if not arm.decides
            for check in MODEL_ALL_GATES]


def check_named(checks: Record, name: str) -> Record:
    """A check by name: a top-level one, ``arms.<arm>`` (all of an arm's checks) or ``arms.<arm>.<check>``."""
    if not name.startswith("arms."):
        return checks[name]  # type: ignore[return-value]
    _, arm, *rest = name.split(".")
    arm_checks: Record = checks["arms"][arm]  # type: ignore[index, assignment]
    return arm_checks[rest[0]] if rest else arm_checks  # type: ignore[return-value]


def invalid_because(checks: Record, names: Iterable[str]) -> dict[str, JsonValue]:
    """Each named check that failed, with its record."""
    return {name: check_named(checks, name) for name in names if not check_named(checks, name)["passed"]}  # type: ignore[misc]


def _check(problems: Sequence[JsonValue], **details: JsonValue) -> dict[str, JsonValue]:
    return {"passed": not problems, "problems": list(problems), **details}


def code_check(document: Record, code: Record) -> dict[str, JsonValue]:
    """The arm ran the pinned export: imported from it, the pinned versions, the summariser's own package sources."""
    recorded: Record = document.get("code_under_test") or {}  # type: ignore[assignment]
    problems: list[JsonValue] = []
    if recorded.get("versions") != dict(selective_design.CODE_VERSIONS):
        problems.append(f"versions {recorded.get('versions')}")
    if recorded.get("package_sources_sha256") != code["package_sources_sha256"] or recorded.get("commit") != code["commit"]:
        problems.append("another package than the summariser's export")
    export = recorded.get("code_export")
    if export is None or recorded.get("package_dir") != str(Path(str(export)) / "src" / "recovery_workbench"):
        problems.append(f"imported from {recorded.get('package_dir')}, not from its export {export}")
    return _check(problems, package_sources_sha256=recorded.get("package_sources_sha256"))


def preregistration_check(document: Record, addenda: Sequence[JsonValue], frozen_sha256: str) -> dict[str, JsonValue]:
    """The arm ran under this pre-registration, under no addendum since changed, with today's frozen settings file."""
    problems: list[JsonValue] = []
    recorded: Sequence[JsonValue] = document.get("addenda") or []  # type: ignore[assignment]
    current = [{"file": entry["file"], "sha256": entry["sha256"]} for entry in addenda]  # type: ignore[index, call-overload]
    unknown = [entry for entry in recorded if {"file": entry["file"], "sha256": entry["sha256"]} not in current]  # type: ignore[index, call-overload]
    if unknown:
        problems.append(f"it ran under addenda that are no longer recorded as they were: {unknown}")
    if document.get("frozen_settings_sha256") != frozen_sha256:
        problems.append("it ran with another frozen settings file")
    return _check(problems, addenda=list(recorded))


def routes_check(arm: selective_design.Arm, run: ArmRun, cases: Sequence[Record],
                 deterministic: Mapping[str, Deterministic]) -> dict[str, JsonValue]:
    """The arm saw the summariser's routes, diagnosed exactly its worlds under them, and (S) answered each MODEL-routed
    world on the MODEL route with the summariser's rules-v3 answer beside it."""
    problems: list[JsonValue] = []
    recorded: Record = run.document.get("routes") or {}  # type: ignore[assignment]
    differ = [case_id for case_id, computed in deterministic.items() if recorded.get(case_id) != computed.route]
    if differ:
        problems.append({"routes_differ": differ})  # type: ignore[dict-item]
    wanted = selective_design.arm_case_ids(arm, cases, {case_id: computed.target for case_id, computed in deterministic.items()})
    if run.document.get("arm_case_ids") != wanted:
        problems.append({"worlds_diagnosed": run.document.get("arm_case_ids"), "worlds_wanted": wanted})  # type: ignore[dict-item]
    if arm.system == selective_design.SELECTIVE_SYSTEM:
        unlike = [case_id for case_id, answer in run.answers.items() if case_id not in deterministic
                  or answer.get("route") != deterministic[case_id].route
                  or answer.get("rules_v3") != deterministic[case_id].rules_v3]
        if unlike:
            problems.append({"answers_with_another_route_or_rules_v3": unlike})  # type: ignore[dict-item]
    return _check(problems, worlds=len(wanted))


def run_record_check(arm: selective_design.Arm, run: ArmRun, frozen: Record) -> dict[str, JsonValue]:
    """Every run's record: the frozen model and digest, the frozen options with the arm's seed, thinking off, and, for a
    run that made a call, the hash of a first request that was sent as built."""
    model: Record = frozen["model"]  # type: ignore[assignment]
    options = {**model["options"], "seed": arm.model_seed}  # type: ignore[dict-item]
    wanted = {"model": model["model"], "model_digest": model["model_digest"], "options": options,
              "seed": arm.model_seed, "think": selective_design.THINK}
    problems: list[JsonValue] = []
    environment_model: Record = (run.document.get("environment") or {}).get("model") or {}  # type: ignore[union-attr, assignment]
    if environment_model.get("model_digest") != model["model_digest"]:
        problems.append(f"the arm's environment records digest {environment_model.get('model_digest')}")
    for case_id, answer in run.answers.items():
        record: Record = answer.get("run_record") or {}  # type: ignore[assignment]
        drifted = sorted(field for field, value in wanted.items() if record.get(field) != value)
        calls: Sequence[JsonValue] = answer["calls"]  # type: ignore[assignment]
        if calls and (record.get("first_call_sent_as_built") is not True or record.get("first_request_sha256") is None):
            drifted.append("first_request")
        if drifted:
            problems.append({"case_id": case_id, "fields": drifted})  # type: ignore[dict-item]
    return _check(problems, runs=len(run.answers))


def operational_failure_check(run: ArmRun) -> dict[str, JsonValue]:
    """Round 3's operational failures (timeout, model error, deadline, no call, unparsable after the repair): each is
    review and stays in every count; more than MAX_OPERATIONAL_FAILURES in one arm fails the check."""
    failures = {case_id: kind for case_id, answer in run.answers.items()
                if (kind := round3_summary.operational_failure(answer)) is not None}
    problems = [f"{len(failures)} operational failures, more than {selective_design.MAX_OPERATIONAL_FAILURES}"] \
        if len(failures) > selective_design.MAX_OPERATIONAL_FAILURES else []
    return _check(problems, failures=failures, allowed=selective_design.MAX_OPERATIONAL_FAILURES)  # type: ignore[arg-type]


def arm_checks(arm: selective_design.Arm, run: ArmRun, cases: Sequence[Record], deterministic: Mapping[str, Deterministic], *,
               code: Record, addenda: Sequence[JsonValue], frozen: Record, frozen_sha256: str) -> dict[str, JsonValue]:
    checks = {
        "code": code_check(run.document, code),
        "preregistration": preregistration_check(run.document, addenda, frozen_sha256),
        "routes": routes_check(arm, run, cases, deterministic),
        "run_records": run_record_check(arm, run, frozen),
        "operational_failures": operational_failure_check(run),
    }
    return {"passed": all(bool(check["passed"]) for check in checks.values()), "decides": arm.decides, **checks}


def validity(cases_document: Record, deterministic: Mapping[str, Deterministic], runs: Mapping[str, ArmRun], *,
             code: Record, preregistration: Record, addenda: Sequence[JsonValue], frozen: Record,
             frozen_sha256: str) -> dict[str, JsonValue]:
    """Every validity check: the three the stage refuses to run without, the controls' routes, and each arm's."""
    cases: Sequence[Record] = cases_document["cases"]  # type: ignore[assignment]
    controls = [str(case["case_id"]) for case in cases if case["role"] == selective_design.CONTROL]
    model_routed = [case_id for case_id in controls if deterministic[case_id].target == MODEL_ROUTE]
    return {
        "preregistration": {"passed": True, "final": True, **preregistration, "addenda": list(addenda)},
        "packets": {"passed": True, "packets": len(cases), "with_build_hash": len(deterministic)},
        "summariser_code": {"passed": True, **code},
        "controls_routed_to_model": _check(
            [f"{len(model_routed)} of {len(controls)} controls route to MODEL"]
            if len(model_routed) > selective_design.MAX_CONTROL_MODEL_ROUTES else [],
            model_routed=model_routed, controls=len(controls), allowed=selective_design.MAX_CONTROL_MODEL_ROUTES),  # type: ignore[arg-type]
        "arms": {name: arm_checks(selective_design.ARMS[name], run, cases, deterministic, code=code, addenda=addenda,
                                  frozen=frozen, frozen_sha256=frozen_sha256)
                 for name, run in runs.items()},
    }


# --------------------------------------------------------------------------- descriptive figures (no verdict depends on them)


def system_figures(rows: Sequence[Record], system: str) -> dict[str, JsonValue]:
    views = [view for row in rows for view in seed_views(row, system)]
    classes = Counter(str(view["class"]) for view in views)
    return {**{name: classes[name] for name in CLASSES}, "answers": len(views),
            "outcome_correct": sum(bool(view["outcome_correct"]) for view in views)}


def slice_figures(rows: Sequence[Record], systems: Sequence[str]) -> dict[str, JsonValue]:
    """Each system's attribution classes and M4 outcome scores over these worlds, counted per world and seed."""
    return {"worlds": len(rows), **{system: system_figures(rows, system) for system in systems}}


def routing(rows: Sequence[Record], control_rows: Sequence[Record]) -> dict[str, JsonValue]:
    """The invocation rate: how many worlds the router sent to the model, overall, per stratum, and why."""
    def rate(selected: Sequence[Record]) -> dict[str, JsonValue]:
        routed = sum(row["route"] == MODEL_ROUTE for row in selected)
        return {"model": routed, "of": len(selected), "exact": f"{routed}/{len(selected)}"}

    return {"heldout": rate(rows), "controls": rate(control_rows),
            "heldout_entries_with_a_model_route": len({str(row["entry"]) for row in rows if row["route"] == MODEL_ROUTE}),
            "heldout_reasons": dict(sorted(Counter(str(row["route_reason"]) for row in rows).items())),
            "control_reasons": dict(sorted(Counter(str(row["route_reason"]) for row in control_rows).items())),
            **{f"by_{key}": {value: rate(group) for value, group in _grouped(rows, key).items()} for key in STRATA}}


def strata(rows: Sequence[Record], key: str, systems: Sequence[str]) -> dict[str, JsonValue]:
    return {value: {"worlds": len(group), "model_routed": sum(row["route"] == MODEL_ROUTE for row in group),
                    **{system: sum(count(row, system, CORRECT) for row in group) for system in systems}}
            for value, group in _grouped(rows, key).items()}


def agreement_figures(rows: Sequence[Record], control_rows: Sequence[Record]) -> dict[str, JsonValue]:
    """S-agree, the agreement variant: descriptive only, and no adoption rule refers to it."""
    routed = [row for row in rows if row["route"] == MODEL_ROUTE]
    sources = Counter(str(view["source"]) for row in [*rows, *control_rows] for view in seed_views(row, S_AGREE)
                      if row["route"] == MODEL_ROUTE)
    return {"note": ("descriptive only: on the MODEL route the model's answer stands only when it names rules-v3's "
                     "deployment, otherwise review; so S-agree is never right where rules-v3 is not"),
            "heldout": system_figures(rows, S_AGREE), "model_routed": system_figures(routed, S_AGREE),
            "model_route_outcomes": dict(sorted(sources.items())),
            "wrong_attributions_all_worlds": totals([*rows, *control_rows], S_AGREE)[WRONG],
            "against_rules_by_entry": paired_by_entry(rows, R, S_AGREE)}


def first_request(answer: Record | None) -> JsonValue:
    record: Record = (answer or {}).get("run_record") or {}  # type: ignore[assignment]
    return record.get("first_request_sha256")


def attribution_change(answer: Record) -> JsonValue:
    output = final_output(answer)
    return None if output is None else output["faulting_change_id"]


def noise(rows: Sequence[Record], runs: Mapping[str, ArmRun], *, with_model_all: bool) -> dict[str, JsonValue]:
    """How much answers move with nothing changed: each model system's two seeds, and (when M-all is reported) S's arm
    against M-all's at the same seed on the same MODEL-routed world: the same prompt, answered in another sequence."""
    routed = [row for row in rows if row["route"] == MODEL_ROUTE]

    def seeds_disagree(system: str, selected: Sequence[Record]) -> int:
        return sum(len({str(view["class"]) for view in seed_views(row, system)}) > 1 for row in selected)

    figures: dict[str, JsonValue] = {"model_routed_worlds": len(routed),
                                     "selective_seeds_disagree_on_model_routed": seeds_disagree(S, routed)}
    if not with_model_all:
        return figures
    same_seed: dict[str, JsonValue] = {}
    for model_seed in selective_design.MODEL_SEEDS:
        selective_run = runs[selective_design.arm_for(selective_design.SELECTIVE_SYSTEM, model_seed).name]
        model_all_run = runs[selective_design.arm_for(selective_design.MODEL_ALL_SYSTEM, model_seed).name]
        pairs = [(selective_run.answers.get(str(row["case_id"])), model_all_run.answers.get(str(row["case_id"]))) for row in routed]
        present = [(one, two) for one, two in pairs if one is not None and two is not None]
        same_seed[str(model_seed)] = {
            "worlds": len(routed), "both_answered": len(present),
            "identical_first_request": sum(first_request(one) is not None and first_request(one) == first_request(two)
                                           for one, two in present),
            "identical_first_reply": sum(bool(round2_summary.first_reply_identical(one, two)) for one, two in present),
            "identical_final_change": sum(attribution_change(one) == attribution_change(two) for one, two in present),
        }
    return {**figures, "model_all_seeds_disagree": seeds_disagree(M_ALL, rows), "selective_against_model_all_same_seed": same_seed}


def arm_summary(run: ArmRun) -> dict[str, JsonValue]:
    """One arm's operational figures, a cost reported whatever the arm: review rate, repairs, calls, operational
    failures, runs with a policy-blocked proposal, and latency."""
    answers = list(run.answers.values())
    failures = {case_id: kind for case_id, answer in run.answers.items()
                if (kind := round3_summary.operational_failure(answer)) is not None}
    needs_review = sum(answer["status"] == "needs_review" for answer in answers)
    return {
        "arm": run.name, "system": run.document["system"], "model_seed": run.document["model_seed"],
        "diagnoses": len(answers), "validated": sum(answer["status"] == "validated" for answer in answers),
        "needs_review": needs_review,
        "review_rate": {"exact": f"{needs_review}/{len(answers)}", "value": needs_review / len(answers) if answers else 0.0},
        "repair_calls": sum(int(answer["repairs"]) for answer in answers),  # type: ignore[call-overload]
        "runs_with_repair": sum(int(answer["repairs"]) > 0 for answer in answers),  # type: ignore[call-overload]
        "model_calls": sum(len(answer["calls"]) for answer in answers),  # type: ignore[arg-type]
        "operational_failures": {"count": len(failures), "by_kind": dict(sorted(Counter(failures.values()).items())),
                                 "cases": sorted(failures)},
        "policy_blocked_runs": sum(bool(answer["unsafe_proposals"]) for answer in answers),
        "runtime": round1_summary.runtime(answers, run.document["environment"]),
    }


def descriptive(rows: Sequence[Record], control_rows: Sequence[Record], runs: Mapping[str, ArmRun], *,
                with_model_all: bool) -> dict[str, JsonValue]:
    systems = systems_in(rows[0]) if rows else [R, S]
    figures: dict[str, JsonValue] = {
        "routing": routing(rows, control_rows),
        "systems": slice_figures(rows, systems),
        "model_routed": slice_figures([row for row in rows if row["route"] == MODEL_ROUTE], systems),
        "rules_routed": slice_figures([row for row in rows if row["route"] == RULES_ROUTE], systems),
        "agreement_variant": agreement_figures(rows, control_rows),
        "strata": {key: strata(rows, key, systems) for key in STRATA},
        "noise": noise(rows, runs, with_model_all=with_model_all),
        "rules_v3_resolutions": dict(sorted(Counter(str(row["rules_v3_resolution"]) for row in rows).items())),
        "controls_rules_route_agreement": {
            "rules_routed": sum(row["route"] == RULES_ROUTE for row in control_rows),
            "S_equals_R": sum(all(row[S][key]["change"] == row[R]["change"] for key in SEED_KEYS)  # type: ignore[index]
                              for row in control_rows if row["route"] == RULES_ROUTE)},
    }
    if with_model_all:
        figures["model_all_against_selective"] = {"by_entry": paired_by_entry(rows, S, M_ALL),
                                                  "by_world": paired_by_world(rows, S, M_ALL)}
        figures["model_all_against_rules"] = {"by_entry": paired_by_entry(rows, R, M_ALL),
                                              "by_world": paired_by_world(rows, R, M_ALL)}
    return figures


# --------------------------------------------------------------------------- the report


def report(cases_document: Record, runs: Mapping[str, ArmRun], deterministic: Mapping[str, Deterministic], *,
           code: Record, preregistration: Record, addenda: Sequence[JsonValue], frozen: Record,
           frozen_sha256: str) -> dict[str, JsonValue]:
    """Every pre-registered figure: the validity checks, both decisions, adoption, and the descriptive figures."""
    cases: Sequence[Record] = cases_document["cases"]  # type: ignore[assignment]
    checks = validity(cases_document, deterministic, runs, code=code, preregistration=preregistration, addenda=addenda,
                      frozen=frozen, frozen_sha256=frozen_sha256)
    model_all_cause = invalid_because(checks, model_all_binding())
    with_model_all = not model_all_cause
    rows = [world_row(case, deterministic, runs, with_model_all=with_model_all)
            for case in cases if case["role"] == selective_design.HELDOUT]
    controls = [control_row(case, deterministic, runs) for case in cases if case["role"] == selective_design.CONTROL]
    primary = selective_against_rules(rows, controls, checks)
    secondary = rules_v3_against_rules_v2(rows, checks)
    return {
        "experiment": cases_document["experiment"], "preregistration": preregistration, "addenda": list(addenda),
        "code_under_test": dict(code),
        "inputs": {name: {key: run.document.get(key) for key in ("model_seed", "cases_sha256", "started_wall_utc",
                                                                  "finished_wall_utc")} for name, run in runs.items()},
        "validity": checks,
        "decisions": {"selective_against_rules": primary, "rules_v3_against_rules_v2": secondary},
        "adoption": adoption(str(primary["verdict"]), str(secondary["verdict"])),
        "model_all": {"reported": with_model_all, "invalid_because": model_all_cause, "gated_by": model_all_binding(),  # type: ignore[dict-item]
                      "note": "descriptive only: M-all decides nothing; its figures are reported only when these checks pass"},
        "descriptive": descriptive(rows, controls, runs, with_model_all=with_model_all),
        "controls": controls,  # type: ignore[dict-item]
        "arms": {name: arm_summary(run) for name, run in runs.items()},
        "per_world": rows,  # type: ignore[dict-item]
    }


def headline(full_report: Record) -> dict[str, JsonValue]:
    decisions: Record = full_report["decisions"]  # type: ignore[assignment]
    primary: Record = decisions["selective_against_rules"]  # type: ignore[assignment]
    secondary: Record = decisions["rules_v3_against_rules_v2"]  # type: ignore[assignment]
    routes: Record = full_report["descriptive"]["routing"]  # type: ignore[index, assignment]
    return {
        "valid": {"decision_1": primary["valid"], "decision_2": secondary["valid"],
                  "model_all_reported": full_report["model_all"]["reported"]},  # type: ignore[index]
        "routing": {"heldout": routes["heldout"], "controls": routes["controls"]},
        "decision_1": {key: primary[key] for key in ("verdict", "clauses", "units", IMPROVED, WORSENED, TIED, "improved_p",
                                                     "worsened_p", "correct_attributions", "wrong_attributions")},
        "decision_2": {key: secondary[key] for key in ("verdict", "rules_v3", "rules_v2")},
        "adoption": full_report["adoption"],
    }


def database_recorder(opened: list[str]) -> Callable[[str, tuple[object, ...]], None]:
    """An audit hook that records every SQLite database this thread opens (Python's ``sqlite3.connect`` event).

    The summariser is single-threaded; other threads of a host process (a test session's servers) are not its own."""
    summariser_thread = threading.get_ident()

    def record(event: str, arguments: tuple[object, ...]) -> None:
        if event == "sqlite3.connect" and threading.get_ident() == summariser_thread:
            opened.append(str(arguments[0]))
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The phase-11b test's validity checks and decisions, from its JSON files.")
    parser.add_argument("--code-export", type=Path, required=True, help="the export the arms ran, its src/ on PYTHONPATH")
    parser.add_argument("--experiment-dir", type=Path, default=selective_design.SELECTIVE_DIR)
    args = parser.parse_args(argv)
    experiment_dir: Path = args.experiment_dir
    cases_document = round1.read_json(experiment_dir / selective_design.CASES_FILE)
    preregistration = selective_design.checked_final_preregistration(cases_document, experiment_dir)
    addenda = selective_design.checked_addenda(experiment_dir)
    code = selective_design.checked_code_under_test(args.code_export.resolve())
    opened: list[str] = []
    sys.addaudithook(database_recorder(opened))  # from here on, every database this process opens is recorded
    packets = selective_design.checked_packets(cases_document, experiment_dir)
    runs = load_runs(experiment_dir, cases_document)
    frozen_path = selective_design.FROZEN_SETTINGS_PATH
    full_report = report(cases_document, runs, compute_deterministic(packets), code=code, preregistration=preregistration,
                         addenda=addenda, frozen=round1.read_json(frozen_path), frozen_sha256=sha256_hex(frozen_path.read_bytes()))
    if opened:
        raise SystemExit(f"the summariser opened databases {opened}; it reads JSON files and packets only, so no report is written")
    checks: Record = full_report["validity"]  # type: ignore[assignment]
    full_report = {**full_report, "validity": {**checks, "no_database": {"passed": True, "databases_opened": 0}}}
    round1.write_derived(experiment_dir / selective_design.REPORT_FILE, json.dumps(full_report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(headline(full_report), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
