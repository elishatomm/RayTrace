import { useState } from "react";
import { api } from "../api";
import { asciiJson } from "../asciiJson";
import { RequestButton } from "./BusyButton";
import type { Perform } from "./RecoveryPanel";

// Downloads the run's report-1 JSON: evidence, diagnoses with mode labels, validations, plans,
// approvals, actions, outcome, versions, and both the replay and the wall clock.
export function ReportPanel({ runId, perform }: Readonly<{ runId: string; perform: Perform }>) {
  const [exported, setExported] = useState<string | null>(null);

  async function exportReport() {
    const report = await perform("export report", () => api.report(runId));
    if (report === undefined) {
      return;
    }
    const blob = new Blob([asciiJson(report)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `report-${runId}.json`;
    link.click();
    URL.revokeObjectURL(url);
    setExported(link.download);
  }

  return (
    <section className="panel report">
      <h2>Report</h2>
      <p className="note">
        The run's report-1 JSON: evidence, diagnoses with their mode labels, validations, plans, approvals, actions,
        outcome, versions, and both the replay and the wall clock.
      </p>
      <div className="row">
        <RequestButton onClick={exportReport}>Export report (JSON)</RequestButton>
        {exported !== null && <span className="saved"> Saved as {exported}.</span>}
      </div>
    </section>
  );
}
