/**
 * Insights view — execution analytics, success rates, top workflows,
 * error trends. Pulls from /api/insights (5-min server-side cache).
 *
 * Slim v1: 4 summary tiles, two top-5 lists, per-bucket count strip.
 * No chart library — keeps the page deps clean.
 */

import { get, post } from '../api.js';

const RANGE_KEY = 'ageniusdesk:insights_range';
const VALID = new Set(['24h', '7d', '30d']);

let _currentRange = '24h';

function initialRange() {
  try {
    const v = sessionStorage.getItem(RANGE_KEY);
    if (v && VALID.has(v)) return v;
  } catch { /* ignore */ }
  return '24h';
}

function persistRange(r) {
  try { sessionStorage.setItem(RANGE_KEY, r); } catch { /* ignore */ }
}

function fmtPct(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return `${(n * 100).toFixed(1)}%`;
}

function fmtMs(ms) {
  if (!ms) return '—';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

function fmtNum(n) {
  if (n === null || n === undefined) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function rateColor(r) {
  if (r >= 0.95) return '#10b981';
  if (r >= 0.80) return '#f59e0b';
  return '#ef4444';
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderTile(label, value, sub, color) {
  const colorStyle = color ? `color:${color}` : '';
  return `
    <div class="card" style="padding:16px">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-secondary)">${label}</div>
      <div style="font-size:28px;font-weight:700;margin-top:4px;${colorStyle}">${value}</div>
      ${sub ? `<div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${sub}</div>` : ''}
    </div>
  `;
}

function renderTimeseries(points, bucket) {
  if (!points.length) {
    return `<div style="color:var(--text-secondary);font-size:12px;padding:12px">No executions in this range.</div>`;
  }
  const max = Math.max(1, ...points.map(p => p.total));
  const w = bucket === 'hour' ? 16 : 24;
  const bars = points.map(p => {
    const sH = Math.round((p.success / max) * 90);
    const eH = Math.round((p.error / max) * 90);
    const rH = Math.round((p.running / max) * 90);
    const tip = `${p.ts}\n${p.success} ok · ${p.error} err · ${p.running} running`;
    return `
      <div title="${escapeHtml(tip)}" style="display:flex;flex-direction:column-reverse;justify-content:flex-start;align-items:center;width:${w}px;height:100px;gap:1px">
        ${rH > 0 ? `<div style="width:${w - 4}px;height:${rH}px;background:#f59e0b;border-radius:2px"></div>` : ''}
        ${eH > 0 ? `<div style="width:${w - 4}px;height:${eH}px;background:#ef4444;border-radius:2px"></div>` : ''}
        ${sH > 0 ? `<div style="width:${w - 4}px;height:${sH}px;background:#10b981;border-radius:2px"></div>` : ''}
      </div>
    `;
  }).join('');
  return `
    <div style="overflow-x:auto;padding:8px 4px">
      <div style="display:flex;align-items:flex-end;gap:2px;height:110px">${bars}</div>
    </div>
    <div style="display:flex;gap:14px;font-size:11px;color:var(--text-secondary);padding:0 8px 6px">
      <span><span style="display:inline-block;width:10px;height:10px;background:#10b981;border-radius:2px;margin-right:4px;vertical-align:middle"></span>success</span>
      <span><span style="display:inline-block;width:10px;height:10px;background:#ef4444;border-radius:2px;margin-right:4px;vertical-align:middle"></span>error</span>
      <span><span style="display:inline-block;width:10px;height:10px;background:#f59e0b;border-radius:2px;margin-right:4px;vertical-align:middle"></span>running</span>
    </div>
  `;
}

function renderTopList(rows, columns) {
  if (!rows.length) {
    return `<div style="color:var(--text-secondary);font-size:12px;padding:12px">Nothing to show.</div>`;
  }
  const head = columns.map(c => `<th style="text-align:${c.align || 'left'};padding:6px 8px;font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-secondary);border-bottom:1px solid var(--border)">${c.label}</th>`).join('');
  const body = rows.map(r => {
    const cells = columns.map(c => {
      const v = c.cell ? c.cell(r) : escapeHtml(r[c.key] ?? '');
      return `<td style="padding:8px;text-align:${c.align || 'left'};font-size:13px">${v}</td>`;
    }).join('');
    return `<tr>${cells}</tr>`;
  }).join('');
  return `<table style="width:100%;border-collapse:collapse"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function loadAndRender() {
  const root = document.getElementById('insights-root');
  if (!root) return;
  root.innerHTML = `<div style="padding:30px;text-align:center;color:var(--text-secondary)">Loading insights...</div>`;
  let payload;
  try {
    payload = await get(`/api/insights?range=${encodeURIComponent(_currentRange)}`);
  } catch (e) {
    root.innerHTML = `<div class="card" style="padding:16px;color:#ef4444">Failed to load: ${escapeHtml(e.message)}</div>`;
    return;
  }
  const s = payload.summary || {};
  const sr = s.success_rate ?? 0;
  const tiles = [
    renderTile('Executions', fmtNum(s.total_executions), `${s.success || 0} ok · ${s.error || 0} err · ${s.running || 0} running`),
    renderTile('Success rate', fmtPct(sr), `${s.success || 0} of ${s.total_executions || 0}`, rateColor(sr)),
    renderTile('Errors', fmtNum(s.error || 0), `${s.local_errors || 0} in local log`, s.error ? '#ef4444' : null),
    renderTile('Avg duration', fmtMs(s.avg_duration_ms), 'measured per execution'),
  ].join('');

  const ts = payload.timeseries?.points || [];
  const bucket = payload.bucket || 'hour';

  const byVolume = renderTopList(payload.top_by_volume || [], [
    { key: 'workflow_name', label: 'Workflow', cell: r => `<a href="#" onclick="event.preventDefault();window.__nav('workflows',{selectId:'${escapeHtml(r.workflow_id)}'})">${escapeHtml(r.workflow_name)}</a>` },
    { key: 'count', label: 'Runs', align: 'right', cell: r => fmtNum(r.count) },
    { key: 'success_rate', label: 'OK', align: 'right', cell: r => `<span style="color:${rateColor(r.success_rate)}">${fmtPct(r.success_rate)}</span>` },
  ]);

  const byErrors = renderTopList(payload.top_by_errors || [], [
    { key: 'workflow_name', label: 'Workflow', cell: r => `<a href="#" onclick="event.preventDefault();window.__nav('workflows',{selectId:'${escapeHtml(r.workflow_id)}'})">${escapeHtml(r.workflow_name)}</a>` },
    { key: 'errors', label: 'Errors', align: 'right', cell: r => `<span style="color:#ef4444;font-weight:600">${fmtNum(r.errors)}</span>` },
    { key: 'count', label: 'of', align: 'right', cell: r => fmtNum(r.count) },
  ]);

  const localTop = renderTopList(payload.top_local_errors || [], [
    { key: 'workflow_name', label: 'Workflow', cell: r => `<a href="#" onclick="event.preventDefault();window.__nav('workflows',{selectId:'${escapeHtml(r.workflow_id)}'})">${escapeHtml(r.workflow_name)}</a>` },
    { key: 'errors', label: 'Errors', align: 'right', cell: r => `<span style="color:#ef4444;font-weight:600">${fmtNum(r.errors)}</span>` },
    { key: 'last_occurred', label: 'Last seen', cell: r => escapeHtml(r.last_occurred || '—') },
  ]);

  const cacheLabel = payload._cached
    ? `<span style="font-size:11px;color:var(--text-secondary)">cached ${payload._cache_age_s}s ago</span>`
    : `<span style="font-size:11px;color:var(--text-secondary)">fresh</span>`;

  const win = payload.window || {};
  const truncated = (win.executions_scanned || 0) >= (win.max_pages || 0) * (win.page_size || 250);
  const truncWarn = truncated
    ? `<div style="background:var(--bg-input);border:1px dashed var(--border);border-radius:var(--radius);padding:8px 12px;font-size:12px;color:var(--text-secondary);margin-bottom:14px">⚠ Hit pagination cap (${win.max_pages} pages × ${win.page_size}). Older runs in this window may be missing.</div>`
    : '';

  root.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:12px;margin-bottom:18px">${tiles}</div>
    ${truncWarn}
    <div class="card" style="margin-bottom:18px">
      <div class="card-header" style="display:flex;justify-content:space-between;align-items:center">
        <span class="card-title">Timeline · ${escapeHtml(bucket === 'hour' ? 'hourly' : 'daily')}</span>
        ${cacheLabel}
      </div>
      ${renderTimeseries(ts, bucket)}
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px">
      <div class="card">
        <div class="card-header"><span class="card-title">Top by volume</span></div>
        <div style="padding:6px">${byVolume}</div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Top by execution errors</span></div>
        <div style="padding:6px">${byErrors}</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Top by local error log</span></div>
      <div style="padding:6px">${localTop}</div>
    </div>
  `;
}

async function refreshNow() {
  try {
    await post(`/api/insights/refresh?range=${encodeURIComponent(_currentRange)}`, {});
  } catch { /* ignore */ }
  await loadAndRender();
}

export async function render(container) {
  _currentRange = initialRange();
  container.innerHTML = `
    <div class="view-header" style="margin-bottom:20px;display:flex;justify-content:space-between;align-items:center;gap:14px">
      <div>
        <h1 style="margin:0">Insights</h1>
        <div class="card-subtitle">Execution analytics for the active n8n instance.</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="insights-range" style="background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius);padding:6px 10px;color:var(--text-primary)">
          <option value="24h">Last 24 hours</option>
          <option value="7d">Last 7 days</option>
          <option value="30d">Last 30 days</option>
        </select>
        <button class="btn btn-sm btn-ghost" id="insights-refresh">Refresh</button>
      </div>
    </div>
    <div id="insights-root"></div>
  `;
  const sel = container.querySelector('#insights-range');
  if (sel) {
    sel.value = _currentRange;
    sel.addEventListener('change', () => {
      _currentRange = sel.value;
      persistRange(_currentRange);
      loadAndRender();
    });
  }
  const refreshBtn = container.querySelector('#insights-refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', refreshNow);
  await loadAndRender();
}

export function teardown() {}
