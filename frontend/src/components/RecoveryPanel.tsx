import { type Ref, type RefObject, useLayoutEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { handOverFocus } from "../focus";
import { orderAmount, replayTime, rupees } from "../format";
import { type ExecutionEntry, type RecoveryProgress, executionsOfPlan, proposedOperation } from "../recovery";
import type {
  DiagnosisDocument,
  ExecutionResult,
  FixOption,
  LateAuthorisedOrder,
  PaymentState,
  PlanPreview,
} from "../types";
import { RequestButton } from "./BusyButton";

export type Perform = <T>(label: string, action: () => Promise<T>) => Promise<T | undefined>;

interface RecoveryPanelProps {
  runId: string;
  diagnosis: DiagnosisDocument | null;
  fixes: FixOption[];
  paymentState: PaymentState | null;
  progress: RecoveryProgress; // what this diagnosis's recovery requests answered; App keeps it for the rail
  perform: Perform;
  focusHandoff: RefObject<boolean>; // App keys this panel by the diagnosis: true while a replaced panel held focus
  onProgress: (change: (previous: RecoveryProgress) => RecoveryProgress) => void;
  onExecuted: (actionId: string) => void; // an execution answered: App reads that action's receipt
  onChanged: () => void;
}

function unfulfilledStatus(order: LateAuthorisedOrder, planTargets: Set<string>): string {
  if (planTargets.has(order.order_id)) {
    return "in the current plan: its capture is real, so fulfilling it restores processing and charges nobody again";
  }
  if (order.exclusions.length > 0) {
    return `excluded for ${order.exclusions.join(", ")}`;
  }
  return "nothing on its order excludes it from a reprocessing plan";
}

function LateAuthorisations({ state, planTargets }: Readonly<{ state: PaymentState; planTargets: Set<string> }>) {
  const facts = state.late_authorisations;
  if (facts.payments === 0) {
    return <p className="note">No payment failed and then authorised later.</p>;
  }
  return (
    <div className="facts">
      <p>
        {facts.payments} payments first failed and later authorised ({facts.captured} now captured);{" "}
        {facts.fulfilled_orders} of their {facts.orders} orders were already fulfilled.
      </p>
      {facts.would_collect_twice > 0 && (
        <p>
          A "payment failed" recovery (asking the customer to pay again) would have collected twice for{" "}
          {facts.would_collect_twice} of the captured payments.
        </p>
      )}
      {facts.collected_twice.length > 0 && (
        <p>Orders that already hold more than one capture (a person must refund one): {facts.collected_twice.join(", ")}</p>
      )}
      {facts.unfulfilled.length > 0 && (
        <ul className="compact">
          {facts.unfulfilled.map((order) => (
            <li key={order.order_id}>
              Not fulfilled yet: {order.order_id} ({order.payment_ids.join(", ")}): {unfulfilledStatus(order, planTargets)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// A target's amount is asserted: the planner excludes any capture that differs from its order in amount or currency.
function PlanView({ plan, draftId, headingRef }: Readonly<{ plan: PlanPreview; draftId: string | null; headingRef: Ref<HTMLHeadingElement> }>) {
  return (
    <div className="plan">
      <h3 ref={headingRef} tabIndex={-1}>Plan {plan.plan_id}</h3>
      {draftId !== null && <p className="note">Replaces draft {draftId}, which was never approved.</p>}
      <p>
        Hash <code>{plan.plan_hash}</code>; expires at replay time {replayTime(plan.expires_at_ms)}; corrected handler{" "}
        {plan.corrected_version}.
      </p>
      <p className="plan-total">
        <strong>{plan.target_count} target orders, value {rupees(plan.target_value_paise)}</strong> (from{" "}
        {plan.candidate_orders} candidate orders). Dry runs passed: {plan.dry_runs_passed} of {plan.target_count}.
      </p>
      <p className="definition">Plan value: {plan.definitions.plan_value}</p>
      <h4>Exclusions by reason</h4>
      {Object.keys(plan.exclusions_by_reason).length === 0 ? (
        <p>None.</p>
      ) : (
        <ul className="compact">
          {Object.entries(plan.exclusions_by_reason).map(([reason, orders]) => (
            <li key={reason}>
              <strong>{reason}</strong> ({orders.length}): {orders.join(", ")}
            </li>
          ))}
        </ul>
      )}
      <h4>Exact target rows</h4>
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Order</th><th>Payment</th><th className="num">Inbox row</th><th className="num">Amount</th></tr>
          </thead>
          <tbody>
            {plan.targets.map((target) => (
              <tr key={target.id}>
                <td className="mono">{target.id}</td>
                <td className="mono">{target.payment_id}</td>
                <td className="num">{target.inbox_id}</td>
                <td className="num">{orderAmount(target.amount_paise, target.currency, "asserted")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h4>Rechecked inside the execution transaction</h4>
      <ul className="compact">
        {plan.preconditions.map((condition) => (
          <li key={condition}>{condition}</li>
        ))}
      </ul>
    </div>
  );
}

function ExecutionView({ result }: Readonly<{ result: ExecutionResult }>) {
  return (
    <li className={result.status === "succeeded" ? "outcome-complete" : "outcome-failed"}>
      Action {result.action_id}: <strong>{result.status}</strong>
      {result.replayed ? " (replayed: the action was already recorded, no new effects)" : ""}; fulfilled{" "}
      {result.fulfilled_order_ids.length} orders; restored processing value {rupees(result.restored_processing_value_paise)}{" "}
      (already captured before the intervention, not new revenue); new capture {rupees(result.additional_captured_paise)}
      {result.reasons.length > 0 && <>; reasons: {result.reasons.join("; ")}</>}
    </li>
  );
}

export function RecoveryPanel({
  runId, diagnosis, fixes, paymentState, progress, perform, focusHandoff, onProgress, onExecuted, onChanged,
}: Readonly<RecoveryPanelProps>) {
  const [fixId, setFixId] = useState(fixes[0]?.fix_id ?? "");
  const { validation, plan, draftId, refreshed, approval, escalation, replay } = progress;
  const planExecutions = executionsOfPlan(progress); // the stale-orders row, Execute's label and the list are per plan
  const section = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const planHeading = useRef<HTMLHeadingElement>(null);
  const staleRefreshButton = useRef<HTMLButtonElement>(null);
  const approveButton = useRef<HTMLButtonElement>(null);
  const approvalLine = useRef<HTMLParagraphElement>(null);
  const approveHeldFocus = useRef(false); // Approve held focus when its approval arrived
  const openRefreshButton = useRef<HTMLButtonElement>(null);
  const escalateButton = useRef<HTMLButtonElement>(null);
  const noRefreshCandidates = (paymentState?.refresh_candidates.length ?? 0) === 0;

  // Each new diagnosis replaces this panel. If the panel it replaces held focus, its removal left focus on the page
  // body, and this panel's heading takes it.
  useLayoutEffect(() => {
    const focusDropped = document.activeElement === null || document.activeElement === document.body;
    if (focusHandoff.current && focusDropped) {
      heading.current?.focus();
    }
    focusHandoff.current = false;
    const panel = section.current;
    return () => {
      focusHandoff.current = panel?.contains(document.activeElement) ?? false;
    };
  }, [focusHandoff]);

  // Approve goes once the plan is approved. If it held focus, the "Approved by" line that replaces it takes focus: it
  // stands where Approve stood, so nothing scrolls, and Execute is the next Tab stop. The line exists only after this
  // render, hence an effect rather than a hand-over before the update.
  useLayoutEffect(() => {
    if (approval !== null && approveHeldFocus.current) {
      approveHeldFocus.current = false;
      approvalLine.current?.focus();
    }
  }, [approval]);

  // The reload after its own request can leave no candidate, which disables Refresh provider state for N payments.
  // Chromium moves focus off a disabled button at its next rendering update, so the heading takes it before then.
  useLayoutEffect(() => {
    if (noRefreshCandidates) {
      handOverFocus(openRefreshButton.current, heading.current);
    }
  }, [noRefreshCandidates]);

  if (diagnosis === null) {
    return (
      <section ref={section} className="panel recovery">
        <h2 ref={heading} tabIndex={-1}>Recovery</h2>
        <p>Run a diagnosis first. The model only proposes an operation class; the planner computes exact rows.</p>
      </section>
    );
  }
  const proposed = proposedOperation(diagnosis);
  const diagnosisId = diagnosis.diagnosis_id;
  const chosenFix = fixId || fixes[0]?.fix_id || "";

  async function validateFix() {
    const result = await perform("validate fix", () => api.validateFix(runId, chosenFix, diagnosisId));
    if (result !== undefined) {
      onProgress((previous) => ({ ...previous, validation: result, plan: null, approval: null }));
      onChanged();
    }
  }

  async function buildPlan(replacing: string | null) {
    if (validation === null) {
      return;
    }
    const result = await perform("build plan", () => api.planReprocess(runId, validation.validation_id));
    if (result !== undefined) {
      if (result.stale_only_orders.length === 0) {
        handOverFocus(staleRefreshButton.current, planHeading.current); // the stale-orders row goes with this plan
      }
      onProgress((previous) => ({ ...previous, plan: result, draftId: replacing, approval: null }));
      onChanged();
    }
  }

  async function refreshStaleAndReplan() {
    if (plan === null) {
      return;
    }
    const payments = plan.stale_only_orders.flatMap((order) => order.payment_ids);
    const summary = await perform("refresh provider state", () => api.refresh(runId, payments));
    if (summary !== undefined) {
      onProgress((previous) => ({ ...previous, refreshed: summary }));
      // The re-plan is a second write. If this panel is gone (another run was opened, a new diagnosis arrived, or the
      // operator logged out while the refresh ran), React has cleared its section ref, and no plan is built for it.
      if (section.current !== null) {
        await buildPlan(plan.plan_id);
      }
    }
  }

  async function approve() {
    if (plan === null) {
      return;
    }
    const result = await perform("approve plan", () => api.approve(runId, plan.plan_id, plan.plan_hash));
    if (result !== undefined) {
      approveHeldFocus.current = approveButton.current === document.activeElement;
      onProgress((previous) => ({ ...previous, approval: result }));
      onChanged();
    }
  }

  function recordExecution(result: ExecutionResult) {
    onProgress((previous) => {
      const entry: ExecutionEntry = { sequence: previous.executions.length + 1, result };
      return { ...previous, executions: [...previous.executions, entry] };
    });
    onExecuted(result.action_id);
  }

  async function execute() {
    if (plan === null) {
      return;
    }
    const planId = plan.plan_id;
    const result = await perform("execute plan", async () => {
      try {
        return await api.execute(runId, planId);
      } catch (problem) {
        // A rejected execution is recorded by the server, which answers 409 PlanRejectedError with its result. The page
        // records that answer as well, so the rail and the money strip show the rejection; perform still reports the
        // error in the banner, as before.
        const rejected = problem instanceof ApiError && problem.errorName === "PlanRejectedError" ? problem.result : null;
        if (rejected !== null && rejected.status === "rejected" && rejected.run_id === runId && rejected.plan_id === planId) {
          recordExecution(rejected);
        }
        throw problem;
      }
    });
    if (result !== undefined) {
      recordExecution(result);
      onChanged();
    }
  }

  async function refreshOpenPayments() {
    const candidates = paymentState?.refresh_candidates ?? [];
    if (candidates.length === 0) {
      return;
    }
    const summary = await perform("refresh provider state", () => api.refresh(runId, candidates));
    if (summary !== undefined) {
      onProgress((previous) => ({ ...previous, refreshed: summary }));
      onChanged();
    }
  }

  async function escalate() {
    const result = await perform("escalate", () => api.escalate(runId, diagnosisId));
    if (result !== undefined) {
      handOverFocus(escalateButton.current, heading.current); // Escalate goes once escalated
      onProgress((previous) => ({ ...previous, escalation: result }));
      onChanged();
    }
  }

  async function prepareReplay() {
    const result = await perform("prepare provider replay", () => api.prepareReplay(runId, diagnosisId));
    if (result !== undefined) {
      onProgress((previous) => ({ ...previous, replay: result }));
      onChanged();
    }
  }

  return (
    <section ref={section} className="panel recovery">
      <h2 ref={heading} tabIndex={-1}>Recovery</h2>
      <p className="proposal">
        Proposed operation: <strong>{proposed ?? "none (needs review)"}</strong>. A person validates, approves and executes;
        the model has no route to any of it.
      </p>
      {paymentState !== null && (
        <LateAuthorisations state={paymentState} planTargets={new Set(plan?.targets.map((target) => target.id) ?? [])} />
      )}

      {proposed === "reprocess_inbox" && (
        <>
          <h3>Fix validation (prewritten fixture tests)</h3>
          <div className="row">
            <label>
              Corrected handler<select value={chosenFix} onChange={(event) => setFixId(event.target.value)}>
                {fixes.map((option) => (
                  <option key={option.fix_id} value={option.fix_id}>{option.fix_id}: {option.label}</option>
                ))}
              </select>
            </label>
            <RequestButton variant="primary" disabled={chosenFix === ""} onClick={validateFix}>Validate this corrected handler</RequestButton>
          </div>
          {validation !== null && (
            <p className={validation.passed ? "verdict outcome-complete" : "verdict outcome-failed"}>
              {validation.fix_id}: reproduced on the deployed handler {validation.deployed_version}:{" "}
              <strong>{validation.reproduced ? "yes" : "no"}</strong> (failed {validation.deployed_failures.join(", ") || "nothing"});
              corrected handler failed {validation.corrected_failures.join(", ") || "nothing"};{" "}
              <strong>{validation.passed ? "passed" : "did not pass"}</strong> (validated by {validation.operator_id})
            </p>
          )}
          {validation?.passed === true && (
            <RequestButton variant="primary" onClick={() => buildPlan(null)}>Build reprocessing plan</RequestButton>
          )}
          {plan !== null && <PlanView plan={plan} draftId={draftId} headingRef={planHeading} />}
          {plan !== null && plan.stale_only_orders.length > 0 && planExecutions.length === 0 && (
            <div className="row stale-orders">
              <span>
                {plan.stale_only_orders.length} orders are excluded only because their provider state is stale:{" "}
                {plan.stale_only_orders.map((order) => order.order_id).join(", ")}
              </span>
              <RequestButton ref={staleRefreshButton} onClick={refreshStaleAndReplan}>
                Refresh provider state (read-only) and re-plan
              </RequestButton>
            </div>
          )}
          {refreshed !== null && (
            <p className="note">
              Refreshed {refreshed.refreshed.length} payments at replay time {replayTime(refreshed.now_ms)}; unknown at provider{" "}
              {refreshed.unknown_at_provider.length}.
            </p>
          )}
          {plan !== null && plan.target_count > 0 && <h3>Approval and execution</h3>}
          {plan !== null && plan.target_count > 0 && approval === null && (
            <RequestButton ref={approveButton} variant="primary" onClick={approve}>
              Approve plan {plan.plan_id} with hash {plan.plan_hash.slice(0, 16)}
            </RequestButton>
          )}
          {approval !== null && (
            <p ref={approvalLine} tabIndex={-1} className="approved">
              Approved by {approval.operator_id}, bound to hash <code>{approval.plan_hash}</code>; the approval expires at replay
              time {replayTime(approval.expires_at_ms)}.
            </p>
          )}
          {approval !== null && (
            <RequestButton variant={planExecutions.length === 0 ? "primary" : "secondary"} onClick={execute}>
              {planExecutions.length === 0 ? "Execute" : "Execute again (shows the replay)"}
            </RequestButton>
          )}
          {planExecutions.length > 0 && (
            <ul className="compact executions">
              {planExecutions.map((entry) => <ExecutionView key={entry.sequence} result={entry.result} />)}
            </ul>
          )}
        </>
      )}

      {proposed === "refresh_provider_state" && (
        <div className="row">
          <RequestButton ref={openRefreshButton} variant="primary" disabled={noRefreshCandidates} onClick={refreshOpenPayments}>
            Refresh provider state for {paymentState?.refresh_candidates.length ?? 0} open or late-authorised payments (read-only)
          </RequestButton>
          {refreshed !== null && (
            <span>
              Refreshed {refreshed.refreshed.length} payments at {replayTime(refreshed.now_ms)}; statuses now{" "}
              {JSON.stringify(Object.values(refreshed.statuses).reduce<Record<string, number>>((counts, status) => {
                counts[status] = (counts[status] ?? 0) + 1;
                return counts;
              }, {}))}
            </span>
          )}
        </div>
      )}

      {proposed === "prepare_provider_replay" && (
        <RequestButton variant="primary" onClick={prepareReplay}>Prepare provider replay instructions (no provider call)</RequestButton>
      )}
      {replay !== null && (
        <ul className="compact">
          {replay.instructions.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}

      {proposed === "no_action" && <p>The diagnosis found no incident; nothing is changed.</p>}

      {proposed !== "reprocess_inbox" && proposed !== "no_action" && escalation === null && (
        <RequestButton ref={escalateButton} onClick={escalate}>Escalate with the evidence needed (no reprocessing)</RequestButton>
      )}
      {escalation !== null && (
        <p className="outcome-complete">
          Escalated as action {escalation.action_id} by {escalation.operator_id} for {escalation.subject_ids.join(", ")}; evidence
          needed: {escalation.evidence_needed.join(", ") || "none named"}.
        </p>
      )}
    </section>
  );
}
