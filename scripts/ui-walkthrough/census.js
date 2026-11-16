// Agent U, phase 11: runs before any page script. Counts every element type ever inserted into the document,
// notes any element the HTML parser did not recognise (what markup taken from text would create), and records
// Content-Security-Policy violations. The walkthrough reads window.__walkthroughCensus at the end.
// A popup keeps its initial about:blank window when it navigates to a same-origin page, so this can run twice
// in one window: the second run reuses the census and watches the new document.
(() => {
  if (!("__walkthroughCensus" in window)) {
    Object.defineProperty(window, "__walkthroughCensus", { value: { added: {}, unknown: [], csp: [] } });
  }
  const census = window.__walkthroughCensus;
  const count = (node) => {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = node.tagName.toLowerCase();
    census.added[tag] = (census.added[tag] || 0) + 1;
    if (node instanceof HTMLUnknownElement) census.unknown.push(tag);
    for (const child of node.children) count(child);
  };
  new MutationObserver((records) => {
    for (const record of records) for (const node of record.addedNodes) count(node);
  }).observe(document, { childList: true, subtree: true });
  document.addEventListener("securitypolicyviolation", (event) => {
    census.csp.push({
      directive: event.violatedDirective, blocked: event.blockedURI, sample: event.sample,
      source: event.sourceFile, line: event.lineNumber,
    });
  });
})();
