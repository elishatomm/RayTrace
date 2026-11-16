import { type FormEvent, type RefObject, useRef, useState } from "react";
import { useFocusAfterSwap } from "../focus";

interface LoginPanelProps {
  focusBeforeSwap: RefObject<Element | null>; // after a logout, the token field takes focus the Log out button had
  onLogin: (token: string, operatorId: string) => void;
}

// The token is printed once on the server console at start; the browser keeps only the
// httpOnly session cookie the server sets in exchange.
export function LoginPanel({ focusBeforeSwap, onLogin }: Readonly<LoginPanelProps>) {
  const [token, setToken] = useState("");
  const [operatorId, setOperatorId] = useState("op_demo_operator");
  const tokenField = useRef<HTMLInputElement>(null);
  const startButton = useRef<HTMLButtonElement>(null);
  useFocusAfterSwap(focusBeforeSwap, tokenField);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onLogin(token.trim(), operatorId.trim());
    // Clearing the token disables Start session, and a disabled button drops focus to the page body. If the button
    // holds focus, the token field takes it: after a failed login, the token is what has to be entered again.
    if (startButton.current === document.activeElement) {
      tokenField.current?.focus();
    }
    setToken("");
  }

  return (
    <section className="panel login">
      <h2>Operator login</h2>
      <p className="note">Paste the operator token the server printed when it started. Approvals carry this operator ID.</p>
      <form onSubmit={submit}>
        <label>
          Operator token<input ref={tokenField} type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} />
        </label>
        <label>
          Operator ID (op_...)<input value={operatorId} pattern="op_[a-z0-9][a-z0-9_]{0,62}" onChange={(event) => setOperatorId(event.target.value)} />
        </label>
        <button ref={startButton} type="submit" className="primary" disabled={token.trim() === ""}>Start session</button>
      </form>
    </section>
  );
}
