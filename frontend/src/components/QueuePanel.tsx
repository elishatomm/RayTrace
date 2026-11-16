import { type RefCallback, type RefObject, useEffect, useRef, useState } from "react";
import { api, type QueueQuery } from "../api";
import { CASE_STATE_LABELS, NEXT_STEP_LABELS, caseTone, duration, orderAmountParts, replayTime } from "../format";
import type { CaseState, OrderQueuePage, QueueRow, QueueTotals, StateRule } from "../types";
import { CaseDetail } from "./CaseDetail";
import { IconSort, IconSortDown, IconSortUp } from "./Icons";
import type { Perform } from "./RecoveryPanel";

// The whole-queue figures of the latest page, for the run header's money strip. A filter never changes them.
export interface QueueSnapshot {
  run_id: string;
  as_of_ms: number;
  totals: QueueTotals;
  definitions: Record<string, string>;
}

interface QueuePanelProps {
  runId: string;
  replayClockMs: number; // the queue reloads at the run's clock whenever the clock moves
  perform: Perform;
  onLoaded: (snapshot: QueueSnapshot) => void;
}

interface QueueFilter {
  key: string;
  label: string;
  query: QueueQuery;
}

type SortKey = "order" | "age" | "amount";

interface SortOrder {
  key: SortKey;
  descending: boolean;
}

const NO_FILTER: QueueQuery = { state: [], next_step: [] };
const ALL_ORDERS: QueueFilter = { key: "all", label: "All orders", query: NO_FILTER };

// The four worklists first, then every state in the server's precedence order. The server applies the filter.
function filterChoices(rules: StateRule[]): QueueFilter[] {
  const states = (...chosen: CaseState[]): QueueQuery => ({ state: chosen, next_step: [] });
  return [
    ALL_ORDERS,
    { key: "captured_unprocessed", label: "Captured, processing incomplete", query: states("captured_processing_incomplete") },
    { key: "unresolved", label: "Payment state unresolved", query: states("payment_state_unresolved") },
    { key: "manual_review", label: "Manual review", query: { state: [], next_step: ["manual_review"] } },
    { key: "completed", label: "Complete", query: states("complete") },
    {
      key: "every_unresolved",
      label: "Every state still awaiting an action or a hold",
      query: states(...rules.filter((rule) => rule.unresolved).map((rule) => rule.state)),
    },
    ...rules.map((rule) => ({ key: `state:${rule.state}`, label: `State: ${CASE_STATE_LABELS[rule.state]}`, query: states(rule.state) })),
  ];
}

// How many orders of the whole queue a filter admits, from the whole-queue totals.
function wholeQueueCount(page: OrderQueuePage, query: QueueQuery): number {
  const admitted = new Set(
    page.states
      .filter((rule) => query.state.length === 0 || query.state.includes(rule.state))
      .filter((rule) => query.next_step.length === 0 || query.next_step.includes(rule.next_step))
      .map((rule) => rule.state),
  );
  return page.totals.by_state.filter((total) => admitted.has(total.state)).reduce((sum, total) => sum + total.orders, 0);
}

function sortValue(row: QueueRow, key: SortKey): number | null {
  if (key === "age") {
    return row.age_ms;
  }
  return key === "amount" ? row.amount_paise : null;
}

// Rows without the value (an order with no record has no age or amount) go last whichever way the sort runs.
function compareNullsLast(left: number | null, right: number | null, direction: number): number {
  if (left === null && right === null) {
    return 0;
  }
  if (left === null) {
    return 1;
  }
  if (right === null) {
    return -1;
  }
  return (left - right) * direction;
}

function sortedRows(rows: QueueRow[], order: SortOrder): QueueRow[] {
  if (order.key === "order") {
    return rows; // the server lists them by order ID
  }
  const direction = order.descending ? -1 : 1;
  return [...rows].sort(
    (left, right) =>
      compareNullsLast(sortValue(left, order.key), sortValue(right, order.key), direction) ||
      left.order_id.localeCompare(right.order_id),
  );
}

function sortNote(order: SortOrder, key: SortKey): string {
  if (order.key !== key || key === "order") {
    return "";
  }
  return order.descending ? " (largest first)" : " (smallest first)";
}

// aria-sort for the column the rows are ordered by: order-ID order counts as ascending.
function ariaSort(order: SortOrder, key: SortKey): "ascending" | "descending" | undefined {
  if (order.key !== key) {
    return undefined;
  }
  return order.descending ? "descending" : "ascending";
}

function SortIcon({ order, sortKey }: Readonly<{ order: SortOrder; sortKey: SortKey }>) {
  if (order.key !== sortKey) {
    return <IconSort />;
  }
  return order.descending ? <IconSortDown /> : <IconSortUp />;
}

interface SortHeaderProps {
  label: string;
  sortKey: SortKey;
  order: SortOrder;
  numeric?: boolean;
  onSort: (key: SortKey) => void;
}

// The map in `buttons` holds the order button of each row on screen: React runs the cleanup when a row leaves the table.
function rowButtonRef(buttons: RefObject<Map<string, HTMLButtonElement>>, orderId: string): RefCallback<HTMLButtonElement> {
  return (button) => {
    if (button === null) {
      return;
    }
    buttons.current.set(orderId, button);
    return () => {
      buttons.current.delete(orderId);
    };
  };
}

function SortHeader({ label, sortKey, order, numeric = false, onSort }: Readonly<SortHeaderProps>) {
  return (
    <th className={numeric ? "num" : undefined} aria-sort={ariaSort(order, sortKey)}>
      <button type="button" className="link sort" onClick={() => onSort(sortKey)}>
        {label}
        {sortNote(order, sortKey)}
        <SortIcon order={order} sortKey={sortKey} />
      </button>
    </th>
  );
}

function Coverage({ page }: Readonly<{ page: OrderQueuePage }>) {
  const read = page.coverage;
  const newest = read.newest_observation_at_ms === null ? "none" : replayTime(read.newest_observation_at_ms);
  return (
    <p className="versions">
      Read by this time: {read.orders} orders ({read.unknown_orders} named only by payment evidence), {read.payments}{" "}
      payments, {read.observations} provider observations (newest known {newest}), {read.inbox_rows} inbox rows,{" "}
      {read.worker_attempts} worker attempts, {read.delivery_attempts} delivery attempts ({read.rejected_deliveries}{" "}
      rejected, {read.deliveries_without_inbox_row} with no inbox row of their own, resent bodies included). Classifier {page.classifier_version}; provider
      state older than {duration(page.stale_after_ms)} is stale.
    </p>
  );
}

function AmountCell({ row }: Readonly<{ row: QueueRow }>) {
  const { shown, note } = orderAmountParts(row.amount_paise, row.currency, row.amount_status);
  return (
    <td className="num">
      <span className="money">{shown}</span>
      {note !== null && <span className="cell-note"> ({note})</span>}
    </td>
  );
}

export function QueuePanel({ runId, replayClockMs, perform, onLoaded }: Readonly<QueuePanelProps>) {
  const [filter, setFilter] = useState<QueueFilter>(ALL_ORDERS);
  const [page, setPage] = useState<OrderQueuePage | null>(null);
  const [sortOrder, setSortOrder] = useState<SortOrder>({ key: "order", descending: false });
  const [selected, setSelected] = useState<string | null>(null);
  const rowButtons = useRef(new Map<string, HTMLButtonElement>());
  const queueTable = useRef<HTMLTableElement>(null);

  useEffect(() => {
    let current = true;
    void perform("load order queue", async () => {
      const loaded = await api.orderQueue(runId, filter.query);
      if (current) {
        setPage(loaded);
        onLoaded({ run_id: loaded.run_id, as_of_ms: loaded.as_of_ms, totals: loaded.totals, definitions: loaded.definitions });
      }
    });
    return () => {
      current = false;
    };
  }, [runId, replayClockMs, filter, perform, onLoaded]);

  if (page === null) {
    return (
      <section className="panel queue">
        <h2>Order recovery queue</h2>
        <p>Loading the queue...</p>
      </section>
    );
  }

  const choices = filterChoices(page.states);
  function choose(key: string) {
    const chosen = choices.find((choice) => choice.key === key);
    if (chosen !== undefined) {
      setFilter(chosen);
    }
  }
  // A first click sorts largest first (oldest, or highest amount); a second click reverses it.
  function sortBy(key: SortKey) {
    setSortOrder((previous) => ({ key, descending: previous.key === key ? !previous.descending : key !== "order" }));
  }
  // Focus returns to the row that opened the case or, when a filter or a reload has removed that row, to the table.
  // It moves before the case view unmounts, so it never drops to the page body.
  function closeCase() {
    const opener = selected === null ? undefined : rowButtons.current.get(selected);
    (opener ?? queueTable.current)?.focus();
    setSelected(null);
  }
  function toggleCase(orderId: string) {
    if (orderId === selected) {
      closeCase();
      return;
    }
    setSelected(orderId);
  }

  return (
    <section className="panel queue">
      <h2>Order recovery queue</h2>
      <p className="lead">
        One case per order as of replay time {replayTime(page.as_of_ms)} (the run's replay clock), with its next step and
        every blocker. Derived from observations only; nothing here calls a model.
      </p>
      {!page.refund_flag_read && <p className="definition">{page.definitions.refund_flag_read}</p>}
      <Coverage page={page} />
      <div className="row">
        <label>
          Show<select value={filter.key} onChange={(event) => choose(event.target.value)}>
            {choices.map((choice) => (
              <option key={choice.key} value={choice.key}>
                {choice.label} ({wholeQueueCount(page, choice.query)})
              </option>
            ))}
          </select>
        </label>
        <span>
          {page.matching_orders} orders match; sort by clicking Age or Amount.
        </span>
      </div>
      <div className="scroll">
        <table ref={queueTable} tabIndex={-1}>
          <thead>
            <tr>
              <SortHeader label="Order" sortKey="order" order={sortOrder} onSort={sortBy} />
              <SortHeader label="Amount" sortKey="amount" order={sortOrder} numeric onSort={sortBy} />
              <th>Case state</th>
              <SortHeader label="Age" sortKey="age" order={sortOrder} numeric onSort={sortBy} />
              <th className="num">Evidence freshness</th>
              <th>Next step</th>
              <th>Blockers</th>
            </tr>
          </thead>
          <tbody>
            {sortedRows(page.orders, sortOrder).map((row) => (
              <tr key={row.order_id} className={row.order_id === selected ? "selected" : undefined}>
                <td>
                  <button
                    type="button"
                    className="link order"
                    ref={rowButtonRef(rowButtons, row.order_id)}
                    aria-current={row.order_id === selected ? "true" : undefined}
                    onClick={() => toggleCase(row.order_id)}
                  >
                    {row.order_id}
                  </button>
                </td>
                <AmountCell row={row} />
                <td><span className={`chip tone-${caseTone(row.state, row.next_step)}`}>{CASE_STATE_LABELS[row.state]}</span></td>
                <td className="num">{duration(row.age_ms)}</td>
                <td className="num">{row.freshness_ms === null ? "no observation" : duration(row.freshness_ms)}</td>
                <td>{NEXT_STEP_LABELS[row.next_step]}</td>
                <td className="blockers">{row.blockers.join(", ") || "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected === null ? (
        <div className="case-placeholder">
          <p>No case is open. Choose an order ID in the queue to see its state, blockers, payments, timeline and receipts.</p>
        </div>
      ) : (
        <CaseDetail runId={runId} orderId={selected} asOfMs={page.as_of_ms} perform={perform} onClose={closeCase} />
      )}
    </section>
  );
}
