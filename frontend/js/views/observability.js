/**
 * Observe — per-execution OpenTelemetry traces from n8n.
 *
 * Top: a span-derived metrics strip (executions, error rate, p50/p95 latency,
 * throughput) for the active instance over a window. Left: recent traces (one per
 * execution). Right: the selected trace as a waterfall. Live-updates on new traces.
 * Accepts a workflow filter via nav opts ({workflowId, workflowName}) so Insights
 * can deep-link into one workflow's traces. Setup instructions when the receiver
 * is off, or when it is on but the active instance has never exported a span.
 */

import { get, post, onEvent } from '../api.js';
import * as toast from '../components/toast.js';
import { buildWaterfall } from '../components/trace-waterfall.js';
import { attr } from '../lib/html.js';

function esc(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

let _wsBound = false;
let _selected = null;
let _filter = { workflowId: '', workflowName: '' };
let _container = null;

function wfQuery() {
  return _filter.workflowId ? `&workflow_id=${encodeURIComponent(_filter.workflowId)}` : '';
}

export async function render(container) {
  const o = window.__viewOpts || {};
  _filter = { workflowId: o.workflowId || '', workflowName: o.workflowName || '' };
  _container = container;

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
    // Silent-failure detected (the global toast lives in app.js); refresh the
    // list + strip so the flagged run and the count update live.
    onEvent('otel:silent', () => {
      if (window.__currentView === 'observe') { refreshList(); renderMetrics(); }
    });
    // Live backfill progress: running summary per execution, full reload on done.
    onEvent('backfill_progress', (d) => {
      if (window.__currentView === 'observe') updateBackfillProgress(d || {});
    });
    onEvent('backfill_done', () => {
      if (window.__currentView === 'observe' && _container) load(_container);
    });
    _wsBound = true;
  }
}

async function load(container) {
  let status = {};
  try { status = await get('/api/otel/status'); } catch { /* receiver may be absent */ }

  // The traces list and metrics strip are scoped to the active instance, so the
  // badge counts this instance's spans, not the fleet's. A fleet-wide number
  // here reads as "data is arriving" on an instance that never exported one.
  const mine = status.instance_span_count ?? status.span_count ?? 0;
  const fleet = status.span_count ?? 0;
  const others = Math.max(0, fleet - mine);

  const statusEl = container.querySelector('#obs-status');
  if (statusEl) {
    statusEl.innerHTML = status.enabled
      ? `<span class="pill pill-success">Receiver on</span> <span class="pill ${mine ? 'pill-neutral' : 'pill-warning'}">${mine} spans</span>`
        + (others ? ` <span class="pill pill-neutral" title="Spans held for other instances">+${others} other</span>` : '')
      : '<span class="pill pill-warning">Receiver off</span>';
  }

  const body = container.querySelector('#obs-body');
  if (!status.enabled) {
    body.innerHTML = setupHtml();
    return;
  }
  // Receiver on, but this instance has never exported a span. The list would
  // just say "run a workflow", which is a dead end when the runs are happening
  // and the instance simply is not wired to export.
  if (!mine) {
    body.innerHTML = setupHtml(status);
    bindBackfill();
    return;
  }
  body.innerHTML = `
    ${filterChip()}
    <div style="display:flex;justify-content:flex-end;margin-bottom:8px">${backfillPanelHtml(true)}</div>
    <div id="obs-metrics" style="margin-bottom:16px"></div>
    <div style="display:flex;gap:16px;align-items:stretch;flex-wrap:wrap">
      <div style="flex:1 1 340px;min-width:280px">
        <div id="obs-traces-list"><div class="spinner"></div></div>
      </div>
      <div style="flex:2 1 460px;min-width:320px">
        <div id="obs-detail" style="position:sticky;top:12px;max-height:calc(100vh - 90px);overflow:auto;padding:16px;border:1px solid var(--border-dim);border-radius:10px;background:var(--bg-elevated)">
          <div class="empty-state"><p>Select a trace to see its waterfall.</p></div>
        </div>
      </div>
    </div>`;
  bindBackfill();
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
  const silent = m.silent_failures ?? 0;
  const silentColor = silent > 0 ? 'var(--warning,#f59e0b)' : 'var(--text-primary)';
  const spend = Number(m.spend_usd || 0);
  el.innerHTML = `
    <div style="display:flex;gap:12px;flex-wrap:wrap">
      ${metricCard('Executions (24h)', m.executions ?? 0, `${(m.throughput_per_hr ?? 0)}/hr`)}
      ${metricCard('Error rate', `<span style="color:${errColor}">${errPct}</span>`, `${m.errors ?? 0} failed`)}
      ${metricCard('Silent failures', `<span style="color:${silentColor}">${silent}</span>`, 'green but broken')}
      ${metricCard('p50 latency', `${m.p50_ms ?? 0} ms`, 'median run')}
      ${metricCard('p95 latency', `${m.p95_ms ?? 0} ms`, 'slow tail')}
      ${metricCard('Spend (24h)', `$${spend.toFixed(spend < 1 ? 4 : 2)}`, 'LLM cost (est)')}
    </div>`;
}

/**
 * Setup instructions. Called two ways:
 *   setupHtml()        receiver is off  — turn it on here first.
 *   setupHtml(status)  receiver is on, but the active instance has never
 *                      exported a span — wire that instance's n8n.
 */
function setupHtml(status) {
  const wired = !!status;
  const name = wired ? (status.instance_name || status.instance_id || 'this instance') : '';
  const others = wired ? Math.max(0, (status.span_count || 0) - (status.instance_span_count || 0)) : 0;
  const endpoint = `${window.location.origin}/api/otel`;
  const header = wired
    ? `<h3>${esc(name)} is not exporting traces yet</h3>
       <p style="color:var(--text-secondary);font-size:13px">The receiver is on and accepting spans${others ? `, and it is holding ${others} span${others === 1 ? '' : 's'} from other instances` : ''}, but nothing has arrived from <strong>${esc(name)}</strong>. AgeniusDesk auto-wires the instances it provisions; an instance you connected by URL needs n8n's own OpenTelemetry export pointed here. Set these on that n8n and restart it:</p>`
    : `<h3>Turn on the trace receiver</h3>
       <p style="color:var(--text-secondary);font-size:13px">Set <code>AGD_OTEL_ENABLED=true</code> (and ideally <code>AGD_OTEL_TOKEN</code>) on AgeniusDesk, then point n8n's native OpenTelemetry exporter at this dashboard and restart n8n:</p>`;
  const target = wired ? esc(endpoint) : 'http://&lt;this-host&gt;:&lt;port&gt;/api/otel';
  return `
    <div class="empty-state" style="max-width:680px;margin:0 auto;text-align:left">
      ${header}
      <pre style="text-align:left;background:var(--bg-void,rgba(0,0,0,.25));border:1px solid var(--border-dim);border-radius:6px;padding:12px;font-size:12px;font-family:var(--font-mono);color:var(--text-secondary);overflow:auto">N8N_OTEL_ENABLED=true
N8N_OTEL_EXPORTER_OTLP_ENDPOINT=${target}
N8N_OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer &lt;AGD_OTEL_TOKEN&gt;</pre>
      ${wired
        ? `<p style="font-size:12px;color:var(--text-dim)">The n8n host must be able to reach that URL. Drop the headers line if <code>AGD_OTEL_TOKEN</code> is unset. Traces, silent-failure detection, and Spend for this instance all stay empty until it exports.</p>`
        : `<p style="font-size:12px;color:var(--text-dim)">Your cron and other production executions stream in here as they run.</p>`}
      ${wired ? `
      <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border-dim)">
        <h3 style="margin:0 0 4px">Recover missing traces</h3>
        <p style="color:var(--text-secondary);font-size:13px;margin:0 0 10px">If workflows already ran while export was broken, n8n still holds their per-node timings. Rebuild those traces from execution history.</p>
        ${backfillPanelHtml(false)}
      </div>` : ''}
    </div>`;
}

// ── Rebuild traces (backfill) ───────────────────────────────────────────────

function backfillPanelHtml(compact) {
  return `
    <div id="obs-backfill" style="${compact ? 'flex:0 1 auto;max-width:560px;text-align:right' : ''}">
      <button id="obs-bf-start" class="btn btn-sm ${compact ? 'btn-ghost' : 'btn-primary'}"
        title="Rebuild missing traces from n8n execution history">Rebuild traces</button>
      <div id="obs-bf-body" style="text-align:left"></div>
    </div>`;
}

function bindBackfill() {
  const btn = document.getElementById('obs-bf-start');
  if (btn) btn.addEventListener('click', startBackfillPreview);
}

// Preview first: counts plus the coverage warning, then an explicit confirm.
async function startBackfillPreview() {
  const body = document.getElementById('obs-bf-body');
  if (!body) return;
  body.innerHTML = '<div class="spinner"></div>';
  let p;
  try {
    p = await get('/api/otel/backfill/preview');
  } catch (e) {
    body.innerHTML = '';
    toast.error(`Preview failed: ${e.message}`);
    return;
  }
  const cov = p.coverage || {};
  let covLine = '';
  if (cov.status === 'degraded') {
    const affected = (cov.affected_workflows || []).map(w => w.name).filter(Boolean).join(', ');
    const tip = affected
      ? `Discards successful run data: ${affected}`
      : (cov.reason || 'instance default discards successful run data');
    covLine = `
      <div style="margin-top:8px;font-size:12px">
        <span class="pill pill-warning" style="font-size:9px">Coverage gap</span>
        <span style="color:var(--text-secondary)">${esc(tip)}. Those runs left no data to rebuild from.</span>
      </div>`;
  }
  const n = p.rebuildable || 0;
  body.innerHTML = `
    <div style="margin-top:10px;padding:12px 14px;border:1px solid var(--border-dim);border-radius:8px;background:var(--bg-elevated);font-size:13px">
      <div><strong>${n}</strong> execution${n === 1 ? '' : 's'} rebuildable
        <span style="color:var(--text-secondary)">of ${p.completed || 0} completed
        (${p.already_traced || 0} already traced, ${p.outside_retention || 0} outside the ${p.retention_hours}h retention window, cap ${p.cap})</span>
      </div>
      ${covLine}
      <div style="display:flex;align-items:center;gap:10px;margin-top:10px">
        <button id="obs-bf-run" class="btn btn-sm btn-primary" ${n ? '' : 'disabled'}>Rebuild ${n} trace${n === 1 ? '' : 's'}</button>
        <span id="obs-bf-progress" style="font-size:12px;color:var(--text-secondary)"></span>
      </div>
    </div>`;
  const runBtn = document.getElementById('obs-bf-run');
  if (runBtn) runBtn.addEventListener('click', () => runBackfill(p.instance_id));
}

async function runBackfill(instanceId) {
  const btn = document.getElementById('obs-bf-run');
  if (btn) btn.disabled = true;
  updateBackfillProgress(null);
  try {
    const res = await post('/api/otel/backfill/run', { instance_id: instanceId });
    const s = res.summary || {};
    toast.success(`Rebuilt ${s.backfilled || 0} trace${s.backfilled === 1 ? '' : 's'} (${s.spans || 0} spans)`);
  } catch (e) {
    if (e.status === 409) toast.error('A backfill is already running');
    else toast.error(`Backfill failed: ${e.message}`);
    if (btn) btn.disabled = false;
  }
}

function updateBackfillProgress(d) {
  const el = document.getElementById('obs-bf-progress');
  if (!el) return;
  if (!d) { el.textContent = 'Rebuilding...'; return; }
  const done = (d.backfilled || 0) + (d.skipped_traced || 0) + (d.no_data || 0) + (d.errors || 0);
  el.textContent = `Rebuilt ${d.backfilled || 0} (${d.spans || 0} spans), ${done} processed`
    + (d.errors ? `, ${d.errors} failed` : '');
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
    <button class="obs-trace-row" data-trace="${attr(t.trace_id)}"
      style="display:block;width:100%;text-align:left;border:1px solid ${t.trace_id === _selected ? 'var(--accent)' : 'var(--border-dim)'};border-radius:8px;background:var(--bg-elevated);padding:10px 12px;margin-bottom:8px;cursor:pointer">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
        <span style="font-size:13px;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(t.workflow_name || '(unknown workflow)')}</span>
        ${t.has_error
          ? '<span class="pill pill-error">error</span>'
          : t.has_silent
            ? '<span class="pill pill-warning" title="Ran green but a node failed or dropped its output">silent</span>'
            : '<span class="pill pill-success">ok</span>'}
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
  // Bring the detail into view when a row low in a long list is clicked (the
  // sticky panel may be scrolled out of view above the click point).
  detail.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  try {
    const d = await get(`/api/otel/traces/${encodeURIComponent(traceId)}`);
    detail.innerHTML = '';
    detail.appendChild(buildWaterfall(d.spans || []));
  } catch (e) {
    detail.innerHTML = `<div class="empty-state"><p>Failed to load trace: ${esc(e.message)}</p></div>`;
  }
}
