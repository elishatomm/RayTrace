import { type Ref, useRef, useState } from "react";
import { handOverFocus } from "../focus";
import { EVIDENCE_LEVEL_LABELS, HOSTED_NOTE, MODE_LABELS, replayTime, shortDigest } from "../format";
import type { DiagnosisDocument, DiagnosisJob, ModelMode } from "../types";

const JOB_TONES: Record<DiagnosisJob["status"], string> = { queued: "info", running: "info", done: "complete", failed: "failed" };
// The modes whose diagnosis is a model call that can fail: a recorded replay is validated or refused, never reviewed.
const MODEL_CALL_MODES: ReadonlySet<ModelMode> = new Set<ModelMode>(["local", "hosted"]);
import { RequestButton } from "./BusyButton";

interface DiagnosisPanelProps {
  job: DiagnosisJob | null;
  history: DiagnosisDocument[];
  defaultMode: ModelMode;
  modes: ModelMode[]; // what a diagnosis may ask for on this server (GET /api/scenarios)
  canRun: boolean;
  onRun: (mode: ModelMode) => Promise<void>; // settles once the start request is answered
  onRulesInstead: (incidentId: string) => Promise<boolean>; // a rules diagnosis of that incident; true once started
  onCite: (evidenceId: string) => void;
}

function Citations({ ids, onCite }: Readonly<{ ids: string[]; onCite: (evidenceId: string) => void }>) {
  if (ids.length === 0) {
    return <>none</>;
  }
  return (
    <>
      {ids.map((evidenceId) => (
        <button key={evidenceId} type="button" className="link citation" onClick={() => onCite(evidenceId)}>
          {evidenceId}
        </button>
      ))}
    </>
  );
}

interface ResultProps {
  diagnosis: DiagnosisDocument;
  onCite: (evidenceId: string) => void;
  onRulesInstead: (() => Promise<void>) | null; // offered when a model diagnosis needs review
  rulesButton: Ref<HTMLButtonElement>;
}

function Result({ diagnosis, onCite, onRulesInstead, rulesButton }: Readonly<ResultProps>) {
  const output = diagnosis.output;
  return (
    <div className="diagnosis-result">
      {diagnosis.mode === "recorded" && (
        <p className="banner recorded">
          RECORDED RUN: this is a stored {diagnosis.model_id} output ({diagnosis.recorded_from}), replayed because the
          evidence packet is identical. It is not live inference.
        </p>
      )}
      {diagnosis.mode === "hosted" && (
        <p className="banner hosted">
          HOSTED MODEL: {diagnosis.model_id} via {diagnosis.provider_host}. This answer is {HOSTED_NOTE}, which
          measured qwen3:8b on the development laptop.
        </p>
      )}
      <p className="mode-label">{diagnosis.mode_label}</p>
      {diagnosis.status === "needs_review" ? (
        <div className="banner needs-review">
          <strong>Needs review.</strong> The model did not produce a valid diagnosis, so nothing is proposed. The
          deterministic evidence is kept; escalate with it.
          <ul>
            {diagnosis.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          {onRulesInstead !== null && (
            <p className="rules-instead">
              <RequestButton ref={rulesButton} onClick={onRulesInstead}>Diagnose with the rules instead</RequestButton>{" "}
              The rules baseline calls no model; its diagnosis of this incident is labelled as the rules baseline.
            </p>
          )}
        </div>
      ) : (
        output !== null && (
          <dl className="diagnosis-fields">
            <dt>Leading hypothesis</dt>
            <dd><code>{output.hypothesis}</code></dd>
            <dt>Faulting change</dt>
            <dd><code>{output.faulting_change_id}</code></dd>
            <dt>Affected method</dt>
            <dd><code>{output.affected_method}</code></dd>
            <dt>Evidence level (a label, not a probability)</dt>
            <dd><span className={`level level-${output.evidence_level}`}>{EVIDENCE_LEVEL_LABELS[output.evidence_level]}</span></dd>
            <dt>Supporting evidence</dt>
            <dd><Citations ids={output.supporting_evidence_ids} onCite={onCite} /></dd>
            <dt>Contradicting evidence</dt>
            <dd><Citations ids={output.contradicting_evidence_ids} onCite={onCite} /></dd>
            <dt>Missing checks</dt>
            <dd>{output.missing_checks.join(", ") || "none named"}</dd>
            <dt>Next check</dt>
            <dd>{output.next_check}</dd>
            <dt>Proposed operation</dt>
            <dd><strong><code>{output.proposed_operation}</code></strong> (a proposal; a person approves any action)</dd>
            <dt>Rationale</dt>
            <dd className="rationale">{output.rationale}</dd>
          </dl>
        )
      )}
      {diagnosis.status === "validated" && diagnosis.reasons.length > 0 && (
        <div className="repairs">
          Rejected answers before the accepted one:
          <ul>
            {diagnosis.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
      {diagnosis.unsafe_proposals.length > 0 && (
        <div className="unsafe">
          Unsafe proposals blocked by policy and recorded:
          <ul>
            {diagnosis.unsafe_proposals.map((proposal) => (
              <li key={`${proposal.call_index}-${proposal.rule}`}>
                call {proposal.call_index + 1}: {proposal.rule}, {proposal.hypothesis} proposed {proposal.proposed_operation}
                (allowed: {proposal.allowed_operations.join(", ")})
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="versions">
        {diagnosis.model_id !== null && (
          <>
            Model {diagnosis.model_id}
            {diagnosis.mode === "hosted"
              ? <> via {diagnosis.provider_host}, no digest (a hosted provider reports none)</>
              : <>, digest {shortDigest(diagnosis.model_digest)}</>}
            , thinking {String(diagnosis.thinking)};{" "}
          </>
        )}
        prompt {diagnosis.prompt_version ?? "n/a"}, validator {diagnosis.validator_version}
        {diagnosis.rules_version !== null && <>, rules {diagnosis.rules_version}</>}; packet {diagnosis.packet_hash.slice(0, 16)}
        {diagnosis.elapsed_ms !== null && <>; {diagnosis.elapsed_ms} ms</>}
      </p>
    </div>
  );
}

export function DiagnosisPanel({ job, history, defaultMode, modes, canRun, onRun, onRulesInstead, onCite }: Readonly<DiagnosisPanelProps>) {
  const [mode, setMode] = useState<ModelMode | null>(null);
  const diagnoseButton = useRef<HTMLButtonElement>(null);
  const rulesButton = useRef<HTMLButtonElement>(null);
  const chosenMode = mode ?? defaultMode;  // the server's execution mode until the operator picks another
  const running = job !== null && (job.status === "queued" || job.status === "running");
  const shown = job?.diagnosis ?? null;
  const alternatives = history.filter((item) => item.diagnosis_id !== shown?.diagnosis_id);
  // A model that was unavailable or failed leaves its diagnosis needing review; the rules are offered, never substituted.
  const offerRules = shown !== null && shown.status === "needs_review" && MODEL_CALL_MODES.has(shown.mode) && canRun;

  async function diagnoseWithRules(incidentId: string) {
    if (await onRulesInstead(incidentId)) {
      // The new job replaces this diagnosis, and the button with it, so Diagnose takes the focus the button held.
      handOverFocus(rulesButton.current, diagnoseButton.current);
    }
  }

  return (
    <section className="panel diagnosis">
      <h2>Diagnosis</h2>
      <div className="row">
        <label>
          Mode<select value={chosenMode} onChange={(event) => setMode(event.target.value as ModelMode)}>
            {modes.map((option) => (
              <option key={option} value={option}>{MODE_LABELS[option]}</option>
            ))}
          </select>
        </label>
        <RequestButton ref={diagnoseButton} variant="primary" busy={running} disabled={!canRun} onClick={() => onRun(chosenMode)}>
          Diagnose the latest incident
        </RequestButton>
      </div>
      {!canRun && <p className="note">No detector has opened an incident by this replay time, so there is nothing to diagnose.</p>}
      {job !== null && (
        <p className="job-line">
          Job {job.job_id} ({MODE_LABELS[job.mode]}) for {job.incident_id}, evidence cutoff {replayTime(job.evidence_cutoff_ms)}:{" "}
          <strong className={`status tone-${JOB_TONES[job.status]}`}>{job.status}</strong>
          {running && " (one inference at a time; polling)"}
        </p>
      )}
      {job?.status === "failed" && <p className="banner error">The diagnosis could not run: {job.error}</p>}
      {shown !== null && (
        <Result
          diagnosis={shown}
          onCite={onCite}
          onRulesInstead={offerRules ? () => diagnoseWithRules(shown.incident_id) : null}
          rulesButton={rulesButton}
        />
      )}
      {alternatives.length > 0 && (
        <div className="alternatives">
          <h3>Other diagnoses of this run (alternatives)</h3>
          <ul className="compact">
            {alternatives.map((item) => (
              <li key={item.diagnosis_id}>
                {item.mode_label}: {item.status}
                {item.output !== null && <>, {item.output.hypothesis} proposing {item.output.proposed_operation} ({EVIDENCE_LEVEL_LABELS[item.output.evidence_level]})</>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
