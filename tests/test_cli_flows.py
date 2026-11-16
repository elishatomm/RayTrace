"""PLAN M2/M3 acceptance through the CLI, against temporary data directories only.

Most tests swap in a small stand-in rules baseline (``install_stub_rules``) so that they exercise CLI
mechanics whatever the real baseline decides; ``test_each_demo_flow_runs_on_the_real_rules_baseline``
runs the three flows on the real one. Local model calls use scripted fakes, so nothing here needs Ollama.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_diagnose_incident import FakeModel, install_stub_rules

from recovery_workbench import cli, diagnosis, packet, selective, storage
from recovery_workbench.contracts import DiagnosisOutput, EvidencePacket
from recovery_workbench.simulator import merchant, runtime

OPERATOR = "op_cli_tester"


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str, expect: int = 0) -> object:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    assert code == expect, captured.err
    return json.loads(captured.out) if expect == 0 else json.loads(captured.err)


def counts(data_dir: Path, run_id: str) -> tuple[int, int]:
    conn = storage.connect(data_dir / cli.DATABASE_FILE)
    try:
        return len(storage.list_fulfilments(conn, run_id=run_id)), len(
            [row for row in storage.list_inbox_rows(conn, run_id=run_id) if row.processing_status.value == "failed"]
        )
    finally:
        conn.close()


def payments_of(data_dir: Path, run_id: str, order_ids: set[str]) -> list[str]:
    conn = storage.connect(data_dir / cli.DATABASE_FILE)
    try:
        return [item.payment_id for item in storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=None)
                if item.order_id in order_ids]
    finally:
        conn.close()


def fulfilled_order_ids(data_dir: Path, run_id: str) -> set[str]:
    conn = storage.connect(data_dir / cli.DATABASE_FILE)
    try:
        return {item.order_id for item in storage.list_fulfilments(conn, run_id=run_id)}
    finally:
        conn.close()


def listed_orders(lines: list[str], step_title: str, pattern: str) -> list[str]:
    """Order IDs printed under the demo step whose title starts with ``step_title``."""
    start = next(index for index, line in enumerate(lines) if re.match(rf"\[\d+\] {re.escape(step_title)}", line))
    found = []
    for line in lines[start + 1:]:
        if line.startswith("["):
            break
        match = re.match(pattern, line)
        if match:
            found.append(match.group(1))
    return found


def reprocess_fulfilments(data_dir: Path, run_id: str) -> list[object]:
    conn = storage.connect(data_dir / cli.DATABASE_FILE)
    try:
        return [item for item in storage.list_fulfilments(conn, run_id=run_id) if item.via == "reprocess"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- Flow A, one command at a time


def test_flow_a_end_to_end_through_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    install_stub_rules(monkeypatch)
    data = ["--data-dir", str(tmp_path)]
    seeded = run_cli(capsys, "seed", "--scenario", "worker_backlog", "--seed", "101", *data)
    run = seeded["run_id"]
    assert run_cli(capsys, "status", "--run-id", run, *data)["backlog"]["failed"] > 0

    diagnosed = run_cli(capsys, "diagnose", "--run-id", run, "--mode", "rules", *data)
    assert diagnosed["mode"] == "rules"
    assert diagnosed["model_id"] is None
    assert diagnosed["output"]["proposed_operation"] == "reprocess_inbox"
    assert len(run_cli(capsys, "fixes")) == len(merchant.CORRECTED_VARIANT_IDS)

    wrong = run_cli(capsys, "validate-fix", "--run-id", run, "--fix-id", "config@5",
                    "--diagnosis-id", diagnosed["diagnosis_id"], "--operator", OPERATOR, *data)
    assert wrong["reproduced"]
    assert not wrong["passed"]
    refused = run_cli(capsys, "plan", "--run-id", run, "--fix-validation-id", wrong["validation_id"], *data, expect=2)
    assert "did not pass" in refused["detail"]

    right = run_cli(capsys, "validate-fix", "--run-id", run, "--fix-id", "payload@5",
                    "--diagnosis-id", diagnosed["diagnosis_id"], "--operator", OPERATOR, *data)
    assert right["reproduced"]
    assert right["passed"]
    draft = run_cli(capsys, "plan", "--run-id", run, "--fix-validation-id", right["validation_id"], *data)
    assert {"cancelled", "multiple_captures", "amount_mismatch", "conflicting_evidence",
            "stale_snapshot"} <= set(draft["exclusions_by_reason"])
    assert "late_authorization" not in draft["exclusions_by_reason"]  # policy-v3: late authorisation is planned

    # Orders held back only by stale provider state become targets after a read-only refresh and a new plan.
    blocked_otherwise = {order for reason, orders in draft["exclusions_by_reason"].items() if reason != "stale_snapshot"
                         for order in orders}
    stale_only = set(draft["exclusions_by_reason"]["stale_snapshot"]) - blocked_otherwise
    assert stale_only, "the run should hold orders blocked only by stale provider state"
    payments = payments_of(tmp_path, run, stale_only)
    refreshed = run_cli(capsys, "refresh", "--run-id", run, *[arg for item in payments for arg in ("--payment-id", item)],
                        *data)
    assert sorted(refreshed["refreshed"]) == sorted(payments)
    plan = run_cli(capsys, "plan", "--run-id", run, "--fix-validation-id", right["validation_id"], *data)
    assert {target["id"] for target in plan["targets"]} == {target["id"] for target in draft["targets"]} | stale_only
    assert "stale_snapshot" not in plan["exclusions_by_reason"]
    assert plan["target_count"] == len(plan["targets"]) > 50
    assert plan["target_value_paise"] == sum(target["amount_paise"] for target in plan["targets"])
    assert plan["dry_runs_passed"] == plan["target_count"]

    refused_approval = run_cli(capsys, "approve", "--run-id", run, "--plan-id", plan["plan_id"], "--plan-hash", "0" * 64,
                               "--operator", OPERATOR, *data, expect=3)
    assert refused_approval["error"] == "StaleStateError"
    run_cli(capsys, "approve", "--run-id", run, "--plan-id", plan["plan_id"], "--plan-hash", plan["plan_hash"],
            "--operator", OPERATOR, *data)
    fulfilled_before, _ = counts(tmp_path, run)
    first = run_cli(capsys, "execute", "--run-id", run, "--plan-id", plan["plan_id"], *data)
    assert first["status"] == "succeeded"
    assert len(first["fulfilled_order_ids"]) == plan["target_count"]
    assert first["restored_processing_value_paise"] == plan["target_value_paise"]
    assert first["additional_captured_paise"] == 0
    fulfilled_after, _ = counts(tmp_path, run)
    assert fulfilled_after == fulfilled_before + plan["target_count"]

    again = run_cli(capsys, "execute", "--run-id", run, "--plan-id", plan["plan_id"], *data)
    assert again["replayed"]
    assert again["action_id"] == first["action_id"]
    assert counts(tmp_path, run)[0] == fulfilled_after

    output = tmp_path / "report.json"
    run_cli(capsys, "report", "--run-id", run, "--output", str(output), *data)
    exported = json.loads(output.read_text())
    assert exported["metadata"]["seed"] == 101
    assert exported["metadata"]["generator_version"]
    assert set(exported["clocks"]) == {"replay_clock_ms", "exported_wall_ms"}
    assert exported["diagnoses"][0]["mode_label"].startswith("rules baseline")
    assert exported["outcome"]["restored_processing_value_paise"] == plan["target_value_paise"]
    assert exported["outcome"]["additional_captured_paise"] == 0
    assert exported["evidence"][0]["items"]
    assert [item["status"] for item in exported["actions"]] == ["succeeded"]
    assert exported["fix_validations"][0]["reproduced"] is True
    assert [item["plan_id"] for item in exported["plans"]] == [draft["plan_id"], plan["plan_id"]]


# --------------------------------------------------------------------------- the demo


def seeded_run_id(lines: list[str]) -> str:
    """The run ID the demo's seed step printed, so no test depends on how run IDs are named."""
    found = re.match(r"\s+run (\S+): ", lines[lines.index("[1] seed") + 1])
    assert found is not None, lines[:3]
    return found.group(1)


def demo_lines(tmp_path: Path, scenario: str, client: FakeModel | None = None, mode: str = "rules") -> list[str]:
    lines: list[str] = []
    from recovery_workbench.contracts import ModelMode

    assert cli.demo(scenario=scenario, seed=101, mode=ModelMode(mode), data_dir=tmp_path,
                    recordings_dir=tmp_path / "recordings", write=lines.append, client=client) == 0
    return lines


@pytest.mark.parametrize(("scenario", "expected"), [
    ("worker_backlog", ("hypothesis: merchant_worker_regression", "reproduced on the deployed handler: yes",
                        "replayed: True", "new capture: INR 0.00")),
    ("method_degradation", ("hypothesis: provider_method_degradation", "affected method: upi", "no reprocessing")),
    ("late_authorization", ("hypothesis: ambiguous_payment_state", "refresh (read-only provider snapshot)",
                            "payments first failed and later authorised", "no reprocessing")),
])
def test_each_demo_flow_runs_on_the_real_rules_baseline(tmp_path: Path, scenario: str, expected: tuple[str, ...]) -> None:
    lines = demo_lines(tmp_path, scenario)
    text = "\n".join(lines)
    assert "    mode: rules baseline rules-v3 (no model)" in lines  # Q28: rules mode answers rules-v3
    assert "rules-stub" not in text
    assert "status: validated" in text
    for fragment in expected:
        assert fragment in text, fragment
    if scenario != "worker_backlog":
        assert any(re.fullmatch(r"\[\d+\] escalate", line) for line in lines)
        assert "validate-fix" not in text


def test_the_diagnose_command_answers_rules_v3_in_rules_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = ["--data-dir", str(tmp_path)]
    run = run_cli(capsys, "seed", "--scenario", "worker_backlog", "--seed", "101", *data)["run_id"]
    diagnosed = run_cli(capsys, "diagnose", "--run-id", run, "--mode", "rules", *data)
    conn = storage.connect(tmp_path / cli.DATABASE_FILE)
    try:
        clock = storage.require_run(conn, run).clock_ms
        built = packet.build_packet(conn, run_id=run, incident_id=diagnosed["incident_id"], cutoff_ms=clock,
                                    deployed=runtime.active_merchant(conn, run_id=run, at_ms=clock), persist=False)
    finally:
        conn.close()
    assert built.packet_hash() == diagnosed["packet_hash"]
    assert diagnosed["output"] == selective.rules_v3(built).model_dump(mode="json")
    assert (diagnosed["rules_version"], diagnosed["mode_label"]) == ("rules-v3", "rules baseline rules-v3 (no model)")


def test_the_demo_runs_flow_a_and_shows_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub_rules(monkeypatch)
    lines = demo_lines(tmp_path, "worker_backlog")
    text = "\n".join(lines)
    for expected in ("[1] seed", "status", "diagnose", "fixes", "validate-fix payload@5", "plan plan_",
                     "refresh (read-only provider snapshot)", "plan again plan_", "approve", "execute", "execute again",
                     "replayed: True", "new capture: INR 0.00", "report"):
        assert expected in text, expected
    assert "(a label, not a probability)" in text
    assert "pending captured value (metric)" in text
    assert "plan value definition" in text
    assert "late_authorization" not in text
    assert "held back" not in text
    assert "holds back" not in text
    planned = listed_orders(lines, "late authorisation:", r"\s+(\S+): late-authorised payments \['pay_[^]]*\]; in the plan")
    assert planned
    assert re.search(rf"late authorisation: {len(planned)} late-authorised orders are in the plan", text)
    assert "the capture is real money already collected" in text
    assert "new capture stays 0" in text
    reprocessed = {item.order_id for item in reprocess_fulfilments(tmp_path, seeded_run_id(lines))}
    assert set(planned) <= reprocessed  # each planned late-authorised order was fulfilled by the reprocessing action
    newly = re.search(r"newly eligible after the refresh: (\d+) of the (\d+) stale-only orders", text)
    assert newly is not None
    assert newly.group(1) == newly.group(2) != "0"
    first_plan, second_plan = (re.search(rf"^\[\d+\] {title} (plan_\w+)", text, re.M) for title in ("plan", "plan again"))
    assert first_plan
    assert second_plan
    assert f"approved plan {second_plan.group(1)}" in text
    assert (tmp_path / "reports" / f"{seeded_run_id(lines)}.json").is_file()


def test_provider_degradation_is_escalated_not_reprocessed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub_rules(monkeypatch)
    lines = demo_lines(tmp_path, "method_degradation")
    text = "\n".join(lines)
    assert "hypothesis: provider_method_degradation" in text
    assert "affected method: upi" in text
    assert any(re.fullmatch(r"\[\d+\] escalate", line) for line in lines), "no escalate step was taken"
    assert "validate-fix" not in text
    assert "plan plan_" not in text
    assert "no reprocessing" in text
    assert ("why not reprocess: the diagnosis is provider_method_degradation, and the operation policy permits "
            "only escalate for it") in text
    assert not reprocess_fulfilments(tmp_path, seeded_run_id(lines))


def test_a_wrong_reprocess_proposal_still_reprocesses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = install_stub_rules(monkeypatch)

    def always_blame_the_worker(built: EvidencePacket) -> DiagnosisOutput:
        return DiagnosisOutput(
            hypothesis="merchant_worker_regression", faulting_change_id="unknown", affected_method="none",
            evidence_level="insufficient", supporting_evidence_ids=("ev_trigger",), contradicting_evidence_ids=(),
            missing_checks=(), next_check="validate_corrected_handler", proposed_operation="reprocess_inbox",
            rationale="A deliberately wrong answer for this test.",
        )

    module.rules_v3 = always_blame_the_worker  # type: ignore[attr-defined]
    # val-5 refuses a worker regression in a packet with no worker_error item, and this run's worker is healthy, so
    # the demo stops at the diagnosis with exit code 2. test_plans.py checks the fix and plan gates on such a run.
    code = cli.main(["demo", "--scenario", "method_degradation", "--seed", "101", "--model-mode", "rules",
                     "--data-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == cli.EXIT_USAGE_ERROR
    assert "no worker_error item" in json.loads(captured.err)["detail"]
    assert not reprocess_fulfilments(tmp_path, seeded_run_id(captured.out.splitlines()))


def test_flow_c_states_what_the_late_authorisations_mean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub_rules(monkeypatch)
    lines = demo_lines(tmp_path, "late_authorization")
    text = "\n".join(lines)
    assert "hypothesis: ambiguous_payment_state" in text
    assert "refresh (read-only provider snapshot)" in text
    facts = re.search(r"(\d+) payments first failed and later authorised \((\d+) now captured\); (\d+) of their (\d+) "
                      r"orders were already fulfilled", text)
    assert facts is not None
    assert int(facts.group(1)) > 0
    if int(facts.group(2)) > 0:
        assert "would have collected twice" in text
    # Unfulfilled late-authorised orders are listed with their order's own exclusions, never as "held back".
    unfulfilled = listed_orders(lines, "late authorisations", r"\s+not fulfilled yet: (\S+),")
    assert not set(unfulfilled) & fulfilled_order_ids(tmp_path, seeded_run_id(lines))
    assert len(unfulfilled) == min(int(facts.group(4)) - int(facts.group(3)), 8)
    assert "held back" not in text
    assert "block automatic recovery" not in text
    # Flow C still escalates: its hypothesis does not permit reprocessing, whatever the late authorisations say.
    assert ("why not reprocess: the diagnosis is ambiguous_payment_state, and the operation policy permits only "
            "escalate and refresh_provider_state for it; reprocessing is not among them") in text
    assert "no reprocessing" in text
    assert not reprocess_fulfilments(tmp_path, seeded_run_id(lines))


def test_malformed_model_output_stops_within_budget_through_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeModel("not json", "{still not")
    monkeypatch.setattr(diagnosis, "OllamaClient", lambda **_: fake)
    data = ["--data-dir", str(tmp_path)]
    run = run_cli(capsys, "seed", "--scenario", "worker_backlog", "--seed", "101", *data)["run_id"]
    diagnosed = run_cli(capsys, "diagnose", "--run-id", run, "--mode", "local", *data)
    assert diagnosed["status"] == "needs_review"
    assert diagnosed["output"] is None
    assert len(fake.requests) == 2
    assert any("not valid JSON" in reason for reason in diagnosed["reasons"])
    assert diagnosed["mode_label"] == "live local model fake-model:1b"


def test_a_needs_review_demo_takes_the_conservative_path(tmp_path: Path) -> None:
    lines = demo_lines(tmp_path, "worker_backlog", client=FakeModel("not json", "nor this"), mode="local")
    text = "\n".join(lines)
    assert "status: needs_review" in text
    assert "needs review because" in text
    assert any(re.fullmatch(r"\[\d+\] escalate", line) for line in lines)
    assert "validate-fix" not in text


def test_refresh_and_escalate_commands_change_no_business_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    install_stub_rules(monkeypatch)
    data = ["--data-dir", str(tmp_path)]
    run = run_cli(capsys, "seed", "--scenario", "late_authorization", "--seed", "101", *data)["run_id"]
    diagnosed = run_cli(capsys, "diagnose", "--run-id", run, "--mode", "rules", *data)
    before = counts(tmp_path, run)
    conn = storage.connect(tmp_path / cli.DATABASE_FILE)
    try:
        payment = storage.list_run_payment_attempts(conn, run_id=run, as_of_ms=None)[0].payment_id
    finally:
        conn.close()
    refreshed = run_cli(capsys, "refresh", "--run-id", run, "--payment-id", payment, *data)
    assert refreshed["refreshed"] == [payment] or refreshed["unknown_at_provider"] == [payment]
    escalated = run_cli(capsys, "escalate", "--run-id", run, "--diagnosis-id", diagnosed["diagnosis_id"],
                        "--operator", OPERATOR, *data)
    assert escalated["plan"]["operation"] == "escalate"
    assert escalated["evidence_needed"]
    assert counts(tmp_path, run) == before
    refused = run_cli(capsys, "escalate", "--run-id", run, "--diagnosis-id", diagnosed["diagnosis_id"],
                      "--operator", "qwen3:8b", *data, expect=2)
    assert refused["error"] == "ScopeError"


@pytest.mark.ollama
def test_the_live_demo_keeps_its_guarantees_whatever_the_model_says(tmp_path: Path) -> None:
    from test_diagnosis import _ollama_has_model

    if not _ollama_has_model():
        pytest.skip(f"local Ollama with {diagnosis.DEFAULT_MODEL} is not available")
    lines = demo_lines(tmp_path, "worker_backlog", mode="local")
    text = "\n".join(lines)
    assert "mode: live local model" in text
    if "proposed operation: reprocess_inbox" in text and "plan plan_" in text:
        assert "replayed: True" in text
        assert "new capture: INR 0.00" in text
    assert (tmp_path / "reports" / f"{seeded_run_id(lines)}.json").is_file()
