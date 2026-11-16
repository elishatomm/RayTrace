// The recovery the operator carries out for the latest diagnosis job, and the six-step rail the run header shows for
// it. The rail invents nothing: each step's state and proof come from an API answer this page received for that
// diagnosis, and a step the diagnosis's path does not use reads "Not applicable" rather than looking skipped or done.
import { receiptLink } from "./api";
import { MODE_LABELS, countOf, hashPrefix, replayTime } from "./format";
import type {
  Approval,
  DiagnosisDocument,
  DiagnosisJob,
  EscalationRecord,
  ExecutionResult,
  FixValidation,
  Operation,
  PlanPreview,
  RefreshSummary,
  ReplayInstructions,
} from "./types";

// One click of Execute; the sequence (1, 2, ...) is a stable list key, assigned when the result arrives.
export interface ExecutionEntry {
  sequence: number;
  result: ExecutionResult;
}

// What the Recovery panel's requests answered for one diagnosis. App keeps one per diagnosis ID, so a new diagnosis
// starts from NO_PROGRESS, as the panel did when it held this state itself.
export interface RecoveryProgress {
  validation: FixValidation | null;
  plan: PlanPreview | null;
  draftId: string | null; // the unapproved plan the current plan replaced
  refreshed: RefreshSummary | null;
  approval: Approval | null;
  executions: ExecutionEntry[];
  escalation: EscalationRecord | null;
  replay: ReplayInstructions | null;
}

export const NO_PROGRESS: RecoveryProgress = {
  validation: null,
  plan: null,
  draftId: null,
  refreshed: null,
  approval: null,
  executions: [],
  escalation: null,
  replay: null,
};

// The executions of the plan on screen. A recovery keeps every execution answer it received, across re-validations
// and re-plans, because the money strip reports the latest of them; Execute's label, the execution list and the
// rail's Execute and Receipt steps describe the current plan only.
export function executionsOfPlan(progress: RecoveryProgress): ExecutionEntry[] {
  const planId = progress.plan?.plan_id;
  return planId === undefined ? [] : progress.executions.filter((entry) => entry.result.plan_id === planId);
}

// The receipt of the latest execution: requested once the execution answers, read for its hash.
export type ReceiptLoad =
  | { status: "loading" }
  | { status: "loaded"; hash: string; definitions: { restored: string | null; newCapture: string | null } }
  | { status: "failed"; reason: string };

// The operation the Recovery panel acts on: a validated diagnosis's proposal, and none when it needs review.
export function proposedOperation(diagnosis: DiagnosisDocument): Operation | null {
  return diagnosis.status === "validated" ? diagnosis.output?.proposed_operation ?? null : null;
}

export type RecoveryPath = "undecided" | "reprocess" | "refresh" | "replay" | "escalate" | "no_action" | "needs_review";

const PATH_OF_OPERATION: Record<Operation, RecoveryPath> = {
  reprocess_inbox: "reprocess",
  refresh_provider_state: "refresh",
  prepare_provider_replay: "replay",
  escalate: "escalate",
  no_action: "no_action",
};

export function recoveryPath(job: DiagnosisJob | null): RecoveryPath {
  const diagnosis = job?.diagnosis ?? null;
  if (diagnosis === null) {
    return "undecided";
  }
  const operation = proposedOperation(diagnosis);
  return operation === null ? "needs_review" : PATH_OF_OPERATION[operation];
}

// The path in words: the operation the diagnosis proposes (null when nothing is proposed) and what that path uses.
export const PATH_SUMMARIES: Record<RecoveryPath, { operation: string | null; summary: string }> = {
  undecided: { operation: null, summary: "set by the diagnosis" },
  reprocess: { operation: "reprocess_inbox", summary: "validate a fix, plan, approve, execute, receipt" },
  refresh: {
    operation: "refresh_provider_state",
    summary: "a read-only refresh; no fix, plan, approval, execution or receipt applies",
  },
  replay: {
    operation: "prepare_provider_replay",
    summary: "instructions only, no provider call; no fix, approval, execution or receipt applies",
  },
  escalate: {
    operation: "escalate",
    summary: "hand over with the evidence needed; no fix, plan, approval, execution or receipt applies",
  },
  no_action: { operation: "no_action", summary: "the diagnosis found no incident; nothing is changed" },
  needs_review: { operation: null, summary: "needs review: nothing is proposed; escalate with the deterministic evidence" },
};

// "review": the step ran, and its answer needs a person (a diagnosis with no valid answer); not done, not failed.
export type StepState = "done" | "current" | "pending" | "skipped" | "failed" | "review";
type StepNumber = 1 | 2 | 3 | 4 | 5 | 6;

const STEP_NAMES: Record<StepNumber, string> = {
  1: "Diagnose",
  2: "Validate fix",
  3: "Plan",
  4: "Approve",
  5: "Execute",
  6: "Receipt",
};

// One line of a step's proof: an ID or hash (code), or words. A hash shows its first 12 digits (truncated).
export interface ProofLine {
  label: string | null;
  value: string;
  code: boolean;
  truncated: boolean;
}

export interface RailStep {
  number: StepNumber;
  name: string;
  state: StepState;
  proof: ProofLine[];
  receiptHref: string | null; // the receipt's link, once an execution names its action
}

export interface RecoveryRail {
  path: RecoveryPath;
  steps: RailStep[];
  next: string; // the next safe step, in words
}

const words = (value: string): ProofLine => ({ label: null, value, code: false, truncated: false });
const id = (value: string, label: string | null = null): ProofLine => ({ label, value, code: true, truncated: false });
const hash = (value: string, label: string): ProofLine => ({ label, value: hashPrefix(value), code: true, truncated: true });

function railStep(number: StepNumber, state: StepState, proof: ProofLine[] = [], receiptHref: string | null = null): RailStep {
  return { number, name: STEP_NAMES[number], state, proof, receiptHref };
}

const LATER_STEPS: StepNumber[] = [2, 3, 4, 5, 6];

function diagnoseStep(job: DiagnosisJob | null, canDiagnose: boolean): RailStep {
  if (job === null) {
    return railStep(1, canDiagnose ? "current" : "pending");
  }
  if (job.status === "queued" || job.status === "running") {
    return railStep(1, "current", [id(job.job_id, "job"), words(job.status)]);
  }
  if (job.status === "failed") {
    return railStep(1, "failed", [id(job.job_id, "job"), words("could not run")]);
  }
  const diagnosis = job.diagnosis;
  if (diagnosis === null) {
    return railStep(1, "failed", [id(job.job_id, "job"), words("done without a diagnosis")]);
  }
  if (proposedOperation(diagnosis) === null) {
    return railStep(1, "review", [words(MODE_LABELS[diagnosis.mode]), words("needs review"), hash(diagnosis.packet_hash, "packet")]);
  }
  return railStep(1, "done", [words(MODE_LABELS[diagnosis.mode]), hash(diagnosis.packet_hash, "packet")]);
}

function planProof(planId: string, targets: number, planHash: string): ProofLine[] {
  return [id(planId), words(countOf(targets, "target order")), hash(planHash, "hash")];
}

function validateStep(validation: FixValidation | null): RailStep {
  if (validation === null) {
    return railStep(2, "current");
  }
  return validation.passed
    ? railStep(2, "done", [id(validation.fix_id), words("passed")])
    : railStep(2, "failed", [id(validation.fix_id), words("did not pass")]);
}

function planStep(validation: FixValidation | null, plan: PlanPreview | null): RailStep {
  if (plan !== null) {
    return railStep(3, "done", planProof(plan.plan_id, plan.target_count, plan.plan_hash));
  }
  return railStep(3, validation?.passed === true ? "current" : "pending");
}

function approveStep(plan: PlanPreview | null, approval: Approval | null): RailStep {
  if (plan === null) {
    return railStep(4, "pending");
  }
  if (plan.target_count === 0) {
    return railStep(4, "skipped");
  }
  if (approval === null) {
    return railStep(4, "current");
  }
  return railStep(4, "done", [id(approval.operator_id, "by"), hash(approval.plan_hash, "hash")]);
}

function executeStep(plan: PlanPreview | null, approval: Approval | null, executions: ExecutionEntry[]): RailStep {
  if (plan !== null && plan.target_count === 0) {
    return railStep(5, "skipped");
  }
  const latest = executions.at(-1)?.result ?? null;
  if (approval === null) {
    return railStep(5, "pending");
  }
  if (latest === null) {
    return railStep(5, "current");
  }
  const proof = [id(latest.action_id), words(latest.status)];
  if (executions.some((entry) => entry.result.replayed)) {
    proof.push(words("replayed"));
  }
  return railStep(5, latest.status === "succeeded" ? "done" : "failed", proof);
}

function receiptStep(runId: string, plan: PlanPreview | null, latest: ExecutionResult | null, receipt: ReceiptLoad | null): RailStep {
  if (plan !== null && plan.target_count === 0) {
    return railStep(6, "skipped");
  }
  if (latest === null) {
    return railStep(6, "pending");
  }
  const href = receiptLink(runId, latest.action_id, "json");
  if (receipt === null) {
    return railStep(6, "current", [], href);
  }
  switch (receipt.status) {
    case "loading":
      return railStep(6, "current", [words("loading")], href);
    case "failed":
      return railStep(6, "failed", [words("not loaded")], href);
    case "loaded":
      return railStep(6, "done", [hash(receipt.hash, "hash")], href);
  }
}

function laterSteps(path: RecoveryPath, runId: string, progress: RecoveryProgress, receipt: ReceiptLoad | null): RailStep[] {
  switch (path) {
    case "undecided":
      return LATER_STEPS.map((number) => railStep(number, "pending"));
    case "reprocess": {
      const { validation, plan, approval } = progress;
      const executions = executionsOfPlan(progress);
      return [
        validateStep(validation),
        planStep(validation, plan),
        approveStep(plan, approval),
        executeStep(plan, approval, executions),
        receiptStep(runId, plan, executions.at(-1)?.result ?? null, receipt),
      ];
    }
    case "replay": {
      const replay = progress.replay;
      const preparePlan = replay === null
        ? railStep(3, "current")
        : railStep(3, "done", planProof(replay.plan.plan_id, replay.plan.targets.length, replay.plan.plan_hash));
      return [railStep(2, "skipped"), preparePlan, railStep(4, "skipped"), railStep(5, "skipped"), railStep(6, "skipped")];
    }
    case "refresh":
    case "escalate":
    case "no_action":
    case "needs_review":
      return LATER_STEPS.map((number) => railStep(number, "skipped"));
  }
}

const escalated = (record: EscalationRecord): string => `Escalated as action ${record.action_id} by ${record.operator_id}.`;

function undecidedNext(job: DiagnosisJob | null, canDiagnose: boolean): string {
  if (job === null) {
    return canDiagnose
      ? "Diagnose the latest incident in the Diagnosis card."
      : "No detector has opened an incident by this replay time.";
  }
  if (job.status === "queued" || job.status === "running") {
    return "Wait for the diagnosis to finish.";
  }
  return "The diagnosis could not run; the Diagnosis card says why. Run it again.";
}

function reprocessNext(progress: RecoveryProgress, receipt: ReceiptLoad | null): string {
  const { validation, plan, approval } = progress;
  const latest = executionsOfPlan(progress).at(-1)?.result ?? null;
  if (validation === null) {
    return "Validate a corrected handler in the Recovery card.";
  }
  if (!validation.passed) {
    return `Validate another corrected handler: ${validation.fix_id} did not pass.`;
  }
  if (plan === null) {
    return "Build the reprocessing plan.";
  }
  if (plan.target_count === 0) {
    return "Nothing to approve: the plan has no target orders.";
  }
  if (approval === null) {
    return "Check the plan's exact target rows, then approve it.";
  }
  if (latest === null) {
    return "Execute the approved plan.";
  }
  if (latest.status !== "succeeded") {
    return `Action ${latest.action_id} was rejected; its reasons are in the Recovery card.`;
  }
  if (receipt?.status === "failed") {
    return "The receipt did not load; open it from its link.";
  }
  return receipt?.status === "loaded"
    ? `Action ${latest.action_id} succeeded and its receipt is recorded.`
    : "Loading the receipt.";
}

function nextSafeStep(
  path: RecoveryPath, job: DiagnosisJob | null, canDiagnose: boolean, progress: RecoveryProgress, receipt: ReceiptLoad | null,
): string {
  const { escalation, refreshed, replay } = progress;
  switch (path) {
    case "undecided":
      return undecidedNext(job, canDiagnose);
    case "reprocess":
      return reprocessNext(progress, receipt);
    case "refresh":
      if (escalation !== null) {
        return escalated(escalation);
      }
      return refreshed === null
        ? "Refresh provider state (read-only) in the Recovery card."
        : `Refreshed ${countOf(refreshed.refreshed.length, "payment")} at replay time ${replayTime(refreshed.now_ms)}.`;
    case "replay":
      if (escalation !== null) {
        return escalated(escalation);
      }
      return replay === null
        ? "Prepare provider replay instructions (no provider call) in the Recovery card."
        : `Instructions prepared: ${countOf(replay.instructions.length, "line")}; provider calls made: ${replay.provider_calls_made}.`;
    case "escalate":
      return escalation === null ? "Escalate with the evidence needed in the Recovery card." : escalated(escalation);
    case "no_action":
      return "Nothing to do: the diagnosis found no incident; nothing is changed.";
    case "needs_review":
      return escalation === null ? "Escalate with the deterministic evidence in the Recovery card." : escalated(escalation);
  }
}

export interface RailInput {
  runId: string;
  job: DiagnosisJob | null;
  canDiagnose: boolean; // the run has an incident to diagnose
  progress: RecoveryProgress; // the latest diagnosis's recovery (NO_PROGRESS without one)
  receipt: ReceiptLoad | null; // the receipt of the current plan's latest execution
}

export function deriveRail({ runId, job, canDiagnose, progress, receipt }: RailInput): RecoveryRail {
  const path = recoveryPath(job);
  return {
    path,
    steps: [diagnoseStep(job, canDiagnose), ...laterSteps(path, runId, progress, receipt)],
    next: nextSafeStep(path, job, canDiagnose, progress, receipt),
  };
}
