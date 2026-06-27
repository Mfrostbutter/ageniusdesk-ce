/**
 * Observe — per-execution OpenTelemetry traces from n8n.
 *
 * Top: a span-derived metrics strip (executions, error rate, p50/p95 latency,
 * throughput) for the active instance over a window. Left: recent traces (one per
 * execution). Right: the selected trace as a waterfall. Live-updates on new traces.
 * Accepts a workflow filter via nav opts ({workflowId, workflowName}) so Insights
 * can deep-link into one workflow's traces. Setup instructions when the receiver
 * is off.
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
let _filter = { workflowId: '', workflowName: '' };

function wfQuery() {
  return _filter.workflowId ? `&workflow_id=${encodeURIComponent(_filter.workflowId)}` : '';
}

export async function render(container) {
  const o = window.__viewOpts || {};
  _filter = { workflowId: o.workflowId || '', workflowName: o.workflowName || '' };

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

  if (!_wsBound) {
    onEvent('otel:trace', () => {
      if (window.__currentView === 'observe') { refreshList(); renderMetrics(); }
    });
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
    ${filterChip()}
    <div id="obs-metrics" style="margin-bottom:16px"></div>
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
  renderMetrics();
  await refreshList();
}

function filterChip() {
  if (!_filter.workflowId) return '';
  return `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:13px">
      <span style="color:var(--text-secondary)">Traces for</span>
      <span class="pill pill-neutral">${esc(_filter.workflowName || _filter.workflowId)}</span>
      <button class="btn btn-sm btn-ghost" style="font-size:11px;padding:2px 8px" onclick="window.__nav('observe')">Clear filter</button>
    </div>`;
}

function metricCard(label, value, sub) {
  return `
    <div style="flex:1 1 120px;min-width:110px;padding:12px 14px;border:1px solid var(--border-dim);border-radius:10px;background:var(--bg-elevated)">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.07em;color:var(--text-secondary)">${label}</div>
      <div style="font-size:22px;font-weight:700;margin-top:3px">${value}</div>
      ${sub ? `<div style="font-size:11px;color:var(--text-dim);margin-top:1px">${sub}</div>` : ''}
    </div>`;
}

async function renderMetrics() {
  const el = document.getElementById('obs-metrics');
  if (!el) return;
  let m = {};
  try { m = await get(`/api/otel/metrics?window_hours=24${wfQuery()}`); } catch { return; }
  const errPct = `${((m.error_rate || 0) * 100).toFixed(1)}%`;
  const errColor = (m.error_rate || 0) > 0 ? 'var(--error)' : 'var(--text-primary)';
  const spend = Number(m.spend_usd || 0);
  el.innerHTML = `
    <div style="display:flex;gap:12px;flex-wrap:wrap">
      ${metricCard('Executions (24h)', m.executions ?? 0, `${(m.throughput_per_hr ?? 0)}/hr`)}
      ${metricCard('Error rate', `<span style="color:${errColor}">${errPct}</span>`, `${m.errors ?? 0} failed`)}
      ${metricCard('p50 latency', `${m.p50_ms ?? 0} ms`, 'median run')}
      ${metricCard('p95 latency', `${m.p95_ms ?? 0} ms`, 'slow tail')}
      ${metricCard('Spend (24h)', `$${spend.toFixed(spend < 1 ? 4 : 2)}`, 'LLM cost (est)')}
    </div>`;
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
  try { traces = (await get(`/api/otel/traces?limit=50${wfQuery()}`)).traces || []; } catch { /* ignore */ }

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
        <span>${t.duration_ms}ms${t.cost_usd ? ` · <span style="color:var(--accent)">$${Number(t.cost_usd).toFixed(t.cost_usd < 1 ? 4 : 2)}</span>` : ''}</span>
      </div>
    </button>`).join('');

  listEl.querySelectorAll('.obs-trace-row').forEach(b =>
    b.addEventListener('click', () => selectTrace(b.dataset.trace)));

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
