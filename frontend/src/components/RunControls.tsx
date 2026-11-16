import { type RefObject, useRef, useState } from "react";
import { useFocusAfterSwap } from "../focus";
import type { RunSummary } from "../types";
import { BusyButton } from "./BusyButton";

interface RunControlsProps {
  scenarios: string[];
  runs: RunSummary[];
  current: RunSummary | null;
  busy: boolean;
  focusBeforeSwap: RefObject<Element | null>; // after a login, the Scenario select takes focus the login form had
  onCreate: (scenario: string, seed: number) => void;
  onOpen: (runId: string) => void;
  onAdvance: (byMs: number) => void;
}

const ADVANCE_STEPS: [string, number][] = [
  ["+1 min", 60_000],
  ["+15 min", 15 * 60_000],
  ["+1 h", 60 * 60_000],
];

export function RunControls({ scenarios, runs, current, busy, focusBeforeSwap, onCreate, onOpen, onAdvance }: Readonly<RunControlsProps>) {
  const [scenario, setScenario] = useState("");
  const chosenScenario = scenario || scenarios[0] || "";  // the list arrives after the first render
  const [seed, setSeed] = useState("101");
  const seedValue = Number.parseInt(seed, 10);
  const seedValid = /^\d{1,9}$/.test(seed) && seedValue >= 0;
  const scenarioSelect = useRef<HTMLSelectElement>(null);
  useFocusAfterSwap(focusBeforeSwap, scenarioSelect);

  // A flat toolbar on purpose: the walkthrough finds Scenario as the first label, Create as the first button and the
  // clock buttons inside span.advance, all direct children of this section.
  return (
    <section className="panel controls" aria-label="Run controls">
      <label>
        Scenario<select ref={scenarioSelect} value={chosenScenario} onChange={(event) => setScenario(event.target.value)}>
          {scenarios.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
        </select>
      </label>
      <label>
        Seed<input className="seed" value={seed} inputMode="numeric" onChange={(event) => setSeed(event.target.value)} />
      </label>
      <BusyButton variant="primary" busy={busy} disabled={!seedValid || chosenScenario === ""} onClick={() => onCreate(chosenScenario, seedValue)}>
        Create synthetic run
      </BusyButton>
      <label className="open-run">
        Open run<select value={current?.run_id ?? ""} onChange={(event) => event.target.value && onOpen(event.target.value)}>
          <option value="">choose a run</option>
          {runs.map((item) => (
            <option key={item.run_id} value={item.run_id}>
              {item.run_id} ({item.scenario}, seed {item.seed})
            </option>
          ))}
        </select>
      </label>
      <span className="advance">
        <span className="advance-label">Advance replay clock:</span>
        {ADVANCE_STEPS.map(([label, byMs]) => (
          <BusyButton key={label} busy={busy} disabled={current === null} onClick={() => onAdvance(byMs)}>
            {label}
          </BusyButton>
        ))}
      </span>
    </section>
  );
}
