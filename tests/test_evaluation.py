"""M4 evaluation: scoring, the runner on development runs (rules only), recovery, freezing and summaries."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest
from test_selective import packet_of, two_worker_deploys_then_failure

from recovery_workbench import diagnosis_store, rules, selective, storage
from recovery_workbench.contracts import DiagnosisOutput
from recovery_workbench.evaluation import freeze, generate, recovery, run, summarize
from recovery_workbench.simulator import families

MANIFEST = families.load_manifest()


def output(hypothesis: str, operation: str, change: str = "none", method: str = "none") -> DiagnosisOutput:
    return DiagnosisOutput(hypothesis=hypothesis, faulting_change_id=change, affected_method=method,
                           evidence_level="supported", supporting_evidence_ids=(), contradicting_evidence_ids=(),
                           missing_checks=(), next_check="none", proposed_operation=operation, rationale="test")


def case(family: str, expected: dict, params: dict | None = None) -> run.Case:
    return run.Case(run_id="run_0000000000", seed=1, family=family, variant="v", params=params or {}, cutoff_ms=0,
                    expected=expected, oracle={}, provider_records=Path("unused"))


D1 = {"hypothesis": "merchant_worker_regression", "faulting_change_id": "deploy_fault", "affected_method": "none",
      "acceptable_operations": ["reprocess_inbox"]}
D2 = {"hypothesis": "provider_method_degradation", "faulting_change_id": "none", "affected_method": "card",
      "acceptable_operations": ["escalate"]}
D4 = {"hypothesis": "ambiguous_payment_state", "faulting_change_id": "none", "affected_method": "none",
      "acceptable_operations": ["escalate", "refresh_provider_state"]}
D5 = {"hypothesis": "no_incident", "faulting_change_id": "none", "affected_method": "none",
      "acceptable_operations": ["no_action"]}


def test_a_worker_regression_needs_the_right_deploy_and_action():
    assert run.score(case("D1", D1), output("merchant_worker_regression", "reprocess_inbox", "deploy_fault"))["outcome_correct"]
    blamed_wrong = run.score(case("D1", D1), output("merchant_worker_regression", "reprocess_inbox", "deploy_innocent"))
    assert (blamed_wrong["outcome_correct"], blamed_wrong["change_correct"]) == (False, False)
    assert run.score(case("D1", D1), None) == {
        "answered": False, "hypothesis_correct": False, "change_correct": False, "method_correct": None,
        "operation_acceptable": False, "abstained": False, "outcome_correct": False, "wrong_recovery_proposal": False}


def test_method_and_abstention_rules():
    assert run.score(case("D2", D2), output("provider_method_degradation", "escalate", method="card"))["outcome_correct"]
    assert not run.score(case("D2", D2), output("provider_method_degradation", "escalate", method="upi"))["outcome_correct"]
    assert run.score(case("D4", D4), output("unknown", "escalate"))["outcome_correct"]  # a correct abstention
    assert run.score(case("D5", D5), output("unknown", "no_action"))["outcome_correct"]
    assert not run.score(case("D5", D5), output("unknown", "escalate"))["outcome_correct"]
    wrong = run.score(case("D2", D2), output("merchant_worker_regression", "reprocess_inbox", "deploy_x"))
    assert wrong["wrong_recovery_proposal"]
    assert not wrong["outcome_correct"]


def test_d6_never_rewards_blaming_the_innocent_deploy():
    d6 = {**D5, "faulting_change_id": "none"}
    assert run.score(case("D6", d6), output("no_incident", "no_action"))["outcome_correct"]
    assert not run.score(case("D6", d6), output("merchant_worker_regression", "reprocess_inbox", "deploy_innocent"))["outcome_correct"]


def test_the_pre_registered_freeze_rule():
    better = {"outcome_correct": 14, "unsafe_proposals": 3, "warm_median_latency_ms": 9_000}
    worse = {"outcome_correct": 13, "unsafe_proposals": 0, "warm_median_latency_ms": 1_000}
    assert freeze.choose(better, worse) == "off"
    assert freeze.choose(worse, better) == "on"
    safer = {**better, "unsafe_proposals": 1}
    assert freeze.choose(better, safer) == "on"
    faster = {**better, "warm_median_latency_ms": 5_000}
    assert freeze.choose(better, faster) == "on"
    assert freeze.choose(better, better) == "off"


@pytest.fixture(scope="module")
def dev_subset(tmp_path_factory: pytest.TempPathFactory):
    """Three real development runs (worker regression, method drop, no incident) generated and evaluated, rules only."""
    root = tmp_path_factory.mktemp("evaluation")
    keys = {"d1_dev_1", "d2_dev_2", "d5_dev_1"}
    manifest_path = root / "manifest.json"
    subset = {**MANIFEST.model_dump(mode="json"), "runs": [r.model_dump(mode="json") for r in MANIFEST.runs if r.run_key in keys]}
    manifest_path.write_text(json.dumps(subset))
    assert generate.main(["--split", "dev", "--data-dir", str(root / "data"), "--manifest", str(manifest_path)]) == 0
    config = run.RunnerConfig(split="dev", data_dir=root / "data", systems=("rules",), model="unused", think=False,
                              output_dir=root / "out", label="dev_rules", manifest_path=manifest_path)
    document = run.evaluate(config)
    return root, config, document


def test_the_runner_scores_rules_and_recovers_the_worker_regression(dev_subset):
    root, config, document = dev_subset
    assert {case["family"]: case["rules"]["score"]["outcome_correct"] for case in document["cases"]} == {
        "D1": True, "D2": True, "D5": True}
    [entry] = json.loads((root / "out" / "runs" / "dev_rules" / "recovery_ledger.json").read_text())["entries"]
    assert (entry["status"], entry["matches_oracle"], entry["additional_captured_paise"]) == ("succeeded", True, 0)
    assert (entry["retry_replayed"], entry["duplicate_fulfilments"]) == (True, 0)
    assert entry["restored_processing_value_paise"] > 0
    for name in ("results.json", "environment.json", "data_manifest.json"):
        assert (root / "out" / "runs" / "dev_rules" / name).is_file()
    traces = (root / "out" / "traces" / "dev_rules.jsonl").read_text().splitlines()
    assert len(traces) == 3
    assert json.loads(traces[0])["packet"]["packet_version"] == "pkt-2"
    with pytest.raises(FileExistsError):
        run.evaluate(config)  # outputs are never overwritten


def test_the_evaluation_stays_on_rules_v2_while_rules_mode_answers_rules_v3(dev_subset):
    """Q28 switched the product's rules mode only. M4's harness answers, records and reports rules-v2."""
    root, _, _ = dev_subset
    environment = json.loads((root / "out" / "runs" / "dev_rules" / "environment.json").read_text())
    assert environment["versions"]["rules"] == rules.RULES_VERSION == "rules-v2"
    [copy] = (root / "data" / "recovery" / "dev_rules").glob("*_rules.sqlite")  # the D1 run's recovery
    with closing(storage.connect(copy)) as conn:
        recorded = diagnosis_store.list_diagnoses(conn, run_id=copy.name.removesuffix("_rules.sqlite"))
    assert [item.rules_version for item in recorded] == ["rules-v2"]
    disagreeing = packet_of(two_worker_deploys_then_failure())  # the two versions name different deployments here
    assert selective.rules_v3(disagreeing) != rules.diagnose(disagreeing)
    assert run.run_rules(disagreeing).output == rules.diagnose(disagreeing)


def test_a_wrong_blame_blocks_recovery_safely(dev_subset):
    root, _, document = dev_subset
    d1 = next(case for case in document["cases"] if case["family"] == "D1")
    oracle = json.loads(next((root / "data" / "oracle").glob(f"{d1['run_id']}.json")).read_text())
    innocent = oracle["harmless_deploy_ids"][0]
    entry = recovery.recover(
        data_db=root / "data" / "workbench.sqlite", work_dir=root / "work", run_id=d1["run_id"], system="rules",
        output=output("merchant_worker_regression", "reprocess_inbox", innocent), packet_hash="0" * 64,
        oracle=oracle, provider_records=root / "data" / "provider" / f"{d1['run_id']}.json",
    )
    assert entry["status"] == "blocked"


@pytest.mark.parametrize("eligibility", [
    {"as_of_ms": 0, "stale_after_ms": 3_600_000, "late_authorization_blocks_reprocessing": True},  # the pre-Q8 rule
    {"as_of_ms": 0, "stale_after_ms": 3_600_000},  # older still: no flag at all
    None,  # no eligibility section
])
def test_recovery_refuses_an_oracle_generated_before_decision_q8(tmp_path: Path, eligibility: dict | None):
    oracle = {"run_id": "run_0000000000", "orders": {}} | ({"eligibility": eligibility} if eligibility else {})
    proposal = output("merchant_worker_regression", "reprocess_inbox", "deploy_fault")
    with pytest.raises(recovery.StaleOracleError, match="Regenerate the split"):
        recovery.recover(data_db=tmp_path / "workbench.sqlite", work_dir=tmp_path / "work", run_id="run_0000000000",
                         system="rules", output=proposal, packet_hash="0" * 64, oracle=oracle,
                         provider_records=tmp_path / "provider.json")
    assert not (tmp_path / "work").exists()  # refused before any database copy


def test_the_runner_refuses_a_split_generated_before_decision_q8(tmp_path: Path):
    """The evaluation stops before any work: recovery would otherwise record the refusal as an executed error."""
    data_dir = tmp_path / "data"
    (data_dir / "oracle").mkdir(parents=True)
    dev_seed = next(entry.seed for entry in MANIFEST.runs if entry.split == "dev")
    stale = {"run_id": "run_0000000000", "expected": D1, "eligibility": {"late_authorization_blocks_reprocessing": True}}
    (data_dir / "oracle" / "run_0000000000.json").write_text(json.dumps(stale))
    (data_dir / "data_manifest.json").write_text(json.dumps({"split": "dev", "runs": [{
        "run_id": "run_0000000000", "seed": dev_seed, "cutoff_ms": 0, "oracle_file": "oracle/run_0000000000.json",
        "provider_records_file": "provider/run_0000000000.json"}]}))
    config = run.RunnerConfig(split="dev", data_dir=data_dir, systems=("rules",), model="unused", think=False,
                              output_dir=tmp_path / "out", label="stale_oracle")
    with pytest.raises(recovery.StaleOracleError, match="Regenerate the split"):
        run.evaluate(config)
    assert not (tmp_path / "out").exists()


def write_frozen_run(root: Path, label: str) -> None:
    """The files summarize reads: one frozen run (two D1 cases, both systems right), its traces and settings."""
    run_dir = root / "runs" / label
    run_dir.mkdir(parents=True)
    expected = {**D1, "faulting_change_id": "deploy_a"}
    model_output = output("merchant_worker_regression", "reprocess_inbox", "deploy_a").model_dump(mode="json")
    cases = [{"run_id": f"run_{i:010d}", "seed": i, "family": "D1", "variant": "top_frame_in_changed_file",
              "params": {"onset": "prompt", "categories": True}, "expected": expected, "order": i,
              "rules": {"status": "validated", "latency_ms": 0, "output": model_output, "unsafe_proposals": [], "repairs": 0,
                        "reasons": [], "score": run.score(case("D1", expected), DiagnosisOutput.model_validate(model_output))},
              "model": {"status": "validated", "latency_ms": 9000 + i, "output": model_output, "unsafe_proposals": [],
                        "repairs": 0, "reasons": [], "score": run.score(case("D1", expected), DiagnosisOutput.model_validate(model_output))}}
             for i in range(2)]
    (run_dir / "results.json").write_text(json.dumps({"cases": cases}))
    (run_dir / "recovery_ledger.json").write_text(json.dumps({"entries": []}))
    environment = {"model": {"model": "qwen3:8b", "model_digest": "a" * 64, "think": False, "prompt_version": "diag-p2"},
                   "versions": {"rules": "rules-v2", "validator": "val-4", "packet": "pkt-2"}}
    (run_dir / "environment.json").write_text(json.dumps(environment))
    figures = {"cases": 2, "outcome_correct": 2, "unsafe_proposals": 0, "needs_review": 0, "cold_latency_ms": 9000,
               "warm_median_latency_ms": 9001}
    (root / "frozen_settings.json").write_text(json.dumps({"selection_rule": freeze.SELECTION_RULE, "chosen_think": "off",
                                                          "dev_figures": {"off": figures, "on": figures}}))
    (root / "traces").mkdir()
    packet_items = [{"evidence_id": "ev_trigger", "title": "Detectors", "facts": {"fired": []}, "text": None}]
    (root / "traces" / f"{label}.jsonl").write_text("\n".join(
        json.dumps({"run_id": c["run_id"], "packet": {"items": packet_items},
                    "answers": {"model": {**c["model"], "output": {**model_output, "supporting_evidence_ids": ["ev_trigger"]}}}})
        for c in cases))
    (run_dir / "data_manifest.json").write_text(json.dumps({"split": "test", "runs": []}))


def test_the_summary_and_spot_check_are_built_from_run_outputs(tmp_path: Path):
    write_frozen_run(tmp_path, "test_frozen")
    text = summarize.build_summary(tmp_path, "test_frozen", None, [])
    assert "| **All** | 2 | 2/2 | 2/2 | **rules tie the model** |" in text
    assert "D1 top_frame_in_changed_file, onset prompt, categories on" in text
    sheet = summarize.build_spot_check(tmp_path, "test_frozen")
    assert sheet.count("| ev_trigger |") == 2
    assert "Supports the claim? (yes/no)" in sheet


SUMMARIZE_ARGS = ["--frozen", "test_frozen", "--dev", ""]


def test_summarize_writes_the_spot_check_sheet_while_it_is_still_blank(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    write_frozen_run(tmp_path, "test_frozen")
    sheet = tmp_path / "citation_spot_check.md"
    blank = summarize.build_spot_check(tmp_path, "test_frozen")
    for _ in range(2):  # first with no sheet, then over the unchanged blank one
        assert summarize.main(["--output-dir", str(tmp_path), *SUMMARIZE_ARGS]) == 0
        assert sheet.read_text(encoding="utf-8") == blank
        assert capsys.readouterr().out.strip() == f"wrote {tmp_path / 'summary.md'} and {sheet}"


def test_summarize_keeps_a_spot_check_sheet_the_owner_has_filled_in(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    write_frozen_run(tmp_path, "test_frozen")
    assert summarize.main(["--output-dir", str(tmp_path), *SUMMARIZE_ARGS]) == 0
    sheet = tmp_path / "citation_spot_check.md"
    filled = sheet.read_text(encoding="utf-8").replace("|  |", "| yes |", 1)
    sheet.write_text(filled, encoding="utf-8")
    (tmp_path / "summary.md").unlink()
    capsys.readouterr()
    assert summarize.main(["--output-dir", str(tmp_path), *SUMMARIZE_ARGS]) == 0
    assert sheet.read_text(encoding="utf-8") == filled  # the owner's judgement survives
    assert (tmp_path / "summary.md").is_file()  # the summary is still rewritten every time
    assert f"kept {sheet} unchanged because it has been filled in" in capsys.readouterr().out
