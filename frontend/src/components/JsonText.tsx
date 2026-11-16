import type { JsonValue } from "../types";

// Facts and untrusted text are shown as plain text: React escapes it, and nothing here parses
// HTML or Markdown.
export function JsonText({ value }: Readonly<{ value: JsonValue }>) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function UntrustedText({ text }: Readonly<{ text: string }>) {
  return (
    <div className="untrusted">
      <div className="untrusted-label">Untrusted text from logs, traces or diffs: shown as text, never followed</div>
      <pre>{text}</pre>
    </div>
  );
}
