/**
 * Export / Backup view — batch export workflows and download backups.
 */

import { get } from '../api.js';
import * as toast from '../components/toast.js';

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">Export / Backup</h2>
    </div>

    <!-- Quick backup row -->
    <div class="card" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
        <div>
          <span class="card-title">Full Backup</span>
          <span style="font-size:12px;color:var(--text-secondary);margin-left:8px">Download all workflows as JSON</span>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-primary" id="backup-all-btn">All Workflows</button>
          <button class="btn" id="backup-active-btn">Active Only</button>
        </div>
      </div>
      <div id="backup-status" style="margin-top:8px"></div>
    </div>

    <!-- Individual export -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">Export Individual</span>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm" id="select-all-btn">Select All</button>
          <button class="btn btn-sm" id="select-none-btn">Select None</button>
          <button class="btn btn-sm btn-primary" id="export-selected-btn">Export Selected</button>
        </div>
      </div>
      <div id="workflow-checklist"><div class="spinner"></div></div>
    </div>

    <!-- Backup history -->
    <div class="card" style="margin-top:16px">
      <div class="card-header">
        <span class="card-title">Restore from Backup</span>
      </div>
      <div class="drop-zone" id="restore-drop-zone">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--text-dim)" stroke-width="1.5">
          <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
          <polyline points="17 8 12 3 7 8"/>
          <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>
        <p style="margin-top:12px;font-size:14px;color:var(--text-secondary)">
          Drop a backup JSON file to restore workflows
        </p>
        <p style="font-size:12px;color:var(--text-dim);margin-top:4px">
          Workflows will be imported as inactive
        </p>
        <input type="file" id="restore-input" accept=".json" style="display:none">
      </div>
      <div id="restore-results" style="margin-top:12px"></div>
    </div>
  `;

  setupHandlers();
  loadWorkflowList();
}

function setupHandlers() {
  document.getElementById('backup-all-btn').addEventListener('click', () => downloadBackup(false));
  document.getElementById('backup-active-btn').addEventListener('click', () => downloadBackup(true));
  document.getElementById('select-all-btn').addEventListener('click', () => toggleAll(true));
  document.getElementById('select-none-btn').addEventListener('click', () => toggleAll(false));
  document.getElementById('export-selected-btn').addEventListener('click', exportSelected);

  // Restore drop zone
  const dropZone = document.getElementById('restore-drop-zone');
  const fileInput = document.getElementById('restore-input');
  dropZone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', (e) => { if (e.target.files[0]) restoreBackup(e.target.files[0]); fileInput.value = ''; });
  dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drop-active'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drop-active'));
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drop-active');
    if (e.dataTransfer.files[0]) restoreBackup(e.dataTransfer.files[0]);
  });
}

async function downloadBackup(activeOnly) {
  const statusEl = document.getElementById('backup-status');
  statusEl.innerHTML = '<div class="spinner" style="margin:0"></div>';

  try {
    const resp = await fetch(`/api/n8n/backup?active_only=${activeOnly}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const disposition = resp.headers.get('Content-Disposition') || '';
    const filenameMatch = disposition.match(/filename="(.+)"/);
    const filename = filenameMatch ? filenameMatch[1] : 'n8n-backup.json';

    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);

    const data = JSON.parse(await blob.text());
    statusEl.innerHTML = `<span class="pill pill-success">Downloaded</span> <span style="font-size:12px;color:var(--text-secondary)">${data.workflow_count} workflows — ${filename}</span>`;
    toast.success(`Backup downloaded: ${data.workflow_count} workflows`);
  } catch (e) {
    statusEl.innerHTML = `<span class="pill pill-error">Failed</span> <span style="font-size:12px;color:var(--text-secondary)">${e.message}</span>`;
    toast.error(e.message);
  }
}

let workflowList = [];

async function loadWorkflowList() {
  const el = document.getElementById('workflow-checklist');
  try {
    const data = await get('/api/n8n/workflows?limit=250');
    workflowList = data.workflows || [];

    if (!workflowList.length) {
      el.innerHTML = '<div class="empty-state"><p>No workflows found</p></div>';
      return;
    }

    el.innerHTML = `<div style="max-height:300px;overflow-y:auto">${workflowList.map(w => `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 4px;border-bottom:1px solid var(--border-dim);cursor:pointer;margin:0">
        <input type="checkbox" class="wf-check" value="${w.id}" checked>
        <span class="status-dot ${w.active ? 'online' : 'offline'}"></span>
        <span style="flex:1;font-size:13px">${esc(w.name)}</span>
        <span class="pill pill-${w.active ? 'success' : 'neutral'}" style="font-size:10px">${w.active ? 'active' : 'off'}</span>
      </label>
    `).join('')}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Failed to load: ${esc(e.message)}</p></div>`;
  }
}

function toggleAll(checked) {
  document.querySelectorAll('.wf-check').forEach(cb => cb.checked = checked);
}

async function exportSelected() {
  const selected = [...document.querySelectorAll('.wf-check:checked')].map(cb => cb.value);
  if (!selected.length) { toast.error('No workflows selected'); return; }

  toast.info(`Exporting ${selected.length} workflows...`);

  try {
    const workflows = [];
    for (const id of selected) {
      const wf = await get(`/api/n8n/workflows/${id}/export`);
      if (wf && wf.id) workflows.push(wf);
    }

    const exportData = {
      backup_version: "1.0",
      created_at: new Date().toISOString(),
      instance: { name: "selected-export" },
      workflow_count: workflows.length,
      workflows,
    };

    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `n8n-export-${selected.length}wf-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);

    toast.success(`Exported ${workflows.length} workflows`);
  } catch (e) {
    toast.error(e.message);
  }
}

async function restoreBackup(file) {
  const resultsEl = document.getElementById('restore-results');
  resultsEl.innerHTML = '<div class="spinner" style="margin:0"></div>';

  try {
    const text = await file.text();
    const data = JSON.parse(text);

    let workflows = [];
    if (data.workflows && Array.isArray(data.workflows)) {
      workflows = data.workflows;
    } else if (data.nodes) {
      // Single workflow file
      workflows = [data];
    } else {
      throw new Error('Unrecognized backup format');
    }

    const results = [];
    for (const wf of workflows) {
      try {
        const resp = await fetch('/api/n8n/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(wf),
        });
        const r = await resp.json();
        results.push({ name: wf.name || 'Unknown', success: r.success, error: r.error });
      } catch (e) {
        results.push({ name: wf.name || 'Unknown', success: false, error: e.message });
      }
    }

    const ok = results.filter(r => r.success).length;
    const fail = results.filter(r => !r.success).length;

    resultsEl.innerHTML = `
      <div style="margin-bottom:8px">
        <span class="pill pill-success">${ok} imported</span>
        ${fail ? `<span class="pill pill-error" style="margin-left:4px">${fail} failed</span>` : ''}
      </div>
      ${results.map(r => `
        <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
          <span class="pill pill-${r.success ? 'success' : 'error'}" style="font-size:10px">${r.success ? 'OK' : 'FAIL'}</span>
          <span>${esc(r.name)}</span>
          ${r.error ? `<span style="color:var(--text-dim)">${esc(r.error)}</span>` : ''}
        </div>
      `).join('')}
    `;

    toast.success(`Restore complete: ${ok}/${workflows.length} workflows`);
  } catch (e) {
    resultsEl.innerHTML = `<span class="pill pill-error">Failed</span> <span style="font-size:12px">${esc(e.message)}</span>`;
    toast.error(e.message);
  }
}

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }
