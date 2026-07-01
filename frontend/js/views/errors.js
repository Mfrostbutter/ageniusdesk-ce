/**
 * Errors view — grouped-by-default feed with instance badges.
 *
 * Default: groups by (workflow, node, error_type) so 80 identical OAuth
 * failures collapse into one row with a ×count. Toggle to Flat to see
 * every occurrence. Both modes show the originating n8n instance as a
 * colored chip so the user knows which stack produced each error.
 */

import { get, post, del, onEvent } from '../api.js';
import { openTraceModal } from '../components/trace-waterfall.js';
import { renderErrorItem } from '../components/error-item.js';
import { getErrorLookback, setErrorLookback, lookbackOptionsHtml } from '../error-prefs.js';

let unsub = null;

// Open the OpenTelemetry trace for an error's execution (degrades to a "no trace
// captured" message when the receiver is off or the run predates it).
window.__observeError = (btn) => {
  const execId = btn.dataset.exec;
  if (!execId) return;
  openTraceModal({ execId, title: `Trace · ${btn.dataset.wf || 'workflow'} · exec ${execId}` });
};

const VIEW_STORAGE_KEY = 'ageniusdesk:errors_view';
const VALID_VIEWS = new Set(['grouped', 'flat']);

let _instanceMap = {};

// The lookback range is the shared, persistent "error reporting window" setting
// (see error-prefs.js), so this view, the Overview, and Settings agree on the span.
function getInitialRange() {
  return getErrorLookback();
}

function getInitialView() {
  try {
    const stored = sessionStorage.getItem(VIEW_STORAGE_KEY);
    if (stored && VALID_VIEWS.has(stored)) return stored;
  } catch { /* ignore */ }
  return 'grouped';
}

function rangeLabel(range) {
  if (range === '7d') return 'last 7 days';
  if (range === '30d') return 'last 30 days';
  if (range === '90d') return 'last 90 days';
  if (range === 'all') return 'all time';
  return 'last 24h';
}

async function loadInstanceMap() {
  try {
    const d = await get('/api/n8n/instances');
    _instanceMap = Object.fromEntries(
      (d.instances || []).map(i => [i.id, { name: i.name || i.id, color: i.color || '#888', n8nUrl: i.login_url || i.url || '' }])
    );
  } catch {
    _instanceMap = {};
  }
}

function instanceBadge(id) {
  const inst = _instanceMap[id];
  const name = inst ? inst.name : (id ? 'unknown' : 'no instance');
  const color = inst && inst.color ? inst.color : '#888';
  return `<span class="instance-badge" title="${esc(id)}" style="display:inline-flex;align-items:center;gap:4px;font-size:10px;padding:2px 6px;border-radius:var(--radius);background:var(--bg-input);color:var(--text-secondary);font-family:var(--font-mono)">`
    + `<span style="width:6px;height:6px;border-radius:50%;background:${attr(color)}"></span>`
    + `${esc(name)}</span>`;
}

export async function render(container) {
  const initialRange = getInitialRange();
  const initialView = getInitialView();

  container.innerHTML = `
    <div class="section-header">
      <div>
        <h2 class="section-title">Workflow Errors</h2>
        <span style="font-size:12px;color:var(--text-secondary)" id="error-count-label">Loading...</span>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <div class="toggle-group" style="display:inline-flex;border:1px solid var(--border-dim);border-radius:var(--radius);overflow:hidden">
          <button class="btn btn-sm btn-ghost errors-view-btn" data-view="grouped" style="border-radius:0;${initialView==='grouped' ? 'background:var(--bg-input);color:var(--text-primary)' : ''}">Grouped</button>
          <button class="btn btn-sm btn-ghost errors-view-btn" data-view="flat" style="border-radius:0;${initialView==='flat' ? 'background:var(--bg-input);color:var(--text-primary)' : ''}">Flat</button>
        </div>
        <label style="display:flex;align-items:center;gap:6px;margin:0;font-size:12px;color:var(--text-secondary)">
          <span>Range</span>
          <select id="errors-range-select" style="width:auto;margin-top:0;padding:6px 10px;font-size:12px">
            ${lookbackOptionsHtml(initialRange)}
          </select>
        </label>
        <button class="btn btn-sm btn-danger" id="clear-errors-btn">Clear All</button>
      </div>
    </div>
    <div class="card">
      <div id="error-feed"><div class="spinner"></div></div>
    </div>
  `;

  await loadInstanceMap();

  document.getElementById('clear-errors-btn').addEventListener('click', clearErrors);

  document.querySelectorAll('.errors-view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const v = btn.dataset.view;
      if (!VALID_VIEWS.has(v)) return;
      try { sessionStorage.setItem(VIEW_STORAGE_KEY, v); } catch { /* ignore */ }
      document.querySelectorAll('.errors-view-btn').forEach(b => {
        const active = b.dataset.view === v;
        b.style.background = active ? 'var(--bg-input)' : '';
        b.style.color = active ? 'var(--text-primary)' : '';
      });
      loadErrors();
    });
  });

  const rangeSelect = document.getElementById('errors-range-select');
  rangeSelect.addEventListener('change', () => {
    setErrorLookback(rangeSelect.value);
    loadErrors();
  });

  if (unsub) unsub();
  unsub = onEvent('error', (data) => {
    // Live event: only surface when current view scope matches the incoming
    // event's instance_id. In grouped mode we reload to keep counts honest.
    if (getCurrentView() === 'grouped') {
      loadErrors();
    } else {
      prependError(data);
    }
    updateBadge();
  });

  loadErrors();
  post('/api/errors/sync').then(res => { if (res.synced > 0) loadErrors(); }).catch(() => {});
}

function getCurrentRange() {
  const select = document.getElementById('errors-range-select');
  return select ? select.value : getInitialRange();
}

function getCurrentView() {
  const btn = document.querySelector('.errors-view-btn[data-view][style*="var(--bg-input)"]');
  if (btn && VALID_VIEWS.has(btn.dataset.view)) return btn.dataset.view;
  return getInitialView();
}

async function loadErrors() {
  const el = document.getElementById('error-feed');
  const range = getCurrentRange();
  const view = getCurrentView();
  try {
    if (view === 'grouped') {
      const data = await get(`/api/errors/grouped?limit=100&range=${encodeURIComponent(range)}`);
      const groups = data.groups || [];
      const rangeCount = (data.count_range !== undefined) ? data.count_range : (data.count_24h || 0);
      const label = document.getElementById('error-count-label');
      if (label) label.textContent = `${rangeCount} in ${rangeLabel(range)} · ${groups.length} group${groups.length === 1 ? '' : 's'}`;
      updateBadge(data.count_24h);

      if (!groups.length) {
        el.innerHTML = '<div class="empty-state"><h3>No errors</h3><p>Errors on the active instance will appear here in real-time</p></div>';
        return;
      }
      el.innerHTML = groups.map(renderGroupItem).join('');
    } else {
      const data = await get(`/api/errors?limit=100&range=${encodeURIComponent(range)}`);
      const errors = data.errors || [];
      const rangeCount = (data.count_range !== undefined) ? data.count_range : (data.count_24h || 0);
      const label = document.getElementById('error-count-label');
      if (label) label.textContent = `${rangeCount} in ${rangeLabel(range)} · ${errors.length} row${errors.length === 1 ? '' : 's'}`;
      updateBadge(data.count_24h);

      if (!errors.length) {
        el.innerHTML = '<div class="empty-state"><h3>No errors</h3><p>Errors on the active instance will appear here in real-time</p></div>';
        return;
      }
      el.innerHTML = errors.map(renderErrorItem).join('');
    }
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Failed to load errors: ${esc(e.message)}</p></div>`;
  }
}

function prependError(error) {
  const feed = document.getElementById('error-feed');
  if (!feed) return;
  const empty = feed.querySelector('.empty-state');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.innerHTML = renderErrorItem(error, { instanceMap: _instanceMap });
  feed.prepend(div.firstElementChild);
}

function renderGroupItem(g) {
  const n8nBase = (window.__n8nUrl || '').replace(/\/$/, '');
  const n8nExecUrl = g.last_execution_id && g.workflow_id && n8nBase
    ? `${n8nBase}/workflow/${encodeURIComponent(g.workflow_id)}/executions/${encodeURIComponent(g.last_execution_id)}`
    : '';
  return `
    <div class="error-item" onclick="this.classList.toggle('expanded')">
      <div class="error-item-header" style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="error-item-workflow" style="display:flex;align-items:center;gap:8px;flex:1;min-width:0">
          ${instanceBadge(g.instance_id)}
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(g.workflow_name)}</span>
        </span>
        <span style="display:flex;gap:8px;align-items:center;flex-shrink:0">
          <span class="error-count-pill" style="background:var(--bg-input);padding:2px 8px;border-radius:var(--radius);font-size:11px;color:var(--text-primary);font-weight:600">×${g.count}</span>
          <span class="error-item-time">${formatTime(g.last_occurred)}</span>
        </span>
      </div>
      <div class="error-item-message"><strong>${esc(g.node_name || 'N/A')}</strong> · ${esc(g.error_type)} · ${esc(g.last_error_message || '')}</div>
      <div class="error-item-detail">
        <div><strong>Workflow ID:</strong> <code style="display:inline;padding:2px 6px;font-size:11px">${esc(g.workflow_id)}</code></div>
        <div><strong>Node:</strong> ${esc(g.node_name || 'N/A')}</div>
        <div><strong>Type:</strong> ${esc(g.error_type)}</div>
        <div><strong>First seen:</strong> ${formatTime(g.first_occurred)}</div>
        <div><strong>Last seen:</strong> ${formatTime(g.last_occurred)}</div>
        ${g.last_execution_id ? `<div><strong>Last execution:</strong> <code style="display:inline;padding:2px 6px;font-size:11px">${esc(g.last_execution_id)}</code></div>` : ''}
        <code>${esc(g.last_error_message || '')}</code>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap" onclick="event.stopPropagation()">
          <button class="btn btn-sm btn-ghost err-ai-btn" data-wf="${attr(g.workflow_name)}" data-node="${attr(g.node_name || '')}" data-type="${attr(g.error_type)}" data-msg="${attr(g.last_error_message || '')}" data-exec="${attr(g.last_execution_id || '')}" data-wfid="${attr(g.workflow_id)}" onclick="event.stopPropagation();window.__askErrorAI(this)" title="Ask AI to analyze this error">&#10022; Ask AI</button>
          ${g.last_execution_id ? `<button class="btn btn-sm btn-ghost" data-wf="${attr(g.workflow_name)}" data-exec="${attr(g.last_execution_id)}" onclick="event.stopPropagation();window.__observeError(this)" title="View this execution's OpenTelemetry trace">&#128202; Trace</button>` : ''}
          <button class="btn btn-sm btn-primary" onclick="window.__nav('workflows',{selectId:'${jsStr(g.workflow_id)}'})">View Workflow</button>
          ${n8nExecUrl ? `<a class="btn btn-sm btn-ghost" href="${n8nExecUrl}" target="_blank" rel="noopener">Open Last in n8n</a>` : ''}
          <button class="btn btn-sm btn-danger" onclick="window.__clearGroup('${jsStr(g.workflow_id)}','${jsStr(g.node_name || '')}','${jsStr(g.error_type)}', ${g.count}, this)">Clear Group (×${g.count})</button>
        </div>
        <div class="err-ai-result" style="display:none;margin-top:10px;padding:10px 12px;background:var(--bg-void);border:1px solid var(--border-dim);border-radius:var(--radius);font-size:12px;line-height:1.6" onclick="event.stopPropagation()"></div>
      </div>
    </div>
  `;
}

// renderErrorItem moved to components/error-item.js (shared across Overview,
// Errors/Executions, and Fleet Health so an error looks + acts identically
// everywhere). Behaviors (__askErrorAI / __observeError / __deleteExecution /
// __clearWorkflowErrors) stay defined in this module, registered app-wide.

// Ask AI about an error — same workflow agent the Workflows page uses. Builds a
// prompt from the error context and renders the analysis inline under the detail.
window.__askErrorAI = async function (btn) {
  const item = btn.closest('.error-item');
  const resultEl = item && item.querySelector('.err-ai-result');
  if (!resultEl) return;

  // Toggle off if already open.
  if (resultEl.style.display !== 'none') { resultEl.style.display = 'none'; return; }
  resultEl.style.display = '';
  resultEl.innerHTML = `<span style="color:var(--text-dim)">✦ Asking AI...</span>`;

  const d = btn.dataset;
  const lines = [`I have an n8n workflow called "${d.wf || 'this workflow'}" that is throwing an error.`];
  if (d.node) lines.push(`Failing node: ${d.node}`);
  if (d.type) lines.push(`Error type: ${d.type}`);
  if (d.exec) lines.push(`Execution: ${d.exec}`);
  lines.push(`\nError message:\n${d.msg || 'Unknown error'}`);
  lines.push(`\nPlease analyze what went wrong and suggest specific fixes.`);
  const prompt = lines.join('\n');

  try {
    const resp = await post('/api/assistant/chat', {
      messages: [{ role: 'user', content: prompt }],
      context: '',
      surface: 'triage',
    });
    // esc() FIRST so a prompt-injected LLM reply containing markup (e.g.
    // <img src=x onerror=...>) is neutralized before the markdown regexes run.
    const md = esc(resp.response || 'No response')
      .replace(/\n/g, '<br>')
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code style="background:var(--bg-input);padding:1px 5px;border-radius:3px;font-size:11px">$1</code>');
    resultEl.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:11px;font-weight:600;color:var(--accent)">✦ AI Analysis</span>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="navigator.clipboard.writeText(${JSON.stringify(resp.response || '').replace(/'/g, '&#39;')}).then(()=>{this.textContent='Copied!'})">Copy</button>
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="this.closest('.err-ai-result').style.display='none'">✕</button>
        </div>
      </div>
      <div style="color:var(--text-secondary)">${md}</div>
    `;
  } catch (e) {
    resultEl.innerHTML = `<span style="color:var(--error)">${esc(e.message)}</span>`;
  }
};

window.__deleteExecution = async (executionId, btn) => {
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Deleting...';
  try {
    await del(`/api/errors/${encodeURIComponent(executionId)}`);
    btn.closest('.error-item')?.remove();
  } catch (e) {
    alert('Error: ' + e.message);
    btn.disabled = false;
    btn.textContent = orig;
  }
};

window.__clearWorkflowErrors = async (workflowId, btn) => {
  if (!confirm('Delete all executions for this workflow from n8n and clear errors here?')) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Clearing...';
  try {
    const res = await del(`/api/errors?workflow_id=${encodeURIComponent(workflowId)}&purge_n8n=true`);
    const n8nOk = res.n8n?.success !== false;
    loadErrors();
    if (!n8nOk) alert('Local errors cleared but n8n purge failed: ' + (res.n8n?.error || 'unknown'));
  } catch (e) {
    alert('Error: ' + e.message);
    btn.disabled = false;
    btn.textContent = orig;
  }
};

window.__clearGroup = async (workflowId, nodeName, errorType, count, btn) => {
  if (!confirm(`Delete ${count} identical error${count === 1 ? '' : 's'} (node: ${nodeName || 'n/a'}, type: ${errorType})?\n\nOK = delete local rows only.\nCancel to also purge the executions from n8n, hit OK with Shift held.`)) return;
  const purgeN8n = window.event && window.event.shiftKey;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Clearing...';
  try {
    const res = await post('/api/errors/clear-group', {
      workflow_id: workflowId,
      node_name: nodeName,
      error_type: errorType,
      purge_n8n: purgeN8n,
    });
    loadErrors();
    if (purgeN8n) {
      const failed = (res.purged || []).filter(p => !p.success).length;
      if (failed) alert(`Deleted ${res.deleted} local rows. n8n purge: ${failed} failures.`);
    }
  } catch (e) {
    alert('Error: ' + e.message);
    btn.disabled = false;
    btn.textContent = orig;
  }
};

async function clearErrors() {
  if (!confirm('Clear all errors for the active instance?')) return;
  try {
    await del('/api/errors');
    loadErrors();
  } catch { /* ignore */ }
}

function updateBadge(count) {
  const badge = document.getElementById('error-badge');
  if (!badge) return;
  if (count === undefined) {
    get('/api/errors?limit=1').then(d => {
      badge.textContent = d.count_24h || 0;
      badge.classList.toggle('hidden', !d.count_24h);
    });
  } else {
    badge.textContent = count;
    badge.classList.toggle('hidden', !count);
  }
}

function formatTime(iso) {
  if (!iso) return 'just now';
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }

function attr(s) {
  // Escape for an HTML double-quoted attribute value (data-* on the Ask AI button).
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}
