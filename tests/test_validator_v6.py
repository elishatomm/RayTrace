"""Validator val-6 (phase 9): a named faulting deployment needs a merchant-side hypothesis backed by that hypothesis's
signature evidence. val-5's worker rule covers merchant_worker_regression; val-6 adds the two other ways to name a
deployment: (a) unknown may not name one, and (b) receiver_rejecting_deliveries needs rejected deliveries in the
packet's delivery_health item, read fail-closed.

Unit cases, the fail-closed reading, the repair loop, correct D3 receiver answers, the expected and rules answers on
all 153 stored packets, the rules baseline on fresh worlds (seeds 11101 and up) and the in-sample counts on every
stored model answer. Both clauses are evidence checks like val-5's rules: fed back for one repair, then needs_review,
and never recorded as unsafe proposals or policy violations.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError
from test_diagnosis import ScriptedClient, reply
from test_validator_v5 import SYMPTOM, WORKER_ERRORS, alerted, answer, evidence, refusal

from recovery_workbench import diagnosis, packet, rules, storage
from recovery_workbench.contracts import (
    DiagnosisOutput,
    DiagnosisStatus,
    EvidenceItem,
    EvidenceKind,
    EvidencePacket,
    Hypothesis,
)
from recovery_workbench.diagnosis import DiagnosisContractError, diagnose, validate_diagnosis
from recovery_workbench.evaluation import run as evaluation_run
from recovery_workbench.simulator import driver, families, runtime
from recovery_workbench.simulator.world import generate_world

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_DIR = REPO_ROOT / "evaluation"
INJECTION_DIR = REPO_ROOT / "experiments" / "injection"
SPIKE = "delivery_rejection_spike"
UNKNOWN_REASON = ("hypothesis 'unknown' cannot name a faulting deployment: with 'unknown', faulting_change_id must be "
                  "'none' or 'unknown'")
RECEIVER_REASON = "hypothesis 'receiver_rejecting_deliveries' needs rejected deliveries in evidence"
UNREADABLE_REASON = ("hypothesis 'receiver_rejecting_deliveries' cannot be checked against rejected deliveries: this "
                     "packet has no single delivery_health item with an integer distinct_events_rejected; choose a "
                     "hypothesis consistent with the evidence, or 'unknown'")


def delivery(count: JsonValue, evidence_id: str = "ev_delivery") -> EvidenceItem:
    return evidence(evidence_id, EvidenceKind.DELIVERY_HEALTH,
                    facts={"delivery_attempts": 400, "rejected_signature": 7, "distinct_events_rejected": count})


def receiving(*extra: EvidenceItem) -> EvidencePacket:
    """A D3-shaped packet: the rejection spike alerted, one deployment, and ``extra`` (its delivery health)."""
    return alerted(SPIKE, [SPIKE, SYMPTOM], [SPIKE, SYMPTOM], *extra)


def receiver_reason(count: int) -> str:
    return (f"{RECEIVER_REASON}, but this packet's delivery_health item shows distinct_events_rejected = {count}; "
            "choose a hypothesis consistent with the evidence, or 'unknown'")


QUIET = alerted(SYMPTOM, [SYMPTOM], [SYMPTOM])  # the symptom alone; one candidate deployment, dep_aaa111
NAMES_THE_DEPLOYMENT = {"faulting_change_id": "dep_aaa111",  # with its deployment item cited, as val-7 asks
                        "supporting_evidence_ids": ["ev_worker", "ev_deploy_aaa111"]}
NAMED_UNKNOWN = answer("unknown", "escalate", **NAMES_THE_DEPLOYMENT)
RECEIVER = answer("receiver_rejecting_deliveries", "prepare_provider_replay", evidence_level="supported",
                  next_check="validate_corrected_handler", **NAMES_THE_DEPLOYMENT)
UNKNOWN = answer("unknown", "escalate")


def clause_hits(hypothesis: str, faulting_change_id: str, built: EvidencePacket) -> set[str]:
    """Which val-6 clauses an answer with this hypothesis and change meets in ``built``: "a", "b", both or none."""
    chosen = Hypothesis(hypothesis)
    hits = set()
    if not diagnosis.unknown_names_no_deployment(chosen, faulting_change_id):
        hits.add("a")
    if not diagnosis.receiver_evidence_consistent(chosen, built):
        hits.add("b")
    return hits


# --------------------------------------------------------------------------- (a) unknown names no deployment


@pytest.mark.parametrize("operation", ["escalate", "refresh_provider_state", "no_action"])
def test_unknown_naming_a_deployment_is_refused_as_an_evidence_check(operation: str) -> None:
    candidate = {**NAMED_UNKNOWN, "proposed_operation": operation}
    refused = refusal(candidate, QUIET)
    assert refused.reasons == (UNKNOWN_REASON,)
    assert refused.violations == ()  # never an unsafe proposal or a policy violation
    assert validate_diagnosis(candidate, QUIET, enforce_policies=False).faulting_change_id == "dep_aaa111"


@pytest.mark.parametrize("change", ["none", "unknown"])
def test_unknown_naming_no_deployment_is_allowed(change: str) -> None:
    accepted = validate_diagnosis({**UNKNOWN, "faulting_change_id": change}, QUIET)
    assert (accepted.hypothesis, accepted.faulting_change_id) == (Hypothesis.UNKNOWN, change)
    assert diagnosis.unknown_names_no_deployment(Hypothesis.UNKNOWN, change)


def test_change_consistency_keeps_its_meaning_and_leaves_unknown_alone() -> None:
    """M4 counted change-consistency refusals as unsafe proposals, so that rule must not grow to cover unknown."""
    assert diagnosis.CHANGE_FREE_HYPOTHESES == {Hypothesis.PROVIDER_METHOD_DEGRADATION,
                                                Hypothesis.AMBIGUOUS_PAYMENT_STATE, Hypothesis.NO_INCIDENT}
    assert diagnosis.change_consistent(Hypothesis.UNKNOWN, "dep_aaa111")
    assert not diagnosis.unknown_names_no_deployment(Hypothesis.UNKNOWN, "dep_aaa111")


def test_the_unknown_refusal_quotes_no_packet_text() -> None:
    [reason] = refusal(NAMED_UNKNOWN, QUIET).reasons
    assert "dep_aaa111" not in reason
    assert "aaa111" not in reason


def test_a_named_deployment_with_its_signature_evidence_is_allowed() -> None:
    worker = answer("merchant_worker_regression", "reprocess_inbox", evidence_level="supported",
                    next_check="validate_corrected_handler", **NAMES_THE_DEPLOYMENT)
    assert validate_diagnosis(worker, alerted(SYMPTOM, [SYMPTOM], [SYMPTOM], WORKER_ERRORS)).faulting_change_id == (
        "dep_aaa111")
    assert validate_diagnosis(RECEIVER, receiving(delivery(3))).faulting_change_id == "dep_aaa111"


# --------------------------------------------------------------------------- (b) a receiver needs rejected deliveries


@pytest.mark.parametrize("count", [0, -1])
def test_a_receiver_diagnosis_without_rejected_deliveries_is_refused(count: int) -> None:
    built = receiving(delivery(count))
    refused = refusal(RECEIVER, built)
    assert refused.reasons == (receiver_reason(count),)
    assert refused.violations == ()
    assert validate_diagnosis(RECEIVER, built, enforce_policies=False).hypothesis is Hypothesis.RECEIVER_REJECTING_DELIVERIES


@pytest.mark.parametrize("count", [1, 236])
@pytest.mark.parametrize("change", ["dep_aaa111", "unknown", "none"])
def test_a_receiver_diagnosis_with_rejected_deliveries_is_allowed(count: int, change: str) -> None:
    built = receiving(delivery(count))
    assert diagnosis.rejected_delivery_count(built) == count
    assert validate_diagnosis({**RECEIVER, "faulting_change_id": change}, built).faulting_change_id == change


@pytest.mark.parametrize("health", [
    (),
    (delivery(5), delivery(5, "ev_delivery_second")),
    (evidence("ev_delivery", EvidenceKind.DELIVERY_HEALTH, facts={"delivery_attempts": 400}),),
    (delivery("5"),),
    (delivery(5.0),),
    (delivery(True),),
    (delivery(None),),
    (evidence("ev_delivery", EvidenceKind.LOG, text="distinct_events_rejected 5"),),
], ids=["no-item", "two-items", "no-count", "count-text", "count-float", "count-bool", "count-null", "wrong-kind"])
def test_unreadable_delivery_health_refuses_a_receiver_diagnosis(health: tuple[EvidenceItem, ...]) -> None:
    built = receiving(*health)
    assert diagnosis.rejected_delivery_count(built) is None
    refused = refusal({**RECEIVER, "faulting_change_id": "unknown"}, built)
    assert refused.reasons == (UNREADABLE_REASON,)
    assert refused.violations == ()
    assert validate_diagnosis(UNKNOWN, built).hypothesis is Hypothesis.UNKNOWN  # only the receiver claim needs it


def test_the_receiver_rule_reads_the_item_kind_not_its_id() -> None:
    built = receiving(delivery(4, "ev_delivery_main"))
    assert diagnosis.rejected_delivery_count(built) == 4
    assert validate_diagnosis(RECEIVER, built).hypothesis is Hypothesis.RECEIVER_REJECTING_DELIVERIES


def test_other_hypotheses_are_untouched_by_the_receiver_rule() -> None:
    built = receiving(delivery(0))
    for hypothesis in Hypothesis:
        assert diagnosis.receiver_evidence_consistent(hypothesis, built) == (
            hypothesis is not Hypothesis.RECEIVER_REJECTING_DELIVERIES)


# --------------------------------------------------------------------------- both clauses: order, policies off


def test_val6_reasons_follow_the_earlier_ones_and_only_the_operation_policy_is_a_violation() -> None:
    built = receiving(delivery(0))
    invented = {**RECEIVER, "proposed_operation": "reprocess_inbox", "supporting_evidence_ids": ["ev_made_up"]}
    refused = refusal(invented, built)
    assert refused.reasons[0] == "supporting_evidence_ids cites IDs that are not in this packet: ['ev_made_up']"
    assert refused.reasons[1].startswith("proposed_operation 'reprocess_inbox' is not allowed")
    assert refused.reasons[2] == receiver_reason(0)
    assert [violation.rule for violation in refused.violations] == ["operation_policy"]
    named = refusal({**NAMED_UNKNOWN, "supporting_evidence_ids": ["ev_made_up", "ev_deploy_aaa111"]}, QUIET)
    assert named.reasons[1:] == (UNKNOWN_REASON,)


def test_with_policies_off_both_refused_answers_validate_on_the_first_call() -> None:
    for candidate, built in ((NAMED_UNKNOWN, QUIET), (RECEIVER, receiving(delivery(0)))):
        run = diagnose(built, ScriptedClient(reply(candidate)), think=False, enforce_policies=False)
        assert (run.status, run.repairs, run.reasons, run.policies_enforced) == (DiagnosisStatus.VALIDATED, 0, (), False)


# --------------------------------------------------------------------------- the repair loop (scripted model)

REFUSED_CASES = [
    pytest.param(NAMED_UNKNOWN, QUIET, UNKNOWN_REASON, id="unknown-names-a-deployment"),
    pytest.param(RECEIVER, receiving(delivery(0)), RECEIVER_REASON, id="receiver-without-rejections"),
    pytest.param(RECEIVER, receiving(), "hypothesis 'receiver_rejecting_deliveries' cannot be checked",
                 id="receiver-unreadable"),
]


@pytest.mark.parametrize(("refused", "built", "reason"), REFUSED_CASES)
def test_a_refused_answer_is_fed_back_and_a_consistent_repair_validates(
    refused: dict[str, object], built: EvidencePacket, reason: str
) -> None:
    client = ScriptedClient(reply(refused), reply(UNKNOWN))
    run = diagnose(built, client, think=False)
    assert (run.status, run.repairs, run.output.hypothesis) == (DiagnosisStatus.VALIDATED, 1, Hypothesis.UNKNOWN)
    assert reason in client.requests[1].messages[-1].content  # the repair call carries the refusal
    assert run.calls[0].rejection_reasons[0].startswith(reason)
    assert run.reasons[0].startswith(f"call 1: {reason}")
    assert (run.unsafe_proposals, run.calls[0].blocked_proposals) == ((), ())
    assert run.validator_version == "val-7"


@pytest.mark.parametrize(("refused", "built", "reason"), REFUSED_CASES)
def test_an_answer_refused_twice_needs_review(refused: dict[str, object], built: EvidencePacket, reason: str) -> None:
    client = ScriptedClient(reply(refused), reply(refused))
    run = diagnose(built, client, think=False)
    assert (run.status, run.output, len(client.requests)) == (DiagnosisStatus.NEEDS_REVIEW, None, 2)
    assert [entry.split(": ", 1)[0] for entry in run.reasons] == ["call 1", "call 2"]
    assert all(reason in entry for entry in run.reasons)
    assert run.unsafe_proposals == ()


# --------------------------------------------------------------------------- stored packets and their expected answers


@dataclass(frozen=True)
class StoredCase:
    """One stored packet, how M4 scores an answer to it, and its expected answer."""

    source: str  # "M4", "round 1" or "round 2"
    label: str
    packet: EvidencePacket
    scoring: evaluation_run.Case

    @property
    def expected(self) -> dict[str, JsonValue]:
        return dict(self.scoring.expected)


def _scoring_case(case: dict, cutoff_ms: int) -> evaluation_run.Case:
    return evaluation_run.Case(run_id=case["run_id"], seed=case["seed"], family=case["family"], variant=case["variant"],
                               params=case["params"], cutoff_ms=cutoff_ms, expected=case["expected"], oracle={},
                               provider_records=Path("unused"))


M4_SPLITS = {  # the 48 frozen M4 answers: the test run and the chosen dev setting, with their results
    "test": (EVALUATION_DIR / "traces" / "test_frozen.jsonl", EVALUATION_DIR / "results.json"),
    "dev": (EVALUATION_DIR / "traces" / "dev_think_off.jsonl",
            EVALUATION_DIR / "runs" / "dev_think_off" / "results.json"),
}
ROUNDS = {"round 1": INJECTION_DIR, "round 2": INJECTION_DIR / "round2"}


def _m4_traces() -> Iterator[tuple[StoredCase, dict]]:
    for split, (trace_path, results_path) in M4_SPLITS.items():
        results = {case["run_id"]: case for case in json.loads(results_path.read_text(encoding="utf-8"))["cases"]}
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            trace = json.loads(line)
            case = results[trace["run_id"]]
            built = EvidencePacket.model_validate(trace["packet"])
            assert built.packet_hash() == trace["packet_hash"], trace["run_id"]
            stored = StoredCase("M4", f"M4 {split} {case['family']} {trace['run_id']}", built,
                                _scoring_case(case, built.incident.evidence_cutoff_ms))
            yield stored, {**trace["answers"]["model"], "stored_correct": case["model"]["score"]["outcome_correct"]}


def _round_cases(name: str) -> dict[str, StoredCase]:
    folder = ROUNDS[name]
    found = {}
    for case in json.loads((folder / "cases.json").read_text(encoding="utf-8"))["cases"]:
        built = EvidencePacket.model_validate_json((folder / case["packet_file"]).read_text(encoding="utf-8"))
        assert built.packet_hash() == case["packet_hash"], case["case_id"]
        found[case["case_id"]] = StoredCase(name, f"{name} {case['case_id']}", built, _scoring_case(case, case["cutoff_ms"]))
    return found


@cache
def stored_cases() -> tuple[StoredCase, ...]:
    """The 153 stored packets: 48 frozen M4, 50 of round 1, 55 of round 2."""
    return (*(stored for stored, _ in _m4_traces()), *_round_cases("round 1").values(), *_round_cases("round 2").values())


def test_no_expected_answer_on_any_stored_packet_meets_either_clause() -> None:
    cases = stored_cases()
    assert Counter(case.source for case in cases) == {"M4": 48, "round 1": 50, "round 2": 55}
    meeting = [case.label for case in cases
               if clause_hits(str(case.expected["hypothesis"]), str(case.expected["faulting_change_id"]), case.packet)]
    assert meeting == []
    # Clause (b) is exercised: 8 expected receiver answers, all with rejected deliveries. Clause (a) cannot be:
    # no expected answer is unknown, while 46 name a deployment, each with a merchant-side hypothesis.
    expected = Counter(str(case.expected["hypothesis"]) for case in cases)
    receiver = [diagnosis.rejected_delivery_count(case.packet) for case in cases
                if case.expected["hypothesis"] == "receiver_rejecting_deliveries"]
    assert (len(receiver), min(receiver) > 0, expected["unknown"]) == (8, True, 0)
    named = Counter(str(case.expected["hypothesis"]) for case in cases if case.expected["faulting_change_id"] != "none")
    assert named == {"merchant_worker_regression": 38, "receiver_rejecting_deliveries": 8}


def test_the_rules_baseline_passes_val6_on_every_stored_packet() -> None:
    refused = {}
    for case in stored_cases():
        try:
            validate_diagnosis(rules.diagnose(case.packet).model_dump(mode="json"), case.packet)
        except DiagnosisContractError as invalid:
            refused[case.label] = invalid.reasons
    assert refused == {}


def _correct_receiver_answer(case: StoredCase) -> dict[str, object]:
    """The expected receiver answer, citing the trigger, delivery health and, as val-7 asks, the deployment's item."""
    change = case.expected["faulting_change_id"]
    [deployment] = [item.evidence_id for item in case.packet.items
                    if item.kind is EvidenceKind.DEPLOYMENT and item.facts.get("deploy_id") == change]
    return answer("receiver_rejecting_deliveries", "prepare_provider_replay", faulting_change_id=change,
                  evidence_level="supported", supporting_evidence_ids=["ev_trigger", "ev_delivery", deployment],
                  next_check="validate_corrected_handler")


def test_a_correct_receiver_answer_is_accepted_on_every_frozen_d3_packet() -> None:
    d3 = [case for case in stored_cases() if case.scoring.family == "D3"]
    assert len(d3) == 8
    for case in d3:
        candidate = _correct_receiver_answer(case)
        accepted = validate_diagnosis(candidate, case.packet)
        assert evaluation_run.score(case.scoring, accepted)["outcome_correct"], case.label
        run = diagnose(case.packet, ScriptedClient(reply(candidate)), think=False)
        assert (run.status, run.repairs) == (DiagnosisStatus.VALIDATED, 0), case.label


# --------------------------------------------------------------------------- the rules baseline on fresh worlds

SWEEP_FIRST_SEED = 11101  # 10101-11100 belong to round 3; the val-5 sweep and rounds 1-2 used lower seeds
SWEEP_TRIES = 5
# The rules baseline answers receiver_rejecting_deliveries only through rules._receiver_rejecting, which a
# failing signed-delivery fixture alone can trigger: the one path on which a rules answer could show no
# rejected delivery and be refused by clause (b). Only D3 worlds reach it, so each D3 parameter set gets a
# second world. (Clause (a) cannot refuse a rules answer: the rules' unknown always names no deployment.)
D3_EXTRA_WORLDS_PER_SET = 1
# Measured on the first run and pinned, so the seeds the sweep used are on record: worlds that opened no incident.
SKIPPED_SEEDS: list[int] = []
LAST_SWEEP_SEED = 11142


def sweep_sources() -> list[families.FamilyRun]:
    """One manifest run per distinct parameter set (35), then the D3 sets again."""
    distinct: dict[str, families.FamilyRun] = {}
    for run in families.load_manifest().runs:
        distinct.setdefault(f"{run.family} {run.variant} {json.dumps(run.params, sort_keys=True)}", run)
    d3 = [run for run in distinct.values() if run.family == "D3"]
    return [*distinct.values(), *d3 * D3_EXTRA_WORLDS_PER_SET]


def fresh_packet(
    conn: sqlite3.Connection, source: families.FamilyRun, seed: int, directory: Path
) -> EvidencePacket | None:
    """Seed a fresh dev-split world with the source's parameters and build its packet at the cutoff, as
    ``evaluation.run.build_case_packet`` does; None when the world opens no incident or several."""
    run = families.FamilyRun(run_key=f"val6_sweep_{source.family.lower()}_{seed}", family=source.family,
                             variant=source.variant, split="dev", seed=seed, params=source.params)
    world = generate_world(families.spec_for(run), seed)
    summary = driver.seed_world(conn, world, oracle_dir=directory / "oracle", provider_dir=directory / "provider")
    incidents = storage.list_incidents(conn, run_id=world.run_id)
    if len(incidents) != 1:
        return None
    deployed = runtime.active_merchant(conn, run_id=world.run_id, at_ms=summary.cutoff_ms)
    return packet.build_packet(conn, run_id=world.run_id, incident_id=incidents[0].incident_id,
                               cutoff_ms=summary.cutoff_ms, deployed=deployed, persist=False)


def test_the_rules_baseline_passes_val6_on_fresh_worlds_of_every_manifest_parameter_set(tmp_path: Path) -> None:
    """Each source takes the next seed from 11101; a world that opens no single incident has no packet to diagnose,
    so its seed is skipped and the next one tried."""
    sources = sweep_sources()
    assert (len(sources), Counter(source.family for source in sources)["D3"]) == (42, 14)
    refused: dict[str, tuple[str, ...]] = {}
    receiver_rejections: list[int | None] = []
    skipped: list[int] = []
    seed = SWEEP_FIRST_SEED
    with closing(storage.connect(tmp_path / "sweep.sqlite")) as conn:
        for source in sources:
            for _ in range(SWEEP_TRIES):
                built, seed = fresh_packet(conn, source, seed, tmp_path), seed + 1
                if built is not None:
                    break
                skipped.append(seed - 1)
            assert built is not None, f"{source.family} {source.variant}: no incident in {SWEEP_TRIES} fresh worlds"
            decided = rules.diagnose(built)
            if decided.hypothesis is Hypothesis.RECEIVER_REJECTING_DELIVERIES:
                receiver_rejections.append(diagnosis.rejected_delivery_count(built))
            try:
                validate_diagnosis(decided.model_dump(mode="json"), built)
            except DiagnosisContractError as invalid:
                refused[f"{seed - 1} {source.family} {source.variant}"] = invalid.reasons
    assert refused == {}
    assert (skipped, seed - 1) == (SKIPPED_SEEDS, LAST_SWEEP_SEED)
    # Not vacuous: every D3 world was answered as a receiver diagnosis, each with rejected deliveries.
    assert len(receiver_rejections) == 14
    assert all(isinstance(count, int) and count > 0 for count in receiver_rejections)


# --------------------------------------------------------------------------- in-sample: stored model answers

# How many stored answers each clause meets, as (clause, "final" or "reply", "right" or "wrong"), with M4's
# scoring. Information only: these answers were produced under val-4 or val-5, and the rules were proposed
# from round 2. M4 is the 48 frozen answers (test run and the chosen dev setting); "reply" counts every model
# reply, the first and the repair.
IN_SAMPLE = {
    "M4": {"answers": 48, "finals": 48, "replies": 75,
           "met": {("b", "final", "wrong"): 3, ("b", "reply", "wrong"): 6}},
    "round 1": {"answers": 50, "finals": 50, "replies": 72, "met": {}},
    "round 2 val-4": {"answers": 55, "finals": 55, "replies": 77,
                      "met": {("b", "final", "wrong"): 2, ("b", "reply", "wrong"): 4}},
    "round 2 val-5": {"answers": 55, "finals": 50, "replies": 95,
                      "met": {("a", "final", "wrong"): 5, ("a", "reply", "wrong"): 5,
                              ("b", "final", "wrong"): 2, ("b", "reply", "wrong"): 4}},
}


def _stored_answers() -> dict[str, list[tuple[StoredCase, dict]]]:
    rounds = {name: _round_cases(name) for name in ROUNDS}
    found: dict[str, list[tuple[StoredCase, dict]]] = {"M4": list(_m4_traces())}
    for name, (round_name, answers_file) in {"round 1": ("round 1", "answers.json"),
                                             "round 2 val-4": ("round 2", "answers_val4.json"),
                                             "round 2 val-5": ("round 2", "answers_val5.json")}.items():
        document = json.loads((ROUNDS[round_name] / answers_file).read_text(encoding="utf-8"))
        found[name] = [(rounds[round_name][entry["case_id"]], entry) for entry in document["answers"]]
    return found


def _correctness(case: StoredCase, candidate: object) -> str:
    try:
        output = DiagnosisOutput.model_validate(candidate)
    except ValidationError:
        return "unscorable"
    return "right" if evaluation_run.score(case.scoring, output)["outcome_correct"] else "wrong"


def test_in_sample_the_clauses_meet_only_wrong_stored_answers() -> None:
    measured = {}
    for name, entries in _stored_answers().items():
        met: Counter[tuple[str, str, str]] = Counter()
        finals = replies = 0
        for case, entry in entries:
            final = entry["output"]
            if final is not None:
                finals += 1
                verdict = _correctness(case, final)
                if "stored_correct" in entry:
                    assert verdict == ("right" if entry["stored_correct"] else "wrong"), case.label  # M4's own score
                for clause in clause_hits(final["hypothesis"], final["faulting_change_id"], case.packet):
                    met[(clause, "final", verdict)] += 1
            for call in entry["calls"]:
                if call["reply"] is None:
                    continue
                replies += 1
                candidate = json.loads(call["reply"]["content"])
                for clause in clause_hits(candidate["hypothesis"], candidate["faulting_change_id"], case.packet):
                    met[(clause, "reply", _correctness(case, candidate))] += 1
        measured[name] = {"answers": len(entries), "finals": finals, "replies": replies, "met": dict(met)}
    assert measured == IN_SAMPLE
