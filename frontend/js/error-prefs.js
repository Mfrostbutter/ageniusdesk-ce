// Shared "error reporting window" preference: how far back error reporting goes.
// Persistent and per-browser (localStorage), honored by the Overview Recent Errors
// widget + Failure Rate card and the Errors view, so they all report the same span.
// Set from the Overview widget header or Settings > Error Handler.

const KEY = 'ageniusdesk:error_lookback';

export const LOOKBACK_OPTIONS = [
  { value: '24h', label: 'Last 24 hours', short: '24h' },
  { value: '7d',  label: 'Last 7 days',   short: '7d' },
  { value: '30d', label: 'Last 30 days',  short: '30d' },
  { value: '90d', label: 'Last 90 days',  short: '90d' },
  { value: 'all', label: 'All time',      short: 'all time' },
];

export const DEFAULT_LOOKBACK = '30d';

const VALID = new Set(LOOKBACK_OPTIONS.map(o => o.value));

export function getErrorLookback() {
  try {
    const v = localStorage.getItem(KEY);
    if (v && VALID.has(v)) return v;
  } catch { /* localStorage may be unavailable */ }
  return DEFAULT_LOOKBACK;
}

export function setErrorLookback(value) {
  if (!VALID.has(value)) return getErrorLookback();
  try { localStorage.setItem(KEY, value); } catch { /* ignore */ }
  return value;
}

// Compact label for inline use in a stat trend, e.g. "30d" / "all time".
export function lookbackShort(value) {
  return (LOOKBACK_OPTIONS.find(o => o.value === value) || {}).short || value;
}

// Build <option> markup for a <select>, marking `selected` as current.
export function lookbackOptionsHtml(selected) {
  return LOOKBACK_OPTIONS
    .map(o => `<option value="${o.value}"${o.value === selected ? ' selected' : ''}>${o.label}</option>`)
    .join('');
}
