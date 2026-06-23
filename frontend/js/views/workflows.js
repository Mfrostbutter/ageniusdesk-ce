/**
 * Workflows view — list all workflows with execution history.
 */

import { get, post } from '../api.js';
import * as toast from '../components/toast.js';
import * as modal from '../components/modal.js';
import { WorkflowDetailPanel } from '../components/workflow-detail-panel.js';

let selectedWorkflow = null;
let selectedWorkflowMeta = { id: '', name: '' };
let lastVisibleArchivedCount = 0;
let _pendingSelectId = null;

export async function render(container) {
  // Resolve initial Active Only toggle state from navigation opts:
  //   opts.filter === 'all'    → toggle OFF (Dashboard "Workflows" card)
  //   opts.filter === 'active' → toggle ON  (Dashboard "Active" card)
  //   undefined                → toggle ON  (sidebar nav / direct navigation)
  // Consume once, then clear so back-navigation doesn't reuse stale state.
  const navOpts = window.__viewOpts || null;
  window.__viewOpts = null;
  _pendingSelectId = navOpts?.selectId || null;
  let activeOnlyDefault = true;
  if (_pendingSelectId) activeOnlyDefault = false;  // show all so target workflow is always visible
  else if (navOpts && navOpts.filter === 'all') activeOnlyDefault = false;
  else if (navOpts && navOpts.filter === 'active') activeOnlyDefault = true;

  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">Workflows</h2>
      <div style="display:flex;gap:12px;align-items:center">
        <button class="btn btn-sm btn-ghost" id="wf-delete-archived-btn" style="display:none;color:var(--error);border-color:var(--error)" title="Hard-delete every workflow that is archived in n8n. Type-to-confirm.">Delete archived</button>
        <button class="btn btn-sm btn-primary" id="wf-import-btn" title="Upload a workflow JSON file to the active instance">Import Workflow</button>
        <input type="file" id="wf-import-file" accept=".json" multiple style="display:none">
        <input type="text" id="wf-search" placeholder="Search workflows..." style="width:200px;padding:6px 10px;font-size:13px">
        <label class="toggle-label" style="display:flex;align-items:center;gap:8px;margin:0;font-size:12px;cursor:pointer;user-select:none">
          <span style="color:var(--text-secondary)">Active only</span>
          <div class="toggle-switch">
            <input type="checkbox" id="wf-active-only"${activeOnlyDefault ? ' checked' : ''}>
            <span class="toggle-slider"></span>
          </div>
        </label>
      </div>
    </div>
    <div class="grid-2" style="height:calc(100vh - 120px);align-items:stretch">
      <div class="card" id="wf-list-card" style="overflow-y:auto;min-height:0">
        <div id="wf-list"><div class="spinner"></div></div>
      </div>
      <div class="card" id="wf-detail-card" style="overflow-y:auto;min-height:0">
        <div id="wf-detail">
          <div class="empty-state"><h3>Select a workflow</h3><p>Click a workflow to see details and execution history</p></div>
        </div>
      </div>
    </div>
  `;

  document.getElementById('wf-search').addEventListener('input', debounce(loadWorkflows, 300));
  document.getElementById('wf-active-only').addEventListener('change', loadWorkflows);

  const importBtn = document.getElementById('wf-import-btn');
  const importFile = document.getElementById('wf-import-file');
  importBtn.addEventListener('click', () => importFile.click());
  importFile.addEventListener('change', async (e) => {
    const files = [...e.target.files];
    importFile.value = '';
    if (!files.length) return;
    await handleQuickImport(files);
  });

  document.getElementById('wf-delete-archived-btn').addEventListener('click', deleteAllArchived);

  loadWorkflows();
}

async function handleQuickImport(files) {
  let ok = 0, fail = 0;
  for (const file of files) {
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      const result = await post('/api/n8n/import', data);
      if (result.success) { ok++; } else { fail++; toast.error(`${file.name}: ${result.error || 'Import failed'}`); }
    } catch (err) {
      fail++;
      toast.error(`${file.name}: ${err.message}`);
    }
  }
  if (ok) toast.success(`Imported ${ok} workflow${ok === 1 ? '' : 's'}${fail ? ` — ${fail} failed` : ''}`);
  loadWorkflows();
}

async function loadWorkflows() {
  const search = document.getElementById('wf-search')?.value || '';
  const activeOnly = document.getElementById('wf-active-only')?.checked || false;
  const el = document.getElementById('wf-list');
  el.innerHTML = '<div class="spinner"></div>';

  try {
    const data = await get(`/api/n8n/workflows?name_contains=${encodeURIComponent(search)}&active_only=${activeOnly}&limit=250`);
    const workflows = data.workflows || [];

    if (!workflows.length) {
      el.innerHTML = '<div class="empty-state"><p>No workflows found</p></div>';
      return;
    }

    // Sort: active first, then alphabetical
    workflows.sort((a, b) => {
      if (a.active !== b.active) return a.active ? -1 : 1;
      return (a.name || '').localeCompare(b.name || '');
    });

    el.innerHTML = workflows.map(w => `
      <div class="workflow-row clickable" data-id="${w.id}" onclick="window.__selectWorkflow('${jsStr(w.id)}')" style="${w.is_archived ? 'opacity:0.55' : ''}">
        <span class="status-dot ${w.active ? 'online' : 'offline'}" title="${w.active ? 'Active' : 'Inactive'}"></span>
        <span class="workflow-name">${esc(w.name)}</span>
        ${w.is_archived ? '<span class="pill pill-warning" style="font-size:10px" title="Archived in n8n">ARCHIVED</span>' : ''}
        <span class="pill pill-${triggerClass(w.trigger_type)}">${w.trigger_type}</span>
      </div>
    `).join('');

    // Auto-select a workflow if navigated here with a selectId opt
    if (_pendingSelectId) {
      const target = workflows.find(w => w.id === _pendingSelectId);
      _pendingSelectId = null;
      if (target) window.__selectWorkflow(target.id);
    }

    lastVisibleArchivedCount = workflows.filter(w => w.is_archived).length;
    const btn = document.getElementById('wf-delete-archived-btn');
    if (btn) {
      if (lastVisibleArchivedCount > 0) {
        btn.textContent = `Delete archived (${lastVisibleArchivedCount})`;
        btn.style.display = '';
      } else {
        btn.style.display = 'none';
      }
    }
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Failed to load workflows: ${esc(e.message)}</p></div>`;
  }
}

async function deleteAllArchived() {
  // Use the cached count (from the loaded workflow list) — the rendered DOM
  // can't be filtered cleanly because pill-warning is shared with the
  // schedule-trigger pill style.
  const visibleArchived = lastVisibleArchivedCount;
  const ok = await modal.confirmDelete({
    title: 'Delete archived workflows',
    bodyHtml: `
      <p style="margin:0 0 10px 0">Permanently delete every archived workflow on this instance.</p>
      <p style="margin:0 0 10px 0"><strong>Visible archived:</strong> ${visibleArchived} <span style="color:var(--text-dim)">(the server scans the full list and may find more)</span></p>
      <p style="margin:0;color:var(--error)">This cannot be undone. Execution history will be lost.</p>
    `,
    confirmLabel: 'Delete all archived',
  });
  if (!ok) return;
  const btn = document.getElementById('wf-delete-archived-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting...'; }
  try {
    const result = await fetch('/api/n8n/workflows/archived', { method: 'DELETE' }).then(r => r.json());
    if (!result.success) {
      toast.error(result.error || 'Bulk delete failed');
      return;
    }
    if (result.deleted === 0 && result.failed === 0) {
      toast.success('No archived workflows to delete');
    } else if (result.failed === 0) {
      toast.success(`Deleted ${result.deleted} archived workflow${result.deleted === 1 ? '' : 's'}`);
    } else {
      toast.error(`Deleted ${result.deleted}, failed ${result.failed}. First failure: ${result.errors?.[0]?.error || 'unknown'}`);
    }
    // If the currently-selected workflow was archived and just got deleted, clear the detail panel.
    if (selectedWorkflow && (result.items || []).some(w => w.id === selectedWorkflow)) {
      selectedWorkflow = null;
      selectedWorkflowMeta = { id: '', name: '' };
      const detailEl = document.getElementById('wf-detail');
      if (detailEl) {
        detailEl.innerHTML = '<div class="empty-state"><h3>Select a workflow</h3><p>Click a workflow to see details and execution history</p></div>';
      }
    }
    loadWorkflows();
  } catch (e) {
    toast.error(e.message);
  } finally {
    if (btn) { btn.disabled = false; }
  }
}

window.__selectWorkflow = async function(id) {
  selectedWorkflow = id;
  const el = document.getElementById('wf-detail');
  el.innerHTML = '<div class="spinner"></div>';

  // Highlight selected row
  document.querySelectorAll('.workflow-row').forEach(r => r.style.background = '');
  document.querySelector(`.workflow-row[data-id="${id}"]`)?.style.setProperty('background', 'var(--bg-hover)');

  try {
    const [wf, execData] = await Promise.all([
      get(`/api/n8n/workflows/${id}`),
      get(`/api/n8n/executions?workflow_id=${id}&limit=15`),
    ]);

    const executions = execData.executions || [];
    selectedWorkflowMeta = { id: wf.id, name: wf.name };

    el.innerHTML = '';
    el.appendChild(WorkflowDetailPanel(wf, executions, {
      onActivate: (wfId, active) => window.__toggleWorkflow(wfId, active),
      onInject: (wfId) => window.__injectDashboardTrigger(wfId),
      onRemove: (wfId) => window.__removeDashboardTrigger(wfId),
      onDelete: (wfId) => window.__deleteWorkflow(wfId),
      onAnalyze: (execId, wfName, wfId) => window.__analyzeExec(execId, wfName, wfId),
    }));
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Failed to load: ${esc(e.message)}</p></div>`;
  }
};

window.__analyzeExec = async function(execId, workflowName, workflowId) {
  const resultEl = document.getElementById(`ai-result-${execId}`);
  const rowEl = document.getElementById(`ai-row-${execId}`);
  if (!resultEl || !rowEl) return;

  // Toggle off if already open
  if (rowEl.style.display !== 'none') { rowEl.style.display = 'none'; return; }

  rowEl.style.display = '';
  resultEl.innerHTML = `<span style="color:var(--text-dim)">✦ Fetching execution details...</span>`;

  try {
    // Get full execution detail for error context
    const detail = await get(`/api/n8n/executions/${execId}`);
    const errorNodes = (detail.nodes || [])
      .filter(n => n.error)
      .map(n => `Node "${n.name}": ${n.error}`);

    const topErr = detail.error || {};
    let topError = topErr.message || '';
    if (topErr.source_node || topErr.destination_node) {
      topError += ` (from "${topErr.source_node}" → "${topErr.destination_node}")`;
    }
    if (topErr.description) topError += `. ${topErr.description}`;

    const errorSummary = errorNodes.length
      ? errorNodes.join('\n')
      : topError || 'Unknown error';

    resultEl.innerHTML = `<span style="color:var(--text-dim)">✦ Asking AI...</span>`;

    const prompt = `I have an n8n workflow called "${workflowName}" that failed in execution ${execId}.\n\nErrors:\n${errorSummary}\n\nPlease analyze what went wrong and suggest specific fixes.`;

    const resp = await post('/api/assistant/chat', {
      messages: [{ role: 'user', content: prompt }],
      context: '',
    });

    const md = (resp.response || 'No response').replace(/\n/g, '<br>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code style="background:var(--bg-input);padding:1px 5px;border-radius:3px;font-size:11px">$1</code>');
    resultEl.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:11px;font-weight:600;color:var(--accent)">✦ AI Analysis</span>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="navigator.clipboard.writeText(${JSON.stringify(resp.response || '').replace(/'/g,"&#39;")}).then(()=>window.__wfToast('Copied!'))">Copy</button>
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="document.getElementById('ai-row-${execId}').style.display='none'">✕</button>
        </div>
      </div>
      <div style="color:var(--text-secondary)">${md}</div>
    `;
  } catch (e) {
    resultEl.innerHTML = `<span style="color:var(--error)">${esc(e.message)}</span>`;
  }
};

window.__wfToast = (msg) => toast.success(msg);

window.__triggerWorkflow = async function(id) {
  try {
    const result = await post(`/api/n8n/workflows/${id}/trigger`, {});
    if (result.success) {
      toast.success(`Workflow triggered via ${result.method}`);
      setTimeout(() => window.__selectWorkflow(id), 1500);
    } else {
      toast.error(result.error || 'Trigger failed');
    }
  } catch (e) {
    toast.error(e.message);
  }
};

window.__injectDashboardTrigger = async function(id) {
  const ok = confirm(
    'Add a Dashboard Trigger webhook node to this workflow?\n\n' +
    '• A node named "__dashboard_trigger" will be added.\n' +
    '• It will be auto-wired to run alongside your existing trigger.\n' +
    '• If the workflow is active, it will be briefly deactivated and reactivated so the new webhook registers.\n\n' +
    'Continue?'
  );
  if (!ok) return;
  try {
    const result = await post(`/api/n8n/workflows/${id}/inject-trigger`, {});
    if (result.success) {
      toast.success(
        result.already_present
          ? 'Dashboard Trigger already present'
          : `Dashboard Trigger added → ${result.downstream_node || 'downstream node'}`
      );
      window.__selectWorkflow(id);
    } else {
      toast.error(result.error || 'Failed to inject trigger');
    }
  } catch (e) {
    toast.error(e.message);
  }
};

window.__removeDashboardTrigger = async function(id) {
  const ok = confirm(
    'Remove the Dashboard Trigger from this workflow?\n\n' +
    'The "__dashboard_trigger" node and its connection will be deleted. The workflow will be briefly deactivated and reactivated if it is active.'
  );
  if (!ok) return;
  try {
    const result = await fetch(`/api/n8n/workflows/${id}/inject-trigger`, { method: 'DELETE' }).then(r => r.json());
    if (result.success) {
      toast.success(result.not_present ? 'No Dashboard Trigger was present' : 'Dashboard Trigger removed');
      window.__selectWorkflow(id);
    } else {
      toast.error(result.error || 'Failed to remove trigger');
    }
  } catch (e) {
    toast.error(e.message);
  }
};

window.__toggleWorkflow = async function(id, active) {
  try {
    const result = await post(`/api/n8n/workflows/${id}/active`, { active });
    if (result.success) {
      toast.success(`Workflow ${active ? 'activated' : 'deactivated'}`);
      loadWorkflows();
      window.__selectWorkflow(id);
    } else {
      toast.error(result.error || 'Failed');
    }
  } catch (e) {
    toast.error(e.message);
  }
};

window.__deleteWorkflow = async function(id) {
  const name = (selectedWorkflowMeta.id === id && selectedWorkflowMeta.name) || id;
  const ok = await modal.confirmDelete({
    title: 'Delete workflow',
    bodyHtml: `
      <p style="margin:0 0 10px 0">Permanently delete this workflow from n8n.</p>
      <p style="margin:0 0 4px 0"><strong>Workflow:</strong> ${esc(name)}</p>
      <p style="margin:0 0 10px 0;font-family:var(--font-mono);font-size:12px;color:var(--text-dim)"><strong style="font-family:inherit">ID:</strong> ${esc(id)}</p>
      <p style="margin:0;color:var(--error)">This cannot be undone. Execution history will be lost.</p>
    `,
  });
  if (!ok) return;
  try {
    const result = await fetch(`/api/n8n/workflows/${id}`, { method: 'DELETE' }).then(r => r.json());
    if (result.success) {
      toast.success(`Deleted: ${name}`);
      selectedWorkflow = null;
      selectedWorkflowMeta = { id: '', name: '' };
      const detailEl = document.getElementById('wf-detail');
      if (detailEl) {
        detailEl.innerHTML = '<div class="empty-state"><h3>Select a workflow</h3><p>Click a workflow to see details and execution history</p></div>';
      }
      loadWorkflows();
    } else {
      toast.error(result.error || 'Delete failed');
    }
  } catch (e) {
    toast.error(e.message);
  }
};

function triggerClass(t) {
  if (t === 'webhook') return 'info';
  if (t === 'schedule') return 'warning';
  if (t === 'error') return 'error';
  return 'neutral';
}

function statusClass(s) {
  if (s === 'success') return 'success';
  if (s === 'error') return 'error';
  if (s === 'running') return 'warning';
  return 'neutral';
}

function formatTime(iso) {
  if (!iso) return '-';
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }


function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

function debounce(fn, ms) { let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); }; }
