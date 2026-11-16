"""Flow A across both tracks: the planner's targets equal the independently derived oracle.

The oracle comes from latent truth plus the simulator's own log; the plan comes from Agent B's
planner reading only the workbench database. Neither side is computed from the other.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from recovery_workbench import diagnosis_store, fixes, plans, refresh, settings, storage
from recovery_workbench.contracts import DiagnosisOutput
from recovery_workbench.simulator import driver, merchant, runtime, scenarios
from recovery_workbench.simulator.oracle import ELIGIBLE
from recovery_workbench.simulator.provider import SimulatedProviderAdapter
from recovery_workbench.simulator.world import World, generate_world

TRUTH = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "manifests" / "merchant_variant_truth.json").read_text()
)
SETTINGS = settings.load_settings()
STALE_AFTER_MS = SETTINGS.stale_after_seconds * 1_000
OPERATOR = "op_integration"


@dataclass
class Planned:
    conn: sqlite3.Connection
    world: World
    oracle: dict
    provider_records: Path
    deployed: merchant.DeployedMerchant
    validation: fixes.FixValidation
    preview: plans.PlanPreview

    def rebuild(self) -> plans.PlanPreview:
        return build_plan(self.conn, self.world, self.deployed, self.validation)

    def oracle_reasons(self, order_id: str) -> list[str]:
        return list(self.oracle["orders"][order_id]["reasons"])


def build_plan(conn: sqlite3.Connection, world: World, deployed: merchant.DeployedMerchant,
               validation: fixes.FixValidation) -> plans.PlanPreview:
    return plans.build_reprocess_plan(
        conn, run_id=world.run_id, fix_validation_id=validation.validation_id, deployed=deployed,
        now_ms=world.cutoff_ms, ttl_ms=SETTINGS.plan_ttl_seconds * 1_000, stale_after_ms=STALE_AFTER_MS,
    )


@pytest.fixture(scope="module", params=[7, 101, 202])
def planned(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    directory = tmp_path_factory.mktemp(f"flow_a_{request.param}")
    conn = storage.connect(directory / "workbench.sqlite")
    world = generate_world(scenarios.WORKER_BACKLOG, request.param)
    summary = driver.seed_world(conn, world, oracle_dir=directory / "oracle", stale_after_ms=STALE_AFTER_MS)
    oracle = json.loads(Path(str(summary.oracle_path)).read_text(encoding="utf-8"))
    [incident] = storage.list_incidents(conn, run_id=world.run_id)
    diagnosis = diagnosis_store.record_rules_diagnosis(
        conn, run_id=world.run_id, incident_id=incident.incident_id, packet_hash="0" * 64,
        output=DiagnosisOutput(
            hypothesis="merchant_worker_regression", faulting_change_id=oracle["faulting_deploy_id"],
            affected_method="none", evidence_level="supported", supporting_evidence_ids=(),
            contradicting_evidence_ids=(), missing_checks=(), next_check="validate_corrected_handler",
            proposed_operation="reprocess_inbox", rationale="Stand-in diagnosis for the integration test.",
        ),
        rules_version="integration-test",
    )
    deployed = runtime.active_merchant(conn, run_id=world.run_id, at_ms=world.cutoff_ms)
    fix_id = TRUTH["variants"][oracle["faulting_variant"]]["repaired_by"][0]
    validation = fixes.validate_fix(
        conn, run_id=world.run_id, deployed=deployed, fix_id=fix_id, diagnosis_id=diagnosis.diagnosis_id,
        operator_id=OPERATOR, now_ms=world.cutoff_ms,
    )
    assert validation.passed
    yield Planned(conn, world, oracle, Path(str(summary.provider_records_path)), deployed, validation,
                  build_plan(conn, world, deployed, validation))
    conn.close()


def test_plan_targets_equal_the_oracle_eligible_set(planned: Planned):
    eligible = {order_id for order_id, order in planned.oracle["orders"].items() if order["expected"] == ELIGIBLE}
    assert eligible
    assert set(planned.preview.plan.target_ids) == eligible
    amounts = {order.order_id: order.amount_paise for order in planned.world.orders}
    assert planned.preview.target_value_paise == sum(amounts[order_id] for order_id in eligible)


def test_every_excluded_candidate_carries_the_oracle_reason(planned: Planned):
    excluded = {item.order_id: set(item.reasons) for item in planned.preview.excluded}
    assert excluded
    for order_id, reasons in excluded.items():
        expected = planned.oracle["orders"][order_id]["expected"]
        assert expected != ELIGIBLE, order_id
        assert expected in reasons, (order_id, expected, sorted(reasons))
    candidates = set(excluded) | set(planned.preview.plan.target_ids)
    assert len(candidates) == planned.preview.candidate_orders
    for order_id, order in planned.oracle["orders"].items():
        if order["expected"] not in (ELIGIBLE, "already_fulfilled", "not_captured", "unresolved_attempt",
                                     "capture_not_durably_received"):
            assert order_id in excluded, (order_id, order["expected"])  # captured and received, so a candidate


def test_stale_orders_become_eligible_after_a_provider_refresh(planned: Planned):
    stale_only = {order_id for order_id, order in planned.oracle["orders"].items() if order["reasons"] == ["stale_snapshot"]}
    assert stale_only, "the run should hold orders blocked only by stale provider state"
    assert not stale_only & set(planned.preview.plan.target_ids)
    payment_ids = [attempt.payment_id for attempt in planned.world.attempts if attempt.order_id in stale_only]
    summary = refresh.refresh_provider_state(
        planned.conn, run_id=planned.world.run_id, payment_ids=payment_ids,
        adapter=SimulatedProviderAdapter.from_records(planned.provider_records), now_ms=planned.world.cutoff_ms,
    )
    assert set(summary.refreshed) == set(payment_ids)
    rebuilt = planned.rebuild()
    assert set(rebuilt.plan.target_ids) == set(planned.preview.plan.target_ids) | stale_only
