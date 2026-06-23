/**
 * Containers view — Docker container management.
 *
 * Shows all containers grouped optionally by compose project.
 * Supports start / stop / restart actions and a live log panel
 * that streams via Server-Sent Events.
 */

import { get, post } from '../api.js';
import * as toast from '../components/toast.js';
import { openModal } from '../components/modal.js';

// Host ports Chrome and most browsers refuse to open (ERR_UNSAFE_PORT). We steer
// the deploy port picker away from these so a deployed service is reachable.
// Mirrors Chromium's restricted-ports list (backend enforces the same set).
const CHROME_UNSAFE_PORTS = new Set([1,7,9,11,13,15,17,19,20,21,22,23,25,37,42,43,53,69,77,79,87,95,101,102,103,104,109,110,111,113,115,117,119,123,135,137,139,143,161,179,389,427,465,512,513,514,515,526,530,531,532,540,548,554,556,563,587,601,636,989,990,993,995,1719,1720,1723,2049,3659,4045,4190,5060,5061,6000,6566,6665,6666,6667,6668,6669,6697,10080]);

let _allContainers = [];
let _projects = [];
let _filter = 'all';     // 'all' | 'running' | '<project-name>'
let _groupByProject = true;

let _logContainerId = null;
let _logSource = null;    // active EventSource
let _logFollow = false;
let _refreshTimer = null;

// Deploy panel state
let _deployTemplates = [];
let _selectedTemplate = null;
let _deploySource = null;  // active deploy SSE

// Public host for synthesizing "Open container" URLs. Resolved from the
// backend on view render (AGD_PUBLIC_HOST env → request Host header →
// "localhost"). Right value for remote dashboards (LXC, NAS, VPS).
let _publicHost = 'localhost';
// Whether the dashboard itself runs inside Docker. When true, its own
// "localhost" is the dashboard container, not the host, so URLs the BACKEND
// uses to reach sibling containers must go through host.docker.internal.
let _inDocker = false;

async function _refreshPublicHost() {
  try {
    const res = await get('/api/containers/public-host');
    if (res && typeof res.public_host === 'string' && res.public_host) {
      _publicHost = res.public_host;
    }
  } catch {
    // Leave fallback in place.
  }
  try {
    const d = await get('/api/health/docker-env');
    _inDocker = !!(d && d.in_docker === true);
  } catch {
    // Assume not in Docker; localhost stays the register host.
  }
}

// ── Render ──────────────────────────────────────────────────────────────────

export async function render(container) {
  container.innerHTML = `
    <style>
      .ct-table { width: 100%; border-collapse: collapse; font-size: 13px; }
      .ct-table th {
        text-align: left; font-size: 10px; text-transform: uppercase;
        letter-spacing: 0.5px; color: var(--text-dim);
        padding: 6px 12px; border-bottom: 1px solid var(--border-dim);
        font-weight: 600;
      }
      .ct-row { border-bottom: 1px solid var(--border-dim); transition: background 0.1s; }
      .ct-row:hover { background: rgba(255,255,255,0.03); }
      .ct-row td { padding: 9px 12px; vertical-align: middle; }
      .ct-badge {
        display: inline-block; padding: 1px 7px; border-radius: 10px;
        font-size: 10px; font-weight: 600; letter-spacing: 0.3px;
      }
      .ct-project-header {
        padding: 8px 12px; font-size: 10px; font-weight: 700;
        text-transform: uppercase; letter-spacing: 0.6px;
        color: var(--text-dim); background: rgba(255,255,255,0.02);
        border-bottom: 1px solid var(--border-dim);
      }
      .ct-action-btn {
        padding: 4px 7px; font-size: 11px; border-radius: 4px;
        border: 1px solid var(--border-dim); background: transparent;
        color: var(--text-secondary); cursor: pointer; transition: all 0.1s;
        margin-right: 3px; display: inline-flex; align-items: center;
        justify-content: center; width: 28px; height: 26px;
      }
      .ct-action-btn:hover { background: rgba(255,255,255,0.07); color: var(--text-primary); }
      .ct-action-btn:disabled { opacity: 0.4; cursor: not-allowed; }
      .ct-action-btn.danger:hover { color: #ff6d5a; border-color: #ff6d5a44; }
      .ct-action-btn.primary:hover { color: #34d399; border-color: #34d39944; }
      .ct-more-btn { width: 28px; height: 26px; font-size: 14px; letter-spacing: -0.5px; }
      #ct-log-panel {
        border: 1px solid var(--border-dim);
        border-radius: var(--radius);
        background: #0a0d14;
        display: flex; flex-direction: column;
        height: 340px; margin-top: 14px;
        overflow: hidden;
      }
      #ct-log-header {
        display: flex; align-items: center; gap: 10px;
        padding: 8px 12px;
        border-bottom: 1px solid var(--border-dim);
        flex-shrink: 0;
      }
      #ct-log-body {
        flex: 1; overflow-y: auto; padding: 10px 14px;
        font-family: var(--font-mono); font-size: 11px;
        line-height: 1.6; color: #c9d1d9; margin: 0;
        white-space: pre-wrap; word-break: break-all;
      }
      .filter-btn {
        padding: 3px 10px; font-size: 11px; border-radius: 4px;
        border: 1px solid var(--border-dim); background: transparent;
        color: var(--text-secondary); cursor: pointer;
      }
      .filter-btn.active {
        background: rgba(96,165,250,0.15); border-color: #60a5fa66;
        color: #60a5fa;
      }
      /* Template tile grid */
      .ct-tmpl-cat {
        font-size: 9px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.7px; color: var(--text-dim);
        padding: 14px 0 6px; margin-bottom: 2px;
      }
      .ct-tmpl-cat:first-child { padding-top: 0; }
      .ct-tmpl-grid-row {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
        gap: 10px; margin-bottom: 4px;
      }
      .ct-tmpl-tile {
        position: relative;
        background: var(--bg-input);
        border: 1px solid var(--border-dim);
        border-radius: var(--radius);
        cursor: pointer;
        display: flex; flex-direction: column; align-items: center;
        padding: 16px 12px 12px;
        text-align: center;
        transition: border-color 0.15s, background 0.15s;
        min-height: 140px;
      }
      .ct-tmpl-tile:hover { border-color: #3b82f655; background: rgba(96,165,250,0.07); }
      .ct-tmpl-tile-icon { font-size: 26px; line-height: 1; margin-bottom: 8px; }
      .ct-tmpl-tile-name {
        font-size: 12px; font-weight: 600; color: var(--text-primary);
        margin-bottom: 4px; word-break: break-word;
      }
      .ct-tmpl-tile-footer { margin-top: auto; width: 100%; }
      .ct-tmpl-tile-desc {
        font-size: 10px; color: var(--text-dim);
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      }
      .ct-tmpl-tile-tags { display: flex; gap: 4px; flex-wrap: wrap; justify-content: center; margin-top: 4px; }
      .ct-tmpl-badge {
        font-size: 9px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.4px; color: #a78bfa;
        background: rgba(167,139,250,0.12); border: 1px solid #a78bfa33;
        border-radius: 3px; padding: 1px 4px;
      }
      .ct-tmpl-tile-running {
        position: absolute; top: 6px; right: 6px;
        display: flex; align-items: center; gap: 4px;
        font-size: 9px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.3px; color: #34d399;
        background: rgba(52,211,153,0.12); border: 1px solid #34d39944;
        border-radius: 3px; padding: 2px 5px;
      }
      .ct-tmpl-docs {
        font-size: 10px; color: #60a5fa; text-decoration: none;
        padding: 1px 5px; border: 1px solid #60a5fa44; border-radius: 3px;
        display: inline-block; margin-top: 4px;
      }
      .ct-tmpl-docs:hover { background: rgba(96,165,250,0.12); }
    </style>

    <div class="section-header">
      <div>
        <h2 class="section-title">Containers</h2>
        <span id="ct-summary" style="font-size:12px;color:var(--text-secondary)">Loading…</span>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <label style="font-size:11px;color:var(--text-dim);display:flex;align-items:center;gap:5px;cursor:pointer">
          <input type="checkbox" id="ct-group-toggle" checked> Group by project
        </label>
        <button class="btn btn-sm btn-ghost" id="ct-deploy-btn" title="Deploy a new container from a template">+ Deploy</button>
        <button class="btn btn-sm btn-ghost" id="ct-refresh-btn">Refresh</button>
      </div>
    </div>

    <!-- Deploy panel -->
    <div id="ct-deploy-panel" style="display:none;margin-bottom:14px;background:var(--bg-panel);border:1px solid var(--accent-dim,#3b82f655);border-radius:var(--radius);overflow:hidden">
      <div style="display:flex;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border-dim)">
        <span style="font-weight:600;font-size:13px">Deploy new instance</span>
        <button id="ct-deploy-close" class="btn btn-sm btn-ghost" style="margin-left:auto">✕</button>
      </div>
      <!-- Step 1: template picker -->
      <div id="ct-deploy-step1" style="padding:14px">
        <div style="font-size:11px;color:var(--text-dim);margin-bottom:10px;text-transform:uppercase;letter-spacing:0.5px">Choose a template</div>
        <div id="ct-template-grid"></div>
      </div>
      <!-- Step 2: config form -->
      <div id="ct-deploy-step2" style="display:none;padding:14px">
        <button id="ct-deploy-back" style="background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:12px;padding:0;margin-bottom:12px">← Back</button>
        <div id="ct-deploy-form-title" style="font-size:13px;font-weight:600;margin-bottom:12px"></div>
        <div id="ct-deploy-fields" style="display:grid;grid-template-columns:1fr 1fr;gap:10px"></div>
        <div style="margin-top:14px;display:flex;align-items:center;gap:10px">
          <button class="btn btn-sm btn-primary" id="ct-deploy-submit">Deploy</button>
          <span id="ct-deploy-err" style="font-size:11px;color:#ff6d5a"></span>
        </div>
      </div>
      <!-- Step 3: progress -->
      <div id="ct-deploy-step3" style="display:none;padding:14px">
        <div id="ct-deploy-steps-list" style="display:flex;flex-direction:column;gap:6px;font-size:12px"></div>
        <div id="ct-deploy-result" style="display:none;margin-top:14px;padding:12px;background:rgba(52,211,153,0.08);border:1px solid #34d39944;border-radius:6px">
          <div style="font-weight:600;color:#34d399;margin-bottom:8px">Deployed successfully!</div>
          <div id="ct-deploy-result-body" style="font-size:12px;color:var(--text-secondary)"></div>
          <div style="display:flex;gap:8px;margin-top:10px">
            <a id="ct-deploy-open-link" href="#" target="_blank" class="btn btn-sm btn-ghost">Open instance</a>
            <button id="ct-deploy-register-btn" class="btn btn-sm btn-ghost">Add to AgeniusDesk</button>
            <button id="ct-deploy-another-btn" class="btn btn-sm btn-ghost">Deploy another</button>
          </div>
        </div>
        <div id="ct-deploy-error-box" style="display:none;margin-top:14px;padding:12px;background:rgba(255,109,90,0.08);border:1px solid #ff6d5a44;border-radius:6px;color:#ff6d5a;font-size:12px"></div>
      </div>
    </div>

    <div style="display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap" id="ct-filter-bar">
      <button class="filter-btn active" data-filter="all">All</button>
      <button class="filter-btn" data-filter="running">Running</button>
    </div>

    <div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);overflow:hidden">
      <table class="ct-table">
        <thead>
          <tr>
            <th style="width:10px"></th>
            <th>Name</th>
            <th>Image</th>
            <th>Ports</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="ct-tbody">
          <tr><td colspan="6" style="padding:40px;text-align:center;color:var(--text-dim)">Loading…</td></tr>
        </tbody>
      </table>
    </div>

    <div id="ct-log-panel" style="display:none">
      <div id="ct-log-header">
        <span style="font-size:11px;font-weight:600;font-family:var(--font-mono)" id="ct-log-title">Logs</span>
        <label style="font-size:11px;color:var(--text-dim);display:flex;align-items:center;gap:4px;cursor:pointer;margin-left:8px">
          <input type="checkbox" id="ct-log-follow"> Follow
        </label>
        <button class="btn btn-sm btn-ghost" id="ct-log-clear-btn" style="margin-left:auto">Clear</button>
        <button class="btn btn-sm btn-ghost" id="ct-log-close-btn">✕</button>
      </div>
      <pre id="ct-log-body"></pre>
    </div>
  `;

  document.getElementById('ct-deploy-btn').addEventListener('click', openDeployPanel);
  document.getElementById('ct-deploy-close').addEventListener('click', closeDeployPanel);
  document.getElementById('ct-deploy-back').addEventListener('click', () => showDeployStep(1));
  document.getElementById('ct-deploy-submit').addEventListener('click', submitDeploy);
  document.getElementById('ct-refresh-btn').addEventListener('click', () => loadContainers(true));
  document.getElementById('ct-group-toggle').addEventListener('change', (e) => {
    _groupByProject = e.target.checked;
    renderContainerTable();
  });
  document.getElementById('ct-log-follow').addEventListener('change', (e) => {
    if (e.target.checked && _logContainerId) {
      openLogs(_logContainerId, _logFollow = true);
    }
  });
  document.getElementById('ct-log-clear-btn').addEventListener('click', () => {
    document.getElementById('ct-log-body').textContent = '';
  });
  document.getElementById('ct-log-close-btn').addEventListener('click', closeLogs);

  document.getElementById('ct-filter-bar').addEventListener('click', (e) => {
    const btn = e.target.closest('.filter-btn[data-filter]');
    if (!btn) return;
    _filter = btn.dataset.filter;
    document.querySelectorAll('.filter-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.filter === _filter));
    renderContainerTable();
  });

  await _refreshPublicHost();
  await loadContainers();
  _refreshTimer = setInterval(loadContainers, 15000);

  // Deep-link handler: dashboard welcome card can route here with opts.
  const opts = window.__viewOpts;
  if (opts && opts.quickdeploy === 'n8n') {
    window.__viewOpts = null;
    quickDeployN8n();
  } else if (opts && opts.openDeploy) {
    window.__viewOpts = null;
    openDeployPanel();
  }
}

// ── One-click n8n quick deploy ──────────────────────────────────────────────
//
// Called via deep-link from the dashboard welcome card. Auto-generates the
// admin password, fills every form field with sensible defaults, submits the
// deploy, and surfaces the generated credentials in the result banner so the
// user can copy them once before they're rotated out of view.

function _genPassword(length = 16) {
  // n8n requires 8+ chars, owner-setup also enforces 1 uppercase + 1 number.
  // This password protects a (potentially internet-facing) admin login —
  // crypto.getRandomValues is the right entropy source, not Math.random.
  const lower = 'abcdefghijkmnopqrstuvwxyz';
  const upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
  const digits = '23456789';
  const all = lower + upper + digits;
  const buf = new Uint32Array(length);
  crypto.getRandomValues(buf);
  const pick = (s, i) => s[buf[i] % s.length];
  // Guarantee one upper and one digit, then fill from the union alphabet.
  const chars = [pick(upper, 0), pick(digits, 1)];
  for (let i = 2; i < length; i++) chars.push(pick(all, i));
  // Fisher-Yates shuffle so the guaranteed upper/digit aren't always at 0/1.
  const shuffleBuf = new Uint32Array(length);
  crypto.getRandomValues(shuffleBuf);
  for (let i = length - 1; i > 0; i--) {
    const j = shuffleBuf[i] % (i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join('');
}

async function quickDeployN8n() {
  await openDeployPanel();
  const n8n = _deployTemplates.find(t => t.id === 'n8n');
  if (!n8n) {
    toast.error('n8n template not available');
    return;
  }
  _selectedTemplate = n8n;
  renderConfigForm();

  const generatedPassword = _genPassword(16);

  // Pre-fill every field with its default; replace the empty password with a
  // generated one. instance_name + port stay at template defaults.
  for (const f of n8n.fields) {
    const el = document.getElementById(`ct-field-${f.id}`);
    if (!el) continue;
    if (f.id === 'password') {
      el.value = generatedPassword;
      el.type = 'text';  // surface it so the user can see + copy before deploy
    } else if (f.default !== undefined && f.default !== null && f.default !== '') {
      el.value = f.default;
    }
  }

  // Surface a banner inside the deploy form noting this was auto-prefilled
  // and the password is the one thing they should save somewhere safe.
  const fieldsEl = document.getElementById('ct-deploy-fields');
  if (fieldsEl && !document.getElementById('ct-quickdeploy-banner')) {
    const banner = document.createElement('div');
    banner.id = 'ct-quickdeploy-banner';
    banner.style.cssText = 'margin-bottom:10px;padding:8px 10px;border-radius:var(--radius);background:rgba(52,211,153,0.08);border:1px solid rgba(52,211,153,0.3);font-size:11px;color:var(--text-secondary);line-height:1.5';
    banner.innerHTML = `
      <strong style="color:#34d399">Quick deploy</strong> — defaults set, password generated.
      Copy the password before clicking Deploy. You can change it now if you'd rather pick your own.
    `;
    fieldsEl.parentNode.insertBefore(banner, fieldsEl);
  }
}

export function cleanup() {
  closeLogs();
  closeDeployPanel();
  if (_refreshTimer) clearInterval(_refreshTimer);
  _refreshTimer = null;
}

// ── Data loading ────────────────────────────────────────────────────────────

async function loadContainers(showToast = false) {
  try {
    const [statusResp, listResp, projResp] = await Promise.all([
      get('/api/containers/status'),
      get('/api/containers?all=true'),
      get('/api/containers/projects'),
    ]);

    if (!statusResp.reachable) {
      renderUnavailable();
      return;
    }

    _allContainers = listResp.containers || [];
    _projects = projResp.projects || [];

    renderSummary(statusResp);
    renderFilterBar();
    renderContainerTable();

    if (showToast) toast.success('Refreshed');
  } catch (e) {
    document.getElementById('ct-tbody').innerHTML =
      `<tr><td colspan="6" style="padding:24px;color:#ff6d5a;font-size:12px">Failed to load: ${escHtml(e.message)}</td></tr>`;
  }
}

function renderUnavailable() {
  document.getElementById('ct-summary').textContent = 'Docker unavailable';
  document.getElementById('ct-tbody').innerHTML = `
    <tr><td colspan="6" style="padding:40px;text-align:center">
      <div style="color:var(--text-dim);font-size:13px;line-height:1.6">
        Docker daemon unreachable.<br>
        <span style="font-size:11px">Make sure <code style="font-family:var(--font-mono)">/var/run/docker.sock</code> is mounted on the dashboard service.</span>
      </div>
    </td></tr>
  `;
}

function renderSummary(info) {
  const el = document.getElementById('ct-summary');
  if (!el) return;
  el.innerHTML = `
    <strong style="color:#34d399">${info.running ?? 0}</strong> running ·
    <strong>${info.stopped ?? 0}</strong> stopped ·
    <strong>${info.images ?? 0}</strong> images
    ${info.docker_version ? `· Docker ${escHtml(info.docker_version)}` : ''}
  `;
}

function renderFilterBar() {
  const bar = document.getElementById('ct-filter-bar');
  if (!bar) return;

  // Remove old project filters, keep All + Running.
  bar.querySelectorAll('.filter-btn[data-project]').forEach(b => b.remove());

  for (const p of _projects) {
    const btn = document.createElement('button');
    btn.className = 'filter-btn' + (_filter === p.name ? ' active' : '');
    btn.dataset.filter = p.name;
    btn.dataset.project = '1';
    btn.textContent = `${p.name} (${p.running}/${p.total})`;
    bar.appendChild(btn);
  }
}

// ── Table rendering ─────────────────────────────────────────────────────────

function getFiltered() {
  if (_filter === 'all') return _allContainers;
  if (_filter === 'running') return _allContainers.filter(c => c.state === 'running');
  return _allContainers.filter(c => c.compose_project === _filter);
}

function renderContainerTable() {
  const tbody = document.getElementById('ct-tbody');
  if (!tbody) return;

  const filtered = getFiltered();
  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="6" style="padding:32px;text-align:center;color:var(--text-dim);font-size:12px">No containers match this filter.</td></tr>`;
    return;
  }

  if (_groupByProject) {
    renderGrouped(tbody, filtered);
  } else {
    tbody.innerHTML = filtered.map(rowHtml).join('');
    attachRowHandlers(tbody, filtered);
  }
}

function renderGrouped(tbody, containers) {
  const groups = new Map();
  for (const c of containers) {
    const key = c.compose_project || '__standalone__';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }

  let html = '';
  for (const [project, items] of groups) {
    const label = project === '__standalone__' ? 'Standalone' : project;
    const running = items.filter(c => c.state === 'running').length;
    html += `<tr><td colspan="6" class="ct-project-header">
      ${escHtml(label)}
      <span style="font-weight:400;margin-left:6px;opacity:0.6">${running}/${items.length} running</span>
    </td></tr>`;
    html += items.map(rowHtml).join('');
  }
  tbody.innerHTML = html;
  attachRowHandlers(tbody, containers);
}

function stateColor(state) {
  return {
    running: '#34d399',
    paused: '#f59e0b',
    restarting: '#60a5fa',
    exited: '#6b7280',
    dead: '#ff6d5a',
    created: '#8b5cf6',
  }[state] || '#6b7280';
}

function getPublicUrl(c) {
  for (const p of c.ports) {
    const m = p.match(/^(\d+)→/);
    if (m) return `http://${_publicHost}:${m[1]}`;
  }
  return '';
}

// HTTP-shaped port detection for the Open button.
// Returns the host-side URL for the first port whose container-side mapping
// looks like a web UI, or '' if none found.
const _HTTP_PORTS = new Set([80, 443, 3000, 4000, 4200, 5000, 5173, 6006, 7860, 8000, 8080, 8090, 8888, 9000, 9001, 11434]);

function getHttpUrl(c) {
  for (const p of c.ports) {
    // Format: "hostPort→containerPort" or "hostPort->containerPort/proto"
    const m = p.match(/^(\d+)[→>](\d+)/);
    if (!m) continue;
    const containerPort = parseInt(m[2], 10);
    if (_HTTP_PORTS.has(containerPort)) {
      return `http://${_publicHost}:${m[1]}`;
    }
  }
  // Fall back to the first published port if none matched the HTTP set.
  for (const p of c.ports) {
    const m = p.match(/^(\d+)→/);
    if (m) return `http://${_publicHost}:${m[1]}`;
  }
  return '';
}

/**
 * Returns the preferred pre-fill URL for registering a container as an
 * n8n instance. Prefers compose service name (reachable from within the
 * same Docker network) over localhost port mapping.
 */
function getRegisterUrl(c) {
  // If the container is in a compose project and has a service name,
  // use http://<service>:<internal_port> so it resolves inside Docker.
  if (c.compose_service) {
    // Try to extract the container-side port from the first port mapping.
    for (const p of c.ports) {
      // Format is "hostPort->containerPort/proto" or "hostPort→containerPort"
      const m = p.match(/^\d+[→>](\d+)/);
      if (m) return `http://${c.compose_service}:${m[1]}`;
    }
    // Fall back to default n8n port if we can't parse the mapping.
    return `http://${c.compose_service}:5678`;
  }
  // Non-compose container: use a host the dashboard's BACKEND can reach.
  // When the dashboard runs in Docker, its own localhost is the container, not
  // the host, so use host.docker.internal: it maps to the host's published port
  // AND is a recognized host alias, so the registered instance is auto-updateable
  // out of the box (no AGD_HOST_ALIASES needed).
  for (const p of c.ports) {
    const m = p.match(/^(\d+)→/);
    if (m) {
      const host = _inDocker ? 'host.docker.internal' : (_publicHost || 'localhost');
      return `http://${host}:${m[1]}`;
    }
  }
  return getPublicUrl(c);
}

function isN8nContainer(c) {
  // Match containers we deployed from the n8n template OR any container
  // whose image name contains "n8n" (e.g. n8nio/n8n, ghcr.io/n8n-io/n8n).
  if ((c.labels || {})['ageniusdesk.template'] === 'n8n') return true;
  if ((c.labels || {})['agd.type'] === 'n8n') return true;
  const img = (c.image || '').toLowerCase();
  return img.includes('n8n');
}

// Keep backward-compat alias used elsewhere in this file.
function isN8nManaged(c) {
  return isN8nContainer(c);
}

function rowHtml(c) {
  const color = stateColor(c.state);
  const isRunning = c.state === 'running';
  const ports = c.ports.slice(0, 3).join(' · ') + (c.ports.length > 3 ? ' …' : '');
  const svc = c.compose_service
    ? `<span class="ct-badge" style="background:rgba(96,165,250,0.12);color:#60a5fa">${escHtml(c.compose_service)}</span> `
    : '';
  const openUrl = getPublicUrl(c);
  const httpUrl = getHttpUrl(c);
  const hasHttp = !!httpUrl;

  // External-link SVG (Open button)
  const iconExternalLink = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>`;
  // Document-text SVG (Logs button)
  const iconDocText = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>`;

  return `
    <tr class="ct-row" data-id="${escHtml(c.id_full)}">
      <td><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color}"></span></td>
      <td>
        ${svc}<code style="font-size:12px">${escHtml(c.name)}</code>
        <div style="font-size:10px;color:var(--text-dim);margin-top:2px">${escHtml(c.id)}</div>
      </td>
      <td style="font-size:11px;color:var(--text-secondary);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(c.image)}">
        ${escHtml(c.image)}
      </td>
      <td style="font-size:11px;color:var(--text-dim);white-space:nowrap">${escHtml(ports)}</td>
      <td style="font-size:11px;white-space:nowrap;color:${color}">${escHtml(c.status)}</td>
      <td style="white-space:nowrap;position:relative">
        ${isRunning
          ? `<button class="ct-action-btn danger" data-action="stop" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}" title="Stop ${escHtml(c.name)}">
               <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
             </button>
             <button class="ct-action-btn" data-action="restart" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}" title="Restart ${escHtml(c.name)}">
               <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.48"/></svg>
             </button>`
          : `<button class="ct-action-btn primary" data-action="start" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}" title="Start ${escHtml(c.name)}">
               <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg>
             </button>`
        }
        <button class="ct-action-btn"
                data-action="open" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}"
                data-url="${escHtml(httpUrl)}"
                title="${hasHttp ? `Open ${escHtml(c.name)} in browser` : 'No web UI exposed'}"
                ${hasHttp ? '' : 'disabled'}>${iconExternalLink}</button>
        <button class="ct-action-btn" data-action="logs" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}"
                title="View logs for ${escHtml(c.name)}">${iconDocText}</button>
        <button class="ct-action-btn ct-more-btn" data-action="more" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}"
                data-n8n="${isN8nManaged(c) ? '1' : ''}" data-url="${escHtml(openUrl)}" title="More actions">⋯</button>
        <div class="ct-dropdown" id="dd-${escHtml(c.id)}" style="display:none;position:absolute;right:0;top:100%;z-index:100;background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);min-width:180px;box-shadow:0 4px 16px rgba(0,0,0,0.4)">
          <button class="ct-dd-item" data-action="inspect" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}">🔍 Inspect</button>
          <button class="ct-dd-item" data-action="update" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}">↑ Recreate (pull latest)</button>
          ${isN8nManaged(c) ? `<button class="ct-dd-item" data-action="register" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}" data-url="${escHtml(getRegisterUrl(c))}">+ Register as instance</button>` : ''}
          <button class="ct-dd-item ct-dd-item--disabled" title="Coming soon — G1 snapshot support">📦 Snapshot <span style="font-size:9px;opacity:0.5;margin-left:4px">soon</span></button>
          ${(c.labels || {})['ageniusdesk.bundle'] ? `<button class="ct-dd-item" data-action="recreate-bundle" data-id="${escHtml(c.id_full)}" data-bundle="${escHtml((c.labels || {})['ageniusdesk.bundle'])}">⟳ Recreate bundle (pull latest)</button>` : ''}
          <button class="ct-dd-item ct-dd-item--danger" data-action="destroy" data-id="${escHtml(c.id_full)}" data-name="${escHtml(c.name)}" data-managed="${(c.labels || {})['ageniusdesk.managed'] === 'true' ? '1' : ''}" data-bundle="${escHtml((c.labels || {})['ageniusdesk.bundle'] || '')}">🗑 Destroy…</button>
        </div>
      </td>
    </tr>
    <tr class="ct-register-row" id="reg-${escHtml(c.id)}" style="display:none">
      <td colspan="6" style="padding:0">
        <div style="padding:12px 14px;background:rgba(96,165,250,0.06);border-bottom:1px solid var(--border-dim)">
          <div style="font-size:11px;font-weight:600;margin-bottom:8px">Register <code>${escHtml(c.name)}</code> as an n8n instance</div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:8px;align-items:end">
            <div>
              <label style="font-size:10px;color:var(--text-dim);display:block;margin-bottom:3px">Instance name</label>
              <input id="ri-name-${escHtml(c.id)}" type="text" value="${escHtml(c.name.replace(/^agd-/, ''))}"
                style="width:100%;padding:5px 8px;font-size:12px;background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary);box-sizing:border-box">
            </div>
            <div>
              <label style="font-size:10px;color:var(--text-dim);display:block;margin-bottom:3px">URL</label>
              <input id="ri-url-${escHtml(c.id)}" type="text" value="${escHtml(openUrl)}"
                style="width:100%;padding:5px 8px;font-size:12px;background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary);box-sizing:border-box">
            </div>
            <div>
              <label style="font-size:10px;color:var(--text-dim);display:block;margin-bottom:3px">API key</label>
              <input id="ri-key-${escHtml(c.id)}" type="password" placeholder="n8n API key"
                style="width:100%;padding:5px 8px;font-size:12px;background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary);box-sizing:border-box">
            </div>
            <div style="display:flex;gap:6px">
              <button class="btn btn-sm btn-primary ct-reg-submit" data-cid="${escHtml(c.id)}">Add</button>
              <button class="btn btn-sm btn-ghost ct-reg-cancel" data-cid="${escHtml(c.id)}">✕</button>
            </div>
          </div>
          <div id="ri-err-${escHtml(c.id)}" style="font-size:11px;color:#ff6d5a;margin-top:6px"></div>
        </div>
      </td>
    </tr>
  `;
}

function attachRowHandlers(tbody, containers) {
  // Close any open dropdown when clicking elsewhere.
  const closeDropdowns = () => {
    tbody.querySelectorAll('.ct-dropdown').forEach(d => (d.style.display = 'none'));
  };
  document.addEventListener('click', closeDropdowns, { once: true });

  tbody.querySelectorAll('.ct-more-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const dd = document.getElementById(`dd-${btn.dataset.id.slice(0, 12)}`);
      if (!dd) return;
      const isOpen = dd.style.display !== 'none';
      closeDropdowns();
      if (!isOpen) {
        dd.style.display = 'block';
        document.addEventListener('click', closeDropdowns, { once: true });
      }
    });
  });

  tbody.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      const id = btn.dataset.id;
      const name = btn.dataset.name || id;
      const shortId = id.slice(0, 12);

      if (action === 'more') return; // handled above

      if (action === 'open') {
        const url = btn.dataset.url;
        if (url) window.open(url, '_blank', 'noopener,noreferrer');
        return;
      }

      if (action === 'logs') {
        openLogs(id, false, name);
        return;
      }

      if (action === 'inspect') {
        closeDropdowns();
        openInspect(id, name);
        return;
      }

      if (action === 'register') {
        closeDropdowns();
        // Pre-fill the shared Add Instance dialog and show it.
        // btn.dataset.url is the host-port URL; if no port binding use service name.
        const preUrl = btn.dataset.url || '';
        if (typeof window.__addInstance === 'function') {
          window.__addInstance();
          // Set URL and name after __addInstance clears the fields.
          const urlInput = document.getElementById('setup-url');
          const nameInput = document.getElementById('setup-name');
          if (urlInput && preUrl) urlInput.value = preUrl;
          if (nameInput && !nameInput.value) nameInput.value = name;
          // Trigger the localhost hint check if the helper is available.
          urlInput?.dispatchEvent(new Event('input', { bubbles: true }));
        }
        return;
      }

      if (action === 'update') {
        closeDropdowns();
        const confirmed = await openModal({
          title: 'Recreate container',
          body: 'This will stop the container, pull the latest image, and restart it. There will be brief downtime.',
          confirmLabel: 'Recreate',
          cancelLabel: 'Cancel',
          danger: true,
          triggerEl: btn,
        });
        if (confirmed) await startRecreate(id, name);
        return;
      }

      if (action === 'destroy') {
        closeDropdowns();
        await destroyContainer(id, name, btn.dataset.managed === '1', btn, btn.dataset.bundle || '');
        return;
      }

      if (action === 'recreate-bundle') {
        closeDropdowns();
        const bundleId = btn.dataset.bundle;
        const confirmed = await openModal({
          title: 'Recreate bundle',
          body: `Pull the latest image for every member of <code>${bundleId}</code> and recreate them in dependency order. Data volumes are preserved.`,
          confirmLabel: 'Recreate bundle',
          cancelLabel: 'Cancel',
          danger: false,
          triggerEl: btn,
        });
        if (confirmed) await startBundleRecreate(bundleId);
        return;
      }

      // start / stop / restart / pause / unpause
      btn.disabled = true;
      try {
        await post(`/api/containers/${encodeURIComponent(id)}/${action}`, {});
        toast.success(`${action.charAt(0).toUpperCase() + action.slice(1)} sent to ${name}`);
        setTimeout(() => loadContainers(), 800);
      } catch (err) {
        toast.error(`${action} failed: ${err.message}`);
        btn.disabled = false;
      }
    });
  });

  // Register-instance form handlers
  tbody.querySelectorAll('.ct-reg-submit').forEach(btn => {
    btn.addEventListener('click', () => submitRegisterInstance(btn.dataset.cid));
  });
  tbody.querySelectorAll('.ct-reg-cancel').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = document.getElementById(`reg-${btn.dataset.cid}`);
      if (row) row.style.display = 'none';
    });
  });
}

// ── Inspect ───────────────────────────────────────────────────────────────────

async function openInspect(id, name) {
  // Show a loading modal while we fetch inspect data.
  let inspectJson = 'Loading…';
  try {
    const resp = await fetch(`/api/containers/${encodeURIComponent(id)}/inspect`);
    if (!resp.ok) throw new Error(resp.statusText);
    const data = await resp.json();
    inspectJson = JSON.stringify(data, null, 2);
  } catch (err) {
    inspectJson = `Failed to load inspect data: ${err.message}`;
  }

  const bodyEl = document.createElement('div');
  bodyEl.style.cssText = 'max-height:60vh;overflow:auto';
  const pre = document.createElement('pre');
  pre.style.cssText = `font-family:var(--font-mono);font-size:11px;line-height:1.5;
    color:#c9d1d9;background:#0a0d14;padding:12px;border-radius:4px;
    white-space:pre;overflow-x:auto;margin:0`;
  pre.textContent = inspectJson;
  bodyEl.appendChild(pre);

  await openModal({
    title: `Inspect: ${name}`,
    body: bodyEl,
    confirmLabel: 'Close',
    cancelLabel: null,
  });
}

// ── Destroy ───────────────────────────────────────────────────────────────────

async function destroyContainer(id, name, isManaged, triggerEl, bundleId = '') {
  // Build the body element with optional volume-delete checkbox.
  const bodyEl = document.createElement('div');

  const descEl = document.createElement('p');
  descEl.style.cssText = 'margin:0 0 12px;color:var(--text-secondary);font-size:14px;line-height:1.5';
  if (bundleId) {
    descEl.innerHTML = `Container <code>${escHtml(name)}</code> is a member of bundle <code>${escHtml(bundleId)}</code>. You can destroy this container alone, or the whole bundle.`;
  } else {
    descEl.textContent = `Permanently remove container "${name}"? This cannot be undone.`;
  }
  bodyEl.appendChild(descEl);

  // Bundle escalation checkbox (cascade destroy).
  let bundleCheckbox = null;
  if (bundleId) {
    const blabel = document.createElement('label');
    blabel.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-secondary);cursor:pointer;user-select:none;margin-bottom:8px';
    bundleCheckbox = document.createElement('input');
    bundleCheckbox.type = 'checkbox';
    bundleCheckbox.id = '_destroy_bundle_chk';
    bundleCheckbox.style.cssText = 'margin:0;flex-shrink:0';
    bundleCheckbox.checked = true;  // Default: destroy whole bundle.
    const blabelText = document.createElement('span');
    blabelText.innerHTML = `Destroy <strong>entire bundle</strong> (recommended)`;
    blabel.appendChild(bundleCheckbox);
    blabel.appendChild(blabelText);
    bodyEl.appendChild(blabel);
  }

  let volumeCheckbox = null;
  if (isManaged || bundleId) {
    const label = document.createElement('label');
    label.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-secondary);cursor:pointer;user-select:none';
    volumeCheckbox = document.createElement('input');
    volumeCheckbox.type = 'checkbox';
    volumeCheckbox.id = '_destroy_volumes_chk';
    volumeCheckbox.style.cssText = 'margin:0;flex-shrink:0';
    const labelText = document.createElement('span');
    labelText.textContent = 'Also delete associated volumes (all data will be lost)';
    label.appendChild(volumeCheckbox);
    label.appendChild(labelText);
    bodyEl.appendChild(label);
  }

  const confirmed = await openModal({
    title: bundleId ? 'Destroy bundle member' : 'Destroy container',
    body: bodyEl,
    confirmLabel: 'Destroy',
    cancelLabel: 'Cancel',
    danger: true,
    triggerEl,
  });

  if (!confirmed) return;

  const removeVolumes = volumeCheckbox?.checked ?? false;
  const cascadeBundle = bundleCheckbox?.checked ?? false;

  try {
    const qs = removeVolumes ? '?remove_volumes=true' : '';
    const url = cascadeBundle
      ? `/api/containers/bundle/${encodeURIComponent(bundleId)}${qs}`
      : `/api/containers/${encodeURIComponent(id)}${qs}`;
    const r = await fetch(url, { method: 'DELETE' });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || r.statusText);
    }
    const data = await r.json().catch(() => ({}));
    const evicted = data.instances_removed || [];
    const volSuffix = removeVolumes ? ' (volumes removed)' : '';
    if (cascadeBundle) {
      const count = (data.removed || []).length;
      toast.success(`Bundle ${bundleId} destroyed (${count} container${count === 1 ? '' : 's'})${volSuffix}`);
    } else {
      toast.success(`${name} destroyed${volSuffix}`);
    }
    if (evicted.length) {
      toast.warning(`Instance${evicted.length > 1 ? 's' : ''} removed: ${evicted.join(', ')}`);
    }
    setTimeout(() => loadContainers(), 500);
  } catch (err) {
    toast.error(`Destroy failed: ${err.message}`);
  }
}

async function startBundleRecreate(bundleId) {
  // Open the deploy panel at step 3 (progress) and wire up the bundle SSE stream.
  const panel = document.getElementById('ct-deploy-panel');
  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  showDeployStep(3);

  const stepsList = document.getElementById('ct-deploy-steps-list');
  stepsList.innerHTML = '';
  document.getElementById('ct-deploy-result').style.display = 'none';
  document.getElementById('ct-deploy-error-box').style.display = 'none';

  let deployId;
  try {
    const res = await post(`/api/containers/bundle/${encodeURIComponent(bundleId)}/recreate`, {});
    deployId = res.deploy_id;
  } catch (err) {
    document.getElementById('ct-deploy-error-box').textContent = `Recreate failed: ${err.message}`;
    document.getElementById('ct-deploy-error-box').style.display = 'block';
    return;
  }

  // Reuse the same SSE consumer as deploy by simulating its event loop.
  _deploySource = new EventSource(`/api/containers/deploy/${deployId}/progress`);
  _deploySource.onmessage = (e) => {
    let item;
    try { item = JSON.parse(e.data); } catch { return; }
    if (item === null) { _deploySource.close(); _deploySource = null; return; }
    if (item.event === 'step') {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:8px;padding:4px 0';
      row.innerHTML = `<span style="color:#34d399;flex-shrink:0">✓</span><span>${escHtml(item.message)}</span>`;
      stepsList.appendChild(row);
      return;
    }
    if (item.event === 'bundle_step') {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 0 4px;margin-top:6px;border-top:1px solid var(--border-dim);font-size:11px;font-weight:600;color:#60a5fa';
      row.innerHTML = `<span style="background:rgba(96,165,250,0.15);color:#60a5fa;padding:2px 7px;border-radius:8px;font-size:10px">${item.current}/${item.total}</span><span>Container <code>${escHtml(item.container_name)}</code></span>`;
      stepsList.appendChild(row);
      return;
    }
    if (item.event === 'done') {
      const resultBox = document.getElementById('ct-deploy-result');
      document.getElementById('ct-deploy-result-body').innerHTML =
        `Bundle <code>${escHtml(bundleId)}</code> recreated.`;
      document.getElementById('ct-deploy-open-link').style.display = 'none';
      document.getElementById('ct-deploy-register-btn').style.display = 'none';
      document.getElementById('ct-deploy-another-btn').onclick = closeDeployPanel;
      document.getElementById('ct-deploy-another-btn').textContent = 'Done';
      resultBox.style.display = 'block';
      setTimeout(() => loadContainers(), 1500);
      return;
    }
    if (item.event === 'error') {
      document.getElementById('ct-deploy-error-box').textContent = item.message;
      document.getElementById('ct-deploy-error-box').style.display = 'block';
      _deploySource.close();
      _deploySource = null;
    }
  };
}

// ── Update (recreate) ─────────────────────────────────────────────────────────

async function startRecreate(id, name) {
  // Open the deploy panel at step 3 (progress) and wire up the SSE stream.
  const panel = document.getElementById('ct-deploy-panel');
  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  showDeployStep(3);

  const stepsList = document.getElementById('ct-deploy-steps-list');
  stepsList.innerHTML = '';
  document.getElementById('ct-deploy-result').style.display = 'none';
  document.getElementById('ct-deploy-error-box').style.display = 'none';

  let deployId;
  try {
    const res = await post(`/api/containers/${encodeURIComponent(id)}/recreate`, {});
    deployId = res.deploy_id;
  } catch (err) {
    document.getElementById('ct-deploy-error-box').textContent = `Update failed: ${err.message}`;
    document.getElementById('ct-deploy-error-box').style.display = 'block';
    return;
  }

  _deploySource = new EventSource(`/api/containers/deploy/${deployId}/progress`);

  _deploySource.onmessage = (e) => {
    let item;
    try { item = JSON.parse(e.data); } catch { return; }
    if (item === null) { _deploySource.close(); _deploySource = null; return; }

    if (item.event === 'step') {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:8px;padding:4px 0';
      row.innerHTML = `<span style="color:#34d399;flex-shrink:0">✓</span><span>${escHtml(item.message)}</span>`;
      stepsList.appendChild(row);
      return;
    }
    if (item.event === 'done') {
      const resultBox = document.getElementById('ct-deploy-result');
      document.getElementById('ct-deploy-result-body').innerHTML =
        `<strong>${escHtml(name)}</strong> updated and restarted.`;
      document.getElementById('ct-deploy-open-link').style.display = 'none';
      document.getElementById('ct-deploy-register-btn').style.display = 'none';
      document.getElementById('ct-deploy-another-btn').onclick = closeDeployPanel;
      document.getElementById('ct-deploy-another-btn').textContent = 'Done';
      resultBox.style.display = 'block';
      setTimeout(() => loadContainers(), 1500);
      return;
    }
    if (item.event === 'error') {
      document.getElementById('ct-deploy-error-box').textContent = item.message;
      document.getElementById('ct-deploy-error-box').style.display = 'block';
      _deploySource.close(); _deploySource = null;
    }
  };

  _deploySource.onerror = () => {
    document.getElementById('ct-deploy-error-box').textContent = 'Lost connection to update stream.';
    document.getElementById('ct-deploy-error-box').style.display = 'block';
    _deploySource = null;
  };
}

// ── Register as n8n instance ──────────────────────────────────────────────────

async function submitRegisterInstance(shortId) {
  const nameEl = document.getElementById(`ri-name-${shortId}`);
  const urlEl  = document.getElementById(`ri-url-${shortId}`);
  const keyEl  = document.getElementById(`ri-key-${shortId}`);
  const errEl  = document.getElementById(`ri-err-${shortId}`);

  const name = nameEl?.value.trim();
  const url  = urlEl?.value.trim();
  const key  = keyEl?.value.trim();

  if (!name || !url || !key) {
    if (errEl) errEl.textContent = 'Name, URL and API key are all required.';
    return;
  }
  if (errEl) errEl.textContent = 'Connecting…';

  try {
    const res = await post('/instances', { name, url, api_key: key, color: '', login_url: url });
    if (res.success) {
      toast.success(`Instance "${name}" added to AgeniusDesk`);
      const row = document.getElementById(`reg-${shortId}`);
      if (row) row.style.display = 'none';
    } else {
      if (errEl) errEl.textContent = res.detail || 'Failed to register instance.';
    }
  } catch (err) {
    if (errEl) errEl.textContent = err.message;
  }
}

// ── Log panel ───────────────────────────────────────────────────────────────

function openLogs(containerId, follow = false, name = containerId) {
  closeLogs();

  _logContainerId = containerId;
  _logFollow = follow;

  const panel = document.getElementById('ct-log-panel');
  const title = document.getElementById('ct-log-title');
  const body = document.getElementById('ct-log-body');
  const followChk = document.getElementById('ct-log-follow');

  title.textContent = name;
  body.textContent = '';
  followChk.checked = follow;
  panel.style.display = 'flex';

  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  const url = `/api/containers/${encodeURIComponent(containerId)}/logs?tail=300${follow ? '&follow=true' : ''}`;
  _logSource = new EventSource(url);
  let lineCount = 0;

  _logSource.onmessage = (e) => {
    try {
      const line = JSON.parse(e.data);
      if (line === '__END__') {
        _logSource.close();
        _logSource = null;
        appendLog('[stream ended]', '#6b7280');
        return;
      }
      appendLog(line);
      lineCount++;
      // Cap display at 2000 lines to avoid DOM bloat.
      if (lineCount > 2000) {
        const lines = body.textContent.split('\n');
        body.textContent = lines.slice(-1500).join('\n');
        lineCount = 1500;
      }
    } catch { /* ignore malformed */ }
  };

  _logSource.onerror = () => {
    appendLog('[connection lost]', '#ff6d5a');
    _logSource = null;
  };
}

function appendLog(text, color = '') {
  const body = document.getElementById('ct-log-body');
  if (!body) return;
  const line = document.createElement('span');
  if (color) line.style.color = color;
  line.textContent = text + '\n';
  body.appendChild(line);
  if (body.scrollTop + body.clientHeight >= body.scrollHeight - 40) {
    body.scrollTop = body.scrollHeight;
  }
}

function closeLogs() {
  if (_logSource) {
    _logSource.close();
    _logSource = null;
  }
  _logContainerId = null;
  const panel = document.getElementById('ct-log-panel');
  if (panel) panel.style.display = 'none';
}

// ── Deploy panel ─────────────────────────────────────────────────────────────

async function openDeployPanel() {
  const panel = document.getElementById('ct-deploy-panel');
  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  showDeployStep(1);
  document.getElementById('ct-quickdeploy-banner')?.remove();

  if (!_deployTemplates.length) {
    try {
      const data = await get('/api/containers/templates');
      _deployTemplates = data.templates || [];
    } catch { _deployTemplates = []; }
  }
  renderTemplatePicker();
}

function closeDeployPanel() {
  const panel = document.getElementById('ct-deploy-panel');
  if (panel) panel.style.display = 'none';
  if (_deploySource) { _deploySource.close(); _deploySource = null; }
  _selectedTemplate = null;
  document.getElementById('ct-quickdeploy-banner')?.remove();
}

function showDeployStep(n) {
  [1, 2, 3].forEach(i => {
    const el = document.getElementById(`ct-deploy-step${i}`);
    if (el) el.style.display = i === n ? 'block' : 'none';
  });
}

const _CATEGORY_ORDER = ['automation', 'ai', 'database', 'storage', 'monitoring', 'community'];
const _CATEGORY_LABELS = {
  automation: 'Automation',
  ai: 'AI & ML',
  database: 'Databases',
  storage: 'Storage',
  monitoring: 'Monitoring',
  community: 'Community',
};

function renderTemplatePicker() {
  const grid = document.getElementById('ct-template-grid');
  if (!grid) return;

  if (!_deployTemplates.length) {
    grid.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:8px">No templates available.</div>';
    return;
  }

  // Group by category.
  const groups = new Map();
  for (const t of _deployTemplates) {
    const cat = t.category || 'community';
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(t);
  }

  // Sort categories by preferred order, then alphabetical for unknowns.
  const sortedCats = [...groups.keys()].sort((a, b) => {
    const ia = _CATEGORY_ORDER.indexOf(a), ib = _CATEGORY_ORDER.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });

  let html = '';
  for (const cat of sortedCats) {
    const label = _CATEGORY_LABELS[cat] || cat.charAt(0).toUpperCase() + cat.slice(1);
    html += `<div class="ct-tmpl-cat">${escHtml(label)}</div><div class="ct-tmpl-grid-row">`;
    for (const t of groups.get(cat)) {
      const isRunning = _allContainers.some(
        c => (c.labels || {})['ageniusdesk.template'] === t.id && c.state === 'running'
      );
      html += `
        <button class="ct-tmpl-tile" data-tid="${escHtml(t.id)}">
          ${isRunning ? `<div class="ct-tmpl-tile-running"><span style="width:6px;height:6px;border-radius:50%;background:#34d399;display:inline-block"></span> Running</div>` : ''}
          <div class="ct-tmpl-tile-icon">${escHtml(t.icon)}</div>
          <div class="ct-tmpl-tile-name">${escHtml(t.name)}</div>
          <div class="ct-tmpl-tile-footer">
            <div class="ct-tmpl-tile-desc" title="${escHtml(t.description)}">${escHtml(t.description)}</div>
            <div class="ct-tmpl-tile-tags">
              ${t.bundle ? '<span class="ct-tmpl-badge" style="background:rgba(96,165,250,0.18);color:#60a5fa">bundle</span>' : ''}
              ${t.community ? '<span class="ct-tmpl-badge">community</span>' : ''}
              ${t.documentation_url ? `<a class="ct-tmpl-docs" href="${escHtml(t.documentation_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">📖 Docs</a>` : ''}
            </div>
          </div>
        </button>
      `;
    }
    html += `</div>`;
  }

  // Community drop-in hint (compact single line at bottom).
  const hasCommunity = groups.has('community');
  html += `
    <div style="margin-top:10px;padding:7px 8px;font-size:10px;color:var(--text-dim);
                border-top:1px solid var(--border-dim);line-height:1.5">
      ${hasCommunity ? '📦' : '💡'}
      Drop any <code style="font-family:var(--font-mono)">.json</code> into
      <code style="font-family:var(--font-mono)">/app/data/templates/</code> — appears instantly.
      <a href="https://github.com/Mfrostbutter/ageniusdesk-ce/tree/main/docs/community-templates"
         target="_blank" rel="noopener" style="color:#60a5fa;text-decoration:none">Schema ↗</a>
    </div>
  `;

  grid.innerHTML = html;

  grid.querySelectorAll('.ct-tmpl-tile').forEach(tile => {
    tile.addEventListener('click', () => {
      _selectedTemplate = _deployTemplates.find(t => t.id === tile.dataset.tid);
      if (_selectedTemplate) renderConfigForm();
    });
  });
}

function renderConfigForm() {
  if (!_selectedTemplate) return;

  document.getElementById('ct-deploy-form-title').textContent =
    `Configure ${_selectedTemplate.name}`;

  const fieldsEl = document.getElementById('ct-deploy-fields');
  fieldsEl.innerHTML = _selectedTemplate.fields.map(f => {
    const inputId = `ct-field-${f.id}`;
    let inputHtml = '';
    if (f.type === 'select') {
      inputHtml = `<select id="${inputId}" style="width:100%;padding:6px 8px;font-size:12px;background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary)">
        ${f.options.map(o => `<option value="${escHtml(o)}" ${o === String(f.default) ? 'selected' : ''}>${escHtml(o)}</option>`).join('')}
      </select>`;
    } else {
      inputHtml = `<input id="${inputId}" type="${f.type}" value="${escHtml(String(f.default))}"
        placeholder="${escHtml(f.placeholder)}"
        style="width:100%;padding:6px 8px;font-size:12px;background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary);box-sizing:border-box"
        ${f.required ? 'required' : ''} autocomplete="off">`;
    }
    return `
      <div>
        <label for="${inputId}" style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:4px">
          ${escHtml(f.label)}${f.required ? '' : ' <span style="opacity:0.5">(optional)</span>'}
        </label>
        ${inputHtml}
        ${f.hint ? `<div style="font-size:10px;color:var(--text-dim);margin-top:3px">${escHtml(f.hint)}</div>` : ''}
      </div>
    `;
  }).join('');

  // Suggest next free port by looking at already-used ones.
  const portField = document.getElementById('ct-field-port');
  if (portField) {
    const usedPorts = new Set(
      _allContainers.flatMap(c => c.ports)
        .map(p => parseInt(p.split('→')[0]))
        .filter(Boolean)
    );
    let suggested = parseInt(portField.value) || 5678;
    while (usedPorts.has(suggested) || CHROME_UNSAFE_PORTS.has(suggested)) suggested++;
    portField.value = suggested;

    // Live warning if the operator types a browser-blocked port.
    let warn = document.getElementById('ct-port-warning');
    if (!warn) {
      warn = document.createElement('div');
      warn.id = 'ct-port-warning';
      warn.style.cssText = 'font-size:10px;color:#fbbf24;margin-top:3px;display:none';
      portField.parentElement.appendChild(warn);
    }
    const checkPort = () => {
      const p = parseInt(portField.value);
      if (CHROME_UNSAFE_PORTS.has(p)) {
        warn.textContent = `Port ${p} is blocked by Chrome and most browsers (ERR_UNSAFE_PORT). Try 5678 or 8080.`;
        warn.style.display = 'block';
        portField.style.borderColor = '#fbbf24';
      } else {
        warn.style.display = 'none';
        portField.style.borderColor = '';
      }
    };
    portField.addEventListener('input', checkPort);
    checkPort();
  }

  document.getElementById('ct-deploy-err').textContent = '';
  showDeployStep(2);
}

async function submitDeploy() {
  if (!_selectedTemplate) return;

  const errEl = document.getElementById('ct-deploy-err');
  const fields = {};
  let valid = true;

  for (const f of _selectedTemplate.fields) {
    const el = document.getElementById(`ct-field-${f.id}`);
    if (!el) continue;
    const val = el.value.trim();
    if (f.required && !val) {
      el.style.borderColor = '#ff6d5a';
      valid = false;
    } else {
      el.style.borderColor = '';
      fields[f.id] = f.type === 'number' ? Number(val) : val;
    }
  }

  if (!valid) {
    errEl.textContent = 'Please fill in all required fields.';
    return;
  }

  // Basic password length check for n8n.
  if (fields.password && fields.password.length < 8) {
    errEl.textContent = 'Password must be at least 8 characters.';
    return;
  }

  // Reject browser-blocked host ports up front (these services open in a browser).
  if (fields.port != null && CHROME_UNSAFE_PORTS.has(Number(fields.port))) {
    errEl.textContent = `Port ${fields.port} is blocked by browsers (ERR_UNSAFE_PORT). Choose a different host port, e.g. 5678 or 8080.`;
    return;
  }

  errEl.textContent = '';
  showDeployStep(3);

  const stepsList = document.getElementById('ct-deploy-steps-list');
  stepsList.innerHTML = '';
  document.getElementById('ct-deploy-result').style.display = 'none';
  document.getElementById('ct-deploy-error-box').style.display = 'none';

  let deployId;
  try {
    const res = await post('/api/containers/deploy', {
      template_id: _selectedTemplate.id,
      fields,
    });
    deployId = res.deploy_id;
  } catch (err) {
    document.getElementById('ct-deploy-error-box').textContent = `Deploy failed: ${err.message}`;
    document.getElementById('ct-deploy-error-box').style.display = 'block';
    return;
  }

  // Connect SSE and stream progress.
  _deploySource = new EventSource(`/api/containers/deploy/${deployId}/progress`);

  _deploySource.onmessage = (e) => {
    let item;
    try { item = JSON.parse(e.data); } catch { return; }

    if (item === null) {
      _deploySource.close();
      _deploySource = null;
      return;
    }

    if (item.event === 'step') {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:8px;padding:4px 0';
      row.innerHTML = `
        <span style="color:#34d399;flex-shrink:0">✓</span>
        <span>${escHtml(item.message)}${item.detail ? `<div style="color:var(--text-dim);font-size:10px;margin-top:1px">${escHtml(item.detail)}</div>` : ''}</span>
      `;
      stepsList.appendChild(row);
      return;
    }

    if (item.event === 'bundle_step') {
      // Bundle progress framing: "Container 2 of 3: redis"
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 0 4px;margin-top:6px;border-top:1px solid var(--border-dim);font-size:11px;font-weight:600;color:#60a5fa';
      row.innerHTML = `
        <span style="background:rgba(96,165,250,0.15);color:#60a5fa;padding:2px 7px;border-radius:8px;font-size:10px">${item.current}/${item.total}</span>
        <span>Container <code>${escHtml(item.container_name)}</code></span>
      `;
      stepsList.appendChild(row);
      return;
    }

    if (item.event === 'done') {
      const resultBox = document.getElementById('ct-deploy-result');
      const isBundle = item.bundle === true;
      if (isBundle) {
        const memberRows = (item.containers || []).map(m => `
          <div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px">
            <span style="background:${m.role === 'primary' ? 'rgba(52,211,153,0.15)' : 'rgba(255,255,255,0.05)'};color:${m.role === 'primary' ? '#34d399' : 'var(--text-dim)'};padding:1px 6px;border-radius:8px;font-size:9px;font-weight:600;text-transform:uppercase">${escHtml(m.role)}</span>
            <code>${escHtml(m.name)}</code>
            ${m.url ? `<a href="${escHtml(m.url)}" target="_blank" style="color:var(--accent);font-size:11px">${escHtml(m.url)} &rarr;</a>` : ''}
          </div>
        `).join('');
        document.getElementById('ct-deploy-result-body').innerHTML = `
          Bundle <code>${escHtml(item.bundle_id)}</code> deployed (${(item.containers || []).length} containers).<br>
          ${item.primary_url ? `Access at <strong>${escHtml(item.primary_url)}</strong>` : ''}
          <div style="margin-top:10px;padding:8px;background:var(--bg-input);border-radius:var(--radius)">${memberRows}</div>
        `;
      } else {
        document.getElementById('ct-deploy-result-body').innerHTML = `
          Container <code>${escHtml(item.container_name)}</code> is running.<br>
          ${item.url ? `Access at <strong>${escHtml(item.url)}</strong>` : ''}
        `;
      }
      const openLink = document.getElementById('ct-deploy-open-link');
      if (item.url) {
        openLink.href = item.url;
        openLink.style.display = '';
      } else {
        openLink.style.display = 'none';
      }

      // "Add to AgeniusDesk" only makes sense for n8n. Hide for bundles and
      // for non-n8n single-container templates.
      const registerBtn = document.getElementById('ct-deploy-register-btn');
      const tmplId = (isBundle ? item.template_id : null) || (_selectedTemplate && _selectedTemplate.id);
      registerBtn.style.display = (tmplId === 'n8n' && !isBundle) ? '' : 'none';

      registerBtn.onclick = () => {
        const instanceUrl = item.url || '';
        const bodyEl = document.createElement('div');
        bodyEl.style.cssText = 'font-size:13px;line-height:1.7';
        bodyEl.innerHTML = `
          <p style="margin:0 0 12px;color:var(--text-secondary)">
            Before you can add this instance, n8n needs a one-time first-run setup:
          </p>
          <ol style="margin:0 0 14px;padding-left:18px;display:flex;flex-direction:column;gap:8px">
            <li>
              <strong>Open the instance</strong> and complete the owner account setup (email + password).
              ${instanceUrl ? `<br><a href="${escHtml(instanceUrl)}" target="_blank" style="color:var(--accent)">${escHtml(instanceUrl)} &rarr;</a>` : ''}
            </li>
            <li>
              Inside n8n: <strong>Settings &rsaquo; n8n API</strong>, enable the API and create an API key. Copy it.
            </li>
            <li>
              Return to <strong>Settings &rsaquo; Instances</strong> here and click Add Instance — paste the URL and key.
            </li>
          </ol>
          ${instanceUrl ? `<p style="margin:0;font-size:11px;color:var(--text-dim)">URL: <code>${escHtml(instanceUrl)}</code></p>` : ''}
        `;
        openModal({
          title: 'Add to AgeniusDesk — setup required',
          body: bodyEl,
          confirmLabel: 'Go to Instances settings',
          cancelLabel: 'Close',
        }).then(confirmed => {
          if (confirmed) {
            window.__nav('settings');
            setTimeout(() => { if (window.__settingsTab) window.__settingsTab('instances'); }, 300);
          }
        });
      };

      document.getElementById('ct-deploy-another-btn').onclick = () => {
        _selectedTemplate = null;
        openDeployPanel();
      };

      resultBox.style.display = 'block';
      // Refresh container list to show the new one.
      setTimeout(() => loadContainers(), 1500);
      return;
    }

    if (item.event === 'error') {
      const errBox = document.getElementById('ct-deploy-error-box');
      if (item.partial) {
        // Bundle deploy failed mid-stream. Show what's running, what failed,
        // and offer destroy + redeploy as the recovery path.
        errBox.innerHTML = `
          <div style="font-weight:600;margin-bottom:4px">Bundle partially deployed</div>
          <div style="font-size:11px;margin-bottom:6px">${escHtml(item.message)}</div>
          <div style="font-size:11px;color:var(--text-dim);line-height:1.6">
            Bundle: <code>${escHtml(item.bundle_id || '')}</code><br>
            Started: ${(item.started || []).map(s => `<code>${escHtml(s)}</code>`).join(', ') || '(none)'}<br>
            Failed: <code>${escHtml(item.failed || '')}</code><br>
            Remaining: ${(item.remaining || []).map(s => `<code>${escHtml(s)}</code>`).join(', ') || '(none)'}
          </div>
          <div style="margin-top:8px;display:flex;gap:6px">
            <button class="btn btn-sm btn-ghost" id="ct-bundle-destroy-partial">Destroy partial bundle</button>
          </div>
        `;
        const destroyBtn = document.getElementById('ct-bundle-destroy-partial');
        if (destroyBtn) {
          destroyBtn.onclick = async () => {
            try {
              await fetch(`/api/containers/bundle/${encodeURIComponent(item.bundle_id)}?remove_volumes=true`, { method: 'DELETE' });
              toast.success('Partial bundle destroyed');
              setTimeout(() => loadContainers(), 500);
              errBox.style.display = 'none';
            } catch (e) {
              toast.error(`Destroy failed: ${e.message}`);
            }
          };
        }
      } else {
        errBox.textContent = item.message;
      }
      errBox.style.display = 'block';
      _deploySource.close();
      _deploySource = null;
    }
  };

  _deploySource.onerror = () => {
    document.getElementById('ct-deploy-error-box').textContent = 'Connection to deploy stream lost.';
    document.getElementById('ct-deploy-error-box').style.display = 'block';
    _deploySource = null;
  };
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function escHtml(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}
