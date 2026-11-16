"""Preconditions for recovery, independent of model output.

Two layers:

* the phase-1 spike port: ``failed_preconditions``, a pure function over small frozen inputs;
* the M2 rules that planning and execution share, so the two cannot drift apart: who counts as a
  human operator, how one reprocessing target is judged eligible, the evidence fingerprint
  (``evidence_version``), the SPEC.md §10 plan hash, and ``recheck_reprocess``, which runs inside
  the executor's ``BEGIN IMMEDIATE`` transaction so nothing can change between check and effect.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from pydantic import JsonValue

from recovery_workbench import ingestion, state, storage
from recovery_workbench.contracts import (
    ContractError,
    ExclusionReason,
    InboxStatus,
    Operation,
    PaymentStatus,
    PlanStatus,
    StrictModel,
    canonical_json,
    sha256_hex,
)

# v1: the phase-1 pure checks. v2 adds the transactional reprocess recheck below. v3 (owner
# decision Q8, 2026-11-12) stops excluding late authorisation: a payment that failed and was
# captured later is a normal target when it is its order's only capture. Plans built under one
# version are refused for execution under another.
POLICY_VERSION = "policy-v3"


class ApproverKind(StrEnum):
    HUMAN = "human"
    MODEL = "model"  # representable only so the policy can refuse it


class FailedPrecondition(StrEnum):
    APPROVER_NOT_HUMAN = "approver_not_human"
    PLAN_HASH_MISMATCH = "plan_hash_mismatch"
    RUN_MISMATCH = "run_mismatch"
    APPROVAL_EXPIRED = "approval_expired"
    EVIDENCE_CHANGED = "evidence_changed"
    SNAPSHOT_STALE = "snapshot_stale"
    NOT_CAPTURED = "not_captured"
    NOT_EXACTLY_ONE_CAPTURE = "not_exactly_one_capture"
    WORKER_UNHEALTHY = "worker_unhealthy"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REFUNDED = "order_refunded"


@dataclass(frozen=True)
class PlanFacts:
    plan_hash: str
    run_id: str
    evidence_version: str


@dataclass(frozen=True)
class ApprovalFacts:
    approver: ApproverKind
    plan_hash: str
    run_id: str
    expires_at_ms: int


@dataclass(frozen=True)
class CurrentFacts:
    """State read at execution time, not at approval time."""

    run_id: str
    evidence_version: str
    snapshot_age_ms: int
    status: PaymentStatus
    captures_for_order: int
    worker_healthy: bool
    cancelled: bool
    refunded: bool


@dataclass(frozen=True)
class PolicySettings:
    max_snapshot_age_ms: int = 30_000


DEFAULT_POLICY = PolicySettings()


def failed_preconditions(
    plan: PlanFacts,
    approval: ApprovalFacts,
    current: CurrentFacts,
    now_ms: int,
    settings: PolicySettings = DEFAULT_POLICY,
) -> tuple[FailedPrecondition, ...]:
    """Every failed precondition, in declaration order; empty means allowed."""
    checks = {
        FailedPrecondition.APPROVER_NOT_HUMAN: approval.approver is not ApproverKind.HUMAN,
        FailedPrecondition.PLAN_HASH_MISMATCH: approval.plan_hash != plan.plan_hash,
        FailedPrecondition.RUN_MISMATCH: not (approval.run_id == plan.run_id == current.run_id),
        FailedPrecondition.APPROVAL_EXPIRED: approval.expires_at_ms <= now_ms,
        FailedPrecondition.EVIDENCE_CHANGED: plan.evidence_version != current.evidence_version,
        FailedPrecondition.SNAPSHOT_STALE: current.snapshot_age_ms > settings.max_snapshot_age_ms,
        FailedPrecondition.NOT_CAPTURED: current.status is not PaymentStatus.CAPTURED,
        FailedPrecondition.NOT_EXACTLY_ONE_CAPTURE: current.captures_for_order != 1,
        FailedPrecondition.WORKER_UNHEALTHY: not current.worker_healthy,
        FailedPrecondition.ORDER_CANCELLED: current.cancelled,
        FailedPrecondition.ORDER_REFUNDED: current.refunded,
    }
    return tuple(precondition for precondition, failed in checks.items() if failed)


# --------------------------------------------------------------------------- operators and replay time

# Operator sessions issue IDs of this shape. A model, agent or tool never holds one, so an approval
# or a fix validation recorded under any other ID is not a human's. (The approvals table has no
# approver-kind column; the ID carries it until one is added.)
HUMAN_OPERATOR_ID = re.compile(r"op_[a-z0-9][a-z0-9_]{0,62}")


def operator_kind(operator_id: str) -> ApproverKind:
    return ApproverKind.HUMAN if HUMAN_OPERATOR_ID.fullmatch(operator_id) else ApproverKind.MODEL


def require_replay_time(conn: sqlite3.Connection, run_id: str, now_ms: int) -> storage.RunRecord:
    """The run, after checking ``now_ms`` is valid replay time that does not precede the run's clock.

    Expiry is judged at ``now_ms``; refusing a time behind the clock means a caller cannot
    revive an expired plan or approval by passing an older time.
    """
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ContractError(f"now_ms must be a non-negative integer of replay time, got {now_ms!r}")
    run = storage.require_run(conn, run_id)
    if now_ms < run.clock_ms:
        raise ContractError(
            f"replay time {now_ms} is before run {run_id!r}'s clock {run.clock_ms}; replay time cannot go back"
        )
    return run


# --------------------------------------------------------------------------- reprocessing targets

UNPROCESSED_INBOX_STATUSES = frozenset({InboxStatus.PENDING, InboxStatus.FAILED})
CAPTURE_EVENT_TYPE = "payment.captured"


class PlanExclusion(StrEnum):
    """Reasons a reprocessing candidate is not a target, beyond ``contracts.ExclusionReason``."""

    TARGET_NOT_THE_CAPTURE = "target_not_the_capture"  # the event's payment is not the order's single capture
    NOT_A_CAPTURE_EVENT = "not_a_capture_event"
    INBOX_ROW_SETTLED = "inbox_row_settled"  # processed or held for review since the plan was built
    DRY_RUN_RAISED = "dry_run_raised"
    DRY_RUN_NO_FULFILMENT = "dry_run_no_fulfilment"
    DRY_RUN_MISMATCH = "dry_run_mismatch"


@dataclass(frozen=True)
class TargetRef:
    """One target as the stored plan holds it. The executor fulfils from these fields, and the plan hash binds only
    the order IDs, so the recheck compares the rest with the target's event (``_stored_differences``)."""

    order_id: str
    inbox_id: int
    event_id: str
    payment_id: str
    amount_paise: int
    currency: str


class TargetAssessment(StrictModel):
    """One candidate inbox event judged against a fresh order projection (no dry run)."""

    order_id: str
    inbox_id: int
    event_id: str
    payment_id: str
    amount_paise: int
    currency: str
    reasons: tuple[str, ...]  # ExclusionReason and PlanExclusion values; empty means eligible
    fingerprint: dict[str, JsonValue]  # the state evidence_version covers
    capture_known_at_ms: int | None  # when capture evidence for this payment was first known

    @property
    def eligible(self) -> bool:
        return not self.reasons


def assess_target(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    inbox_row: storage.InboxRecord,
    now_ms: int,
    stale_after_ms: int,
) -> TargetAssessment:
    """Judge one inbox event as a reprocessing target, from a fresh projection of its order and the event's own
    amount and currency.

    Uses ``read_transaction`` underneath, so inside the executor's write transaction it reads the
    same locked snapshot the effect will be applied to.
    """
    # Verified and schema-checked on receipt; a parse failure here would mean a corrupted row.
    event = ingestion.parse_payment_event(inbox_row.raw_body)
    payment = event.payment
    projection = state.load_order_projection(conn, run_id, payment.order_id, now_ms, stale_after_ms=stale_after_ms)
    found = set(projection.exclusions) | mismatches_with_order(payment.amount, payment.currency, projection.order)
    reasons = [reason.value for reason in ExclusionReason if reason in found]
    if event.event != CAPTURE_EVENT_TYPE:
        reasons.append(PlanExclusion.NOT_A_CAPTURE_EVENT.value)
    if inbox_row.processing_status not in UNPROCESSED_INBOX_STATUSES:
        reasons.append(PlanExclusion.INBOX_ROW_SETTLED.value)
    captured_id = projection.single_captured_payment_id
    if captured_id is not None and captured_id != payment.id:
        reasons.append(PlanExclusion.TARGET_NOT_THE_CAPTURE.value)
    # A late authorisation alone is no exclusion (policy-v3): its capture is real money already
    # collected, so fulfilling the order restores processing without charging anyone again. A
    # customer who retried on the same order after a reported failure leaves a second capture,
    # which projection.exclusions reports as multiple_captures and keeps out of every plan.
    target_payment = next((item for item in projection.payments if item.payment_id == payment.id), None)
    capture_times = (
        []
        if target_payment is None
        else [
            observation.known_at_ms
            for observation in target_payment.history
            if observation.status in (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED)
        ]
    )
    return TargetAssessment(
        order_id=payment.order_id,
        inbox_id=inbox_row.inbox_id,
        event_id=inbox_row.event_id,
        payment_id=payment.id,
        amount_paise=payment.amount,
        currency=payment.currency,
        reasons=tuple(reasons),
        fingerprint=_fingerprint(inbox_row, payment.id, projection),
        capture_known_at_ms=min(capture_times, default=None),
    )


def mismatches_with_order(
    amount_paise: int, currency: str, order: storage.OrderRecord | None
) -> frozenset[ExclusionReason]:
    """Where a capture event's own amount or currency is not its order's; an unknown order is excluded anyway.

    Reprocessing an event fulfils its order for the event's amount and currency. The order projection compares the
    order with every capture it can see, but an event's own observation is visible only from its envelope time on,
    which the signed body sets: an event dated after its receipt is missing from the projection until then. So the
    event itself is compared with the order, as the worker compares a handler's request with it.
    """
    if order is None:
        return frozenset()
    checks = {
        ExclusionReason.AMOUNT_MISMATCH: amount_paise != order.amount_paise,
        ExclusionReason.CURRENCY_MISMATCH: currency != order.currency,
    }
    return frozenset(reason for reason, differs in checks.items() if differs)


def _fingerprint(
    row: storage.InboxRecord, payment_id: str, projection: state.OrderProjection
) -> dict[str, JsonValue]:
    """The facts a reprocessing effect depends on, in plain JSON.

    Semantic state, not observation IDs: a later snapshot confirming the same capture leaves the
    fingerprint unchanged, while a refund, cancellation, new conflict or fulfilment changes it.
    Freshness is checked separately at execution, because it depends on time, not evidence.
    """
    order = projection.order
    fulfilment = projection.fulfilment
    return {
        "order_id": projection.order_id,
        "inbox_id": row.inbox_id,
        "event_id": row.event_id,
        "body_sha256": row.body_sha256,
        "inbox_unprocessed": row.processing_status in UNPROCESSED_INBOX_STATUSES,
        "payment_id": payment_id,
        "order": None
        if order is None
        else {
            "amount_paise": order.amount_paise,
            "currency": order.currency,
            "cancelled_at_ms": order.cancelled_at_ms,
            "refunded": order.refunded,
            "fulfilment_state": order.fulfilment_state,
        },
        "payments": [
            {
                "payment_id": payment.payment_id,
                "status": payment.status.value,
                "amount_paise": payment.amount_paise,
                "currency": payment.currency,
                "conflicts": [reason.value for reason in payment.conflict_reasons],
                "late_authorization": payment.late_authorization,
                "amount_refunded_paise": payment.amount_refunded_paise,
                "order_ids": list(payment.order_ids),
            }
            for payment in projection.payments
        ],
        "conflicting_delivery_ids": list(projection.conflicting_delivery_ids),
        "fulfilment": None if fulfilment is None else {"payment_id": fulfilment.payment_id, "via": fulfilment.via},
    }


def evidence_version(assessments: Iterable[TargetAssessment]) -> str:
    """Canonical hash of every target's fingerprint, independent of the order they are listed in."""
    fingerprints = sorted(
        (assessment.fingerprint for assessment in assessments),
        key=lambda fingerprint: (str(fingerprint["order_id"]), str(fingerprint["inbox_id"])),
    )
    return sha256_hex(canonical_json({"targets": fingerprints}))


def compute_plan_hash(
    *,
    operation: Operation,
    target_ids: Iterable[str],
    evidence_version: str,
    policy_version: str,
    run_id: str,
    expires_at_ms: int,
) -> str:
    """SHA256(canonical operation + sorted target IDs + evidence_version + policy_version + run_id + expiry).

    SPEC.md §10, serialised with ``contracts.canonical_json``. Target IDs are a set: listing order
    never changes the hash, and a repeated ID is refused rather than silently collapsed.
    """
    ordered = sorted(target_ids)
    if len(ordered) != len(set(ordered)):
        raise ContractError(f"plan target IDs must be unique, got repeats in {ordered}")
    return sha256_hex(
        canonical_json(
            {
                "operation": operation.value,
                "target_ids": ordered,
                "evidence_version": evidence_version,
                "policy_version": policy_version,
                "run_id": run_id,
                "expires_at_ms": expires_at_ms,
            }
        )
    )


# --------------------------------------------------------------------------- the transactional recheck


class RecheckFailure(StrEnum):
    RUN_SCOPE = "run_scope"
    PLAN_NOT_APPROVED = "plan_not_approved"
    PLAN_EXPIRED = "plan_expired"
    PLAN_HASH_MISMATCH = "plan_hash_mismatch"
    POLICY_VERSION_CHANGED = "policy_version_changed"
    APPROVAL_MISSING = "approval_missing"
    APPROVER_NOT_HUMAN = "approver_not_human"
    APPROVAL_HASH_MISMATCH = "approval_hash_mismatch"
    APPROVAL_EXPIRED = "approval_expired"
    FIX_NOT_VALIDATED = "fix_not_validated"
    DEPLOYED_VERSION_CHANGED = "deployed_version_changed"
    TARGET_INELIGIBLE = "target_ineligible"
    EVIDENCE_CHANGED = "evidence_changed"
    PRIOR_ACTION_EXISTS = "prior_action_exists"


# What each recheck guarantees, in operator language; plan previews list these.
RECHECK_DESCRIPTIONS: Mapping[RecheckFailure, str] = MappingProxyType(
    {
        RecheckFailure.RUN_SCOPE: "plan, approval and targets belong to this run",
        RecheckFailure.PLAN_NOT_APPROVED: "the plan is in the approved state",
        RecheckFailure.PLAN_EXPIRED: "the plan has not expired on the replay clock",
        RecheckFailure.PLAN_HASH_MISMATCH: "the stored plan still hashes to the approved plan hash",
        RecheckFailure.POLICY_VERSION_CHANGED: "the policy version is the one the plan was built under",
        RecheckFailure.APPROVAL_MISSING: "an approval exists for this plan",
        RecheckFailure.APPROVER_NOT_HUMAN: "the approver is a human operator",
        RecheckFailure.APPROVAL_HASH_MISMATCH: "the approval names this exact plan hash",
        RecheckFailure.APPROVAL_EXPIRED: "the approval has not expired on the replay clock",
        RecheckFailure.FIX_NOT_VALIDATED: "the operator's fix validation passed",
        RecheckFailure.DEPLOYED_VERSION_CHANGED: "the deployed handler is the one the fix was validated against",
        RecheckFailure.TARGET_INELIGIBLE: (
            "every target is still eligible on a fresh projection: unfulfilled, not cancelled or refunded, "
            "exactly one capture (a second capture blocks) matching the order in amount and currency, no "
            "unresolved attempt, no conflict, nothing stale; a capture that followed a reported failure counts"
        ),
        RecheckFailure.EVIDENCE_CHANGED: "the targets' evidence version equals the plan's",
        RecheckFailure.PRIOR_ACTION_EXISTS: "no action exists yet for this operation key",
    }
)


class PreconditionFailure(StrictModel):
    code: RecheckFailure
    detail: str


class PreconditionCheck(StrictModel):
    """One check the recheck evaluated, with every failure it found; none found means it passed."""

    code: RecheckFailure
    details: tuple[str, ...]  # in the order found

    @property
    def passed(self) -> bool:
        return not self.details


@dataclass(frozen=True)
class ApprovalRecord:
    operator_id: str
    run_id: str
    plan_hash: str
    expires_at_ms: int


@dataclass(frozen=True)
class ReprocessFacts:
    """Everything the executor read inside its write transaction, for one reprocess plan."""

    run_id: str
    plan_run_id: str
    plan_id: str
    plan_status: PlanStatus
    plan_hash: str
    target_refs: tuple[TargetRef, ...]
    evidence_version: str
    policy_version: str
    plan_expires_at_ms: int
    approval: ApprovalRecord | None
    validation_passed: bool | None
    validated_deployed_version: str | None
    deployed_version: str
    prior_action_exists: bool


@dataclass(frozen=True)
class RecheckOutcome:
    failures: tuple[PreconditionFailure, ...]  # in the order found, which is the order a rejected action records
    # One per check evaluated, passed or failed, in RecheckFailure order. A check with nothing to examine is
    # absent: without an approval, the approval's own checks.
    checks: tuple[PreconditionCheck, ...]
    assessments: tuple[TargetAssessment, ...]  # fresh assessments of the plan's targets

    @property
    def passed(self) -> bool:
        return not self.failures


def recheck_reprocess(
    conn: sqlite3.Connection, facts: ReprocessFacts, *, now_ms: int, stale_after_ms: int
) -> RecheckOutcome:
    """Every reprocess precondition, evaluated inside the executor's ``BEGIN IMMEDIATE``.

    Collects all failures rather than stopping at the first, so the rejected action records
    every reason. Also reports every check it evaluated, with its outcome, so nobody has to infer
    from a missing failure that a check passed.
    """
    if not conn.in_transaction:
        raise ContractError("recheck_reprocess must run inside the executor's write transaction")
    evaluated = [*_plan_checks(facts, now_ms), *_approval_checks(facts, now_ms), *_validation_checks(facts)]
    assessments, target_checks = _target_checks(conn, facts, now_ms=now_ms, stale_after_ms=stale_after_ms)
    evaluated.extend(target_checks)
    evaluated.append(_check(RecheckFailure.PRIOR_ACTION_EXISTS, facts.prior_action_exists,
                            f"plan {facts.plan_id!r} already has an action"))
    return RecheckOutcome(
        failures=tuple(PreconditionFailure(code=check.code, detail=detail)
                       for check in evaluated for detail in check.details),
        checks=_by_code(evaluated),
        assessments=tuple(assessments),
    )


def _check(code: RecheckFailure, failed: bool, detail: str) -> PreconditionCheck:
    """One evaluation of a check; ``detail`` describes the failure and is kept only if it failed."""
    return PreconditionCheck(code=code, details=(detail,) if failed else ())


def _by_code(evaluated: Sequence[PreconditionCheck]) -> tuple[PreconditionCheck, ...]:
    """One check per code, in ``RecheckFailure`` order, with every detail in the order found.

    run_scope is evaluated twice when there is an approval, on the plan's run and then the approval's; it passes
    only if both evaluations did.
    """
    details: dict[RecheckFailure, list[str]] = {}
    for check in evaluated:
        details.setdefault(check.code, []).extend(check.details)
    return tuple(PreconditionCheck(code=code, details=tuple(details[code])) for code in RecheckFailure if code in details)


def _plan_checks(facts: ReprocessFacts, now_ms: int) -> list[PreconditionCheck]:
    found = [
        _check(RecheckFailure.RUN_SCOPE, facts.plan_run_id != facts.run_id, f"plan belongs to run {facts.plan_run_id!r}"),
        _check(RecheckFailure.PLAN_NOT_APPROVED, facts.plan_status is not PlanStatus.APPROVED,
               f"plan status is {facts.plan_status.value!r}"),
        _check(RecheckFailure.PLAN_EXPIRED, facts.plan_expires_at_ms <= now_ms,
               f"plan expired at {facts.plan_expires_at_ms}, now {now_ms}"),
        _check(RecheckFailure.POLICY_VERSION_CHANGED, facts.policy_version != POLICY_VERSION,
               f"plan built under {facts.policy_version!r}, now {POLICY_VERSION!r}"),
    ]
    try:
        recomputed = compute_plan_hash(
            operation=Operation.REPROCESS_INBOX,
            target_ids=[ref.order_id for ref in facts.target_refs],
            evidence_version=facts.evidence_version,
            policy_version=facts.policy_version,
            run_id=facts.plan_run_id,
            expires_at_ms=facts.plan_expires_at_ms,
        )
    except ContractError as invalid:  # repeated targets cannot hash; the stored plan was altered
        return [*found, _check(RecheckFailure.PLAN_HASH_MISMATCH, True, str(invalid))]
    return [*found, _check(RecheckFailure.PLAN_HASH_MISMATCH, recomputed != facts.plan_hash,
                           "stored plan fields no longer hash to its plan hash")]


def _approval_checks(facts: ReprocessFacts, now_ms: int) -> list[PreconditionCheck]:
    """That an approval exists, then its own checks; without one, those have nothing to examine and are not run."""
    approval = facts.approval
    exists = _check(RecheckFailure.APPROVAL_MISSING, approval is None, f"plan {facts.plan_id!r} has no approval")
    if approval is None:
        return [exists]
    return [
        exists,
        _check(RecheckFailure.APPROVER_NOT_HUMAN, operator_kind(approval.operator_id) is not ApproverKind.HUMAN,
               f"{approval.operator_id!r} is not a human operator"),
        _check(RecheckFailure.APPROVAL_HASH_MISMATCH, approval.plan_hash != facts.plan_hash,
               "the approval names a different plan hash"),
        _check(RecheckFailure.RUN_SCOPE, approval.run_id != facts.run_id, f"approval belongs to run {approval.run_id!r}"),
        _check(RecheckFailure.APPROVAL_EXPIRED, approval.expires_at_ms <= now_ms,
               f"approval expired at {approval.expires_at_ms}, now {now_ms}"),
    ]


def _validation_checks(facts: ReprocessFacts) -> list[PreconditionCheck]:
    return [
        _check(RecheckFailure.FIX_NOT_VALIDATED, facts.validation_passed is not True,
               "no passing fix validation is attached to the plan"),
        _check(RecheckFailure.DEPLOYED_VERSION_CHANGED, facts.validated_deployed_version != facts.deployed_version,
               f"fix validated against {facts.validated_deployed_version!r}, deployed now {facts.deployed_version!r}"),
    ]


def _target_checks(
    conn: sqlite3.Connection, facts: ReprocessFacts, *, now_ms: int, stale_after_ms: int
) -> tuple[list[TargetAssessment], list[PreconditionCheck]]:
    """The targets' eligibility, one check over all of them (so a plan without targets passes it), then the evidence.

    A target whose stored fields differ from its event's is ineligible too: the executor would fulfil from them.
    """
    assessments: list[TargetAssessment] = []
    ineligible: list[str] = []
    for ref in facts.target_refs:
        row = storage.get_inbox_row(conn, run_id=facts.run_id, inbox_id=ref.inbox_id)
        if row is None:
            ineligible.append(f"{ref.order_id}: inbox row {ref.inbox_id} is not in the run")
            continue
        assessment = assess_target(conn, run_id=facts.run_id, inbox_row=row, now_ms=now_ms, stale_after_ms=stale_after_ms)
        assessments.append(assessment)
        if assessment.order_id != ref.order_id:  # another order's event: comparing its fields would add nothing
            ineligible.append(f"inbox row {ref.inbox_id} names order {assessment.order_id!r}")
            continue
        ineligible.extend(_stored_differences(ref, assessment))
        if not assessment.eligible:
            ineligible.append(f"{ref.order_id}: {', '.join(assessment.reasons)}")
    changed = evidence_version(assessments) != facts.evidence_version
    return assessments, [
        PreconditionCheck(code=RecheckFailure.TARGET_INELIGIBLE, details=tuple(ineligible)),
        _check(RecheckFailure.EVIDENCE_CHANGED, changed, "the targets' state changed since the plan was built"),
    ]


def _stored_differences(ref: TargetRef, assessment: TargetAssessment) -> list[str]:
    """Each field the stored plan holds for a target that is not its event's, in ``TargetRef`` order.

    Neither the plan hash nor the evidence version covers these stored values (the evidence version hashes the event
    itself), so only this comparison stops a fulfilment of a payment, amount or currency the approval never saw.
    """
    return [
        f"{ref.order_id}: plan stores {field} {getattr(ref, field)!r}, its event has {getattr(assessment, field)!r}"
        for field in ("event_id", "payment_id", "amount_paise", "currency")
        if getattr(ref, field) != getattr(assessment, field)
    ]
