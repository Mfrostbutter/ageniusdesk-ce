/**
 * Export / Backup view — batch export workflows and download backups.
 */

import { get, post, put } from '../api.js';
import * as toast from '../components/toast.js';

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">Export / Backup</h2>
    </div>

    <!-- Scheduled backups -->
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">Scheduled Backups</span>
        <label class="switch" style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" id="sb-enabled">
          <span style="font-size:12px;color:var(--text-secondary)">Enabled</span>
        </label>
      </div>
      <p style="font-size:12px;color:var(--text-secondary);margin:0 0 12px">
        Snapshot every connected instance's workflows to disk on a schedule, keeping the most recent copies.
      </p>
      <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end">
        <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--text-secondary)">
          Every (hours)
          <input type="number" id="sb-interval" min="1" max="720" style="width:100px" class="input">
        </label>
        <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--text-secondary)">
          Keep (snapshots/instance)
          <input type="number" id="sb-retention" min="1" max="500" style="width:160px" class="input">
        </label>
        <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-secondary);padding-bottom:6px">
          <input type="checkbox" id="sb-active-only"> Active workflows only
        </label>
        <div style="display:flex;gap:8px;margin-left:auto">
          <button class="btn btn-sm" id="sb-run-btn">Back up now</button>
          <button class="btn btn-sm btn-primary" id="sb-save-btn">Save</button>
        </div>
      </div>
      <div id="sb-status" style="margin-top:10px;font-size:12px;color:var(--text-secondary)"></div>
      <div id="sb-list" style="margin-top:12px"></div>
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
  setupScheduledBackups();
  loadWorkflowList();
}

async function setupScheduledBackups() {
  document.getElementById('sb-save-btn').addEventListener('click', saveScheduledSettings);
  document.getElementById('sb-run-btn').addEventListener('click', runScheduledBackupNow);
  await loadScheduledSettings();
  await loadBackupList();
}

async function loadScheduledSettings() {
  try {
    const data = await get('/api/backups/settings');
    const s = data.settings || {};
    document.getElementById('sb-enabled').checked = !!s.enabled;
    document.getElementById('sb-interval').value = s.interval_hours ?? 24;
    document.getElementById('sb-retention').value = s.retention ?? 14;
    document.getElementById('sb-active-only').checked = !!s.active_only;
    renderJobStatus(data.job);
  } catch (e) {
    document.getElementById('sb-status').textContent = `Could not load settings: ${e.message}`;
  }
}

function renderJobStatus(job) {
  const el = document.getElementById('sb-status');
  if (!job) { el.textContent = ''; return; }
  const bits = [];
  if (job.last_run_at) {
    const when = new Date(job.last_run_at).toLocaleString();
    const badge = job.last_status === 'ok' ? 'pill-success' : (job.last_status === 'error' ? 'pill-error' : 'pill-neutral');
    bits.push(`<span class="pill ${badge}" style="font-size:10px">${job.last_status || 'idle'}</span> last run ${esc(when)}`);
    if (job.last_error) bits.push(`<span style="color:var(--error)">${esc(job.last_error)}</span>`);
  } else {
    bits.push('Never run yet.');
  }
  if (job.enabled && job.next_run_in_seconds != null) {
    bits.push(`next in ~${fmtDuration(job.next_run_in_seconds)}`);
  }
  el.innerHTML = bits.join(' · ');
}

async function saveScheduledSettings() {
  const body = {
    enabled: document.getElementById('sb-enabled').checked,
    interval_hours: parseInt(document.getElementById('sb-interval').value, 10) || 24,
    retention: parseInt(document.getElementById('sb-retention').value, 10) || 14,
    active_only: document.getElementById('sb-active-only').checked,
  };
  try {
    await put('/api/backups/settings', body);
    toast.success('Backup schedule saved');
    await loadScheduledSettings();
  } catch (e) {
    toast.error(e.message);
  }
}

async function runScheduledBackupNow() {
  const btn = document.getElementById('sb-run-btn');
  btn.disabled = true;
  toast.info('Running backup...');
  try {
    const r = await post('/api/backups/run', {});
    const res = r.last_result;
    if (res) {
      toast.success(`Backed up ${res.instances_ok}/${res.instances_total} instances, ${res.workflows_total} workflows`);
    } else if (r.skipped) {
      toast.info(`Skipped: ${r.skipped}`);
    } else if (r.last_status === 'error') {
      toast.error(r.last_error || 'Backup failed');
    }
    await loadScheduledSettings();
    await loadBackupList();
  } catch (e) {
    toast.error(e.message);
  } finally {
    btn.disabled = false;
  }
}

async function loadBackupList() {
  const el = document.getElementById('sb-list');
  try {
    const data = await get('/api/backups');
    const instances = data.instances || [];
    if (!instances.length) {
      el.innerHTML = '<div style="font-size:12px;color:var(--text-dim)">No stored snapshots yet.</div>';
      return;
    }
    el.innerHTML = instances.map(inst => `
      <div style="margin-top:8px">
        <div style="font-size:12px;font-weight:600;margin-bottom:4px">
          ${esc(inst.instance_name)}
          ${inst.known ? '' : '<span class="pill pill-neutral" style="font-size:10px;margin-left:4px">removed instance</span>'}
          <span style="color:var(--text-dim);font-weight:400">${inst.count} snapshot${inst.count === 1 ? '' : 's'}</span>
        </div>
        ${inst.files.map(f => `
          <div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px">
            <a href="/api/backups/${encodeURIComponent(inst.instance_id)}/${encodeURIComponent(f.filename)}"
               download style="color:var(--accent)">${esc(f.filename)}</a>
            <span style="color:var(--text-dim)">${f.created_at ? new Date(f.created_at).toLocaleString() : ''} · ${fmtBytes(f.size_bytes)}</span>
          </div>
        `).join('')}
      </div>
    `).join('');
  } catch (e) {
    el.innerHTML = `<div style="font-size:12px;color:var(--error)">${esc(e.message)}</div>`;
  }
}

function fmtDuration(secs) {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  return `${Math.round(secs / 3600)}h`;
}

function fmtBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
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
