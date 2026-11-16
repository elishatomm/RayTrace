import { countOf, replayTime, rupees } from "../format";
import {
  PATH_SUMMARIES,
  type ProofLine,
  type RailStep,
  type ReceiptLoad,
  type RecoveryProgress,
  type StepState,
  deriveRail,
} from "../recovery";
import type { DiagnosisJob, ExecutionResult } from "../types";
import { IconCurrent, IconDone, IconExternal, IconFailed, IconPending, IconReview, IconSkipped } from "./Icons";
import type { QueueSnapshot } from "./QueuePanel";

interface RunHeaderProps {
  runId: string;
  job: DiagnosisJob | null; // the latest diagnosis job in this session, the one the Recovery panel acts on
  storedDiagnoses: number; // diagnoses of this run the server holds, including earlier sessions' and the CLI's
  canDiagnose: boolean;
  queue: QueueSnapshot | null; // the queue's whole-queue totals for this run, once loaded
  progress: RecoveryProgress;
  railReceipt: ReceiptLoad | null; // the receipt of the current plan's latest execution
  stripReceipt: ReceiptLoad | null; // the receipt of the recovery's latest execution (the strip's definitions)
}

// ------------------------------------------------------------------ the money strip

interface FigureProps {
  label: string;
  tone: "stuck" | "complete" | "failed" | "plain";
  value: string | null; // null: not known yet, shown as such and never as 0
  unknown: string; // what "not known" means here
  detail: string | null;
}

function Figure({ label, tone, value, unknown, detail }: Readonly<FigureProps>) {
  return (
    <div className={`figure figure-${value === null ? "unknown" : tone}`}>
      <dt>{label}</dt>
      <dd className="figure-value">{value ?? unknown}</dd>
      {detail !== null && <dd className="figure-detail">{detail}</dd>}
    </div>
  );
}

function MoneyStrip({ queue, execution, receipt }: Readonly<{ queue: QueueSnapshot | null; execution: ExecutionResult | null; receipt: ReceiptLoad | null }>) {
  const totals = queue?.totals ?? null;
  // A zero is good news, not a missing number: it says so in words and in green.
  const capturedNone = totals !== null && totals.captured_unprocessed_orders === 0;
  const awaitingNone = totals !== null && totals.unresolved_orders === 0;
  const captured = totals === null ? null
    : capturedNone ? "0 orders: no captured payment is waiting on our processing" : countOf(totals.captured_unprocessed_orders, "order");
  const awaiting = totals === null ? null : awaitingNone ? "0 orders: nothing is waiting"
    : countOf(totals.unresolved_orders, "order")
      + (totals.unresolved_unasserted_orders > 0 ? `; ${totals.unresolved_unasserted_orders} of them are counted but not summed` : "");
  const executed = execution === null ? null : `action ${execution.action_id}, ${execution.status}`;
  const definitions = receipt?.status === "loaded" ? receipt.definitions : null;
  return (
    <section className="card money-strip" aria-labelledby="money-heading">
      <div className="card-head">
        <h2 id="money-heading">Money at stake</h2>
        <p className="card-meta">
          {queue === null ? "Loading the queue's totals..." : <>as of replay time <span className="mono">{replayTime(queue.as_of_ms)}</span></>}
        </p>
      </div>
      <dl className="figures">
        <Figure
          label="Captured, processing incomplete"
          tone={capturedNone ? "complete" : "stuck"}
          value={totals === null ? null : rupees(totals.captured_unprocessed_value_paise)}
          unknown="Loading"
          detail={captured}
        />
        <Figure
          label="Still awaiting an action or a hold"
          tone={awaitingNone ? "complete" : "stuck"}
          value={totals === null ? null : rupees(totals.unresolved_value_paise)}
          unknown="Loading"
          detail={awaiting}
        />
        <Figure
          label="Restored processing value"
          tone={execution?.status === "succeeded" ? "complete" : "failed"}
          value={execution === null ? null : rupees(execution.restored_processing_value_paise)}
          unknown="Not yet known"
          detail={executed ?? "nothing executed in this recovery yet"}
        />
        <Figure
          label="New capture"
          tone="plain"
          value={execution === null ? null : rupees(execution.additional_captured_paise)}
          unknown="Not yet known"
          detail={executed ?? "nothing executed in this recovery yet"}
        />
      </dl>
      <p className="money-note">
        Captured, processing incomplete: money already captured; an approved reprocess plan restores their processing, it is
        not new revenue. Restored processing value, likewise, is money already captured before the intervention, not new
        revenue.
      </p>
      <details className="definitions">
        <summary>How these numbers are defined</summary>
        <dl>
          {queue?.definitions.captured_unprocessed_value_paise !== undefined && (
            <div><dt>Captured, processing incomplete</dt><dd>{queue.definitions.captured_unprocessed_value_paise}</dd></div>
          )}
          {queue?.definitions.unresolved_value_paise !== undefined && (
            <div><dt>Still awaiting an action or a hold</dt><dd>{queue.definitions.unresolved_value_paise}</dd></div>
          )}
          {queue?.definitions.totals !== undefined && (
            <div><dt>Both totals</dt><dd>{queue.definitions.totals}</dd></div>
          )}
          <div>
            <dt>Restored processing value and new capture</dt>
            <dd>
              The values the Execute answer returned for this recovery: the recovery of the latest diagnosis in this
              session. Until that recovery executes a plan they are not yet known. Earlier executions, from another session
              or the CLI, are in the exported report and in each order's receipts.
            </dd>
          </div>
          {definitions !== null && definitions.restored !== null && (
            <div><dt>Restored processing value, from the receipt</dt><dd>{definitions.restored}</dd></div>
          )}
          {definitions !== null && definitions.newCapture !== null && (
            <div><dt>New capture, from the receipt</dt><dd>{definitions.newCapture}</dd></div>
          )}
        </dl>
      </details>
    </section>
  );
}

// ------------------------------------------------------------------ the recovery rail

const STATE_LABELS: Record<StepState, string> = {
  done: "Done",
  current: "Current",
  pending: "Not yet",
  skipped: "Not applicable",
  failed: "Failed",
  review: "Needs review",
};

function StateIcon({ state }: Readonly<{ state: StepState }>) {
  switch (state) {
    case "done":
      return <IconDone />;
    case "current":
      return <IconCurrent />;
    case "pending":
      return <IconPending />;
    case "skipped":
      return <IconSkipped />;
    case "failed":
      return <IconFailed />;
    case "review":
      return <IconReview />;
  }
}

function Proof({ line }: Readonly<{ line: ProofLine }>) {
  const value = line.truncated ? `${line.value}\u2026` : line.value;
  return (
    <span className="proof-line">
      {line.label !== null && <span className="proof-label">{line.label} </span>}
      {line.code ? <code>{value}</code> : value}
    </span>
  );
}

function Step({ step }: Readonly<{ step: RailStep }>) {
  return (
    <li className={`rail-step step-${step.state}`} aria-current={step.state === "current" ? "step" : undefined}>
      <span className="step-name"><span className="step-number">{step.number}</span> {step.name}</span>
      <span className="step-state"><StateIcon state={step.state} />{STATE_LABELS[step.state]}</span>
      <span className="step-proof">
        {step.proof.map((line) => <Proof key={`${line.label ?? ""}:${line.value}`} line={line} />)}
        {step.receiptHref !== null && (
          <a className="proof-link" href={step.receiptHref} target="_blank" rel="noopener noreferrer">
            Open receipt (JSON)<IconExternal />
          </a>
        )}
      </span>
    </li>
  );
}

// The cause in a few words, from the latest diagnosis; the Diagnosis card has the whole answer.
function Cause({ job, storedDiagnoses }: Readonly<{ job: DiagnosisJob | null; storedDiagnoses: number }>) {
  if (job === null) {
    return storedDiagnoses === 0
      ? <>Not diagnosed yet.</>
      : <>Not diagnosed in this session; the Diagnosis card lists {countOf(storedDiagnoses, "stored diagnosis", "stored diagnoses")} of this run.</>;
  }
  if (job.status === "queued" || job.status === "running") {
    return <>Diagnosing <code>{job.incident_id}</code>; the job is {job.status}.</>;
  }
  const output = job.diagnosis?.output ?? null;
  if (job.diagnosis === null) {
    return <>The diagnosis could not run.</>;
  }
  if (job.diagnosis.status === "needs_review" || output === null) {
    return <>Needs review: the model did not produce a valid diagnosis, so nothing is proposed.</>;
  }
  return (
    <>
      <code>{output.hypothesis}</code>; faulting change <code>{output.faulting_change_id}</code>; affected method{" "}
      <code>{output.affected_method}</code>
    </>
  );
}

function RecoveryRail({ runId, job, storedDiagnoses, canDiagnose, progress, railReceipt }: Readonly<Omit<RunHeaderProps, "queue" | "stripReceipt">>) {
  const rail = deriveRail({ runId, job, canDiagnose, progress, receipt: railReceipt });
  const path = PATH_SUMMARIES[rail.path];
  return (
    <section className="card rail" aria-labelledby="rail-heading">
      <div className="card-head">
        <h2 id="rail-heading">Recovery steps</h2>
        <p className="card-meta">for the latest diagnosis in this session</p>
      </div>
      <dl className="rail-facts">
        <div className="rail-cause">
          <dt>Cause</dt>
          <dd><Cause job={job} storedDiagnoses={storedDiagnoses} /></dd>
        </div>
        <div className="rail-path">
          <dt>Path</dt>
          <dd>{path.operation !== null && <><code>{path.operation}</code>: </>}{path.summary}</dd>
        </div>
        <div className="rail-next">
          <dt>Next safe step</dt>
          <dd>{rail.next}</dd>
        </div>
      </dl>
      <ol className="rail-steps">
        {rail.steps.map((step) => <Step key={step.number} step={step} />)}
      </ol>
    </section>
  );
}

export function RunHeader({
  runId, job, storedDiagnoses, canDiagnose, queue, progress, railReceipt, stripReceipt,
}: Readonly<RunHeaderProps>) {
  const execution = progress.executions.at(-1)?.result ?? null; // the strip reports the recovery's latest execution
  return (
    <div className="run-header">
      <MoneyStrip queue={queue} execution={execution} receipt={stripReceipt} />
      <RecoveryRail
        runId={runId}
        job={job}
        storedDiagnoses={storedDiagnoses}
        canDiagnose={canDiagnose}
        progress={progress}
        railReceipt={railReceipt}
      />
    </div>
  );
}
