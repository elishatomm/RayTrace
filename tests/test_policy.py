"""Policy: operators, the operation policy, evidence fingerprints, the transactional recheck, and M0 comparability."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import AMOUNT, ORDER_1, PAY_1, PAY_2, START_MS, SeededRun, add_order, deliver, observation, store_observations
from test_prompt_p3 import diag_p2_rendering

from recovery_workbench import diagnosis, policy, storage
from recovery_workbench.contracts import (
    ContractError,
    DiagnosisStatus,
    EvidencePacket,
    Hypothesis,
    Operation,
    PaymentStatus,
    PlanStatus,
)
from recovery_workbench.diagnosis import ChatReply, ChatRequest, DiagnosisContractError, diagnose, validate_diagnosis

M0_DIR = Path(__file__).resolve().parents[1] / "experiments" / "m0"
RUN_1 = "20261111T184026Z_diag-p1"


# --------------------------------------------------------------------------- operators and replay time


@pytest.mark.parametrize(
    ("operator_id", "kind"),
    [
        ("op_alice", policy.ApproverKind.HUMAN),
        ("op_local_demo_2", policy.ApproverKind.HUMAN),
        ("qwen3:4b", policy.ApproverKind.MODEL),
        ("model", policy.ApproverKind.MODEL),
        ("op_", policy.ApproverKind.MODEL),
        ("OP_alice", policy.ApproverKind.MODEL),
        ("op_alice\n", policy.ApproverKind.MODEL),
        ("agent_op_alice", policy.ApproverKind.MODEL),
    ],
)
def test_only_operator_session_ids_count_as_human(operator_id: str, kind: policy.ApproverKind) -> None:
    assert policy.operator_kind(operator_id) is kind


def test_replay_time_cannot_precede_the_run_clock(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    assert policy.require_replay_time(conn, run_a.run_id, START_MS).run_id == run_a.run_id
    for bad in (START_MS - 1, -1, True, 1.5):
        with pytest.raises(ContractError):
            policy.require_replay_time(conn, run_a.run_id, bad)  # type: ignore[arg-type]


def test_the_phase_one_pure_check_still_refuses_a_model_approver() -> None:
    plan = policy.PlanFacts(plan_hash="p", run_id="r", evidence_version="e")
    approval = policy.ApprovalFacts(approver=policy.ApproverKind.MODEL, plan_hash="p", run_id="r", expires_at_ms=10)
    current = policy.CurrentFacts(
        run_id="r", evidence_version="e", snapshot_age_ms=0, status=PaymentStatus.CAPTURED, captures_for_order=1,
        worker_healthy=True, cancelled=False, refunded=False,
    )
    assert policy.failed_preconditions(plan, approval, current, 5) == (policy.FailedPrecondition.APPROVER_NOT_HUMAN,)


# --------------------------------------------------------------------------- the operation policy (diagnosis.py)


def test_operation_policy_is_the_table_from_the_brief() -> None:
    expected = {
        Hypothesis.MERCHANT_WORKER_REGRESSION: {Operation.REPROCESS_INBOX, Operation.ESCALATE},
        Hypothesis.RECEIVER_REJECTING_DELIVERIES: {
            Operation.PREPARE_PROVIDER_REPLAY, Operation.REFRESH_PROVIDER_STATE, Operation.ESCALATE,
        },
        Hypothesis.PROVIDER_METHOD_DEGRADATION: {Operation.ESCALATE},
        Hypothesis.AMBIGUOUS_PAYMENT_STATE: {Operation.REFRESH_PROVIDER_STATE, Operation.ESCALATE},
        Hypothesis.NO_INCIDENT: {Operation.NO_ACTION},
        Hypothesis.UNKNOWN: {Operation.ESCALATE, Operation.REFRESH_PROVIDER_STATE, Operation.NO_ACTION},
    }
    assert {hypothesis: set(allowed) for hypothesis, allowed in diagnosis.OPERATION_POLICY.items()} == expected


def m0_packet(packet_id: str) -> EvidencePacket:
    return EvidencePacket.model_validate_json((M0_DIR / "packets" / f"{packet_id}.json").read_text(encoding="utf-8"))


def receiver_answer(operation: str) -> dict[str, object]:
    return {
        "rationale": "Every delivery is rejected with 401 since the secret reference changed.",
        "supporting_evidence_ids": ["ev_delivery", "ev_fixture", "ev_deploy_bc3989"],
        "contradicting_evidence_ids": [],
        "hypothesis": "receiver_rejecting_deliveries",
        "faulting_change_id": "dep_bc3989",
        "affected_method": "none",
        "evidence_level": "supported",
        "proposed_operation": operation,
        "next_check": "fetch_provider_snapshot",
        "missing_checks": [],
    }


def test_the_validator_refuses_an_operation_the_hypothesis_does_not_allow() -> None:
    packet = m0_packet("m0_07")
    candidate = receiver_answer("reprocess_inbox")
    with pytest.raises(DiagnosisContractError) as caught:
        validate_diagnosis(candidate, packet)
    assert [(item.rule, item.hypothesis, item.proposed_operation) for item in caught.value.violations] == [
        ("operation_policy", Hypothesis.RECEIVER_REJECTING_DELIVERIES, Operation.REPROCESS_INBOX)
    ]
    assert any("not allowed for hypothesis" in reason for reason in caught.value.reasons)
    output = validate_diagnosis(receiver_answer("reprocess_inbox"), packet, enforce_policies=False)
    assert output.proposed_operation is Operation.REPROCESS_INBOX


class ScriptedClient:
    def __init__(self, *answers: dict[str, object]) -> None:
        self._answers = list(answers)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest, *, timeout_s: float) -> ChatReply:
        self.requests.append(request)
        return ChatReply(content=json.dumps(self._answers.pop(0)), done_reason="stop")


def test_a_blocked_proposal_stays_recorded_as_unsafe_after_a_repair() -> None:
    client = ScriptedClient(receiver_answer("reprocess_inbox"), receiver_answer("prepare_provider_replay"))
    run = diagnose(m0_packet("m0_07"), client, think=False)
    assert run.status is DiagnosisStatus.VALIDATED
    assert run.output.proposed_operation is Operation.PREPARE_PROVIDER_REPLAY
    assert [(item.call_index, item.proposed_operation) for item in run.unsafe_proposals] == [(0, Operation.REPROCESS_INBOX)]
    assert run.calls[0].blocked_proposals == run.unsafe_proposals
    assert "not allowed for hypothesis" in client.requests[1].messages[-1].content  # fed back in the repair
    assert run.policies_enforced
    assert run.validator_version == diagnosis.VALIDATOR_VERSION


def test_with_the_policy_off_nothing_is_blocked_or_counted() -> None:
    run = diagnose(m0_packet("m0_07"), ScriptedClient(receiver_answer("reprocess_inbox")), think=False,
                   enforce_policies=False)
    assert run.status is DiagnosisStatus.VALIDATED
    assert run.unsafe_proposals == ()
    assert not run.policies_enforced


# --------------------------------------------------------------------------- M0 comparability with run 1


def run_one() -> dict[str, object]:
    report = json.loads((M0_DIR / "m0_model_report.json").read_text(encoding="utf-8"))
    return next(run for run in report["runs"] if run["run_label"] == RUN_1)


def test_the_system_prompt_is_byte_identical_to_run_one() -> None:
    recorded = run_one()["prompt"]
    assert recorded["version"] == "diag-p1"
    assert diagnosis.PROMPT_VERSION == "diag-p5"
    assert hashlib.sha256(diagnosis.SYSTEM_PROMPT.encode("utf-8")).hexdigest() == recorded["system_prompt_sha256"]


def test_the_user_message_differs_from_run_one_only_by_the_incident_line_commit_messages_and_failure_details() -> None:
    """diag-p2 replaces run 1's "Incident <id> (run <id>)" line with "Incident card"; diag-p3 then moves each
    deployment's commit message off its facts line into a fence, diag-p4 strips it, and diag-p5 moves each fixture
    failure detail off its facts line into a fence (diag_p2_rendering undoes exactly the relocation, the strip and
    the move)."""
    trace = M0_DIR / "traces" / f"{RUN_1}_think_off.jsonl"
    lines = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 10
    for line in lines:
        built = m0_packet(line["packet_id"])
        sent = line["run"]["calls"][0]["messages"]
        assert sent[0]["content"] == diagnosis.SYSTEM_PROMPT
        run_one_lines = sent[1]["content"].split("\n")
        assert run_one_lines[1] == f"Incident {built.incident.incident_id} (run {built.incident.run_id})"
        assert diag_p2_rendering(built) == "\n".join([run_one_lines[0], "Incident card", *run_one_lines[2:]])
        assert line["packet_hash"] == built.packet_hash()


def test_with_the_policy_off_the_validator_accepts_every_run_one_answer() -> None:
    blocked = {}
    for setting, block in run_one()["settings"].items():
        outputs = [(row["packet_id"], row["output"]) for row in block["per_packet"] if row["output"]]
        for packet_id, output in outputs:
            validate_diagnosis(output, m0_packet(packet_id), enforce_policies=False)
        blocked[setting] = sorted(
            packet_id for packet_id, output in outputs
            if not diagnosis.operation_allowed(Hypothesis(output["hypothesis"]), Operation(output["proposed_operation"]))
        )
    # Offline count reported in phase 2: the policy would have caught only m0_07 of run 1's three unsafe proposals.
    assert blocked == {"think_off": ["m0_05", "m0_07", "m0_08", "m0_10"], "think_on": ["m0_05"]}


# --------------------------------------------------------------------------- evidence fingerprints and the recheck


def capture_row(conn: sqlite3.Connection, run: SeededRun) -> storage.InboxRecord:
    add_order(conn, run)
    response = deliver(conn, run, "evt_capture_1", status=PaymentStatus.CAPTURED)
    row = storage.get_inbox_row(conn, run_id=run.run_id, inbox_id=response.inbox_id)
    assert row is not None
    return row


def assess(conn: sqlite3.Connection, run: SeededRun, row: storage.InboxRecord, now_ms: int) -> policy.TargetAssessment:
    return policy.assess_target(conn, run_id=run.run_id, inbox_row=row, now_ms=now_ms, stale_after_ms=600_000)


def test_a_confirming_snapshot_keeps_the_evidence_version(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    row = capture_row(conn, run_a)
    first = assess(conn, run_a, row, START_MS + 60_000)
    store_observations(conn, [observation(PaymentStatus.CAPTURED, START_MS + 1_000, START_MS + 30_000)])
    confirmed = assess(conn, run_a, row, START_MS + 60_000)
    assert first.eligible
    assert confirmed.eligible
    assert policy.evidence_version([confirmed]) == policy.evidence_version([first])


def test_a_refund_changes_the_evidence_version_and_eligibility(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    row = capture_row(conn, run_a)
    first = assess(conn, run_a, row, START_MS + 60_000)
    store_observations(conn, [
        observation(PaymentStatus.REFUNDED, START_MS + 20_000, START_MS + 30_000, amount_refunded_paise=AMOUNT),
    ])
    refunded = assess(conn, run_a, row, START_MS + 60_000)
    assert not refunded.eligible
    assert "refunded" in refunded.reasons
    assert policy.evidence_version([refunded]) != policy.evidence_version([first])


def test_evidence_version_does_not_depend_on_listing_order(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    row = capture_row(conn, run_a)
    add_order(conn, run_a, "order_SYNTHETIC_0002", payment_ids=("pay_SYNTHETIC_0002",))
    second = deliver(conn, run_a, "evt_capture_2", payment_id="pay_SYNTHETIC_0002", order_id="order_SYNTHETIC_0002")
    other = storage.get_inbox_row(conn, run_id=run_a.run_id, inbox_id=second.inbox_id)
    a, b = assess(conn, run_a, row, START_MS + 60_000), assess(conn, run_a, other, START_MS + 60_000)
    assert policy.evidence_version([a, b]) == policy.evidence_version([b, a])
    assert (a.order_id, a.payment_id) == (ORDER_1, PAY_1)


def test_the_recheck_refuses_to_run_outside_a_write_transaction(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    facts = policy.ReprocessFacts(
        run_id=run_a.run_id, plan_run_id=run_a.run_id, plan_id="plan_x", plan_status=PlanStatus.APPROVED,
        plan_hash="0" * 64, target_refs=(), evidence_version="e", policy_version=policy.POLICY_VERSION,
        plan_expires_at_ms=START_MS + 1, approval=None, validation_passed=True, validated_deployed_version="v",
        deployed_version="v", prior_action_exists=False,
    )
    with pytest.raises(ContractError, match="inside the executor's write transaction"):
        policy.recheck_reprocess(conn, facts, now_ms=START_MS, stale_after_ms=1)
    with storage.write_transaction(conn):
        outcome = policy.recheck_reprocess(conn, facts, now_ms=START_MS, stale_after_ms=1)
    assert {failure.code for failure in outcome.failures} == {
        policy.RecheckFailure.APPROVAL_MISSING, policy.RecheckFailure.PLAN_HASH_MISMATCH, policy.RecheckFailure.EVIDENCE_CHANGED,
    }


def reprocess_facts(
    run: SeededRun, *, plan_run_id: str | None = None, approval: policy.ApprovalRecord | None = None
) -> policy.ReprocessFacts:
    """An approved plan with no targets whose stored hash and evidence version no longer match its fields."""
    return policy.ReprocessFacts(
        run_id=run.run_id, plan_run_id=plan_run_id or run.run_id, plan_id="plan_x", plan_status=PlanStatus.APPROVED,
        plan_hash="0" * 64, target_refs=(), evidence_version="e", policy_version=policy.POLICY_VERSION,
        plan_expires_at_ms=START_MS + 1, approval=approval, validation_passed=True, validated_deployed_version="v",
        deployed_version="v", prior_action_exists=False,
    )


def recheck(conn: sqlite3.Connection, facts: policy.ReprocessFacts) -> policy.RecheckOutcome:
    with storage.write_transaction(conn):
        return policy.recheck_reprocess(conn, facts, now_ms=START_MS, stale_after_ms=1)


def test_without_an_approval_the_recheck_reports_every_check_except_the_approvals_own(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """Those three have no approval to examine, so they are never evaluated and the recheck does not report them."""
    outcome = recheck(conn, reprocess_facts(run_a))
    never_evaluated = {policy.RecheckFailure.APPROVER_NOT_HUMAN, policy.RecheckFailure.APPROVAL_HASH_MISMATCH,
                       policy.RecheckFailure.APPROVAL_EXPIRED}
    assert [check.code for check in outcome.checks] == [
        code for code in policy.RECHECK_DESCRIPTIONS if code not in never_evaluated]
    failed = {check.code: check.details for check in outcome.checks if not check.passed}
    assert failed == {
        policy.RecheckFailure.PLAN_HASH_MISMATCH: ("stored plan fields no longer hash to its plan hash",),
        policy.RecheckFailure.APPROVAL_MISSING: ("plan 'plan_x' has no approval",),
        policy.RecheckFailure.EVIDENCE_CHANGED: ("the targets' state changed since the plan was built",),
    }
    # Every other reported check passed, the targets' eligibility too: it was evaluated over no targets.
    assert all(check.passed for check in outcome.checks if check.code not in failed)
    assert [(failure.code, failure.detail) for failure in outcome.failures] == [
        (code, detail) for code, details in failed.items() for detail in details]


def test_the_recheck_reports_each_check_once_and_keeps_its_failures_in_the_order_found(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """run_scope is evaluated on the plan and again on the approval: one check, both details. The failures keep
    the order they were found in, the order a rejected action records, which is not the checks' order."""
    approval = policy.ApprovalRecord(operator_id="qwen3:8b", run_id="run_unit_other", plan_hash="0" * 64,
                                     expires_at_ms=START_MS)
    outcome = recheck(conn, reprocess_facts(run_a, plan_run_id="run_unit_other", approval=approval))
    assert [check.code for check in outcome.checks] == list(policy.RECHECK_DESCRIPTIONS)
    assert {check.code.value: check.details for check in outcome.checks if not check.passed} == {
        "run_scope": ("plan belongs to run 'run_unit_other'", "approval belongs to run 'run_unit_other'"),
        "plan_hash_mismatch": ("stored plan fields no longer hash to its plan hash",),
        "approver_not_human": ("'qwen3:8b' is not a human operator",),
        "approval_expired": (f"approval expired at {START_MS}, now {START_MS}",),
        "evidence_changed": ("the targets' state changed since the plan was built",),
    }
    assert [failure.code.value for failure in outcome.failures] == [
        "run_scope", "plan_hash_mismatch", "approver_not_human", "run_scope", "approval_expired", "evidence_changed"]
    assert not outcome.passed


def test_a_repeated_target_fails_the_plan_hash_with_the_contract_error_and_each_copy_is_assessed(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """Repeated targets cannot hash: ``compute_plan_hash`` refuses them, so the plan-hash check fails with that error
    rather than a mismatch. Each copy of the target is still assessed, in the order a rejected action records."""
    repeated = policy.TargetRef(order_id="order_unit_x", inbox_id=987_654, event_id="evt_unit_x",
                                payment_id="pay_unit_x", amount_paise=AMOUNT, currency="INR")
    outcome = recheck(conn, dataclasses.replace(reprocess_facts(run_a), target_refs=(repeated, repeated)))
    unhashable = "plan target IDs must be unique, got repeats in ['order_unit_x', 'order_unit_x']"
    missing = "order_unit_x: inbox row 987654 is not in the run"
    changed = "the targets' state changed since the plan was built"
    assert [(failure.code.value, failure.detail) for failure in outcome.failures] == [
        ("plan_hash_mismatch", unhashable), ("approval_missing", "plan 'plan_x' has no approval"),
        ("target_ineligible", missing), ("target_ineligible", missing), ("evidence_changed", changed),
    ]
    assert {check.code.value: check.details for check in outcome.checks if not check.passed} == {
        "plan_hash_mismatch": (unhashable,), "approval_missing": ("plan 'plan_x' has no approval",),
        "target_ineligible": (missing, missing), "evidence_changed": (changed,),
    }
    assert not outcome.passed


CHECKED_AT = START_MS + 60_000  # when ``assess`` judges order 1's capture eligible
FRESH_FOR_MS = 600_000  # ``assess``'s threshold


def stored_target(assessment: policy.TargetAssessment, **altered: object) -> policy.TargetRef:
    """The target as a plan stores it from ``assessment``, with ``altered`` fields changed in storage afterwards."""
    stored = policy.TargetRef(
        order_id=assessment.order_id, inbox_id=assessment.inbox_id, event_id=assessment.event_id,
        payment_id=assessment.payment_id, amount_paise=assessment.amount_paise, currency=assessment.currency,
    )
    return dataclasses.replace(stored, **altered)


def approved_one_target_facts(
    run: SeededRun, assessment: policy.TargetAssessment, ref: policy.TargetRef
) -> policy.ReprocessFacts:
    """An approved plan of the one target ``ref``, its hash, evidence version and approval made from ``assessment``:
    no other check has anything to refuse."""
    evidence = policy.evidence_version([assessment])
    expires_at_ms = CHECKED_AT + 60_000
    plan_hash = policy.compute_plan_hash(
        operation=Operation.REPROCESS_INBOX, target_ids=[assessment.order_id], evidence_version=evidence,
        policy_version=policy.POLICY_VERSION, run_id=run.run_id, expires_at_ms=expires_at_ms,
    )
    approval = policy.ApprovalRecord(operator_id="op_unit_operator", run_id=run.run_id, plan_hash=plan_hash,
                                     expires_at_ms=expires_at_ms)
    return dataclasses.replace(reprocess_facts(run, approval=approval), plan_hash=plan_hash, target_refs=(ref,),
                               evidence_version=evidence, plan_expires_at_ms=expires_at_ms)


def recheck_fresh(conn: sqlite3.Connection, facts: policy.ReprocessFacts) -> policy.RecheckOutcome:
    with storage.write_transaction(conn):
        return policy.recheck_reprocess(conn, facts, now_ms=CHECKED_AT, stale_after_ms=FRESH_FOR_MS)


def test_a_stored_target_equal_to_its_event_passes_every_check(conn: sqlite3.Connection, run_a: SeededRun) -> None:
    """The control for the two tests below: the facts they alter pass every check as they are."""
    assessment = assess(conn, run_a, capture_row(conn, run_a), CHECKED_AT)
    outcome = recheck_fresh(conn, approved_one_target_facts(run_a, assessment, stored_target(assessment)))
    assert [check.code for check in outcome.checks] == list(policy.RECHECK_DESCRIPTIONS)
    assert all(check.passed for check in outcome.checks)
    assert outcome.passed


@pytest.mark.parametrize(("field", "stored", "detail"), [
    ("event_id", "evt_capture_9", f"{ORDER_1}: plan stores event_id 'evt_capture_9', its event has 'evt_capture_1'"),
    ("payment_id", PAY_2, f"{ORDER_1}: plan stores payment_id {PAY_2!r}, its event has {PAY_1!r}"),
    ("amount_paise", 2 * AMOUNT, f"{ORDER_1}: plan stores amount_paise 240000, its event has 120000"),
    ("currency", "USD", f"{ORDER_1}: plan stores currency 'USD', its event has 'INR'"),
], ids=["event_id", "payment_id", "amount_paise", "currency"])
def test_a_stored_target_field_unlike_its_event_fails_target_ineligible_alone(
    conn: sqlite3.Connection, run_a: SeededRun, field: str, stored: object, detail: str
) -> None:
    """The plan hash binds the order IDs and the evidence version hashes the event itself, so a stored field changed
    after approval passes both; the comparison with the event refuses it, naming the order, the field and both values."""
    assessment = assess(conn, run_a, capture_row(conn, run_a), CHECKED_AT)
    altered = stored_target(assessment, **{field: stored})
    outcome = recheck_fresh(conn, approved_one_target_facts(run_a, assessment, altered))
    assert [(failure.code, failure.detail) for failure in outcome.failures] == [
        (policy.RecheckFailure.TARGET_INELIGIBLE, detail)]
    assert {check.code: check.details for check in outcome.checks if not check.passed} == {
        policy.RecheckFailure.TARGET_INELIGIBLE: (detail,)}


def test_every_stored_field_unlike_its_event_is_reported_before_the_events_own_reasons(
    conn: sqlite3.Connection, run_a: SeededRun
) -> None:
    """The recheck records every reason: each differing field in ``TargetRef`` order, then why the event itself is no
    longer eligible (a refund here)."""
    row = capture_row(conn, run_a)
    store_observations(conn, [
        observation(PaymentStatus.REFUNDED, START_MS + 20_000, START_MS + 30_000, amount_refunded_paise=AMOUNT),
    ])
    assessment = assess(conn, run_a, row, CHECKED_AT)
    assert "refunded" in assessment.reasons
    altered = stored_target(assessment, event_id="evt_capture_9", payment_id=PAY_2, amount_paise=1, currency="USD")
    outcome = recheck_fresh(conn, approved_one_target_facts(run_a, assessment, altered))
    assert [(failure.code.value, failure.detail) for failure in outcome.failures] == [
        ("target_ineligible", f"{ORDER_1}: plan stores event_id 'evt_capture_9', its event has 'evt_capture_1'"),
        ("target_ineligible", f"{ORDER_1}: plan stores payment_id {PAY_2!r}, its event has {PAY_1!r}"),
        ("target_ineligible", f"{ORDER_1}: plan stores amount_paise 1, its event has 120000"),
        ("target_ineligible", f"{ORDER_1}: plan stores currency 'USD', its event has 'INR'"),
        ("target_ineligible", f"{ORDER_1}: {', '.join(assessment.reasons)}"),
    ]
