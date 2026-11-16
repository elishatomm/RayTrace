// Shapes of the workbench API responses (src/recovery_workbench/api.py). Money is integer paise.

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export type ModelMode = "local" | "recorded" | "rules" | "hosted";

// The model source the server chose once, at start: the local model, a hosted model (with its host) or the rules.
export interface ModelSource {
  mode: ModelMode;
  model_id: string | null;
  provider_host: string | null; // the hosted model's host; null for every other source
  label: string; // the server's own wording, shown as it is
}

export type Operation =
  | "reprocess_inbox"
  | "prepare_provider_replay"
  | "refresh_provider_state"
  | "escalate"
  | "no_action";

export type EvidenceLevel = "insufficient" | "supported" | "reproduced_in_fixture";

// GET /health needs no session. Outside public mode it holds every field below but status; the server's public mode
// answers {"status": "ok"} alone. So every field is optional, the page reads none without checking, and it takes its
// model source from ScenarioList, behind the session.
export interface Health {
  status?: string;
  app?: string;
  version?: string;
  environment?: string;
  execution_mode?: ModelMode;
  model?: {
    id: string;
    thinking: boolean;
    checked: boolean;
    available: boolean | null;
    digest: string | null;
  };
  model_source?: ModelSource;
  versions?: { prompt: string; validator: string; packet: string; policy: string; report: string };
  wall_clock_ms?: number;
}

export interface SessionState {
  operator_id: string | null;
}

export interface ScenarioList {
  scenarios: string[];
  execution_mode: ModelMode;
  model_modes: ModelMode[]; // what a diagnosis may ask for here: hosted only when the server has its settings
  model_source: ModelSource; // the page reads the model source here: /health may say less (public mode)
}

export interface RunSummary {
  run_id: string;
  scenario: string;
  seed: number;
  split: string;
  environment: string;
  generator_version: string;
  model_mode: ModelMode;
  replay_clock_ms: number;
  incident_ids: string[];
  created_wall_ms: number;
}

export interface Ratio {
  numerator: number;
  denominator: number;
  value: number | null;
}

export interface ProviderMeasure {
  cohort: string;
  as_of_ms: number;
  window_start_ms: number;
  window_end_ms: number;
  authorization: Ratio;
  capture: Ratio;
  coverage: Ratio;
  unresolved: Ratio;
  freshness_ms: number | null;
}

export interface MerchantMeasures {
  as_of_ms: number;
  completion: Ratio;
  pending_captured_orders: number;
  pending_captured_value_paise: number;
  logical_events: number;
  delivery_attempts: number;
  acknowledged_attempts: number;
  rejected_attempts: number;
  backlog_rows: number;
  rows_with_failures: number;
  manual_review_rows: number;
  worker_failures: number;
  oldest_backlog_age_ms: number | null;
}

export interface IncidentSummary {
  incident_id: string;
  trigger: string;
  cohort: Record<string, JsonValue>;
  evidence_cutoff_ms: number;
  state: string;
  fired_detectors: string[] | null;
}

export interface DetectorResult {
  detector: string;
  cohort: Record<string, JsonValue>;
  fired: boolean;
  reason: string;
  window_start_ms: number;
  window_end_ms: number;
}

export interface Deployment {
  deploy_id: string;
  service_id: string;
  kind: string;
  deployed_at_ms: number;
  summary: string;
  files_changed: string[];
}

export interface IncidentsResponse {
  run: { run_id: string; clock_ms: number; as_of_ms: number };
  windows: { horizon_ms: number; window_ms: number; baseline_ms: number; stale_after_ms: number };
  provider: { current: ProviderMeasure[]; baseline: ProviderMeasure[] };
  merchant: MerchantMeasures;
  backlog: Record<string, number>;
  worker_failures: Record<string, number>;
  deployments: Deployment[];
  incidents: IncidentSummary[];
  detectors_now: DetectorResult[];
  definitions: Record<string, string>;
}

// An order with a payment that failed first and authorised later. Since policy-v3 a late
// authorisation alone excludes nothing; `exclusions` are the order's own (empty: it may be planned).
export interface LateAuthorisedOrder {
  order_id: string;
  payment_ids: string[];
  exclusions: string[];
  fulfilled: boolean;
}

export interface PaymentState {
  late_authorisations: {
    payments: number;
    captured: number;
    orders: number;
    fulfilled_orders: number;
    would_collect_twice: number;
    collected_twice: string[];
    unfulfilled: LateAuthorisedOrder[];
  };
  refresh_candidates: string[];
}

export interface DiagnosisOutput {
  hypothesis: string;
  faulting_change_id: string;
  affected_method: string;
  evidence_level: EvidenceLevel;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  missing_checks: string[];
  next_check: string;
  proposed_operation: Operation;
  rationale: string;
}

export interface UnsafeProposal {
  call_index: number;
  rule: string;
  hypothesis: string;
  proposed_operation: string;
  faulting_change_id: string | null;
  allowed_operations: string[];
}

export interface DiagnosisDocument {
  diagnosis_id: string;
  incident_id: string;
  mode: ModelMode;
  mode_label: string;
  status: "validated" | "needs_review";
  model_id: string | null;
  model_digest: string | null;
  thinking: boolean | null;
  prompt_version: string | null;
  validator_version: string;
  rules_version: string | null;
  recorded_from: string | null;
  provider_host?: string; // hosted diagnoses only
  packet_hash: string;
  output: DiagnosisOutput | null;
  reasons: string[];
  unsafe_proposals: UnsafeProposal[];
  elapsed_ms: number | null;
}

export interface EvidenceItem {
  evidence_id: string;
  kind: string;
  known_at_ms: number;
  title: string;
  facts: Record<string, JsonValue>;
  text: string | null;
}

export interface EvidencePacket {
  packet_version: string;
  incident: {
    run_id: string;
    incident_id: string;
    symptom: string;
    trigger: string;
    cohort: Record<string, JsonValue>;
    window_start_ms: number;
    window_end_ms: number;
    evidence_cutoff_ms: number;
    summary_counts: Record<string, number>;
  };
  items: EvidenceItem[];
  candidate_change_ids: string[];
  candidate_methods: string[];
}

export interface DiagnosisJob {
  job_id: string;
  run_id: string;
  incident_id: string;
  mode: ModelMode;
  evidence_cutoff_ms: number;
  status: "queued" | "running" | "done" | "failed";
  error: string | null;
  created_wall_ms: number;
  finished_wall_ms: number | null;
  diagnosis: DiagnosisDocument | null;
  packet: EvidencePacket | null;
}

export interface FixOption {
  fix_id: string;
  label: string;
  files: string[];
}

export interface FixValidation {
  validation_id: string;
  fix_id: string;
  deployed_version: string;
  reproduced: boolean;
  deployed_failures: string[];
  corrected_failures: string[];
  passed: boolean;
  operator_id: string;
}

export interface PlanTarget {
  id: string;
  payment_id: string;
  inbox_id: number;
  event_id: string;
  amount_paise: number;
  currency: string;
}

export interface ExcludedOrder {
  order_id: string;
  inbox_id: number;
  reasons: string[];
}

export interface PlanPreview {
  plan_id: string;
  plan_hash: string;
  operation: Operation;
  expires_at_ms: number;
  target_count: number;
  target_value_paise: number;
  targets: PlanTarget[];
  exclusions_by_reason: Record<string, string[]>;
  dry_runs_passed: number;
  preconditions: string[];
  deployed_version: string;
  corrected_version: string;
  excluded: ExcludedOrder[];
  stale_only_orders: { order_id: string; payment_ids: string[] }[];
  candidate_orders: number;
  definitions: { plan_value: string };
}

export interface Approval {
  run_id: string;
  approval_id: string;
  plan_id: string;
  plan_hash: string;
  operator_id: string;
  approved_at_ms: number;
  expires_at_ms: number;
}

export interface ExecutionResult {
  run_id: string;
  plan_id: string;
  action_id: string;
  operation_key: string;
  status: "succeeded" | "rejected";
  reasons: string[];
  fulfilled_order_ids: string[];
  restored_processing_value_paise: number;
  additional_captured_paise: number;
  executed_at_ms: number;
  replayed: boolean;
}

export interface RefreshSummary {
  run_id: string;
  now_ms: number;
  refreshed: string[];
  already_recorded: string[];
  unknown_at_provider: string[];
  statuses: Record<string, string>;
}

export interface EscalationRecord {
  operation: "escalate";
  action_id: string;
  summary: string;
  evidence_needed: string[];
  subject_ids: string[];
  operator_id: string;
}

// The stored plan record an escalation or a replay answer carries (plans.StoredPlan); only the fields the UI reads.
export interface StoredPlanRecord {
  plan_id: string;
  plan_hash: string;
  operation: Operation;
  targets: Record<string, JsonValue>[];
  target_value_paise: number;
}

export interface ReplayInstructions {
  operation: "prepare_provider_replay";
  plan: StoredPlanRecord;
  instructions: string[];
  // Captures whose order is blocked (the reprocess planner's exclusions); never resent until settled.
  held_for_reconciliation: { payment_id: string; order_id: string; reasons: string[] }[];
  total_value_paise: number;
  provider_calls_made: number;
}

// One reprocess action's receipt (GET /api/runs/{run_id}/actions/{action_id}/receipt, report.build_receipt); only
// the fields the UI reads. The value section holds the definitions of restored value and new capture.
export interface ActionReceipt {
  receipt_version: string;
  receipt_hash: string;
  identity: { action_id: string; plan_id: string };
  value: Record<string, JsonValue>;
}

// The order recovery queue (GET /api/runs/{run_id}/orders and /orders/{order_id}); src/recovery_workbench/order_cases.py.

export type CaseState =
  | "payment_order_mismatch"
  | "multiple_captures"
  | "business_state_conflict"
  | "payment_state_unresolved"
  | "capture_not_confirmed"
  | "complete"
  | "captured_processing_closed"
  | "notification_acknowledged_not_stored"
  | "notification_not_accepted"
  | "captured_processing_incomplete"
  | "payment_failed"
  | "insufficient_evidence";

export type NextStep =
  | "manual_review"
  | "refresh_then_reevaluate"
  | "hold"
  | "prepare_provider_replay"
  | "validate_fix_then_reprocess"
  | "none";

export type AmountStatus = "asserted" | "conflicting" | "unknown" | "other_currency";

export interface QueueRow {
  order_id: string;
  order_known: boolean;
  amount_paise: number | null;
  currency: string | null;
  amount_status: AmountStatus;
  state: CaseState;
  next_step: NextStep;
  blockers: string[];
  age_ms: number | null;
  freshness_ms: number | null;
  newest_observation_at_ms: number | null;
}

export interface StateTotal {
  state: CaseState;
  orders: number;
  asserted_value_paise: number;
  unasserted_orders: number;
}

// Whole-queue figures at as_of_ms: a filter never changes them.
export interface QueueTotals {
  currency: string;
  captured_unprocessed_orders: number;
  captured_unprocessed_value_paise: number;
  unresolved_orders: number;
  unresolved_value_paise: number;
  unresolved_unasserted_orders: number;
  by_state: StateTotal[];
}

export interface QueueCoverage {
  orders: number;
  unknown_orders: number;
  payments: number;
  observations: number;
  inbox_rows: number;
  worker_attempts: number;
  delivery_attempts: number;
  rejected_deliveries: number;
  deliveries_without_inbox_row: number;
  newest_observation_at_ms: number | null;
}

export interface StateRule {
  state: CaseState;
  next_step: NextStep;
  condition: string;
  unresolved: boolean;
}

export interface QueueHeader {
  run_id: string;
  as_of_ms: number;
  replay_clock_ms: number;
  stale_after_ms: number;
  classifier_version: string;
  refund_flag_read: boolean;
}

export interface OrderQueuePage extends QueueHeader {
  filters: { state: CaseState[]; next_step: NextStep[]; blocker: string | null };
  totals: QueueTotals;
  coverage: QueueCoverage;
  states: StateRule[];
  matching_orders: number;
  orders: QueueRow[];
  next_after: string | null;
  definitions: Record<string, string>;
}

export interface PaymentEvidence {
  payment_id: string;
  status: string;
  amount_paise: number | null;
  currency: string | null;
  attempt_order_id: string | null;
  late_authorization: boolean;
  conflict_reasons: string[];
  last_known_at_ms: number | null;
  stale: boolean;
  webhook_observations: number;
  snapshot_observations: number;
}

export interface OrderCase {
  order_id: string;
  order_known: boolean;
  amount_paise: number | null;
  currency: string | null;
  amount_status: AmountStatus;
  state: CaseState;
  next_step: NextStep;
  blockers: string[];
  payments: PaymentEvidence[];
  capture_processing: {
    captured_inbox_ids: number[];
    unsettled_inbox_ids: number[];
    target_inbox_id: number | null;
    worker_failures: number;
    settled_results: string[];
  };
  fulfilment: Record<string, JsonValue> | null;
  freshness: {
    newest_observation_at_ms: number | null;
    age_ms: number | null;
    stale_payment_ids: string[];
    stale_open_payment_ids: string[];
  };
  coverage: {
    payments_with_attempt_row: number;
    payments_seen_by_webhook: number;
    payments_seen_by_snapshot: number;
    unobserved_payment_ids: string[];
    captured_events_received: number;
  };
  evidence: Record<string, JsonValue>;
}

export type TimelineKind =
  | "order_created"
  | "payment_attempt"
  | "provider_observation"
  | "delivery_attempt"
  | "inbox_event"
  | "conflicting_delivery"
  | "worker_attempt"
  | "fulfilment"
  | "order_cancelled";

export interface TimelineEntry {
  at_ms: number;
  stage: "order" | "provider" | "delivery" | "inbox" | "worker";
  kind: TimelineKind;
  record_id: string;
  payment_id: string | null;
  facts: Record<string, JsonValue>;
  text: string | null; // untrusted (a worker stack trace)
}

export interface CaseReceipt {
  action_id: string;
  plan_id: string;
  status: "succeeded" | "rejected" | "failed" | "unknown_outcome";
  executed_at_ms: number;
  order_result: "completed" | "no_op" | "refused" | "failed" | null;
  order_reasons: string[];
  receipt_path: string;
}

export interface OrderCaseDetail extends QueueHeader {
  summary: QueueRow;
  case: OrderCase;
  state_condition: string;
  timeline: TimelineEntry[];
  receipts: CaseReceipt[];
  definitions: Record<string, string>;
}
