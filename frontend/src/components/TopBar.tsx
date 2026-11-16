import type { ReactNode } from "react";
import { HOSTED_NOTE, MODE_LABELS, replayTime, shortDigest } from "../format";
import type { DiagnosisJob, Health, ModelSource, RunSummary } from "../types";
import { RequestButton } from "./BusyButton";

interface TopBarProps {
  health: Health | null;
  source: ModelSource | null; // the server's model source (GET /api/scenarios), while an operator is logged in
  run: RunSummary | null;
  job: DiagnosisJob | null;
  wallClockMs: number;
  operator: string | null;
  onLogout: () => Promise<void>;
}

// One fact of the context line, read as "Label: value". The colon and space stay in the text, so the line reads the
// same to a screen reader and to a search of the page as it looks.
function Fact({ label, mono = false, children }: Readonly<{ label: string; mono?: boolean; children: ReactNode }>) {
  return (
    <span className={mono ? "fact clock" : "fact"}>
      <span className="fact-label">{label}:</span> <span className="fact-value">{children}</span>
    </span>
  );
}

export function TopBar({ health, source, run, job, wallClockMs, operator, onLogout }: Readonly<TopBarProps>) {
  const diagnosis = job?.diagnosis ?? null;
  // /health may hold only its status (public mode), so each of its fields is a fallback that can be missing.
  const mode = diagnosis?.mode ?? job?.mode ?? source?.mode ?? health?.execution_mode ?? null;
  const modelId = diagnosis?.model_id ?? (mode === "rules" ? null : source?.model_id ?? health?.model?.id ?? null);
  const digest = diagnosis?.model_digest ?? (mode === "local" ? health?.model?.digest ?? null : null);
  const host = diagnosis?.provider_host ?? source?.provider_host ?? null; // a hosted model reports a host, no digest
  return (
    <header className="topbar">
      <div className="topbar-main">
        <h1 className="product">RayTrace</h1>
        <span className="badge sandbox">Synthetic demonstration</span>
        <span className={`badge mode mode-${mode ?? "unknown"}`}>{mode === null ? "Mode unknown" : MODE_LABELS[mode]}</span>
        {mode === "recorded" && <span className="badge recorded">RECORDED: replay of stored model output, not live inference</span>}
        {mode === "hosted" && <span className="badge hosted">HOSTED: {HOSTED_NOTE}</span>}
        {operator !== null && (
          <span className="operator">
            Operator <span className="operator-id">{operator}</span> <RequestButton onClick={onLogout}>Log out</RequestButton>
          </span>
        )}
      </div>
      <div className="topbar-facts">
        {source !== null && <Fact label="Model source">{source.label}</Fact>}
        <Fact label="Run" mono>{run?.run_id ?? "none"}</Fact>
        <Fact label="Seed" mono>{run?.seed ?? "none"}</Fact>
        <Fact label="Evidence cutoff" mono>{job === null ? "no diagnosis yet" : replayTime(job.evidence_cutoff_ms)}</Fact>
        <Fact label="Model" mono>
          {modelId ?? (mode === "rules" ? "none (rules)" : "unknown")}
          {modelId !== null && (mode === "hosted" ? ` via ${host ?? "an unknown host"} (no digest)` : ` (digest ${shortDigest(digest)})`)}
        </Fact>
        <Fact label="Replay clock" mono>{run === null ? "n/a" : replayTime(run.replay_clock_ms)}</Fact>
        <Fact label="Wall clock" mono>{replayTime(wallClockMs)}</Fact>
      </div>
    </header>
  );
}
