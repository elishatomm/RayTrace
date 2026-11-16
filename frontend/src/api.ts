// Thin client for the workbench API. Every state-changing call carries the X-Workbench-Request
// header the server requires; the session is the httpOnly cookie the login sets.
import type {
  ActionReceipt,
  Approval,
  CaseState,
  DiagnosisDocument,
  DiagnosisJob,
  EscalationRecord,
  ExecutionResult,
  FixOption,
  FixValidation,
  Health,
  IncidentsResponse,
  JsonValue,
  ModelMode,
  NextStep,
  OrderCaseDetail,
  OrderQueuePage,
  PaymentState,
  PlanPreview,
  QueueRow,
  RefreshSummary,
  ReplayInstructions,
  RunSummary,
  ScenarioList,
  SessionState,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly errorName: string;
  // The execution result a 409 PlanRejectedError carries: the server records a rejected execution, and the page records
  // its answer too. Null for every other error, and whenever the body's `result` does not have the expected shape.
  readonly result: ExecutionResult | null;

  constructor(status: number, errorName: string, detail: string, result: ExecutionResult | null = null) {
    super(detail);
    this.status = status;
    this.errorName = errorName;
    this.result = result;
  }
}

type Method = "GET" | "POST" | "DELETE";

const isText = (value: unknown): value is string => typeof value === "string";
const isTextList = (value: unknown): value is string[] => Array.isArray(value) && value.every(isText);
const isWholeNumber = (value: unknown): value is number => Number.isSafeInteger(value);

// An error body's `result` is trusted only when every field the page reads has its type (executor.ExecutionResult).
function isExecutionResult(value: unknown): value is ExecutionResult {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const result = value as Record<string, unknown>;
  return isText(result.run_id) && isText(result.plan_id) && isText(result.action_id) && isText(result.operation_key)
    && (result.status === "succeeded" || result.status === "rejected") && isTextList(result.reasons)
    && isTextList(result.fulfilled_order_ids) && isWholeNumber(result.restored_processing_value_paise)
    && isWholeNumber(result.additional_captured_paise) && isWholeNumber(result.executed_at_ms)
    && typeof result.replayed === "boolean";
}

function describeFailure(status: number, body: string): ApiError {
  try {
    const parsed: unknown = JSON.parse(body);
    if (parsed !== null && typeof parsed === "object") {
      const record = parsed as Record<string, unknown>;
      const errorName = typeof record.error === "string" ? record.error : `HTTP ${status}`;
      const detail = typeof record.detail === "string" ? record.detail : JSON.stringify(record.detail ?? record);
      return new ApiError(status, errorName, detail, isExecutionResult(record.result) ? record.result : null);
    }
  } catch {
    // Not JSON (for example a proxy error page): report the raw text below.
  }
  return new ApiError(status, `HTTP ${status}`, body.slice(0, 300) || "no response body");
}

async function call<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers, credentials: "same-origin" };
  if (method !== "GET") {
    headers["X-Workbench-Request"] = "1";
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body ?? {});
  }
  const response = await fetch(path, init);
  const text = await response.text();
  if (!response.ok) {
    throw describeFailure(response.status, text);
  }
  return JSON.parse(text) as T;
}

const run = (runId: string): string => `/api/runs/${encodeURIComponent(runId)}`;

// The server's filters for the order queue; empty lists mean no filter.
export interface QueueQuery {
  state: CaseState[];
  next_step: NextStep[];
}

const QUEUE_PAGE_ROWS = 5000; // the server's largest page
const MAX_QUEUE_PAGES = 20;

function queueSearch(query: QueueQuery, asOfMs: number | null, after: string | null): string {
  const params = new URLSearchParams({ limit: String(QUEUE_PAGE_ROWS) });
  query.state.forEach((state) => params.append("state", state));
  query.next_step.forEach((step) => params.append("next_step", step));
  if (asOfMs !== null) {
    params.set("as_of_ms", String(asOfMs));
  }
  if (after !== null) {
    params.set("after", after);
  }
  return params.toString();
}

// The pages after a cursor, each read at the first page's replay time, so a clock that moves meanwhile cannot
// mix two times in one list.
async function laterPages(
  runId: string, query: QueueQuery, asOfMs: number, after: string | null, pagesLeft: number,
): Promise<QueueRow[]> {
  if (after === null) {
    return [];
  }
  if (pagesLeft === 0) {
    throw new Error(`the queue has more than ${MAX_QUEUE_PAGES * QUEUE_PAGE_ROWS} matching orders; narrow the filter`);
  }
  const page = await call<OrderQueuePage>("GET", `${run(runId)}/orders?${queueSearch(query, asOfMs, after)}`);
  return [...page.orders, ...(await laterPages(runId, query, asOfMs, page.next_after, pagesLeft - 1))];
}

// Every matching order at one replay time, in order-ID order: the first page fixes the time.
async function wholeQueue(runId: string, query: QueueQuery): Promise<OrderQueuePage> {
  const first = await call<OrderQueuePage>("GET", `${run(runId)}/orders?${queueSearch(query, null, null)}`);
  const rest = await laterPages(runId, query, first.as_of_ms, first.next_after, MAX_QUEUE_PAGES - 1);
  return { ...first, orders: [...first.orders, ...rest], next_after: null };
}

// A link built from the stored IDs, never from a URL the server or a record supplies.
export function receiptLink(runId: string, actionId: string, format: "json" | "markdown"): string {
  const path = `${run(runId)}/actions/${encodeURIComponent(actionId)}/receipt`;
  return format === "markdown" ? `${path}?format=markdown` : path;
}

export const api = {
  health: () => call<Health>("GET", "/health"),
  session: () => call<SessionState>("GET", "/api/session"),
  login: (token: string, operatorId: string) =>
    call<SessionState>("POST", "/api/session", { token, operator_id: operatorId }),
  logout: () => call<SessionState>("DELETE", "/api/session"),
  scenarios: () => call<ScenarioList>("GET", "/api/scenarios"),
  fixes: () => call<FixOption[]>("GET", "/api/fixes"),
  runs: () => call<RunSummary[]>("GET", "/api/runs"),
  createRun: (scenario: string, seed: number) => call<RunSummary>("POST", "/api/runs", { scenario, seed }),
  run: (runId: string) => call<RunSummary>("GET", run(runId)),
  advance: (runId: string, byMs: number) => call<RunSummary>("POST", `${run(runId)}/advance`, { by_ms: byMs }),
  incidents: (runId: string) => call<IncidentsResponse>("GET", `${run(runId)}/incidents`),
  paymentState: (runId: string) => call<PaymentState>("GET", `${run(runId)}/payment-state`),
  // Without an incident the server diagnoses the run's latest one; JSON leaves an undefined incident_id out.
  startDiagnosis: (runId: string, mode: ModelMode, incidentId?: string) =>
    call<DiagnosisJob>("POST", `${run(runId)}/diagnoses`, { mode, incident_id: incidentId }),
  diagnosisJob: (runId: string, jobId: string) =>
    call<DiagnosisJob>("GET", `${run(runId)}/diagnoses/${encodeURIComponent(jobId)}`),
  diagnoses: (runId: string) => call<DiagnosisDocument[]>("GET", `${run(runId)}/diagnoses`),
  validateFix: (runId: string, fixId: string, diagnosisId: string) =>
    call<FixValidation>("POST", `${run(runId)}/fixes/${encodeURIComponent(fixId)}/validate`, {
      diagnosis_id: diagnosisId,
    }),
  planReprocess: (runId: string, fixValidationId: string) =>
    call<PlanPreview>("POST", `${run(runId)}/plans`, {
      operation: "reprocess_inbox",
      fix_validation_id: fixValidationId,
    }),
  escalate: (runId: string, diagnosisId: string) =>
    call<EscalationRecord>("POST", `${run(runId)}/plans`, { operation: "escalate", diagnosis_id: diagnosisId }),
  prepareReplay: (runId: string, diagnosisId: string) =>
    call<ReplayInstructions>("POST", `${run(runId)}/plans`, {
      operation: "prepare_provider_replay",
      diagnosis_id: diagnosisId,
    }),
  approve: (runId: string, planId: string, planHash: string) =>
    call<Approval>("POST", `${run(runId)}/plans/${encodeURIComponent(planId)}/approve`, { plan_hash: planHash }),
  execute: (runId: string, planId: string) =>
    call<ExecutionResult>("POST", `${run(runId)}/plans/${encodeURIComponent(planId)}/execute`),
  refresh: (runId: string, paymentIds: string[]) =>
    call<RefreshSummary>("POST", `${run(runId)}/refresh`, { payment_ids: paymentIds }),
  report: (runId: string) => call<Record<string, JsonValue>>("GET", `${run(runId)}/report`),
  // The receipt the CLI's receipt command writes, read so the recovery rail can show its hash.
  receipt: (runId: string, actionId: string) => call<ActionReceipt>("GET", receiptLink(runId, actionId, "json")),
  orderQueue: (runId: string, query: QueueQuery) => wholeQueue(runId, query),
  orderCase: (runId: string, orderId: string, asOfMs: number) =>
    call<OrderCaseDetail>("GET", `${run(runId)}/orders/${encodeURIComponent(orderId)}?as_of_ms=${asOfMs}`),
};
