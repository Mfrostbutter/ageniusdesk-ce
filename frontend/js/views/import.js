/**
 * Import Workflows view — upload n8n workflow JSON files to your instance.
 */

import { get, post } from '../api.js';
import * as toast from '../components/toast.js';

let targetInstanceId = '';

export async function render(container) {
  // Load instances for the selector
  let instancesHtml = '';
  try {
    const data = await get('/api/n8n/instances');
    const instances = data.instances || [];
    if (instances.length >= 1) {
      instancesHtml = `
        <div class="card" style="margin-bottom:16px">
          <div class="card-header">
            <span class="card-title">Import to</span>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            ${instances.map(inst => `
              <button class="btn btn-sm instance-target ${inst.active ? 'active' : ''}" data-id="${inst.id}" onclick="window.__setImportTarget('${jsStr(inst.id)}', this)" style="${inst.active ? 'background:var(--accent-glow);color:var(--accent);border-color:var(--accent)' : ''}">
                <span class="instance-dot" style="background:${inst.color || '#ff6d5a'}"></span>
                ${esc(inst.name)}
              </button>
            `).join('')}
          </div>
        </div>
      `;
      targetInstanceId = (instances.find(i => i.active) || instances[0]).id;
    }
  } catch { /* single instance, use active */ }

  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">Import Workflows</h2>
    </div>

    ${instancesHtml}

    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">Import options</span>
        <span style="font-size:11px;color:var(--text-dim)">Applied to every workflow imported below</span>
      </div>
      <div class="grid-2" style="gap:12px">
        <div>
          <label for="import-title" style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">
            Title override <span style="color:var(--text-dim)">(optional, replaces the JSON's name)</span>
          </label>
          <input id="import-title" type="text" style="width:100%" placeholder="Leave blank to keep original name">
        </div>
        <div>
          <label for="import-tags" style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">
            Tags <span style="color:var(--text-dim)">(comma-separated; created if missing)</span>
          </label>
          <input id="import-tags" type="text" style="width:100%" placeholder="e.g. agenius, images, kie-ai">
        </div>
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--text-dim)">
        Bulk uploads share these options. Title override is ignored when importing multiple files at once.
      </div>
    </div>

    <div class="grid-2">
      <!-- File upload -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Upload JSON</span>
        </div>
        <div class="drop-zone" id="drop-zone">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--text-dim)" stroke-width="1.5">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
            <polyline points="17 8 12 3 7 8"/>
            <line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
          <p style="margin-top:12px;font-size:14px;color:var(--text-secondary)">
            Drop workflow JSON files here
          </p>
          <p style="font-size:12px;color:var(--text-dim);margin-top:4px">
            or click to browse
          </p>
          <input type="file" id="file-input" accept=".json" multiple style="display:none">
        </div>
      </div>

      <!-- Paste JSON -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Paste JSON</span>
        </div>
        <textarea id="json-paste" class="json-editor" placeholder='Paste n8n workflow JSON here...\n\n{\n  "name": "My Workflow",\n  "nodes": [...],\n  "connections": {...}\n}' style="min-height:200px"></textarea>
        <div style="margin-top:12px;display:flex;gap:8px">
          <button class="btn btn-primary" id="import-paste-btn">Import</button>
          <button class="btn" id="clear-paste-btn">Clear</button>
        </div>
      </div>
    </div>

    <!-- Import results -->
    <div class="card" style="margin-top:16px" id="import-results-card">
      <div class="card-header">
        <span class="card-title">Import History</span>
      </div>
      <div id="import-results">
        <div class="empty-state"><p>No imports yet this session</p></div>
      </div>
    </div>
  `;

  setupHandlers();
}

const importLog = [];

function setupHandlers() {
  const dropZone = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');

  // Click to browse
  dropZone.addEventListener('click', () => fileInput.click());

  // File selection — if multiple, suppress title override (it'd clobber all to the same name)
  fileInput.addEventListener('change', (e) => {
    const files = [...e.target.files];
    const isBulk = files.length > 1;
    for (const file of files) processFile(file, { isBulk });
    fileInput.value = '';
  });

  // Drag and drop
  dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drop-active'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drop-active'));
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drop-active');
    const jsonFiles = [...e.dataTransfer.files].filter(f => f.name.endsWith('.json'));
    const isBulk = jsonFiles.length > 1;
    for (const file of jsonFiles) processFile(file, { isBulk });
  });

  // Paste import
  document.getElementById('import-paste-btn').addEventListener('click', async () => {
    const text = document.getElementById('json-paste').value.trim();
    if (!text) return;
    try {
      const data = JSON.parse(text);
      await importWorkflow(data, 'pasted', { isBulk: false });
      document.getElementById('json-paste').value = '';
    } catch (e) {
      toast.error('Invalid JSON: ' + e.message);
    }
  });

  document.getElementById('clear-paste-btn').addEventListener('click', () => {
    document.getElementById('json-paste').value = '';
  });
}

function readImportOptions({ isBulk }) {
  const title = document.getElementById('import-title')?.value.trim() || '';
  const tagsRaw = document.getElementById('import-tags')?.value.trim() || '';
  const tags = tagsRaw
    ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean)
    : [];
  return {
    name_override: !isBulk && title ? title : null,
    tags,
  };
}

async function processFile(file, { isBulk } = { isBulk: false }) {
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    await importWorkflow(data, file.name, { isBulk });
  } catch (e) {
    toast.error(`Failed to parse ${file.name}: ${e.message}`);
    addResult(file.name, false, e.message);
  }
}

window.__setImportTarget = async function(id, btn) {
  targetInstanceId = id;
  document.querySelectorAll('.instance-target').forEach(b => {
    b.style.background = ''; b.style.color = ''; b.style.borderColor = '';
    b.classList.remove('active');
  });
  btn.style.background = 'var(--accent-glow)';
  btn.style.color = 'var(--accent)';
  btn.style.borderColor = 'var(--accent)';
  btn.classList.add('active');
  // Switch active instance on the backend
  await post(`/api/n8n/instances/${id}/activate`);
  if (window.__refreshInstances) window.__refreshInstances();
};

async function importWorkflow(data, source, { isBulk } = { isBulk: false }) {
  const opts = readImportOptions({ isBulk });
  const displayName = opts.name_override || data.name || 'Unnamed Workflow';
  try {
    const result = await post('/api/n8n/import', {
      workflow: data,
      name_override: opts.name_override,
      tags: opts.tags,
    });
    if (result.success) {
      const finalName = result.name || displayName;
      const tagBadge = (result.tags_applied && result.tags_applied.length)
        ? ` [${result.tags_applied.join(', ')}]`
        : '';
      toast.success(`Imported: ${finalName}${tagBadge}`);
      let msg = `${finalName} (ID: ${result.workflow_id})${tagBadge}`;
      if (result.warning) msg += ` — ${result.warning}`;
      addResult(source, true, msg);
    } else {
      toast.error(result.error || 'Import failed');
      addResult(source, false, result.error || 'Unknown error');
    }
  } catch (e) {
    toast.error(`Import failed: ${e.message}`);
    addResult(source, false, e.message);
  }
}

function addResult(source, success, message) {
  importLog.unshift({ source, success, message, time: new Date() });
  renderResults();
}

function renderResults() {
  const el = document.getElementById('import-results');
  if (!importLog.length) {
    el.innerHTML = '<div class="empty-state"><p>No imports yet this session</p></div>';
    return;
  }
  el.innerHTML = importLog.map(r => `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-dim)">
      <span class="pill pill-${r.success ? 'success' : 'error'}">${r.success ? 'OK' : 'FAIL'}</span>
      <span style="flex:1;font-size:13px">${esc(r.message)}</span>
      <span style="font-size:11px;color:var(--text-dim);font-family:var(--font-mono)">${r.source}</span>
    </div>
  `).join('');
}

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }


function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}
