/**
 * Settings view — instance management, secrets store, themes, error handler.
 */

import { get, post, del } from '../api.js';
import * as toast from '../components/toast.js';
import { setActiveTheme, getCurrentTheme } from '../themes.js';
import { secretField, invalidateRefsCache } from '../components/secretfield.js';
import { renderModules } from './settings-modules.js';
import { openModal } from '../components/modal.js';
import { renderQR } from '../vendor/qrcode.js';
import { mountChecklist } from '../components/password-policy.js';
import { getErrorLookback, setErrorLookback, lookbackOptionsHtml } from '../error-prefs.js';

const COLORS = ['#ff6d5a', '#60a5fa', '#34d399', '#fbbf24', '#a78bfa', '#f472b6', '#38bdf8', '#fb923c'];

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">Settings</h2>
    </div>

    <!-- Tabs -->
    <div style="display:flex;gap:2px;margin-bottom:20px;border-bottom:1px solid var(--border-dim);flex-wrap:wrap">
      <button class="tab-btn active" data-tab="instances" onclick="window.__settingsTab('instances')">Instances</button>
      <button class="tab-btn" data-tab="account" onclick="window.__settingsTab('account')">Account</button>
      <button class="tab-btn" data-tab="mcp" onclick="window.__settingsTab('mcp')">MCP</button>
      <button class="tab-btn" data-tab="assistant" onclick="window.__settingsTab('assistant')">AI Settings</button>
      <button class="tab-btn" data-tab="secrets" onclick="window.__settingsTab('secrets')">Secrets</button>
      <button class="tab-btn" data-tab="themes" onclick="window.__settingsTab('themes')">Themes</button>
      <button class="tab-btn" data-tab="error-handler" onclick="window.__settingsTab('error-handler')">Error Handler</button>
      <button class="tab-btn" data-tab="modules" onclick="window.__settingsTab('modules')">Modules</button>
      <button class="tab-btn" data-tab="help" onclick="window.__settingsTab('help')">Help &amp; Tips</button>
    </div>

    <div id="settings-tab-content"></div>
  `;

  window.__settingsTab = switchTab;
  switchTab('instances');
}

function switchTab(tab) {
  // Music Player settings moved to dedicated "Your Vibe" page
  if (tab === 'music') {
    if (window.__nav) window.__nav('music');
    return;
  }
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  const el = document.getElementById('settings-tab-content');
  if (tab === 'instances') renderInstances(el);
  else if (tab === 'account') renderAccount(el);
  else if (tab === 'mcp') renderMCP(el);
  else if (tab === 'assistant') renderModelsTab(el);
  else if (tab === 'secrets') renderSecrets(el);
  else if (tab === 'themes') renderThemes(el);
  else if (tab === 'error-handler') renderErrorHandler(el);
  else if (tab === 'modules') renderModules(el);
  else if (tab === 'help') renderHelp(el);
}

// ── Help & Tips ───────────────────────────────────────────────────────────────

function renderHelp(el) {
  const tipsOn = window.__tipsEnabled ? window.__tipsEnabled() : true;
  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header"><span class="card-title">Page tips</span></div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:14px">
        Short coachmarks appear the first time you open each view, pointing out the
        key controls. Manage them here.
      </p>
      <label style="display:flex;align-items:center;gap:10px;font-size:13px;cursor:pointer;margin-bottom:16px">
        <input type="checkbox" id="help-tips-toggle" ${tipsOn ? 'checked' : ''}
               style="width:15px;height:15px;accent-color:var(--accent)">
        Show page tips
      </label>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-sm" id="help-replay">Replay tips on this page</button>
        <button class="btn btn-sm btn-ghost" id="help-reset">Reset all tips</button>
      </div>
      <p style="font-size:11px;color:var(--text-dim);margin-top:10px">
        "Replay" opens the tour for the last view you visited before Settings.
      </p>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">Setup</span></div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:14px">
        Reopen the guided setup if you dismissed it or want to revisit a step.
      </p>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-sm" id="help-checklist">Reopen setup checklist</button>
        <button class="btn btn-sm btn-ghost" id="help-wizard">Reopen setup wizard</button>
      </div>
    </div>`;

  el.querySelector('#help-tips-toggle')?.addEventListener('change', (e) => {
    if (window.__setTipsEnabled) window.__setTipsEnabled(e.target.checked);
  });
  el.querySelector('#help-replay')?.addEventListener('click', () => {
    // window.__currentView is "settings" right now, so target the prior view.
    const prior = window.__priorView && window.__priorView !== 'settings' ? window.__priorView : 'settings';
    if (window.__replayTour) window.__replayTour(prior);
    if (prior !== 'settings' && window.__nav) window.__nav(prior);
  });
  el.querySelector('#help-reset')?.addEventListener('click', () => {
    if (window.__resetTips) window.__resetTips();
    toast.success('All tips reset. They will reappear as you visit views.');
  });
  el.querySelector('#help-checklist')?.addEventListener('click', () => {
    try { localStorage.removeItem('agd_getstarted_dismissed'); } catch { /* ignore */ }
    if (window.__nav) window.__nav('dashboard');
  });
  el.querySelector('#help-wizard')?.addEventListener('click', () => {
    if (window.__openWizard) window.__openWizard();
  });
}

// ── Instances ───────────────────────────────────────────────────────────────

export async function renderInstances(el) {
  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">n8n Instances</span>
        <div id="inst-add-area" style="display:flex;align-items:center;gap:8px"></div>
      </div>
      <p style="font-size:12px;color:var(--text-secondary);margin-bottom:12px">
        Manage any n8n instance — self-hosted, DigitalOcean, Hostinger, Railway, Render, Hetzner, Coolify, or n8n Cloud. Just provide the URL and an API key.
      </p>
      <div id="inst-list"><div class="spinner"></div></div>
    </div>
  `;
  loadInstances();
}

async function loadInstances() {
  const el = document.getElementById('inst-list');
  const addArea = document.getElementById('inst-add-area');
  if (!el) return;
  try {
    // Fetch instances, containers, and host-aliases in parallel. Container and
    // alias fetches are best-effort (Docker may be unavailable).
    const [data, containersData, aliasData] = await Promise.all([
      get('/api/n8n/instances'),
      get('/api/containers?all=true').catch(() => ({ containers: [] })),
      get('/api/containers/host-aliases').catch(() => ({ aliases: [] })),
    ]);
    const instances = data.instances || [];
    const containers = containersData.containers || [];
    const hostAliases = new Set((aliasData.aliases || []).map(a => a.toLowerCase()));
    // Render add button / instance count
    if (addArea) {
      addArea.innerHTML = `
        <span style="font-size:11px;color:var(--text-dim)">${instances.length} instances</span>
        <button class="btn btn-sm btn-primary" onclick="window.__addInstance()">+ Add Instance</button>
      `;
    }

    if (!instances.length) {
      el.innerHTML = '<div class="empty-state"><p>No instances connected yet. Click <strong>+ Add Instance</strong> to get started.</p></div>';
      return;
    }

    el.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th></th><th>Name</th><th>URL</th><th>API Key</th><th></th></tr></thead>
      <tbody>${instances.map(inst => {
        const matched = _matchContainer(inst, containers, hostAliases);
        const containerId = matched ? matched.id_full : null;
        const updateCell = containerId
          ? `<button class="btn btn-sm btn-ghost" onclick="window.__instUpdate('${jsStr(inst.id)}','${jsStr(inst.name)}','${jsStr(containerId)}')">Update</button>`
          : `<span class="pill pill-neutral" style="font-size:9px" title="No managed n8n container matched this instance's URL. If this n8n runs on the same host as the dashboard, set AGD_HOST_ALIASES to this host's LAN IP or hostname (the host shown in the URL above), then recreate the dashboard to enable one-click updates.">Not auto-updateable</span>`;
        return `
        <tr id="inst-row-${esc(inst.id)}">
          <td>
            <span class="instance-dot" style="background:${attr(inst.color || '#ff6d5a')};cursor:pointer" onclick="window.__activateInst('${jsStr(inst.id)}')" title="${inst.active ? 'Active' : 'Click to switch'}"></span>
          </td>
          <td style="font-weight:500">
            ${esc(inst.name)}
            ${inst.active ? '<span class="pill pill-success" style="font-size:9px;margin-left:4px">ACTIVE</span>' : ''}
          </td>
          <td style="font-family:var(--font-mono);font-size:12px">${esc(inst.url)}</td>
          <td style="font-size:12px;color:var(--text-dim)">${esc(inst.key_hint || 'configured')}</td>
          <td style="white-space:nowrap">
            ${updateCell}
            ${inst.has_login ? `<button class="btn btn-sm btn-ghost" onclick="window.__instLogin('${jsStr(inst.id)}','${jsStr(inst.name)}','${inst.color || ''}')" title="Show n8n sign-in details">Sign in to n8n</button>` : ''}
            <button class="btn btn-sm btn-ghost" onclick="window.__instCredentials('${jsStr(inst.id)}','${jsStr(inst.name)}','${jsStr(inst.url)}','${inst.color || ''}')">Credentials</button>
            <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__removeInst('${jsStr(inst.id)}','${jsStr(inst.name)}')">Remove</button>
          </td>
        </tr>
        <tr id="inst-prog-${esc(inst.id)}" style="display:none">
          <td colspan="5" style="padding:0">
            <div style="padding:10px 14px;background:rgba(96,165,250,0.05);border-top:1px solid var(--border-dim)">
              <div id="inst-prog-header-${esc(inst.id)}" style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                <span class="spinner" style="width:12px;height:12px;border-width:2px;margin:0;flex-shrink:0" id="inst-prog-spinner-${esc(inst.id)}"></span>
                <span style="font-size:11px;font-weight:600;color:var(--text-secondary)">Updating ${esc(inst.name)}...</span>
              </div>
              <div id="inst-prog-log-${esc(inst.id)}"
                   style="font-family:var(--font-mono);font-size:11px;color:var(--text-secondary);
                          max-height:80px;overflow-y:auto;display:flex;flex-direction:column;gap:2px">
              </div>
            </div>
          </td>
        </tr>`;
      }).join('')}</tbody>
    </table></div>`;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Failed to load: ${esc(e.message)}</p></div>`;
  }
}

// ── Container matching ───────────────────────────────────────────────────────

/**
 * Given an instance record, a list of containers from GET /api/containers,
 * and the set of host aliases from GET /api/containers/host-aliases,
 * return the first running container that appears to back this instance, or null.
 *
 * Matching strategy (first win):
 *  1. Instance URL hostname matches container's compose_service name and
 *     instance URL port matches a container-side port in the port bindings.
 *  2. Instance URL hostname is local (in hostAliases) AND a running container
 *     publishes the instance URL port on the host.
 *
 * "Local" means the hostname resolves to this Docker host — determined by the
 * /api/containers/host-aliases endpoint which includes loopback aliases,
 * host.docker.internal, and the Docker host's gateway/LAN IP.
 *
 * Restricted to containers whose image contains "n8n" or whose labels
 * mark them as an n8n deployment, so we don't accidentally match a Postgres
 * container that happens to share a port.
 *
 * Port string format from _normalize(): "5678→5678/tcp" (unicode arrow U+2192).
 */
function _matchContainer(inst, containers, hostAliases) {
  if (!inst || !containers || !containers.length) return null;

  const n8nContainers = containers.filter(c => {
    const labels = c.labels || {};
    if (labels['ageniusdesk.template'] === 'n8n') return true;
    if (labels['agd.type'] === 'n8n') return true;
    return (c.image || '').toLowerCase().includes('n8n');
  });
  if (!n8nContainers.length) return null;

  // Parse a URL safely, return {hostname, port} or null.
  const parseUrl = (raw) => {
    if (!raw) return null;
    try {
      const u = new URL(raw);
      const port = u.port || (u.protocol === 'https:' ? '443' : '80');
      return { hostname: u.hostname.toLowerCase(), port };
    } catch { return null; }
  };

  // Extract the published (host-side) port from a normalized port string.
  // Handles both "5678→5678/tcp" (unicode arrow) and legacy "5678->5678/tcp".
  const hostPort = (p) => {
    const m = p.match(/^(\d+)[→>-]/);
    return m ? m[1] : null;
  };

  // Extract the container-side (internal) port from a normalized port string.
  const containerPort = (p) => {
    const m = p.match(/\d+[→>-]+(\d+)/);
    return m ? m[1] : null;
  };

  const instUrl  = parseUrl(inst.url);
  const loginUrl = parseUrl(inst.login_url);

  for (const c of n8nContainers) {
    // Strategy 1: compose service name match + internal port match.
    // Only applies when the instance URL uses the service name as hostname.
    if (c.compose_service && instUrl && c.compose_service === instUrl.hostname) {
      const hasInternalPort = (c.ports || []).some(p => containerPort(p) === instUrl.port);
      if (hasInternalPort || !(c.ports || []).length) return c;
    }

    // Strategy 2: host port match — but only when the instance URL hostname
    // resolves to THIS Docker host (i.e. it's in the hostAliases set).
    // This prevents falsely matching a container when the instance points to a
    // different physical host than the one running this dashboard.
    const aliases = hostAliases instanceof Set ? hostAliases : new Set(hostAliases || []);
    for (const candidateUrl of [instUrl, loginUrl]) {
      if (!candidateUrl) continue;
      if (!aliases.has(candidateUrl.hostname)) continue;
      const hasHostPort = (c.ports || []).some(p => hostPort(p) === candidateUrl.port);
      if (hasHostPort) return c;
    }
  }

  return null;
}

// ── Instance update (n8n version push) ───────────────────────────────────────

window.__instUpdate = async (instId, name, containerId) => {
  const confirmed = await openModal({
    title: 'Update n8n instance',
    body: 'This will stop the container, pull the latest n8n image, and restart it. There will be brief downtime.',
    confirmLabel: 'Update',
    cancelLabel: 'Cancel',
    danger: true,
  });
  if (!confirmed) return;

  _instUpdateSSE(instId, name, containerId);
};

async function _instUpdateSSE(instId, name, containerId) {
  const progRow  = document.getElementById(`inst-prog-${instId}`);
  const logEl    = document.getElementById(`inst-prog-log-${instId}`);
  const spinner  = document.getElementById(`inst-prog-spinner-${instId}`);
  const headerEl = document.getElementById(`inst-prog-header-${instId}`);

  if (!progRow || !logEl) return;

  progRow.style.display = '';
  logEl.innerHTML = '';

  const appendLine = (text, color = '') => {
    const line = document.createElement('span');
    if (color) line.style.color = color;
    line.textContent = text;
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
  };

  const setStatus = (text) => {
    if (!headerEl) return;
    const statusSpan = headerEl.querySelector('span:last-child');
    if (statusSpan) statusSpan.textContent = text;
  };

  let deployId;
  try {
    const res = await post(`/api/containers/${encodeURIComponent(containerId)}/recreate`, {});
    deployId = res.deploy_id;
  } catch (err) {
    appendLine(`Update failed: ${err.message}`, '#ff6d5a');
    if (spinner) spinner.style.display = 'none';
    setStatus(`Update failed: ${name}`);
    return;
  }

  const es = new EventSource(`/api/containers/deploy/${deployId}/progress`);

  es.onmessage = (e) => {
    let item;
    try { item = JSON.parse(e.data); } catch { return; }
    if (item === null) { es.close(); return; }

    if (item.event === 'step') {
      appendLine(item.message);
      return;
    }

    if (item.event === 'done') {
      es.close();
      appendLine(`${name} updated and restarted.`, '#34d399');
      if (spinner) spinner.style.display = 'none';
      setStatus(`${name} updated successfully.`);
      setTimeout(() => loadInstances(), 2000);
      return;
    }

    if (item.event === 'error') {
      es.close();
      appendLine(item.message || 'Update failed.', '#ff6d5a');
      if (spinner) spinner.style.display = 'none';
      setStatus(`Update failed for ${name}.`);
    }
  };

  es.onerror = () => {
    appendLine('Lost connection to update stream.', '#ff6d5a');
    if (spinner) spinner.style.display = 'none';
    es.close();
  };
}

window.__activateInst = async (id) => {
  try {
    await post(`/api/n8n/instances/${id}/activate`);
    toast.success('Switched instance');
    loadInstances();
    if (window.__refreshInstances) window.__refreshInstances();
  } catch (e) { toast.error(e.message); }
};

window.__removeInst = async (id, name) => {
  if (!confirm(`Remove "${name}"? This won't affect the n8n server itself.`)) return;
  try {
    await del(`/api/n8n/instances/${id}`);
    toast.success(`Removed "${name}"`);
    loadInstances();
    if (window.__refreshInstances) window.__refreshInstances();
  } catch (e) { toast.error(e.message); }
};

// n8n sign-in modal — shows owner URL/email/password for instances that
// were provisioned with a known login. Fetches on demand so we never ship
// plaintext passwords in the /instances list response.
window.__instLogin = async (id, name, color) => {
  const existing = document.getElementById('login-modal');
  if (existing) existing.remove();

  let login;
  try {
    login = await get(`/api/n8n/instances/${id}/login`);
  } catch (e) {
    toast.error(e.message || 'Could not load login');
    return;
  }

  const modal = document.createElement('div');
  modal.id = 'login-modal';
  modal.className = 'modal';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = `
    <div class="modal-content" style="max-width:520px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="margin:0;display:flex;align-items:center;gap:8px">
          <span class="instance-dot" style="background:${attr(color || '#ff6d5a')}"></span>
          Sign in to ${esc(name)}
        </h2>
        <button class="btn btn-sm btn-ghost" onclick="this.closest('.modal').remove()" style="font-size:18px">&times;</button>
      </div>
      <p style="font-size:12px;color:var(--text-secondary);margin:0 0 14px">
        Open n8n in a new tab, then paste these credentials on the sign-in screen.
      </p>
      <div id="login-fields" style="display:grid;gap:10px"></div>
      <div style="display:flex;gap:8px;margin-top:16px">
        <a id="login-open" target="_blank" rel="noopener" class="btn btn-primary">Open n8n &rarr;</a>
        <button type="button" class="btn" onclick="this.closest('.modal').remove()">Close</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const openBtn = modal.querySelector('#login-open');
  openBtn.href = login.url;
  mountLoginField(modal.querySelector('#login-fields'), 'URL', login.url, { mono: true });
  mountLoginField(modal.querySelector('#login-fields'), 'Email', login.email, {});
  mountLoginField(modal.querySelector('#login-fields'), 'Password', login.password, { mask: true });
};

// Build one row of the login modal. Values are assigned via DOM properties
// (never interpolated into HTML) so quotes/angle brackets in passwords can
// never break out of their container.
function mountLoginField(parent, label, value, { mono = false, mask = false } = {}) {
  const wrap = document.createElement('div');
  const lab = document.createElement('label');
  lab.textContent = label;
  lab.style.cssText = 'font-size:11px;color:var(--text-secondary);display:block;margin-bottom:3px';

  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:6px';

  const input = document.createElement('input');
  input.type = mask ? 'password' : 'text';
  input.value = value;
  input.readOnly = true;
  input.style.cssText = `flex:1;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:8px 10px;color:var(--text-primary);font-size:13px;font-family:${mono ? 'var(--font-mono)' : 'inherit'}`;
  row.appendChild(input);

  if (mask) {
    const reveal = document.createElement('button');
    reveal.type = 'button';
    reveal.className = 'btn btn-sm btn-ghost';
    reveal.textContent = 'Show';
    reveal.addEventListener('click', () => {
      const shown = input.type === 'text';
      input.type = shown ? 'password' : 'text';
      reveal.textContent = shown ? 'Show' : 'Hide';
    });
    row.appendChild(reveal);
  }

  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'btn btn-sm';
  copy.textContent = 'Copy';
  copy.addEventListener('click', () => {
    navigator.clipboard.writeText(value).then(() => {
      toast.success(`Copied ${label.toLowerCase()}`);
    });
  });
  row.appendChild(copy);

  wrap.appendChild(lab);
  wrap.appendChild(row);
  parent.appendChild(wrap);
}

// Credentials modal for an instance
window.__instCredentials = (id, name, url, color) => {
  const existing = document.getElementById('cred-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'cred-modal';
  modal.className = 'modal';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = `
    <div class="modal-content" style="max-width:520px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="margin:0;display:flex;align-items:center;gap:8px">
          <span class="instance-dot" style="background:${attr(color || '#ff6d5a')}"></span>
          ${esc(name)} &ndash; Credentials
        </h2>
        <button class="btn btn-sm btn-ghost" onclick="this.closest('.modal').remove()" style="font-size:18px">&times;</button>
      </div>
      <form id="cred-form">
        <label>
          Instance Name
          <input type="text" id="cred-name" value="${attr(name)}">
        </label>
        <label>
          n8n URL
          <input type="url" id="cred-url" value="${attr(url)}">
        </label>
        <div style="margin-top:10px;margin-bottom:8px">
          <div id="cred-key-field"></div>
          <small style="display:block;margin-top:4px;color:var(--text-dim)">Leave blank to keep the existing key. Paste a raw key (it will be saved to the secrets store) or pick an existing <code>$SECRET_NAME</code>.</small>
        </div>
        <label>
          Color
          <div id="cred-color-picker" style="display:flex;gap:6px;margin-top:6px"></div>
          <input type="hidden" id="cred-color" value="${attr(color || '#ff6d5a')}">
        </label>
        <div style="display:flex;gap:8px">
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn" onclick="this.closest('.modal').remove()">Cancel</button>
          <button type="button" class="btn" id="cred-test-btn">Test Connection</button>
        </div>
        <p id="cred-status" style="margin-top:8px;font-size:12px"></p>
      </form>
    </div>
  `;
  document.body.appendChild(modal);

  // Credential field (SecretField)
  const credField = secretField({
    container: modal.querySelector('#cred-key-field'),
    label: 'API Key',
    prefix: 'N8N_KEY',
    context: name,
    initialValue: '',
    placeholder: 'Enter new key to update, or leave blank to keep current',
  });

  // Color picker
  const picker = document.getElementById('cred-color-picker');
  COLORS.forEach(c => {
    const s = document.createElement('div');
    s.style.cssText = `width:24px;height:24px;border-radius:6px;background:${c};cursor:pointer;border:2px solid ${c === (color || '#ff6d5a') ? '#fff' : 'transparent'};transition:border-color 0.15s`;
    s.addEventListener('click', () => {
      document.getElementById('cred-color').value = c;
      picker.querySelectorAll('div').forEach(d => d.style.borderColor = 'transparent');
      s.style.borderColor = '#fff';
    });
    picker.appendChild(s);
  });

  document.getElementById('cred-name').addEventListener('input', (e) => {
    credField.setContext(e.target.value.trim());
  });

  document.getElementById('cred-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    let api_key = credField.getValue();
    const newName = document.getElementById('cred-name').value.trim();

    // Promote raw key to secrets store if provided.
    if (api_key && !api_key.startsWith('$')) {
      try {
        const r = await post('/api/admin/secrets/promote', {
          value: api_key,
          prefix: 'N8N_KEY',
          context: newName,
        });
        if (r && r.ref) {
          api_key = r.ref;
          invalidateRefsCache();
        }
      } catch (err) {
        toast.error(`Could not save key to secrets: ${err.message}`);
        return;
      }
    }

    const updates = {
      name: newName,
      url: document.getElementById('cred-url').value.trim(),
      api_key,
      color: document.getElementById('cred-color').value,
    };
    try {
      const resp = await fetch(`/api/n8n/instances/${id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(updates)
      });
      if (!resp.ok) throw new Error('Update failed');
      toast.success(`Updated "${updates.name}"`);
      modal.remove();
      loadInstances();
      if (window.__refreshInstances) window.__refreshInstances();
    } catch (err) { toast.error(err.message); }
  });

  document.getElementById('cred-test-btn').addEventListener('click', async () => {
    const statusEl = document.getElementById('cred-status');
    statusEl.textContent = 'Testing...';
    statusEl.style.color = 'var(--text-secondary)';
    try {
      const result = await get('/api/n8n/test');
      statusEl.textContent = result.connected ? 'Connection successful!' : 'Connection failed';
      statusEl.style.color = result.connected ? 'var(--success)' : 'var(--error)';
    } catch (e) {
      statusEl.textContent = 'Connection failed: ' + e.message;
      statusEl.style.color = 'var(--error)';
    }
  });
};

// ── MCP Servers ─────────────────────────────────────────────────────────────

async function renderN8nMcpCard() {
  const host = document.getElementById('n8n-mcp-card');
  if (!host) return;
  let s;
  try {
    s = await get('/api/mcp/n8n-mcp/status');
  } catch {
    host.innerHTML = '';
    return;
  }
  const badge = (text, color) =>
    `<span class="badge" style="background:${color}22;color:${color};border:1px solid ${color}55;font-size:11px">${esc(text)}</span>`;
  let state = '';
  let action = '';
  if (s.registered && s.mode === 'full') {
    state = badge('Active · full', '#34d399');
    action = `<button class="btn btn-sm btn-ghost" id="n8nmcp-disable">Remove</button>`;
  } else if (s.registered) {
    state = badge('Active · docs', '#34d399');
    action = `<button class="btn btn-sm btn-primary" id="n8nmcp-upgrade">Wire to active instance</button>`
      + ` <button class="btn btn-sm btn-ghost" id="n8nmcp-disable">Remove</button>`;
  } else if (s.docker_available) {
    state = badge('Not installed', '#fbbf24');
    action = `<button class="btn btn-sm btn-primary" id="n8nmcp-enable">Enable</button>`;
  } else {
    state = badge('Docker unavailable', '#ff6d5a');
  }
  const runNote = s.registered
    ? (s.container_running ? ' Container healthy.' : ' Container not running — try Enable to restart it.')
    : '';
  host.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">n8n Intelligence (n8n-mcp)</span>
        ${state}
      </div>
      <p style="font-size:12px;color:var(--text-secondary);margin:6px 0 10px">
        A built-in MCP server giving the assistant and Code Lab real n8n node knowledge, search, and workflow
        validation. Docs-only by default (no n8n credentials); wire it to the active instance for workflow
        create/update tools.<span style="color:var(--text-dim)">${esc(runNote)}</span>
      </p>
      <div style="font-size:11px;color:var(--text-dim);margin-bottom:8px">
        Powered by <a href="https://github.com/czlonkowski/n8n-mcp" target="_blank" rel="noopener" style="color:var(--accent)">n8n-mcp</a> by czlonkowski (MIT).
      </div>
      ${(!s.docker_available && !s.registered)
        ? `<div style="font-size:11px;color:var(--text-dim);background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:8px 10px;margin-bottom:10px">Docker isn't reachable from the dashboard, so n8n-mcp can't auto-start. Run it yourself in HTTP mode and add it below, or give the dashboard Docker access and reload.</div>`
        : ''}
      ${action ? `<div style="display:flex;gap:8px;flex-wrap:wrap">${action}</div>` : ''}
      <div id="n8nmcp-result" style="font-size:12px;margin-top:8px"></div>
    </div>`;

  const result = document.getElementById('n8nmcp-result');
  const run = async (path, btn, busyText) => {
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = busyText;
    result.textContent = '';
    try {
      const r = await post(path, {});
      if (r.ok) {
        toast.success('n8n-mcp updated');
        renderN8nMcpCard();
      } else {
        result.innerHTML = `<span style="color:var(--error)">${esc(r.error || 'Failed')}</span>`;
        btn.disabled = false;
        btn.textContent = orig;
      }
    } catch (e) {
      result.innerHTML = `<span style="color:var(--error)">${esc(e.message)}</span>`;
      btn.disabled = false;
      btn.textContent = orig;
    }
  };
  document.getElementById('n8nmcp-enable')?.addEventListener('click', (e) => run('/api/mcp/n8n-mcp/enable', e.target, 'Starting…'));
  document.getElementById('n8nmcp-upgrade')?.addEventListener('click', (e) => run('/api/mcp/n8n-mcp/upgrade', e.target, 'Wiring…'));
  document.getElementById('n8nmcp-disable')?.addEventListener('click', (e) => run('/api/mcp/n8n-mcp/disable', e.target, 'Removing…'));
}


export async function renderMCP(el) {
  el.innerHTML = `
    <div id="n8n-mcp-card" style="margin-bottom:16px"></div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">MCP Servers</span>
        <button class="btn btn-sm btn-primary" id="add-mcp-btn">+ Add Server</button>
      </div>
      <p style="font-size:12px;color:var(--text-secondary);margin-bottom:8px">
        Connect MCP servers to give the AI assistant access to external tools: databases, APIs, knowledge bases, and more.
        Assign servers to specific n8n instances or make them available globally.
      </p>
      <div style="font-size:11px;color:var(--text-dim);background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:8px 10px;margin-bottom:12px">
        Building n8n workflows? <a href="https://github.com/czlonkowski/n8n-mcp" target="_blank" rel="noopener" style="color:var(--accent)">n8n-mcp</a> (by czlonkowski)
        gives the assistant deep n8n node knowledge plus workflow validation and create/update tools. Run it in HTTP mode and
        add its <code>/mcp</code> URL below with its auth token (it powers the Code Lab assistant too).
      </div>
      <div id="mcp-list"><div class="spinner"></div></div>
    </div>

    <div class="card hidden" id="add-mcp-card">
      <div class="card-header">
        <span class="card-title">Add MCP Server</span>
        <button class="btn btn-sm btn-ghost" onclick="document.getElementById('add-mcp-card').classList.add('hidden')">&times;</button>
      </div>
      <form id="add-mcp-form" style="max-width:500px">
        <label>
          Name
          <input type="text" id="mcp-name" placeholder="e.g. Qdrant Knowledge Base" required>
        </label>
        <label>
          Server URL
          <input type="text" id="mcp-url" placeholder="http://localhost:8091 or $VAR" required>
          <small>The MCP server's HTTP endpoint</small>
        </label>
        <div style="margin-top:10px;margin-bottom:10px">
          <div id="mcp-token-field"></div>
          <small style="display:block;margin-top:4px;color:var(--text-dim)">Optional. Bearer token saved to the secrets store.</small>
        </div>
        <label>
          Description <span style="font-size:11px;color:var(--text-dim)">(optional)</span>
          <input type="text" id="mcp-desc" placeholder="What tools does this server provide?">
        </label>
        <label>
          Available to instances
          <div id="mcp-instances" style="margin-top:4px"></div>
          <small>Leave unchecked for all instances</small>
        </label>
        <button type="submit" class="btn btn-primary">Add &amp; Test Connection</button>
      </form>
    </div>

    <!-- All Tools -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">Available Tools</span>
        <span class="card-subtitle" id="tools-count"></span>
      </div>
      <div id="all-tools-list"><div class="spinner"></div></div>
    </div>
  `;

  renderN8nMcpCard().catch(() => {});

  let mcpTokenField = null;
  document.getElementById('add-mcp-btn').addEventListener('click', async () => {
    document.getElementById('add-mcp-card').classList.remove('hidden');
    // Mount SecretField for the auth token.
    const tokenContainer = document.getElementById('mcp-token-field');
    if (tokenContainer) {
      if (mcpTokenField) mcpTokenField.destroy();
      mcpTokenField = secretField({
        container: tokenContainer,
        label: 'Auth Token (optional)',
        prefix: 'MCP_TOKEN',
        context: document.getElementById('mcp-name')?.value.trim() || '',
        initialValue: '',
        placeholder: 'Bearer token',
      });
      // Live-update context as the server name is typed.
      const nameEl = document.getElementById('mcp-name');
      if (nameEl) {
        nameEl.addEventListener('input', () => {
          mcpTokenField && mcpTokenField.setContext(nameEl.value.trim());
        });
      }
    }
    // Load instances for checkboxes
    try {
      const data = await get('/api/n8n/instances');
      document.getElementById('mcp-instances').innerHTML = (data.instances || []).map(inst =>
        `<label style="display:flex;align-items:center;gap:6px;margin:2px 0;font-size:12px;cursor:pointer">
          <input type="checkbox" class="mcp-inst-check" value="${inst.id}">
          <span class="instance-dot" style="background:${inst.color || '#ff6d5a'}"></span> ${esc(inst.name)}
        </label>`
      ).join('') || '<span style="font-size:11px;color:var(--text-dim)">No instances configured</span>';
    } catch { /* ignore */ }
  });

  document.getElementById('add-mcp-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const instances = [...document.querySelectorAll('.mcp-inst-check:checked')].map(c => c.value);
    const name = document.getElementById('mcp-name').value.trim();
    let token = mcpTokenField ? mcpTokenField.getValue() : '';

    // Promote raw token to secrets store.
    if (token && !token.startsWith('$')) {
      try {
        const r = await post('/api/admin/secrets/promote', {
          value: token,
          prefix: 'MCP_TOKEN',
          context: name,
        });
        if (r && r.ref) {
          token = r.ref;
          invalidateRefsCache();
        }
      } catch (err) {
        toast.error(`Could not save token to secrets: ${err.message}. Storing inline.`);
      }
    }

    try {
      const result = await post('/api/mcp/servers', {
        name,
        url: document.getElementById('mcp-url').value.trim(),
        token,
        description: document.getElementById('mcp-desc').value.trim(),
        instances,
      });
      toast.success(`Connected! ${result.tools_count} tools discovered.`);
      document.getElementById('add-mcp-card').classList.add('hidden');
      document.getElementById('add-mcp-form').reset();
      if (mcpTokenField) { mcpTokenField.destroy(); mcpTokenField = null; }
      loadMCPList();
      loadAllTools();
    } catch (e) { toast.error(e.message); }
  });

  loadMCPList();
  loadAllTools();
}

async function loadMCPList() {
  const el = document.getElementById('mcp-list');
  if (!el) return;
  try {
    const data = await get('/api/mcp/servers');
    const servers = data.servers || [];
    if (!servers.length) {
      el.innerHTML = '<div class="empty-state"><p>No MCP servers connected. Add one to extend the AI assistant.</p></div>';
      return;
    }
    el.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>URL</th><th>Auth</th><th>Instances</th><th></th></tr></thead>
      <tbody>${servers.map(s => `
        <tr>
          <td style="font-weight:500">${esc(s.name)}</td>
          <td style="font-family:var(--font-mono);font-size:11px">${esc(s.url)}</td>
          <td><span class="pill pill-${s.token_hint ? 'success' : 'neutral'}" style="font-size:10px">${esc(s.token_hint || 'none')}</span></td>
          <td style="font-size:11px">${s.instances.length ? s.instances.length + ' assigned' : '<span style="color:var(--text-dim)">all</span>'}</td>
          <td style="white-space:nowrap">
            <button class="btn btn-sm btn-ghost" onclick="window.__testMCP('${jsStr(s.id)}')">Test</button>
            <button class="btn btn-sm btn-ghost" onclick="window.__mcpTools('${jsStr(s.id)}','${jsStr(s.name)}')">Tools</button>
            <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__removeMCP('${jsStr(s.id)}','${jsStr(s.name)}')">Remove</button>
          </td>
        </tr>
      `).join('')}</tbody>
    </table></div>`;
  } catch { el.innerHTML = '<div class="empty-state"><p>Failed to load</p></div>'; }
}

async function loadAllTools() {
  const el = document.getElementById('all-tools-list');
  const countEl = document.getElementById('tools-count');
  if (!el) return;
  try {
    const data = await get('/api/mcp/tools');
    const tools = data.tools || [];
    // Classify built-in tools: workspace_* are Harness filesystem tools; the
    // rest are n8n tools. Everything non-built-in comes from an MCP server.
    const isHarness = (t) => t.source === 'built-in' && t.name.startsWith('workspace_');
    const nHarness = tools.filter(isHarness).length;
    const nN8n = tools.filter(t => t.source === 'built-in' && !isHarness(t)).length;
    const nMcp = tools.filter(t => t.source !== 'built-in').length;
    if (countEl) countEl.textContent = `${nN8n} n8n · ${nHarness} harness · ${nMcp} MCP`;
    if (!tools.length) {
      el.innerHTML = '<div class="empty-state"><p>No tools available</p></div>';
      return;
    }
    el.innerHTML = tools.map(t => {
      const harness = isHarness(t);
      const builtin = t.source === 'built-in';
      const label = harness ? 'harness' : (builtin ? 'n8n' : 'MCP');
      const pill = harness ? 'warning' : (builtin ? 'info' : 'success');
      return `
      <div style="display:flex;align-items:flex-start;gap:8px;padding:5px 0;border-bottom:1px solid var(--border-dim);font-size:12px;overflow:hidden">
        <span class="pill pill-${pill}" style="font-size:9px;flex-shrink:0">${label}</span>
        <code style="font-size:11px;flex-shrink:0;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.name)}</code>
        <span style="color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0">${esc(t.description)}</span>
      </div>`;
    }).join('');
  } catch { el.innerHTML = ''; }
}

window.__testMCP = async (id) => {
  try {
    const result = await post(`/api/mcp/servers/${id}/test`);
    if (result.connected) toast.success(`Connected! ${result.tools_count} tools, protocol: ${result.protocol}`);
    else toast.error(result.error || 'Failed');
  } catch (e) { toast.error(e.message); }
};

window.__mcpTools = async (id, name) => {
  try {
    const data = await get(`/api/mcp/servers/${id}/tools`);
    const tools = data.tools || [];
    alert(`${name} — ${tools.length} tools:\n\n${tools.map(t => `• ${t.name}`).join('\n')}`);
  } catch (e) { toast.error(e.message); }
};

window.__removeMCP = async (id, name) => {
  if (!confirm(`Remove MCP server "${name}"?`)) return;
  try {
    await del(`/api/mcp/servers/${id}`);
    toast.success('Removed');
    loadMCPList();
    loadAllTools();
  } catch (e) { toast.error(e.message); }
};

// ── Secrets Store ───────────────────────────────────────────────────────────

const _STAB_ACTIVE = 'background:var(--bg-input);color:var(--text-primary);border-bottom:2px solid var(--accent)';
const _STAB_IDLE   = 'background:transparent;color:var(--text-secondary);border-bottom:2px solid transparent';

async function renderSecrets(el) {
  el.innerHTML = `
    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Stored Secrets</span>
        </div>
        <p style="font-size:12px;color:var(--text-secondary);margin-bottom:12px">
          Store API keys and tokens securely. Reference them in instance settings using <code>$SECRET_NAME</code>.
          Values are encrypted at rest.
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
            <input type="text" id="secret-name" placeholder="e.g. N8N_PROD_KEY" required pattern="[A-Za-z_][A-Za-z0-9_]*" title="Letters, numbers, underscores. No spaces.">
            <small>This becomes the <code>$NAME</code> you reference in instance API Key fields</small>
          </label>
          <label>
            Value
            <input type="password" id="secret-value" placeholder="Paste your API key or token" required>
          </label>
          <button type="submit" class="btn btn-primary">Save Secret</button>
        </form>
      </div>
    </div>
  `;

  document.getElementById('add-secret-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = document.getElementById('secret-name').value.trim().toUpperCase().replace(/[^A-Z0-9_]/g, '_');
    const value = document.getElementById('secret-value').value;
    try {
      await post('/api/admin/secrets', { name, value });
      toast.success(`Secret "$${name}" saved. Use $${name} as your API key.`);
      document.getElementById('add-secret-form').reset();
      loadSecrets();
    } catch (e) { toast.error(e.message); }
  });

  loadSecrets();
}

async function loadSecrets() {
  const el = document.getElementById('secrets-list');
  if (!el) return;
  try {
    const data = await get('/api/admin/secrets');
    const secrets = data.secrets || [];

    if (!secrets.length) {
      el.innerHTML = '<div class="empty-state"><p>No secrets stored yet. Add one to get started.</p></div>';
      return;
    }

    el.innerHTML = secrets.map(s => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-dim)">
        <code style="flex:1;font-size:13px">$${esc(s.name)}</code>
        <span style="font-size:11px;color:var(--text-dim);font-family:var(--font-mono)">${esc(s.hint || (s.kind === 'compound' ? `${(s.fields || []).length} fields` : ''))}</span>
        <button class="btn btn-sm btn-ghost" onclick="window.__copyRef('${jsStr(s.name)}', this)" title="Copy $${esc(s.name)} to clipboard">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
          Copy
        </button>
        <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__deleteSecret('${jsStr(s.name)}')">Remove</button>
      </div>
    `).join('');
  } catch {
    el.innerHTML = '<div class="empty-state"><p>Could not load secrets</p></div>';
  }
}

window.__copyRef = (name, btn) => {
  navigator.clipboard.writeText(`$${name}`).then(() => {
    const original = btn.innerHTML;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--success)" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Copied';
    setTimeout(() => { btn.innerHTML = original; }, 1500);
  });
};

window.__deleteSecret = async (name) => {
  if (!confirm(`Delete secret "$${name}"? Instances using it will stop working.`)) return;
  try {
    await del(`/api/admin/secrets/${name}`);
    toast.success(`Deleted "$${name}"`);
    loadSecrets();
  } catch (e) { toast.error(e.message); }
};

// ── Themes ──────────────────────────────────────────────────────────────────

// ── AI Settings (provider config, instructions, knowledge files) ────────────

// Hard-coded model lists. Mirrors backend/modules/assistant/providers.py so the
// Provider dropdown works even when /api/assistant/models is unavailable.
const ASSISTANT_MODELS = {
  openrouter: [
    { id: 'anthropic/claude-sonnet-4', name: 'Claude Sonnet 4' },
    { id: 'anthropic/claude-haiku-4', name: 'Claude Haiku 4' },
    { id: 'openai/gpt-4o', name: 'GPT-4o' },
    { id: 'openai/gpt-4o-mini', name: 'GPT-4o Mini' },
    { id: 'google/gemini-2.5-flash-preview', name: 'Gemini 2.5 Flash' },
    { id: 'google/gemini-2.5-pro-preview', name: 'Gemini 2.5 Pro' },
    { id: 'meta-llama/llama-4-maverick', name: 'Llama 4 Maverick' },
    { id: 'meta-llama/llama-4-scout', name: 'Llama 4 Scout' },
    { id: 'mistralai/mistral-medium-3', name: 'Mistral Medium 3' },
    { id: 'deepseek/deepseek-chat-v3-0324', name: 'DeepSeek V3' },
    { id: 'qwen/qwen-2.5-72b-instruct', name: 'Qwen 2.5 72B' },
  ],
  openai: [
    { id: 'gpt-4o', name: 'GPT-4o' },
    { id: 'gpt-4o-mini', name: 'GPT-4o Mini' },
    { id: 'gpt-4.1', name: 'GPT-4.1' },
    { id: 'gpt-4.1-mini', name: 'GPT-4.1 Mini' },
    { id: 'gpt-4.1-nano', name: 'GPT-4.1 Nano' },
    { id: 'o3-mini', name: 'o3-mini' },
  ],
  anthropic: [
    { id: 'claude-sonnet-4-20250514', name: 'Claude Sonnet 4' },
    { id: 'claude-haiku-4-20250414', name: 'Claude Haiku 4' },
    { id: 'claude-opus-4-20250514', name: 'Claude Opus 4' },
  ],
  perplexity: [
    { id: 'sonar', name: 'Sonar' },
    { id: 'sonar-pro', name: 'Sonar Pro' },
    { id: 'sonar-reasoning', name: 'Sonar Reasoning' },
    { id: 'sonar-reasoning-pro', name: 'Sonar Reasoning Pro' },
    { id: 'sonar-deep-research', name: 'Sonar Deep Research' },
  ],
  groq: [
    { id: 'llama-3.3-70b-versatile', name: 'Llama 3.3 70B Versatile' },
    { id: 'llama-3.1-8b-instant', name: 'Llama 3.1 8B Instant' },
    { id: 'deepseek-r1-distill-llama-70b', name: 'DeepSeek R1 Distill 70B' },
    { id: 'qwen-2.5-32b', name: 'Qwen 2.5 32B' },
  ],
  deepseek: [
    { id: 'deepseek-chat', name: 'DeepSeek V3 (chat)' },
    { id: 'deepseek-reasoner', name: 'DeepSeek R1 (reasoner)' },
  ],
  mistral: [
    { id: 'mistral-large-latest', name: 'Mistral Large' },
    { id: 'mistral-small-latest', name: 'Mistral Small' },
    { id: 'codestral-latest', name: 'Codestral' },
    { id: 'open-mistral-nemo', name: 'Mistral Nemo' },
  ],
  xai: [
    { id: 'grok-3', name: 'Grok 3' },
    { id: 'grok-3-mini', name: 'Grok 3 Mini' },
    { id: 'grok-2-latest', name: 'Grok 2' },
    { id: 'grok-2-vision-latest', name: 'Grok 2 Vision' },
  ],
  together: [
    { id: 'meta-llama/Llama-3.3-70B-Instruct-Turbo', name: 'Llama 3.3 70B Turbo' },
    { id: 'deepseek-ai/DeepSeek-V3', name: 'DeepSeek V3' },
    { id: 'Qwen/Qwen2.5-72B-Instruct-Turbo', name: 'Qwen 2.5 72B Turbo' },
    { id: 'mistralai/Mixtral-8x7B-Instruct-v0.1', name: 'Mixtral 8x7B' },
  ],
  custom: [],
  ollama: [],
};

// Cache of provider -> [{id, name, provider}]. Populated lazily from /api/assistant/models
// with ASSISTANT_MODELS as the fallback when the network call fails.
const assistantModelCache = {};

async function fetchAssistantModels(provider, ollamaUrl = '', keyRef = '') {
  // Cache per (provider, key) so a custom-keyed area's live list doesn't shadow
  // the convention-keyed one.
  const cacheKey = `${provider}|${keyRef || ''}`;
  const cached = assistantModelCache[cacheKey];
  if (cached && provider !== 'ollama') return cached;
  try {
    const qs = new URLSearchParams({ provider });
    if (provider === 'ollama' && ollamaUrl) qs.set('ollama_url', ollamaUrl);
    if (keyRef) qs.set('api_key_ref', keyRef);
    const data = await get(`/api/assistant/models?${qs.toString()}`);
    const models = Array.isArray(data.models) ? data.models : [];
    if (models.length) {
      assistantModelCache[cacheKey] = models;
      return models;
    }
    return ASSISTANT_MODELS[provider] || [];
  } catch {
    return ASSISTANT_MODELS[provider] || [];
  }
}

// Music Player settings moved to dedicated Your Vibe view (views/music.js).
// Spotify OAuth return handler lives there; kept here for legacy ?spotify_* query params.
if (location.search.includes('spotify_connected=1')) {
  history.replaceState({}, '', location.pathname);
  setTimeout(() => toast.success('Spotify connected!'), 500);
} else if (location.search.includes('spotify_error=')) {
  const err = new URLSearchParams(location.search).get('spotify_error');
  history.replaceState({}, '', location.pathname);
  setTimeout(() => toast.error(`Spotify error: ${err}`), 500);
}

// ── Models tab: three self-contained agents (Code Lab / Error Triage /
// General Assistant). Each owns provider + model + instructions + optional
// fallback. Keys come from the Secrets store by provider. No global default,
// no instruction layering.
const MODEL_JOB_ORDER = ['codelab', 'triage', 'assistant'];
const MODEL_JOB_SUBTITLE = {
  codelab: 'Powers the Code Lab assistant (Code Node + Workflow Builder).',
  triage: 'Powers the "Ask AI" analysis on workflow errors.',
  assistant: 'Powers the main dashboard assistant chat.',
};
const MODEL_PROVIDERS = [
  ['openrouter', 'OpenRouter'], ['openai', 'OpenAI'], ['anthropic', 'Anthropic'],
  ['perplexity', 'Perplexity'], ['groq', 'Groq'], ['deepseek', 'DeepSeek'],
  ['mistral', 'Mistral'], ['xai', 'xAI (Grok)'], ['together', 'Together AI'],
  ['ollama', 'Ollama'], ['custom', 'Custom (OpenAI-compatible)'],
];
const MODEL_KEY_REF = {
  anthropic: '$ANTHROPIC_KEY', openai: '$OPEN_AI_KEY', openrouter: '$OPEN_ROUTER_KEY',
  perplexity: '$PERPLEXITY_KEY', groq: '$GROQ_KEY', deepseek: '$DEEPSEEK_KEY',
  mistral: '$MISTRAL_KEY', xai: '$XAI_KEY', together: '$TOGETHER_KEY', custom: '$CUSTOM_LLM_KEY',
};

async function _fillJobModelSelect(modelSel, provider, preferred, keyRef = '') {
  if (!modelSel) return;
  modelSel.innerHTML = '<option value="">Loading...</option>';
  const ollamaUrl = provider === 'ollama' ? (document.getElementById('ai-ollama-url')?.value || '') : '';
  const models = await fetchAssistantModels(provider, ollamaUrl, keyRef);
  let html = models.map(m => `<option value="${attr(m.id)}">${esc(m.name || m.id)}</option>`).join('');
  if (!models.length) html = '<option value="">No models</option>';
  // Preserve a saved model that isn't in the live list (e.g. a custom id).
  if (preferred && !models.some(m => m.id === preferred)) {
    html = `<option value="${attr(preferred)}">${esc(preferred)} (saved)</option>` + html;
  }
  modelSel.innerHTML = html;
  if (preferred) modelSel.value = preferred;
  else modelSel.selectedIndex = 0;
}

async function _fillJobFallbackSelect(modelSel, provider, preferred) {
  if (!modelSel) return;
  if (!provider) { modelSel.innerHTML = '<option value="">None</option>'; modelSel.disabled = true; return; }
  modelSel.disabled = false;
  await _fillJobModelSelect(modelSel, provider, preferred);
}

export async function renderModelsTab(el) {
  const provOpts = MODEL_PROVIDERS.map(([v, n]) => `<option value="${v}">${n}</option>`).join('');
  const fbProvOpts = '<option value="">None (disabled)</option>' + provOpts;
  const taStyle = 'width:100%;box-sizing:border-box;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:10px;color:var(--text-primary);font-family:var(--font-mono);font-size:12px;resize:vertical;line-height:1.5';

  el.innerHTML = `
    <div style="margin-bottom:14px;font-size:13px;color:var(--text-secondary);line-height:1.5">
      Each area is its own assistant: pick a provider and model, and write its instructions.
      Instructions are independent, nothing overrides another. API keys come from the
      <a href="#" id="ai-go-secrets" style="color:var(--accent)">Secrets tab</a> by provider.
    </div>
    ${MODEL_JOB_ORDER.map(key => `
      <div class="card" style="margin-bottom:16px">
        <div class="card-header"><span class="card-title" id="job-${key}-title">${key}</span></div>
        <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px">${MODEL_JOB_SUBTITLE[key] || ''}</p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
          <label>Provider<select id="job-${key}-provider">${provOpts}</select></label>
          <label>Model<select id="job-${key}-model"></select></label>
        </div>
        <label style="display:block;margin-bottom:4px">API key</label>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
          <select id="job-${key}-keyref" style="flex:1"></select>
          <button class="btn btn-sm" id="job-${key}-test" type="button" style="white-space:nowrap">Test &amp; load models</button>
        </div>
        <div id="job-${key}-keyhint" style="font-size:11px;margin-bottom:10px;min-height:14px"></div>
        <label style="display:block">Instructions
          <textarea id="job-${key}-instructions" rows="7" style="${taStyle}" placeholder="How this assistant should behave..."></textarea>
        </label>
        <div style="display:flex;justify-content:flex-end;margin:4px 0 8px">
          <a href="#" id="job-${key}-reset" style="font-size:11px;color:var(--text-dim)">Reset to default</a>
        </div>
        <details>
          <summary style="font-size:12px;color:var(--text-secondary);cursor:pointer">Fallback model (optional)</summary>
          <p style="font-size:12px;color:var(--text-dim);margin:8px 0">Used only if the primary errors (5xx / 429 / timeout).</p>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <label>Fallback provider<select id="job-${key}-fbprovider">${fbProvOpts}</select></label>
            <label>Fallback model<select id="job-${key}-fbmodel"><option value="">None</option></select></label>
          </div>
        </details>
        <div style="display:flex;gap:10px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--border-dim)">
          <button class="btn btn-primary btn-sm" id="job-${key}-save" type="button">Save</button>
          <span id="job-${key}-save-result" style="font-size:12px"></span>
        </div>
      </div>
    `).join('')}

    <div class="card" style="margin-bottom:16px">
      <div class="card-header"><span class="card-title">Shared: Local Ollama</span></div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:10px">
        Endpoint used whenever a job's provider is set to Ollama.
      </p>
      <label>Ollama URL<input type="url" id="ai-ollama-url" placeholder="http://localhost:11434"></label>
      <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
        <button class="btn" id="ai-shared-save" type="button">Save Ollama URL</button>
        <span id="ai-shared-result" style="font-size:12px"></span>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-header"><span class="card-title">Shared: Custom OpenAI-compatible endpoint</span></div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:10px">
        Base URL used whenever a job's provider is set to <strong>Custom</strong>. Point it at any
        OpenAI-compatible API root (Azure OpenAI, LiteLLM, vLLM, LocalAI, Fireworks, ...). The key
        comes from <code>$CUSTOM_LLM_KEY</code> in Secrets (or pick a saved key per area). Save here
        before testing the Custom provider.
      </p>
      <label>Base URL<input type="url" id="ai-custom-base-url" placeholder="https://my-proxy.example.com/v1"></label>
      <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
        <button class="btn" id="ai-custom-save" type="button">Save endpoint</button>
        <span id="ai-custom-result" style="font-size:12px"></span>
      </div>
    </div>
  `;

  let cfg = {};
  try { cfg = await get('/api/assistant/config'); } catch { /* offline */ }

  const jobs = cfg.jobs || {};
  const keyStatus = cfg.key_status || {};
  const defaults = cfg.instruction_defaults || {};
  const labels = cfg.job_labels || { codelab: 'Code Lab', triage: 'Error Triage', assistant: 'General Assistant' };

  // Stashed secrets, for the per-area "API key" dropdown. Each is { ref: "$NAME", hint }.
  let secretRefs = [];
  try { secretRefs = (await get('/api/admin/secrets/refs')).refs || []; } catch { /* offline */ }

  // Build the <option> list for a key-ref select: a "use provider default" entry
  // first, then every saved secret. The empty value means "convention key".
  function keyRefOptions(selectedRef) {
    const opts = [`<option value=""${!selectedRef ? ' selected' : ''}>Use provider default key</option>`];
    for (const r of secretRefs) {
      const sel = r.ref === selectedRef ? ' selected' : '';
      opts.push(`<option value="${attr(r.ref)}"${sel}>${esc(r.ref)}${r.hint ? ` — ${esc(r.hint)}` : ''}</option>`);
    }
    // A saved value that no longer matches any secret still shows, so the user sees it.
    if (selectedRef && !secretRefs.some(r => r.ref === selectedRef)) {
      opts.push(`<option value="${attr(selectedRef)}" selected>${esc(selectedRef)} (not found)</option>`);
    }
    return opts.join('');
  }

  const ollamaInput = document.getElementById('ai-ollama-url');
  if (ollamaInput) ollamaInput.value = cfg.ollama_url || 'http://localhost:11434';
  const customInput = document.getElementById('ai-custom-base-url');
  if (customInput) customInput.value = cfg.custom_base_url || '';

  const goSecrets = (e) => { e.preventDefault(); if (window.__goSettings) window.__goSettings('secrets'); };
  document.getElementById('ai-go-secrets')?.addEventListener('click', goSecrets);

  function renderKeyHint(key) {
    const prov = document.getElementById(`job-${key}-provider`).value;
    const hintEl = document.getElementById(`job-${key}-keyhint`);
    if (!hintEl) return;
    if (prov === 'ollama') {
      hintEl.innerHTML = '<span style="color:var(--text-dim)">No API key needed (uses the shared Ollama URL below).</span>';
      return;
    }
    // An explicit secret chosen for this area takes precedence over the convention key.
    const chosenRef = document.getElementById(`job-${key}-keyref`)?.value || '';
    if (chosenRef) {
      hintEl.innerHTML = `<span style="color:var(--success, #34d399)">&#10003; Using ${esc(chosenRef)} for this area</span>`;
      return;
    }
    const ok = !!keyStatus[prov];
    if (ok) {
      hintEl.innerHTML = `<span style="color:var(--success, #34d399)">&#10003; ${esc(prov)} key found in Secrets (${esc(MODEL_KEY_REF[prov] || '')})</span>`;
    } else {
      hintEl.innerHTML = `<span style="color:var(--warning, #fbbf24)">&#9888; no ${esc(prov)} key &mdash; add ${esc(MODEL_KEY_REF[prov] || '')} in <a href="#" class="job-go-secrets" style="color:var(--accent)">Secrets</a>, or pick a saved key above</span>`;
      hintEl.querySelector('.job-go-secrets')?.addEventListener('click', goSecrets);
    }
  }

  for (const key of MODEL_JOB_ORDER) {
    const titleEl = document.getElementById(`job-${key}-title`);
    if (titleEl) titleEl.textContent = labels[key] || key;
    const j = jobs[key] || {};
    const provSel = document.getElementById(`job-${key}-provider`);
    const modelSel = document.getElementById(`job-${key}-model`);
    const instr = document.getElementById(`job-${key}-instructions`);
    const fbProv = document.getElementById(`job-${key}-fbprovider`);
    const fbModel = document.getElementById(`job-${key}-fbmodel`);

    provSel.value = j.provider || 'openrouter';
    instr.value = j.instructions || '';
    const keyRefSel = document.getElementById(`job-${key}-keyref`);
    if (keyRefSel) keyRefSel.innerHTML = keyRefOptions(j.api_key_ref || '');

    // Reload the model list live using whatever key this area is set to (the
    // chosen secret, else the convention key). Optionally test the connection
    // first and surface the result in the key hint — done when the user picks a
    // key, so selecting a key both validates it and pulls the full live list.
    const refreshArea = async (test) => {
      const prov = provSel.value;
      const keyRef = keyRefSel ? keyRefSel.value : '';
      const hintEl = document.getElementById(`job-${key}-keyhint`);
      if (test && prov !== 'ollama') {
        if (hintEl) hintEl.innerHTML = '<span style="color:var(--text-dim)">Testing connection&hellip;</span>';
        let ok = false, errMsg = '';
        try {
          const r = await post('/api/assistant/test-creds', { provider: prov, api_key_ref: keyRef, model: modelSel.value || '' });
          ok = !!r.ok; errMsg = r.error || '';
        } catch (e) { errMsg = e.message; }
        await _fillJobModelSelect(modelSel, prov, modelSel.value || '', keyRef);
        if (hintEl) {
          hintEl.innerHTML = ok
            ? `<span style="color:var(--success, #34d399)">&#10003; Connected${keyRef ? ` with ${esc(keyRef)}` : ''} &mdash; live models loaded</span>`
            : `<span style="color:var(--error)">&#10007; ${esc(errMsg || 'Connection failed')}</span>`;
        }
      } else {
        await _fillJobModelSelect(modelSel, prov, modelSel.value || '', keyRef);
        renderKeyHint(key);
      }
    };

    await _fillJobModelSelect(modelSel, provSel.value, j.model || '', keyRefSel ? keyRefSel.value : '');
    renderKeyHint(key);
    if (keyRefSel) keyRefSel.addEventListener('change', () => refreshArea(true));
    document.getElementById(`job-${key}-test`)?.addEventListener('click', () => refreshArea(true));
    provSel.addEventListener('change', async () => {
      // A key chosen for the old provider won't authenticate the new one; reset
      // to the provider-default key so the model list reloads cleanly.
      if (keyRefSel) keyRefSel.value = '';
      modelSel.value = '';
      await refreshArea(false);
    });

    fbProv.value = j.fallback_provider || '';
    await _fillJobFallbackSelect(fbModel, fbProv.value, j.fallback_model || '');
    fbProv.addEventListener('change', async () => { await _fillJobFallbackSelect(fbModel, fbProv.value, ''); });

    document.getElementById(`job-${key}-reset`)?.addEventListener('click', (e) => {
      e.preventDefault();
      instr.value = defaults[key] || '';
    });

    // Per-area Save: only this area's config is sent. The /jobs endpoint merges
    // partial payloads, so the other two areas are left untouched.
    document.getElementById(`job-${key}-save`)?.addEventListener('click', async (e) => {
      const fbp = document.getElementById(`job-${key}-fbprovider`).value;
      const payload = {
        provider: document.getElementById(`job-${key}-provider`).value,
        model: document.getElementById(`job-${key}-model`).value,
        instructions: document.getElementById(`job-${key}-instructions`).value,
        fallback_provider: fbp,
        fallback_model: fbp ? document.getElementById(`job-${key}-fbmodel`).value : '',
        api_key_ref: document.getElementById(`job-${key}-keyref`)?.value || '',
      };
      const btn = e.currentTarget;
      const res = document.getElementById(`job-${key}-save-result`);
      btn.disabled = true;
      try {
        await post('/api/assistant/jobs', { jobs: { [key]: payload } });
        // A saved area model supersedes any sticky inline session pick, and a
        // mounted Code Lab / Assistant picker updates live.
        try {
          sessionStorage.removeItem('ageniusdesk:codelab_override');
          sessionStorage.removeItem('ageniusdesk:assistant_override');
        } catch { /* ignore */ }
        try { window.dispatchEvent(new CustomEvent('agd:area-defaults-saved', { detail: { [key]: payload } })); } catch { /* ignore */ }
        if (res) { res.textContent = 'Saved'; res.style.color = 'var(--success, #34d399)'; }
        toast.success(`${labels[key] || key} saved`);
      } catch (err) {
        if (res) { res.textContent = err.message; res.style.color = 'var(--error)'; }
      } finally {
        btn.disabled = false;
      }
    });
  }

  document.getElementById('ai-shared-save')?.addEventListener('click', async () => {
    const res = document.getElementById('ai-shared-result');
    try {
      await post('/api/assistant/shared', { ollama_url: ollamaInput?.value || '' });
      if (res) { res.textContent = 'Saved'; res.style.color = 'var(--success, #34d399)'; }
    } catch (e) {
      if (res) { res.textContent = e.message; res.style.color = 'var(--error)'; }
    }
  });

  document.getElementById('ai-custom-save')?.addEventListener('click', async () => {
    const res = document.getElementById('ai-custom-result');
    try {
      await post('/api/assistant/shared', { custom_base_url: customInput?.value || '' });
      if (res) { res.textContent = 'Saved'; res.style.color = 'var(--success, #34d399)'; }
    } catch (e) {
      if (res) { res.textContent = e.message; res.style.color = 'var(--error)'; }
    }
  });
}

async function renderThemes(el) {
  el.innerHTML = `
    <div class="card">
      <div class="card-header"><span class="card-title">Theme</span></div>
      <div id="theme-picker"><div class="spinner"></div></div>
    </div>
  `;
  try {
    const data = await get('/api/themes');
    const active = getCurrentTheme();
    document.getElementById('theme-picker').innerHTML = `<div class="theme-grid">${(data.themes || []).map(t => `
      <div class="theme-card ${t.id === active ? 'active' : ''}" onclick="window.__setTheme('${jsStr(t.id)}')">
        <div class="theme-swatches">${Object.entries(t.colors).slice(0, 4).map(([,v]) => `<div class="theme-swatch" style="background:${v}"></div>`).join('')}</div>
        <div class="theme-card-name">${esc(t.name)}</div>
      </div>
    `).join('')}</div>`;
  } catch { document.getElementById('theme-picker').innerHTML = ''; }
}

window.__setTheme = async (id) => {
  try { await setActiveTheme(id); toast.success('Theme applied'); renderThemes(document.getElementById('settings-tab-content')); } catch(e) { toast.error(e.message); }
};

// ── Error Handler ───────────────────────────────────────────────────────────

function renderErrorHandler(el) {
  const webhookUrl = `${location.origin}/api/errors/webhook`;
  const templateUrl = `/api/errors/handler-template?dashboard_url=${encodeURIComponent(location.origin)}`;
  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">Error Handler Setup</span>
      </div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px">
        Install the global error handler into your active n8n instance to get real-time errors in this dashboard.
        It catches failures from every workflow and posts them here.
      </p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
        <button class="btn btn-primary" id="eh-install-btn">Install to active instance</button>
        <a class="btn btn-ghost" href="${templateUrl}" download="global-error-handler.json">Download workflow JSON</a>
      </div>
      <div id="eh-result" style="font-size:12px;margin-bottom:12px;min-height:14px"></div>
      <div style="background:var(--bg-input);border-radius:var(--radius);padding:16px;font-size:13px">
        <p style="margin-bottom:8px"><strong>Webhook URL</strong> (where n8n posts errors):</p>
        <code style="display:block;padding:8px;background:var(--bg-void);border-radius:4px;word-break:break-all;margin-bottom:16px">${esc(webhookUrl)}</code>
        <div style="color:var(--text-secondary);line-height:1.8">
          <strong>After installing, one step in n8n:</strong><br>
          open n8n, then <strong>Settings, Workflows, Error Workflow</strong>, and select
          <em>"Global Error Handler → AgeniusDesk"</em>. That tells n8n to run it on every failure.
          <br><br>
          <span style="color:var(--text-dim)">If your n8n cannot reach <code>${esc(location.origin)}</code> (for example it runs on a
          different network), edit the imported workflow's HTTP node URL or set <code>FLOW_DASHBOARD_URL</code>
          in n8n's environment.</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span class="card-title">Error reporting window</span>
      </div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px">
        How far back error reporting looks across the dashboard. Sets the span for the Overview
        Recent Errors widget, the Failure Rate card, and the Errors view. Saved in this browser.
      </p>
      <label style="display:flex;align-items:center;gap:8px;margin:0;font-size:13px">
        <span>Report errors from the</span>
        <select id="error-lookback-select" style="width:auto;margin:0;padding:6px 10px;font-size:12px">
          ${lookbackOptionsHtml(getErrorLookback())}
        </select>
      </label>
    </div>
  `;

  const lookbackSel = document.getElementById('error-lookback-select');
  if (lookbackSel) {
    lookbackSel.addEventListener('change', () => {
      setErrorLookback(lookbackSel.value);
      toast.success('Error reporting window updated');
    });
  }

  const btn = document.getElementById('eh-install-btn');
  const res = document.getElementById('eh-result');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Installing…';
    res.innerHTML = '';
    try {
      const r = await post('/api/errors/install-handler', { dashboard_url: location.origin, activate: true });
      const verb = r.activated ? 'imported and activated' : 'imported';
      res.innerHTML = `<span style="color:var(--success)">Error handler ${verb} as "${esc(r.name || 'workflow')}". Now select it as the Error Workflow in n8n (see below).</span>`;
      if (!r.activated && r.activation_error) {
        res.innerHTML += `<br><span style="color:var(--text-dim)">Auto-activate failed (${esc(r.activation_error)}); activate it manually in n8n.</span>`;
      }
      toast.success('Error handler installed');
    } catch (e) {
      res.innerHTML = `<span style="color:var(--error)">${esc(e.message || 'Install failed')}</span>`;
      toast.error(e.message || 'Install failed');
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  });
}

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }

// Escape for a double-quoted HTML attribute. esc() leaves " and ' intact, so it
// is unsafe in attribute context (a quote breaks out of the attribute); use this
// for value="..." / style="..." interpolations of server- or user-sourced data.
function attr(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

// ── Account (password, 2FA, sessions) ────────────────────────────────────────

function _esc(s) { const d = document.createElement('span'); d.textContent = s == null ? '' : s; return d.innerHTML; }

async function renderAccount(el) {
  el.innerHTML = `<div class="card"><p class="muted">Loading account…</p></div>`;
  let me;
  try {
    const r = await get('/api/auth/me');
    me = r.user;
  } catch (e) {
    if (e.status === 403 || e.status === 401) {
      el.innerHTML = `<div class="card"><h3>Account</h3>
        <p class="muted">Browser login is disabled or this session is managed by your
        edge proxy, so there is no local account to manage here.</p></div>`;
      return;
    }
    el.innerHTML = `<div class="card"><p class="error-text">Could not load account: ${_esc(e.message)}</p></div>`;
    return;
  }
  if (!me) { el.innerHTML = `<div class="card"><p class="muted">No account.</p></div>`; return; }

  const twofa = me.totp && me.totp.enabled;
  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <h3 style="margin-top:0">Account</h3>
      <p class="muted" style="margin:0">
        Signed in as <strong>${_esc(me.email || me.username)}</strong>
        <span class="badge">${_esc(me.role)}</span></p>
      ${me.display_name && me.display_name !== me.username
        ? `<p class="muted" style="margin:6px 0 0;font-size:12px">${_esc(me.display_name)}</p>` : ''}
      <button class="btn btn-secondary" id="acc-logout" style="margin-top:14px">Sign out</button>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3 style="margin-top:0">Change password</h3>
      <div class="field"><label>Current password</label>
        <input type="password" id="pw-cur" autocomplete="current-password"></div>
      <div class="field"><label>New password</label>
        <input type="password" id="pw-new" autocomplete="new-password"></div>
      <div id="pw-ck"></div>
      <div class="field" style="margin-top:12px"><label>Confirm new password</label>
        <input type="password" id="pw-new2" autocomplete="new-password"></div>
      <button class="btn btn-primary" id="pw-save" style="margin-top:18px">Update password</button>
      <div id="pw-msg" class="muted" style="margin-top:10px"></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3 style="margin-top:0">Two-factor authentication</h3>
      <div id="twofa-body"></div>
    </div>

    <div class="card">
      <h3 style="margin-top:0">Active sessions</h3>
      <div id="sessions-body"><p class="muted">Loading…</p></div>
    </div>`;

  el.querySelector('#acc-logout').onclick = async () => {
    try { await post('/api/auth/logout', {}); location.reload(); }
    catch (e) { toast.error(e.message); }
  };

  // Live password-policy checklist on the new-password field. Policy comes from
  // the (unauthenticated) status endpoint so it always mirrors the server.
  let pwChecklist = null;
  get('/api/auth/status')
    .then(s => { pwChecklist = mountChecklist(el.querySelector('#pw-ck'), el.querySelector('#pw-new'), s.password_policy); })
    .catch(() => { pwChecklist = mountChecklist(el.querySelector('#pw-ck'), el.querySelector('#pw-new'), null); });

  el.querySelector('#pw-save').onclick = async () => {
    const cur = el.querySelector('#pw-cur').value;
    const nw = el.querySelector('#pw-new').value;
    const nw2 = el.querySelector('#pw-new2').value;
    const msg = el.querySelector('#pw-msg');
    if (pwChecklist && !pwChecklist.isValid()) { msg.textContent = 'Password does not meet the requirements below'; return; }
    if (nw !== nw2) { msg.textContent = 'New passwords do not match'; return; }
    try {
      await post('/api/auth/password', { current_password: cur, new_password: nw });
      msg.textContent = 'Password updated. Other sessions were signed out.';
      el.querySelector('#pw-cur').value = el.querySelector('#pw-new').value = el.querySelector('#pw-new2').value = '';
      if (pwChecklist) pwChecklist.refresh();
    } catch (e) { msg.textContent = e.message; }
  };

  renderTwoFactor(el.querySelector('#twofa-body'), twofa);
  renderSessions(el.querySelector('#sessions-body'));
}

function renderTwoFactor(box, enabled) {
  if (enabled) {
    box.innerHTML = `
      <p class="muted">Two-factor is <strong style="color:var(--success,#34d399)">on</strong>.</p>
      <div class="field"><label>Password</label><input type="password" id="td-pw"></div>
      <div class="field"><label>Current 2FA code</label><input id="td-code" inputmode="numeric" placeholder="123456"></div>
      <button class="btn btn-danger" id="td-disable" style="margin-top:18px">Disable 2FA</button>
      <div id="td-msg" class="muted" style="margin-top:10px"></div>`;
    box.querySelector('#td-disable').onclick = async () => {
      const msg = box.querySelector('#td-msg');
      try {
        await post('/api/auth/totp/disable', {
          password: box.querySelector('#td-pw').value,
          code: box.querySelector('#td-code').value.trim(),
        });
        toast.success('Two-factor disabled');
        renderTwoFactor(box, false);
      } catch (e) { msg.textContent = e.message; }
    };
    return;
  }
  box.innerHTML = `
    <p class="muted">Add a second factor with any authenticator app
      (Google Authenticator, Authy, 1Password).</p>
    <button class="btn btn-primary" id="te-start">Enable 2FA</button>
    <div id="te-flow" style="margin-top:14px"></div>`;
  box.querySelector('#te-start').onclick = async () => {
    const flow = box.querySelector('#te-flow');
    box.querySelector('#te-start').disabled = true;
    let data;
    try { data = await post('/api/auth/totp/enroll', {}); }
    catch (e) { toast.error(e.message); box.querySelector('#te-start').disabled = false; return; }
    flow.innerHTML = `
      <canvas id="te-qr" style="background:#fff;border-radius:8px;padding:8px"></canvas>
      <p class="muted" style="margin:10px 0 4px">Scan the code, or enter this key manually:</p>
      <code style="display:inline-block;padding:6px 10px;background:var(--bg-primary);
        border-radius:6px;letter-spacing:1px;word-break:break-all">${_esc(data.secret)}</code>
      <div class="field" style="margin-top:14px"><label>Enter the 6-digit code to confirm</label>
        <input id="te-code" inputmode="numeric" placeholder="123456"></div>
      <button class="btn btn-primary" id="te-activate" style="margin-top:18px">Confirm</button>
      <div id="te-msg" class="muted" style="margin-top:10px"></div>`;
    try { renderQR(flow.querySelector('#te-qr'), data.otpauth_uri, { scale: 5 }); } catch { /* manual key still works */ }
    flow.querySelector('#te-activate').onclick = async () => {
      const msg = flow.querySelector('#te-msg');
      try {
        const r = await post('/api/auth/totp/activate', { code: flow.querySelector('#te-code').value.trim() });
        showRecoveryCodes(box, r.recovery_codes);
      } catch (e) { msg.textContent = e.message; }
    };
  };
}

function showRecoveryCodes(box, codes) {
  box.innerHTML = `
    <p style="color:var(--success,#34d399)"><strong>Two-factor is now on.</strong></p>
    <p class="muted">Save these recovery codes somewhere safe. Each works once if you
      lose your authenticator. They are shown only now.</p>
    <pre style="padding:12px;background:var(--bg-primary);border-radius:8px;
      line-height:1.8">${codes.map(_esc).join('\n')}</pre>
    <button class="btn btn-secondary" id="rc-done">Done</button>`;
  box.querySelector('#rc-done').onclick = () => renderTwoFactor(box, true);
}

async function renderSessions(box) {
  let sessions;
  try { sessions = (await get('/api/auth/sessions')).sessions; }
  catch (e) { box.innerHTML = `<p class="muted">${_esc(e.message)}</p>`; return; }
  if (!sessions.length) { box.innerHTML = `<p class="muted">No active sessions.</p>`; return; }
  box.innerHTML = sessions.map(s => `
    <div style="display:flex;justify-content:space-between;align-items:center;
      padding:8px 0;border-bottom:1px solid var(--border-dim)">
      <div>
        <div>${_esc(s.user_agent.slice(0, 60) || 'Unknown device')}
          ${s.current ? '<span class="badge">this device</span>' : ''}</div>
        <div class="muted" style="font-size:12px">${_esc(s.ip)} · last seen ${_esc(s.last_seen)}</div>
      </div>
      ${s.current ? '' : `<button class="btn btn-secondary btn-sm" data-revoke="${_esc(s.id)}">Revoke</button>`}
    </div>`).join('');
  box.querySelectorAll('[data-revoke]').forEach(btn => {
    btn.onclick = async () => {
      try { await del(`/api/auth/sessions/${btn.dataset.revoke}`); renderSessions(box); }
      catch (e) { toast.error(e.message); }
    };
  });
}

// Fallback so a deep-link into a settings tab works even before app.js wires window.__goSettings.
window.__goSettings = window.__goSettings || ((tab) => {
  if (window.__nav) window.__nav('settings');
  setTimeout(() => { if (window.__settingsTab) window.__settingsTab(tab); }, 100);
});

