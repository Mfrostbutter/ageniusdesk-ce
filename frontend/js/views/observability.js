/**
 * Observe — per-execution OpenTelemetry traces from n8n.
 *
 * Left: recent traces (one per execution) for the active instance. Right: the
 * selected trace as a waterfall. Live-updates when the receiver broadcasts a
 * new trace. When the receiver is off, shows setup instructions instead.
 */

import { get, onEvent } from '../api.js';
import { buildWaterfall } from '../components/trace-waterfall.js';

function esc(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

let _wsBound = false;
let _selected = null;

export async function render(container) {
  container.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px" data-tour="observe-header">
      <div>
        <h1 style="margin:0 0 4px">Observe</h1>
        <p style="margin:0;color:var(--text-secondary);font-size:13px;max-width:640px">Per-execution OpenTelemetry traces from n8n: node-by-node timing and exactly where a run slowed down or failed.</p>
      </div>
      <div id="obs-status" style="white-space:nowrap"></div>
    </div>
    <div id="obs-body"><div class="spinner"></div></div>
  `;
  await load(container);

  // Bind the live-refresh listener once for the app lifetime; it self-guards on
  // the active view so it does nothing while the user is elsewhere.
  if (!_wsBound) {
    onEvent('otel:trace', () => { if (window.__currentView === 'observe') refreshList(); });
    _wsBound = true;
  }
}

async function load(container) {
  let status = {};
  try { status = await get('/api/otel/status'); } catch { /* receiver may be absent */ }

  const statusEl = container.querySelector('#obs-status');
  if (statusEl) {
    statusEl.innerHTML = status.enabled
      ? `<span class="pill pill-success">Receiver on</span> <span class="pill pill-neutral">${status.span_count || 0} spans</span>`
      : '<span class="pill pill-warning">Receiver off</span>';
  }

  const body = container.querySelector('#obs-body');
  if (!status.enabled) {
    body.innerHTML = setupHtml();
    return;
  }
  body.innerHTML = `
    <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap">
      <div style="flex:1 1 340px;min-width:280px">
        <div id="obs-traces-list"><div class="spinner"></div></div>
      </div>
      <div style="flex:2 1 460px;min-width:320px">
        <div id="obs-detail" style="padding:16px;border:1px solid var(--border-dim);border-radius:10px;background:var(--bg-elevated)">
          <div class="empty-state"><p>Select a trace to see its waterfall.</p></div>
        </div>
      </div>
    </div>`;
  await refreshList();
}

function setupHtml() {
  return `
    <div class="empty-state" style="max-width:680px;margin:0 auto;text-align:left">
      <h3>Turn on the trace receiver</h3>
      <p style="color:var(--text-secondary);font-size:13px">Set <code>AGD_OTEL_ENABLED=true</code> (and ideally <code>AGD_OTEL_TOKEN</code>) on AgeniusDesk, then point n8n's native OpenTelemetry exporter at this dashboard and restart n8n:</p>
      <pre style="text-align:left;background:var(--bg-void,rgba(0,0,0,.25));border:1px solid var(--border-dim);border-radius:6px;padding:12px;font-size:12px;font-family:var(--font-mono);color:var(--text-secondary);overflow:auto">N8N_OTEL_ENABLED=true
N8N_OTEL_EXPORTER_OTLP_ENDPOINT=http://&lt;this-host&gt;:&lt;port&gt;/api/otel
N8N_OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer &lt;AGD_OTEL_TOKEN&gt;</pre>
      <p style="font-size:12px;color:var(--text-dim)">Your cron and other production executions stream in here as they run.</p>
    </div>`;
}

async function refreshList() {
  const listEl = document.getElementById('obs-traces-list');
  if (!listEl) return;
  let traces = [];
  try { traces = (await get('/api/otel/traces?limit=50')).traces || []; } catch { /* ignore */ }

  if (!traces.length) {
    listEl.innerHTML = '<div class="empty-state"><p>No traces yet. Run a workflow on the active instance.</p></div>';
    return;
  }

  listEl.innerHTML = traces.map(t => `
    <button class="obs-trace-row" data-trace="${esc(t.trace_id)}"
      style="display:block;width:100%;text-align:left;border:1px solid ${t.trace_id === _selected ? 'var(--accent)' : 'var(--border-dim)'};border-radius:8px;background:var(--bg-elevated);padding:10px 12px;margin-bottom:8px;cursor:pointer">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
        <span style="font-size:13px;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(t.workflow_name || '(unknown workflow)')}</span>
        <span class="pill pill-${t.has_error ? 'error' : 'success'}">${t.has_error ? 'error' : 'ok'}</span>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--text-secondary);font-family:var(--font-mono)">
        <span>exec ${esc(t.execution_id || '—')} · ${t.span_count} spans</span>
        <span>${t.duration_ms}ms</span>
      </div>
    </button>`).join('');

  listEl.querySelectorAll('.obs-trace-row').forEach(b =>
    b.addEventListener('click', () => selectTrace(b.dataset.trace)));

  // Auto-select the first trace (or keep the current selection if still present).
  const keep = _selected && traces.some(t => t.trace_id === _selected);
  selectTrace(keep ? _selected : traces[0].trace_id);
}

async function selectTrace(traceId) {
  _selected = traceId;
  document.querySelectorAll('.obs-trace-row').forEach(r => {
    r.style.borderColor = r.dataset.trace === traceId ? 'var(--accent)' : 'var(--border-dim)';
  });
  const detail = document.getElementById('obs-detail');
  if (!detail) return;
  detail.innerHTML = '<div class="spinner"></div>';
  try {
    const d = await get(`/api/otel/traces/${encodeURIComponent(traceId)}`);
    detail.innerHTML = '';
    detail.appendChild(buildWaterfall(d.spans || []));
  } catch (e) {
    detail.innerHTML = `<div class="empty-state"><p>Failed to load trace: ${esc(e.message)}</p></div>`;
  }
}
