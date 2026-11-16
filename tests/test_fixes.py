"""Corrected-handler validation and the diagnosis store it depends on."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from conftest import MERCHANT_A, seed_run
from test_plans import CORRECT_FIX, INCIDENT, NOW, OPERATOR, WRONG_FIX, Batch, add_incident, build_batch, validate

from recovery_workbench import diagnosis_store, fixes
from recovery_workbench.contracts import (
    ContractError,
    DiagnosisStatus,
    EvidencePacket,
    ModelMode,
    ScopeError,
)
from recovery_workbench.diagnosis import ChatReply, ChatRequest, diagnose
from recovery_workbench.simulator import merchant

M0_PACKET = Path(__file__).resolve().parents[1] / "experiments" / "m0" / "packets" / "m0_05.json"


@pytest.fixture
def batch(conn: sqlite3.Connection) -> Batch:
    return build_batch(conn)


# --------------------------------------------------------------------------- the allowlist


def test_the_same_neutral_fix_list_is_offered_in_every_run() -> None:
    first, second = fixes.available_fixes(), fixes.available_fixes()
    assert first == second
    assert tuple(option.fix_id for option in first) == merchant.CORRECTED_VARIANT_IDS
    leak = re.compile(r"\b(bug|fault|faulty|broken|fix|fixes|fixed|regression|wrong|culprit)\b", re.IGNORECASE)
    for option in first:
        assert option.label.startswith(option.files[0]), option.label
        assert not leak.search(option.label), option.label


# --------------------------------------------------------------------------- validation


def test_a_fix_is_refused_before_any_diagnosis_exists(conn: sqlite3.Connection) -> None:
    run = seed_run(conn, "run_unit_nodiag", MERCHANT_A, "int_unit_nodiag", "fixture_secret_a")
    add_incident(conn, run.run_id)
    deployed = merchant.compose({"payload": 2})
    with pytest.raises(fixes.FixRefusedError, match="only after a diagnosis"):
        fixes.validate_fix(
            conn, run_id=run.run_id, deployed=deployed, fix_id=CORRECT_FIX,
            diagnosis_id="diag_not_yet", operator_id=OPERATOR, now_ms=NOW,
        )
    assert conn.execute("SELECT COUNT(*) FROM fix_validations").fetchone()[0] == 0


def test_the_matching_fix_passes_and_both_results_are_stored(conn: sqlite3.Connection, batch: Batch) -> None:
    validation = validate(conn, batch)
    assert validation.passed
    assert validation.deployed_version == batch.deployed.version
    assert set(validation.deployed_failures) == {
        "captured_payment_builds_fulfilment", "authorized_payment_changes_nothing", "failed_payment_changes_nothing",
    }
    assert validation.corrected_failures == ()
    stored = fixes.require_fix_validation(conn, run_id=batch.run.run_id, validation_id=validation.validation_id)
    assert stored == validation
    row = conn.execute("SELECT * FROM fix_validations WHERE validation_id = ?", (validation.validation_id,)).fetchone()
    assert row["fixture_test_id"] == merchant.FIXTURE_SUITE_ID
    assert row["passed"] == 1
    assert json.loads(row["deployed_result"])["version"] == batch.deployed.version


def test_a_fix_for_another_module_fails_validation(conn: sqlite3.Connection, batch: Batch) -> None:
    validation = validate(conn, batch, WRONG_FIX)
    assert not validation.passed
    assert "captured_payment_builds_fulfilment" in validation.corrected_failures
    assert conn.execute("SELECT passed FROM fix_validations").fetchone()[0] == 0  # recorded, not hidden


def test_only_allowlisted_fixes_and_human_operators_are_accepted(conn: sqlite3.Connection, batch: Batch) -> None:
    with pytest.raises(ContractError, match="not allowlisted"):
        validate(conn, batch, "payload@2")
    with pytest.raises(ScopeError, match="not a human operator"):
        fixes.validate_fix(
            conn, run_id=batch.run.run_id, deployed=batch.deployed, fix_id=CORRECT_FIX, diagnosis_id=batch.diagnosis_id,
            operator_id="qwen3:4b", now_ms=NOW,
        )


def test_a_diagnosis_from_another_run_does_not_unlock_a_fix(conn: sqlite3.Connection, batch: Batch) -> None:
    other = seed_run(conn, "run_unit_other", MERCHANT_A, "int_unit_other", "fixture_secret_a")
    with pytest.raises(fixes.FixRefusedError):
        fixes.validate_fix(
            conn, run_id=other.run_id, deployed=batch.deployed, fix_id=CORRECT_FIX, diagnosis_id=batch.diagnosis_id,
            operator_id=OPERATOR, now_ms=NOW,
        )


# --------------------------------------------------------------------------- the diagnosis store


class ScriptedClient:
    def __init__(self, *answers: dict[str, object]) -> None:
        self._answers = list(answers)

    def chat(self, request: ChatRequest, *, timeout_s: float) -> ChatReply:
        return ChatReply(content=json.dumps(self._answers.pop(0)), done_reason="stop")


def degradation_answer(operation: str) -> dict[str, object]:
    return {
        "rationale": "UPI authorisation fell with bank-side reasons while processing stayed healthy.",
        "supporting_evidence_ids": ["ev_metric_upi", "ev_provider_state"],
        "contradicting_evidence_ids": [],
        "hypothesis": "provider_method_degradation",
        "faulting_change_id": "none",
        "affected_method": "upi",
        "evidence_level": "supported",
        "proposed_operation": operation,
        "next_check": "fetch_provider_snapshot",
        "missing_checks": [],
    }


def test_a_local_diagnosis_round_trips_with_its_unsafe_proposal(conn: sqlite3.Connection, batch: Batch) -> None:
    packet = EvidencePacket.model_validate_json(M0_PACKET.read_text(encoding="utf-8"))
    run = diagnose(packet, ScriptedClient(degradation_answer("reprocess_inbox"), degradation_answer("escalate")), think=False)
    assert run.status is DiagnosisStatus.VALIDATED
    assert len(run.unsafe_proposals) == 1
    stored = diagnosis_store.record_model_diagnosis(
        conn, run_id=batch.run.run_id, incident_id=INCIDENT, run=run, model_id="qwen3:4b", model_digest="d" * 64,
    )
    loaded = diagnosis_store.get_diagnosis(conn, run_id=batch.run.run_id, diagnosis_id=stored.diagnosis_id)
    assert loaded == stored
    assert loaded.mode is ModelMode.LOCAL
    assert loaded.recorded_from is None
    assert loaded.thinking is False
    assert loaded.unsafe_proposals[0].proposed_operation.value == "reprocess_inbox"
    assert loaded.validator_version == run.validator_version
    assert loaded.policies_enforced


def test_recorded_mode_is_always_labelled_and_names_its_source(conn: sqlite3.Connection, batch: Batch) -> None:
    packet = EvidencePacket.model_validate_json(M0_PACKET.read_text(encoding="utf-8"))
    run = diagnose(packet, ScriptedClient(degradation_answer("escalate")), think=True)
    with pytest.raises(ValueError, match="recorded_from"):
        diagnosis_store.record_model_diagnosis(
            conn, run_id=batch.run.run_id, incident_id=INCIDENT, run=run, model_id="qwen3:4b", model_digest="d" * 64,
            mode=ModelMode.RECORDED,
        )
    stored = diagnosis_store.record_model_diagnosis(
        conn, run_id=batch.run.run_id, incident_id=INCIDENT, run=run, model_id="qwen3:4b", model_digest="d" * 64,
        mode=ModelMode.RECORDED, recorded_from="traces/run1_think_on.jsonl#m0_05", recorded_original_wall_ms=1,
    )
    row = conn.execute("SELECT mode FROM diagnoses WHERE diagnosis_id = ?", (stored.diagnosis_id,)).fetchone()
    assert row["mode"] == "recorded"
    assert diagnosis_store.get_diagnosis(conn, run_id=batch.run.run_id, diagnosis_id=stored.diagnosis_id).recorded_from


def test_a_needs_review_diagnosis_keeps_its_reasons_and_no_output(conn: sqlite3.Connection, batch: Batch) -> None:
    packet = EvidencePacket.model_validate_json(M0_PACKET.read_text(encoding="utf-8"))
    invalid = {**degradation_answer("escalate"), "faulting_change_id": "dep_invented"}
    run = diagnose(packet, ScriptedClient(invalid, invalid), think=False)
    stored = diagnosis_store.record_model_diagnosis(
        conn, run_id=batch.run.run_id, incident_id=INCIDENT, run=run, model_id="qwen3:4b", model_digest="d" * 64,
    )
    loaded = diagnosis_store.get_diagnosis(conn, run_id=batch.run.run_id, diagnosis_id=stored.diagnosis_id)
    assert loaded.status is DiagnosisStatus.NEEDS_REVIEW
    assert loaded.output is None
    assert loaded.reasons
    assert conn.execute("SELECT error FROM diagnoses WHERE diagnosis_id = ?", (stored.diagnosis_id,)).fetchone()[0]


def test_rules_diagnoses_carry_no_model_fields(conn: sqlite3.Connection, batch: Batch) -> None:
    stored = diagnosis_store.get_diagnosis(conn, run_id=batch.run.run_id, diagnosis_id=batch.diagnosis_id)
    assert stored.mode is ModelMode.RULES
    assert stored.rules_version == "rules-unit"
    assert (stored.model_id, stored.model_digest, stored.thinking, stored.prompt_version) == (None, None, None, None)
    assert [item.diagnosis_id for item in diagnosis_store.list_diagnoses(conn, run_id=batch.run.run_id)] == [batch.diagnosis_id]


def test_a_diagnosis_needs_an_incident_of_its_own_run(conn: sqlite3.Connection, batch: Batch) -> None:
    from test_plans import worker_regression_output

    output = worker_regression_output()
    with pytest.raises(ScopeError, match="incident"):
        diagnosis_store.record_rules_diagnosis(
            conn, run_id=batch.run.run_id, incident_id="inc_elsewhere", packet_hash="0" * 64,
            output=output, rules_version="rules-unit",
        )
