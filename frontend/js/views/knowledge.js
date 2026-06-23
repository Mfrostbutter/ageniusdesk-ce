/**
 * Knowledge — unified view consolidating Sources, Connectors, Instructions,
 * and Notes into collapsible sections. Replaces the former four-tab layout.
 *
 * Each section is lazy-rendered on first expand so we don't hit the network
 * for sections the user never opens. State is module-level so re-visits
 * within a session don't re-fetch unnecessarily.
 */

import { get, post, put, del } from '../api.js';
import * as toast from '../components/toast.js';
import * as notesView from './notes.js';

// ── Styles ───────────────────────────────────────────────────────────────────

const STYLES = `
  <style>
    .ku-section {
      background: var(--bg-panel);
      border: 1px solid var(--border-dim);
      border-radius: var(--radius);
      margin-bottom: 12px;
      overflow: hidden;
    }
    .ku-section summary {
      list-style: none;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 13px 16px;
      cursor: pointer;
      user-select: none;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary);
      border-bottom: 1px solid transparent;
      transition: background 0.1s;
    }
    .ku-section[open] summary {
      border-bottom-color: var(--border-dim);
    }
    .ku-section summary:hover { background: rgba(255,255,255,0.03); }
    .ku-section summary::-webkit-details-marker { display: none; }
    .ku-sect-chevron {
      margin-left: auto; color: var(--text-dim); transition: transform 0.15s;
      flex-shrink: 0;
    }
    .ku-section[open] .ku-sect-chevron { transform: rotate(90deg); }
    .ku-sect-badge {
      font-size: 10px; font-weight: 500; color: var(--text-dim);
      background: var(--bg-input); border: 1px solid var(--border-dim);
      border-radius: 10px; padding: 1px 7px;
    }
    .ku-sect-body { padding: 16px; }
    .ku-kc-pill-on {
      font-size: 10px; font-weight: 700; background: rgba(52,211,153,0.15);
      color: #34d399; border: 1px solid #34d39944; border-radius: 3px;
      padding: 2px 6px; cursor: pointer;
    }
    .ku-kc-pill-off {
      font-size: 10px; font-weight: 700; background: var(--bg-input);
      color: var(--text-dim); border: 1px solid var(--border-dim); border-radius: 3px;
      padding: 2px 6px; cursor: pointer;
    }
  </style>
`;

// ── Module-level state ────────────────────────────────────────────────────────

let _sourcesCache = [];
let _sectRendered = { sources: false, connectors: false, instructions: false };
let _constitutionVersion = null;
let _constitutionContent = '';
let _constitutionDirty = false;

// ── Entry point ───────────────────────────────────────────────────────────────

export async function render(container) {
  _sectRendered = { sources: false, connectors: false, instructions: false };

  container.innerHTML = `
    ${STYLES}
    <div style="height:100%;display:flex;flex-direction:column;min-height:0">
      <div class="section-header" style="margin-bottom:12px;flex:none">
        <h2 class="section-title">Harness</h2>
        <span style="font-size:12px;color:var(--text-dim)">The workspace files every agent works within</span>
      </div>

      <!-- Config drawer: external knowledge + agent instructions (collapsed) -->
      <details class="ku-section" id="ku-config" style="flex:none">
        <summary>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        Sources, Connectors &amp; Instructions
        <span class="ku-sect-badge">external knowledge + agent rules</span>
        <svg class="ku-sect-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
        </summary>
        <div class="ku-sect-body" style="display:flex;flex-direction:column;gap:10px">
          <!-- Sources -->
          <details class="ku-section" id="ku-sources" open style="margin:0">
            <summary>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6"/></svg>
              Sources
              <span class="ku-sect-badge" id="ku-sources-badge"></span>
              <svg class="ku-sect-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
            </summary>
            <div class="ku-sect-body" id="ku-sources-body"><div class="spinner"></div></div>
          </details>

          <!-- Connectors -->
          <details class="ku-section" id="ku-connectors" style="margin:0">
            <summary>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/></svg>
              Connectors
              <span class="ku-sect-badge" id="ku-connectors-badge"></span>
              <svg class="ku-sect-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
            </summary>
            <div class="ku-sect-body" id="ku-connectors-body"></div>
          </details>

          <!-- Instructions -->
          <details class="ku-section" id="ku-instructions" style="margin:0">
            <summary>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
              Instructions (AGENTS.md)
              <svg class="ku-sect-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
            </summary>
            <div class="ku-sect-body" id="ku-instructions-body"></div>
          </details>
        </div>
      </details>

      <!-- Workspace vault: the harness files (primary content) -->
      <div id="ku-vault" style="flex:1;min-height:0;margin-top:12px"></div>
    </div>
  `;

  // Lazy-render the config subsections. Sources is open by default, so render
  // it the first time the config drawer is opened; connectors/instructions
  // render on their own toggles.
  document.getElementById('ku-config').addEventListener('toggle', (e) => {
    if (e.target.open && !_sectRendered.sources) renderSources();
  });
  document.getElementById('ku-sources').addEventListener('toggle', onSourcesToggle, { once: false });
  document.getElementById('ku-connectors').addEventListener('toggle', onConnectorsToggle, { once: false });
  document.getElementById('ku-instructions').addEventListener('toggle', onInstructionsToggle, { once: false });

  // Mount the workspace vault inline as the harness's primary surface.
  await notesView.render(document.getElementById('ku-vault'));
}

export function teardown() {
  _sectRendered = { sources: false, connectors: false, instructions: false };
  _constitutionDirty = false;
}

// ── Section toggle handlers ───────────────────────────────────────────────────

function onSourcesToggle(e) {
  if (e.target.open && !_sectRendered.sources) renderSources();
}

function onConnectorsToggle(e) {
  if (e.target.open && !_sectRendered.connectors) renderConnectors();
}

function onInstructionsToggle(e) {
  if (e.target.open && !_sectRendered.instructions) renderInstructions();
}

// ── Sources ───────────────────────────────────────────────────────────────────

async function renderSources() {
  _sectRendered.sources = true;
  const body = document.getElementById('ku-sources-body');
  if (!body) return;

  try {
    const data = await get('/api/knowledge/sources');
    _sourcesCache = data.sources || [];
  } catch (e) {
    _sourcesCache = [];
    body.innerHTML = `<div style="color:var(--error);font-size:12px">Failed to load: ${escHtml(e.message || String(e))}</div>`;
    return;
  }

  const badge = document.getElementById('ku-sources-badge');
  if (badge) {
    const enabled = _sourcesCache.filter(s => s.enabled).length;
    badge.textContent = `${_sourcesCache.length} sources · ${enabled} enabled`;
  }

  renderSourcesList(body);
}

function renderSourcesList(body) {
  const addBtn = `<div style="display:flex;justify-content:flex-end;margin-bottom:12px"><button class="btn btn-sm btn-primary" id="ku-src-add">+ Add source</button></div>`;

  if (!_sourcesCache.length) {
    body.innerHTML = `${addBtn}<div class="empty-state"><p>No sources registered yet. Click <strong>+ Add source</strong> to wire your first Qdrant collection.</p></div>`;
    body.querySelector('#ku-src-add').addEventListener('click', () => openSourceDialog());
    return;
  }

  body.innerHTML = `
    ${addBtn}
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Name</th><th>Kind</th><th>Collection</th><th>Description</th><th>Status</th>
          <th style="text-align:right">Actions</th>
        </tr></thead>
        <tbody>
          ${_sourcesCache.map(s => `
            <tr data-id="${s.id}">
              <td style="font-weight:600">${escHtml(s.name)}</td>
              <td><span class="pill pill-neutral">${escHtml(s.kind)}</span></td>
              <td style="font-family:var(--font-mono);font-size:12px;color:var(--text-secondary)">${escHtml((s.config || {}).collection || '—')}</td>
              <td style="color:var(--text-secondary);max-width:240px">${escHtml(s.description || '—')}</td>
              <td>
                <span class="${s.enabled ? 'pill pill-success' : 'pill pill-neutral'}">${s.enabled ? 'enabled' : 'disabled'}</span>
                <span class="ks-probe" data-id="${s.id}" style="font-size:11px;font-family:var(--font-mono);color:var(--text-dim);margin-left:6px"></span>
              </td>
              <td style="text-align:right;white-space:nowrap">
                <button class="btn btn-sm btn-ghost" data-act="test" data-id="${s.id}">Test</button>
                <button class="btn btn-sm btn-ghost" data-act="edit" data-id="${s.id}">Edit</button>
                <button class="btn btn-sm btn-ghost btn-danger" data-act="delete" data-id="${s.id}">Delete</button>
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;

  body.querySelector('#ku-src-add').addEventListener('click', () => openSourceDialog());
  body.querySelectorAll('button[data-act]').forEach(btn => {
    btn.addEventListener('click', () => onSourceRowAction(btn.dataset.act, Number(btn.dataset.id)));
  });
}

async function onSourceRowAction(act, id) {
  const src = _sourcesCache.find(s => s.id === id);
  if (!src) return;
  if (act === 'edit') return openSourceDialog(src);
  if (act === 'delete') {
    if (!confirm(`Delete source "${src.name}"?`)) return;
    try {
      await del(`/api/knowledge/sources/${id}`);
      toast.success('Source deleted');
      await renderSources();
    } catch (e) { toast.error('Delete failed: ' + (e.message || e)); }
    return;
  }
  if (act === 'test') {
    const cell = document.querySelector(`.ks-probe[data-id="${id}"]`);
    if (cell) cell.textContent = '…testing';
    try {
      const r = await post(`/api/knowledge/sources/${id}/test`, {});
      if (cell) {
        cell.textContent = r.ok ? 'reachable' : (r.error || 'failed');
        cell.style.color = r.ok ? 'var(--success)' : 'var(--error)';
      }
    } catch (e) {
      if (cell) { cell.textContent = e.message || String(e); cell.style.color = 'var(--error)'; }
    }
  }
}

function openSourceDialog(src) {
  const existing = document.getElementById('ku-src-modal');
  if (existing) existing.remove();

  const c = src ? (src.config || {}) : {};
  const title = src ? `Edit ${escHtml(src.name)}` : 'Add source';

  const modal = document.createElement('div');
  modal.id = 'ku-src-modal';
  modal.className = 'modal';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = `
    <div class="modal-content" role="dialog" aria-modal="true" aria-label="${title}"
         style="max-width:640px;width:92%;padding:0;overflow:hidden;display:flex;flex-direction:column;max-height:90vh">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:20px 24px 16px;border-bottom:1px solid var(--border-dim)">
        <h3 style="margin:0;font-size:16px;font-weight:600">${title}</h3>
        <button class="btn btn-sm btn-ghost" id="ku-src-modal-close" style="font-size:18px;line-height:1;padding:2px 8px">&times;</button>
      </div>
      <form id="ku-src-form" style="padding:20px 24px;display:flex;flex-direction:column;gap:14px;max-height:calc(80vh - 120px);overflow-y:auto">
        <div>
          <label for="ku-f-name">Name <small style="font-weight:400;color:var(--text-dim)">(stable identifier — lowercase, dots/dashes ok)</small></label>
          <input id="ku-f-name" name="name" required pattern="[a-z0-9][a-z0-9._-]*" value="${escAttr(src ? src.name : '')}" />
        </div>
        <div>
          <label for="ku-f-kind">Kind</label>
          <select id="ku-f-kind" name="kind">
            <option value="qdrant"${(!src || src.kind === 'qdrant') ? ' selected' : ''}>qdrant</option>
          </select>
        </div>
        <div>
          <label for="ku-f-desc">Description <small style="font-weight:400;color:var(--text-dim)">(routing signal — describe what's inside, one sentence)</small></label>
          <textarea id="ku-f-desc" name="description" rows="3" style="resize:vertical">${escHtml(src ? (src.description || '') : '')}</textarea>
        </div>
        <div class="grid-2">
          <div><label for="ku-f-url">Qdrant URL</label><input id="ku-f-url" name="url" placeholder="http://localhost:6333" value="${escAttr(c.url || '')}" /></div>
          <div><label for="ku-f-collection">Collection</label><input id="ku-f-collection" name="collection" value="${escAttr(c.collection || '')}" /></div>
          <div><label for="ku-f-vector">Vector name</label><input id="ku-f-vector" name="vector_name" value="${escAttr(c.vector_name ?? '')}" placeholder="dense (leave blank for unnamed vectors)" /></div>
          <div><label for="ku-f-payload">Text payload key</label><input id="ku-f-payload" name="text_payload_key" value="${escAttr(c.text_payload_key || 'text')}" /></div>
          <div><label for="ku-f-apikey">Qdrant API key secret <small style="font-weight:400;color:var(--text-dim)">(optional)</small></label><input id="ku-f-apikey" name="api_key_secret" placeholder="$QDRANT_API_KEY" value="${escAttr(c.api_key_secret || '')}" /></div>
          <div>
            <label for="ku-f-embedder">Embedder</label>
            <select id="ku-f-embedder" name="embedder">
              <option value="openai"${(!src || (c.embedder || 'openai') === 'openai') ? ' selected' : ''}>openai</option>
              <option value="voyage"${c.embedder === 'voyage' ? ' selected' : ''}>voyage (legacy)</option>
            </select>
          </div>
          <div><label for="ku-f-embed-model">Embed model</label><input id="ku-f-embed-model" name="embed_model" value="${escAttr(c.embed_model || 'text-embedding-3-large')}" /></div>
          <div><label for="ku-f-openai">OpenAI key secret</label><input id="ku-f-openai" name="openai_key_secret" value="${escAttr(c.openai_key_secret || '$OPENAI_API_KEY')}" /></div>
          <div><label for="ku-f-voyage">Voyage key secret <small style="font-weight:400;color:var(--text-dim)">(legacy)</small></label><input id="ku-f-voyage" name="voyage_key_secret" value="${escAttr(c.voyage_key_secret || '')}" /></div>
        </div>
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:0;cursor:pointer">
          <input type="checkbox" name="enabled"${(!src || src.enabled) ? ' checked' : ''} /> Enabled
        </label>
      </form>
      <div style="display:flex;gap:8px;justify-content:flex-end;padding:14px 24px;border-top:1px solid var(--border-dim)">
        <button type="button" class="btn btn-sm" id="ku-src-cancel">Cancel</button>
        <button type="button" class="btn btn-sm btn-primary" id="ku-src-save">Save</button>
      </div>
    </div>
  `;

  modal.dataset.editId = src ? String(src.id) : '';
  document.body.appendChild(modal);

  document.getElementById('ku-src-modal-close').addEventListener('click', () => modal.remove());
  document.getElementById('ku-src-cancel').addEventListener('click', () => modal.remove());
  document.getElementById('ku-src-save').addEventListener('click', () => saveSource(modal));

  const onKey = (e) => { if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', onKey); } };
  document.addEventListener('keydown', onKey);
  modal._keyHandler = onKey;

  setTimeout(() => document.getElementById('ku-f-name')?.focus(), 0);
}

async function saveSource(modal) {
  const form = document.getElementById('ku-src-form');
  if (!form) return;
  const payload = {
    name: form.name.value.trim(),
    kind: form.kind.value,
    description: form.description.value.trim(),
    enabled: form.enabled.checked,
    config: {
      url: form.url.value.trim(),
      collection: form.collection.value.trim(),
      vector_name: form.vector_name.value.trim(),
      text_payload_key: form.text_payload_key.value.trim() || 'text',
      api_key_secret: form.api_key_secret.value.trim(),
      embedder: form.embedder.value,
      embed_model: form.embed_model.value.trim() || 'text-embedding-3-large',
      openai_key_secret: form.openai_key_secret.value.trim(),
      voyage_key_secret: form.voyage_key_secret.value.trim(),
    },
  };
  const saveBtn = document.getElementById('ku-src-save');
  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }
  try {
    const id = modal.dataset.editId;
    if (id) {
      await put(`/api/knowledge/sources/${id}`, payload);
      toast.success('Source updated');
    } else {
      await post('/api/knowledge/sources', payload);
      toast.success('Source added');
    }
    modal.remove();
    _sectRendered.sources = false;
    await renderSources();
  } catch (err) {
    toast.error('Save failed: ' + (err.message || err));
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
  }
}

// ── Connectors ────────────────────────────────────────────────────────────────

async function renderConnectors() {
  _sectRendered.connectors = true;
  const body = document.getElementById('ku-connectors-body');
  if (!body) return;
  body.innerHTML = '<div class="spinner"></div>';

  try {
    const data = await get('/api/knowledge/connectors');
    const connectors = data.connectors || [];
    const enabledCount = connectors.filter(c => c.knowledge_enabled).length;

    const badge = document.getElementById('ku-connectors-badge');
    if (badge) badge.textContent = `${connectors.length} servers · ${enabledCount} in Harness`;

    if (!connectors.length) {
      body.innerHTML = `<div class="empty-state"><p>No MCP servers registered yet. <a href="#" onclick="window.__nav('assistant');return false" style="color:var(--accent)">Add one in Assistant settings.</a></p></div>`;
      return;
    }

    body.innerHTML = `
      <div style="font-size:12px;color:var(--text-secondary);margin-bottom:12px;line-height:1.5">
        Toggle which MCP servers are available to the Harness. Enabled connectors are surfaced in the Instructions document so agents know what tools are available.
      </div>
      <div id="ku-kc-list">
        ${connectors.map(c => `
          <div class="ku-kc-row" data-id="${escAttr(c.id)}"
               style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--border-dim)">
            <label style="display:flex;align-items:center;gap:0;cursor:pointer;flex-shrink:0">
              <input type="checkbox" class="ku-kc-check" data-id="${escAttr(c.id)}" ${c.knowledge_enabled ? 'checked' : ''} style="display:none" />
              <span class="${c.knowledge_enabled ? 'ku-kc-pill-on' : 'ku-kc-pill-off'}">
                ${c.knowledge_enabled ? 'In Harness' : 'Off'}
              </span>
            </label>
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:13px">${escHtml(c.name)}</div>
              <div style="font-size:11px;font-family:var(--font-mono);color:var(--text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(c.url)}</div>
              ${c.description ? `<div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${escHtml(c.description)}</div>` : ''}
            </div>
            <span class="${c.enabled ? 'pill pill-neutral' : 'pill'}" style="${c.enabled ? '' : 'opacity:0.45'}">${c.enabled ? 'enabled' : 'disabled'}</span>
          </div>
        `).join('')}
      </div>
    `;

    body.querySelectorAll('.ku-kc-check').forEach(cb => {
      cb.addEventListener('change', () => onConnectorToggle(cb.dataset.id, cb.checked));
    });
  } catch (e) {
    body.innerHTML = `<div style="color:var(--error)">${escHtml(e.message || String(e))}</div>`;
  }
}

async function onConnectorToggle(serverId, enabled) {
  const row = document.querySelector(`.ku-kc-row[data-id="${serverId}"]`);
  const pill = row?.querySelector('span.ku-kc-pill-on, span.ku-kc-pill-off');
  if (pill) { pill.textContent = '…'; pill.className = 'ku-kc-pill-off'; }
  try {
    await put(`/api/knowledge/connectors/${serverId}`, { knowledge_enabled: enabled });
    if (pill) {
      pill.textContent = enabled ? 'In Harness' : 'Off';
      pill.className = enabled ? 'ku-kc-pill-on' : 'ku-kc-pill-off';
    }
    const checks = document.querySelectorAll('.ku-kc-check');
    const on = [...checks].filter(c => c.checked).length;
    const badge = document.getElementById('ku-connectors-badge');
    if (badge) badge.textContent = `${checks.length} servers · ${on} in Harness`;
    toast.success(enabled ? 'Connector added to Harness' : 'Connector removed from Harness');
  } catch (e) {
    if (pill) {
      pill.textContent = enabled ? 'Off' : 'In Harness';
      pill.className = enabled ? 'ku-kc-pill-off' : 'ku-kc-pill-on';
      const cb = row?.querySelector('.ku-kc-check');
      if (cb) cb.checked = !enabled;
    }
    toast.error('Update failed: ' + (e.message || e));
  }
}

// ── Instructions (baseline constitution) ─────────────────────────────────────

async function renderInstructions() {
  _sectRendered.instructions = true;
  const body = document.getElementById('ku-instructions-body');
  if (!body) return;

  body.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div style="font-size:12px;color:var(--text-secondary)">
        This document is prepended to every agent's system prompt.
        Use H2 headings as override anchors.
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span id="ku-ki-saved" style="font-size:12px;color:var(--text-dim);opacity:0;transition:opacity 0.3s">Saved</span>
        <button class="btn btn-sm btn-primary" id="ku-ki-save" disabled>Save</button>
      </div>
    </div>
    <div id="ku-ki-disabled" style="display:none">
      <div class="card" style="padding:20px;text-align:center;color:var(--text-secondary)">
        <p style="margin:0 0 8px;font-weight:600">Constitution disabled</p>
        <p style="margin:0;font-size:13px">Set <code>AGD_CONSTITUTION_ENABLED=true</code> to enable.</p>
      </div>
    </div>
    <div id="ku-ki-main">
      <div style="display:flex;gap:12px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <label style="font-size:12px;color:var(--text-secondary);white-space:nowrap">Overrideable sections:</label>
        <input id="ku-ki-sections" type="text" placeholder="tone, tools"
               style="flex:1;min-width:200px;padding:6px 10px;font-family:var(--font-mono);font-size:12px;background:var(--bg-input);border:1px solid var(--border-mid);border-radius:6px;color:var(--text-primary);outline:none" />
      </div>
      <textarea id="ku-ki-editor" spellcheck="false"
        style="width:100%;min-height:320px;padding:14px;font-family:var(--font-mono);font-size:13px;line-height:1.65;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-dim);resize:vertical;outline:none;border-radius:var(--radius);box-sizing:border-box"
        placeholder="Loading..."></textarea>
      <div style="margin-top:8px;color:var(--text-dim);font-size:11px;display:flex;gap:16px">
        <span id="ku-ki-chars"></span>
        <span>Saved to <code>data/baseline/baseline.md</code> on the server</span>
      </div>
    </div>
  `;

  const editor = body.querySelector('#ku-ki-editor');
  const saveBtn = body.querySelector('#ku-ki-save');
  const sectionsInput = body.querySelector('#ku-ki-sections');
  const charCount = body.querySelector('#ku-ki-chars');

  function markDirty() {
    _constitutionDirty = true;
    saveBtn.disabled = false;
  }
  editor.addEventListener('input', () => {
    markDirty();
    const bytes = new TextEncoder().encode(editor.value).length;
    if (charCount) charCount.textContent = `${bytes} bytes`;
  });
  sectionsInput.addEventListener('input', markDirty);

  saveBtn.addEventListener('click', () => saveConstitution(body));
  editor.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 's') {
      e.preventDefault();
      if (!saveBtn.disabled) saveConstitution(body);
    }
  });

  await loadConstitution(body);
}

async function loadConstitution(body) {
  const editor = body.querySelector('#ku-ki-editor');
  const sectionsInput = body.querySelector('#ku-ki-sections');
  const disabledEl = body.querySelector('#ku-ki-disabled');
  const mainEl = body.querySelector('#ku-ki-main');
  const charCount = body.querySelector('#ku-ki-chars');
  const saveBtn = body.querySelector('#ku-ki-save');

  try {
    const resp = await fetch('/api/assistant/baseline');
    if (resp.status === 503) {
      disabledEl.style.display = '';
      mainEl.style.display = 'none';
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _constitutionVersion = data.version;
    _constitutionContent = data.content || '';
    _constitutionDirty = false;
    editor.value = _constitutionContent;
    sectionsInput.value = (data.overrideable_sections || []).join(', ');
    const bytes = new TextEncoder().encode(_constitutionContent).length;
    if (charCount) charCount.textContent = `${bytes} bytes`;
    if (saveBtn) saveBtn.disabled = true;
  } catch (e) {
    if (editor) editor.value = '';
    toast.error('Failed to load constitution: ' + (e.message || e));
  }
}

async function saveConstitution(body) {
  if (_constitutionVersion === null) { toast.error('Version not loaded yet — reload.'); return; }
  const editor = body.querySelector('#ku-ki-editor');
  const sectionsInput = body.querySelector('#ku-ki-sections');
  const saveBtn = body.querySelector('#ku-ki-save');
  const indicator = body.querySelector('#ku-ki-saved');
  const charCount = body.querySelector('#ku-ki-chars');
  const content = editor.value;
  const overrideable_sections = sectionsInput.value.split(',').map(s => s.trim()).filter(Boolean);

  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';

  try {
    const resp = await fetch('/api/assistant/baseline', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_version: _constitutionVersion, overrideable_sections, content }),
    });
    if (resp.status === 409) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; toast.error('Constitution modified elsewhere — reload to merge.'); return; }
    if (resp.status === 413) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; toast.error('Constitution too large (max 64 KiB).'); return; }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _constitutionVersion = data.version;
    _constitutionContent = content;
    _constitutionDirty = false;
    saveBtn.textContent = 'Save';
    if (charCount) charCount.textContent = `${new TextEncoder().encode(content).length} bytes`;
    if (indicator) { indicator.style.opacity = '1'; setTimeout(() => { indicator.style.opacity = '0'; }, 2000); }
    toast.success(`Constitution saved (v${data.version})`);
  } catch (e) {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save';
    toast.error('Save failed: ' + (e.message || e));
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function escHtml(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function escAttr(s) {
  return String(s || '').replace(/"/g, '&quot;');
}
