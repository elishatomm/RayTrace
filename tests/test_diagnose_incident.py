"""Incident diagnosis end to end: rules, local and recorded modes, policies, failures and locking."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import types
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from test_attribution_rules import fresh_runs
from test_tools import seed

from recovery_workbench import diagnosis, diagnosis_store, packet, report, rules, selective, settings, storage
from recovery_workbench.contracts import (
    ContractError,
    DiagnosisOutput,
    DiagnosisStatus,
    EvidenceKind,
    EvidencePacket,
    ModelMode,
)
from recovery_workbench.diagnosis import ChatReply, ChatRequest, ModelCallError
from recovery_workbench.simulator import driver, families, merchant, runtime
from recovery_workbench.simulator.world import generate_world

FAKE_DIGEST = "f" * 64


# --------------------------------------------------------------------------- a stand-in for the rules baseline (core keys only)


def stub_rules_diagnose(built: EvidencePacket) -> DiagnosisOutput:
    """A small stand-in rules baseline over pkt-1 core keys, so these tests do not move with the real one."""
    items = {item.evidence_id: item for item in built.items}
    fired = items["ev_trigger"].facts["fired"]
    fixture = items["ev_fixture"]
    errors = items.get("ev_worker_errors")
    base = {"contradicting_evidence_ids": (), "missing_checks": (), "affected_method": "none"}
    if errors is not None:
        first_failure = errors.facts["first_failure_ms"]
        before = [item for item in built.items if item.kind is EvidenceKind.DEPLOYMENT
                  and item.facts["deployed_at_ms"] <= first_failure]
        level = "reproduced_in_fixture" if fixture.facts["tests_failed"] else "supported"
        return DiagnosisOutput(
            **base, hypothesis="merchant_worker_regression",
            faulting_change_id=before[-1].facts["deploy_id"] if before else "unknown",
            evidence_level=level, supporting_evidence_ids=("ev_worker_errors", "ev_fixture"),
            next_check="validate_corrected_handler", proposed_operation="reprocess_inbox",
            rationale="Worker failures started after a deployment; the fixture reproduces them.",
        )
    drops = [name for name in fired if name.startswith("method_authorization_drop[")]
    if drops:
        return DiagnosisOutput(
            **{**base, "affected_method": drops[0].removeprefix("method_authorization_drop[").removesuffix("]")},
            hypothesis="provider_method_degradation", faulting_change_id="none", evidence_level="supported",
            supporting_evidence_ids=("ev_trigger",), next_check="fetch_provider_snapshot", proposed_operation="escalate",
            rationale="One method's mature authorisation ratio fell while processing stayed healthy.",
        )
    state = items["ev_provider_state"].facts
    if state["failed_then_authorised_later"] or state["stale_payments"] or "unresolved_spike" in fired:
        return DiagnosisOutput(
            **base, hypothesis="ambiguous_payment_state", faulting_change_id="none", evidence_level="supported",
            supporting_evidence_ids=("ev_provider_state",), next_check="fetch_provider_snapshot",
            proposed_operation="refresh_provider_state",
            rationale="Payments failed and then authorised later, so earlier failures are not final.",
        )
    return DiagnosisOutput(
        **base, hypothesis="no_incident", faulting_change_id="none", evidence_level="insufficient",
        supporting_evidence_ids=("ev_trigger",), next_check="none", proposed_operation="no_action",
        rationale="Nothing points at a failure.",
    )


def install_stub_rules(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Stand in for the module ``diagnosis_store.rules_baseline`` imports: ``selective``, whose ``rules_v3`` answers."""
    module = types.ModuleType("recovery_workbench.selective")
    module.RULES_V3_VERSION = "rules-stub"  # type: ignore[attr-defined]
    module.rules_v3 = stub_rules_diagnose  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "recovery_workbench.selective", module)
    return module


# --------------------------------------------------------------------------- fakes and fixtures


class FakeModel:
    """Scripted model client. ``lock_probe`` opens a second connection during the call to prove no write lock is held."""

    model = "fake-model:1b"

    def __init__(self, *answers: object, lock_probe: Path | None = None, delay_s: float = 0.0,
                 digest_error: Exception | None = None) -> None:
        self._answers = list(answers)
        self.lock_probe = lock_probe
        self.delay_s = delay_s
        self.digest_error = digest_error
        self.requests: list[ChatRequest] = []
        self.lock_was_free: list[bool] = []

    def model_digest(self) -> str:
        if self.digest_error is not None:
            raise self.digest_error
        return FAKE_DIGEST

    def chat(self, request: ChatRequest, *, timeout_s: float) -> ChatReply:
        self.requests.append(request)
        if self.lock_probe is not None:
            probe = sqlite3.connect(self.lock_probe, timeout=0.2, isolation_level=None)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("ROLLBACK")
                self.lock_was_free.append(True)
            except sqlite3.OperationalError:
                self.lock_was_free.append(False)
            finally:
                probe.close()
        if self.delay_s:
            time.sleep(self.delay_s)
        answer = self._answers.pop(0)
        return ChatReply(content=answer if isinstance(answer, str) else json.dumps(answer), done_reason="stop")


def answer(built: EvidencePacket, **changes: object) -> dict[str, object]:
    """A valid worker-regression answer for ``built``, with ``changes`` applied."""
    return {
        "rationale": "The parser fails on captured events after a deployment.",
        "supporting_evidence_ids": ["ev_worker_errors", "ev_fixture"],
        "contradicting_evidence_ids": [],
        "hypothesis": "merchant_worker_regression",
        "faulting_change_id": built.candidate_change_ids[1],
        "affected_method": "none",
        "evidence_level": "supported",
        "proposed_operation": "reprocess_inbox",
        "next_check": "validate_corrected_handler",
        "missing_checks": [],
        **changes,
    }


def make_settings(**overrides: str) -> settings.Settings:
    env = {"WORKBENCH_RUN_TIMEOUT_SECONDS": "30", "WORKBENCH_MODEL": "fake-model:1b", **overrides}
    return settings.load_settings(env)


@pytest.fixture
def backlog(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, driver.SeedSummary, Path]]:
    conn, summary = seed(tmp_path, "worker_backlog")
    yield conn, summary, tmp_path / "workbench.sqlite"
    conn.close()


def deployed_now(conn: sqlite3.Connection, summary: driver.SeedSummary) -> merchant.DeployedMerchant:
    return runtime.active_merchant(conn, run_id=summary.run_id, at_ms=storage.require_run(conn, summary.run_id).clock_ms)


def the_packet(conn: sqlite3.Connection, summary: driver.SeedSummary) -> EvidencePacket:
    return packet.build_packet(conn, run_id=summary.run_id, incident_id=summary.incident_ids[0],
                               cutoff_ms=summary.cutoff_ms, deployed=deployed_now(conn, summary), persist=False)


def diagnose(conn: sqlite3.Connection, summary: driver.SeedSummary, mode: ModelMode, *,
             client: FakeModel | None = None, recordings: Path | None = None,
             configured: settings.Settings | None = None,
             deployed: merchant.DeployedMerchant | None = None) -> diagnosis_store.StoredDiagnosis:
    return diagnosis_store.diagnose_incident(
        conn, run_id=summary.run_id, incident_id=summary.incident_ids[0], mode=mode,
        settings=configured or make_settings(), deployed=deployed or deployed_now(conn, summary), client=client,
        recordings_dir=recordings,
    )


# --------------------------------------------------------------------------- modes


def test_rules_mode_stores_a_rules_diagnosis_without_model_fields(backlog, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, summary, _ = backlog
    install_stub_rules(monkeypatch)
    stored = diagnose(conn, summary, ModelMode.RULES)
    assert stored.mode is ModelMode.RULES
    assert stored.rules_version == "rules-stub"
    assert (stored.model_id, stored.model_digest, stored.thinking, stored.prompt_version) == (None, None, None, None)
    assert stored.status is DiagnosisStatus.VALIDATED
    assert stored.output.hypothesis.value == "merchant_worker_regression"
    assert "rules baseline" in stored.mode_label
    assert storage.list_evidence(conn, run_id=summary.run_id, source_record_id=f"{summary.incident_ids[0]}@{summary.cutoff_ms}")


def test_a_missing_rules_baseline_is_refused_clearly(backlog, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, summary, _ = backlog
    monkeypatch.setitem(sys.modules, "recovery_workbench.selective", None)
    with pytest.raises(ContractError, match="rules baseline"):
        diagnose(conn, summary, ModelMode.RULES)


def test_rules_mode_stores_rules_v3_where_it_names_another_deployment_than_rules_v2(tmp_path: Path) -> None:
    """Q28: rules mode answers rules-v3. In attribution-v1's first delayed-onset development world (seeds 12101-12123)
    rules-v2 names the innocent deployment and rules-v3 the faulting one, so the stored answer shows which ran."""
    run = next(run for run in fresh_runs() if run.family == "D1" and run.params.get("onset") == "delayed")
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        summary = driver.seed_world(conn, generate_world(families.spec_for(run), run.seed),
                                    oracle_dir=tmp_path / "oracle", provider_dir=tmp_path / "provider")
        built = the_packet(conn, summary)
        stored = diagnose(conn, summary, ModelMode.RULES)
    fault = json.loads(Path(str(summary.oracle_path)).read_text(encoding="utf-8"))["expected"]["faulting_change_id"]
    assert stored.packet_hash == built.packet_hash()
    assert stored.output == selective.rules_v3(built)
    assert stored.output.faulting_change_id == fault != rules.diagnose(built).faulting_change_id
    assert (stored.status, stored.rules_version, stored.validator_version, stored.mode_label) == (
        DiagnosisStatus.VALIDATED, "rules-v3", diagnosis.VALIDATOR_VERSION, "rules baseline rules-v3 (no model)")


def test_a_rules_v2_diagnosis_stored_before_the_switch_keeps_its_row_version_and_label(backlog) -> None:
    """Q28 rewrites no stored data: what rules mode stored before it is read, listed and reported as rules-v2."""
    conn, summary, _ = backlog
    built = packet.build_packet(conn, run_id=summary.run_id, incident_id=summary.incident_ids[0],
                                cutoff_ms=summary.cutoff_ms, deployed=deployed_now(conn, summary))
    earlier = diagnosis_store.record_rules_diagnosis(  # as rules mode stored it before Q28: rules-v2's answer
        conn, run_id=summary.run_id, incident_id=summary.incident_ids[0], packet_hash=built.packet_hash(),
        output=rules.diagnose(built), rules_version=rules.RULES_VERSION,
    )
    select = "SELECT * FROM diagnoses WHERE run_id = ? AND diagnosis_id = ?"
    row_before = tuple(conn.execute(select, (summary.run_id, earlier.diagnosis_id)).fetchone())
    later = diagnose(conn, summary, ModelMode.RULES)
    assert tuple(conn.execute(select, (summary.run_id, earlier.diagnosis_id)).fetchone()) == row_before
    listed = {item.diagnosis_id: (item.rules_version, item.mode_label)
              for item in diagnosis_store.list_diagnoses(conn, run_id=summary.run_id)}
    assert listed == {earlier.diagnosis_id: ("rules-v2", "rules baseline rules-v2 (no model)"),
                      later.diagnosis_id: ("rules-v3", "rules baseline rules-v3 (no model)")}
    reported = report.build_report(conn, run_id=summary.run_id)["diagnoses"]
    assert {entry["diagnosis_id"]: (entry["rules_version"], entry["mode_label"]) for entry in reported} == listed


def test_local_mode_records_model_digest_and_holds_no_lock_during_the_call(backlog, tmp_path: Path) -> None:
    conn, summary, database = backlog
    built = the_packet(conn, summary)
    client = FakeModel(answer(built), lock_probe=database)
    stored = diagnose(conn, summary, ModelMode.LOCAL, client=client, recordings=tmp_path / "recordings")
    assert stored.status is DiagnosisStatus.VALIDATED
    assert stored.mode is ModelMode.LOCAL
    assert (stored.model_id, stored.model_digest, stored.thinking) == ("fake-model:1b", FAKE_DIGEST, False)
    assert stored.prompt_version == diagnosis.PROMPT_VERSION
    assert stored.validator_version == diagnosis.VALIDATOR_VERSION
    assert stored.packet_hash == built.packet_hash()
    assert client.lock_was_free == [True]
    assert (tmp_path / "recordings" / f"{built.packet_hash()}.json").is_file()
    assert client.requests[0].messages[1].content == diagnosis.render_packet(built)


def test_recorded_mode_replays_only_an_identical_packet(backlog) -> None:
    conn, summary, _ = backlog
    built = the_packet(conn, summary)
    live = diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel(answer(built)))
    replay = diagnose(conn, summary, ModelMode.RECORDED)
    assert replay.mode is ModelMode.RECORDED
    assert replay.recorded_from == f"diagnosis:{live.diagnosis_id}"
    assert (replay.model_id, replay.model_digest, replay.output) == (live.model_id, live.model_digest, live.output)
    assert replay.recorded_original_wall_ms == live.created_wall_ms
    assert replay.mode_label.startswith("RECORDED")
    assert "not live inference" in replay.mode_label
    with storage.write_transaction(conn):  # time moves on, so the next packet is a different packet
        storage.advance_clock(conn, summary.run_id, summary.cutoff_ms + 60_000)
    with pytest.raises(ContractError, match="identical packet"):
        diagnose(conn, summary, ModelMode.RECORDED)


def fresh_directory(parent: Path, name: str) -> Path:
    """A new, empty folder next to the backlog run's own files in this test's ``tmp_path``."""
    directory = parent / name
    directory.mkdir()
    return directory


def test_a_recording_replays_on_a_freshly_seeded_copy(backlog, tmp_path: Path) -> None:
    conn, summary, _ = backlog
    recordings = fresh_directory(tmp_path, "recordings")
    diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel(answer(the_packet(conn, summary))), recordings=recordings)
    other_conn, other_summary = seed(fresh_directory(tmp_path, "fresh_copy"), "worker_backlog")
    try:
        replay = diagnose(other_conn, other_summary, ModelMode.RECORDED, recordings=recordings)
        assert replay.mode is ModelMode.RECORDED
        assert replay.recorded_from.startswith("recording:")
        empty = fresh_directory(tmp_path, "empty")
        with pytest.raises(ContractError, match="identical packet"):
            diagnose(other_conn, other_summary, ModelMode.RECORDED, recordings=empty)
    finally:
        other_conn.close()


def test_a_refused_diagnosis_leaves_its_packet_in_the_report(backlog) -> None:
    """The packet is stored before diagnosing, so its evidence is exported even when no diagnosis was stored."""
    conn, summary, _ = backlog
    with pytest.raises(ContractError, match="identical packet"):
        diagnose(conn, summary, ModelMode.RECORDED)  # no recording exists for this packet
    exported = report.build_report(conn, run_id=summary.run_id)
    assert exported["diagnoses"] == []
    assert [entry["packet"] for entry in exported["evidence"]] == [f"{summary.incident_ids[0]}@{summary.cutoff_ms}"]
    assert exported["evidence"][0]["items"]


def test_a_recording_made_with_another_prompt_is_not_replayed(backlog, tmp_path: Path) -> None:
    conn, summary, _ = backlog
    recordings = fresh_directory(tmp_path, "recordings")
    diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel(answer(the_packet(conn, summary))), recordings=recordings)
    [path] = recordings.glob("*.json")
    older = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**older, "prompt_version": "diag-p1"}), encoding="utf-8")
    other_conn, other_summary = seed(fresh_directory(tmp_path, "fresh_copy"), "worker_backlog")
    try:
        with pytest.raises(ContractError, match="made with prompt diag-p1"):
            diagnose(other_conn, other_summary, ModelMode.RECORDED, recordings=recordings)
    finally:
        other_conn.close()


# --------------------------------------------------------------------------- policies and failures


def test_naming_a_change_under_a_change_free_hypothesis_is_refused_and_recorded(backlog) -> None:
    conn, summary, _ = backlog
    built = the_packet(conn, summary)
    inconsistent = answer(built, hypothesis="provider_method_degradation", proposed_operation="escalate",
                          affected_method="upi", next_check="fetch_provider_snapshot")
    consistent = {**inconsistent, "faulting_change_id": "none"}
    stored = diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel(inconsistent, consistent))
    assert stored.status is DiagnosisStatus.VALIDATED
    assert stored.output.faulting_change_id == "none"
    assert [(item.rule, item.faulting_change_id) for item in stored.unsafe_proposals] == [
        ("change_consistency", built.candidate_change_ids[1])
    ]


def test_a_blocked_operation_stays_recorded_after_the_repair(backlog) -> None:
    conn, summary, _ = backlog
    built = the_packet(conn, summary)
    unsafe = answer(built, hypothesis="ambiguous_payment_state", faulting_change_id="none",
                    proposed_operation="reprocess_inbox")
    stored = diagnose(conn, summary, ModelMode.LOCAL,
                      client=FakeModel(unsafe, {**unsafe, "proposed_operation": "refresh_provider_state"}))
    assert stored.status is DiagnosisStatus.VALIDATED
    assert [(item.rule, item.proposed_operation.value) for item in stored.unsafe_proposals] == [
        ("operation_policy", "reprocess_inbox")
    ]
    loaded = diagnosis_store.get_diagnosis(conn, run_id=summary.run_id, diagnosis_id=stored.diagnosis_id)
    assert loaded.unsafe_proposals == stored.unsafe_proposals


def test_malformed_output_stops_within_budget_with_the_evidence_intact(backlog) -> None:
    conn, summary, _ = backlog
    started = time.monotonic()
    stored = diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel("not json", "{still not"),
                      configured=make_settings(WORKBENCH_RUN_TIMEOUT_SECONDS="5"))
    assert time.monotonic() - started < 5
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert stored.output is None
    assert any("not valid JSON" in reason for reason in stored.reasons)
    reference = f"{summary.incident_ids[0]}@{summary.cutoff_ms}"
    assert packet.stored_packet_items(conn, run_id=summary.run_id, reference=reference)


def test_a_slow_model_is_cut_off_by_the_deadline(backlog) -> None:
    conn, summary, _ = backlog
    built = the_packet(conn, summary)
    started = time.monotonic()
    stored = diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel(answer(built), delay_s=3.0),
                      configured=make_settings(WORKBENCH_RUN_TIMEOUT_SECONDS="1"))
    assert time.monotonic() - started < 2.5
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert any("deadline" in reason for reason in stored.reasons)


def test_an_unreachable_model_is_needs_review_not_a_crash(backlog) -> None:
    conn, summary, _ = backlog
    stored = diagnose(conn, summary, ModelMode.LOCAL,
                      client=FakeModel(digest_error=ModelCallError("connection refused")))
    assert stored.status is DiagnosisStatus.NEEDS_REVIEW
    assert stored.model_digest is None
    assert "connection refused" in stored.reasons[0]


def test_removing_the_reproducing_fixture_lowers_the_evidence_level(backlog) -> None:
    conn, summary, _ = backlog
    built = the_packet(conn, summary)
    claim = answer(built, evidence_level="reproduced_in_fixture")
    with_failure = diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel(claim))
    assert with_failure.output.evidence_level.value == "reproduced_in_fixture"

    # The same incident judged a minute later with a handler whose fixture checks pass: the key evidence
    # is gone, so the same claim is refused and the repaired answer settles for a weaker label. (A new
    # cutoff, because a packet for one incident and cutoff is fixed once stored.)
    with storage.write_transaction(conn):
        storage.advance_clock(conn, summary.run_id, summary.cutoff_ms + 60_000)
    healthy = merchant.compose(version="deploy_healthy_reference")
    weaker = {**claim, "evidence_level": "supported"}
    without_failure = diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel(claim, weaker), deployed=healthy)
    assert without_failure.output.evidence_level.value == "supported"
    assert any("fixture" in reason for reason in without_failure.reasons)


def test_the_real_rules_baseline_weakens_its_label_when_the_fixture_stops_reproducing(backlog) -> None:
    conn, summary, _ = backlog
    reproducing = diagnose(conn, summary, ModelMode.RULES)
    assert reproducing.status is DiagnosisStatus.VALIDATED
    assert reproducing.rules_version != "rules-stub"
    assert reproducing.output.evidence_level.value == "reproduced_in_fixture"
    with storage.write_transaction(conn):
        storage.advance_clock(conn, summary.run_id, summary.cutoff_ms + 60_000)
    healthy = diagnose(conn, summary, ModelMode.RULES, deployed=merchant.compose(version="deploy_healthy_reference"))
    assert healthy.status is DiagnosisStatus.VALIDATED
    assert (healthy.output.evidence_level.value != "reproduced_in_fixture"
            or healthy.output.proposed_operation.value == "escalate")


def test_the_evidence_level_is_a_label_never_a_percentage(backlog, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, summary, _ = backlog
    install_stub_rules(monkeypatch)
    stored = diagnose(conn, summary, ModelMode.RULES)
    level = stored.output.evidence_level.value
    assert level in {"insufficient", "supported", "reproduced_in_fixture"}
    row = conn.execute("SELECT output FROM diagnoses WHERE diagnosis_id = ?", (stored.diagnosis_id,)).fetchone()
    assert "%" not in row["output"]
    assert "probability" not in row["output"]
    assert "confidence" not in row["output"]


def test_diagnosis_refuses_to_start_inside_a_transaction(backlog) -> None:
    conn, summary, _ = backlog
    client = FakeModel()
    with storage.read_transaction(conn), pytest.raises(ContractError, match="outside any transaction"):
        diagnose(conn, summary, ModelMode.LOCAL, client=client)
