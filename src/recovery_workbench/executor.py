"""Approve and execute recovery plans in the synthetic sandbox (SPEC.md §10, PLAN.md §5 M2).

* ``approve_plan`` binds a human operator's approval to the exact plan hash they saw. No model
  path reaches it: the model only proposes an operation class.
* ``execute_plan`` runs ``policy.recheck_reprocess`` inside ``BEGIN IMMEDIATE`` and applies the
  effect in that same transaction, so nothing can change between check and write. It is
  idempotent on the plan's unique operation key: a double click or a retried request returns
  the recorded action and changes nothing. A failed recheck is recorded as a rejected action,
  the plan is invalidated, and ``PlanRejectedError`` (HTTP 409) is raised after that commits.

Reported values stay separate and are never summed into one "revenue recovered" figure:
restored processing value (already-captured orders newly processed) and additional captured
amount (new money, which reprocessing never creates).

Every execution, applied or rejected, also records an ``ActionAudit`` in the same transaction: each
check with its result (the ones that passed too), the provider observations each target was judged
on, each order's result, the capture and fulfilment postconditions, and the restored and unresolved
values. ``report.build_receipt`` reads it back; nothing here decides anything from it.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

import recovery_workbench
from recovery_workbench import fixes, metrics, plans, policy, state, storage
from recovery_workbench.contracts import (
    SUPPORTED_ORDER_CURRENCY,
    ActionStatus,
    ContractError,
    InboxStatus,
    ObservationSource,
    Operation,
    PaymentObservation,
    PaymentStatus,
    PlanStatus,
    ScopeError,
    StaleStateError,
    StrictModel,
)
from recovery_workbench.simulator import merchant

APPLICATION_VERSION = recovery_workbench.__version__


class StoredApproval(StrictModel):
    run_id: str
    approval_id: str
    plan_id: str
    plan_hash: str
    operator_id: str
    approved_at_ms: int
    expires_at_ms: int


class ExecutionResult(StrictModel):
    run_id: str
    plan_id: str
    action_id: str
    operation_key: str
    status: ActionStatus
    reasons: tuple[str, ...]
    fulfilled_order_ids: tuple[str, ...]
    restored_processing_value_paise: int  # captured before the intervention, newly processed now
    additional_captured_paise: int  # new captures caused by the action; reprocessing creates none
    executed_at_ms: int
    replayed: bool  # true when this call found the action already recorded and changed nothing


class PlanRejectedError(StaleStateError):
    """A precondition failed at execution: the action is recorded as rejected and the plan invalidated."""

    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        super().__init__(f"plan {result.plan_id!r} was rejected at execution: {'; '.join(result.reasons)}")


# --------------------------------------------------------------------------- the action audit (receipt facts)

CheckResult = Literal["passed", "failed", "not_run"]
# failed: the effect ran, yet the fulfilments table holds no row by this action for the order. Never expected,
# because the effect is one transaction; it is measured rather than assumed, so the receipt would show it.
OrderResult = Literal["completed", "no_op", "refused", "failed"]
RERUN_CHECK = "corrected_handler_rerun"
RERUN_DESCRIPTION = (
    "the corrected handler, run once more on every target inside the transaction, asks to fulfil exactly that "
    "order and payment, for its amount and currency"
)
PLAN_REJECTED = "plan_rejected"  # an eligible order left untouched because another check refused the whole plan


class ExecutionCheck(StrictModel):
    """One precondition evaluated at execution; ``details`` say what failed."""

    code: str
    description: str
    result: CheckResult
    details: tuple[str, ...]


class ObservationUsed(StrictModel):
    """One provider observation a target's fresh projection read at execution."""

    observation_id: str
    payment_id: str
    status: PaymentStatus
    source: ObservationSource
    source_ref: str  # the inbox event ID, or the snapshot ID
    occurred_at_ms: int
    known_at_ms: int


class TargetEvidence(StrictModel):
    order_id: str
    newest_known_at_ms: int | None
    freshness_ms: int | None  # execution time minus newest_known_at_ms
    observations: tuple[ObservationUsed, ...]  # by known time, then ID


class ExecutionEvidence(StrictModel):
    planned_version: str  # the evidence version the plan hash binds
    version_at_execution: str  # recomputed by the recheck from fresh projections; equal unless evidence_changed
    targets: tuple[TargetEvidence, ...]


class OrderExecution(StrictModel):
    order_id: str
    inbox_id: int
    payment_id: str
    amount_paise: int
    result: OrderResult
    reasons: tuple[str, ...]  # empty when completed; the order's own reasons when refused; plan_rejected for a no-op


class TargetCaptures(StrictModel):
    """Capture evidence for one target, read before and after the effect."""

    order_id: str
    payment_id: str
    order_captures_before: int  # the order's payments with capture evidence
    order_captures_after: int
    payment_captured_paise_before: int | None  # None when the payment's observations disagree on its amount
    payment_captured_paise_after: int | None


class OutstandingOrder(StrictModel):
    """A candidate order of the plan still unfulfilled after this action, and what holds it."""

    order_id: str
    stage: Literal["planning", "execution"]
    reasons: tuple[str, ...]


class Postconditions(StrictModel):
    captured_payments_before: int
    captured_payments_after: int
    captured_paise_before: int
    captured_paise_after: int
    new_captured_payment_ids: tuple[str, ...]
    targets: tuple[TargetCaptures, ...]
    duplicate_fulfilments: int  # target orders with more than one fulfilment row (the table's key allows one)
    payments_fulfilling_several_orders: tuple[str, ...]  # target payments behind more than one fulfilment
    candidates_recorded: bool  # False for a plan built before plan audits: outstanding then covers its targets only
    outstanding: tuple[OutstandingOrder, ...]


class ExecutionAmounts(StrictModel):
    currency: str
    restored_processing_value_paise: int
    additional_captured_paise: int
    unresolved_value_paise: int  # order value of the outstanding orders
    unresolved_orders: int
    unvalued_unresolved_order_ids: tuple[str, ...]  # no order record, or another currency: counted, not summed


class ActionAudit(StrictModel):
    """What one execution checked, read and changed (``storage.action_audits``)."""

    action_id: str
    application_version: str
    stale_after_ms: int
    checks: tuple[ExecutionCheck, ...]
    evidence: ExecutionEvidence
    orders: tuple[OrderExecution, ...]  # one per target, in plan order
    postconditions: Postconditions
    amounts: ExecutionAmounts


@dataclass(frozen=True)
class _Observed:
    """The run's capture ledger and each target's fresh projection, read at one moment of the transaction."""

    ledger: metrics.CaptureLedger
    projections: Mapping[str, state.OrderProjection]  # by target order ID


# --------------------------------------------------------------------------- approval


def approve_plan(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    plan_id: str,
    plan_hash: str,
    operator_id: str,
    now_ms: int,
    ttl_ms: int,
    approval_id: str | None = None,
) -> StoredApproval:
    """Record a human operator's approval of the exact plan hash they reviewed.

    Raises ``ScopeError`` for anyone who is not a human operator or a plan outside the run, and
    ``StaleStateError`` if the plan changed, expired or is no longer a draft.
    """
    if policy.operator_kind(operator_id) is not policy.ApproverKind.HUMAN:
        raise ScopeError(f"{operator_id!r} is not a human operator ID; only a human operator can approve a plan")
    if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or ttl_ms <= 0:
        raise ContractError(f"ttl_ms must be a positive integer of milliseconds, got {ttl_ms!r}")
    with storage.write_transaction(conn):
        policy.require_replay_time(conn, run_id, now_ms)
        plan = plans.require_plan(conn, run_id=run_id, plan_id=plan_id)
        if plan.operation is not Operation.REPROCESS_INBOX:
            raise ContractError(f"plan {plan_id!r} is {plan.operation.value}; only reprocess_inbox plans take approval")
        if plan.status is not PlanStatus.DRAFT:
            raise StaleStateError(f"plan {plan_id!r} is {plan.status.value}; only a draft plan can be approved")
        if plan_hash != plan.plan_hash:
            raise StaleStateError(f"plan {plan_id!r} has hash {plan.plan_hash}, not the reviewed {plan_hash}; review it again")
        if plan.expires_at_ms <= now_ms:
            raise StaleStateError(f"plan {plan_id!r} expired at {plan.expires_at_ms}; build a new plan")
        if not plan.targets:
            raise ContractError(f"plan {plan_id!r} has no targets, so there is nothing to approve")
        approval = StoredApproval(
            run_id=run_id,
            approval_id=approval_id or f"appr_{uuid.uuid4().hex[:16]}",
            plan_id=plan_id,
            plan_hash=plan_hash,
            operator_id=operator_id,
            approved_at_ms=now_ms,
            expires_at_ms=now_ms + ttl_ms,
        )
        conn.execute(
            "INSERT INTO approvals (run_id, approval_id, plan_id, plan_hash, operator_id, approved_at_ms, expires_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, approval.approval_id, plan_id, plan_hash, operator_id, now_ms, approval.expires_at_ms),
        )
        plans.set_plan_status(conn, run_id=run_id, plan_id=plan_id, status=PlanStatus.APPROVED, expected=PlanStatus.DRAFT)
    return approval


def get_approval(conn: sqlite3.Connection, *, run_id: str, plan_id: str) -> StoredApproval | None:
    with storage.read_transaction(conn):
        row = conn.execute("SELECT * FROM approvals WHERE run_id = ? AND plan_id = ?", (run_id, plan_id)).fetchone()
    return None if row is None else StoredApproval.model_validate({key: row[key] for key in row.keys()})


# --------------------------------------------------------------------------- execution


def execute_plan(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    plan_id: str,
    deployed: merchant.DeployedMerchant,
    now_ms: int,
    stale_after_ms: int,
) -> ExecutionResult:
    """Execute an approved reprocess_inbox plan exactly once.

    ``deployed`` is the merchant code deployed now (dependency injection; the executor never looks
    it up). The corrected handler is the deployed one plus the plan's validated fix.
    """
    if isinstance(stale_after_ms, bool) or not isinstance(stale_after_ms, int) or stale_after_ms <= 0:
        raise ContractError(f"stale_after_ms must be a positive integer of milliseconds, got {stale_after_ms!r}")
    with storage.write_transaction(conn):
        policy.require_replay_time(conn, run_id, now_ms)
        plan = plans.require_plan(conn, run_id=run_id, plan_id=plan_id)
        if plan.operation is not Operation.REPROCESS_INBOX:
            raise ContractError(f"plan {plan_id!r} is {plan.operation.value}; the executor only runs reprocess_inbox")
        key = plans.operation_key(plan)
        existing = _action_by_key(conn, run_id=run_id, key=key)
        if existing is not None:
            outcome = _result_from_action(existing, plan, replayed=True)
        else:
            outcome = _execute_once(conn, plan, key=key, deployed=deployed, now_ms=now_ms, stale_after_ms=stale_after_ms)
    if outcome.status is ActionStatus.REJECTED:
        raise PlanRejectedError(outcome)
    return outcome


def _execute_once(
    conn: sqlite3.Connection,
    plan: plans.StoredPlan,
    *,
    key: str,
    deployed: merchant.DeployedMerchant,
    now_ms: int,
    stale_after_ms: int,
) -> ExecutionResult:
    targets = plan.reprocess_targets()
    approval = get_approval(conn, run_id=plan.run_id, plan_id=plan.plan_id)
    validation = (
        None
        if plan.fix_validation_id is None
        else fixes.get_fix_validation(conn, run_id=plan.run_id, validation_id=plan.fix_validation_id)
    )
    facts = policy.ReprocessFacts(
        run_id=plan.run_id,
        plan_run_id=plan.run_id,
        plan_id=plan.plan_id,
        plan_status=plan.status,
        plan_hash=plan.plan_hash,
        # The stored fields _apply writes from, taken from the same targets it is given: the effect is what was rechecked.
        target_refs=tuple(
            policy.TargetRef(order_id=target.id, inbox_id=target.inbox_id, event_id=target.event_id,
                             payment_id=target.payment_id, amount_paise=target.amount_paise, currency=target.currency)
            for target in targets
        ),
        evidence_version=plan.evidence_version,
        policy_version=plan.policy_version,
        plan_expires_at_ms=plan.expires_at_ms,
        approval=None
        if approval is None
        else policy.ApprovalRecord(
            operator_id=approval.operator_id,
            run_id=approval.run_id,
            plan_hash=approval.plan_hash,
            expires_at_ms=approval.expires_at_ms,
        ),
        validation_passed=None if validation is None else validation.passed,
        validated_deployed_version=None if validation is None else validation.deployed_version,
        deployed_version=deployed.version,
        prior_action_exists=False,  # the caller returned early when an action exists for this key
    )
    recheck = policy.recheck_reprocess(conn, facts, now_ms=now_ms, stale_after_ms=stale_after_ms)
    corrected: merchant.DeployedMerchant | None = None
    reruns: tuple[plans.DryRunResult, ...] = ()
    if recheck.passed and validation is not None:
        corrected = merchant.with_corrected_variant(deployed, validation.fix_id)
        reruns = _rerun_corrected_handler(conn, plan, corrected, recheck.assessments)
    failures = [*recheck.failures, *_rerun_failures(reruns)]
    before = _observe(conn, plan, targets, now_ms=now_ms, stale_after_ms=stale_after_ms)
    if failures or corrected is None:
        result = _reject(conn, plan, key=key, failures=failures, now_ms=now_ms)
        orders = _orders_of_rejected_plan(targets, recheck.assessments, reruns)
    else:
        result = _apply(conn, plan, key=key, targets=targets, recheck=recheck, corrected=corrected, now_ms=now_ms,
                        capture_before=before.ledger)
        orders = _orders_of_applied_plan(conn, plan, targets, action_id=result.action_id)
    after = _observe(conn, plan, targets, now_ms=now_ms, stale_after_ms=stale_after_ms)
    checks = _execution_checks(recheck, reruns=reruns, rerun_ran=corrected is not None)
    audit = _audit(conn, plan, result, checks=checks, recheck=recheck, orders=orders, before=before, after=after,
                   stale_after_ms=stale_after_ms)
    storage.insert_action_audit(conn, storage.ActionAuditRecord(
        run_id=plan.run_id, **audit.model_dump(mode="json")
    ))
    return result


def _rerun_corrected_handler(
    conn: sqlite3.Connection,
    plan: plans.StoredPlan,
    corrected: merchant.DeployedMerchant,
    assessments: tuple[policy.TargetAssessment, ...],
) -> tuple[plans.DryRunResult, ...]:
    """Run the corrected handler on every target once more, inside the transaction, before any write."""
    found = []
    for assessment in assessments:
        row = storage.get_inbox_row(conn, run_id=plan.run_id, inbox_id=assessment.inbox_id)
        if row is None:  # the recheck already reported it; keep the type checker honest
            continue
        found.append(plans.dry_run_target(corrected, row, assessment))
    return tuple(found)


def _rerun_failures(reruns: Sequence[plans.DryRunResult]) -> list[policy.PreconditionFailure]:
    return [
        policy.PreconditionFailure(
            code=policy.RecheckFailure.TARGET_INELIGIBLE, detail=f"{rerun.order_id}: {rerun.reason} ({rerun.detail})"
        )
        for rerun in reruns
        if not rerun.passed
    ]


def _observe(
    conn: sqlite3.Connection, plan: plans.StoredPlan, targets: Sequence[plans.PlanTarget], *, now_ms: int,
    stale_after_ms: int,
) -> _Observed:
    return _Observed(
        ledger=_capture_ledger(conn, run_id=plan.run_id, as_of_ms=now_ms),
        projections={
            target.id: state.load_order_projection(conn, plan.run_id, target.id, now_ms, stale_after_ms=stale_after_ms)
            for target in targets
        },
    )


def _order_execution(target: plans.PlanTarget, result: OrderResult, reasons: tuple[str, ...]) -> OrderExecution:
    return OrderExecution(order_id=target.id, inbox_id=target.inbox_id, payment_id=target.payment_id,
                          amount_paise=target.amount_paise, result=result, reasons=reasons)


def _orders_of_applied_plan(
    conn: sqlite3.Connection, plan: plans.StoredPlan, targets: Sequence[plans.PlanTarget], *, action_id: str
) -> tuple[OrderExecution, ...]:
    """Each target as the fulfilments table shows it after the effect: completed by this action's row, or failed."""
    found = []
    for target in targets:
        fulfilment = storage.get_fulfilment(conn, run_id=plan.run_id, order_id=target.id)
        if fulfilment is not None and fulfilment.action_id == action_id:
            found.append(_order_execution(target, "completed", ()))
        else:
            found.append(_order_execution(target, "failed", ("no_fulfilment_by_this_action",)))
    return tuple(found)


def _orders_of_rejected_plan(
    targets: Sequence[plans.PlanTarget],
    assessments: Sequence[policy.TargetAssessment],
    reruns: Sequence[plans.DryRunResult],
) -> tuple[OrderExecution, ...]:
    """A target that failed its own check is refused; every other target is a no-op, untouched with the plan."""
    by_inbox = {assessment.inbox_id: assessment for assessment in assessments}
    reruns_by_inbox = {rerun.inbox_id: rerun for rerun in reruns}
    found = []
    for target in targets:
        own = _own_refusal(target, by_inbox.get(target.inbox_id), reruns_by_inbox.get(target.inbox_id))
        found.append(_order_execution(target, "refused", tuple(own)) if own else
                     _order_execution(target, "no_op", (PLAN_REJECTED,)))
    return tuple(found)


def _own_refusal(
    target: plans.PlanTarget, assessment: policy.TargetAssessment | None, rerun: plans.DryRunResult | None
) -> list[str]:
    """Why this target itself could not be fulfilled at execution; empty when it was still eligible."""
    if assessment is None:
        return ["inbox_row_missing"]
    if assessment.order_id != target.id:
        return ["inbox_row_names_another_order"]
    if not assessment.eligible:
        return list(assessment.reasons)
    if rerun is not None and not rerun.passed:
        return [str(rerun.reason)]
    return []


def _execution_checks(
    recheck: policy.RecheckOutcome, *, reruns: Sequence[plans.DryRunResult], rerun_ran: bool
) -> tuple[ExecutionCheck, ...]:
    """Every recheck in ``policy.RECHECK_DESCRIPTIONS`` order, then the corrected handler's rerun.

    Each recheck is recorded as ``recheck_reprocess`` reported it; one it did not evaluate is ``not_run``.
    """
    reported = {check.code: check for check in recheck.checks}
    checks = [_reported_check(code, description, reported.get(code))
              for code, description in policy.RECHECK_DESCRIPTIONS.items()]
    rerun_failed = tuple(failure.detail for failure in _rerun_failures(reruns))
    rerun_result: CheckResult = "failed" if rerun_failed else "passed"
    if not rerun_ran:  # the recheck failed first, or the plan has no fix validation to correct the handler with
        rerun_result = "not_run"
    checks.append(ExecutionCheck(code=RERUN_CHECK, description=RERUN_DESCRIPTION, result=rerun_result,
                                 details=rerun_failed))
    return tuple(checks)


def _reported_check(
    code: policy.RecheckFailure, description: str, reported: policy.PreconditionCheck | None
) -> ExecutionCheck:
    if reported is None:
        return ExecutionCheck(code=code.value, description=description, result="not_run", details=())
    return ExecutionCheck(code=code.value, description=description, result="passed" if reported.passed else "failed",
                          details=reported.details)


def _audit(
    conn: sqlite3.Connection,
    plan: plans.StoredPlan,
    result: ExecutionResult,
    *,
    checks: tuple[ExecutionCheck, ...],
    recheck: policy.RecheckOutcome,
    orders: tuple[OrderExecution, ...],
    before: _Observed,
    after: _Observed,
    stale_after_ms: int,
) -> ActionAudit:
    plan_audit = plans.get_plan_audit(conn, run_id=plan.run_id, plan_id=plan.plan_id)
    outstanding = _outstanding(conn, plan, orders, plan_audit)
    change = metrics.capture_change(before.ledger, after.ledger)
    return ActionAudit(
        action_id=result.action_id,
        application_version=APPLICATION_VERSION,
        stale_after_ms=stale_after_ms,
        checks=checks,
        evidence=ExecutionEvidence(
            planned_version=plan.evidence_version,
            version_at_execution=policy.evidence_version(recheck.assessments),
            targets=tuple(_target_evidence(order.order_id, before.projections[order.order_id], result.executed_at_ms)
                          for order in orders),
        ),
        orders=orders,
        postconditions=_postconditions(conn, plan, orders, before=before, after=after, change=change,
                                       outstanding=outstanding, candidates_recorded=plan_audit is not None),
        amounts=_amounts(conn, plan, result, change=change, outstanding=outstanding),
    )


def _target_evidence(order_id: str, projection: state.OrderProjection, executed_at_ms: int) -> TargetEvidence:
    observations = sorted((item for payment in projection.payments for item in payment.history),
                          key=lambda item: (item.known_at_ms, item.observation_id))
    newest = max((item.known_at_ms for item in observations), default=None)
    return TargetEvidence(
        order_id=order_id,
        newest_known_at_ms=newest,
        freshness_ms=None if newest is None else executed_at_ms - newest,
        observations=tuple(
            ObservationUsed(observation_id=item.observation_id, payment_id=item.payment_id, status=item.status,
                            source=item.source, source_ref=item.source_ref, occurred_at_ms=item.occurred_at_ms,
                            known_at_ms=item.known_at_ms)
            for item in observations
        ),
    )


def _outstanding(
    conn: sqlite3.Connection, plan: plans.StoredPlan, orders: Sequence[OrderExecution],
    plan_audit: plans.PlanAudit | None,
) -> tuple[OutstandingOrder, ...]:
    """The plan's candidate orders with no fulfilment after the effect: left out at planning, or not fulfilled now.

    Without a plan audit (a plan built before migration 3) only the targets are known.
    """
    executed = {order.order_id: order for order in orders}
    if plan_audit is None:
        held = [OutstandingOrder(order_id=order.order_id, stage="execution", reasons=order.reasons) for order in orders]
    else:
        held = [_held(candidate, executed.get(candidate.order_id)) for candidate in plan_audit.candidates]
    return tuple(item for item in held if storage.get_fulfilment(conn, run_id=plan.run_id, order_id=item.order_id) is None)


def _held(candidate: plans.PlanCandidate, executed: OrderExecution | None) -> OutstandingOrder:
    if not candidate.targeted:
        return OutstandingOrder(order_id=candidate.order_id, stage="planning", reasons=candidate.reasons)
    # A target recorded at planning but missing from the plan executed: its stored targets were altered.
    reasons = ("not_in_the_executed_plan",) if executed is None else executed.reasons
    return OutstandingOrder(order_id=candidate.order_id, stage="execution", reasons=reasons)


def _postconditions(
    conn: sqlite3.Connection,
    plan: plans.StoredPlan,
    orders: Sequence[OrderExecution],
    *,
    before: _Observed,
    after: _Observed,
    change: metrics.CaptureChange,
    outstanding: tuple[OutstandingOrder, ...],
    candidates_recorded: bool,
) -> Postconditions:
    fulfilments = storage.list_fulfilments(conn, run_id=plan.run_id)
    rows_by_order = Counter(item.order_id for item in fulfilments)
    rows_by_payment = Counter(item.payment_id for item in fulfilments)
    return Postconditions(
        captured_payments_before=len(before.ledger.captured_payment_ids),
        captured_payments_after=len(after.ledger.captured_payment_ids),
        captured_paise_before=before.ledger.captured_paise,
        captured_paise_after=after.ledger.captured_paise,
        new_captured_payment_ids=change.new_captured_payment_ids,
        targets=tuple(
            TargetCaptures(
                order_id=order.order_id, payment_id=order.payment_id,
                order_captures_before=_order_captures(before.projections[order.order_id]),
                order_captures_after=_order_captures(after.projections[order.order_id]),
                payment_captured_paise_before=_captured_paise(before.projections[order.order_id], order.payment_id),
                payment_captured_paise_after=_captured_paise(after.projections[order.order_id], order.payment_id),
            )
            for order in orders
        ),
        duplicate_fulfilments=sum(rows_by_order[order.order_id] > 1 for order in orders),
        payments_fulfilling_several_orders=tuple(sorted(
            {order.payment_id for order in orders if rows_by_payment[order.payment_id] > 1}
        )),
        candidates_recorded=candidates_recorded,
        outstanding=outstanding,
    )


def _order_captures(projection: state.OrderProjection) -> int:
    return sum(payment.has_capture_evidence for payment in projection.payments)


def _captured_paise(projection: state.OrderProjection, payment_id: str) -> int | None:
    payment = next((item for item in projection.payments if item.payment_id == payment_id), None)
    if payment is None or not payment.has_capture_evidence:
        return 0
    return payment.amount_paise  # None when its observations disagree on the amount


def _amounts(
    conn: sqlite3.Connection, plan: plans.StoredPlan, result: ExecutionResult, *, change: metrics.CaptureChange,
    outstanding: tuple[OutstandingOrder, ...],
) -> ExecutionAmounts:
    valued, unvalued = 0, []
    for item in outstanding:
        order = storage.get_order(conn, run_id=plan.run_id, order_id=item.order_id)
        if order is None or order.currency != SUPPORTED_ORDER_CURRENCY:
            unvalued.append(item.order_id)
            continue
        valued += order.amount_paise
    return ExecutionAmounts(
        currency=SUPPORTED_ORDER_CURRENCY,
        restored_processing_value_paise=result.restored_processing_value_paise,
        additional_captured_paise=change.additional_captured_paise,
        unresolved_value_paise=valued,
        unresolved_orders=len(outstanding),
        unvalued_unresolved_order_ids=tuple(sorted(unvalued)),
    )


def _target_snapshot(conn: sqlite3.Connection, plan: plans.StoredPlan) -> list[dict[str, JsonValue]]:
    snapshot: list[dict[str, JsonValue]] = []
    for target in plan.reprocess_targets():
        row = storage.get_inbox_row(conn, run_id=plan.run_id, inbox_id=target.inbox_id)
        order = storage.get_order(conn, run_id=plan.run_id, order_id=target.id)
        snapshot.append(
            {
                "order_id": target.id,
                "inbox_id": target.inbox_id,
                "inbox_status": None if row is None else row.processing_status.value,
                "order_fulfilment_state": None if order is None else order.fulfilment_state,
                "amount_paise": target.amount_paise,
            }
        )
    return snapshot


def _reject(
    conn: sqlite3.Connection,
    plan: plans.StoredPlan,
    *,
    key: str,
    failures: list[policy.PreconditionFailure],
    now_ms: int,
) -> ExecutionResult:
    if not failures:  # a plan with no fix validation cannot reach here without a recorded failure
        failures = [
            policy.PreconditionFailure(code=policy.RecheckFailure.FIX_NOT_VALIDATED, detail="no fix validation on the plan")
        ]
    action_id = plans.action_id_for(key)
    plans.insert_action(
        conn,
        run_id=plan.run_id,
        action_id=action_id,
        key=key,
        plan_id=plan.plan_id,
        status=ActionStatus.REJECTED,
        before={"targets": _target_snapshot(conn, plan), "evidence_version": plan.evidence_version},
        after=None,
        error=json.dumps({"failures": [failure.model_dump(mode="json") for failure in failures]}, sort_keys=True),
        executed_at_ms=now_ms,
    )
    plans.set_plan_status(conn, run_id=plan.run_id, plan_id=plan.plan_id, status=PlanStatus.INVALIDATED)
    return ExecutionResult(
        run_id=plan.run_id,
        plan_id=plan.plan_id,
        action_id=action_id,
        operation_key=key,
        status=ActionStatus.REJECTED,
        reasons=tuple(f"{failure.code.value}: {failure.detail}" for failure in failures),
        fulfilled_order_ids=(),
        restored_processing_value_paise=0,
        additional_captured_paise=0,
        executed_at_ms=now_ms,
        replayed=False,
    )


def _apply(
    conn: sqlite3.Connection,
    plan: plans.StoredPlan,
    *,
    key: str,
    targets: tuple[plans.PlanTarget, ...],
    recheck: policy.RecheckOutcome,
    corrected: merchant.DeployedMerchant,
    now_ms: int,
    capture_before: metrics.CaptureLedger,  # read in this transaction before any write
) -> ExecutionResult:
    action_id = plans.action_id_for(key)
    before: dict[str, JsonValue] = {
        "targets": _target_snapshot(conn, plan),
        "evidence_version": plan.evidence_version,
        "corrected_version": corrected.version,
        "captured_paise": capture_before.captured_paise,
    }
    # The action row comes first: each fulfilment references it.
    plans.insert_action(
        conn, run_id=plan.run_id, action_id=action_id, key=key, plan_id=plan.plan_id, status=ActionStatus.SUCCEEDED,
        before=before, after=None, error=None, executed_at_ms=now_ms,
    )
    for target in targets:
        row = storage.get_inbox_row(conn, run_id=plan.run_id, inbox_id=target.inbox_id)
        if row is None:
            raise ScopeError(f"inbox row {target.inbox_id} vanished inside the execution transaction")
        storage.insert_fulfilment(
            conn,
            storage.FulfilmentRecord(
                run_id=plan.run_id, order_id=target.id, payment_id=target.payment_id, amount_paise=target.amount_paise,
                currency=target.currency, fulfilled_at_ms=now_ms, via="reprocess", action_id=action_id,
            ),
        )
        storage.mark_order_fulfilled(conn, run_id=plan.run_id, order_id=target.id)
        storage.record_inbox_processing(
            conn, run_id=plan.run_id, inbox_id=target.inbox_id, status=InboxStatus.PROCESSED,
            attempts=row.attempts + 1, last_error=None, processed_at_ms=now_ms,
        )
    capture_after = _capture_ledger(conn, run_id=plan.run_id, as_of_ms=now_ms)
    change = metrics.capture_change(capture_before, capture_after)
    known_at = {assessment.order_id: assessment.capture_known_at_ms for assessment in recheck.assessments}
    restored = metrics.restored_processing_value(
        (metrics.ProcessedOrder(order_id=target.id, amount_paise=target.amount_paise, capture_known_at_ms=known_at[target.id])
         for target in targets),
        intervention_at_ms=now_ms,
    )
    fulfilled = tuple(target.id for target in targets)
    after: dict[str, JsonValue] = {
        "fulfilled_order_ids": list(fulfilled),
        "processed_inbox_ids": [target.inbox_id for target in targets],
        "restored_processing_value_paise": restored,
        "additional_captured_paise": change.additional_captured_paise,
        "captured_paise": capture_after.captured_paise,
    }
    conn.execute(
        "UPDATE actions SET \"after\" = ? WHERE run_id = ? AND action_id = ?",
        (json.dumps(after, sort_keys=True), plan.run_id, action_id),
    )
    plans.set_plan_status(conn, run_id=plan.run_id, plan_id=plan.plan_id, status=PlanStatus.EXECUTED, expected=PlanStatus.APPROVED)
    return ExecutionResult(
        run_id=plan.run_id,
        plan_id=plan.plan_id,
        action_id=action_id,
        operation_key=key,
        status=ActionStatus.SUCCEEDED,
        reasons=(),
        fulfilled_order_ids=fulfilled,
        restored_processing_value_paise=restored,
        additional_captured_paise=change.additional_captured_paise,
        executed_at_ms=now_ms,
        replayed=False,
    )


def _capture_ledger(conn: sqlite3.Connection, *, run_id: str, as_of_ms: int) -> metrics.CaptureLedger:
    """Captured and refunded value over every payment of the run, as of ``as_of_ms``."""
    by_payment: defaultdict[str, list[PaymentObservation]] = defaultdict(list)
    for observation in storage.list_observations(conn, run_id=run_id, as_of_ms=as_of_ms):
        by_payment[observation.payment_id].append(observation)
    return metrics.capture_ledger(
        state.project_payment(items, as_of_ms, payment_id=payment_id) for payment_id, items in by_payment.items()
    )


# --------------------------------------------------------------------------- recorded actions


class StoredAction(StrictModel):
    run_id: str
    action_id: str
    operation_key: str
    plan_id: str
    status: ActionStatus
    before: JsonValue
    after: JsonValue
    error: str | None
    executed_at_ms: int


def _action_from_row(row: sqlite3.Row) -> StoredAction:
    return StoredAction(
        run_id=row["run_id"],
        action_id=row["action_id"],
        operation_key=row["operation_key"],
        plan_id=row["plan_id"],
        status=ActionStatus(row["status"]),
        before=None if row["before"] is None else json.loads(str(row["before"])),
        after=None if row["after"] is None else json.loads(str(row["after"])),
        error=row["error"],
        executed_at_ms=row["executed_at_ms"],
    )


def _action_by_key(conn: sqlite3.Connection, *, run_id: str, key: str) -> StoredAction | None:
    row = conn.execute("SELECT * FROM actions WHERE operation_key = ?", (key,)).fetchone()
    if row is None:
        return None
    if row["run_id"] != run_id:
        raise ScopeError(f"operation key {key!r} belongs to another run")
    return _action_from_row(row)


def get_action(conn: sqlite3.Connection, *, run_id: str, action_id: str) -> StoredAction | None:
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        row = conn.execute("SELECT * FROM actions WHERE run_id = ? AND action_id = ?", (run_id, action_id)).fetchone()
    return None if row is None else _action_from_row(row)


def get_action_audit(conn: sqlite3.Connection, *, run_id: str, action_id: str) -> ActionAudit | None:
    """The audit the execution recorded; None for an action recorded before migration 3, or an escalation."""
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        record = storage.get_action_audit(conn, run_id=run_id, action_id=action_id)
    return None if record is None else ActionAudit.model_validate(record.model_dump(mode="json", exclude={"run_id"}))


def list_actions(conn: sqlite3.Connection, *, run_id: str, plan_id: str | None = None) -> tuple[StoredAction, ...]:
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        rows = conn.execute(
            "SELECT * FROM actions WHERE run_id = ? AND (? IS NULL OR plan_id = ?) ORDER BY executed_at_ms, action_id",
            (run_id, plan_id, plan_id),
        ).fetchall()
    return tuple(_action_from_row(row) for row in rows)


def _result_from_action(action: StoredAction, plan: plans.StoredPlan, *, replayed: bool) -> ExecutionResult:
    after = action.after if isinstance(action.after, dict) else {}
    reasons: tuple[str, ...] = ()
    if action.status is ActionStatus.REJECTED and action.error:
        reasons = tuple(f"{item['code']}: {item['detail']}" for item in json.loads(action.error)["failures"])
    return ExecutionResult(
        run_id=action.run_id,
        plan_id=plan.plan_id,
        action_id=action.action_id,
        operation_key=action.operation_key,
        status=action.status,
        reasons=reasons,
        fulfilled_order_ids=tuple(str(order_id) for order_id in after.get("fulfilled_order_ids", [])),
        restored_processing_value_paise=int(after.get("restored_processing_value_paise", 0)),
        additional_captured_paise=int(after.get("additional_captured_paise", 0)),
        executed_at_ms=action.executed_at_ms,
        replayed=replayed,
    )
