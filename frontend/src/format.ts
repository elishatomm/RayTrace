// Presentation helpers. Values stay as the API sent them; these only format for reading.
import type { AmountStatus, CaseState, EvidenceLevel, ModelMode, NextStep, Ratio } from "./types";

export function replayTime(epochMs: number): string {
  return new Date(epochMs).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, "Z");
}

export function duration(ms: number | null): string {
  if (ms === null) {
    return "n/a";
  }
  const seconds = Math.round(ms / 1000);
  if (seconds < 120) {
    return `${seconds} s`;
  }
  const minutes = Math.round(seconds / 60);
  return minutes < 120 ? `${minutes} min` : `${(minutes / 60).toFixed(1)} h`;
}

export function rupees(paise: number): string {
  const sign = paise < 0 ? "-" : "";
  const whole = Math.floor(Math.abs(paise) / 100);
  const fraction = String(Math.abs(paise) % 100).padStart(2, "0");
  return `${sign}INR ${whole.toLocaleString("en-US")}.${fraction}`;
}

export function ratio(value: Ratio): string {
  if (value.denominator === 0 || value.value === null) {
    return `no data (0 of 0)`;
  }
  return `${value.value.toFixed(4)} (${value.numerator} of ${value.denominator})`;
}

export const MODE_LABELS: Record<ModelMode, string> = {
  local: "Local model",
  recorded: "Recorded run",
  rules: "Rules baseline",
  hosted: "Hosted model",
};

// What every hosted label adds: the M4 evaluation measured qwen3:8b on the development laptop, not any hosted model.
export const HOSTED_NOTE = "not part of the M4 evaluation";

// A label, never a probability: the contract has three levels and no numbers.
export const EVIDENCE_LEVEL_LABELS: Record<EvidenceLevel, string> = {
  insufficient: "Insufficient evidence",
  supported: "Supported by evidence",
  reproduced_in_fixture: "Reproduced in a fixture test",
};

// Names for the queue's states and next steps; the server decides every state and step.
export const CASE_STATE_LABELS: Record<CaseState, string> = {
  payment_order_mismatch: "Payment/order mismatch",
  multiple_captures: "Multiple captures",
  business_state_conflict: "Business-state conflict",
  payment_state_unresolved: "Payment state unresolved",
  capture_not_confirmed: "Capture not confirmed",
  complete: "Complete",
  captured_processing_closed: "Captured; processing closed",
  notification_acknowledged_not_stored: "Notification acknowledged, not stored",
  notification_not_accepted: "Notification not accepted",
  captured_processing_incomplete: "Captured; processing incomplete",
  payment_failed: "Payment failed",
  insufficient_evidence: "Insufficient evidence",
};

export const NEXT_STEP_LABELS: Record<NextStep, string> = {
  manual_review: "Manual review",
  refresh_then_reevaluate: "Refresh provider state, then re-evaluate",
  hold: "Hold: no fulfilment until a capture is confirmed",
  prepare_provider_replay: "Prepare provider replay instructions",
  validate_fix_then_reprocess: "Validate the fix, then an approved reprocess plan",
  none: "None",
};

const AMOUNT_NOTES: Record<Exclude<AmountStatus, "asserted">, string> = {
  conflicting: "the evidence disagrees: counted, not summed",
  other_currency: "another currency: counted, not summed",
  unknown: "no order record",
};

// An order's amount as shown, and the note that qualifies it (null when the amount is asserted). orderAmount joins
// the two as "shown (note)"; the queue table sets the note in a lighter style without changing that text.
export function orderAmountParts(
  amountPaise: number | null, currency: string | null, status: AmountStatus,
): { shown: string; note: string | null } {
  if (amountPaise === null || currency === null) {
    return { shown: "unknown", note: AMOUNT_NOTES.unknown };
  }
  const shown = currency === "INR" ? rupees(amountPaise) : `${currency} ${amountPaise} (minor units)`;
  return { shown, note: status === "asserted" ? null : AMOUNT_NOTES[status] };
}

export function orderAmount(amountPaise: number | null, currency: string | null, status: AmountStatus): string {
  const { shown, note } = orderAmountParts(amountPaise, currency, status);
  return note === null ? shown : `${shown} (${note})`;
}

export function shortDigest(digest: string | null): string {
  if (digest === null) {
    return "no digest";
  }
  const bare = digest.replace(/^sha256:/, "");
  return `${bare.slice(0, 12)}...`;
}

// The first 12 hex digits of a SHA-256 (packet, plan, approval or receipt hash), shown with an ellipsis after it.
export function hashPrefix(hash: string): string {
  return hash.replace(/^sha256:/, "").slice(0, 12);
}

export function countOf(count: number, noun: string, plural = `${noun}s`): string {
  return `${count} ${count === 1 ? noun : plural}`;
}

// What colour a case state carries; the state's own words always sit beside it. Stuck or incomplete is amber,
// complete green, failed red, manual review violet; insufficient evidence asks for nothing, so it stays neutral.
export type Tone = "complete" | "stuck" | "failed" | "review" | "neutral";

export function caseTone(state: CaseState, nextStep: NextStep): Tone {
  if (state === "complete") {
    return "complete";
  }
  if (state === "payment_failed") {
    return "failed";
  }
  if (nextStep === "manual_review") {
    return "review";
  }
  return nextStep === "none" ? "neutral" : "stuck";
}

export function describeError(problem: unknown): string {
  if (problem instanceof Error) {
    return "errorName" in problem ? `${String(problem.errorName)}: ${problem.message}` : problem.message;
  }
  return String(problem);
}
