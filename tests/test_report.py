"""The run report: an explicit field list for every record it exports, one read transaction, and an ASCII file.

Security audit run 2, hardening notes 8, 9 and 2. ``build_report`` names each field it exports, so a column or model
field added later stays out of every report until someone lists it. For the records that exist it exports exactly what
the code before the lists did: ``report_built_the_old_way`` keeps that code, and a seeded run with every kind of record
gives the same bytes both ways. The whole export reads one snapshot, so a write that lands while it runs is either all
in the report or not in it at all. The file the UI saves escapes what the CLI's file escapes, so both are ASCII.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import RUN_A
from pydantic import JsonValue
from test_executor import APPROVAL_TTL_MS, APPROVED_AT
from test_plans import OPERATOR, build_batch, build_plan, validate
from test_tools import seed

from recovery_workbench import cli, diagnosis, diagnosis_store, executor, fixes, packet, plans, policy, report, storage
from recovery_workbench.contracts import DiagnosisOutput, DiagnosisStatus, EvidenceItem, ModelMode
from recovery_workbench.diagnosis import BlockedProposal

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORTED_AT_MS = 1_794_500_000_000  # the export's wall clock, fixed so that two exports can be compared byte for byte
FROZEN_DIGEST = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"
# What the report leaves out of a record on purpose. Every other field of every exported record is in the report.
WITHHELD_VALIDATION_FIELDS = frozenset({"run_id", "fixture_suite_id", "deployed_results", "corrected_results"})
# Q46: fields only a hosted diagnosis fills. Its entry exports provider_host; token_usage stays in the stored envelope.
HOSTED_ONLY_FIELDS = frozenset({"provider_host", "token_usage"})


def report_built_the_old_way(conn: sqlite3.Connection, *, run_id: str) -> dict[str, JsonValue]:
    """``report.build_report`` before the field lists and the single read transaction (commit 3f6163c), unchanged.

    It calls the two helpers that change did not touch, ``_validation_entry`` and ``_outcome``.
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
            "SELECT * FROM approvals WHERE run_id = ? ORDER BY approved_at_ms, approval_id", (run_id,)
        ).fetchall()
    diagnoses = diagnosis_store.list_diagnoses(conn, run_id=run_id)
    stored_plans = [plans.require_plan(conn, run_id=run_id, plan_id=row["plan_id"]) for row in plan_rows]
    validations = [
        fixes.require_fix_validation(conn, run_id=run_id, validation_id=row["validation_id"]) for row in validation_rows
    ]
    actions = executor.list_actions(conn, run_id=run_id)
    return {
        "report_version": report.REPORT_VERSION,
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
        "incidents": [incident.model_dump(mode="json") for incident in incidents],
        "evidence": _old_evidence_by_packet(conn, run_id),
        "diagnoses": [_old_diagnosis_entry(stored) for stored in diagnoses],
        "fix_validations": [report._validation_entry(validation) for validation in validations],
        "plans": [stored.model_dump(mode="json") for stored in stored_plans],
        "approvals": [{key: row[key] for key in row.keys()} for row in approval_rows],
        "actions": [action.model_dump(mode="json") for action in actions],
        "outcome": report._outcome(stored_plans, actions),
    }


def _old_evidence_by_packet(conn: sqlite3.Connection, run_id: str) -> list[JsonValue]:
    with storage.read_transaction(conn):
        references = sorted({
            record.source_record_id
            for record in storage.list_evidence(conn, run_id=run_id)
            if record.source_table == packet.EVIDENCE_SOURCE_TABLE
        })
    entries: list[JsonValue] = []
    for reference in references:
        items = packet.stored_packet_items(conn, run_id=run_id, reference=reference)
        entries.append({"packet": reference, "items": [item.model_dump(mode="json") for item in items]})
    return entries


def _old_diagnosis_entry(stored: diagnosis_store.StoredDiagnosis) -> dict[str, JsonValue]:
    entry = stored.model_dump(mode="json", exclude=set(HOSTED_ONLY_FIELDS))  # added since; no hosted diagnosis here
    entry["mode_label"] = stored.mode_label
    return entry


# --------------------------------------------------------------------------- a run with every kind of record


def _model_run(stored: diagnosis_store.StoredDiagnosis, status: DiagnosisStatus) -> diagnosis.DiagnosisRun:
    """A model run over the rules diagnosis's packet: validated after two refused proposals, or needing review."""
    blocked = tuple(BlockedProposal.model_validate(proposal) for proposal in (
        {"call_index": 0, "hypothesis": "ambiguous_payment_state", "proposed_operation": "reprocess_inbox",
         "allowed_operations": ["refresh_provider_state", "escalate"]},
        {"call_index": 0, "rule": "change_consistency", "hypothesis": "provider_method_degradation",
         "proposed_operation": "escalate", "faulting_change_id": "deploy_9b85880366",
         "allowed_operations": ["escalate"]},
    ))
    validated = status is DiagnosisStatus.VALIDATED
    return diagnosis.DiagnosisRun(
        status=status, output=stored.output if validated else None, calls=(), think=False, deadline_s=90.0,
        reasons=("call 1: the operation policy refused reprocess_inbox",) if validated else (
            "the model is unavailable: Ollama GET /api/tags failed: ConnectError('connection refused')",),
        repairs=1 if validated else 0, elapsed_ms=7_200 if validated else 0, prompt_version=diagnosis.PROMPT_VERSION,
        packet_hash=stored.packet_hash, unsafe_proposals=blocked if validated else (),
    )


def _record_every_kind(conn: sqlite3.Connection, run_id: str, recordings: Path) -> None:
    """Diagnoses of every mode and status, a rejected and an executed reprocess, an escalation and a replay plan."""
    rules = cli.diagnose_run(conn, run_id=run_id, mode=ModelMode.RULES, recordings_dir=recordings)
    validation = cli.validate_fix(conn, run_id=run_id, fix_id="payload@5", diagnosis_id=rules.diagnosis_id,
                                  operator=OPERATOR)
    unapproved = cli.build_plan(conn, run_id=run_id, fix_validation_id=validation.validation_id)
    with pytest.raises(executor.PlanRejectedError):  # no approval: recorded as a rejected action
        cli.execute_plan(conn, run_id=run_id, plan_id=unapproved.plan.plan_id)
    approved = cli.build_plan(conn, run_id=run_id, fix_validation_id=validation.validation_id)
    cli.approve_plan(conn, run_id=run_id, plan_id=approved.plan.plan_id, plan_hash=approved.plan.plan_hash,
                     operator=OPERATOR)
    cli.execute_plan(conn, run_id=run_id, plan_id=approved.plan.plan_id)
    local = diagnosis_store.record_model_diagnosis(
        conn, run_id=run_id, incident_id=rules.incident_id, run=_model_run(rules, DiagnosisStatus.VALIDATED),
        model_id="qwen3:8b", model_digest=FROZEN_DIGEST,
    )
    diagnosis_store.record_model_diagnosis(
        conn, run_id=run_id, incident_id=rules.incident_id, run=_model_run(rules, DiagnosisStatus.VALIDATED),
        model_id="qwen3:8b", model_digest=FROZEN_DIGEST, mode=ModelMode.RECORDED,
        recorded_from=f"diagnosis:{local.diagnosis_id}", recorded_original_wall_ms=local.created_wall_ms,
    )
    unavailable = diagnosis_store.record_model_diagnosis(
        conn, run_id=run_id, incident_id=rules.incident_id, run=_model_run(rules, DiagnosisStatus.NEEDS_REVIEW),
        model_id="qwen3:8b", model_digest=None,
    )
    cli.escalate_diagnosis(conn, run_id=run_id, diagnosis_id=unavailable.diagnosis_id, operator=OPERATOR)
    plans.prepare_provider_replay(conn, run_id=run_id, incident_id=rules.incident_id, diagnosis_id=local.diagnosis_id,
                                  now_ms=cli.next_operator_time(conn, run_id), ttl_ms=1_800_000,
                                  stale_after_ms=3_600_000)
    cli.diagnose_run(conn, run_id=run_id, mode=ModelMode.RULES, recordings_dir=recordings)  # a later packet


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sqlite3.Connection, str]]:
    directory = tmp_path_factory.mktemp("report_run")
    conn, summary = seed(directory, "worker_backlog")
    try:
        _record_every_kind(conn, summary.run_id, directory / "recordings")
        yield conn, summary.run_id
    finally:
        conn.close()


@pytest.fixture
def frozen_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "wall_clock_ms", lambda: EXPORTED_AT_MS)


# --------------------------------------------------------------------------- note 8: explicit field lists


@pytest.mark.usefixtures("frozen_wall_clock")
def test_the_field_lists_export_a_seeded_run_byte_for_byte_as_before(
    recorded: tuple[sqlite3.Connection, str], tmp_path: Path
) -> None:
    conn, run_id = recorded
    old, new = report_built_the_old_way(conn, run_id=run_id), report.build_report(conn, run_id=run_id)
    # Not vacuous: every kind of record, and every field of each, is in the comparison.
    assert {(entry["mode"], entry["status"], bool(entry["unsafe_proposals"])) for entry in new["diagnoses"]} == {
        ("rules", "validated", False), ("local", "validated", True), ("recorded", "validated", True),
        ("local", "needs_review", False)}
    assert sorted({entry["operation"] for entry in new["plans"]}) == [
        "escalate", "prepare_provider_replay", "reprocess_inbox"]
    assert sorted(entry["status"] for entry in new["actions"]) == ["rejected", "succeeded", "succeeded"]
    assert (len(new["approvals"]), len(new["evidence"]), len(new["incidents"])) == (1, 2, 1)
    assert new["fix_validations"]
    # What the API sends (keys in order) and the file the CLI writes, byte for byte.
    assert json.dumps(new, ensure_ascii=False, separators=(",", ":")) == json.dumps(
        old, ensure_ascii=False, separators=(",", ":"))
    written = report.write_report(conn, run_id=run_id, path=tmp_path / "report.json")
    assert written.read_bytes() == (json.dumps(old, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert repr(new) == repr(old)  # and the same Python types: no enum member stands in for its value


def test_every_field_of_an_exported_record_is_exported_or_withheld_on_purpose(
    recorded: tuple[sqlite3.Connection, str]
) -> None:
    """A column or model field added later fails here until someone decides whether the report exports it."""
    conn, run_id = recorded
    exported = report.build_report(conn, run_id=run_id)
    records = {"incidents": storage.IncidentRecord, "diagnoses": diagnosis_store.StoredDiagnosis,
               "plans": plans.StoredPlan, "actions": executor.StoredAction}
    for name, model in records.items():
        derived = {"mode_label"} if name == "diagnoses" else set()
        hosted_only = HOSTED_ONLY_FIELDS if name == "diagnoses" else frozenset()  # this run has no hosted diagnosis
        fields = frozenset(model.model_fields) - hosted_only | derived
        assert {frozenset(entry) for entry in exported[name]} == {fields}, name
    columns = frozenset(row["name"] for row in conn.execute("PRAGMA table_info(approvals)"))
    assert {frozenset(entry) for entry in exported["approvals"]} == {columns}
    outputs = [entry["output"] for entry in exported["diagnoses"] if entry["output"] is not None]
    assert {frozenset(output) for output in outputs} == {frozenset(DiagnosisOutput.model_fields)}
    proposals = [proposal for entry in exported["diagnoses"] for proposal in entry["unsafe_proposals"]]
    assert {frozenset(proposal) for proposal in proposals} == {frozenset(BlockedProposal.model_fields)}
    items = [item for entry in exported["evidence"] for item in entry["items"]]
    assert {frozenset(item) for item in items} == {frozenset(EvidenceItem.model_fields)}
    validation_fields = frozenset(fixes.FixValidation.model_fields) - WITHHELD_VALIDATION_FIELDS
    assert {frozenset(entry) for entry in exported["fix_validations"]} == {
        validation_fields | {"deployed_failures", "corrected_failures"}}


# --------------------------------------------------------------------------- note 9: one read transaction


def test_the_whole_report_is_read_in_one_transaction(recorded: tuple[sqlite3.Connection, str]) -> None:
    conn, run_id = recorded
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        report.build_report(conn, run_id=run_id)
    finally:
        conn.set_trace_callback(None)
    boundaries = [statement for statement in statements if statement.split()[0] in ("BEGIN", "COMMIT", "ROLLBACK")]
    assert boundaries == ["BEGIN DEFERRED", "COMMIT"]
    assert (statements[0], statements[-1]) == ("BEGIN DEFERRED", "COMMIT")  # every read is inside it
    assert len(statements) > 10  # not vacuous: the helpers' reads are traced too


@pytest.mark.usefixtures("frozen_wall_clock")
def test_a_write_that_lands_while_the_report_reads_is_all_in_it_or_not_at_all(
    conn: sqlite3.Connection, open_connection: Callable[[], sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval commits after the export has read the approvals and before it reads the plans. Read in several
    transactions, the export showed the plan approved with no approval for it; read in one, it shows neither."""
    batch = build_batch(conn)
    preview = build_plan(conn, batch, validate(conn, batch))
    before = report.build_report(conn, run_id=RUN_A)
    other = open_connection()
    read_diagnoses = diagnosis_store.list_diagnoses
    approved: list[str] = []

    def approve_then_read(
        reader: sqlite3.Connection, *, run_id: str, incident_id: str | None = None
    ) -> tuple[diagnosis_store.StoredDiagnosis, ...]:
        if not approved:  # from another connection, between the export's reads of approvals and of plans
            approved.append(executor.approve_plan(
                other, run_id=RUN_A, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                operator_id=OPERATOR, now_ms=APPROVED_AT, ttl_ms=APPROVAL_TTL_MS).approval_id)
        return read_diagnoses(reader, run_id=run_id, incident_id=incident_id)

    monkeypatch.setattr(diagnosis_store, "list_diagnoses", approve_then_read)
    during = report.build_report(conn, run_id=RUN_A)
    after = report.build_report(conn, run_id=RUN_A)
    assert approved
    assert [(plan["plan_id"], plan["status"]) for plan in during["plans"]] == [(preview.plan.plan_id, "draft")]
    assert during == before
    assert [plan["status"] for plan in after["plans"]] == ["approved"]
    assert [approval["approval_id"] for approval in after["approvals"]] == approved


# --------------------------------------------------------------------------- note 2: the file the UI saves

# The frontend has no test runner and this image has no JavaScript engine, so the rule is checked as written: the
# source must hold it, and the rule, applied to what JSON.stringify writes, must give what json.dumps writes.
UI_RULE = REPO_ROOT / "frontend" / "src" / "asciiJson.ts"
UI_PANEL = REPO_ROOT / "frontend" / "src" / "components" / "ReportPanel.tsx"
UI_STRINGIFY = "JSON.stringify(value, null, 2)"
ESCAPED_FROM, ESCAPED_TO = 0x7F, 0xFFFF  # every UTF-16 code unit from U+007F up
UI_ESCAPED_UNITS = rf"/[\u{ESCAPED_FROM:04x}-\u{ESCAPED_TO:04x}]/g"
UI_ESCAPE = '(unit) => String.raw`\\u${unit.codePointAt(0)!.toString(16).padStart(4, "0")}`'
# C0 and C1 controls with the 8-bit CSI, DEL, NEL, bidi and zero-width characters, the line and paragraph separators,
# a BOM, characters outside the BMP (a tag character too), lone surrogates and plain text, as keys and as values: a
# signed webhook's error_reason becomes a fact key.
TRICKY = (
    "\x00\x07\x08\t\n\x0b\x0c\r\x1b[2J\x1b]52;c;QQ==\x07\x1f", "\x7f", "\x80\x85\x9b\x9f", "\xa0\xad",
    "\xe9 \u65e5\u672c", "\u200b\u200c\u200d\u200e\u200f", "\u202a\u202b\u202c\u202d\u202e",
    "\u2066\u2067\u2068\u2069", "\u2028\u2029", "\ufeff\ufffd\uffff", "\U0001f600\U000e0041", "\ud800", "x\udfffy",
    'quote " backslash \\ slash /', "", "plain",
)


def stringified_like_the_ui(value: JsonValue) -> str:
    """``JSON.stringify(value, null, 2)`` for these inputs: the same escapes for C0, quote and backslash, the rest raw.

    JSON.stringify also escapes a lone surrogate, which this leaves raw; the rule escapes it to the same text.
    """
    return json.dumps(value, ensure_ascii=False, indent=2)


def escaped_like_the_ui(text: str, low: int, high: int) -> str:
    """The UI's ``replace``: a regular expression without the u flag sees UTF-16 code units, one at a time."""
    encoded = text.encode("utf-16-le", "surrogatepass")
    units = (int.from_bytes(encoded[index:index + 2], "little") for index in range(0, len(encoded), 2))
    return "".join(f"\\u{unit:04x}" if low <= unit <= high else chr(unit) for unit in units)


def test_the_report_file_the_ui_saves_escapes_like_the_cli() -> None:
    rule, panel = UI_RULE.read_text(encoding="utf-8"), UI_PANEL.read_text(encoding="utf-8")
    assert UI_STRINGIFY in rule
    assert UI_ESCAPED_UNITS in rule
    assert UI_ESCAPE in rule
    assert "new Blob([asciiJson(report)]" in panel
    assert "JSON.stringify" not in panel
    report_like: JsonValue = {
        "evidence": [{"items": [{"facts": {text: index for index, text in enumerate(TRICKY)},
                                 "text": "".join(TRICKY)}]}],
        "values": list(TRICKY), "numbers": [0, -1, 12_502_700], "flags": [True, False, None], "empty": [{}, []],
    }
    saved = escaped_like_the_ui(stringified_like_the_ui(report_like), ESCAPED_FROM, ESCAPED_TO)
    assert saved == json.dumps(report_like, indent=2)  # json.dumps escapes as ensure_ascii=True, write_report's default
    assert saved.isascii()
    assert not stringified_like_the_ui(report_like).isascii()  # not vacuous
