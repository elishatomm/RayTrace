import { useEffect, useRef, useState } from "react";
import { api, receiptLink } from "../api";
import { CASE_STATE_LABELS, NEXT_STEP_LABELS, caseTone, duration, orderAmount, replayTime } from "../format";
import type { CaseReceipt, JsonValue, OrderCase, OrderCaseDetail, PaymentEvidence, TimelineEntry } from "../types";
import { JsonText, UntrustedText } from "./JsonText";
import type { Perform } from "./RecoveryPanel";

interface CaseDetailProps {
  runId: string;
  orderId: string;
  asOfMs: number; // the queue's replay time: the case is read at the same time as the list it was picked from
  perform: Perform;
  onClose: () => void;
}

// What one load answered; `detail` is undefined when it failed (the error banner says why).
interface Loaded {
  key: string;
  detail: OrderCaseDetail | undefined;
}

const STAGE_LABELS: Record<TimelineEntry["stage"], string> = {
  order: "Order",
  provider: "Provider",
  delivery: "Delivery",
  inbox: "Inbox",
  worker: "Worker",
};

function plain(value: JsonValue): string {
  if (value === null) {
    return "none";
  }
  if (Array.isArray(value)) {
    return value.map(plain).join(", ");
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return `${value}`;
}

function fact(entry: TimelineEntry, name: string): string {
  const value = entry.facts[name];
  return value === undefined ? "none" : plain(value);
}

// One line per record, from its typed fields; the record itself is below it, as text.
function headline(entry: TimelineEntry): string {
  switch (entry.kind) {
    case "order_created":
      return "order created";
    case "order_cancelled":
      return "order cancelled";
    case "payment_attempt":
      return `payment attempt started (${fact(entry, "method")})`;
    case "provider_observation":
      return `provider state ${fact(entry, "status")}, from a ${fact(entry, "source")}`;
    case "delivery_attempt":
      return `delivery attempt ${fact(entry, "attempt_no")}: ${fact(entry, "outcome")} (HTTP ${fact(entry, "http_status")})`;
    case "inbox_event":
      return `${fact(entry, "event_type")} event stored durably`;
    case "conflicting_delivery":
      return `same event ID with a different body, held for review (differs in ${fact(entry, "differing_fields")})`;
    case "worker_attempt":
      return `worker attempt: ${fact(entry, "result")}`;
    case "fulfilment":
      return `order fulfilled (via ${fact(entry, "via")})`;
  }
}

function freshnessText(orderCase: OrderCase): string {
  const freshness = orderCase.freshness;
  if (freshness.newest_observation_at_ms === null) {
    return "no provider observation of its payments is known yet";
  }
  const stale = freshness.stale_payment_ids.length === 0 ? "none" : freshness.stale_payment_ids.join(", ");
  return `newest provider observation known at ${replayTime(freshness.newest_observation_at_ms)}, ${duration(freshness.age_ms)} before this time; payments with stale provider state: ${stale}`;
}

function coverageText(orderCase: OrderCase): string {
  const coverage = orderCase.coverage;
  const unobserved = coverage.unobserved_payment_ids.length === 0 ? "none" : coverage.unobserved_payment_ids.join(", ");
  return `${orderCase.payments.length} payments: ${coverage.payments_with_attempt_row} with the merchant's attempt row, ${coverage.payments_seen_by_webhook} seen by webhook, ${coverage.payments_seen_by_snapshot} by provider snapshot; never observed: ${unobserved}; captured events received: ${coverage.captured_events_received}`;
}

function Receipts({ runId, receipts }: Readonly<{ runId: string; receipts: CaseReceipt[] }>) {
  if (receipts.length === 0) {
    return <p>No recovery action touched this order by this replay time, so there is no receipt.</p>;
  }
  return (
    <ul className="compact">
      {receipts.map((receipt) => (
        <li key={receipt.action_id}>
          Action {receipt.action_id} of plan {receipt.plan_id}: {receipt.status} at {replayTime(receipt.executed_at_ms)};
          result for this order: {receipt.order_result ?? "not recorded"}
          {receipt.order_reasons.length > 0 && <> ({receipt.order_reasons.join(", ")})</>}.{" "}
          <a href={receiptLink(runId, receipt.action_id, "json")} target="_blank" rel="noopener noreferrer">
            Receipt (JSON)
          </a>{" "}
          <a href={receiptLink(runId, receipt.action_id, "markdown")} target="_blank" rel="noopener noreferrer">
            Receipt (Markdown)
          </a>
        </li>
      ))}
    </ul>
  );
}

function Payments({ payments }: Readonly<{ payments: PaymentEvidence[] }>) {
  if (payments.length === 0) {
    return <p>No payment attempt or observation names this order.</p>;
  }
  // The table keeps its columns and scrolls sideways inside its own box when the case view is narrower than it.
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Payment</th><th>Status</th><th className="num">Amount</th><th>Late authorisation</th><th>Conflicts</th>
            <th>Last known</th><th>Stale</th><th className="num">Webhook / snapshot observations</th>
          </tr>
        </thead>
        <tbody>
          {payments.map((payment) => (
            <tr key={payment.payment_id}>
              <td className="mono">{payment.payment_id}</td>
              <td>{payment.status}</td>
              <td className="num">{payment.amount_paise === null ? "not agreed" : `${payment.amount_paise} paise ${payment.currency ?? ""}`}</td>
              <td>{payment.late_authorization ? "yes" : "no"}</td>
              <td>{payment.conflict_reasons.join(", ") || "none"}</td>
              <td className="mono">{payment.last_known_at_ms === null ? "never observed" : replayTime(payment.last_known_at_ms)}</td>
              <td>{payment.stale ? "yes" : "no"}</td>
              <td className="num">{payment.webhook_observations} / {payment.snapshot_observations}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Timeline({ entries }: Readonly<{ entries: TimelineEntry[] }>) {
  if (entries.length === 0) {
    return <p>No record names this order by this replay time.</p>;
  }
  return (
    <ol className="timeline">
      {entries.map((entry) => (
        <li key={`${entry.kind}-${entry.record_id}`}>
          <span className="clock">{replayTime(entry.at_ms)}</span> <span className="stage">{STAGE_LABELS[entry.stage]}</span>: {headline(entry)}
          {entry.payment_id !== null && <> (payment {entry.payment_id})</>}
          <details>
            <summary>{entry.kind} record {entry.record_id}</summary>
            <JsonText value={entry.facts} />
            {entry.text !== null && <UntrustedText text={entry.text} />}
          </details>
        </li>
      ))}
    </ol>
  );
}

export function CaseDetail({ runId, orderId, asOfMs, perform, onClose }: Readonly<CaseDetailProps>) {
  const key = `${runId}/${orderId}@${asOfMs}`;
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    let current = true;
    void perform("load order case", () => api.orderCase(runId, orderId, asOfMs)).then((detail) => {
      if (current) {
        setLoaded({ key: `${runId}/${orderId}@${asOfMs}`, detail });
      }
    });
    return () => {
      current = false;
    };
  }, [runId, orderId, asOfMs, perform]);

  // Opening a case, by keyboard or mouse, moves focus to its heading, so the next Tab is the case's Close button
  // rather than every row left in the table. A reload at a new replay time is not an opening: focus stays put.
  useEffect(() => {
    heading.current?.focus();
  }, [runId, orderId]);

  // The Close button is set at the heading's right edge by position, not by flex: a flex heading would split
  // "Case <order>" into separate blocks in its rendered text.
  const header = (
    <h3 ref={heading} tabIndex={-1}>
      Case <span className="mono">{orderId}</span> <button type="button" onClick={onClose}>Close</button>
    </h3>
  );
  if (loaded?.key !== key) {
    return <div className="case-detail">{header}<p className="note">Loading the case...</p></div>;
  }
  if (loaded.detail === undefined) {
    return <div className="case-detail">{header}<p>The case could not be loaded; the message above says why.</p></div>;
  }
  const { detail } = loaded;
  const orderCase = detail.case;
  return (
    <div className="case-detail">
      {header}
      <p className="as-of">As of replay time {replayTime(detail.as_of_ms)}.</p>
      <dl className="fields">
        <dt>Amount</dt>
        <dd className="money">{orderAmount(orderCase.amount_paise, orderCase.currency, orderCase.amount_status)}</dd>
        <dt>Case state</dt>
        <dd><strong className={`tone-text tone-${caseTone(orderCase.state, orderCase.next_step)}`}>{CASE_STATE_LABELS[orderCase.state]}</strong>: {detail.state_condition}</dd>
        <dt>Next step</dt>
        <dd><strong>{NEXT_STEP_LABELS[orderCase.next_step]}</strong></dd>
        <dt>Blockers (every one)</dt>
        <dd className="blockers">{orderCase.blockers.join(", ") || "none"}</dd>
        <dt>Age</dt>
        <dd>{duration(detail.summary.age_ms)}</dd>
        <dt>Evidence freshness</dt>
        <dd>{freshnessText(orderCase)}</dd>
        <dt>Coverage</dt>
        <dd>{coverageText(orderCase)}</dd>
      </dl>
      <h4>Recovery receipts</h4>
      <Receipts runId={runId} receipts={detail.receipts} />
      <h4>Payments</h4>
      <Payments payments={orderCase.payments} />
      <h4>Timeline: provider, delivery and inbox, worker, order (by replay time)</h4>
      <Timeline entries={detail.timeline} />
    </div>
  );
}
