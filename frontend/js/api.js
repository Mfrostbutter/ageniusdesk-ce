/**
 * API client + WebSocket connection for AgeniusDesk.
 */

const BASE = '';  // Same origin

// ── HTTP ────────────────────────────────────────────────────────────────────

function readCookie(name) {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}

// Global double-submit CSRF: every same-origin mutating request needs the
// `agd_csrf` cookie echoed as a header, or the server 403s it. Patch window.fetch
// once so raw fetch() callers (workflow delete, container actions, the player,
// etc.) are covered too — not just calls routed through api() below. Without
// this, anything bypassing api() breaks under the CSRF middleware.
(function patchFetchForCsrf() {
  if (typeof window === 'undefined' || window.__agdFetchPatched) return;
  const orig = window.fetch.bind(window);
  window.__agdFetchPatched = true;
  window.fetch = (input, init = {}) => {
    try {
      const isReq = typeof Request !== 'undefined' && input instanceof Request;
      const method = (init.method || (isReq ? input.method : 'GET') || 'GET').toUpperCase();
      const url = typeof input === 'string' ? input : (isReq ? input.url : String(input || ''));
      const sameOrigin = url.startsWith('/') || url.startsWith(location.origin);
      if (sameOrigin && method !== 'GET' && method !== 'HEAD') {
        const csrf = readCookie('agd_csrf');
        if (csrf) {
          const headers = new Headers((init && init.headers) || (isReq ? input.headers : undefined) || {});
          if (!headers.has('X-AGD-CSRF')) headers.set('X-AGD-CSRF', csrf);
          init = { ...init, headers };
        }
      }
    } catch { /* never let the shim break a request */ }
    return orig(input, init);
  };
})();

let _authRedirecting = false;

export async function api(path, options = {}, _retried = false) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = { 'Content-Type': 'application/json', ...options.headers };
  // Double-submit CSRF: echo the readable agd_csrf cookie on mutations.
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = readCookie('agd_csrf');
    if (csrf) headers['X-AGD-CSRF'] = csrf;
  }
  const resp = await fetch(`${BASE}${path}`, { headers, ...options });
  if (!resp.ok) {
    // CSRF self-heal: a mutation 403s when the readable agd_csrf cookie was cleared
    // from under a still-valid session (e.g. another AgeniusDesk on a different
    // localhost port clears the shared-domain cookie). Hit /status once (the server
    // re-issues the cookie for a valid session), then retry the original call once.
    if (
      resp.status === 403 && !_retried &&
      method !== 'GET' && method !== 'HEAD' && !path.startsWith('/api/auth/')
    ) {
      const probe = await resp.clone().json().catch(() => ({}));
      if ((probe.detail || '') === 'CSRF check failed') {
        try { await fetch(`${BASE}/api/auth/status`); } catch { /* best effort */ }
        if (readCookie('agd_csrf')) return api(path, options, true);
      }
    }
    // Session EXPIRY mid-use: bounce to the auth gate by reloading. Gated on the
    // readable agd_csrf cookie, which only exists once a session was issued — so
    // pre-login boot (where many background calls 401 by design) never reloads,
    // avoiding a reload loop. Never on the auth endpoints themselves.
    if (
      resp.status === 401 &&
      !path.startsWith('/api/auth/') &&
      !_authRedirecting &&
      readCookie('agd_csrf')
    ) {
      _authRedirecting = true;
      location.reload();
    }
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
