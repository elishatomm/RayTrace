import { useRef } from "react";
import type { LoginPanel as ProductionLoginPanel } from "../../src/components/LoginPanel";
import { useFocusAfterSwap } from "../../src/focus";

type LoginPanelProps = Parameters<typeof ProductionLoginPanel>[0];

// The operator the recording logged in as: the ID its approvals and escalations carry.
const RECORDED_OPERATOR = "op_demo_operator";

// Stands in for src/components/LoginPanel.tsx in the demo build only (frontend/demo/vite.config.ts swaps it). The
// page shows a login after Log out; the recorded demo has no server and asks for no token.
export function LoginPanel({ focusBeforeSwap, onLogin }: LoginPanelProps) {
  const continueButton = useRef<HTMLButtonElement>(null);
  useFocusAfterSwap(focusBeforeSwap, continueButton);
  return (
    <section className="panel login">
      <h2>Operator login</h2>
      <p className="note">
        No token is needed in this recorded demo: there is no server to log in to. Continuing replays the recorded login
        of {RECORDED_OPERATOR}, the operator the recorded steps name.
      </p>
      <button ref={continueButton} type="button" className="primary" onClick={() => onLogin("", RECORDED_OPERATOR)}>
        Continue as {RECORDED_OPERATOR}
      </button>
    </section>
  );
}
