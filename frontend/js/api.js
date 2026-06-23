/**
 * API client + WebSocket connection for AgeniusDesk.
 */

const BASE = '';  // Same origin

// ── HTTP ────────────────────────────────────────────────────────────────────

export async function api(path, options = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const detail = body.detail;
    const message = (detail && typeof detail === 'object')
      ? (detail.message || `HTTP ${resp.status}`)
      : (detail || `HTTP ${resp.status}`);
    const err = new Error(message);
    err.status = resp.status;
    if (detail && typeof detail === 'object') err.errorClass = detail.error_class || 'generic';
    throw err;
  }
  return resp.json();
}

export const get = (path) => api(path);
export const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
export const put = (path, body) => api(path, { method: 'PUT', body: JSON.stringify(body) });
export const patch = (path, body) => api(path, { method: 'PATCH', body: JSON.stringify(body) });
export const del = (path) => api(path, { method: 'DELETE' });

// ── WebSocket ───────────────────────────────────────────────────────────────

let ws = null;
let reconnectTimer = null;
const listeners = new Map();  // event -> Set<callback>

export function onEvent(event, callback) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(callback);
  return () => listeners.get(event).delete(callback);
}

function dispatch(event, data) {
  const cbs = listeners.get(event);
  if (cbs) cbs.forEach(cb => cb(data));
}

export function connectWS() {
  if (ws && ws.readyState <= 1) return;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    dispatch('ws:connected', null);
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  };

  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      dispatch(msg.event, msg.data);
    } catch { /* ignore malformed */ }
  };

  ws.onclose = () => {
    dispatch('ws:disconnected', null);
    reconnectTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => ws.close();
}

export function disconnectWS() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (ws) ws.close();
}
