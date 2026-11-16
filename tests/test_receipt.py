"""The recovery receipt (phase 11): one deterministic record per executed or rejected reprocess action.

The batch tests (``test_plans.build_batch``: 41 targets, 12 exclusions) pin the content of every section: the
totals against the committed effects, a cancellation after approval, a plan-level refusal that leaves every order
untouched, a missing approval, and a receipt for an action recorded before the audits existed. Flow A runs end to
end through the CLI on the real rules baseline, twice in separate databases (the same hash, different random IDs),
and once through the API route. Migration 3 is applied to a database that already holds a run.

Since receipt-2 (Q52) the hash leaves out the database's own inbox row numbers. The README's demo of Flow A gives one
receipt hash whether its run was the first its data directory seeded or came after other runs, and so does the batch's
recovery after another run's events took the first inbox rows; another target set, amount or outcome still hashes
differently.
"""
from __future__ import annotations

import io
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import (
    INTEGRATION_A,
    INTEGRATION_B,
    MERCHANT_A,
    MERCHANT_B,
    PAY_1,
    RUN_A,
    RUN_B,
    SECRET_A,
    SECRET_B,
    add_order,
    count_rows,
    deliver,
    seed_run,
)
from test_api import create_run, diagnose, get, log_in, make_client, post
from test_executor import APPROVAL_TTL_MS, APPROVED_AT, EXECUTED_AT, approved_plan, execute, rejection
from test_plans import (
    INCIDENT,
    NOW,
    OPERATOR,
    PLAN_TTL_MS,
    STALE_AFTER_MS,
    UNKNOWN_ORDER,
    Batch,
    build_batch,
    build_plan,
    order_id,
    validate,
)

import recovery_workbench
from recovery_workbench import cli, diagnosis, diagnosis_store, executor, fixes, plans, policy, report, selective, storage
from recovery_workbench.contracts import ContractError, DiagnosisStatus, ReceiveOutcome, ScopeError
from recovery_workbench.simulator import merchant, scenarios, signing
from recovery_workbench.simulator.world import run_id_for

SECTIONS = ("receipt_version", "run_id", "versions", "evidence", "proposal", "approved_operation", "preconditions",
            "execution", "postconditions", "value", "boundary")
ALL_CHECKS = (*(code.value for code in policy.RECHECK_DESCRIPTIONS), executor.RERUN_CHECK)
AUDIT_TABLES = ("plan_audits", "action_audits")
# What the batch's plan leaves out and nobody has fulfilled: every exclusion except order 44, which the worker
# fulfilled before the fault. Their order value; the unknown order has no order record, so it is counted only.
LEFT_OUT = frozenset({order_id(number) for number in (45, 46, 47, 48, 49, 51, 52, 53, 54, 56)} | {UNKNOWN_ORDER})
LEFT_OUT_VALUE_PAISE = 501_000


@pytest.fixture
def batch(conn: sqlite3.Connection) -> Batch:
    return build_batch(conn)


def receipt_for(conn: sqlite3.Connection, action_id: str) -> dict:
    return report.build_receipt(conn, run_id=RUN_A, action_id=action_id)


def check_results(receipt: dict) -> dict[str, str]:
    return {check["code"]: check["result"] for check in receipt["preconditions"]["execution"]["checks"]}


def executed_receipt(conn: sqlite3.Connection, batch: Batch) -> tuple[plans.PlanPreview, executor.ExecutionResult, dict]:
    preview = approved_plan(conn, batch)
    result = execute(conn, batch, preview.plan.plan_id)
    return preview, result, receipt_for(conn, result.action_id)


# --------------------------------------------------------------------------- an executed plan, section by section


def test_an_executed_plan_gives_a_receipt_with_every_section(conn: sqlite3.Connection, batch: Batch) -> None:
    preview, result, receipt = executed_receipt(conn, batch)
    approval = executor.get_approval(conn, run_id=RUN_A, plan_id=preview.plan.plan_id)
    assert approval is not None
    assert report.HASHED_SECTIONS == SECTIONS
    assert set(receipt) == {*SECTIONS, "receipt_hash", "hashed_sections", "hash_rule", "identity", "wall_clock"}
    assert receipt["receipt_hash"] == report.receipt_hash_of(receipt)
    assert (receipt["receipt_version"], receipt["hash_rule"]) == ("receipt-2", report.HASH_RULE)
    markdown = report.render_receipt_markdown(receipt)  # states the hash and its rule as the JSON does
    assert markdown.startswith("# Recovery receipt `receipt-2`")
    assert f"Receipt hash `{receipt['receipt_hash']}`: SHA-256" in markdown
    assert report.HASH_RULE in markdown
    assert receipt["identity"] == {
        "run_id": RUN_A, "incident_id": INCIDENT, "diagnosis_id": batch.diagnosis_id,
        "fix_validation_id": preview.plan.fix_validation_id, "plan_id": preview.plan.plan_id,
        "approval_id": approval.approval_id, "action_id": result.action_id, "operation_key": result.operation_key,
        "recorded_from": None,
    }
    assert receipt["versions"] == {
        "application": recovery_workbench.__version__, "simulator_generator": "unit-tests",
        "policy": policy.POLICY_VERSION, "prompt": None, "model": None, "model_digest": None, "thinking": None,
        "validator": diagnosis.VALIDATOR_VERSION, "rules": "rules-unit",
    }
    proposal = receipt["proposal"]
    assert (proposal["mode"], proposal["status"], proposal["hypothesis"], proposal["faulting_change_id"],
            proposal["proposed_operation"]) == ("rules", "validated", "merchant_worker_regression",
                                                "dep_payload_accessor", "reprocess_inbox")
    assert (proposal["supporting_evidence_ids"], proposal["unsafe_proposals"]) == (["ev_worker_errors"], 0)
    approved = receipt["approved_operation"]
    assert approved["target_ids"] == sorted(batch.eligible) == sorted(preview.plan.target_ids)
    assert (approved["operation"], approved["target_count"], approved["target_value_paise"], approved["plan_hash"]) == (
        "reprocess_inbox", 41, sum(batch.eligible.values()), preview.plan.plan_hash)
    assert approved["approval"] == {"operator_id": OPERATOR, "approved_at_ms": APPROVED_AT,
                                    "expires_at_ms": APPROVED_AT + APPROVAL_TTL_MS,
                                    "plan_hash": preview.plan.plan_hash, "matches_plan_hash": True}
    assert (approved["fix"]["fix_id"], approved["fix"]["reproduced"], approved["fix"]["passed"]) == ("payload@5", True, True)
    assert (receipt["execution"]["status"], receipt["execution"]["executed_at_ms"]) == ("succeeded", EXECUTED_AT)
    assert receipt["execution"]["results"] == {"completed": 41, "no_op": 0, "refused": 0, "failed": 0}
    assert check_results(receipt) == dict.fromkeys(ALL_CHECKS, "passed")
    assert receipt["boundary"] == {"environment": "synthetic", "provider_calls_made": 0,
                                   "original_capture_unchanged": True, "statement": report.BOUNDARY_STATEMENT}
    assert receipt["wall_clock"]["recorded_original_wall_ms"] is None
    assert "exported" not in json.dumps(receipt)  # the export time is never part of a receipt


def test_the_planning_section_is_what_the_operator_was_shown(conn: sqlite3.Connection, batch: Batch) -> None:
    preview, _, receipt = executed_receipt(conn, batch)
    planning = receipt["preconditions"]["planning"]
    assert {item["order_id"]: set(item["reasons"]) for item in planning["exclusions"]} == batch.expected_exclusions
    assert planning["exclusions_by_reason"] == {reason: list(orders) for reason, orders in
                                                preview.exclusions_by_reason.items()}
    assert planning["dry_runs"] == [dry_run.model_dump(mode="json") for dry_run in preview.dry_runs]
    assert (planning["candidate_orders"], planning["targets"], planning["dry_runs_passed"]) == (
        preview.candidate_orders, 41, 41)
    assert planning["superseded_inbox_ids"] == {order: list(ids) for order, ids in preview.superseded_inbox_ids.items()}
    assert (planning["deployed_version"], planning["corrected_version"], planning["stale_after_ms"]) == (
        preview.deployed_version, preview.corrected_version, STALE_AFTER_MS)


def test_the_evidence_section_names_every_observation_a_target_was_judged_on(
    conn: sqlite3.Connection, batch: Batch
) -> None:
    preview, _, receipt = executed_receipt(conn, batch)
    evidence = receipt["evidence"]
    assert evidence["planned_evidence_version"] == evidence["evidence_version_at_execution"] == preview.plan.evidence_version
    assert (evidence["evidence_unchanged"], evidence["stale_after_ms"], evidence["packet_hash"]) == (True, STALE_AFTER_MS,
                                                                                                    "0" * 64)
    assert [target["order_id"] for target in evidence["targets"]] == [target.id for target in preview.targets]
    for target in evidence["targets"]:
        known = [item["known_at_ms"] for item in target["observations"]]
        assert known == sorted(known)
        assert target["newest_known_at_ms"] == max(known)
        assert target["freshness_ms"] == EXECUTED_AT - max(known) <= STALE_AFTER_MS
        assert {item["status"] for item in target["observations"]} >= {"captured"}
    assert evidence["snapshot_ids"] == sorted({item["source_ref"] for target in evidence["targets"]
                                               for item in target["observations"] if item["source"] == "snapshot"})


def test_the_receipt_totals_equal_the_committed_unique_effects(conn: sqlite3.Connection, batch: Batch) -> None:
    _, result, receipt = executed_receipt(conn, batch)
    committed = [item for item in storage.list_fulfilments(conn, run_id=RUN_A) if item.action_id == result.action_id]
    completed = [order for order in receipt["execution"]["orders"] if order["result"] == "completed"]
    assert len({item.order_id for item in committed}) == len(committed) == len(completed) == 41
    assert {order["order_id"]: (order["payment_id"], order["amount_paise"]) for order in completed} == {
        item.order_id: (item.payment_id, item.amount_paise) for item in committed}
    assert receipt["value"]["restored_processing_value_paise"] == sum(item.amount_paise for item in committed) == (
        result.restored_processing_value_paise)
    measured = receipt["postconditions"]
    ledger = executor._capture_ledger(conn, run_id=RUN_A, as_of_ms=EXECUTED_AT)  # the executor's own ledger
    assert measured["captured_paise_before"] == measured["captured_paise_after"] == ledger.captured_paise
    assert measured["captured_payments_before"] == measured["captured_payments_after"] == len(ledger.captured_payment_ids)
    assert (measured["new_captured_payment_ids"], receipt["value"]["additional_captured_paise"]) == ([], 0)
    assert (measured["duplicate_fulfilments"], measured["payments_fulfilling_several_orders"]) == (0, [])
    assert {(item["order_captures_before"], item["order_captures_after"]) for item in measured["targets"]} == {(1, 1)}
    assert all(item["payment_captured_paise_before"] == item["payment_captured_paise_after"] == batch.eligible[item["order_id"]]
               for item in measured["targets"])


def test_a_partial_recovery_lists_and_values_what_it_left_out(conn: sqlite3.Connection, batch: Batch) -> None:
    _, _, receipt = executed_receipt(conn, batch)
    outstanding = {item["order_id"]: item for item in receipt["postconditions"]["outstanding"]}
    assert set(outstanding) == LEFT_OUT
    assert all(item["stage"] == "planning" and set(item["reasons"]) == batch.expected_exclusions[order]
               for order, item in outstanding.items())
    orders = {order.order_id: order.amount_paise for order in storage.list_orders(conn, run_id=RUN_A)}
    assert sum(orders[order] for order in LEFT_OUT if order in orders) == LEFT_OUT_VALUE_PAISE
    assert receipt["value"] == {
        "recorded": True, **{f"{name.removesuffix('_paise')}_definition": text
                             for name, text in report.VALUE_DEFINITIONS.items()},
        "currency": "INR", "restored_processing_value_paise": sum(batch.eligible.values()),
        "additional_captured_paise": 0, "unresolved_value_paise": LEFT_OUT_VALUE_PAISE,
        "unresolved_orders": len(LEFT_OUT), "unvalued_unresolved_order_ids": [UNKNOWN_ORDER],
    }


# --------------------------------------------------------------------------- determinism


def test_regenerating_a_receipt_gives_identical_bytes(
    conn: sqlite3.Connection, batch: Batch, open_connection: Callable[[], sqlite3.Connection], tmp_path: Path
) -> None:
    preview, result, receipt = executed_receipt(conn, batch)
    first_bytes, first_markdown = report.receipt_bytes(receipt), report.render_receipt_markdown(receipt)
    # Later work in the run leaves the receipt alone: a second plan over what is left, built a minute later.
    plans.build_reprocess_plan(conn, run_id=RUN_A, fix_validation_id=str(preview.plan.fix_validation_id),
                               deployed=batch.deployed, now_ms=EXECUTED_AT + 60_000, ttl_ms=PLAN_TTL_MS,
                               stale_after_ms=STALE_AFTER_MS)
    again = receipt_for(open_connection(), result.action_id)
    assert report.receipt_bytes(again) == first_bytes
    assert report.render_receipt_markdown(again) == first_markdown
    json_path, markdown_path, digest = report.write_receipt(conn, run_id=RUN_A, action_id=result.action_id,
                                                            directory=tmp_path / "receipts")
    assert (json_path.read_bytes(), markdown_path.read_text(encoding="utf-8"), digest) == (
        first_bytes, first_markdown, receipt["receipt_hash"])
    assert json_path.name == f"receipt_{result.action_id}.json"


def test_the_hash_covers_the_hashed_sections_and_nothing_else(conn: sqlite3.Connection, batch: Batch) -> None:
    _, _, receipt = executed_receipt(conn, batch)
    renamed = {**receipt, "identity": {**receipt["identity"], "plan_id": "plan_elsewhere"},
               "wall_clock": {"diagnosis_created_wall_ms": 1}}
    assert report.receipt_hash_of(renamed) == receipt["receipt_hash"]
    altered = {**receipt, "value": {**receipt["value"], "unresolved_value_paise": 0}}
    assert report.receipt_hash_of(altered) != receipt["receipt_hash"]
    # Q52: the digests computed from inbox row numbers differ in a database where other runs came first, so they are
    # left out; whether they match is hashed.
    approved, evidence = receipt["approved_operation"], receipt["evidence"]
    redigested = {
        **receipt,
        "approved_operation": {**approved, "plan_hash": "1" * 64,
                               "approval": {**approved["approval"], "plan_hash": "1" * 64}},
        "evidence": {**evidence, "planned_evidence_version": "2" * 64, "evidence_version_at_execution": "2" * 64},
    }
    assert report.receipt_hash_of(redigested) == receipt["receipt_hash"]
    unmatched = {**receipt,
                 "approved_operation": {**approved, "approval": {**approved["approval"], "matches_plan_hash": False}}}
    assert report.receipt_hash_of(unmatched) != receipt["receipt_hash"]
    changed = {**receipt, "evidence": {**evidence, "evidence_unchanged": False}}
    assert report.receipt_hash_of(changed) != receipt["receipt_hash"]
    with pytest.raises(ContractError, match="recomputes receipt-2 hashes, and this receipt is 'receipt-1'"):
        report.receipt_hash_of({**receipt, "receipt_version": "receipt-1"})  # the two kinds of hash are never compared


# --------------------------------------------------------------------------- rejected actions


def test_a_cancellation_after_approval_gives_a_receipt_that_shows_the_failed_check(
    conn: sqlite3.Connection, batch: Batch, open_connection: Callable[[], sqlite3.Connection]
) -> None:
    preview = approved_plan(conn, batch)
    second = open_connection()
    with storage.write_transaction(second):  # the shop cancels one target order from another connection
        second.execute("UPDATE orders SET cancelled_at_ms = ? WHERE run_id = ? AND order_id = ?",
                       (APPROVED_AT + 1_000, RUN_A, order_id(7)))
    fulfilled_before = count_rows(conn, "fulfilments", RUN_A)
    result = rejection(conn, batch, preview.plan.plan_id)
    receipt = receipt_for(conn, result.action_id)
    assert receipt["execution"]["status"] == "rejected"
    assert check_results(receipt) == {**dict.fromkeys(ALL_CHECKS, "passed"), "target_ineligible": "failed",
                                      "evidence_changed": "failed", executor.RERUN_CHECK: "not_run"}
    checks = {check["code"]: check for check in receipt["preconditions"]["execution"]["checks"]}
    assert checks["target_ineligible"]["details"] == [f"{order_id(7)}: cancelled"]
    # The failed checks are exactly the reasons the rejected action itself recorded.
    assert sorted(f"{code}: {detail}" for code, check in checks.items() for detail in check["details"]) == sorted(
        result.reasons)
    orders = {order["order_id"]: order for order in receipt["execution"]["orders"]}
    assert (orders[order_id(7)]["result"], orders[order_id(7)]["reasons"]) == ("refused", ["cancelled"])
    assert {(order["result"], tuple(order["reasons"])) for key, order in orders.items() if key != order_id(7)} == {
        ("no_op", (executor.PLAN_REJECTED,))}
    assert receipt["execution"]["results"] == {"completed": 0, "no_op": 40, "refused": 1, "failed": 0}
    assert receipt["evidence"]["evidence_unchanged"] is False
    assert (receipt["value"]["restored_processing_value_paise"], receipt["value"]["additional_captured_paise"]) == (0, 0)
    assert receipt["value"]["unresolved_value_paise"] == sum(batch.eligible.values()) + LEFT_OUT_VALUE_PAISE
    assert {item["order_id"] for item in receipt["postconditions"]["outstanding"]} == set(batch.eligible) | LEFT_OUT
    assert receipt["boundary"]["original_capture_unchanged"] is True
    assert count_rows(conn, "fulfilments", RUN_A) == fulfilled_before


def test_a_plan_refused_by_a_plan_level_check_is_a_no_op_for_every_order(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = approved_plan(conn, batch)
    redeployed = merchant.compose({"payload": 2, "notifications": 2}, version="dep_later_notifications")
    result = rejection(conn, batch, preview.plan.plan_id, deployed=redeployed)
    receipt = receipt_for(conn, result.action_id)
    assert check_results(receipt) == {**dict.fromkeys(ALL_CHECKS, "passed"), "deployed_version_changed": "failed",
                                      executor.RERUN_CHECK: "not_run"}
    assert {(order["result"], tuple(order["reasons"])) for order in receipt["execution"]["orders"]} == {
        ("no_op", (executor.PLAN_REJECTED,))}
    assert receipt["execution"]["results"] == {"completed": 0, "no_op": 41, "refused": 0, "failed": 0}
    assert receipt["evidence"]["evidence_unchanged"] is True
    assert receipt["value"]["restored_processing_value_paise"] == 0
    assert receipt["boundary"]["original_capture_unchanged"] is True


def test_a_plan_from_a_needs_review_model_diagnosis_shows_no_cause_and_the_model_versions(
    conn: sqlite3.Connection, batch: Batch
) -> None:
    """A fix can be validated against any stored diagnosis, so a plan may rest on one that never validated."""
    stored = diagnosis_store.record_model_diagnosis(
        conn, run_id=RUN_A, incident_id=INCIDENT, model_id="qwen3:8b", model_digest=None,
        run=diagnosis.DiagnosisRun(status=DiagnosisStatus.NEEDS_REVIEW, output=None,
                                   reasons=("call 1: the overall deadline of 90 s passed before call 1",), calls=(),
                                   repairs=0, elapsed_ms=90_000, think=False, deadline_s=90.0,
                                   prompt_version=diagnosis.PROMPT_VERSION, packet_hash="0" * 64),
    )
    validation = fixes.validate_fix(conn, run_id=RUN_A, deployed=batch.deployed, fix_id="payload@5",
                                    diagnosis_id=stored.diagnosis_id, operator_id=OPERATOR, now_ms=NOW)
    preview = build_plan(conn, batch, validation)
    executor.approve_plan(conn, run_id=RUN_A, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                          operator_id=OPERATOR, now_ms=APPROVED_AT, ttl_ms=APPROVAL_TTL_MS)
    receipt = receipt_for(conn, execute(conn, batch, preview.plan.plan_id).action_id)
    proposal = receipt["proposal"]
    assert (proposal["mode"], proposal["status"], proposal["hypothesis"], proposal["supporting_evidence_ids"],
            proposal["validator_reasons"]) == ("local", "needs_review", None, None, 1)
    versions = receipt["versions"]
    assert (versions["model"], versions["model_digest"], versions["prompt"], versions["thinking"], versions["rules"]) == (
        "qwen3:8b", None, diagnosis.PROMPT_VERSION, False, None)
    assert receipt["wall_clock"]["diagnosis_created_wall_ms"] == stored.created_wall_ms  # kept, but outside the hash
    assert "- Cause: none; faulting change none" in report.render_receipt_markdown(receipt)


def test_without_an_approval_its_own_checks_are_not_run(conn: sqlite3.Connection, batch: Batch) -> None:
    preview = build_plan(conn, batch, validate(conn, batch))
    result = rejection(conn, batch, preview.plan.plan_id)
    receipt = receipt_for(conn, result.action_id)
    results = check_results(receipt)
    assert (results["approval_missing"], results["plan_not_approved"]) == ("failed", "failed")
    assert {code for code, outcome in results.items() if outcome == "not_run"} == {
        "approver_not_human", "approval_hash_mismatch", "approval_expired", executor.RERUN_CHECK}
    assert (receipt["approved_operation"]["approval"], receipt["identity"]["approval_id"]) == (None, None)


# --------------------------------------------------------------------------- scope, other actions, older records


def test_a_receipt_is_only_for_a_reprocess_action_of_its_own_run(conn: sqlite3.Connection, batch: Batch) -> None:
    _, result, _ = executed_receipt(conn, batch)
    escalation = plans.escalate(conn, run_id=RUN_A, incident_id=INCIDENT, diagnosis_id=batch.diagnosis_id,
                                operator_id=OPERATOR, summary="A person decides the held orders", evidence_needed=(),
                                subject_ids=(INCIDENT,), now_ms=NOW)
    with pytest.raises(ContractError, match="receipts cover reprocess_inbox actions"):
        report.build_receipt(conn, run_id=RUN_A, action_id=escalation.action_id)
    with pytest.raises(ScopeError, match="is not part of run"):
        report.build_receipt(conn, run_id=RUN_A, action_id="act_of_nobody")
    seed_run(conn, "run_unit_other", MERCHANT_A, "int_unit_other", SECRET_A)
    with pytest.raises(ScopeError, match="is not part of run"):
        report.build_receipt(conn, run_id="run_unit_other", action_id=result.action_id)


def test_an_action_recorded_before_the_audits_still_gets_a_receipt(
    conn: sqlite3.Connection, batch: Batch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The code before migration 3 wrote no audits; skipping the two writes leaves the rows such a version left."""
    monkeypatch.setattr(storage, "insert_plan_audit", lambda _conn, audit: audit)
    monkeypatch.setattr(storage, "insert_action_audit", lambda _conn, audit: audit)
    preview = approved_plan(conn, batch)
    result = execute(conn, batch, preview.plan.plan_id)
    monkeypatch.undo()
    assert [count_rows(conn, table, RUN_A) for table in AUDIT_TABLES] == [0, 0]
    receipt = receipt_for(conn, result.action_id)
    assert receipt["preconditions"] == {"planning": {"recorded": False}, "execution": {"recorded": False}}
    assert receipt["postconditions"] == {"recorded": False}
    assert (receipt["execution"]["recorded"], receipt["execution"]["fulfilled_order_ids"]) == (False, sorted(batch.eligible))
    assert (receipt["value"]["restored_processing_value_paise"], receipt["value"]["unresolved_value_paise"]) == (
        sum(batch.eligible.values()), None)
    assert (receipt["versions"]["application"], receipt["boundary"]["original_capture_unchanged"]) == (None, None)
    assert receipt["approved_operation"]["target_ids"] == sorted(batch.eligible)
    assert report.receipt_bytes(receipt_for(conn, result.action_id)) == report.receipt_bytes(receipt)
    assert "Not recorded (executed before receipt records)" in report.render_receipt_markdown(receipt)


# A CSI that clears the screen, an OSC 52 clipboard write ended by BEL, the 8-bit CSI U+009B, a right-to-left override
# and an invisible tag character from outside the BMP: none may reach a terminal that prints the Markdown receipt.
TERMINAL_CONTROL = "\x1b[2J\x1b]52;c;QQ==\x07\x9b\u202e\U000e0041"


def test_markdown_renders_stored_text_inert(conn: sqlite3.Connection, batch: Batch) -> None:
    _, _, receipt = executed_receipt(conn, batch)
    checks = receipt["preconditions"]["execution"]["checks"]
    hostile = {**receipt, "versions": {**receipt["versions"], "model_digest": TERMINAL_CONTROL},
               "preconditions": {**receipt["preconditions"], "execution": {
                   **receipt["preconditions"]["execution"],
                   "checks": [{**checks[0], "result": "failed",
                               "details": ["a|b <i>c</i> [d](e) `f`\n# g", TERMINAL_CONTROL]}, *checks[1:]]}}}
    markdown = report.render_receipt_markdown(hostile)
    assert r"a\|b \<i\>c\</i\> \[d\]\(e\) \`f\` \# g" in markdown
    assert "<i>" not in markdown
    assert not [line for line in markdown.splitlines() if line.startswith("# g")]
    # Each control or format character shows as its escape: as is in a code span, with the backslash escaped in text.
    assert r"- model digest: `\u001b[2J\u001b]52;c;QQ==\u0007\u009b\u202e\U000e0041`" in markdown
    assert r"\\u001b\[2J\\u001b\]52;c;QQ==\\u0007\\u009b\\u202e\\U000e0041 |" in markdown
    assert [char for char in markdown if unicodedata.category(char) in ("Cc", "Cf") and char != "\n"] == []


# Runs of whitespace and line breaks around C0 and C1 controls (a CSI, an OSC 52 ended by BEL, DEL, NEL and the 8-bit
# CSI), a no-break space, a soft hyphen, zero-width and bidi characters, the line and paragraph separators, a BOM, an
# emoji and a tag character. The receipt collapses the whitespace, NEL and the separators included, then escapes each
# Cc or Cf character left: as is in a code span, with the backslash and brackets escaped in text.
VARIED_TEXT = " a\t\tb\r\n\x1b[2J\x1b]52;c;QQ==\x07\x7f\x85\x9b\xa0\xad\u200b\u202e\u2028\u2029\ufeff\U0001f600\U000e0041 c "
VARIED_IN_CODE = r"a b \u001b[2J\u001b]52;c;QQ==\u0007\u007f \u009b \u00ad\u200b\u202e \ufeff" + "\U0001f600" + r"\U000e0041 c"
VARIED_IN_TEXT = (r"a b \\u001b\[2J\\u001b\]52;c;QQ==\\u0007\\u007f \\u009b \\u00ad\\u200b\\u202e \\ufeff" + "\U0001f600"
                  + r"\\U000e0041 c")


def test_markdown_still_collapses_whitespace_then_escapes_each_control_or_format_character(
    conn: sqlite3.Connection, batch: Batch
) -> None:
    """Q43 gave the CLI the receipt's escape; the receipt's own rule, and so its bytes, stay as they were."""
    _, _, receipt = executed_receipt(conn, batch)
    checks = receipt["preconditions"]["execution"]["checks"]
    varied = {**receipt, "versions": {**receipt["versions"], "model_digest": VARIED_TEXT},
              "preconditions": {**receipt["preconditions"], "execution": {
                  **receipt["preconditions"]["execution"],
                  "checks": [{**checks[0], "result": "failed", "details": [VARIED_TEXT]}, *checks[1:]]}}}
    lines = report.render_receipt_markdown(varied).splitlines()
    assert f"- model digest: `{VARIED_IN_CODE}`" in lines
    assert f"| `{checks[0]['code']}` | failed | {VARIED_IN_TEXT} |" in lines


# --------------------------------------------------------------------------- the migration on an existing database


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_the_migration_applies_to_a_database_that_already_holds_a_run(db_path: Path) -> None:
    # The tables migrations 1 and 2 built; 3 adds the audits, 6 the provider's delivery log (Q29 (b)).
    previous_tables = [table for table in storage.TABLES if table not in (*AUDIT_TABLES, "provider_delivery_attempts")]
    with closing(storage.connect(db_path)) as conn:
        # The database as the previous version left it: migrations 1 and 2 only (the private helper takes a prefix).
        assert storage._apply_migrations(conn, storage.MIGRATIONS[:2]) == (1, 2)
        batch = build_batch(conn)
        rows_before = {table: count_rows(conn, table, RUN_A) for table in previous_tables}
        assert not set(AUDIT_TABLES) & _table_names(conn)
    with closing(storage.connect(db_path)) as conn:  # the upgraded application opens it
        # 4 and 5: the receiver's body indexes (Q32 option a, Q34); 6: the provider's delivery log (Q29 (b));
        # 7: the attempt-number and conflict-subject indexes (Q40); 8: the hosted model mode (Q46)
        assert storage.migrate(conn) == (3, 4, 5, 6, 7, 8)
        assert storage.migrate(conn) == ()
        assert {table: count_rows(conn, table, RUN_A) for table in previous_tables} == rows_before
        assert [tuple(row) for row in conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")] == [
            (migration.version, migration.name) for migration in storage.MIGRATIONS]
        preview = approved_plan(conn, batch)
        result = execute(conn, batch, preview.plan.plan_id)
        receipt = receipt_for(conn, result.action_id)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), storage.write_transaction(conn):
            conn.execute("DELETE FROM action_audits WHERE run_id = ?", (RUN_A,))
    assert receipt["preconditions"]["planning"]["recorded"]
    assert receipt["preconditions"]["execution"]["recorded"]
    assert receipt["execution"]["results"]["completed"] == 41


def _applied_versions(database: Path) -> list[int]:
    with closing(storage.connect(database)) as conn:
        return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def test_cli_commands_migrate_an_older_data_directory_but_webhook_request_only_reads(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / cli.DATABASE_FILE
    with closing(storage.connect(database)) as conn:
        storage._apply_migrations(conn, storage.MIGRATIONS[:2])
        add_order(conn, seed_run(conn, RUN_A, MERCHANT_A, INTEGRATION_A, SECRET_A))
    with redirect_stdout(io.StringIO()):
        assert cli.main(["webhook-request", "--data-dir", str(data_dir), "--run-id", RUN_A, "--payment-id", PAY_1,
                         "--status", "captured", "--event-id", "evt_receipt_0001", "--integration-id", INTEGRATION_A,
                         "--output", str(tmp_path / "body.json")]) == 0
    assert _applied_versions(database) == [1, 2]
    with redirect_stderr(io.StringIO()) as refused:
        code = cli.main(["receipt", "--data-dir", str(data_dir), "--run-id", RUN_A, "--action-id", "act_of_nobody",
                         "--output-dir", str(tmp_path / "receipts")])
    assert code == cli.EXIT_USAGE_ERROR
    assert json.loads(refused.getvalue())["error"] == "ScopeError"
    assert _applied_versions(database) == [1, 2, 3, 4, 5, 6, 7, 8]


# --------------------------------------------------------------------------- Flow A end to end through the CLI

CLI_OPERATOR = "op_receipt_tester"


@dataclass(frozen=True)
class CliFlow:
    data_dir: Path
    run_id: str
    plan: dict
    executed: dict
    exported: dict  # what the receipt command printed


def cli_json(*argv: str) -> dict:
    printed, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(printed), redirect_stderr(errors):
        code = cli.main(list(argv))
    assert code == 0, errors.getvalue()
    return json.loads(printed.getvalue())


def flow_a(directory: Path) -> CliFlow:
    """Seed, diagnose with the real rules baseline, validate payload@5, plan, approve, execute, then the receipt."""
    data = ("--data-dir", str(directory / "data"))
    run_id = cli_json("seed", "--scenario", "worker_backlog", "--seed", "101", *data)["run_id"]
    diagnosed = cli_json("diagnose", "--run-id", run_id, "--mode", "rules", *data)
    assert (diagnosed["mode"], diagnosed["output"]["proposed_operation"]) == ("rules", "reprocess_inbox")
    validation = cli_json("validate-fix", "--run-id", run_id, "--fix-id", "payload@5", "--diagnosis-id",
                          diagnosed["diagnosis_id"], "--operator", CLI_OPERATOR, *data)
    assert validation["passed"]
    plan = cli_json("plan", "--run-id", run_id, "--fix-validation-id", validation["validation_id"], *data)
    cli_json("approve", "--run-id", run_id, "--plan-id", plan["plan_id"], "--plan-hash", plan["plan_hash"],
             "--operator", CLI_OPERATOR, *data)
    executed = cli_json("execute", "--run-id", run_id, "--plan-id", plan["plan_id"], *data)
    assert executed["status"] == "succeeded"
    exported = cli_json("receipt", "--run-id", run_id, "--action-id", executed["action_id"], "--output-dir",
                        str(directory / "receipts"), *data)
    return CliFlow(directory / "data", run_id, plan, executed, exported)


@pytest.fixture(scope="module")
def cli_flows(tmp_path_factory: pytest.TempPathFactory) -> tuple[CliFlow, CliFlow]:
    """The same Flow A in two separate data directories."""
    return flow_a(tmp_path_factory.mktemp("receipt_flow_first")), flow_a(tmp_path_factory.mktemp("receipt_flow_second"))


def test_flow_a_end_to_end_gives_a_receipt_the_database_regenerates_byte_for_byte(
    cli_flows: tuple[CliFlow, CliFlow]
) -> None:
    flow = cli_flows[0]
    exported_bytes = Path(flow.exported["json"]).read_bytes()
    with closing(storage.connect(flow.data_dir / cli.DATABASE_FILE)) as conn:
        regenerated = report.build_receipt(conn, run_id=flow.run_id, action_id=flow.executed["action_id"])
    assert report.receipt_bytes(regenerated) == exported_bytes
    assert Path(flow.exported["markdown"]).read_text(encoding="utf-8") == report.render_receipt_markdown(regenerated)
    receipt = json.loads(exported_bytes)
    assert flow.exported["receipt_hash"] == receipt["receipt_hash"] == report.receipt_hash_of(receipt)
    assert isinstance(flow.exported["exported_wall_ms"], int)  # printed beside the files, never inside them
    assert b"exported_wall_ms" not in exported_bytes
    assert (receipt["proposal"]["mode"], receipt["proposal"]["hypothesis"]) == ("rules", "merchant_worker_regression")
    assert receipt["versions"] == {
        "application": recovery_workbench.__version__, "simulator_generator": receipt["versions"]["simulator_generator"],
        "policy": policy.POLICY_VERSION, "prompt": None, "model": None, "model_digest": None, "thinking": None,
        "validator": diagnosis.VALIDATOR_VERSION, "rules": selective.RULES_V3_VERSION,
    }
    assert receipt["approved_operation"]["approval"]["operator_id"] == CLI_OPERATOR
    assert receipt["approved_operation"]["target_ids"] == sorted(target["id"] for target in flow.plan["targets"])
    assert check_results(receipt) == dict.fromkeys(ALL_CHECKS, "passed")
    planning = receipt["preconditions"]["planning"]
    assert planning["exclusions_by_reason"] == flow.plan["exclusions_by_reason"]
    assert planning["dry_runs_passed"] == flow.plan["dry_runs_passed"] == flow.plan["target_count"]


def test_the_flow_a_receipt_totals_equal_the_committed_unique_effects(cli_flows: tuple[CliFlow, CliFlow]) -> None:
    flow = cli_flows[0]
    receipt = json.loads(Path(flow.exported["json"]).read_bytes())
    with closing(storage.connect(flow.data_dir / cli.DATABASE_FILE)) as conn:
        committed = [item for item in storage.list_fulfilments(conn, run_id=flow.run_id)
                     if item.action_id == flow.executed["action_id"]]
        orders = {order.order_id: order.amount_paise for order in storage.list_orders(conn, run_id=flow.run_id)}
        fulfilled = {item.order_id for item in storage.list_fulfilments(conn, run_id=flow.run_id)}
    completed = sorted(order["order_id"] for order in receipt["execution"]["orders"] if order["result"] == "completed")
    assert completed == sorted(item.order_id for item in committed) == sorted(flow.executed["fulfilled_order_ids"])
    assert len(set(completed)) == len(completed) == flow.plan["target_count"]
    assert receipt["value"]["restored_processing_value_paise"] == sum(item.amount_paise for item in committed) == (
        flow.executed["restored_processing_value_paise"])
    assert (receipt["value"]["additional_captured_paise"], receipt["postconditions"]["capture_unchanged"]) == (0, True)
    excluded = {order for listed in flow.plan["exclusions_by_reason"].values() for order in listed}
    outstanding = {item["order_id"] for item in receipt["postconditions"]["outstanding"]}
    assert outstanding == excluded - fulfilled
    assert receipt["value"]["unresolved_value_paise"] == sum(orders[order] for order in outstanding if order in orders)
    assert receipt["value"]["unresolved_orders"] == len(outstanding) > 0


def test_the_same_recovery_in_another_database_gives_the_same_receipt_hash(cli_flows: tuple[CliFlow, CliFlow]) -> None:
    first, second = (json.loads(Path(flow.exported["json"]).read_bytes()) for flow in cli_flows)
    assert {section: first[section] for section in SECTIONS} == {section: second[section] for section in SECTIONS}
    assert first["receipt_hash"] == second["receipt_hash"]
    for record in ("plan_id", "approval_id", "action_id", "diagnosis_id", "fix_validation_id"):
        assert first["identity"][record] != second["identity"][record], record  # random IDs, kept out of the hash


# --------------------------------------------------------------------------- the API route


def test_the_receipt_route_serves_an_operator_the_bytes_the_cli_writes(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        log_in(client)
        run_id = create_run(client)
        found = diagnose(client, run_id)["diagnosis"]
        validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate", {"diagnosis_id": found["diagnosis_id"]})
        plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
        action_id = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")["action_id"]
        route = f"/api/runs/{run_id}/actions/{action_id}/receipt"
        served = client.get(route)
        as_markdown = client.get(f"{route}?format=markdown")
        assert client.get(f"{route}?format=html").status_code == 422
        assert get(client, f"/api/runs/{run_id}/actions/act_of_nobody/receipt", expect=403)["error"] == "ScopeError"
        with closing(storage.connect(client.app.state.workbench.database)) as conn:
            expected = report.build_receipt(conn, run_id=run_id, action_id=action_id)
    assert (served.status_code, served.headers["content-type"]) == (200, "application/json")
    assert served.content == report.receipt_bytes(expected)
    assert as_markdown.status_code == 200
    assert as_markdown.headers["content-type"].startswith("text/markdown")
    assert as_markdown.text == report.render_receipt_markdown(expected)
    with make_client(tmp_path) as anonymous:  # the same data directory, no session
        assert get(anonymous, route, expect=403)["error"] == "ScopeError"


# --------------------------------------------------------------------------- Q52: the hash and the database's numbering

# The README demo's receipt hash (Flow A in rules mode) since receipt-2. Under receipt-1 the demo gave befcf35d... only
# in a database whose first run was Flow A's.
DEMO_RECEIPT = "eb9d6f30f04d9ebc6694a7f153cc60d4465459c60ba07b6f698b7a3304c6afa6"
DEMO_RUN = run_id_for(scenarios.demo_scenario("worker_backlog"), 101)
OTHER_RUN_EVENTS = 5  # inbox rows another run's events take before the batch's
EARLIER_RUNS = {  # what a data directory seeds before Flow A, which stays seed 101
    "after_late_authorization_and_method_degradation": (("late_authorization", 101), ("method_degradation", 101)),
    "after_method_degradation_alone": (("method_degradation", 101),),
}


@contextmanager
def batch_after_another_run(database: Path) -> Iterator[tuple[sqlite3.Connection, Batch]]:
    """The batch in a database where another run's events took the first inbox rows: each of the batch's rows has a
    number OTHER_RUN_EVENTS higher. Order 4's two captures, received at one instant, then also list the other way round,
    because observation IDs compare as text: obs_inbox_46 before obs_inbox_9, but obs_inbox_14 before obs_inbox_51."""
    with closing(storage.connect(database)) as conn:
        storage.migrate(conn)
        other = seed_run(conn, RUN_B, MERCHANT_B, INTEGRATION_B, SECRET_B)
        for number in range(901, 901 + OTHER_RUN_EVENTS):
            received = deliver(conn, other, f"evt_other_run_{number}", payment_id=signing.synthetic_id("pay", number),
                               order_id=signing.synthetic_id("order", number))
            assert received.outcome is ReceiveOutcome.ACCEPTED
        yield conn, build_batch(conn)


def inbox_rows(receipt: dict) -> list[int]:
    """Each target's inbox row number, as the receipt's execution section shows it."""
    return [order["inbox_id"] for order in receipt["execution"]["orders"]]


def observation_ids(receipt: dict, order: str) -> list[str]:
    """The observations the receipt lists for one target, in its order."""
    (target,) = [target for target in receipt["evidence"]["targets"] if target["order_id"] == order]
    return [item["observation_id"] for item in target["observations"]]


def test_the_batch_recovery_hashes_alike_after_another_run_took_the_first_inbox_rows(
    conn: sqlite3.Connection, batch: Batch, tmp_path: Path
) -> None:
    """Each receipt shows its own database's inbox row numbers, plan hash and evidence versions; the hash leaves them
    out, and hashes order 4's observations in one order whichever order its database lists them in."""
    _, _, first = executed_receipt(conn, batch)
    with batch_after_another_run(tmp_path / "after.sqlite") as (after_conn, after_batch):
        _, _, after = executed_receipt(after_conn, after_batch)
    # Not vacuous: every row number differs, and with them the digests and the listing of order 4's observations.
    assert inbox_rows(after) == [number + OTHER_RUN_EVENTS for number in inbox_rows(first)]
    assert [receipt["preconditions"]["planning"]["superseded_inbox_ids"] for receipt in (first, after)] == [
        {batch.superseded_order: [46]}, {batch.superseded_order: [51]}]
    assert [observation_ids(receipt, order_id(4)) for receipt in (first, after)] == [
        ["obs_inbox_46", "obs_inbox_9"], ["obs_inbox_14", "obs_inbox_51"]]
    assert first["approved_operation"]["plan_hash"] != after["approved_operation"]["plan_hash"]
    assert first["evidence"]["planned_evidence_version"] != after["evidence"]["planned_evidence_version"]
    assert after["receipt_hash"] == first["receipt_hash"]


def swapped_and_refused(conn: sqlite3.Connection, batch: Batch) -> dict:
    """The receipt of an approved plan whose first two targets' inbox rows were swapped in the stored plan. The plan
    hash binds the target orders, not their rows, so only the recheck refuses it, naming each row by its number."""
    preview = approved_plan(conn, batch)
    stored = conn.execute("SELECT targets FROM plans WHERE plan_id = ?", (preview.plan.plan_id,)).fetchone()[0]
    targets = json.loads(stored)
    targets[0]["inbox_id"], targets[1]["inbox_id"] = targets[1]["inbox_id"], targets[0]["inbox_id"]
    with storage.write_transaction(conn):
        conn.execute("UPDATE plans SET targets = ? WHERE plan_id = ?", (json.dumps(targets), preview.plan.plan_id))
    return receipt_for(conn, rejection(conn, batch, preview.plan.plan_id).action_id)


def test_check_details_that_name_inbox_rows_hash_alike_after_another_run_took_the_first_inbox_rows(
    conn: sqlite3.Connection, batch: Batch, tmp_path: Path
) -> None:
    first = swapped_and_refused(conn, batch)
    with batch_after_another_run(tmp_path / "after.sqlite") as (after_conn, after_batch):
        after = swapped_and_refused(after_conn, after_batch)
    named = [check_details(receipt)["target_ineligible"] for receipt in (first, after)]
    assert named == [[f"inbox row 7 names order {order_id(2)!r}", f"inbox row 6 names order {order_id(1)!r}"],
                     [f"inbox row 12 names order {order_id(2)!r}", f"inbox row 11 names order {order_id(1)!r}"]]
    assert after["receipt_hash"] == first["receipt_hash"]


def check_details(receipt: dict) -> dict[str, list[str]]:
    return {check["code"]: check["details"] for check in receipt["preconditions"]["execution"]["checks"]}


def test_the_hash_still_changes_with_the_approved_targets_an_amount_or_the_outcome(
    conn: sqlite3.Connection, batch: Batch, tmp_path: Path
) -> None:
    """The guard on Q52's fix: leaving the database's numbering out of the hash must not leave out what the recovery
    did. Each recovery here runs after another run's events took the first inbox rows; only the one that does what the
    batch's recovery does in a fresh database hashes as that one does."""
    _, _, fresh = executed_receipt(conn, batch)
    with batch_after_another_run(tmp_path / "same.sqlite") as (same_conn, same_batch):
        _, _, same = executed_receipt(same_conn, same_batch)
    with batch_after_another_run(tmp_path / "fewer.sqlite") as (fewer_conn, fewer_batch):
        with storage.write_transaction(fewer_conn):  # the shop cancels order 7 before the plan is built
            fewer_conn.execute("UPDATE orders SET cancelled_at_ms = ? WHERE run_id = ? AND order_id = ?",
                               (NOW - 1, RUN_A, order_id(7)))
        _, _, fewer = executed_receipt(fewer_conn, fewer_batch)
    with batch_after_another_run(tmp_path / "refused.sqlite") as (refused_conn, refused_batch):
        preview = approved_plan(refused_conn, refused_batch)  # then another handler is deployed before execution
        redeployed = merchant.compose({"payload": 2, "notifications": 2}, version="dep_later_notifications")
        refusal = rejection(refused_conn, refused_batch, preview.plan.plan_id, deployed=redeployed)
        refused = receipt_for(refused_conn, refusal.action_id)
    first_order, *other_orders = fresh["execution"]["orders"]
    repriced = {**fresh, "execution": {**fresh["execution"], "orders": [
        {**first_order, "amount_paise": first_order["amount_paise"] + 100}, *other_orders]}}
    assert same["receipt_hash"] == fresh["receipt_hash"]
    # Another target set and so another approved value; the same targets with one amount changed; the same plan refused.
    assert (fewer["approved_operation"]["target_count"], fewer["approved_operation"]["target_value_paise"]) == (
        40, sum(batch.eligible.values()) - batch.eligible[order_id(7)])
    assert refused["approved_operation"]["target_ids"] == fresh["approved_operation"]["target_ids"]
    assert (refused["execution"]["status"], refused["value"]["restored_processing_value_paise"]) == ("rejected", 0)
    hashes = [fresh["receipt_hash"], fewer["receipt_hash"], report.receipt_hash_of(repriced), refused["receipt_hash"]]
    assert len(set(hashes)) == len(hashes)


@dataclass(frozen=True)
class DemoReceipt:
    """The README's demo of Flow A in one data directory, and the receipt of its recovery action."""

    data_dir: Path
    action_id: str
    printed: dict  # what the receipt command printed
    json_bytes: bytes
    markdown: str

    @property
    def receipt(self) -> dict:
        return json.loads(self.json_bytes)


def demo_receipt(directory: Path, earlier: tuple[tuple[str, int], ...] = ()) -> DemoReceipt:
    """``cli demo`` on worker_backlog with seed 101 in rules mode, then ``cli receipt``, in a data directory that seeded
    the ``earlier`` runs first."""
    data = ("--data-dir", str(directory / "data"))
    for scenario, seed in earlier:
        cli_json("seed", "--scenario", scenario, "--seed", str(seed), *data)
    with redirect_stdout(io.StringIO()):
        assert cli.main(["demo", "--scenario", "worker_backlog", "--seed", "101", "--model-mode", "rules", *data]) == 0
    with closing(storage.connect(directory / "data" / cli.DATABASE_FILE)) as conn:
        (action,) = executor.list_actions(conn, run_id=DEMO_RUN)  # executed once; the demo's second execute replays it
    printed = cli_json("receipt", "--run-id", DEMO_RUN, "--action-id", action.action_id, "--output-dir",
                       str(directory / "receipts"), *data)
    return DemoReceipt(directory / "data", action.action_id, printed, Path(printed["json"]).read_bytes(),
                       Path(printed["markdown"]).read_text(encoding="utf-8"))


def assert_names_its_own_records(flow: DemoReceipt) -> None:
    """The receipt shows its own database's records as stored, so it can be checked against them: the plan hash and
    evidence version, each target's inbox row and each observation."""
    receipt = flow.receipt
    with closing(storage.connect(flow.data_dir / cli.DATABASE_FILE)) as conn:
        plan = plans.require_plan(conn, run_id=DEMO_RUN, plan_id=receipt["identity"]["plan_id"])
        stored = {row[0] for row in conn.execute("SELECT observation_id FROM payment_observations WHERE run_id = ?",
                                                 (DEMO_RUN,))}
    assert (receipt["approved_operation"]["plan_hash"], receipt["evidence"]["planned_evidence_version"]) == (
        plan.plan_hash, plan.evidence_version)
    assert {order["order_id"]: order["inbox_id"] for order in receipt["execution"]["orders"]} == {
        target.id: target.inbox_id for target in plan.reprocess_targets()}
    shown = {item["observation_id"] for target in receipt["evidence"]["targets"] for item in target["observations"]}
    assert shown <= stored


@pytest.fixture(scope="module")
def fresh_demo(tmp_path_factory: pytest.TempPathFactory) -> DemoReceipt:
    """The README's demo in a data directory of its own, so its run is the first that database numbers."""
    return demo_receipt(tmp_path_factory.mktemp("demo_fresh"))


@pytest.mark.parametrize("earlier", list(EARLIER_RUNS.values()), ids=list(EARLIER_RUNS))
def test_flow_a_gives_one_receipt_hash_whatever_its_data_directory_seeded_before_it(
    fresh_demo: DemoReceipt, tmp_path: Path, earlier: tuple[tuple[str, int], ...]
) -> None:
    """Seeded after other runs, Flow A's events have other inbox row numbers, and its plan another plan hash. Each
    receipt shows its own database's, and the hash is the one Flow A gets when its run is the first."""
    after = demo_receipt(tmp_path, earlier)
    first, later = fresh_demo.receipt, after.receipt
    assert inbox_rows(later) != inbox_rows(first)  # not vacuous
    assert later["approved_operation"]["plan_hash"] != first["approved_operation"]["plan_hash"]
    assert_names_its_own_records(fresh_demo)
    assert_names_its_own_records(after)
    assert later["receipt_hash"] == first["receipt_hash"]


def test_two_fresh_data_directories_give_the_demo_receipt_and_each_regenerates_it_byte_for_byte(
    fresh_demo: DemoReceipt, tmp_path: Path
) -> None:
    again = demo_receipt(tmp_path)
    for flow in (fresh_demo, again):
        with closing(storage.connect(flow.data_dir / cli.DATABASE_FILE)) as conn:
            regenerated = report.build_receipt(conn, run_id=DEMO_RUN, action_id=flow.action_id)
        assert report.receipt_bytes(regenerated) == flow.json_bytes
        assert report.render_receipt_markdown(regenerated) == flow.markdown
        assert flow.printed["receipt_hash"] == flow.receipt["receipt_hash"] == report.receipt_hash_of(flow.receipt)
    assert fresh_demo.receipt["receipt_hash"] == again.receipt["receipt_hash"] == DEMO_RECEIPT
    assert fresh_demo.action_id != again.action_id  # record IDs are random per database, and not hashed
