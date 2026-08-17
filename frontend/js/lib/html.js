/**
 * Shared HTML/markdown-safe escaping helpers. Phase 1 of the 2026-08-16 bug-hunt
 * remediation: route every attribute-context interpolation through `attr()` and
 * every markdown-lite pipeline through `fmtMd()`. `esc()` stays text-node only.
 * Do NOT extend `esc()` to escape quotes; that regresses its legitimate callers.
 */

// Text-node escaping. Escapes & < > only, deliberately. Safe for element
// content, unsafe for attribute values.
export function esc(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

// Attribute-value escaping. Escapes & < > " ', so it is safe to interpolate
// into <tag attr="${attr(x)}"> for double-quoted attributes. Single-quoted
// attributes also remain closed because ' is escaped.
export function attr(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Markdown-lite, escape-then-format. The response is escaped as ONE STRING
// first, so the ** and ` capture groups below carry already-inert text into
// the <strong>/<code> tags. Interpolating raw text into those groups (the old
// pattern in workflows.js) is itself the injection vector. Links are limited
// to http(s) and escape the url via attr().
export function fmtMd(raw) {
  let s = esc(raw);
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  // [label](url) links, http(s) scheme only. LLM-supplied hrefs must not
  // become javascript: or data: URLs.
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (m, label, url) => {
    // url was captured from esc()'d text, so & is already &amp;; undo before
    // attr() or query-string links double-escape to &amp;amp; and break.
    const u = url.replace(/&amp;/g, '&');
    return `<a href="${attr(u)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  return s;
}
