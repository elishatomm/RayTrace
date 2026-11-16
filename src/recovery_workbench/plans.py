"""Deterministic recovery plans (SPEC.md §10): reprocess_inbox, prepare_provider_replay and escalate.

A plan names exact targets, their value in paise, the evidence version they were judged on, the
policy version and an expiry; its plan hash (``policy.compute_plan_hash``) binds all of them.
Building a plan changes no business state. A reprocess plan only dry-runs the corrected handler;
effects belong to ``executor.py``. Every stored target is a JSON object whose ``id`` is the value
the plan hash sorts: the order ID for reprocessing, the payment ID for replay and the subject ID
for an escalation.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence

from pydantic import JsonValue, ValidationError

from recovery_workbench import diagnosis_store, fixes, ingestion, metrics, policy, state, storage
from recovery_workbench.contracts import (
    ActionStatus,
    CaseBlocker,
    CheckType,
    ContractError,
    ExclusionReason,
    HandlerAction,
    HandlerOutcome,
    ObservationSource,
    Operation,
    PaymentObservation,
    PaymentStatus,
    PlanStatus,
    ScopeError,
    StrictModel,
    canonical_json,
    sha256_hex,
)
from recovery_workbench.simulator import merchant


class PlanRefusedError(ContractError):
    """A plan cannot be built from these inputs (for example, a fix validation that did not pass)."""


class PlanTarget(StrictModel):
    """One reprocessing target: the order and the one unprocessed captured event chosen for it."""

    id: str  # the order ID
    payment_id: str
    inbox_id: int
    event_id: str
    amount_paise: int
    currency: str


class ReplayTarget(StrictModel):
    """A provider-observed capture whose captured event never reached durable receipt."""

    id: str  # the payment ID
    order_id: str
    amount_paise: int
    currency: str
    captured_at_ms: int


class StoredPlan(StrictModel):
    run_id: str
    plan_id: str
    incident_id: str
    diagnosis_id: str | None
    fix_validation_id: str | None
    operation: Operation
    targets: tuple[dict[str, JsonValue], ...]
    target_value_paise: int
    evidence_version: str
    policy_version: str
    plan_hash: str
    expires_at_ms: int
    status: PlanStatus
    created_at_ms: int

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(str(target["id"]) for target in self.targets)

    def reprocess_targets(self) -> tuple[PlanTarget, ...]:
        if self.operation is not Operation.REPROCESS_INBOX:
            raise ContractError(f"plan {self.plan_id!r} is a {self.operation.value} plan, not reprocess_inbox")
        return tuple(PlanTarget.model_validate(target) for target in self.targets)


class DryRunResult(StrictModel):
    order_id: str
    inbox_id: int
    passed: bool
    reason: str | None  # a PlanExclusion value when the dry run did not pass
    detail: str | None
    outcome: dict[str, JsonValue] | None


class ExcludedOrder(StrictModel):
    order_id: str
    inbox_id: int
    reasons: tuple[str, ...]


class PlanCandidate(StrictModel):
    """One candidate order as planning judged it: a target, or left out for the reasons the preview shows."""

    order_id: str
    inbox_id: int  # the candidate event: the order's earliest received unprocessed capture
    event_id: str
    payment_id: str
    amount_paise: int  # the candidate event's payment amount
    currency: str
    targeted: bool
    reasons: tuple[str, ...]  # empty exactly when targeted


class PlanAudit(StrictModel):
    """What ``build_reprocess_plan`` recorded beside the plan, for its receipt (``storage.plan_audits``)."""

    plan_id: str
    deployed_version: str
    corrected_version: str
    stale_after_ms: int
    candidates: tuple[PlanCandidate, ...]  # sorted by order ID
    dry_runs: tuple[DryRunResult, ...]  # every dry run, passed or not, in candidate order
    superseded_inbox_ids: dict[str, tuple[int, ...]]


class PlanPreview(StrictModel):
    """What the operator sees before approving: exact targets, value, exclusions and checks."""

    plan: StoredPlan
    targets: tuple[PlanTarget, ...]
    target_count: int
    target_value_paise: int
    excluded: tuple[ExcludedOrder, ...]
    exclusions_by_reason: dict[str, tuple[str, ...]]  # reason -> order IDs
    # Other unprocessed captured events of a target order; left untouched (one effect per order).
    superseded_inbox_ids: dict[str, tuple[int, ...]]
    dry_runs: tuple[DryRunResult, ...]
    preconditions: tuple[str, ...]
    deployed_version: str
    corrected_version: str
    candidate_orders: int


class HeldReplay(StrictModel):
    """A provider-observed capture whose order is blocked, so its captured event must not be resent: not yet, or,
    when the provider already answered 2xx to it, not at all."""

    payment_id: str
    order_id: str  # the order the payment's observations name
    # ExclusionReason values of the order, as the reprocess planner reads them, then the order queue's
    # capture_event_acknowledged_not_stored when the provider's delivery log shows a 2xx answer to the capture's event.
    reasons: tuple[str, ...]


class ReplayInstructions(StrictModel):
    """Operator instructions only: no provider call is made and no business state changes."""

    plan: StoredPlan
    targets: tuple[ReplayTarget, ...]
    # A resend would let the worker act on these orders before a refresh or a person has settled them.
    held_for_reconciliation: tuple[HeldReplay, ...]
    instructions: tuple[str, ...]
    total_value_paise: int
    provider_calls_made: int = 0


class EscalationRecord(StrictModel):
    plan: StoredPlan
    action_id: str
    summary: str
    evidence_needed: tuple[CheckType, ...]
    subject_ids: tuple[str, ...]
    operator_id: str


# --------------------------------------------------------------------------- shared helpers


def operation_key(plan: StoredPlan) -> str:
    """The one key an effect of this exact plan may be recorded under (UNIQUE in ``actions``)."""
    return f"{plan.operation.value}:{plan.run_id}:{plan.plan_id}:{plan.plan_hash}"


def action_id_for(key: str) -> str:
    return f"act_{sha256_hex(key.encode('utf-8'))[:20]}"


def _require_positive(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{name} must be a positive integer of milliseconds, got {value!r}")


def _new_plan_id() -> str:
    return f"plan_{uuid.uuid4().hex[:16]}"


def _insert_plan(conn: sqlite3.Connection, plan: StoredPlan) -> None:
    conn.execute(
        "INSERT INTO plans (run_id, plan_id, incident_id, diagnosis_id, fix_validation_id, operation, targets,"
        " target_value_paise, evidence_version, policy_version, plan_hash, expires_at_ms, status, created_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            plan.run_id, plan.plan_id, plan.incident_id, plan.diagnosis_id, plan.fix_validation_id,
            plan.operation.value, json.dumps(list(plan.targets), sort_keys=True), plan.target_value_paise,
            plan.evidence_version, plan.policy_version, plan.plan_hash, plan.expires_at_ms, plan.status.value,
            plan.created_at_ms,
        ),
    )


def get_plan(conn: sqlite3.Connection, *, run_id: str, plan_id: str) -> StoredPlan | None:
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        row = conn.execute("SELECT * FROM plans WHERE run_id = ? AND plan_id = ?", (run_id, plan_id)).fetchone()
    if row is None:
        return None
    return StoredPlan(
        run_id=row["run_id"],
        plan_id=row["plan_id"],
        incident_id=row["incident_id"],
        diagnosis_id=row["diagnosis_id"],
        fix_validation_id=row["fix_validation_id"],
        operation=Operation(row["operation"]),
        targets=tuple(json.loads(str(row["targets"]))),
        target_value_paise=row["target_value_paise"],
        evidence_version=row["evidence_version"],
        policy_version=row["policy_version"],
        plan_hash=row["plan_hash"],
        expires_at_ms=row["expires_at_ms"],
        status=PlanStatus(row["status"]),
        created_at_ms=row["created_at_ms"],
    )


def require_plan(conn: sqlite3.Connection, *, run_id: str, plan_id: str) -> StoredPlan:
    plan = get_plan(conn, run_id=run_id, plan_id=plan_id)
    if plan is None:
        raise ScopeError(f"plan {plan_id!r} is not part of run {run_id!r}")
    return plan


def set_plan_status(
    conn: sqlite3.Connection, *, run_id: str, plan_id: str, status: PlanStatus, expected: PlanStatus | None = None
) -> None:
    """Move a plan to ``status`` inside the caller's write transaction, optionally only from ``expected``."""
    if not conn.in_transaction:
        raise ContractError("set_plan_status must run inside a write transaction with the effect it records")
    cursor = conn.execute(
        "UPDATE plans SET status = ? WHERE run_id = ? AND plan_id = ? AND (? IS NULL OR status = ?)",
        (status.value, run_id, plan_id, None if expected is None else expected.value,
         None if expected is None else expected.value),
    )
    if cursor.rowcount != 1:
        raise ScopeError(f"plan {plan_id!r} of run {run_id!r} is not in the state required to become {status.value}")


def insert_action(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    action_id: str,
    key: str,
    plan_id: str,
    status: ActionStatus,
    before: JsonValue,
    after: JsonValue,
    error: str | None,
    executed_at_ms: int,
) -> None:
    if not conn.in_transaction:
        raise ContractError("insert_action must run inside the write transaction of the effect it records")
    conn.execute(
        "INSERT INTO actions (run_id, action_id, operation_key, plan_id, status, \"before\", \"after\", error,"
        " executed_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, action_id, key, plan_id, status.value,
            None if before is None else json.dumps(before, sort_keys=True),
            None if after is None else json.dumps(after, sort_keys=True),
            error, executed_at_ms,
        ),
    )


def _diagnosis_incident(conn: sqlite3.Connection, *, run_id: str, diagnosis_id: str) -> str:
    stored = diagnosis_store.get_diagnosis(conn, run_id=run_id, diagnosis_id=diagnosis_id)
    if stored is None:
        raise ScopeError(f"diagnosis {diagnosis_id!r} is not part of run {run_id!r}")
    return stored.incident_id


# --------------------------------------------------------------------------- reprocess_inbox


def _unprocessed_capture_rows(conn: sqlite3.Connection, *, run_id: str, received_by_ms: int) -> list[storage.InboxRecord]:
    rows = conn.execute(
        "SELECT * FROM webhook_inbox WHERE run_id = ? AND event_type = ? AND processing_status IN (?, ?)"
        " AND received_at_ms <= ? ORDER BY received_at_ms, inbox_id",
        (
            run_id, policy.CAPTURE_EVENT_TYPE, *sorted(status.value for status in policy.UNPROCESSED_INBOX_STATUSES),
            received_by_ms,
        ),
    ).fetchall()
    return [storage.InboxRecord.model_validate({key: row[key] for key in row.keys()}) for row in rows]


def dry_run_target(
    corrected: merchant.DeployedMerchant, row: storage.InboxRecord, assessment: policy.TargetAssessment
) -> DryRunResult:
    """Run the corrected handler on one target event without writing anything.

    It must ask to fulfil exactly the target's order and payment, for the event's amount and
    currency; anything else excludes the target.
    """

    def result(passed: bool, reason: policy.PlanExclusion | None, detail: str | None, outcome: HandlerOutcome | None) -> DryRunResult:
        return DryRunResult(
            order_id=assessment.order_id,
            inbox_id=row.inbox_id,
            passed=passed,
            reason=None if reason is None else reason.value,
            detail=detail,
            outcome=None if outcome is None else outcome.model_dump(mode="json", exclude_none=True),
        )

    try:
        reply = corrected.handle(json.loads(row.raw_body))
    except Exception as exc:  # the corrected handler raised on this event: that is the dry-run result
        report = corrected.describe_failure(exc)
        return result(False, policy.PlanExclusion.DRY_RUN_RAISED, report.stack_trace.rstrip().splitlines()[-1], None)
    try:
        outcome = HandlerOutcome.model_validate(dict(reply))
    except (ValidationError, TypeError, ValueError) as invalid:
        return result(False, policy.PlanExclusion.DRY_RUN_MISMATCH, f"handler outcome is invalid: {invalid}", None)
    if outcome.action is not HandlerAction.FULFIL:
        return result(False, policy.PlanExclusion.DRY_RUN_NO_FULFILMENT, f"handler chose {outcome.action.value!r}", outcome)
    expected = {
        "order_id": assessment.order_id,
        "payment_id": assessment.payment_id,
        "amount_paise": assessment.amount_paise,
        "currency": assessment.currency,
    }
    differing = sorted(field for field, value in expected.items() if getattr(outcome, field) != value)
    if differing:
        return result(False, policy.PlanExclusion.DRY_RUN_MISMATCH, f"fulfil request differs in {differing}", outcome)
    return result(True, None, None, outcome)


def build_reprocess_plan(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    fix_validation_id: str,
    deployed: merchant.DeployedMerchant,
    now_ms: int,
    ttl_ms: int,
    stale_after_ms: int,
    plan_id: str | None = None,
) -> PlanPreview:
    """Build and store a draft reprocess_inbox plan, returning the operator's preview.

    Candidates are unprocessed (pending or failed) captured inbox events, grouped by order, with
    one deterministic target event per order (earliest received). Each order is judged on a fresh
    projection, and each surviving target is dry-run through the corrected handler.
    """
    _require_positive("ttl_ms", ttl_ms)
    _require_positive("stale_after_ms", stale_after_ms)
    validation = fixes.require_fix_validation(conn, run_id=run_id, validation_id=fix_validation_id)
    if not validation.passed:
        raise PlanRefusedError(
            f"fix validation {fix_validation_id!r} did not pass (corrected handler failed {list(validation.corrected_failures)}); "
            "validate a different corrected handler before planning"
        )
    if validation.deployed_version != deployed.version:
        raise PlanRefusedError(
            f"fix {validation.fix_id!r} was validated against {validation.deployed_version!r} but "
            f"{deployed.version!r} is deployed now; validate it again"
        )
    incident_id = _diagnosis_incident(conn, run_id=run_id, diagnosis_id=validation.diagnosis_id)
    corrected = merchant.with_corrected_variant(deployed, validation.fix_id)
    with storage.write_transaction(conn):
        policy.require_replay_time(conn, run_id, now_ms)
        by_order: dict[str, list[storage.InboxRecord]] = {}
        for row in _unprocessed_capture_rows(conn, run_id=run_id, received_by_ms=now_ms):
            # Verified on receipt, so the order ID read here is the one the event was signed with.
            by_order.setdefault(_event_order_id(row), []).append(row)
        eligible: list[policy.TargetAssessment] = []
        excluded: list[ExcludedOrder] = []
        dry_runs: list[DryRunResult] = []
        superseded: dict[str, tuple[int, ...]] = {}
        assessed: list[policy.TargetAssessment] = []
        for order_id in sorted(by_order):
            target_row, *others = by_order[order_id]
            assessment = policy.assess_target(
                conn, run_id=run_id, inbox_row=target_row, now_ms=now_ms, stale_after_ms=stale_after_ms
            )
            assessed.append(assessment)
            if not assessment.eligible:
                excluded.append(ExcludedOrder(order_id=order_id, inbox_id=target_row.inbox_id, reasons=assessment.reasons))
                continue
            dry_run = dry_run_target(corrected, target_row, assessment)
            dry_runs.append(dry_run)
            if not dry_run.passed:
                excluded.append(
                    ExcludedOrder(order_id=order_id, inbox_id=target_row.inbox_id, reasons=(str(dry_run.reason),))
                )
                continue
            eligible.append(assessment)
            if others:
                superseded[order_id] = tuple(row.inbox_id for row in others)
        targets = tuple(
            PlanTarget(
                id=assessment.order_id,
                payment_id=assessment.payment_id,
                inbox_id=assessment.inbox_id,
                event_id=assessment.event_id,
                amount_paise=assessment.amount_paise,
                currency=assessment.currency,
            )
            for assessment in sorted(eligible, key=lambda item: item.order_id)
        )
        version = policy.evidence_version(eligible)
        expires_at_ms = now_ms + ttl_ms
        plan = StoredPlan(
            run_id=run_id,
            plan_id=plan_id or _new_plan_id(),
            incident_id=incident_id,
            diagnosis_id=validation.diagnosis_id,
            fix_validation_id=validation.validation_id,
            operation=Operation.REPROCESS_INBOX,
            targets=tuple(target.model_dump(mode="json") for target in targets),
            target_value_paise=sum(target.amount_paise for target in targets),
            evidence_version=version,
            policy_version=policy.POLICY_VERSION,
            plan_hash=policy.compute_plan_hash(
                operation=Operation.REPROCESS_INBOX,
                target_ids=[target.id for target in targets],
                evidence_version=version,
                policy_version=policy.POLICY_VERSION,
                run_id=run_id,
                expires_at_ms=expires_at_ms,
            ),
            expires_at_ms=expires_at_ms,
            status=PlanStatus.DRAFT,
            created_at_ms=now_ms,
        )
        _insert_plan(conn, plan)
        storage.insert_plan_audit(conn, storage.PlanAuditRecord(
            run_id=run_id, plan_id=plan.plan_id, deployed_version=deployed.version,
            corrected_version=corrected.version, stale_after_ms=stale_after_ms,
            candidates=tuple(candidate.model_dump(mode="json") for candidate in _candidates(assessed, excluded)),
            dry_runs=tuple(run.model_dump(mode="json") for run in dry_runs), superseded_inbox_ids=superseded,
        ))
    return PlanPreview(
        plan=plan,
        targets=targets,
        target_count=len(targets),
        target_value_paise=plan.target_value_paise,
        excluded=tuple(excluded),
        exclusions_by_reason=_group_by_reason(excluded),
        superseded_inbox_ids=superseded,
        dry_runs=tuple(dry_runs),
        preconditions=tuple(policy.RECHECK_DESCRIPTIONS.values()),
        deployed_version=deployed.version,
        corrected_version=corrected.version,
        candidate_orders=len(by_order),
    )


def _event_order_id(row: storage.InboxRecord) -> str:
    return ingestion.parse_payment_event(row.raw_body).payment.order_id


def _candidates(
    assessed: Sequence[policy.TargetAssessment], excluded: Sequence[ExcludedOrder]
) -> tuple[PlanCandidate, ...]:
    """Every candidate with the decision the preview shows: the exclusion's own reasons, or a target."""
    reasons = {item.order_id: item.reasons for item in excluded}
    return tuple(
        PlanCandidate(
            order_id=assessment.order_id, inbox_id=assessment.inbox_id, event_id=assessment.event_id,
            payment_id=assessment.payment_id, amount_paise=assessment.amount_paise, currency=assessment.currency,
            targeted=assessment.order_id not in reasons, reasons=reasons.get(assessment.order_id, ()),
        )
        for assessment in assessed
    )


def get_plan_audit(conn: sqlite3.Connection, *, run_id: str, plan_id: str) -> PlanAudit | None:
    """The audit ``build_reprocess_plan`` recorded; None for a plan built before it recorded one (migration 3)."""
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        record = storage.get_plan_audit(conn, run_id=run_id, plan_id=plan_id)
    if record is None:
        return None
    return PlanAudit(
        plan_id=record.plan_id,
        deployed_version=record.deployed_version,
        corrected_version=record.corrected_version,
        stale_after_ms=record.stale_after_ms,
        candidates=tuple(PlanCandidate.model_validate(item) for item in record.candidates),
        dry_runs=tuple(DryRunResult.model_validate(item) for item in record.dry_runs),
        superseded_inbox_ids=record.superseded_inbox_ids,
    )


def _group_by_reason(excluded: Sequence[ExcludedOrder]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for item in excluded:
        for reason in item.reasons:
            grouped.setdefault(reason, []).append(item.order_id)
    return {reason: tuple(sorted(order_ids)) for reason, order_ids in sorted(grouped.items())}


# --------------------------------------------------------------------------- prepare_provider_replay (D3)

# Owner decision Q29 (a). A receiver that answered 2xx and then lost the event before storing it leaves a capture the
# workbench files like any capture whose notification never got in, but the provider's dashboard refuses to resend an
# event it recorded as answered 2xx. Without this line the operator retries, and the next plan targets it again.
REFUSED_RESEND_INSTRUCTION = (
    "If the dashboard refuses a resend because the provider already recorded a 2xx answer for the event, do not try "
    "again or prepare the replay again for it: escalate the order instead, because the provider will not send that "
    "event again."
)


def prepare_provider_replay(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    incident_id: str,
    diagnosis_id: str | None,
    now_ms: int,
    ttl_ms: int,
    stale_after_ms: int,
    plan_id: str | None = None,
) -> ReplayInstructions:
    """Operator instructions for captured payments whose captured event never reached the inbox.

    A payment qualifies when the provider's observations show it captured but no accepted webhook
    carried that capture (ingestion stores one webhook observation per accepted event). Its order is
    then judged as the reprocess planner judges a candidate, blockers first, on a fresh projection
    with every exclusion: a mismatch, multiple captures, a cancellation or refund, conflicting,
    stale or missing provider state, an unresolved extra attempt, or an existing fulfilment. A
    blocked order's capture is held with the order's reasons and is never a target: once resent,
    the event would reach the worker, which waits for no refresh. ``stale_after_ms`` must be the
    reprocess planner's freshness threshold (``WORKBENCH_STALE_AFTER_SECONDS``, as ``cli.build_plan`` passes it),
    so that both judge an order alike.

    A capture is held as well when the provider's delivery log, as a refresh read it by ``now_ms``, shows a 2xx
    answer to its payment.captured event (the queue's ``capture_event_acknowledged_not_stored``): the provider will
    not send that event again, so its line says to escalate the order (owner decision Q29 (b)). Before any read of
    the log such a capture is a target like any other, and the instructions say what the dashboard's refusal means.
    This makes no provider call and changes no business state; it stores the instruction as a draft plan.
    """
    _require_positive("ttl_ms", ttl_ms)
    _require_positive("stale_after_ms", stale_after_ms)
    with storage.write_transaction(conn):
        policy.require_replay_time(conn, run_id, now_ms)
        observations = storage.list_observations(conn, run_id=run_id, as_of_ms=now_ms)
        acknowledged = storage.acknowledged_unstored_payment_ids(conn, run_id=run_id, as_of_ms=now_ms,
                                                                 event_type=policy.CAPTURE_EVENT_TYPE)
        stored_capture_orders = (
            _orders_with_stored_capture(conn, run_id=run_id, as_of_ms=now_ms) if acknowledged else frozenset()
        )
        targets: list[ReplayTarget] = []
        held: list[HeldReplay] = []
        for payment_id, items, projection in _captures_without_stored_event(observations, now_ms):
            order_id = projection.order_ids[0]
            reasons = _replay_hold_reasons(conn, run_id=run_id, order_id=order_id, payment=projection, now_ms=now_ms,
                                           stale_after_ms=stale_after_ms, acknowledged=acknowledged,
                                           stored_capture_orders=stored_capture_orders)
            # An amount or currency the observations do not agree on is itself a reason, so reasons is never empty here.
            if reasons or projection.amount_paise is None or projection.currency is None:
                held.append(HeldReplay(payment_id=payment_id, order_id=order_id, reasons=reasons))
                continue
            captured_at = min(item.occurred_at_ms for item in items if item.status is PaymentStatus.CAPTURED)
            targets.append(
                ReplayTarget(
                    id=payment_id,
                    order_id=order_id,
                    amount_paise=projection.amount_paise,
                    currency=projection.currency,
                    captured_at_ms=captured_at,
                )
            )
        answers = _first_acknowledged_answers(
            conn, run_id=run_id, now_ms=now_ms,
            payment_ids=[item.payment_id for item in held if _ACKNOWLEDGED_NOT_STORED in item.reasons],
        )
        version = sha256_hex(canonical_json({"missing_captures": [target.model_dump(mode="json") for target in targets]}))
        expires_at_ms = now_ms + ttl_ms
        plan = StoredPlan(
            run_id=run_id,
            plan_id=plan_id or _new_plan_id(),
            incident_id=incident_id,
            diagnosis_id=diagnosis_id,
            fix_validation_id=None,
            operation=Operation.PREPARE_PROVIDER_REPLAY,
            targets=tuple(target.model_dump(mode="json") for target in targets),
            target_value_paise=sum(target.amount_paise for target in targets),
            evidence_version=version,
            policy_version=policy.POLICY_VERSION,
            plan_hash=policy.compute_plan_hash(
                operation=Operation.PREPARE_PROVIDER_REPLAY,
                target_ids=[target.id for target in targets],
                evidence_version=version,
                policy_version=policy.POLICY_VERSION,
                run_id=run_id,
                expires_at_ms=expires_at_ms,
            ),
            expires_at_ms=expires_at_ms,
            status=PlanStatus.DRAFT,
            created_at_ms=now_ms,
        )
        _insert_plan(conn, plan)
    return ReplayInstructions(
        plan=plan,
        targets=tuple(targets),
        held_for_reconciliation=tuple(held),
        instructions=_replay_instructions(targets, held, answers),
        total_value_paise=plan.target_value_paise,
    )


def captures_without_stored_event(conn: sqlite3.Connection, *, run_id: str, as_of_ms: int) -> tuple[str, ...]:
    """The payments a replay plan at ``as_of_ms`` would consider, sorted: captured at the provider, and no accepted
    webhook carried the capture. Only the provider's delivery log tells which of their events were rejected and which
    were acknowledged and lost (owner decision Q29 (b)). Reads one snapshot and changes nothing."""
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        observations = storage.list_observations(conn, run_id=run_id, as_of_ms=as_of_ms)
    return tuple(payment_id for payment_id, _, _ in _captures_without_stored_event(observations, as_of_ms))


def _captures_without_stored_event(
    observations: Sequence[PaymentObservation], now_ms: int
) -> list[tuple[str, list[PaymentObservation], state.PaymentProjection]]:
    by_payment: dict[str, list[PaymentObservation]] = {}
    for observation in observations:
        by_payment.setdefault(observation.payment_id, []).append(observation)
    found = []
    for payment_id in sorted(by_payment):
        items = by_payment[payment_id]
        if any(item.source is ObservationSource.WEBHOOK and item.status is PaymentStatus.CAPTURED for item in items):
            continue  # its captured event was durably received; the inbox, not the provider, holds it
        projection = state.project_payment(items, now_ms, payment_id=payment_id)
        if projection.status is PaymentStatus.CAPTURED:
            found.append((payment_id, items, projection))
    return found


_ACKNOWLEDGED_NOT_STORED = CaseBlocker.CAPTURE_EVENT_ACKNOWLEDGED_NOT_STORED.value


def _replay_hold_reasons(
    conn: sqlite3.Connection, *, run_id: str, order_id: str, payment: state.PaymentProjection, now_ms: int,
    stale_after_ms: int, acknowledged: frozenset[str], stored_capture_orders: frozenset[str],
) -> tuple[str, ...]:
    """Why a captured payment must not be resent: its order's exclusions, as ``policy.assess_target`` reads them.

    The payment's own disagreement (a conflict, or no agreed amount or currency) is also the order's
    ``conflicting_evidence``; it is added here as well, so a held capture always carries a reason. Last comes
    ``capture_event_acknowledged_not_stored``, under the order queue's own condition (``order_cases._blockers``): no
    durable payment.captured event names the order, and the provider answered 2xx to one of its captures' events.
    """
    order = state.load_order_projection(conn, run_id, order_id, now_ms, stale_after_ms=stale_after_ms)
    found = set(order.exclusions)
    if payment.needs_reconciliation or payment.amount_paise is None or payment.currency is None:
        found.add(ExclusionReason.CONFLICTING_EVIDENCE)
    reasons = [reason.value for reason in ExclusionReason if reason in found]
    lost_after_acknowledgement = any(str(item.payment_id) in acknowledged for item in order.captured_payments)
    if lost_after_acknowledgement and order_id not in stored_capture_orders:
        reasons.append(_ACKNOWLEDGED_NOT_STORED)
    return tuple(reasons)


def _orders_with_stored_capture(conn: sqlite3.Connection, *, run_id: str, as_of_ms: int) -> frozenset[str]:
    """Orders a durable payment.captured event names, as the order queue reads them: inbox rows received by then."""
    return frozenset(
        ingestion.parse_payment_event(row.raw_body).payment.order_id
        for row in storage.list_inbox_rows(conn, run_id=run_id)
        if row.event_type == policy.CAPTURE_EVENT_TYPE and row.received_at_ms <= as_of_ms
    )


def _first_acknowledged_answers(
    conn: sqlite3.Connection, *, run_id: str, now_ms: int, payment_ids: Sequence[str]
) -> dict[str, storage.ProviderDeliveryAttemptRecord]:
    """Each payment's earliest 2xx answer to a payment.captured event, as the provider's delivery log records it."""
    if not payment_ids:
        return {}
    answers: dict[str, storage.ProviderDeliveryAttemptRecord] = {}
    for attempt in storage.list_provider_delivery_attempts(conn, run_id=run_id, as_of_ms=now_ms, payment_ids=payment_ids):
        if attempt.acknowledged and attempt.event_type == policy.CAPTURE_EVENT_TYPE:
            answers.setdefault(attempt.payment_id, attempt)
    return answers


def _replay_instructions(
    targets: Sequence[ReplayTarget], held: Sequence[HeldReplay],
    answers: Mapping[str, storage.ProviderDeliveryAttemptRecord],
) -> tuple[str, ...]:
    lines = [
        "Before asking for any resend, confirm the receiver accepts a correctly signed delivery "
        "(fixture test signed_delivery_verifies passes on the deployed receiver).",
        "Then, in the provider's dashboard, resend the payment.captured webhook for each payment below. "
        "The workbench makes no provider call itself.",
        REFUSED_RESEND_INSTRUCTION,
    ]
    lines.extend(
        f"Resend payment.captured for {target.id} (order {target.order_id}, {target.amount_paise} paise "
        f"{target.currency}, captured at {target.captured_at_ms} ms)."
        for target in targets
    )
    lines.extend(_held_line(item, answers.get(item.payment_id)) for item in held)
    return tuple(lines)


def _held_line(item: HeldReplay, answer: storage.ProviderDeliveryAttemptRecord | None) -> str:
    held_for = f"Do not resend {item.payment_id} (order {item.order_id}): the order is held for {', '.join(item.reasons)}; "
    if answer is None:
        return held_for + "settle that first, then prepare the replay again."
    return held_for + (
        f"the provider recorded a {answer.provider_http_status} answer to its payment.captured event {answer.event_id} "
        f"at {answer.attempted_at_ms} ms and will not send it again, so escalate the order instead of preparing the "
        "replay again."
    )


# --------------------------------------------------------------------------- escalate


def escalate(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    incident_id: str,
    diagnosis_id: str | None,
    operator_id: str,
    summary: str,
    evidence_needed: Sequence[CheckType],
    subject_ids: Sequence[str],
    now_ms: int,
    plan_id: str | None = None,
) -> EscalationRecord:
    """Record an exception and the evidence a person needs to resolve it; no business state changes."""
    if policy.operator_kind(operator_id) is not policy.ApproverKind.HUMAN:
        raise ScopeError(f"{operator_id!r} is not a human operator ID; only an operator records an escalation")
    if not summary.strip():
        raise ContractError("an escalation needs a summary of what is unresolved")
    subjects = tuple(sorted(set(subject_ids)))
    checks = tuple(dict.fromkeys(evidence_needed))
    with storage.write_transaction(conn):
        policy.require_replay_time(conn, run_id, now_ms)
        version = sha256_hex(
            canonical_json({"summary": summary, "evidence_needed": [check.value for check in checks], "subjects": list(subjects)})
        )
        plan = StoredPlan(
            run_id=run_id,
            plan_id=plan_id or _new_plan_id(),
            incident_id=incident_id,
            diagnosis_id=diagnosis_id,
            fix_validation_id=None,
            operation=Operation.ESCALATE,
            targets=tuple({"id": subject} for subject in subjects),
            target_value_paise=0,
            evidence_version=version,
            policy_version=policy.POLICY_VERSION,
            plan_hash=policy.compute_plan_hash(
                operation=Operation.ESCALATE,
                target_ids=subjects,
                evidence_version=version,
                policy_version=policy.POLICY_VERSION,
                run_id=run_id,
                expires_at_ms=now_ms,
            ),
            expires_at_ms=now_ms,
            status=PlanStatus.EXECUTED,  # recording the exception is the whole effect
            created_at_ms=now_ms,
        )
        _insert_plan(conn, plan)
        key = operation_key(plan)
        action_id = action_id_for(key)
        insert_action(
            conn,
            run_id=run_id,
            action_id=action_id,
            key=key,
            plan_id=plan.plan_id,
            status=ActionStatus.SUCCEEDED,
            before=None,
            after={
                "exception": summary,
                "evidence_needed": [check.value for check in checks],
                "subjects": list(subjects),
                "operator_id": operator_id,
            },
            error=None,
            executed_at_ms=now_ms,
        )
    return EscalationRecord(
        plan=plan, action_id=action_id, summary=summary, evidence_needed=checks, subject_ids=subjects, operator_id=operator_id
    )


# --------------------------------------------------------------------------- late-authorised orders (facts)


class LateAuthorisedOrder(StrictModel):
    """An order with a payment that failed first and authorised later, as the projection sees it (read-only)."""

    order_id: str
    payment_ids: tuple[str, ...]  # the late-authorised payments
    exclusions: tuple[str, ...]  # the order projection's own exclusions; late authorisation is not one of them
    fulfilled: bool


def late_authorised_orders(
    conn: sqlite3.Connection, *, run_id: str, now_ms: int, stale_after_ms: int
) -> tuple[LateAuthorisedOrder, ...]:
    """Orders with a payment that failed and then authorised later, with every exclusion they carry.

    Since policy-v3 (owner decision Q8) a late authorisation alone does not keep an order out of a
    reprocessing plan: when its payment is the order's only capture, fulfilling the order restores
    processing of money already collected. A second capture on the same order (multiple_captures)
    still blocks, because it is what a customer who paid again after a reported failure leaves.
    Changes nothing.
    """
    _require_positive("stale_after_ms", stale_after_ms)
    with storage.read_transaction(conn):
        policy.require_replay_time(conn, run_id, now_ms)
    view = metrics.load_run_view(conn, run_id, now_ms, stale_after_ms=stale_after_ms)
    found = []
    for order_id, projection in sorted(view.order_projections.items()):
        late = tuple(str(payment.payment_id) for payment in projection.payments if payment.late_authorization)
        if not late:
            continue
        found.append(LateAuthorisedOrder(
            order_id=order_id, payment_ids=late, exclusions=tuple(reason.value for reason in projection.exclusions),
            fulfilled=projection.fulfilment is not None,
        ))
    return tuple(found)
