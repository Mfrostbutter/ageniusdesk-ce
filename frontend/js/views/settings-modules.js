/**
 * Modules tab in Settings — lists built-in + community modules with status,
 * declared secrets, and install/uninstall controls.
 */

import { get, post, del } from '../api.js';
import * as toast from '../components/toast.js';

function statusBadge(status) {
  const colors = {
    loaded: '#34d399',
    missing_secrets: '#fbbf24',
    incompatible: '#ff6d5a',
    failed: '#ff6d5a',
    disabled: '#888',
  };
  const label = {
    loaded: 'Loaded',
    missing_secrets: 'Missing secrets',
    incompatible: 'Incompatible',
    failed: 'Failed',
    disabled: 'Disabled',
  }[status] || status;
  return `<span class="badge" style="background:${colors[status] || '#888'};color:#000;font-weight:600">${label}</span>`;
}

function secretRow(req, present) {
  const color = present ? '#34d399' : (req.required ? '#ff6d5a' : '#888');
  const icon = present ? '✓' : (req.required ? '✗' : '○');
  const label = req.required ? '' : ' <span style="opacity:0.6;font-size:11px">(optional)</span>';
  return `
    <div style="display:flex;align-items:center;gap:8px;font-size:12px;padding:2px 0">
      <span style="color:${color};font-weight:bold;width:14px">${icon}</span>
      <code style="color:var(--text-secondary)">$${req.key}</code>${label}
      ${req.description ? `<span style="opacity:0.6">— ${req.description}</span>` : ''}
    </div>
  `;
}

function moduleCard(entry, knownRefs) {
  const mf = entry.manifest;
  const missing = new Set(entry.missing_secrets || []);
  const isBuiltin = entry.source === 'builtin';
  const secretsHtml = (mf.secrets_required || []).map(req => {
    const present = !missing.has(req.key) && knownRefs.has(req.key);
    return secretRow(req, present);
  }).join('');

  const hasNav = !!mf.frontend?.nav;
  const navLabel = mf.frontend?.nav?.label || '';

  const navBadge = hasNav
    ? `<span style="opacity:0.6;font-size:11px;margin-left:8px">nav: ${navLabel}</span>`
    : '';

  const sourceBadge = isBuiltin
    ? '<span class="badge-core">Core</span>'
    : '<span class="badge" style="background:#60a5fa22;color:#60a5fa;font-size:10px;border:1px solid #60a5fa44">community</span>';

  const uninstallBtn = isBuiltin
    ? ''
    : `<button class="btn btn-sm btn-ghost module-uninstall-btn" data-module="${mf.id}" style="color:#ff6d5a">Uninstall</button>`;

  const navToggle = (!isBuiltin && hasNav)
    ? (() => {
        const hidden = JSON.parse(localStorage.getItem('nav-modules-hidden') || '[]');
        const isVisible = !hidden.includes(mf.id);
        return `
          <label class="module-toggle" title="Show/hide in sidebar nav" style="margin-left:auto">
            <input type="checkbox" ${isVisible ? 'checked' : ''} data-module-toggle="${mf.id}">
            <span class="module-toggle-track"></span>
          </label>`;
      })()
    : '';

  return `
    <div class="module-card" style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:14px;margin-bottom:10px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <strong style="font-size:14px">${mf.name}</strong>
          <code style="font-size:11px;opacity:0.6">${mf.id}</code>
          <span style="opacity:0.6;font-size:11px">v${mf.version}</span>
          ${statusBadge(entry.status)}
          ${sourceBadge}
          ${navBadge}
        </div>
        <div style="display:flex;align-items:center;gap:8px">${navToggle}${uninstallBtn}</div>
      </div>
      ${mf.description ? `<div style="font-size:12px;opacity:0.7;margin-bottom:8px">${mf.description}</div>` : ''}
      ${mf.routes_prefix ? `<div style="font-size:11px;opacity:0.6;margin-bottom:8px"><code>${mf.routes_prefix}</code></div>` : ''}
      ${entry.error ? `<div style="font-size:12px;color:#ff6d5a;margin-bottom:8px">Error: ${entry.error}</div>` : ''}
      ${secretsHtml ? `<div style="border-top:1px solid var(--border-dim);padding-top:6px;margin-top:6px"><div style="font-size:11px;opacity:0.5;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">Declared secrets</div>${secretsHtml}</div>` : ''}
    </div>
  `;
}

async function fetchKnownRefs() {
  try {
    const data = await get('/api/admin/secrets/refs');
    return new Set((data.refs || []).map(r => (r.startsWith('$') ? r.slice(1) : r)));
  } catch {
    return new Set();
  }
}

export async function renderModules(el) {
  el.innerHTML = `<div class="spinner"></div>`;

  try {
    const [data, knownRefs] = await Promise.all([
      get('/api/modules'),
      fetchKnownRefs(),
    ]);

    const builtins = data.modules.filter(m => m.source === 'builtin');
    const community = data.modules.filter(m => m.source === 'community');

    el.innerHTML = `
      <div style="max-width:900px">
        <div style="margin-bottom:20px;padding:14px;background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius)">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
            <div>
              <strong style="font-size:14px">Install a community module</strong>
              <div style="font-size:12px;opacity:0.6;margin-top:4px">Installs run third-party code with access to your data. Only install modules from repos you trust.</div>
            </div>
          </div>
          <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
            <input id="module-install-repo" class="input" placeholder="owner/repo or https://github.com/owner/repo" style="flex:2;min-width:260px">
            <input id="module-install-ref" class="input" placeholder="tag, branch, or SHA (default: main)" style="flex:1;min-width:180px">
            <button id="module-install-btn" class="btn btn-primary">Install</button>
          </div>
          <div id="module-install-msg" style="margin-top:8px;font-size:12px"></div>
        </div>

        ${community.length ? `
          <h3 style="font-size:12px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.6;margin:16px 0 10px">Community modules (${community.length})</h3>
          ${community.map(m => moduleCard(m, knownRefs)).join('')}
        ` : ''}

        <h3 style="font-size:12px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.6;margin:16px 0 10px">Built-in modules (${builtins.length})</h3>
        ${builtins.map(m => moduleCard(m, knownRefs)).join('')}

        <div style="margin-top:16px;padding:10px;font-size:11px;opacity:0.5;text-align:center">
          AgeniusDesk v${data.app_version} · ${data.count} modules registered
        </div>
      </div>
    `;

    wireInstall(el);
    wireUninstall(el);
    wireNavToggles(el);
  } catch (e) {
    el.innerHTML = `<div class="error-banner">Failed to load modules: ${e.message}</div>`;
  }
}

function wireInstall(el) {
  const btn = el.querySelector('#module-install-btn');
  const repoInput = el.querySelector('#module-install-repo');
  const refInput = el.querySelector('#module-install-ref');
  const msg = el.querySelector('#module-install-msg');

  btn?.addEventListener('click', async () => {
    const repo = repoInput.value.trim();
    if (!repo) {
      msg.style.color = '#ff6d5a';
      msg.textContent = 'Repo is required.';
      return;
    }
    btn.disabled = true;
    msg.style.color = 'var(--text-secondary)';
    msg.textContent = 'Downloading tarball…';
    try {
      const result = await post('/api/modules/install', {
        repo,
        ref: refInput.value.trim() || 'main',
      });
      msg.style.color = '#34d399';
      msg.innerHTML = `Installed <code>${result.id}</code> v${result.version} (sha ${result.installed_sha.slice(0, 7)}). <strong>Restart the app to activate.</strong>`;
      toast.success(`Installed ${result.name}. Restart required.`);
    } catch (e) {
      msg.style.color = '#ff6d5a';
      msg.textContent = `Install failed: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

function wireUninstall(el) {
  el.querySelectorAll('.module-uninstall-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.module;
      if (!confirm(`Uninstall module "${id}"? The files will be removed but declared secrets will remain in your store.`)) return;
      btn.disabled = true;
      try {
        await del(`/api/modules/${id}`);
        toast.success(`Uninstalled ${id}. Restart required.`);
        renderModules(el.parentElement || el);  // re-render
      } catch (e) {
        toast.error(`Uninstall failed: ${e.message}`);
        btn.disabled = false;
      }
    });
  });
}

function wireNavToggles(el) {
  el.querySelectorAll('[data-module-toggle]').forEach(input => {
    input.addEventListener('change', () => {
      const moduleId = input.dataset.moduleToggle;
      if (window.__setModuleNavVisible) {
        window.__setModuleNavVisible(moduleId, input.checked);
      }
    });
  });
}
