"""Export one run as a self-contained JSON report, and one recovery action as a receipt (read-only).

The report carries the evidence of every packet assembled for the run (so each packet a stored
diagnosis saw), every diagnosis with its mode label (live, recorded, hosted or rules), fix validations, plans,
approvals, actions and the before/after outcome. A hosted diagnosis's entry, and a receipt whose proposal
came from one, also carry the provider host; no other entry or receipt gains a field, so theirs stay byte
for byte as before. It also
carries the metadata a reader needs to judge it: seed, generator version, model and digest,
thinking, prompt and validator versions, and both clocks (the replay clock the business events use,
and the wall clock of the export). Money is reported as separate figures, never one "recovered"
number: restored processing value is already-captured money, and additional capture is new money.

The receipt (``build_receipt``) covers one reprocess_inbox action, executed or rejected. It is built from
stored records only, the plan and action audits included, and reads no clock, so the same database gives
the same bytes every time. Its hash covers every section except the identity (random record IDs) and the
stored wall-clock times, in a form without the database's own inbox row numbers (``hashed_form``), so the
same recovery of the same world hashes alike in any database, whatever runs it held before; the export time
is not part of a receipt at all.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import diagnosis, diagnosis_store, executor, fixes, packet, plans, policy, storage
from recovery_workbench.contracts import (
    ActionStatus,
    ContractError,
    DiagnosisOutput,
    EvidenceItem,
    ModelMode,
    ObservationSource,
    Operation,
    ScopeError,
    canonical_json,
    sha256_hex,
)

REPORT_VERSION = "report-1"
# receipt-2 (Q52) hashes its sections in their hashed form. receipt-1 hashed them as stored, the database's inbox row
# numbers included, so a recovery seeded after other runs in its database got another hash. The version is hashed too,
# so the two kinds of hash never match.
RECEIPT_VERSION = "receipt-2"
# The receipt hash is sha256 over contracts.canonical_json of exactly these sections, in their hashed form. The identity
# holds random record IDs (plan, approval, action, diagnosis, fix validation) and the wall_clock section stored
# wall-clock times; neither is hashed.
HASHED_SECTIONS: tuple[str, ...] = (
    "receipt_version", "run_id", "versions", "evidence", "proposal", "approved_operation", "preconditions",
    "execution", "postconditions", "value", "boundary",
)
# SQLite numbers inbox rows across every run a database holds, so the same event of the same run has another number in
# a database where other runs were seeded first. These digests are computed from those numbers (each target's evidence
# fingerprint names its inbox row, and the plan hash binds the evidence version), so the hashed form leaves them out.
# The receipt still shows them, and whether they match stays hashed: evidence_unchanged, matches_plan_hash.
DATABASE_LOCAL_DIGESTS = frozenset({"plan_hash", "planned_evidence_version", "evidence_version_at_execution"})
HASH_RULE = (
    "Database-local numbers are taken out first. Each inbox row number in the hashed sections (an `inbox_id`, each "
    "number in `superseded_inbox_ids`, the N of an `obs_inbox_N` observation ID or of `inbox row N` in a check's "
    "details) becomes its rank among them in ascending order, from 1; each target's observations are then sorted by "
    "their canonical JSON; and `plan_hash`, `planned_evidence_version` and `evidence_version_at_execution`, computed "
    "from those numbers, are left out."
)
_INBOX_OBSERVATION_ID = re.compile(r"obs_inbox_(\d+)")  # ingestion names each webhook observation after its inbox row
_INBOX_ROW_IN_DETAIL = re.compile(r"\binbox row (\d+)\b")  # how a failed target check names a row (policy)
VALUE_DEFINITIONS: dict[str, str] = {
    "restored_processing_value_paise": (
        "Order value of the orders this action fulfilled whose capture was known before it ran: money already "
        "collected, now processed. Not new revenue."
    ),
    "unresolved_value_paise": (
        "Order value of this plan's candidate orders still unfulfilled after this action, whether left out at "
        "planning or not fulfilled at execution; an order with no order record is counted, not summed."
    ),
    "additional_captured_paise": (
        "New provider captures this action caused. Reprocessing fulfils captures already received, so it is 0."
    ),
}
BOUNDARY_STATEMENT = (
    "Synthetic sandbox: no real money, customers or provider accounts. The workbench called no provider; "
    "reprocessing fulfils orders from captures it had already received and captures nothing."
)
ORDER_RESULTS: tuple[str, ...] = ("completed", "no_op", "refused", "failed")
_PROPOSAL_FIELDS: tuple[str, ...] = (
    "hypothesis", "faulting_change_id", "affected_method", "evidence_level", "proposed_operation", "next_check",
    "supporting_evidence_ids", "contradicting_evidence_ids",
)


def build_report(conn: sqlite3.Connection, *, run_id: str) -> dict[str, JsonValue]:
    """The run's report-1 document. Every field it exports is listed below, so a field added later stays out of it
    until someone lists it. Every read shares one transaction, and so one snapshot: a write that lands while the
    export runs is all in the report or not in it at all. The helpers' own read transactions reuse this one.
    """
    with storage.read_transaction(conn):
        run = storage.require_run(conn, run_id)
        incidents = storage.list_incidents(conn, run_id=run_id)
        plan_rows = conn.execute(
            "SELECT plan_id FROM plans WHERE run_id = ? ORDER BY created_at_ms, plan_id", (run_id,)
        ).fetchall()
        validation_rows = conn.execute(
            "SELECT validation_id FROM fix_validations WHERE run_id = ? ORDER BY validated_at_ms, validation_id", (run_id,)
        ).fetchall()
        approval_rows = conn.execute(
            "SELECT run_id, approval_id, plan_id, plan_hash, operator_id, approved_at_ms, expires_at_ms FROM approvals"
            " WHERE run_id = ? ORDER BY approved_at_ms, approval_id", (run_id,)
        ).fetchall()
        diagnoses = diagnosis_store.list_diagnoses(conn, run_id=run_id)
        stored_plans = [plans.require_plan(conn, run_id=run_id, plan_id=row["plan_id"]) for row in plan_rows]
        validations = [
            fixes.require_fix_validation(conn, run_id=run_id, validation_id=row["validation_id"])
            for row in validation_rows
        ]
        actions = executor.list_actions(conn, run_id=run_id)
        evidence = _evidence_by_packet(conn, run_id)
    return {
        "report_version": REPORT_VERSION,
        "notice": "Synthetic sandbox: no real money, customers or provider accounts.",
        "metadata": {
            "run_id": run.run_id,
            "scenario": run.scenario,
            "seed": run.seed,
            "split": run.split,
            "environment": run.environment,
            "generator_version": run.generator_version,
            "run_model_mode": run.model_mode.value,
            "policy_version": policy.POLICY_VERSION,
            "current_prompt_version": diagnosis.PROMPT_VERSION,
            "current_validator_version": diagnosis.VALIDATOR_VERSION,
            "packet_version": packet.PACKET_VERSION,
        },
        "clocks": {"replay_clock_ms": run.clock_ms, "exported_wall_ms": storage.wall_clock_ms()},
        "incidents": [_incident_entry(incident) for incident in incidents],
        "evidence": evidence,
        "diagnoses": [_diagnosis_entry(stored) for stored in diagnoses],
        "fix_validations": [_validation_entry(validation) for validation in validations],
        "plans": [_plan_entry(stored) for stored in stored_plans],
        "approvals": [_approval_entry(row) for row in approval_rows],
        "actions": [_action_entry(action) for action in actions],
        "outcome": _outcome(stored_plans, actions),
    }


def _evidence_by_packet(conn: sqlite3.Connection, run_id: str) -> list[JsonValue]:
    """The persisted evidence of every packet assembled for the run, keyed by incident and cutoff.

    ``diagnose_incident`` stores a packet before it diagnoses, so this includes every packet a
    stored diagnosis saw, and also a packet whose diagnosis was refused afterwards, for example a
    recorded replay with no recording. The packet is kept because its evidence was assembled.
    Diagnoses do not record their cutoff, so packets are not matched to them here.
    """
    with storage.read_transaction(conn):
        references = sorted({
            record.source_record_id
            for record in storage.list_evidence(conn, run_id=run_id)
            if record.source_table == packet.EVIDENCE_SOURCE_TABLE
        })
    entries: list[JsonValue] = []
    for reference in references:
        items = packet.stored_packet_items(conn, run_id=run_id, reference=reference)
        entries.append({"packet": reference, "items": [_evidence_item_entry(item) for item in items]})
    return entries


# The entries below list every field the report exports, in the order the API sends them. A value that is a stored
# JSON document (incident cohort and impact, evidence facts, plan targets, an action's before and after) is exported
# as stored: its keys are what the workbench recorded for that kind of record.


def _incident_entry(incident: storage.IncidentRecord) -> dict[str, JsonValue]:
    return {
        "run_id": incident.run_id,
        "incident_id": incident.incident_id,
        "trigger": incident.trigger,
        "cohort": incident.cohort,
        "window_start_ms": incident.window_start_ms,
        "window_end_ms": incident.window_end_ms,
        "evidence_cutoff_ms": incident.evidence_cutoff_ms,
        "state": incident.state,
        "measured_impact": incident.measured_impact,
    }


def _evidence_item_entry(item: EvidenceItem) -> dict[str, JsonValue]:
    return {
        "evidence_id": item.evidence_id,
        "kind": item.kind.value,
        "known_at_ms": item.known_at_ms,
        "title": item.title,
        "facts": item.facts,
        "text": item.text,
    }


def _diagnosis_entry(stored: diagnosis_store.StoredDiagnosis) -> dict[str, JsonValue]:
    entry: dict[str, JsonValue] = {
        "run_id": stored.run_id,
        "diagnosis_id": stored.diagnosis_id,
        "incident_id": stored.incident_id,
        "mode": stored.mode.value,
        "model_id": stored.model_id,
        "model_digest": stored.model_digest,
        "thinking": stored.thinking,
        "prompt_version": stored.prompt_version,
        "validator_version": stored.validator_version,
        "policies_enforced": stored.policies_enforced,
        "rules_version": stored.rules_version,
        "recorded_from": stored.recorded_from,
        "recorded_original_wall_ms": stored.recorded_original_wall_ms,
        "packet_hash": stored.packet_hash,
        "status": stored.status.value,
        "output": None if stored.output is None else _diagnosis_output_entry(stored.output),
        "reasons": list(stored.reasons),
        "unsafe_proposals": [_blocked_proposal_entry(proposal) for proposal in stored.unsafe_proposals],
        "elapsed_ms": stored.elapsed_ms,
        "created_wall_ms": stored.created_wall_ms,
        "mode_label": stored.mode_label,
    }
    if stored.mode is ModelMode.HOSTED:
        entry["provider_host"] = stored.provider_host
    return entry


def _diagnosis_output_entry(output: DiagnosisOutput) -> dict[str, JsonValue]:
    return {
        "hypothesis": output.hypothesis.value,
        "faulting_change_id": output.faulting_change_id,
        "affected_method": output.affected_method,
        "evidence_level": output.evidence_level.value,
        "supporting_evidence_ids": list(output.supporting_evidence_ids),
        "contradicting_evidence_ids": list(output.contradicting_evidence_ids),
        "missing_checks": [check.value for check in output.missing_checks],
        "next_check": output.next_check.value,
        "proposed_operation": output.proposed_operation.value,
        "rationale": output.rationale,
    }


def _blocked_proposal_entry(proposal: diagnosis.BlockedProposal) -> dict[str, JsonValue]:
    return {
        "call_index": proposal.call_index,
        "rule": proposal.rule,
        "hypothesis": proposal.hypothesis.value,
        "proposed_operation": proposal.proposed_operation.value,
        "faulting_change_id": proposal.faulting_change_id,
        "allowed_operations": [operation.value for operation in proposal.allowed_operations],
    }


def _plan_entry(plan: plans.StoredPlan) -> dict[str, JsonValue]:
    return {
        "run_id": plan.run_id,
        "plan_id": plan.plan_id,
        "incident_id": plan.incident_id,
        "diagnosis_id": plan.diagnosis_id,
        "fix_validation_id": plan.fix_validation_id,
        "operation": plan.operation.value,
        "targets": list(plan.targets),
        "target_value_paise": plan.target_value_paise,
        "evidence_version": plan.evidence_version,
        "policy_version": plan.policy_version,
        "plan_hash": plan.plan_hash,
        "expires_at_ms": plan.expires_at_ms,
        "status": plan.status.value,
        "created_at_ms": plan.created_at_ms,
    }


def _approval_entry(row: sqlite3.Row) -> dict[str, JsonValue]:
    return {
        "run_id": row["run_id"],
        "approval_id": row["approval_id"],
        "plan_id": row["plan_id"],
        "plan_hash": row["plan_hash"],
        "operator_id": row["operator_id"],
        "approved_at_ms": row["approved_at_ms"],
        "expires_at_ms": row["expires_at_ms"],
    }


def _action_entry(action: executor.StoredAction) -> dict[str, JsonValue]:
    return {
        "run_id": action.run_id,
        "action_id": action.action_id,
        "operation_key": action.operation_key,
        "plan_id": action.plan_id,
        "status": action.status.value,
        "before": action.before,
        "after": action.after,
        "error": action.error,
        "executed_at_ms": action.executed_at_ms,
    }


def _validation_entry(validation: fixes.FixValidation) -> dict[str, JsonValue]:
    return {
        "validation_id": validation.validation_id,
        "diagnosis_id": validation.diagnosis_id,
        "fix_id": validation.fix_id,
        "deployed_version": validation.deployed_version,
        "corrected_version": validation.corrected_version,
        "reproduced": validation.reproduced,
        "passed": validation.passed,
        "deployed_failures": list(validation.deployed_failures),
        "corrected_failures": list(validation.corrected_failures),
        "validated_at_ms": validation.validated_at_ms,
        "operator_id": validation.operator_id,
    }


def _outcome(stored_plans: list[plans.StoredPlan], actions: tuple[executor.StoredAction, ...]) -> dict[str, JsonValue]:
    reprocess_plans = {item.plan_id for item in stored_plans if item.operation is Operation.REPROCESS_INBOX}
    succeeded = [
        action for action in actions
        if action.plan_id in reprocess_plans and action.status is ActionStatus.SUCCEEDED and isinstance(action.after, dict)
    ]
    return {
        "reprocess_actions_succeeded": len(succeeded),
        "reprocess_actions_rejected": sum(
            action.plan_id in reprocess_plans and action.status is ActionStatus.REJECTED for action in actions
        ),
        "orders_fulfilled_by_reprocessing": sum(len(action.after.get("fulfilled_order_ids", [])) for action in succeeded),
        "restored_processing_value_paise": sum(int(action.after.get("restored_processing_value_paise", 0)) for action in succeeded),
        "additional_captured_paise": sum(int(action.after.get("additional_captured_paise", 0)) for action in succeeded),
        "escalations": [
            action.after for action in actions
            if any(item.plan_id == action.plan_id and item.operation is Operation.ESCALATE for item in stored_plans)
        ],
        "note": "Restored processing value is money already captured before the intervention; it is not new revenue.",
    }


def write_report(conn: sqlite3.Connection, *, run_id: str, path: Path) -> Path:
    report = build_report(conn, run_id=run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- the recovery receipt


def build_receipt(conn: sqlite3.Connection, *, run_id: str, action_id: str) -> dict[str, JsonValue]:
    """The receipt of one reprocess_inbox action, executed or rejected, from stored records only.

    Raises ``ScopeError`` for an action outside the run and ``ContractError`` for an action of another
    operation: an escalation changes no business state, so it has nothing to give a receipt for.
    """
    with storage.read_transaction(conn):
        run = storage.require_run(conn, run_id)
        action = executor.get_action(conn, run_id=run_id, action_id=action_id)
        if action is None:
            raise ScopeError(f"action {action_id!r} is not part of run {run_id!r}")
        plan = plans.require_plan(conn, run_id=run_id, plan_id=action.plan_id)
        if plan.operation is not Operation.REPROCESS_INBOX:
            raise ContractError(f"action {action_id!r} records a {plan.operation.value} plan; receipts cover "
                                "reprocess_inbox actions, the ones that change business state")
        approval = executor.get_approval(conn, run_id=run_id, plan_id=plan.plan_id)
        validation = (None if plan.fix_validation_id is None
                      else fixes.get_fix_validation(conn, run_id=run_id, validation_id=plan.fix_validation_id))
        stored = (None if plan.diagnosis_id is None
                  else diagnosis_store.get_diagnosis(conn, run_id=run_id, diagnosis_id=plan.diagnosis_id))
        plan_audit = plans.get_plan_audit(conn, run_id=run_id, plan_id=plan.plan_id)
        audit = executor.get_action_audit(conn, run_id=run_id, action_id=action_id)
    hashed: dict[str, JsonValue] = {
        "receipt_version": RECEIPT_VERSION,
        "run_id": run.run_id,
        "versions": _versions(run, plan, stored, audit),
        "evidence": _receipt_evidence(plan, stored, audit),
        "proposal": _proposal(stored),
        "approved_operation": _approved_operation(plan, approval, validation),
        "preconditions": {"planning": _planning(plan_audit), "execution": _checks(audit)},
        "execution": _execution(action, audit),
        "postconditions": _postconditions(audit),
        "value": _value(action, audit),
        "boundary": _boundary(run, audit),
    }
    return {
        **hashed,
        "receipt_hash": receipt_hash_of(hashed),
        "hashed_sections": list(HASHED_SECTIONS),
        "hash_rule": HASH_RULE,
        "identity": {
            "run_id": run.run_id, "incident_id": plan.incident_id, "diagnosis_id": plan.diagnosis_id,
            "fix_validation_id": plan.fix_validation_id, "plan_id": plan.plan_id,
            "approval_id": None if approval is None else approval.approval_id, "action_id": action.action_id,
            "operation_key": action.operation_key, "recorded_from": None if stored is None else stored.recorded_from,
        },
        "wall_clock": {
            "diagnosis_created_wall_ms": None if stored is None else stored.created_wall_ms,
            "recorded_original_wall_ms": None if stored is None else stored.recorded_original_wall_ms,
        },
    }


def receipt_hash_of(receipt: dict[str, JsonValue]) -> str:
    """Recompute a receipt's hash from its hashed sections (HASH_RULE), as a reader of an exported receipt would."""
    if receipt["receipt_version"] != RECEIPT_VERSION:  # the two kinds of hash are never compared
        raise ContractError(
            f"receipt_hash_of recomputes {RECEIPT_VERSION} hashes, and this receipt is {receipt['receipt_version']!r}; "
            "regenerate it from the database with the receipt command or route to get a receipt of this version"
        )
    return sha256_hex(canonical_json(hashed_form(receipt)))


def hashed_form(receipt: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The hashed sections as the receipt hash covers them, from the receipt alone (HASH_RULE).

    A run's events keep their order in any database, so each inbox row number's rank among those the sections show is
    the same wherever the run was seeded, while the number is not. The receipt lists a target's observations by known
    time, then observation ID, which compares inbox row numbers as text, so a tie can come out in another order; the
    hashed form sorts them by content instead.
    """
    shown: set[int] = set()

    def note(number: int) -> int:
        shown.add(number)
        return number

    for section in HASHED_SECTIONS:  # this first pass only notes the inbox row numbers the sections show
        _in_hashed_form(receipt[section], note, section)
    ranks = {number: rank for rank, number in enumerate(sorted(shown), start=1)}
    return {section: _in_hashed_form(receipt[section], ranks.__getitem__, section) for section in HASHED_SECTIONS}


def _in_hashed_form(value: JsonValue, renumber: Callable[[int], int], key: str) -> JsonValue:
    """``value`` in its hashed form, with ``renumber`` applied to each inbox row number in it. ``key`` is the name that
    holds ``value``, which is how an inbox row number is told from other numbers."""
    if isinstance(value, dict):
        if key == "superseded_inbox_ids":  # order ID -> the inbox rows of its other unprocessed captures
            return {order_id: _in_hashed_form(rows, renumber, "inbox_id") for order_id, rows in value.items()}
        return {name: _in_hashed_form(item, renumber, name) for name, item in value.items()
                if name not in DATABASE_LOCAL_DIGESTS}
    if isinstance(value, list):
        items = [_in_hashed_form(item, renumber, key) for item in value]
        return sorted(items, key=canonical_json) if key == "observations" else items
    if key == "inbox_id" and isinstance(value, int) and not isinstance(value, bool):
        return renumber(value)
    if key == "observation_id" and isinstance(value, str):
        row = _INBOX_OBSERVATION_ID.fullmatch(value)
        return value if row is None else f"obs_inbox_{renumber(int(row[1]))}"
    if key == "details" and isinstance(value, str):
        return _INBOX_ROW_IN_DETAIL.sub(lambda row: f"inbox row {renumber(int(row[1]))}", value)
    return value


def receipt_bytes(receipt: dict[str, JsonValue]) -> bytes:
    """The receipt's JSON export: sorted keys and ASCII only, so equal receipts are equal bytes."""
    return (json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def write_receipt(conn: sqlite3.Connection, *, run_id: str, action_id: str, directory: Path) -> tuple[Path, Path, str]:
    """Write ``receipt_<action>.json`` and ``.md``; returns both paths and the receipt hash."""
    receipt = build_receipt(conn, run_id=run_id, action_id=action_id)
    stem = f"receipt_{_section(receipt, 'identity')['action_id']}"  # the stored ID, never the caller's text
    directory.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = directory / f"{stem}.json", directory / f"{stem}.md"
    json_path.write_bytes(receipt_bytes(receipt))
    markdown_path.write_text(render_receipt_markdown(receipt), encoding="utf-8")
    return json_path, markdown_path, str(receipt["receipt_hash"])


def _versions(
    run: storage.RunRecord, plan: plans.StoredPlan, stored: diagnosis_store.StoredDiagnosis | None,
    audit: executor.ActionAudit | None,
) -> dict[str, JsonValue]:
    return {
        "application": None if audit is None else audit.application_version,
        "simulator_generator": run.generator_version,
        "policy": plan.policy_version,
        "prompt": None if stored is None else stored.prompt_version,
        "model": None if stored is None else stored.model_id,
        "model_digest": None if stored is None else stored.model_digest,
        "thinking": None if stored is None else stored.thinking,
        "validator": None if stored is None else stored.validator_version,
        "rules": None if stored is None else stored.rules_version,
    }


def _receipt_evidence(
    plan: plans.StoredPlan, stored: diagnosis_store.StoredDiagnosis | None, audit: executor.ActionAudit | None
) -> dict[str, JsonValue]:
    evidence: dict[str, JsonValue] = {
        "packet_hash": None if stored is None else stored.packet_hash,
        "planned_evidence_version": plan.evidence_version,
        "recorded": audit is not None,
    }
    if audit is None:
        return evidence
    targets = audit.evidence.targets
    return {
        **evidence,
        "evidence_version_at_execution": audit.evidence.version_at_execution,
        "evidence_unchanged": audit.evidence.version_at_execution == plan.evidence_version,
        "stale_after_ms": audit.stale_after_ms,
        "snapshot_ids": sorted({item.source_ref for target in targets for item in target.observations
                                if item.source is ObservationSource.SNAPSHOT}),
        "targets": [target.model_dump(mode="json") for target in targets],
    }


def _proposal(stored: diagnosis_store.StoredDiagnosis | None) -> dict[str, JsonValue]:
    """The diagnosis's cause and citations; a fix can be validated against a needs_review diagnosis, so all may be None.

    The rationale is left out: it is the model's free text, and the cited IDs carry what the receipt needs.
    """
    if stored is None:
        return {"recorded": False}
    output = {} if stored.output is None else stored.output.model_dump(mode="json")
    proposal: dict[str, JsonValue] = {
        "recorded": True,
        "mode": stored.mode.value,
        "status": stored.status.value,
        **{name: output.get(name) for name in _PROPOSAL_FIELDS},
        "unsafe_proposals": len(stored.unsafe_proposals),
        # With a validated output, the refusals its repair answered; with needs_review, why nothing validated.
        "validator_reasons": len(stored.reasons),
    }
    if stored.mode is ModelMode.HOSTED:  # hashed with the rest: the receipt binds where its proposal came from
        proposal["provider_host"] = stored.provider_host
        proposal["mode_label"] = stored.mode_label
    return proposal


def _approved_operation(
    plan: plans.StoredPlan, approval: executor.StoredApproval | None, validation: fixes.FixValidation | None
) -> dict[str, JsonValue]:
    return {
        "operation": plan.operation.value,
        "incident_id": plan.incident_id,
        "target_ids": sorted(plan.target_ids),
        "target_count": len(plan.targets),
        "target_value_paise": plan.target_value_paise,
        "plan_hash": plan.plan_hash,
        "policy_version": plan.policy_version,
        "planned_at_ms": plan.created_at_ms,
        "plan_expires_at_ms": plan.expires_at_ms,
        "approval": None if approval is None else {
            "operator_id": approval.operator_id, "approved_at_ms": approval.approved_at_ms,
            "expires_at_ms": approval.expires_at_ms, "plan_hash": approval.plan_hash,
            "matches_plan_hash": approval.plan_hash == plan.plan_hash,
        },
        "fix": None if validation is None else {
            "fix_id": validation.fix_id, "deployed_version": validation.deployed_version,
            "corrected_version": validation.corrected_version, "reproduced": validation.reproduced,
            "passed": validation.passed, "deployed_failures": list(validation.deployed_failures),
            "corrected_failures": list(validation.corrected_failures), "validated_at_ms": validation.validated_at_ms,
            "operator_id": validation.operator_id,
        },
    }


def _planning(plan_audit: plans.PlanAudit | None) -> dict[str, JsonValue]:
    if plan_audit is None:
        return {"recorded": False}
    excluded = [candidate for candidate in plan_audit.candidates if not candidate.targeted]
    by_reason: dict[str, list[str]] = {}
    for candidate in excluded:
        for reason in candidate.reasons:
            by_reason.setdefault(reason, []).append(candidate.order_id)
    return {
        "recorded": True,
        "deployed_version": plan_audit.deployed_version,
        "corrected_version": plan_audit.corrected_version,
        "stale_after_ms": plan_audit.stale_after_ms,
        "candidate_orders": len(plan_audit.candidates),
        "targets": len(plan_audit.candidates) - len(excluded),
        "exclusions": [
            {"order_id": item.order_id, "inbox_id": item.inbox_id, "payment_id": item.payment_id,
             "amount_paise": item.amount_paise, "currency": item.currency, "reasons": list(item.reasons)}
            for item in excluded
        ],
        "exclusions_by_reason": {reason: sorted(orders) for reason, orders in sorted(by_reason.items())},
        "dry_runs": [dry_run.model_dump(mode="json") for dry_run in plan_audit.dry_runs],
        "dry_runs_passed": sum(dry_run.passed for dry_run in plan_audit.dry_runs),
        "superseded_inbox_ids": {order: list(ids) for order, ids in sorted(plan_audit.superseded_inbox_ids.items())},
    }


def _checks(audit: executor.ActionAudit | None) -> dict[str, JsonValue]:
    if audit is None:
        return {"recorded": False}
    results = Counter(check.result for check in audit.checks)
    return {
        "recorded": True,
        "checks": [check.model_dump(mode="json") for check in audit.checks],
        "passed": results["passed"], "failed": results["failed"], "not_run": results["not_run"],
    }


def _execution(action: executor.StoredAction, audit: executor.ActionAudit | None) -> dict[str, JsonValue]:
    execution: dict[str, JsonValue] = {
        "status": action.status.value, "executed_at_ms": action.executed_at_ms, "recorded": audit is not None,
    }
    if audit is None:  # what the action row itself keeps: the orders a succeeded action fulfilled
        after = action.after if isinstance(action.after, dict) else {}
        return {**execution, "fulfilled_order_ids": sorted(str(item) for item in after.get("fulfilled_order_ids", []))}
    results = Counter(order.result for order in audit.orders)
    return {
        **execution,
        "orders": [order.model_dump(mode="json") for order in audit.orders],
        "results": {result: results[result] for result in ORDER_RESULTS},
    }


def _postconditions(audit: executor.ActionAudit | None) -> dict[str, JsonValue]:
    if audit is None:
        return {"recorded": False}
    measured = audit.postconditions
    return {
        "recorded": True,
        **measured.model_dump(mode="json"),
        "capture_unchanged": _capture_unchanged(audit),
    }


def _capture_unchanged(audit: executor.ActionAudit) -> bool:
    measured = audit.postconditions
    return (measured.captured_payments_before == measured.captured_payments_after
            and measured.captured_paise_before == measured.captured_paise_after
            and not measured.new_captured_payment_ids
            and all(item.order_captures_before == item.order_captures_after
                    and item.payment_captured_paise_before == item.payment_captured_paise_after
                    for item in measured.targets))


def _value(action: executor.StoredAction, audit: executor.ActionAudit | None) -> dict[str, JsonValue]:
    definitions = {f"{name.removesuffix('_paise')}_definition": text for name, text in VALUE_DEFINITIONS.items()}
    if audit is None:  # restored and additional capture are on a succeeded action's row; nothing else was kept
        after = action.after if isinstance(action.after, dict) else {}
        return {
            "recorded": False, **definitions,
            "restored_processing_value_paise": int(after.get("restored_processing_value_paise", 0)),
            "additional_captured_paise": int(after.get("additional_captured_paise", 0)),
            "unresolved_value_paise": None,
        }
    return {"recorded": True, **definitions, **audit.amounts.model_dump(mode="json")}


def _boundary(run: storage.RunRecord, audit: executor.ActionAudit | None) -> dict[str, JsonValue]:
    return {
        "environment": run.environment,
        "provider_calls_made": 0,  # the executor holds no provider adapter; it writes fulfilments only
        "original_capture_unchanged": None if audit is None else _capture_unchanged(audit),
        "statement": BOUNDARY_STATEMENT,
    }


# --------------------------------------------------------------------------- the receipt as Markdown

# Every character that means something in Markdown or HTML. Stored text (handler details, check details) is
# synthetic, but it is still text another system wrote, so it renders inert.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")
# Only a character outside printable ASCII can be a control or format character (Unicode category Cc or Cf).
_OUTSIDE_PRINTABLE_ASCII = re.compile(r"[^ -~]")
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf"})
NOT_RECORDED = "not recorded"
_THREE_COLUMNS = "|---|---|---|"


def _text(value: object) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", visible(" ".join(str(value).split())))


def _code(value: object) -> str:
    if value is None:  # a needs_review diagnosis, for one, has no cause to show
        return "none"
    # A backtick would close the span; a pipe would end a table cell (GFM honours an escaped pipe even inside a span).
    return "`" + visible(" ".join(str(value).split())).replace("`", "'").replace("|", "\\|") + "`"


def visible(text: str) -> str:
    """``text`` with every Unicode Cc or Cf character, line breaks and tabs included, shown as the escape of its code
    point; every other character is kept as it is."""
    # A terminal would act on ESC, BEL, BS, DEL and C1 controls, complete CSI and OSC sequences included; bidi and
    # zero-width characters would reorder or hide text. The escape is \u and four hex digits, or \U and eight beyond
    # U+FFFF. The receipt is read in a terminal too (the CLI writes it as a .md file) and collapses whitespace first;
    # the CLI demo applies this to the model endpoint's text as stored (Q43).
    return _OUTSIDE_PRINTABLE_ASCII.sub(_escaped_control, text)


def _escaped_control(found: re.Match[str]) -> str:
    character = found.group()
    if unicodedata.category(character) not in _CONTROL_CATEGORIES:
        return character
    code = ord(character)
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _ms(duration_ms: object) -> str:
    if not isinstance(duration_ms, int):
        return NOT_RECORDED
    minutes = f" ({duration_ms // 60_000} min)" if duration_ms and duration_ms % 60_000 == 0 else ""
    return f"{duration_ms} ms{minutes}"


def _when(epoch_ms: object) -> str:
    if not isinstance(epoch_ms, int):
        return NOT_RECORDED
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    return f"{moment} ({epoch_ms} ms)"


def _money(paise: object) -> str:
    if not isinstance(paise, int):
        return NOT_RECORDED
    whole, fraction = divmod(abs(paise), 100)
    return f"{'-' if paise < 0 else ''}INR {whole:,}.{fraction:02d} ({paise} paise)"


def _ids(values: object, *, empty: str = "none") -> str:
    listed = list(values) if isinstance(values, list) else []
    return ", ".join(_code(value) for value in listed) if listed else empty


def _yes(value: object) -> str:
    if value is True:
        return "yes"
    return "no" if value is False else NOT_RECORDED


def _section(receipt: dict[str, JsonValue], name: str) -> dict[str, JsonValue]:
    section = receipt[name]
    if not isinstance(section, dict):
        raise ContractError(f"receipt section {name!r} is not an object; render only what build_receipt returned")
    return section


def render_receipt_markdown(receipt: dict[str, JsonValue]) -> str:
    """The receipt for a person to read. Built from the JSON receipt alone, so it is as deterministic."""
    lines = [
        f"# Recovery receipt {_code(receipt['receipt_version'])}",
        "",
        f"Receipt hash {_code(receipt['receipt_hash'])}: SHA-256 over the canonical JSON of the sections "
        f"{', '.join(_code(name) for name in HASHED_SECTIONS)}. {HASH_RULE} Identity and wall-clock times are not "
        "hashed.",
        "",
        f"> {BOUNDARY_STATEMENT}",  # the module's own sentence, not stored text
        "",
    ]
    for render in (_md_identity, _md_versions, _md_evidence, _md_proposal, _md_approved, _md_preconditions,
                   _md_execution, _md_postconditions, _md_value, _md_boundary):
        lines.extend(render(receipt))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _md_identity(receipt: dict[str, JsonValue]) -> list[str]:
    identity = _section(receipt, "identity")
    rows = [f"| {_text(name.replace('_', ' '))} | {_code(value) if value is not None else 'none'} |"
            for name, value in identity.items()]
    return ["## Identity (not hashed)", "", "| Record | ID |", "|---|---|", *rows]


def _md_versions(receipt: dict[str, JsonValue]) -> list[str]:
    versions = _section(receipt, "versions")
    hosted = _section(receipt, "proposal").get("mode") == ModelMode.HOSTED.value

    def shown(name: str, value: JsonValue) -> str:
        if value is not None:
            return _code(value)
        return "not reported by a hosted provider" if hosted and name == "model_digest" else "not used"

    return ["## Versions", "", *(f"- {_text(name.replace('_', ' '))}: {shown(name, value)}"
                                 for name, value in versions.items())]


def _md_evidence(receipt: dict[str, JsonValue]) -> list[str]:
    evidence = _section(receipt, "evidence")
    lines = ["## Evidence", "", f"- Diagnosis packet hash: {_code(evidence['packet_hash'])}",
             f"- Evidence version the plan hash binds: {_code(evidence['planned_evidence_version'])}"]
    if not evidence["recorded"]:
        return [*lines, "- Execution-time evidence: not recorded (executed before receipt records)"]
    targets = evidence["targets"] if isinstance(evidence["targets"], list) else []
    lines += [
        f"- Evidence version at execution: {_code(evidence['evidence_version_at_execution'])} "
        f"({'unchanged' if evidence['evidence_unchanged'] else 'changed'})",
        f"- Provider state counts as fresh for {_ms(evidence['stale_after_ms'])}",
        f"- Snapshot IDs used: {len(evidence['snapshot_ids']) if isinstance(evidence['snapshot_ids'], list) else 0}; "
        "each, with its known-at time, is listed per target in the JSON receipt",
        "", "| Order | Observations | Snapshots | Newest known at | Freshness at execution |", "|---|---|---|---|---|",
    ]
    for target in targets:
        if isinstance(target, dict):
            observations = target["observations"] if isinstance(target["observations"], list) else []
            snapshots = sum(isinstance(item, dict) and item["source"] == "snapshot" for item in observations)
            lines.append(f"| {_code(target['order_id'])} | {len(observations)} | {snapshots} | "
                         f"{_when(target['newest_known_at_ms'])} | {_ms(target['freshness_ms'])} |")
    return lines


def _md_proposal(receipt: dict[str, JsonValue]) -> list[str]:
    proposal = _section(receipt, "proposal")
    if not proposal["recorded"]:
        return ["## Proposal", "", "- No diagnosis is linked to this plan"]
    source = [f"- Source: {_text(proposal['mode_label'])}"] if "mode_label" in proposal else []  # hosted only
    return [
        "## Proposal", "",
        f"- Mode: {_text(proposal['mode'])}; status: {_text(proposal['status'])}",
        *source,
        f"- Cause: {_code(proposal['hypothesis'])}; faulting change {_code(proposal['faulting_change_id'])}; "
        f"method {_code(proposal['affected_method'])}; evidence level {_code(proposal['evidence_level'])}",
        f"- Proposed operation: {_code(proposal['proposed_operation'])}; next check {_code(proposal['next_check'])}",
        f"- Supporting evidence: {_ids(proposal['supporting_evidence_ids'])}",
        f"- Contradicting evidence: {_ids(proposal['contradicting_evidence_ids'])}",
        f"- Unsafe proposals blocked: {_text(proposal['unsafe_proposals'])}; validator reasons recorded: "
        f"{_text(proposal['validator_reasons'])}",
    ]


def _md_approved(receipt: dict[str, JsonValue]) -> list[str]:
    approved = _section(receipt, "approved_operation")
    approval, fix = approved["approval"], approved["fix"]
    lines = [
        "## Approved operation", "",
        f"- Operation: {_code(approved['operation'])} on {_text(approved['target_count'])} orders worth "
        f"{_money(approved['target_value_paise'])}",
        f"- Plan hash: {_code(approved['plan_hash'])} (policy {_code(approved['policy_version'])})",
        f"- Planned at {_when(approved['planned_at_ms'])}; the plan expires at {_when(approved['plan_expires_at_ms'])}",
    ]
    if isinstance(approval, dict):
        lines.append(f"- Approved by {_code(approval['operator_id'])} at {_when(approval['approved_at_ms'])}; the "
                     f"approval expires at {_when(approval['expires_at_ms'])}; it names this plan hash: "
                     f"{_yes(approval['matches_plan_hash'])}")
    else:
        lines.append("- Approval: none recorded")
    if isinstance(fix, dict):
        lines.append(f"- Fix {_code(fix['fix_id'])} validated by {_code(fix['operator_id'])} against "
                     f"{_code(fix['deployed_version'])}: reproduced {_yes(fix['reproduced'])}, passed "
                     f"{_yes(fix['passed'])}")
    if _section(receipt, "execution")["recorded"]:  # its per-order table lists every target, one per row
        lines.append(f"- Target orders: {_text(approved['target_count'])}, one per row under Execution, sorted")
    else:
        lines.append(f"- Target orders, sorted: {_ids(approved['target_ids'])}")
    return lines


def _md_preconditions(receipt: dict[str, JsonValue]) -> list[str]:
    preconditions = _section(receipt, "preconditions")
    return ["## Preconditions", "", "### At planning", "", *_md_planning(preconditions["planning"]), "",
            "### At execution", "", *_md_checks(preconditions["execution"])]


def _md_planning(planning: JsonValue) -> list[str]:
    if not isinstance(planning, dict) or not planning["recorded"]:
        return ["- Not recorded (planned before receipt records)"]
    exclusions = planning["exclusions"] if isinstance(planning["exclusions"], list) else []
    dry_runs = planning["dry_runs"] if isinstance(planning["dry_runs"], list) else []
    failed_runs = [item for item in dry_runs if isinstance(item, dict) and not item["passed"]]
    return [
        f"- {_text(planning['candidate_orders'])} candidate orders: {_text(planning['targets'])} targets, "
        f"{len(exclusions)} excluded",
        f"- Dry runs of {_code(planning['corrected_version'])}: {len(dry_runs)}, {_text(planning['dry_runs_passed'])} "
        "passed",
        "", "| Excluded order | Amount | Reasons |", _THREE_COLUMNS,
        *(f"| {_code(item['order_id'])} | {_money(item['amount_paise'])} | {_ids(item['reasons'], empty='')} |"
          for item in exclusions if isinstance(item, dict) and isinstance(item["reasons"], list)),
        "", "| Dry run that did not pass | Reason | Detail |", _THREE_COLUMNS,
        *(f"| {_code(item['order_id'])} | {_text(item['reason'])} | {_text(item['detail'])} |" for item in failed_runs),
    ]


def _md_checks(execution: JsonValue) -> list[str]:
    if not isinstance(execution, dict) or not execution["recorded"]:
        return ["- Not recorded (executed before receipt records)"]
    checks = execution["checks"] if isinstance(execution["checks"], list) else []
    return [
        f"- {_text(execution['passed'])} passed, {_text(execution['failed'])} failed, {_text(execution['not_run'])} "
        "not run",
        "", "| Check | Result | Details |", _THREE_COLUMNS,
        *(f"| {_code(check['code'])} | {_text(check['result'])} | {_text('; '.join(check['details']))} |"
          for check in checks if isinstance(check, dict) and isinstance(check["details"], list)),
    ]


def _md_execution(receipt: dict[str, JsonValue]) -> list[str]:
    execution = _section(receipt, "execution")
    lines = ["## Execution", "", f"- Status: {_code(execution['status'])} at {_when(execution['executed_at_ms'])}"]
    if not execution["recorded"]:
        return [*lines, f"- Fulfilled orders (from the action row): {_ids(execution['fulfilled_order_ids'])}"]
    results = execution["results"] if isinstance(execution["results"], dict) else {}
    orders = execution["orders"] if isinstance(execution["orders"], list) else []
    lines += [f"- Orders: {', '.join(f'{_text(name)} {_text(results.get(name, 0))}' for name in ORDER_RESULTS)}",
              "", "| Order | Payment | Amount | Result | Reasons |", "|---|---|---|---|---|"]
    lines += [f"| {_code(order['order_id'])} | {_code(order['payment_id'])} | {_money(order['amount_paise'])} | "
              f"{_text(order['result'])} | {_ids(order['reasons'], empty='')} |"
              for order in orders if isinstance(order, dict) and isinstance(order["reasons"], list)]
    return lines


def _md_postconditions(receipt: dict[str, JsonValue]) -> list[str]:
    measured = _section(receipt, "postconditions")
    if not measured["recorded"]:
        return ["## Postconditions", "", "- Not recorded (executed before receipt records)"]
    outstanding = measured["outstanding"] if isinstance(measured["outstanding"], list) else []
    lines = [
        "## Postconditions", "",
        f"- Captured payments: {_text(measured['captured_payments_before'])} before, "
        f"{_text(measured['captured_payments_after'])} after; captured value "
        f"{_money(measured['captured_paise_before'])} before, {_money(measured['captured_paise_after'])} after",
        f"- New captures: {_ids(measured['new_captured_payment_ids'])}; capture unchanged on every target: "
        f"{_yes(measured['capture_unchanged'])}",
        f"- Duplicate fulfilments: {_text(measured['duplicate_fulfilments'])}; payments behind more than one "
        f"fulfilment: {_ids(measured['payments_fulfilling_several_orders'])}",
        f"- Outstanding exceptions: {len(outstanding)}"
        + ("" if measured["candidates_recorded"] else " (targets only: the plan's candidates were not recorded)"),
    ]
    if outstanding:
        lines += ["", "| Order | Held at | Reasons |", _THREE_COLUMNS]
        lines += [f"| {_code(item['order_id'])} | {_text(item['stage'])} | {_ids(item['reasons'], empty='')} |"
                  for item in outstanding if isinstance(item, dict) and isinstance(item["reasons"], list)]
    return lines


def _md_value(receipt: dict[str, JsonValue]) -> list[str]:
    value = _section(receipt, "value")
    lines = ["## Value", "", "| Figure | Amount | Definition |", _THREE_COLUMNS]
    for name, label in (("restored_processing_value_paise", "Restored processing value"),
                        ("unresolved_value_paise", "Unresolved value"),
                        ("additional_captured_paise", "Additional captured")):
        lines.append(f"| {label} | {_money(value.get(name))} | {VALUE_DEFINITIONS[name]} |")  # module text
    if value["recorded"]:
        lines += ["", f"- Unresolved orders: {_text(value['unresolved_orders'])}; counted but not valued: "
                      f"{_ids(value['unvalued_unresolved_order_ids'])}"]
    return lines


def _md_boundary(receipt: dict[str, JsonValue]) -> list[str]:
    boundary = _section(receipt, "boundary")
    return [
        "## Boundary", "",
        f"- Environment: {_code(boundary['environment'])}",
        f"- Provider calls made by this action: {_text(boundary['provider_calls_made'])}",
        f"- Original capture unchanged: {_yes(boundary['original_capture_unchanged'])}",
    ]
