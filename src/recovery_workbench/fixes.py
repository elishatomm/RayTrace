"""Operator-validated corrected handlers (SPEC.md §2, PLAN.md §5 M2 step 3).

The corrected-handler allowlist is the same in every run, so offering it reveals nothing about the
cause. A corrected handler enters a run only through ``validate_fix``, and only once a diagnosis
exists for that run. Validation runs every allowlisted fixture test against the deployed handler
and against the deployed handler with the fix applied, and records both results.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from pydantic import JsonValue, model_validator

from recovery_workbench import diagnosis_store, policy, storage
from recovery_workbench.contracts import ContractError, ScopeError, StrictModel
from recovery_workbench.simulator import merchant


class FixRefusedError(ContractError):
    """The fix cannot be validated in this run's current state (for example, no diagnosis yet)."""


class FixOption(StrictModel):
    """One allowlisted corrected variant, as the operator sees it after a diagnosis."""

    fix_id: str
    label: str
    files: tuple[str, ...]


class FixValidation(StrictModel):
    run_id: str
    validation_id: str
    diagnosis_id: str
    fix_id: str
    fixture_suite_id: str
    deployed_version: str
    corrected_version: str
    deployed_results: tuple[merchant.FixtureCheckResult, ...]
    corrected_results: tuple[merchant.FixtureCheckResult, ...]
    passed: bool  # true only when the corrected handler passes every allowlisted fixture test
    # True when the deployed handler failed at least one fixture test, i.e. the fixture reproduced the
    # problem. Recorded and shown, never a gate: the dry run on the real failing rows is the stronger check.
    reproduced: bool
    validated_at_ms: int
    operator_id: str

    @model_validator(mode="after")
    def _flags_match_results(self) -> FixValidation:
        if self.reproduced != any(not result.passed for result in self.deployed_results):
            raise ValueError("reproduced must say whether the deployed handler failed a fixture test")
        if self.passed != all(result.passed for result in self.corrected_results):
            raise ValueError("passed must say whether the corrected handler passed every fixture test")
        return self

    @property
    def deployed_failures(self) -> tuple[str, ...]:
        return tuple(result.test_id for result in self.deployed_results if not result.passed)

    @property
    def corrected_failures(self) -> tuple[str, ...]:
        return tuple(result.test_id for result in self.corrected_results if not result.passed)


def available_fixes() -> tuple[FixOption, ...]:
    """The allowlisted corrected variants, identical in every run, with neutral labels."""
    options = []
    for fix_id in merchant.CORRECTED_VARIANT_IDS:
        module, number = merchant.parse_variant_id(fix_id)
        path = merchant.logical_path(module)
        summary = merchant.commit_summaries(fix_id)[0]
        options.append(FixOption(fix_id=fix_id, label=f"{path} revision {number}: {summary}", files=(path,)))
    return tuple(options)


def _run_suite(deployed: merchant.DeployedMerchant) -> tuple[merchant.FixtureCheckResult, ...]:
    return tuple(merchant.run_fixture_check(test_id, deployed) for test_id in merchant.FIXTURE_TEST_IDS)


def _suite_json(version: str, results: tuple[merchant.FixtureCheckResult, ...], fix_id: str | None) -> str:
    document: dict[str, JsonValue] = {
        "version": version,
        "fix_id": fix_id,
        "failed": sum(not result.passed for result in results),
        "results": [result.model_dump(mode="json") for result in results],
    }
    return json.dumps(document, sort_keys=True)


def validate_fix(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    deployed: merchant.DeployedMerchant,
    fix_id: str,
    diagnosis_id: str,
    operator_id: str,
    now_ms: int,
    validation_id: str | None = None,
) -> FixValidation:
    """Run the allowlisted fixture tests on the deployed handler and on deployed plus ``fix_id``.

    Refused unless a diagnosis with ``diagnosis_id`` exists in this run: the corrected handler
    never enters a run before its diagnosis. ``passed`` is true only if the corrected handler
    passes every test. A failed validation is stored too, and it blocks planning.
    """
    if fix_id not in merchant.CORRECTED_VARIANT_IDS:
        raise ContractError(f"fix {fix_id!r} is not allowlisted; choose one of {merchant.CORRECTED_VARIANT_IDS}")
    if policy.operator_kind(operator_id) is not policy.ApproverKind.HUMAN:
        raise ScopeError(f"{operator_id!r} is not a human operator ID; only an operator validates a corrected handler")
    with storage.read_transaction(conn):
        policy.require_replay_time(conn, run_id, now_ms)
    if diagnosis_store.get_diagnosis(conn, run_id=run_id, diagnosis_id=diagnosis_id) is None:
        raise FixRefusedError(
            f"run {run_id!r} has no diagnosis {diagnosis_id!r}; a corrected handler can be validated only after "
            "a diagnosis exists for the run"
        )
    corrected = merchant.with_corrected_variant(deployed, fix_id)
    deployed_results, corrected_results = _run_suite(deployed), _run_suite(corrected)
    validation = FixValidation(
        run_id=run_id,
        validation_id=validation_id or f"fixval_{uuid.uuid4().hex[:16]}",
        diagnosis_id=diagnosis_id,
        fix_id=fix_id,
        fixture_suite_id=merchant.FIXTURE_SUITE_ID,
        deployed_version=deployed.version,
        corrected_version=corrected.version,
        deployed_results=deployed_results,
        corrected_results=corrected_results,
        passed=all(result.passed for result in corrected_results),
        reproduced=any(not result.passed for result in deployed_results),
        validated_at_ms=now_ms,
        operator_id=operator_id,
    )
    with storage.write_transaction(conn):
        policy.require_replay_time(conn, run_id, now_ms)
        conn.execute(
            "INSERT INTO fix_validations (run_id, validation_id, diagnosis_id, fix_id, fixture_test_id,"
            " deployed_result, corrected_result, passed, validated_at_ms, operator_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, validation.validation_id, diagnosis_id, fix_id, validation.fixture_suite_id,
                _suite_json(deployed.version, validation.deployed_results, None),
                _suite_json(corrected.version, validation.corrected_results, fix_id),
                int(validation.passed), now_ms, operator_id,
            ),
        )
    return validation


def get_fix_validation(conn: sqlite3.Connection, *, run_id: str, validation_id: str) -> FixValidation | None:
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        row = conn.execute(
            "SELECT * FROM fix_validations WHERE run_id = ? AND validation_id = ?", (run_id, validation_id)
        ).fetchone()
    if row is None:
        return None
    deployed = json.loads(str(row["deployed_result"]))
    corrected = json.loads(str(row["corrected_result"]))
    deployed_results = tuple(merchant.FixtureCheckResult.model_validate(item) for item in deployed["results"])
    return FixValidation(
        run_id=row["run_id"],
        validation_id=row["validation_id"],
        diagnosis_id=row["diagnosis_id"],
        fix_id=row["fix_id"],
        fixture_suite_id=row["fixture_test_id"],
        deployed_version=deployed["version"],
        corrected_version=corrected["version"],
        deployed_results=deployed_results,
        corrected_results=tuple(merchant.FixtureCheckResult.model_validate(item) for item in corrected["results"]),
        passed=bool(row["passed"]),
        reproduced=any(not result.passed for result in deployed_results),
        validated_at_ms=row["validated_at_ms"],
        operator_id=row["operator_id"],
    )


def require_fix_validation(conn: sqlite3.Connection, *, run_id: str, validation_id: str) -> FixValidation:
    validation = get_fix_validation(conn, run_id=run_id, validation_id=validation_id)
    if validation is None:
        raise ScopeError(f"fix validation {validation_id!r} is not part of run {run_id!r}")
    return validation
