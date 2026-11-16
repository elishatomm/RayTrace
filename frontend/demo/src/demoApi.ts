// Stands in for src/api.ts in the demo build only (frontend/demo/vite.config.ts swaps it for every importer but this
// file). Everything is src/api.ts's own, except that a receipt link points at the receipt file the build wrote beside
// the page, the bytes the API answered while recording. The page's own read of a receipt (the recovery rail's hash)
// still goes through src/api.ts to the replay.
export * from "../../src/api";

export function receiptLink(runId: string, actionId: string, format: "json" | "markdown"): string {
  const extension = format === "markdown" ? "md" : "json";
  return `receipts/${encodeURIComponent(runId)}/${encodeURIComponent(actionId)}.${extension}`;
}
