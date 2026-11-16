import type { JsonValue } from "./types";

// The report file the UI saves, escaped as the CLI writes its own: Python's json.dumps writes every UTF-16 code unit
// from U+007F up as an escape, so that file is ASCII. JSON.stringify keeps those characters raw, C1 controls, bidi
// overrides and U+2028/U+2029 from webhook text among them, which a terminal or editor showing the file may act on.
// Without the u flag the pattern matches single code units, so a character beyond the BMP becomes the escapes of its
// surrogate pair, as in Python.
export function asciiJson(value: JsonValue): string {
  return JSON.stringify(value, null, 2).replace(
    /[\u007f-\uffff]/g,
    (unit) => String.raw`\u${unit.codePointAt(0)!.toString(16).padStart(4, "0")}`,
  );
}
