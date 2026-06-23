/**
 * Secrets view -- single-pane local encrypted store.
 *
 * AgeniusDesk's own Fernet-encrypted secrets.json.
 * Reference secrets as $NAME (or $NAME.field for compound) in API key fields.
 * Supports n8n credential mirroring and per-instance scope gating.
 */

import { del, get, post, put } from '../api.js';
import * as toast from '../components/toast.js';
import { invalidateRefsCache } from '../components/secretfield.js';

// ── Local store state ─────────────────────────────────────────────────────────
let _instances = [];
let _typesByInstance = {};
let _mirrorsByInstance = {};
let _templates = {};

function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : String(s); return el.innerHTML; }

function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

// ═════════════════════════════════════════════════════════════════════════════
// render()
// ═════════════════════════════════════════════════════════════════════════════

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">Encrypted Secrets</h2>
      <p style="font-size:12px;color:var(--text-secondary);margin-top:4px">
        Manage credentials in the local encrypted store.
        Reference secrets as <code>$NAME</code> or <code>$NAME.field</code> in API key fields.
      </p>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Stored Secrets</span>
        </div>
        <p style="font-size:12px;color:var(--text-secondary);margin-bottom:12px">
          Encrypted at rest. Reference as <code>$NAME</code> or <code>$NAME.field</code> in API key fields.
        </p>
        <div id="secrets-list"><div class="spinner"></div></div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="card-title">Add Secret</span>
        </div>
        <form id="add-secret-form">
          <label>
            Name
            <input type="text" id="secret-name" placeholder="e.g. N8N_PROD_KEY" required
                   pattern="[A-Za-z_][A-Za-z0-9_]*" title="Letters, numbers, underscores. No spaces.">
            <small>Becomes <code>$NAME</code>. Compound fields addressable as <code>$NAME.fieldName</code>.</small>
          </label>
          <label>
            Type
            <select id="secret-type" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:6px 8px;color:var(--text-primary)">
              <option value="api_key">API Key / Token</option>
            </select>
            <small id="secret-type-hint" style="color:var(--text-dim)"></small>
          </label>
          <div id="secret-fields"></div>
          <button type="submit" class="btn btn-primary">Save Secret</button>
        </form>
      </div>
    </div>
  `;

  // ── Local store init ───────────────────────────────────────────────────────
  try {
    const data = await get('/api/admin/secret-templates');
    _templates = data.templates || {};
  } catch {
    _templates = {};
  }
  populateTypePicker();
  renderTypeFields('api_key');
  document.getElementById('secret-type').addEventListener('change', (e) => renderTypeFields(e.target.value));
  document.getElementById('add-secret-form').addEventListener('submit', handleSubmit);
  loadSecrets();
}


// ═════════════════════════════════════════════════════════════════════════════
// LOCAL STORE
// ═════════════════════════════════════════════════════════════════════════════

function populateTypePicker() {
  const sel = document.getElementById('secret-type');
  if (!sel) return;
  const preferredOrder = ['api_key', 'oauth2_client', 'connectwise_manage', 'aws', 'azure_ad', 'custom'];
  const keys = Object.keys(_templates);
  keys.sort((a, b) => {
    const ai = preferredOrder.indexOf(a);
    const bi = preferredOrder.indexOf(b);
    if (ai === -1 && bi === -1) return a.localeCompare(b);
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  });
  sel.innerHTML = keys.map(k => `<option value="${esc(k)}">${esc(_templates[k].label || k)}</option>`).join('')
    || '<option value="api_key">API Key / Token</option>';
}

function renderTypeFields(type) {
  const host = document.getElementById('secret-fields');
  const hint = document.getElementById('secret-type-hint');
  if (!host) return;
  const tpl = _templates[type];
  hint.textContent = tpl && tpl.description ? tpl.description : '';

  if (!tpl || type === 'api_key') {
    host.innerHTML = `
      <label>
        Value
        <input type="password" id="sf-value" class="secret-field-input" data-field="value"
               placeholder="Paste your API key or token" required>
      </label>
    `;
    return;
  }

  if (type === 'custom') {
    host.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:6px" id="custom-field-rows"></div>
      <button type="button" id="custom-field-add" class="btn btn-sm btn-ghost" style="margin-top:6px">+ Add field</button>
    `;
    const addRow = () => {
      const row = document.createElement('div');
      row.style.cssText = 'display:grid;grid-template-columns:1fr 2fr auto;gap:6px;align-items:center';
      row.innerHTML = `
        <input type="text" class="custom-field-name" placeholder="field name"
               style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:4px 6px;color:var(--text-primary);font-size:12px">
        <input type="password" class="custom-field-value" placeholder="value"
               style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:4px 6px;color:var(--text-primary);font-size:12px">
        <button type="button" class="btn btn-sm btn-ghost custom-field-remove" aria-label="Remove field">&times;</button>
      `;
      row.querySelector('.custom-field-remove').addEventListener('click', () => row.remove());
      document.getElementById('custom-field-rows').appendChild(row);
    };
    document.getElementById('custom-field-add').addEventListener('click', addRow);
    addRow();
    return;
  }

  const fields = tpl.fields || [];
  host.innerHTML = fields.map(f => {
    const input_type = f.secret ? 'password' : 'text';
    const required = f.secret ? 'required' : '';
    const def = f.default != null ? esc(f.default) : '';
    const secretBadge = f.secret
      ? '<span style="font-size:10px;color:var(--text-dim);margin-left:6px">encrypted</span>'
      : '';
    return `
      <label>
        ${esc(f.label || f.name)} ${secretBadge}
        <input type="${input_type}" class="secret-field-input" data-field="${esc(f.name)}"
               placeholder="${esc(f.label || f.name)}" value="${def}" ${required}>
      </label>
    `;
  }).join('');
}

async function handleSubmit(e) {
  e.preventDefault();
  const name = document.getElementById('secret-name').value.trim().toUpperCase().replace(/[^A-Z0-9_]/g, '_');
  const type = document.getElementById('secret-type').value;

  let payload;
  if (type === 'api_key') {
    const value = (document.querySelector('.secret-field-input[data-field="value"]')?.value || '').trim();
    if (!value) { toast.error('Value is required'); return; }
    payload = { name, value };
  } else if (type === 'custom') {
    const rows = document.querySelectorAll('#custom-field-rows > div');
    const fields = {};
    rows.forEach((row) => {
      const key = (row.querySelector('.custom-field-name')?.value || '').trim();
      const val = row.querySelector('.custom-field-value')?.value || '';
      if (key && val) fields[key] = val;
    });
    if (!Object.keys(fields).length) { toast.error('Add at least one field'); return; }
    payload = { name, type: 'custom', fields };
  } else {
    const inputs = document.querySelectorAll('.secret-field-input');
    const fields = {};
    inputs.forEach((inp) => {
      const k = inp.dataset.field;
      const v = inp.value;
      if (k && v !== '') fields[k] = v;
    });
    if (!Object.keys(fields).length) { toast.error('Fill at least one field'); return; }
    payload = { name, type, fields };
  }

  try {
    await post('/api/admin/secrets', payload);
    invalidateRefsCache();
    toast.success(`Secret "$${name}" saved.`);
    document.getElementById('add-secret-form').reset();
    document.getElementById('secret-type').value = 'api_key';
    renderTypeFields('api_key');
    loadSecrets();
  } catch (err) {
    toast.error(err.message);
  }
}

async function loadSecrets() {
  const el = document.getElementById('secrets-list');
  if (!el) return;
  try {
    const [secretsData, instancesData] = await Promise.all([
      get('/api/admin/secrets'),
      get('/api/n8n/instances').catch(() => ({ instances: [] })),
    ]);
    const secrets = secretsData.secrets || [];
    _instances = instancesData.instances || [];

    _typesByInstance = {};
    _mirrorsByInstance = {};
    await Promise.all(_instances.map(async (inst) => {
      const [typesRes, mapRes] = await Promise.all([
        get(`/api/n8n-credentials/${inst.id}/mappings`).catch(() => ({ types: [] })),
        get(`/api/n8n-credentials/${inst.id}/mapped`).catch(() => ({ mirrors: {} })),
      ]);
      _typesByInstance[inst.id] = typesRes.types || [];
      _mirrorsByInstance[inst.id] = mapRes.mirrors || {};
    }));

    if (!secrets.length) {
      el.innerHTML = '<div class="empty-state"><p>No secrets stored yet. Add one to get started.</p></div>';
      return;
    }

    el.innerHTML = secrets.map(s => renderSecretRow(s)).join('');
    wireRowHandlers(el);
    wireSyncHandlers(el);
  } catch {
    el.innerHTML = '<div class="empty-state"><p>Could not load secrets</p></div>';
  }
}

function renderSecretRow(s) {
  const canSync = _instances.some(inst => (_typesByInstance[inst.id] || []).length > 0);
  const isCompound = s.kind === 'compound';
  const typeBadge = isCompound
    ? `<span class="secret-type-badge" style="font-size:10px;padding:2px 6px;border-radius:var(--radius);background:var(--bg-input);color:var(--text-dim);font-family:var(--font-mono)">${esc(s.type_label || s.type || 'compound')}</span>`
    : '';
  const hintLine = isCompound
    ? `<span style="font-size:11px;color:var(--text-dim)">${(s.fields || []).length} fields</span>`
    : `<span style="font-size:11px;color:var(--text-dim);font-family:var(--font-mono)">${esc(s.hint || '')}</span>`;
  const expandBtn = isCompound
    ? `<button class="btn btn-sm btn-ghost secret-expand-toggle" data-name="${esc(s.name)}" title="Show fields">▾</button>`
    : '';
  // Scope editor UI is hidden pre-beta: scopes are only consulted by the n8n
  // credential mirror in n8n_credentials/router.py, not by general secret
  // resolution in _resolve_secret_ref. Surfacing an editor here over-promises
  // a security boundary the resolver does not enforce. See code-review-2026-04-23
  // (P0-2). Backend PUT /secrets/{name}/scope stays available for the mirror.
  const scopeRow = '';

  return `
    <div class="secret-block" data-secret="${esc(s.name)}" style="border-bottom:1px solid var(--border-dim)">
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0">
        <code style="flex:1;font-size:13px">$${esc(s.name)}</code>
        ${typeBadge}
        ${hintLine}
        ${expandBtn}
        <button class="btn btn-sm btn-ghost" onclick="window.__copyRef('${jsStr(s.name)}', this)" title="Copy $${esc(s.name)} to clipboard">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
          Copy
        </button>
        ${canSync ? `<button class="btn btn-sm btn-ghost secret-sync-toggle" data-name="${esc(s.name)}" title="Mirror this secret into an n8n instance as a typed credential">Sync to n8n</button>` : ''}
        <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__deleteSecretStandalone('${jsStr(s.name)}')">Remove</button>
      </div>
      ${scopeRow}
      ${isCompound ? renderCompoundFields(s) : ''}
      ${canSync ? renderSyncPanel(s) : ''}
    </div>
  `;
}

function renderScopeEditor(s) {
  const allowed = Array.isArray(s.allowed_instances) ? s.allowed_instances : [];
  const appliesAll = allowed.length === 0;
  const chips = appliesAll
    ? `<span class="scope-pill-all" style="font-size:10px;padding:2px 6px;border-radius:var(--radius);background:var(--bg-input);color:var(--text-dim)">All instances</span>`
    : allowed.map((id) => {
        const inst = _instances.find((i) => i.id === id);
        const label = inst ? inst.name || inst.id : id;
        return `
          <span class="scope-chip" data-instance="${esc(id)}" style="display:inline-flex;align-items:center;gap:4px;font-size:10px;padding:2px 6px;border-radius:var(--radius);background:var(--bg-input);color:var(--text-primary)">
            ${esc(label)}
            <button class="scope-chip-remove" data-secret="${esc(s.name)}" data-instance="${esc(id)}" aria-label="Remove"
                    style="background:none;border:none;color:var(--text-dim);cursor:pointer;padding:0;font-size:12px;line-height:1">x</button>
          </span>
        `;
      }).join('');
  const available = _instances.filter((inst) => !allowed.includes(inst.id));
  const addMenu = available.length
    ? `<select class="scope-chip-add" data-secret="${esc(s.name)}" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:2px 6px;color:var(--text-dim);font-size:11px">
        <option value="">+ Add instance</option>
        ${available.map((inst) => `<option value="${esc(inst.id)}">${esc(inst.name || inst.id)}</option>`).join('')}
      </select>`
    : '';
  return `
    <div class="secret-scope" data-secret="${esc(s.name)}" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:4px 0 8px 0;font-size:11px">
      <span style="color:var(--text-dim);margin-right:4px">Applies to:</span>
      ${chips}
      ${addMenu}
    </div>
  `;
}

function renderCompoundFields(s) {
  const rows = (s.fields || []).map(f => `
    <div style="display:grid;grid-template-columns:minmax(120px,200px) minmax(100px,180px) auto 1fr;gap:8px;padding:4px 0;align-items:center;font-size:12px">
      <code style="font-size:12px;color:var(--text-secondary)">$${esc(s.name)}.${esc(f.name)}</code>
      <span style="font-size:11px;color:var(--text-dim)">${esc(f.label || f.name)}</span>
      <span style="font-size:11px;color:var(--text-dim);font-family:var(--font-mono)">${esc(f.hint || '')}</span>
      <button class="btn btn-sm btn-ghost secret-field-copy" data-ref="$${esc(s.name)}.${esc(f.name)}" title="Copy $${esc(s.name)}.${esc(f.name)}">Copy</button>
    </div>
  `).join('');
  return `
    <div class="secret-fields-panel" data-name="${esc(s.name)}" hidden style="padding:8px 12px;background:var(--bg-void);border-left:2px solid var(--accent-alt, var(--accent));margin:0 0 8px">
      <div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">Reference individual fields with <code>$${esc(s.name)}.fieldName</code>.</div>
      ${rows}
    </div>
  `;
}

function wireRowHandlers(root) {
  root.querySelectorAll('.secret-expand-toggle').forEach((btn) => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.name;
      const panel = root.querySelector(`.secret-fields-panel[data-name="${CSS.escape(name)}"]`);
      if (!panel) return;
      panel.hidden = !panel.hidden;
      btn.textContent = panel.hidden ? '▾' : '▴';
    });
  });
  root.querySelectorAll('.secret-field-copy').forEach((btn) => {
    btn.addEventListener('click', () => {
      const ref = btn.dataset.ref || '';
      if (!ref) return;
      navigator.clipboard.writeText(ref).then(() => {
        const original = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = original; }, 1200);
      });
    });
  });
  root.querySelectorAll('.scope-chip-remove').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.secret;
      const instId = btn.dataset.instance;
      const current = await fetchScope(name);
      await updateScope(name, current.filter((id) => id !== instId));
      loadSecrets();
    });
  });
  root.querySelectorAll('.scope-chip-add').forEach((sel) => {
    sel.addEventListener('change', async () => {
      const name = sel.dataset.secret;
      const instId = sel.value;
      if (!instId) return;
      const current = await fetchScope(name);
      if (!current.includes(instId)) current.push(instId);
      await updateScope(name, current);
      loadSecrets();
    });
  });
}

async function fetchScope(name) {
  try {
    const r = await get(`/api/admin/secrets/${encodeURIComponent(name)}/scope`);
    return Array.isArray(r.allowed_instances) ? r.allowed_instances : [];
  } catch { return []; }
}

async function updateScope(name, allowed) {
  try {
    await put(`/api/admin/secrets/${encodeURIComponent(name)}/scope`, { allowed_instances: allowed });
  } catch (err) {
    toast.error(err.message || 'Failed to update scope');
  }
}

function renderSyncPanel(s) {
  const secretName = typeof s === 'string' ? s : s.name;
  const allowed = (s && Array.isArray(s.allowed_instances)) ? s.allowed_instances : [];
  const scopedInstances = allowed.length ? _instances.filter((inst) => allowed.includes(inst.id)) : _instances;
  if (!scopedInstances.length) {
    return `
      <div class="secret-sync-panel" data-name="${esc(secretName)}" hidden style="padding:10px 12px;background:var(--bg-void);border-left:2px solid var(--accent);margin:0 0 8px">
        <div style="font-size:11px;color:var(--text-dim)">No instances in this secret's scope. Add one via "Applies to" above to enable mirroring.</div>
      </div>
    `;
  }
  const rows = scopedInstances.map((inst) => {
    const types = _typesByInstance[inst.id] || [];
    const mirrored = (_mirrorsByInstance[inst.id] || {})[secretName] || null;
    const detected = mirrored?.credential_type || detectTypeIn(secretName, types);
    const statusLine = mirrored
      ? `<span style="color:var(--success);font-size:11px">✓ ${esc(mirrored.credential_name || mirrored.credential_id || 'mirrored')}</span>`
      : `<span style="color:var(--text-dim);font-size:11px">Not mirrored</span>`;
    return `
      <div class="secret-sync-row" data-instance="${esc(inst.id)}" data-secret="${esc(secretName)}"
           style="display:grid;grid-template-columns:minmax(120px,1fr) minmax(140px,1fr) 90px 1fr 80px;gap:8px;padding:6px 0;align-items:center;font-size:12px">
        <span style="font-family:var(--font-mono);color:var(--text-secondary);font-size:12px">${esc(inst.name || inst.id)}</span>
        <select class="secret-sync-type" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:4px 8px;color:var(--text-primary);font-size:11px">
          <option value="">(pick type)</option>
          ${types.map(t => `<option value="${esc(t.type)}" ${t.type === detected ? 'selected' : ''}>${esc(t.display_name)}</option>`).join('')}
        </select>
        <button class="btn btn-sm secret-sync-btn" style="font-size:11px;padding:4px 8px">${mirrored ? 'Re-mirror' : 'Mirror'}</button>
        <span class="secret-sync-status">${statusLine}</span>
        ${mirrored ? `<button class="btn btn-sm btn-ghost btn-danger secret-unlink-btn" style="font-size:11px;padding:4px 8px" title="Delete the mirrored credential in n8n and forget the mapping">Unlink</button>` : `<span></span>`}
      </div>
    `;
  }).join('');
  return `
    <div class="secret-sync-panel" data-name="${esc(secretName)}" hidden style="padding:10px 12px;background:var(--bg-void);border-left:2px solid var(--accent);margin:0 0 8px">
      <div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">Mirror <code>$${esc(secretName)}</code> into n8n as a typed credential. Pick the credential type per instance.</div>
      ${rows}
    </div>
  `;
}

function detectTypeIn(secretName, types) {
  const upper = (secretName || '').toUpperCase();
  for (const t of types) {
    for (const p of (t.name_patterns || [])) {
      if (upper.includes(p.toUpperCase())) return t.type;
    }
  }
  return '';
}

function wireSyncHandlers(root) {
  root.querySelectorAll('.secret-sync-toggle').forEach((btn) => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.name;
      const panel = root.querySelector(`.secret-sync-panel[data-name="${CSS.escape(name)}"]`);
      if (!panel) return;
      panel.hidden = !panel.hidden;
      btn.textContent = panel.hidden ? 'Sync to n8n' : 'Close';
    });
  });

  root.querySelectorAll('.secret-sync-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const row = btn.closest('.secret-sync-row');
      const instanceId = row.dataset.instance;
      const secretName = row.dataset.secret;
      const typeSel = row.querySelector('.secret-sync-type');
      const statusEl = row.querySelector('.secret-sync-status');
      const credType = typeSel.value;
      if (!credType) {
        statusEl.innerHTML = `<span style="color:var(--warning);font-size:11px">Pick a credential type first</span>`;
        return;
      }
      btn.disabled = true;
      const prevLabel = btn.textContent;
      btn.textContent = 'Mirroring...';
      statusEl.innerHTML = `<span style="color:var(--text-dim);font-size:11px">Working...</span>`;
      try {
        const r = await post(`/api/n8n-credentials/${instanceId}/mirror`, {
          items: [{ secret_name: secretName, credential_type: credType, skip: false }],
        });
        const result = (r.results || [])[0] || { status: 'error', error: 'No result' };
        if (result.status === 'ok') {
          statusEl.innerHTML = `<span style="color:var(--success);font-size:11px">✓ ${esc(result.credential_name || result.credential_id || 'mirrored')}</span>`;
          btn.textContent = 'Re-mirror';
          _mirrorsByInstance[instanceId] = _mirrorsByInstance[instanceId] || {};
          _mirrorsByInstance[instanceId][secretName] = {
            credential_id: result.credential_id,
            credential_name: result.credential_name,
            credential_type: result.credential_type,
          };
          if (!row.querySelector('.secret-unlink-btn')) {
            const placeholder = row.lastElementChild;
            if (placeholder && placeholder.tagName === 'SPAN') {
              const unlink = document.createElement('button');
              unlink.className = 'btn btn-sm btn-ghost btn-danger secret-unlink-btn';
              unlink.style.cssText = 'font-size:11px;padding:4px 8px';
              unlink.title = 'Delete the mirrored credential in n8n and forget the mapping';
              unlink.textContent = 'Unlink';
              unlink.addEventListener('click', () => handleUnlink(row));
              placeholder.replaceWith(unlink);
            }
          }
        } else {
          const err = (result.error || 'failed').toString().slice(0, 200);
          statusEl.innerHTML = `<span style="color:var(--error);font-size:11px">✗ ${esc(err)}</span>`;
          btn.textContent = prevLabel;
        }
      } catch (e) {
        statusEl.innerHTML = `<span style="color:var(--error);font-size:11px">✗ ${esc(e.message)}</span>`;
        btn.textContent = prevLabel;
      } finally {
        btn.disabled = false;
      }
    });
  });

  root.querySelectorAll('.secret-unlink-btn').forEach((btn) => {
    btn.addEventListener('click', () => handleUnlink(btn.closest('.secret-sync-row')));
  });
}

async function handleUnlink(row) {
  const instanceId = row.dataset.instance;
  const secretName = row.dataset.secret;
  const statusEl = row.querySelector('.secret-sync-status');
  const mirrored = (_mirrorsByInstance[instanceId] || {})[secretName];
  if (!mirrored) return;
  if (!confirm(`Delete the n8n credential mirrored from $${secretName} on this instance?`)) return;
  try {
    const r = await del(`/api/n8n-credentials/${instanceId}/${secretName}`);
    delete _mirrorsByInstance[instanceId][secretName];
    statusEl.innerHTML = `<span style="color:var(--text-dim);font-size:11px">Not mirrored</span>`;
    const btn = row.querySelector('.secret-sync-btn');
    if (btn) btn.textContent = 'Mirror';
    const unlinkBtn = row.querySelector('.secret-unlink-btn');
    if (unlinkBtn) unlinkBtn.replaceWith(document.createElement('span'));
    if (r && r.n8n_error) {
      toast.error(`Mapping cleared. n8n delete failed: ${r.n8n_error.slice(0, 120)}`);
    } else {
      toast.success('Unlinked');
    }
  } catch (e) {
    statusEl.innerHTML = `<span style="color:var(--error);font-size:11px">Unlink failed: ${esc(e.message)}</span>`;
  }
}

window.__deleteSecretStandalone = async (name) => {
  if (!confirm(`Delete secret "$${name}"? Anything using it will stop working.`)) return;
  try {
    await del(`/api/admin/secrets/${name}`);
    invalidateRefsCache();
    toast.success(`Deleted "$${name}"`);
    loadSecrets();
  } catch (err) {
    toast.error(err.message);
  }
};

if (typeof window.__copyRef !== 'function') {
  window.__copyRef = (name, btn) => {
    navigator.clipboard.writeText(`$${name}`).then(() => {
      const original = btn.innerHTML;
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--success)" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Copied';
      setTimeout(() => { btn.innerHTML = original; }, 1500);
    });
  };
}
