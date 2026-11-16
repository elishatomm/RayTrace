"""CLI: seed and status against temporary data directories only."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from recovery_workbench import cli


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    data_dir = tmp_path_factory.mktemp("cli_data")
    output = subprocess.run(
        [sys.executable, "-m", "recovery_workbench.cli", "seed", "--scenario", "worker_backlog", "--seed", "101",
         "--data-dir", str(data_dir)],
        capture_output=True, text=True, check=True, timeout=300,
    )
    return data_dir, json.loads(output.stdout)


def test_seed_prints_a_summary_and_writes_database_and_oracle(seeded):
    data_dir, summary = seeded
    assert summary["run_id"]  # read from the seed output; the naming of run IDs is the generator's business
    assert summary["cutoff_ms"] > summary["start_ms"]
    assert summary["counts"]["orders"] >= 50
    assert summary["incident_ids"] == ["inc_0001"]
    assert Path(summary["database"]) == data_dir / "workbench.sqlite"
    assert Path(summary["oracle_path"]).parent == data_dir / "oracle"
    assert Path(summary["oracle_path"]).is_file()


def test_status_reports_measures_with_denominators_and_freshness(seeded, capsys: pytest.CaptureFixture[str]):
    data_dir, summary = seeded
    assert cli.main(["status", "--run-id", summary["run_id"], "--data-dir", str(data_dir)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run"]["as_of_ms"] == summary["cutoff_ms"]
    current = {item["cohort"]: item for item in report["provider"]["current"]}
    assert set(current) == {"upi", "card", "netbanking", "all"}
    assert {"numerator", "denominator", "value"} <= set(current["all"]["authorization"])
    assert current["all"]["freshness_ms"] is not None
    assert report["merchant"]["completion"]["denominator"] > 0
    assert report["merchant"]["pending_captured_value_paise"] > 0
    assert report["backlog"]["failed"] > 0
    assert any("event_parse_error" in key for key in report["worker_failures"])
    assert len(report["deployments"]) == 3
    assert [incident["incident_id"] for incident in report["incidents"]] == ["inc_0001"]
    assert {item["detector"] for item in report["detectors_now"]} >= {"orders_not_completing", "merchant_processing_gap"}


def test_status_as_of_an_earlier_time_sees_less(seeded, capsys: pytest.CaptureFixture[str]):
    data_dir, summary = seeded
    early = summary["start_ms"] + 30 * 60_000
    assert cli.main(["status", "--run-id", summary["run_id"], "--data-dir", str(data_dir), "--as-of-ms", str(early)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["incidents"] == []
    assert len(report["deployments"]) < 3
    assert all(d["deployed_at_ms"] <= early for d in report["deployments"])
    assert report["merchant"]["logical_events"] < summary["counts"]["webhook_inbox"]


def test_seeding_the_same_run_twice_is_refused(seeded, capsys: pytest.CaptureFixture[str]):
    data_dir, _ = seeded
    assert cli.main(["seed", "--scenario", "worker_backlog", "--seed", "101", "--data-dir", str(data_dir)]) == 2
    assert "already exists" in json.loads(capsys.readouterr().err)["detail"]


def test_status_without_a_database_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["status", "--run-id", "run_missing", "--data-dir", str(tmp_path)]) == 2
    assert "run the seed command first" in json.loads(capsys.readouterr().err)["detail"]
    assert not (tmp_path / "workbench.sqlite").exists()


def test_status_of_an_unknown_run_is_a_scope_error(seeded, capsys: pytest.CaptureFixture[str]):
    data_dir, _ = seeded
    assert cli.main(["status", "--run-id", "run_missing", "--data-dir", str(data_dir)]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "ScopeError"


def test_unknown_scenarios_are_rejected_by_the_parser(tmp_path: Path):
    with pytest.raises(SystemExit) as raised:
        cli.main(["seed", "--scenario", "nonexistent", "--seed", "1", "--data-dir", str(tmp_path)])
    assert raised.value.code == 2


def test_the_data_dir_defaults_to_the_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "from_settings"))
    assert cli.main(["seed", "--scenario", "late_authorization", "--seed", "3"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert Path(summary["database"]) == tmp_path / "from_settings" / "workbench.sqlite"


def test_fixes_lists_the_same_allowlist_for_everyone(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["fixes"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["fix_id"] for item in listed] == ["config@5", "config@6", "payload@5", "payload@6", "receiver@4"]


def test_diagnose_without_a_rules_baseline_is_refused_with_a_clear_message(
    seeded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    data_dir, summary = seeded
    monkeypatch.setitem(sys.modules, "recovery_workbench.selective", None)
    assert cli.main(["diagnose", "--run-id", summary["run_id"], "--mode", "rules", "--data-dir", str(data_dir)]) == 2
    assert "rules baseline" in json.loads(capsys.readouterr().err)["detail"]


def test_recovery_commands_refuse_unknown_runs_and_missing_databases(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["plan", "--run-id", "run_missing", "--fix-validation-id", "fixval_x", "--data-dir", str(tmp_path)]) == 2
    assert "run the seed command first" in json.loads(capsys.readouterr().err)["detail"]


def test_operator_actions_use_replay_time_that_moves_forward(seeded, capsys: pytest.CaptureFixture[str]):
    data_dir, summary = seeded
    from contextlib import closing

    from recovery_workbench import storage

    with closing(storage.connect(data_dir / "workbench.sqlite")) as conn:
        before = storage.require_run(conn, summary["run_id"]).clock_ms
        assert cli.next_operator_time(conn, summary["run_id"]) == before + cli.OPERATOR_STEP_MS
