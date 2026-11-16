"""End-to-end recovery for a ``reprocess_inbox`` proposal, with the evaluator as the operator.

Runs on a private copy of the run's database, so each system starts from identical state.
The operator validates the allowlisted fix for the module that the BLAMED deploy changed: a
wrong blame fails validation and recovery stops there (``blocked``, a safe failure). Otherwise
the operator plans, refreshes orders held back only by stale provider state, re-plans, approves
and executes, then executes once more to show a retried request changes nothing. Everything
recorded here is an effect or a refusal, kept apart from what was proposed.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import diagnosis, diagnosis_store, executor, fixes, plans, refresh, rules, settings, storage
from recovery_workbench.contracts import CHOICE_NONE, CHOICE_UNKNOWN, DiagnosisOutput
from recovery_workbench.simulator import merchant, runtime
from recovery_workbench.simulator.oracle import ELIGIBLE
from recovery_workbench.simulator.provider import SimulatedProviderAdapter

OPERATOR = "op_evaluator"


class StaleOracleError(ValueError):
    """The oracle was generated under an eligibility rule the planner no longer applies."""


def require_current_oracle(oracle: Mapping[str, JsonValue]) -> None:
    """Refuse an oracle written before owner decision Q8, when late authorisation still blocked reprocessing.

    Its expected fulfilments leave out the late-authorised captures the planner now targets, so
    comparing against it would report planner errors that are really a stale oracle.
    """
    eligibility = oracle.get("eligibility")
    blocks = eligibility.get("late_authorization_blocks_reprocessing") if isinstance(eligibility, Mapping) else None
    if blocks is not False:
        raise StaleOracleError(
            f"the oracle for run {oracle.get('run_id')!r} was generated before owner decision Q8 "
            f"(late_authorization_blocks_reprocessing is {blocks!r}, expected false), so its expected fulfilments "
            "leave out late-authorised captures the planner now reprocesses. Regenerate the split into a fresh data "
            "directory: python -m recovery_workbench.evaluation.generate --split <dev|test> --data-dir <new dir>"
        )


def copy_database(source: Path, target: Path) -> None:
    """A consistent private copy through SQLite's backup API (WAL contents included)."""
    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(target)) as copy:
        origin.backup(copy)


def recover(
    *,
    data_db: Path,
    work_dir: Path,
    run_id: str,
    system: str,
    output: DiagnosisOutput,
    model_run: diagnosis.DiagnosisRun | None = None,
    model_id: str | None = None,
    model_digest: str | None = None,
    packet_hash: str,
    oracle: Mapping[str, JsonValue],
    provider_records: Path,
) -> dict[str, JsonValue]:
    """Act on one proposal; return the ledger entry (status, effects, comparison with the oracle)."""
    require_current_oracle(oracle)
    config = settings.load_settings()
    stale_after_ms = config.stale_after_seconds * 1_000
    work_dir.mkdir(parents=True, exist_ok=True)
    copy = work_dir / f"{run_id}_{system}.sqlite"
    copy.unlink(missing_ok=True)
    copy_database(data_db, copy)
    entry: dict[str, JsonValue] = {"run_id": run_id, "system": system, "blamed_change": output.faulting_change_id}
    with closing(storage.connect(copy)) as conn:
        now = storage.require_run(conn, run_id).clock_ms
        [incident] = storage.list_incidents(conn, run_id=run_id)
        if model_run is not None:
            stored = diagnosis_store.record_model_diagnosis(
                conn, run_id=run_id, incident_id=incident.incident_id, run=model_run, model_id=str(model_id),
                model_digest=model_digest,
            )
        else:
            stored = diagnosis_store.record_rules_diagnosis(
                conn, run_id=run_id, incident_id=incident.incident_id, packet_hash=packet_hash, output=output,
                rules_version=rules.RULES_VERSION,
            )
        deployed = runtime.active_merchant(conn, run_id=run_id, at_ms=now)
        fix_ids = _fixes_for_blamed_change(conn, run_id, output.faulting_change_id, now)
        entry["fix_candidates"] = list(fix_ids)
        if not fix_ids:
            return {**entry, "status": "blocked", "reason": "no allowlisted fix changes the module the blamed deploy changed"}
        validation = None
        for fix_id in fix_ids:
            attempt = fixes.validate_fix(conn, run_id=run_id, deployed=deployed, fix_id=fix_id,
                                         diagnosis_id=stored.diagnosis_id, operator_id=OPERATOR, now_ms=now)
            if attempt.passed:
                validation = attempt
                break
        if validation is None:
            return {**entry, "status": "blocked", "reason": "no candidate fix passed the fixture tests"}
        entry["fix_id"] = validation.fix_id
        preview = _plan(conn, run_id, validation, deployed, now, stale_after_ms)
        stale_only = sorted(item.order_id for item in preview.excluded if set(item.reasons) == {"stale_snapshot"})
        if stale_only:
            payment_ids = [a.payment_id for a in storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=now)
                           if a.order_id in stale_only]
            refresh.refresh_provider_state(conn, run_id=run_id, payment_ids=payment_ids,
                                           adapter=SimulatedProviderAdapter.from_records(provider_records), now_ms=now)
            preview = _plan(conn, run_id, validation, deployed, now, stale_after_ms)
        entry["refreshed_orders"] = stale_only
        if preview.target_count == 0:
            return {**entry, "status": "no_targets", "reason": "the plan found nothing eligible to reprocess"}
        executor.approve_plan(conn, run_id=run_id, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                              operator_id=OPERATOR, now_ms=now, ttl_ms=config.approval_ttl_seconds * 1_000)
        result = executor.execute_plan(conn, run_id=run_id, plan_id=preview.plan.plan_id, deployed=deployed,
                                       now_ms=now, stale_after_ms=stale_after_ms)
        retried = executor.execute_plan(conn, run_id=run_id, plan_id=preview.plan.plan_id, deployed=deployed,
                                        now_ms=now, stale_after_ms=stale_after_ms)
        duplicates = conn.execute(
            "SELECT COUNT(*) FROM (SELECT order_id FROM fulfilments WHERE run_id = ? GROUP BY order_id HAVING COUNT(*) > 1)",
            (run_id,),
        ).fetchone()[0]
    expected = _expected_fulfilments(oracle, stale_only)
    fulfilled = set(result.fulfilled_order_ids)
    return {
        **entry,
        "status": result.status.value,
        "plan_targets": preview.target_count,
        "fulfilled": len(fulfilled),
        "expected_fulfilments": len(expected),
        "matches_oracle": fulfilled == expected,
        "fulfilled_not_expected": sorted(fulfilled - expected),
        "expected_not_fulfilled": sorted(expected - fulfilled),
        "restored_processing_value_paise": result.restored_processing_value_paise,
        "additional_captured_paise": result.additional_captured_paise,
        "retry_replayed": retried.replayed,
        "retry_fulfilled": len(retried.fulfilled_order_ids) if not retried.replayed else 0,
        "duplicate_fulfilments": int(duplicates),
    }


def _plan(conn: sqlite3.Connection, run_id: str, validation: fixes.FixValidation, deployed: merchant.DeployedMerchant,
          now: int, stale_after_ms: int) -> plans.PlanPreview:
    return plans.build_reprocess_plan(
        conn, run_id=run_id, fix_validation_id=validation.validation_id, deployed=deployed, now_ms=now,
        ttl_ms=settings.load_settings().plan_ttl_seconds * 1_000, stale_after_ms=stale_after_ms,
    )


def _fixes_for_blamed_change(conn: sqlite3.Connection, run_id: str, blamed: str, now: int) -> tuple[str, ...]:
    """Allowlisted corrected variants of the modules the blamed deploy changed (none for none/unknown)."""
    if blamed in (CHOICE_NONE, CHOICE_UNKNOWN):
        return ()
    deployment = next((d for d in storage.list_deployments(conn, run_id=run_id, as_of_ms=now) if d.deploy_id == blamed), None)
    if deployment is None:
        return ()
    modules = {path.removeprefix(f"{merchant.PACKAGE}/").removesuffix(".py") for path in deployment.files_changed}
    return tuple(fix for fix in merchant.CORRECTED_VARIANT_IDS if merchant.parse_variant_id(fix)[0] in modules)


def _expected_fulfilments(oracle: Mapping[str, JsonValue], refreshed: list[str]) -> set[str]:
    """The oracle's eligible orders, plus those held back only by staleness that the operator refreshed."""
    orders: Mapping[str, Mapping[str, JsonValue]] = oracle["orders"]  # type: ignore[assignment]
    return {order_id for order_id, order in orders.items()
            if order["expected"] == ELIGIBLE or (order["reasons"] == ["stale_snapshot"] and order_id in refreshed)}
