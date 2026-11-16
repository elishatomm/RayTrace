// The recorded demo's stand-in for the network. scripts/demo/record.py drove this UI against the real API in rules
// mode through the README's three flows and kept every answer, filed by run and by stage of that run's flow (a stage
// is the server state between two state-changing requests). This module replaces window.fetch before the App loads:
//   * a read gets the answer the server gave to the same request at the same stage of the same run's flow;
//   * a state-changing request moves its run to the next stage when it is the request the recording made next there,
//     and gets the answer the server gave to it;
//   * anything else gets an honest refusal ("Not in this recorded demo: ..."), which the page shows in its error banner.
// Nothing is sent anywhere: the replay reads only its own recorded files, from the folder the page was served from.
import { MODE_LABELS, replayTime } from "../../src/format";
import type { ModelMode } from "../../src/types";

const FORMAT = "recovery-workbench-recorded-demo/1";
export const MANIFEST_PATH = "demo-data/manifest.json";
const REFUSAL_ERROR = "NotInRecordedDemo";
const COVERS = "The demo covers the README's three flows.";
const RUN_PATH = /^\/api\/runs\/([^/]+)(\/.*)?$/;
const GLOBAL_READS = new Set(["/health", "/api/scenarios", "/api/fixes", "/api/runs"]);

// One answer the real API gave while the recording ran: its status, content type and text, byte for byte.
interface RecordedAnswer {
  status: number;
  type: string | null;
  body: string;
}

type StageEntry = { answers: RecordedAnswer[] } | { same_as_stage: number };

interface StageFile {
  format: string;
  run_id: string;
  stage: number;
  answers: Record<string, StageEntry>;
}

interface RecordedStage {
  name: string;
  clock_ms: number | null;
  file: string;
  covered: boolean; // every Show filter, the first orders' cases and the report were recorded at this stage
}

interface RecordedTransition {
  label: string; // what the step does, in words: "approve the plan"
  method: string;
  path: string;
  query: string;
  request: unknown; // the request's JSON body
  answer: RecordedAnswer;
}

interface RecordedReceipt {
  action_id: string;
  from_stage: number; // the stage from which the action exists
  json: string;
  json_type: string;
  markdown: string;
  markdown_type: string;
  receipt_hash: string;
}

export interface RecordedRun {
  run_id: string;
  flow: string;
  title: string;
  guide: string;
  scenario: string;
  seed: number;
  stages: RecordedStage[];
  transitions: RecordedTransition[]; // transitions[i] moves the run from stage i to stage i + 1
  receipts: RecordedReceipt[];
  case_orders: string[];
}

interface DemoManifest {
  format: string;
  global: Record<string, RecordedAnswer[]>;
  session: { login: RecordedAnswer; logout: RecordedAnswer };
  runs: RecordedRun[];
}

// What the banner shows: the recorded runs, once the manifest has loaded, and the latest refusal.
export interface DemoReplay {
  subscribe: (listener: () => void) => () => void;
  notice: () => string | null;
  runs: () => readonly RecordedRun[] | null;
  dismiss: () => void;
}

class DemoStore implements DemoReplay {
  private latest: string | null = null;
  private recorded: readonly RecordedRun[] | null = null;
  private readonly listeners = new Set<() => void>();

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  readonly notice = (): string | null => this.latest;
  readonly runs = (): readonly RecordedRun[] | null => this.recorded;
  readonly dismiss = (): void => this.setNotice(null);

  loaded(runs: readonly RecordedRun[]): void {
    this.recorded = runs;
    this.emit();
  }

  setNotice(text: string | null): void {
    if (this.latest !== text) {
      this.latest = text;
      this.emit();
    }
  }

  private emit(): void {
    this.listeners.forEach((listener) => listener());
  }
}

function replayed(answer: RecordedAnswer): Response {
  return new Response(answer.body, {
    status: answer.status,
    headers: answer.type === null ? {} : { "Content-Type": answer.type },
  });
}

function jsonAnswer(status: number, value: unknown): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

// JSON with its object keys sorted, so two bodies compare by value and not by key order.
function canonical(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonical).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function parseBody(body: string | null): unknown {
  if (body === null) {
    return null;
  }
  try {
    return JSON.parse(body) as unknown;
  } catch {
    return body; // not JSON: compared as text, so it matches no recorded request
  }
}

function field(request: unknown, name: string): unknown {
  return request !== null && typeof request === "object" ? (request as Record<string, unknown>)[name] : undefined;
}

function decoded(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function runLabel(run: RecordedRun): string {
  return `${run.run_id} (${run.scenario}, seed ${run.seed})`;
}

function isModelMode(value: unknown): value is ModelMode {
  return typeof value === "string" && Object.hasOwn(MODE_LABELS, value);
}

function checkManifest(value: unknown): DemoManifest {
  if (field(value, "format") !== FORMAT) {
    throw new Error(`${MANIFEST_PATH} is not a ${FORMAT} recording`);
  }
  return value as DemoManifest;
}

// The recorded API: one instance per page load, so reloading the page restarts every flow.
class RecordedApi {
  private readonly stages = new Map<string, number>(); // each run's current stage
  private readonly served = new Map<string, number>(); // how often each recorded read was answered, for sequences
  private readonly files = new Map<string, Promise<StageFile>>();
  private signedIn = true; // the recording starts after the operator logged in

  constructor(
    private readonly manifest: DemoManifest,
    private readonly loadOwn: (path: string) => Promise<Response>,
    private readonly store: DemoStore,
  ) {}

  async answer(method: string, url: URL, body: string | null): Promise<Response> {
    const path = url.pathname;
    if (path === "/api/session") {
      return this.session(method);
    }
    if (!this.signedIn) {
      return this.refuse(`${method} ${path} while logged out`, "Use Continue as op_demo_operator to replay the recorded session.");
    }
    if (method === "GET" && GLOBAL_READS.has(path)) {
      return this.globalRead(`${method} ${path}${url.search}`);
    }
    if (method === "POST" && path === "/api/runs") {
      return this.createRun(parseBody(body));
    }
    const match = RUN_PATH.exec(path);
    if (match === null) {
      return this.refuse(`${method} ${path}`, "");
    }
    const runId = decoded(match[1] ?? "");
    const run = this.manifest.runs.find((item) => item.run_id === runId);
    if (run === undefined) {
      return this.refuse(`run ${runId}`, "The recording holds one run for each flow: choose one under Open run.");
    }
    const rest = match[2] ?? "";
    if (method === "GET") {
      return this.read(run, rest, url);
    }
    return this.change(run, method, rest, url, parseBody(body));
  }

  private stageOf(run: RecordedRun): number {
    return this.stages.get(run.run_id) ?? 0;
  }

  private session(method: string): Response {
    if (method === "POST") {
      this.signedIn = true;
      return replayed(this.manifest.session.login);
    }
    if (method === "DELETE") {
      this.signedIn = false;
      return replayed(this.manifest.session.logout);
    }
    const answers = this.manifest.global["GET /api/session"];
    if (method === "GET" && this.signedIn && answers !== undefined) {
      return this.next("global|GET /api/session", answers);
    }
    return this.refuse(`${method} /api/session after logging out`, "Use Continue as op_demo_operator to replay the recorded session.");
  }

  private globalRead(key: string): Response {
    const answers = this.manifest.global[key];
    return answers === undefined ? this.refuse(key, "") : this.next(`global|${key}`, answers);
  }

  // Answers recorded as a sequence (a diagnosis job that was queued, then done) are served in order; the last repeats.
  private next(counterKey: string, answers: RecordedAnswer[]): Response {
    const count = this.served.get(counterKey) ?? 0;
    this.served.set(counterKey, count + 1);
    const answer = answers[Math.min(count, answers.length - 1)];
    if (answer === undefined) {
      throw new Error(`the recording holds an empty answer list for ${counterKey}`);
    }
    return replayed(answer);
  }

  private stageFile(run: RecordedRun, stage: number): Promise<StageFile> {
    const recorded = run.stages[stage];
    if (recorded === undefined) {
      return Promise.reject(new Error(`${run.run_id} has no recorded stage ${stage}`));
    }
    let file = this.files.get(recorded.file);
    if (file === undefined) {
      file = this.loadOwn(recorded.file)
        .then((response) => response.json() as Promise<StageFile>)
        .then((loaded) => {
          if (loaded.format !== FORMAT || loaded.run_id !== run.run_id || loaded.stage !== stage) {
            throw new Error(`${recorded.file} is not stage ${stage} of ${run.run_id}`);
          }
          return loaded;
        });
      file.catch(() => this.files.delete(recorded.file)); // a failed load is tried again on the next request
      this.files.set(recorded.file, file);
    }
    return file;
  }

  private async recordedAnswers(run: RecordedRun, stage: number, key: string): Promise<RecordedAnswer[] | null> {
    const entry = (await this.stageFile(run, stage)).answers[key];
    if (entry === undefined) {
      return null;
    }
    if ("answers" in entry) {
      return entry.answers;
    }
    const earlier = (await this.stageFile(run, entry.same_as_stage)).answers[key];
    if (earlier === undefined || !("answers" in earlier)) {
      throw new Error(`stage ${stage} of ${run.run_id} points at stage ${entry.same_as_stage} for ${key}, which lacks it`);
    }
    return earlier.answers;
  }

  private async read(run: RecordedRun, rest: string, url: URL): Promise<Response> {
    const receipt = /^\/actions\/([^/]+)\/receipt$/.exec(rest);
    if (receipt !== null) {
      return this.receipt(run, decoded(receipt[1] ?? ""), url.searchParams.get("format") === "markdown");
    }
    const stage = this.stageOf(run);
    const key = `GET ${url.pathname}${url.search}`;
    const answers = await this.recordedAnswers(run, stage, key);
    if (answers === null) {
      return this.refuseRead(run, rest, url);
    }
    return this.next(`${run.run_id}|${stage}|${key}`, answers);
  }

  // A receipt is served from the file the build wrote: the bytes the API answered, once the action exists in this flow.
  private async receipt(run: RecordedRun, actionId: string, markdown: boolean): Promise<Response> {
    const receipt = run.receipts.find((item) => item.action_id === actionId);
    if (receipt === undefined || this.stageOf(run) < receipt.from_stage) {
      return this.refuse(`the receipt of action ${actionId} at this point of ${run.run_id}'s flow`, "");
    }
    const file = await this.loadOwn(markdown ? receipt.markdown : receipt.json);
    return new Response(await file.text(), {
      status: 200,
      headers: { "Content-Type": markdown ? receipt.markdown_type : receipt.json_type },
    });
  }

  private change(run: RecordedRun, method: string, rest: string, url: URL, request: unknown): Response {
    const stage = this.stageOf(run);
    const next = run.transitions[stage];
    if (
      next !== undefined &&
      next.method === method &&
      next.path === url.pathname &&
      next.query === url.search &&
      canonical(next.request) === canonical(request)
    ) {
      this.stages.set(run.run_id, stage + 1);
      return replayed(next.answer);
    }
    return this.refuseChange(run, next, rest, request);
  }

  private refuse(what: string, hint: string): Response {
    const detail = `Not in this recorded demo: ${what}. ${hint ? `${hint} ` : ""}${COVERS}`;
    this.store.setNotice(detail);
    return jsonAnswer(501, { error: REFUSAL_ERROR, detail });
  }

  // A recorded scenario and seed opens its recorded run, as public mode opens a run that exists already (HTTP 200, the
  // run's own document from the recorded run list). Any other scenario or seed is not in the recording.
  private createRun(request: unknown): Response {
    const scenario = String(field(request, "scenario"));
    const seed = String(field(request, "seed"));
    const recorded = this.manifest.runs.find((run) => run.scenario === scenario && String(run.seed) === seed);
    const document = recorded === undefined ? undefined : this.recordedRunDocument(recorded.run_id);
    if (document !== undefined) {
      return jsonAnswer(200, document);
    }
    return this.refuse(
      `creating a run (${scenario}, seed ${seed})`,
      "The recording holds one run for each of the README's flows: create one of them, or choose it under Open run.",
    );
  }

  private recordedRunDocument(runId: string): unknown {
    const listed = this.manifest.global["GET /api/runs"]?.[0];
    if (listed === undefined) {
      return undefined;
    }
    const runs: unknown = JSON.parse(listed.body);
    return Array.isArray(runs) ? runs.find((run: unknown) => field(run, "run_id") === runId) : undefined;
  }

  private refuseRead(run: RecordedRun, rest: string, url: URL): Response {
    const at = `at this point of ${run.run_id}'s flow`;
    const order = /^\/orders\/([^/]+)$/.exec(rest);
    if (order !== null) {
      const asOf = Number(url.searchParams.get("as_of_ms"));
      const when = Number.isFinite(asOf) && asOf > 0 ? `at replay time ${replayTime(asOf)}` : at;
      return this.refuse(
        `the case of ${decoded(order[1] ?? "")} ${when}`,
        "The recording holds the cases of the orders each Show filter lists first (by order, by Amount and by Age) and of the orders the README names.",
      );
    }
    if (rest === "/orders") {
      return this.refuse(`the order queue with this filter ${at}`, "");
    }
    if (rest === "/report") {
      return this.refuse(`the report ${at}`, "");
    }
    return this.refuse(`${url.pathname}${url.search} ${at}`, "");
  }

  private refuseChange(run: RecordedRun, next: RecordedTransition | undefined, rest: string, request: unknown): Response {
    const nextStep = next === undefined
      ? `The recording of ${run.run_id} ends at this point; use Restart the demo, at the top of the page, to take it again.`
      : `The recording's next step for ${run.run_id} is to ${next.label}.`;
    if (rest === "/advance") {
      const minutes = Number(field(request, "by_ms")) / 60_000;
      return this.refuse(
        `advancing the replay clock of ${run.run_id} by ${minutes} min`,
        "The recorded runs move only through the README's steps, one replay minute per operator step.",
      );
    }
    if (rest === "/diagnoses") {
      const mode = field(request, "mode");
      if (mode !== "rules") {
        const label = isModelMode(mode) ? MODE_LABELS[mode] : String(mode);
        return this.refuse(`a diagnosis in ${label} mode`, "The recording holds rules-baseline diagnoses only, and nothing here calls a model.");
      }
      if (next?.path.endsWith("/diagnoses") !== true) {
        return this.refuse(
          `a second diagnosis of ${runLabel(run)}`,
          "The recording diagnosed each run once. Opening a run clears the page's diagnosis view, as it does with the real server; use Restart the demo, at the top of the page, to take this flow again.",
        );
      }
    }
    const fix = /^\/fixes\/([^/]+)\/validate$/.exec(rest);
    if (fix !== null) {
      const recordedFix = next === undefined ? null : /\/fixes\/([^/]+)\/validate$/.exec(next.path);
      const hint = recordedFix === null
        ? nextStep
        : `The recording validated ${decoded(recordedFix[1] ?? "")}, as the README's Flow A does: choose it under Corrected handler.`;
      return this.refuse(`validating the corrected handler ${decoded(fix[1] ?? "")} on ${run.run_id}`, hint);
    }
    return this.refuse(`${this.describeChange(rest, request)} on ${run.run_id} at this point of its flow`, nextStep);
  }

  private describeChange(rest: string, request: unknown): string {
    const plan = /^\/plans\/([^/]+)\/(approve|execute)$/.exec(rest);
    if (plan !== null) {
      return `${plan[2] === "approve" ? "approving" : "executing"} plan ${decoded(plan[1] ?? "")}`;
    }
    if (rest === "/plans") {
      const operation = field(request, "operation");
      if (operation === "reprocess_inbox") {
        return "building a reprocessing plan";
      }
      return operation === "escalate" ? "escalating" : `the operation ${String(operation)}`;
    }
    if (rest === "/refresh") {
      const payments = field(request, "payment_ids");
      return `refreshing provider state for ${Array.isArray(payments) ? payments.length : 0} payments`;
    }
    return `the request ${rest}`;
  }
}

function toUrl(input: RequestInfo | URL, base: string): URL {
  if (input instanceof URL) {
    return input;
  }
  return new URL(typeof input === "string" ? input : input.url, base);
}

function unavailable(problem: unknown): Response {
  const reason = problem instanceof Error ? problem.message : String(problem);
  console.error("The recorded demo could not answer from its recording:", problem);
  return jsonAnswer(503, {
    error: "RecordingUnavailable",
    detail: `The recorded demo could not load its recorded answers (${reason}). Reload the page; if it persists, the site lacks its demo-data folder.`,
  });
}

// Replaces window.fetch: requests for the API (/api/... and /health) are answered from the recording; requests for
// the page's own files go out unchanged; a request to any other origin is refused before it is made.
export function installReplay(target: Window): DemoReplay {
  const realFetch = target.fetch.bind(target);
  const folder = new URL(".", target.document.baseURI);
  const loadOwn = async (path: string): Promise<Response> => {
    const response = await realFetch(new URL(path, folder), { cache: path === MANIFEST_PATH ? "no-cache" : "default" });
    if (!response.ok) {
      throw new Error(`${path} answered HTTP ${response.status}`);
    }
    return response;
  };
  const store = new DemoStore();
  const recorded = loadOwn(MANIFEST_PATH)
    .then(async (response) => checkManifest(await response.json()))
    .then((manifest) => {
      store.loaded(manifest.runs);
      return new RecordedApi(manifest, loadOwn, store);
    });
  recorded.catch((problem: unknown) => console.error("The recorded demo could not load its manifest:", problem));

  target.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = toUrl(input, target.location.href);
    if (url.origin !== target.location.origin) {
      throw new TypeError(`The recorded demo makes no request outside its own origin; it did not send one to ${url.origin}.`);
    }
    if (url.pathname !== "/health" && !url.pathname.startsWith("/api/")) {
      return realFetch(input, init);
    }
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const body = typeof init?.body === "string" ? init.body : null;
    try {
      const response = await (await recorded).answer(method, url, body);
      if (response.ok) {
        // The page clears its error banner when it starts its next request; the banner's copy of a refusal goes with it.
        store.setNotice(null);
      }
      return response;
    } catch (problem) {
      return unavailable(problem);
    }
  };
  return store;
}
