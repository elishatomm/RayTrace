import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { DiagnosisPanel } from "./components/DiagnosisPanel";
import { EvidencePanel } from "./components/EvidencePanel";
import { IncidentsPanel } from "./components/IncidentsPanel";
import { LoginPanel } from "./components/LoginPanel";
import { type QueueSnapshot, QueuePanel } from "./components/QueuePanel";
import { type Perform, RecoveryPanel } from "./components/RecoveryPanel";
import { ReportPanel } from "./components/ReportPanel";
import { RunControls } from "./components/RunControls";
import { RunHeader } from "./components/RunHeader";
import { TopBar } from "./components/TopBar";
import { describeError } from "./format";
import { NO_PROGRESS, type ReceiptLoad, type RecoveryProgress, executionsOfPlan } from "./recovery";
import type {
  DiagnosisDocument,
  DiagnosisJob,
  ExecutionResult,
  FixOption,
  Health,
  IncidentsResponse,
  ModelMode,
  PaymentState,
  RunSummary,
  ScenarioList,
} from "./types";

const POLL_INTERVAL_MS = 500;
const POLL_LABEL = "poll diagnosis";
// The modes every server accepts, until GET /api/scenarios says which this one does (hosted only with its settings).
const ALWAYS_ACCEPTED_MODES: ModelMode[] = ["local", "recorded", "rules"];

// Receipts are keyed by run and action: an action ID is unique within its run.
const receiptKey = (runId: string, actionId: string): string => `${runId}/${actionId}`;

function withEntry<V>(map: ReadonlyMap<string, V>, key: string, value: V): ReadonlyMap<string, V> {
  return new Map(map).set(key, value);
}

// The server's public mode answers a scenario and seed that already have a run with that run (200, not 409), so the
// run may be listed already: it is replaced where it stands, and only a new run goes to the top.
function withRun(runs: RunSummary[], run: RunSummary): RunSummary[] {
  return runs.some((item) => item.run_id === run.run_id)
    ? runs.map((item) => (item.run_id === run.run_id ? run : item))
    : [run, ...runs];
}

function receiptDefinition(value: Record<string, unknown>, name: string): string | null {
  const text = value[name];
  return typeof text === "string" ? text : null;
}

export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [operator, setOperator] = useState<string | null>(null);
  const [sessionChecked, setSessionChecked] = useState(false);
  const [scenarios, setScenarios] = useState<ScenarioList | null>(null);
  const [fixes, setFixes] = useState<FixOption[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [run, setRun] = useState<RunSummary | null>(null);
  const [observed, setObserved] = useState<IncidentsResponse | null>(null);
  const [paymentState, setPaymentState] = useState<PaymentState | null>(null);
  const [queue, setQueue] = useState<QueueSnapshot | null>(null);
  const [job, setJob] = useState<DiagnosisJob | null>(null);
  const [history, setHistory] = useState<DiagnosisDocument[]>([]);
  const [progressByDiagnosis, setProgressByDiagnosis] = useState<ReadonlyMap<string, RecoveryProgress>>(() => new Map());
  const [receipts, setReceipts] = useState<ReadonlyMap<string, ReceiptLoad>>(() => new Map());
  const [selectedEvidence, setSelectedEvidence] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [wallClock, setWallClock] = useState(() => Date.now());
  const recoveryHeldFocus = useRef(false); // set by a Recovery panel that a new diagnosis replaced while it held focus
  const focusBeforeSwap = useRef<Element | null>(null); // what held focus when a login or logout swapped the controls
  const receiptsAsked = useRef(new Set<string>()); // receipt keys requested, so a replayed execution asks once
  // Bumped whenever the recoveries are forgotten (opening a run, logging out): a receipt read started before that
  // belongs to a page that is gone, so its answer, its error and its mark in receiptsAsked are dropped.
  const receiptEpoch = useRef(0);
  // The job this page shows. A poll answer, or a poll failure, for any other job is about a page the operator has left.
  const currentJobId = useRef<string | null>(null);
  // The run the page shows: a new object each time the page changes run (opening or creating a run, logging out). An
  // answer asked for on one page and arriving on another is dropped, and so is its error: perform drops the error of
  // every request whose page has changed, and each answer that writes the run's state checks the page first.
  const pageRun = useRef<{ readonly runId: string | null }>({ runId: null });

  const perform: Perform = useCallback(async <T,>(label: string, action: () => Promise<T>) => {
    const askedOn = pageRun.current;
    setBusy(label);
    setError(null);
    try {
      return await action();
    } catch (actionError) {
      if (pageRun.current === askedOn) {
        setError(`${label} failed: ${describeError(actionError)}`);
      }
      return undefined;
    } finally {
      setBusy(null);
    }
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setWallClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    void perform("load health", async () => setHealth(await api.health()));
    void perform("check session", async () => {
      setOperator((await api.session()).operator_id);
      setSessionChecked(true);
    });
  }, [perform]);

  useEffect(() => {
    if (operator === null) {
      return;
    }
    void perform("load workbench", async () => {
      const [scenarioList, fixList, runList] = await Promise.all([api.scenarios(), api.fixes(), api.runs()]);
      setScenarios(scenarioList);
      setFixes(fixList);
      setRuns(runList);
    });
  }, [operator, perform]);

  const reloadRun = useCallback(
    async (runId: string) => {
      const askedOn = pageRun.current;
      if (askedOn.runId !== runId) {
        return; // asked for by an answer that arrived after another run was opened, or after Log out
      }
      await perform("load run", async () => {
        const [summary, incidents, payments, diagnoses] = await Promise.all([
          api.run(runId),
          api.incidents(runId),
          api.paymentState(runId),
          api.diagnoses(runId),
        ]);
        if (pageRun.current !== askedOn) {
          return; // the page changed run while these were read
        }
        setRun(summary);
        setObserved(incidents);
        setPaymentState(payments);
        setHistory(diagnoses);
      });
    },
    [perform],
  );

  useEffect(() => {
    if (job === null || (job.status !== "queued" && job.status !== "running")) {
      return;
    }
    const timer = window.setTimeout(() => {
      void perform(POLL_LABEL, async () => {
        const stale = () => currentJobId.current !== job.job_id;
        let next: DiagnosisJob;
        try {
          next = await api.diagnosisJob(job.run_id, job.job_id);
        } catch (pollError) {
          if (stale()) {
            return; // the operator opened another run or logged out: this job's failure is not news on their page
          }
          throw pollError;
        }
        if (stale()) {
          return;
        }
        setJob(next);
        if (next.status === "done" || next.status === "failed") {
          await reloadRun(next.run_id);
        }
      });
    }, POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [job, perform, reloadRun]);

  useEffect(() => {
    if (selectedEvidence !== null) {
      document.getElementById(`evidence-${selectedEvidence}`)?.scrollIntoView({ block: "center" });
    }
  }, [selectedEvidence]);

  const updateProgress = useCallback(
    (diagnosisId: string, change: (previous: RecoveryProgress) => RecoveryProgress) => {
      setProgressByDiagnosis((previous) => withEntry(previous, diagnosisId, change(previous.get(diagnosisId) ?? NO_PROGRESS)));
    },
    [],
  );

  // The receipt of an executed action, for the rail's hash. It is read outside `perform` on purpose: the busy line
  // speaks for the requests a control is waiting on, and this read must not end it early while the reload the same
  // execution started is still running. A failure is still reported in the error banner, and the rail says so.
  const requestReceipt = useCallback((runId: string, actionId: string) => {
    const key = receiptKey(runId, actionId);
    if (pageRun.current.runId !== runId || receiptsAsked.current.has(key)) {
      return; // an execution answered after the page changed run is not read for this page
    }
    receiptsAsked.current.add(key);
    setReceipts((previous) => withEntry(previous, key, { status: "loading" }));
    const epoch = receiptEpoch.current;
    void (async () => {
      try {
        const receipt = await api.receipt(runId, actionId);
        if (receipt.identity.action_id !== actionId) {
          throw new Error(`the receipt read for ${actionId} names action ${receipt.identity.action_id}`);
        }
        if (epoch !== receiptEpoch.current) {
          return; // the recoveries were forgotten while it was read
        }
        const definitions = {
          restored: receiptDefinition(receipt.value, "restored_processing_value_definition"),
          newCapture: receiptDefinition(receipt.value, "additional_captured_definition"),
        };
        setReceipts((previous) => withEntry(previous, key, { status: "loaded", hash: receipt.receipt_hash, definitions }));
      } catch (readError) {
        if (epoch !== receiptEpoch.current) {
          return; // a page the operator has left: neither its error nor its mark in receiptsAsked concerns this one
        }
        receiptsAsked.current.delete(key); // the next execution answer for this action asks again
        const reason = describeError(readError);
        setReceipts((previous) => withEntry(previous, key, { status: "failed", reason }));
        setError(`load receipt failed: ${reason}`);
      }
    })();
  }, []);

  function forgetRecoveries() {
    receiptEpoch.current += 1;
    setProgressByDiagnosis(new Map());
    setReceipts(new Map());
    receiptsAsked.current.clear();
  }

  function showJob(next: DiagnosisJob | null) {
    currentJobId.current = next?.job_id ?? null;
    setJob(next);
  }

  async function login(token: string, operatorId: string) {
    const session = await perform("log in", () => api.login(token, operatorId));
    if (session !== undefined) {
      focusBeforeSwap.current = document.activeElement;
      setOperator(session.operator_id);
    }
  }

  async function logout() {
    await perform("log out", () => api.logout());
    pageRun.current = { runId: null };
    focusBeforeSwap.current = document.activeElement;
    setOperator(null);
    setRun(null);
    showJob(null);
    setQueue(null);
    forgetRecoveries();
  }

  function openRun(runId: string) {
    pageRun.current = { runId };
    showJob(null);
    setHistory([]);
    setSelectedEvidence(null);
    forgetRecoveries();
    void reloadRun(runId);
  }

  async function createRun(scenario: string, seed: number) {
    const askedOn = pageRun.current;
    const created = await perform("create run", () => api.createRun(scenario, seed));
    if (created === undefined) {
      return;
    }
    setRuns((previous) => withRun(previous, created)); // the run exists, whatever the page shows now
    if (pageRun.current === askedOn) {
      openRun(created.run_id); // only if the operator has not opened another run, or logged out, meanwhile
    }
  }

  async function advance(byMs: number) {
    if (run === null) {
      return;
    }
    const moved = await perform("advance replay clock", () => api.advance(run.run_id, byMs));
    if (moved !== undefined) {
      await reloadRun(moved.run_id);
    }
  }

  // Without an incident the server takes the run's latest. True once the job is shown on the page it was asked from.
  async function startDiagnosis(mode: ModelMode, incidentId?: string): Promise<boolean> {
    if (run === null) {
      return false;
    }
    const askedOn = pageRun.current;
    const started = await perform("start diagnosis", () => api.startDiagnosis(run.run_id, mode, incidentId));
    if (started === undefined || pageRun.current !== askedOn) {
      return false;
    }
    setSelectedEvidence(null);
    showJob(started);
    return true;
  }

  async function diagnose(mode: ModelMode) {
    await startDiagnosis(mode);
  }

  const diagnosis = job?.diagnosis ?? null;
  const progress = diagnosis === null ? NO_PROGRESS : progressByDiagnosis.get(diagnosis.diagnosis_id) ?? NO_PROGRESS;
  // The strip reports the recovery's latest execution; the rail, the current plan's. They differ only after a re-plan.
  const receiptOf = (execution: ExecutionResult | null): ReceiptLoad | null =>
    run === null || execution === null ? null : receipts.get(receiptKey(run.run_id, execution.action_id)) ?? null;
  const stripReceipt = receiptOf(progress.executions.at(-1)?.result ?? null);
  const railReceipt = receiptOf(executionsOfPlan(progress).at(-1)?.result ?? null);
  const jobRunning = job !== null && (job.status === "queued" || job.status === "running");
  // A running job is polled every 500 ms, and announcing each poll would repeat the same news. While the job runs, the
  // busy line says so once and keeps that text between polls; any other request still shows its own label.
  const working = jobRunning && (busy === null || busy === POLL_LABEL) ? "diagnosis running" : busy;
  return (
    <>
      <TopBar
        health={health}
        source={operator === null ? null : scenarios?.model_source ?? null}
        run={run}
        job={job}
        wallClockMs={wallClock}
        operator={operator}
        onLogout={logout}
      />
      {error !== null && <p className="banner error" role="alert">{error}</p>}
      {/* A polite live region: it stays in the page, empty while idle, so screen readers announce each change of its text. */}
      <p className={working === null ? undefined : "busy"} role="status">{working !== null && `Working: ${working}...`}</p>{/* NOSONAR(S6819) a <p role="status"> on purpose (owner decision Q33 (4)): styles.css and the UI walkthrough find it by the role, and the walkthrough requires a p */}
      {sessionChecked && operator === null && (
        <main className="login-page">
          <LoginPanel focusBeforeSwap={focusBeforeSwap} onLogin={login} />
        </main>
      )}
      {operator !== null && (
        <main>
          <RunControls
            scenarios={scenarios?.scenarios ?? []}
            runs={runs}
            current={run}
            busy={busy !== null}
            focusBeforeSwap={focusBeforeSwap}
            onCreate={createRun}
            onOpen={openRun}
            onAdvance={advance}
          />
          {run === null ? (
            <p className="hint">Create a synthetic run or open an existing one.</p>
          ) : (
            <>
              <RunHeader
                runId={run.run_id}
                job={job}
                storedDiagnoses={history.length}
                canDiagnose={run.incident_ids.length > 0}
                queue={queue?.run_id === run.run_id ? queue : null}
                progress={progress}
                railReceipt={railReceipt}
                stripReceipt={stripReceipt}
              />
              <div className="grid">
                <QueuePanel key={run.run_id} runId={run.run_id} replayClockMs={run.replay_clock_ms} perform={perform} onLoaded={setQueue} />
                {observed !== null && <IncidentsPanel observed={observed} />}
                <DiagnosisPanel
                  job={job}
                  history={history}
                  defaultMode={scenarios?.execution_mode ?? "rules"}
                  modes={scenarios?.model_modes ?? ALWAYS_ACCEPTED_MODES}
                  canRun={run.incident_ids.length > 0}
                  onRun={diagnose}
                  onRulesInstead={(incidentId) => startDiagnosis("rules", incidentId)}
                  onCite={setSelectedEvidence}
                />
                <EvidencePanel packet={job?.packet ?? null} selectedId={selectedEvidence} onSelect={setSelectedEvidence} />
                <RecoveryPanel
                  key={diagnosis?.diagnosis_id ?? "none"}
                  runId={run.run_id}
                  diagnosis={diagnosis}
                  fixes={fixes}
                  paymentState={paymentState}
                  progress={progress}
                  perform={perform}
                  focusHandoff={recoveryHeldFocus}
                  onProgress={(change) => {
                    if (diagnosis !== null) {
                      updateProgress(diagnosis.diagnosis_id, change);
                    }
                  }}
                  onExecuted={(actionId) => requestReceipt(run.run_id, actionId)}
                  onChanged={() => void reloadRun(run.run_id)}
                />
                {/* Keyed per run (a key of its own: QueuePanel, a sibling, is keyed by the bare run ID) so an export
                    answered after another run was opened cannot report "Saved as" on that run's page. */}
                <ReportPanel key={`report/${run.run_id}`} runId={run.run_id} perform={perform} />
              </div>
            </>
          )}
        </main>
      )}
    </>
  );
}
