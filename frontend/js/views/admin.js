/**
 * Admin view — dashboard users + n8n instance users.
 */

import { get, post, del } from '../api.js';
import * as toast from '../components/toast.js';

let activeTab = 'n8n-users';

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">Admin</h2>
    </div>

    <!-- Tabs -->
    <div style="display:flex;gap:2px;margin-bottom:20px;border-bottom:1px solid var(--border-dim);padding-bottom:0">
      <button class="tab-btn active" data-tab="n8n-users" onclick="window.__adminTab('n8n-users')">n8n Instance Users</button>
      <button class="tab-btn" data-tab="dashboard-users" onclick="window.__adminTab('dashboard-users')">Dashboard Users</button>
      <button class="tab-btn" data-tab="system" onclick="window.__adminTab('system')">System</button>
    </div>

    <div id="admin-tab-content"></div>
  `;

  window.__adminTab = switchTab;
  switchTab(activeTab);
}

function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));

  const el = document.getElementById('admin-tab-content');
  if (tab === 'n8n-users') renderN8nUsers(el);
  else if (tab === 'dashboard-users') renderDashboardUsers(el);
  else if (tab === 'system') renderSystem(el);
}

// ── n8n Instance Users ──────────────────────────────────────────────────────

async function renderN8nUsers(el) {
  el.innerHTML = `
    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Users on Active Instance</span>
          <button class="btn btn-sm btn-primary" id="invite-n8n-btn">Invite User</button>
        </div>
        <div id="n8n-users-list"><div class="spinner"></div></div>
      </div>

      <div class="card hidden" id="invite-n8n-card">
        <div class="card-header">
          <span class="card-title">Invite to n8n</span>
          <button class="btn btn-sm btn-ghost" onclick="document.getElementById('invite-n8n-card').classList.add('hidden')">&times;</button>
        </div>
        <p style="font-size:12px;color:var(--text-secondary);margin-bottom:12px">
          The user will receive an email invitation to set up their account on the active n8n instance.
        </p>
        <form id="invite-n8n-form">
          <label>
            Email
            <input type="email" id="invite-email" placeholder="user@example.com" required>
          </label>
          <label>
            Role
            <select id="invite-role">
              <option value="global:member">Member — can view and run workflows</option>
              <option value="global:admin">Admin — full access</option>
            </select>
          </label>
          <button type="submit" class="btn btn-primary">Send Invite</button>
        </form>
      </div>
    </div>
  `;

  document.getElementById('invite-n8n-btn').addEventListener('click', () => {
    document.getElementById('invite-n8n-card').classList.remove('hidden');
  });

  document.getElementById('invite-n8n-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('invite-email').value.trim();
    const role = document.getElementById('invite-role').value;
    try {
      const result = await post('/api/n8n/users/invite', { email, role });
      if (result.success) {
        toast.success(`Invited ${email}`);
        document.getElementById('invite-n8n-card').classList.add('hidden');
        document.getElementById('invite-n8n-form').reset();
        loadN8nUsers();
      } else {
        toast.error(result.error || 'Invite failed');
      }
    } catch (e) {
      toast.error(e.message);
    }
  });

  loadN8nUsers();
}

async function loadN8nUsers() {
  const el = document.getElementById('n8n-users-list');
  if (!el) return;
  try {
    const data = await get('/api/n8n/users');
    const users = data.users || [];

    if (!users.length) {
      el.innerHTML = '<div class="empty-state"><p>No users found</p></div>';
      return;
    }

    el.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th></th></tr></thead>
      <tbody>${users.map(u => `
        <tr>
          <td style="font-weight:500">${esc(u.first_name)} ${esc(u.last_name)}</td>
          <td style="font-family:var(--font-mono);font-size:12px">${esc(u.email)}</td>
          <td><span class="pill pill-${u.role === 'global:admin' ? 'error' : 'neutral'}">${(u.role || '').replace('global:', '')}</span></td>
          <td><span class="pill pill-${u.pending ? 'warning' : 'success'}">${u.pending ? 'Pending' : 'Active'}</span></td>
          <td><button class="btn btn-sm btn-ghost btn-danger" onclick="window.__deleteN8nUser('${jsStr(u.id)}', '${jsStr(u.email)}')">Remove</button></td>
        </tr>
      `).join('')}</tbody>
    </table></div>`;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Failed to load: ${esc(e.message)}</p></div>`;
  }
}

window.__deleteN8nUser = async function(userId, email) {
  if (!confirm(`Remove "${email}" from this n8n instance? Their workflows will need to be transferred.`)) return;
  try {
    const result = await del(`/api/n8n/users/${userId}`);
    if (result.success) {
      toast.success(`Removed ${email}`);
      loadN8nUsers();
    } else {
      toast.error(result.error || 'Failed');
    }
  } catch (e) {
    toast.error(e.message);
  }
};

// ── Dashboard Users ─────────────────────────────────────────────────────────

async function renderDashboardUsers(el) {
  el.innerHTML = `
    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Dashboard Access</span>
          <button class="btn btn-sm btn-primary" id="add-dash-user-btn">Add User</button>
        </div>
        <p style="font-size:12px;color:var(--text-secondary);margin-bottom:12px">
          Control who can access this dashboard. Without users, the dashboard is open access.
        </p>
        <div id="dash-users-list"><div class="spinner"></div></div>
      </div>

      <div class="card hidden" id="add-dash-user-card">
        <div class="card-header">
          <span class="card-title">Add Dashboard User</span>
          <button class="btn btn-sm btn-ghost" onclick="document.getElementById('add-dash-user-card').classList.add('hidden')">&times;</button>
        </div>
        <form id="add-dash-user-form">
          <label>
            Username
            <input type="text" id="dash-username" placeholder="username" required pattern="[a-zA-Z0-9_-]+" title="Letters, numbers, hyphens, underscores">
          </label>
          <label>
            Display Name
            <input type="text" id="dash-displayname" placeholder="John Doe">
          </label>
          <label>
            Role
            <select id="dash-role">
              <option value="viewer">Viewer — read-only access</option>
              <option value="operator">Operator — can trigger workflows</option>
              <option value="admin">Admin — full access</option>
            </select>
          </label>
          <label>
            Password
            <input type="password" id="dash-password" placeholder="Min 6 characters" required minlength="6">
          </label>
          <button type="submit" class="btn btn-primary">Create User</button>
        </form>
      </div>
    </div>
  `;

  document.getElementById('add-dash-user-btn').addEventListener('click', () => {
    document.getElementById('add-dash-user-card').classList.remove('hidden');
  });

  document.getElementById('add-dash-user-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const user = {
      username: document.getElementById('dash-username').value.trim(),
      display_name: document.getElementById('dash-displayname').value.trim(),
      role: document.getElementById('dash-role').value,
      password: document.getElementById('dash-password').value,
    };
    try {
      await post('/api/admin/users', user);
      toast.success(`User "${user.username}" created`);
      document.getElementById('add-dash-user-card').classList.add('hidden');
      document.getElementById('add-dash-user-form').reset();
      loadDashUsers();
    } catch (e) {
      toast.error(e.message);
    }
  });

  loadDashUsers();
}

async function loadDashUsers() {
  const el = document.getElementById('dash-users-list');
  if (!el) return;
  try {
    const data = await get('/api/admin/users');
    const users = data.users || [];

    if (!users.length) {
      el.innerHTML = '<div class="empty-state"><p>No dashboard users configured.<br>The dashboard is currently open access.</p></div>';
      return;
    }

    el.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Username</th><th>Name</th><th>Role</th><th></th></tr></thead>
      <tbody>${users.map(u => `
        <tr>
          <td style="font-family:var(--font-mono);font-size:12px">${esc(u.username)}</td>
          <td>${esc(u.display_name || '-')}</td>
          <td><span class="pill pill-${roleClass(u.role)}">${u.role}</span></td>
          <td><button class="btn btn-sm btn-ghost btn-danger" onclick="window.__deleteDashUser('${jsStr(u.username)}')">Remove</button></td>
        </tr>
      `).join('')}</tbody>
    </table></div>`;
  } catch {
    el.innerHTML = '<div class="empty-state"><p>Failed to load users</p></div>';
  }
}

window.__deleteDashUser = async function(username) {
  if (!confirm(`Remove dashboard user "${username}"?`)) return;
  try {
    await del(`/api/admin/users/${username}`);
    toast.success(`Removed "${username}"`);
    loadDashUsers();
  } catch (e) {
    toast.error(e.message);
  }
};

// ── System ──────────────────────────────────────────────────────────────────

async function renderSystem(el) {
  el.innerHTML = `
    <div class="grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">System Info</span></div>
        <div id="system-info"><div class="spinner"></div></div>
      </div>

      <div class="card" style="border-color:var(--error-glow)">
        <div class="card-header"><span class="card-title" style="color:var(--error)">Danger Zone</span></div>
        <div style="display:grid;gap:16px">
          <div>
            <p style="font-size:13px;margin-bottom:8px">Clear all stored errors from the database.</p>
            <button class="btn btn-sm btn-danger" id="clear-all-errors">Clear Error History</button>
          </div>
          <div>
            <p style="font-size:13px;margin-bottom:8px">Reset dashboard configuration. You will need to reconnect all instances.</p>
            <button class="btn btn-sm btn-danger" id="reset-config">Reset Config</button>
          </div>
        </div>
      </div>
    </div>
  `;

  document.getElementById('clear-all-errors').addEventListener('click', async () => {
    if (!confirm('Clear all error history? This cannot be undone.')) return;
    try { await del('/api/errors'); toast.success('Errors cleared'); } catch (e) { toast.error(e.message); }
  });

  document.getElementById('reset-config').addEventListener('click', async () => {
    if (!confirm('Reset dashboard configuration? You will need to reconnect all n8n instances.')) return;
    try { await post('/api/admin/reset'); toast.success('Config reset — reloading...'); setTimeout(() => location.reload(), 1000); } catch (e) { toast.error(e.message); }
  });

  // Load system info
  try {
    const [status, instances] = await Promise.all([get('/api/status'), get('/api/n8n/instances')]);
    document.getElementById('system-info').innerHTML = `
      <div style="display:grid;gap:8px;font-size:13px">
        ${infoRow('Version', status.version)}
        ${infoRow('Instances', `${(instances.instances || []).length} configured`)}
        ${infoRow('Active Instance', status.active_instance ? status.active_instance.name : 'None')}
        ${infoRow('n8n URL', status.n8n_url || 'Not configured')}
        ${infoRow('WebSocket Clients', status.websocket_clients)}
        ${infoRow('Theme', status.theme)}
        ${infoRow('Configured', `<span class="pill pill-${status.configured ? 'success' : 'error'}">${status.configured ? 'Yes' : 'No'}</span>`)}
      </div>
    `;
  } catch {
    document.getElementById('system-info').innerHTML = '<div class="empty-state"><p>Failed to load</p></div>';
  }
}

function infoRow(label, value) {
  return `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border-dim)">
    <span style="color:var(--text-secondary)">${label}</span>
    <span style="font-family:var(--font-mono);font-size:12px">${value}</span>
  </div>`;
}

function roleClass(r) {
  if (r === 'admin') return 'error';
  if (r === 'operator') return 'warning';
  return 'neutral';
}

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }


function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}
