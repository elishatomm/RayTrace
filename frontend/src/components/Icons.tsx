import type { ReactNode } from "react";

// Small inline icons. Each one sits beside visible text that carries the meaning, so each is hidden from assistive
// technology. They are drawn in the text's own colour (currentColor), as SVG elements: nothing is fetched, so the
// page's CSP (img-src 'self' data:) has nothing to allow.
function Glyph({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <svg
      className="icon"
      viewBox="0 0 16 16"
      width="14"
      height="14"
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

export function IconDone() {
  return (
    <Glyph>
      <circle cx="8" cy="8" r="6.2" />
      <path d="M5.3 8.2l1.9 1.9 3.6-3.9" />
    </Glyph>
  );
}

export function IconCurrent() {
  return (
    <Glyph>
      <circle cx="8" cy="8" r="6.2" />
      <circle cx="8" cy="8" r="2.5" fill="currentColor" stroke="none" />
    </Glyph>
  );
}

export function IconPending() {
  return (
    <Glyph>
      <circle cx="8" cy="8" r="6.2" strokeDasharray="2.4 2.2" />
    </Glyph>
  );
}

export function IconSkipped() {
  return (
    <Glyph>
      <circle cx="8" cy="8" r="6.2" />
      <path d="M4 12l8-8" />
    </Glyph>
  );
}

export function IconFailed() {
  return (
    <Glyph>
      <circle cx="8" cy="8" r="6.2" />
      <path d="M5.9 5.9l4.2 4.2M10.1 5.9l-4.2 4.2" />
    </Glyph>
  );
}

// A flag: the step ran and its answer is for a person to review.
export function IconReview() {
  return (
    <Glyph>
      <path d="M4.3 14V2.6" />
      <path d="M4.3 3h7.4l-1.8 2.9 1.8 2.9H4.3" />
    </Glyph>
  );
}

export function IconAlert() {
  return (
    <Glyph>
      <path d="M8 2.3l6 10.9H2z" />
      <path d="M8 6.6v2.8" />
      <path d="M8 11.4v.1" />
    </Glyph>
  );
}

export function IconExternal() {
  return (
    <Glyph>
      <path d="M9.6 2.6h3.8v3.8" />
      <path d="M13.4 2.6L7.6 8.4" />
      <path d="M12 9.6v3.1c0 .4-.3.7-.7.7H3.3c-.4 0-.7-.3-.7-.7V4.7c0-.4.3-.7.7-.7h3.1" />
    </Glyph>
  );
}

export function IconSort() {
  return (
    <Glyph>
      <path d="M5.5 6.2L8 3.7l2.5 2.5M5.5 9.8L8 12.3l2.5-2.5" />
    </Glyph>
  );
}

export function IconSortDown() {
  return (
    <Glyph>
      <path d="M8 3.5v8.6M4.8 9l3.2 3.2L11.2 9" />
    </Glyph>
  );
}

export function IconSortUp() {
  return (
    <Glyph>
      <path d="M8 12.5V3.9M4.8 7L8 3.8 11.2 7" />
    </Glyph>
  );
}
