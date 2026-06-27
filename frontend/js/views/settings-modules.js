/**
 * Modules tab in Settings — lists built-in + community modules with status,
 * declared secrets, and inspect/install/uninstall controls.
 *
 * Community install is two-phase: Install runs a dry-run /inspect (download +
 * static AST scan, no registration), shows a consent modal with the declared
 * capabilities, the severity-ranked scan findings, and the declared-vs-detected
 * diff, then /install only after the operator consents. The scan is a heuristic
 * review, NOT a sandbox — the modal says so plainly.
 */

import { get, post, del } from '../api.js';
import * as toast from '../components/toast.js';

const SEV_COLOR = {
  CRITICAL: '#ff6d5a',
  HIGH: '#fb923c',
  MEDIUM: '#fbbf24',
  INFO: '#8a94a6',
};

function esc(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

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
      <code style="color:var(--text-secondary)">$${esc(req.key)}</code>${label}
      ${req.description ? `<span style="opacity:0.6">— ${esc(req.description)}</span>` : ''}
    </div>
  `;
}

function sevChip(sev, count) {
  if (!count) return '';
  return `<span class="badge" style="background:${SEV_COLOR[sev]}22;color:${SEV_COLOR[sev]};border:1px solid ${SEV_COLOR[sev]}55;font-size:10px;margin-left:6px">${count} ${sev.toLowerCase()}</span>`;
}

function moduleCard(entry, knownRefs, lock) {
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
    ? `<span style="opacity:0.6;font-size:11px;margin-left:8px">nav: ${esc(navLabel)}</span>`
    : '';

  const sourceBadge = isBuiltin
    ? '<span class="badge-core">Core</span>'
    : '<span class="badge" style="background:#60a5fa22;color:#60a5fa;font-size:10px;border:1px solid #60a5fa44">community</span>';

  const uninstallBtn = isBuiltin
    ? ''
    : `<button class="btn btn-sm btn-ghost module-uninstall-btn" data-module="${esc(mf.id)}" style="color:#ff6d5a">Uninstall</button>`;

  const navToggle = (!isBuiltin && hasNav)
    ? (() => {
        const hidden = JSON.parse(localStorage.getItem('nav-modules-hidden') || '[]');
        const isVisible = !hidden.includes(mf.id);
        return `
          <label class="module-toggle" title="Show/hide in sidebar nav" style="margin-left:auto">
            <input type="checkbox" ${isVisible ? 'checked' : ''} data-module-toggle="${esc(mf.id)}">
            <span class="module-toggle-track"></span>
          </label>`;
      })()
    : '';

  // Provenance line for community modules: who approved it, at what scan level.
  const prov = (!isBuiltin && lock && lock[mf.id]) ? lock[mf.id] : null;
  const provHtml = prov
    ? `<div style="border-top:1px solid var(--border-dim);padding-top:6px;margin-top:6px;font-size:11px;opacity:0.7">
         <code>${esc((prov.installed_sha || '').slice(0, 7))}</code>
         ${prov.approved_by ? ` · approved by ${esc(prov.approved_by)}` : ''}
         ${prov.scan_max_severity ? ` · scan: <span style="color:${SEV_COLOR[(prov.scan_max_severity || '').toUpperCase()] || 'inherit'}">${esc(prov.scan_max_severity)}</span>` : ''}
       </div>`
    : '';

  return `
    <div class="module-card" style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:14px;margin-bottom:10px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <strong style="font-size:14px">${esc(mf.name)}</strong>
          <code style="font-size:11px;opacity:0.6">${esc(mf.id)}</code>
          <span style="opacity:0.6;font-size:11px">v${esc(mf.version)}</span>
          ${statusBadge(entry.status)}
          ${sourceBadge}
          ${navBadge}
        </div>
        <div style="display:flex;align-items:center;gap:8px">${navToggle}${uninstallBtn}</div>
      </div>
      ${mf.description ? `<div style="font-size:12px;opacity:0.7;margin-bottom:8px">${esc(mf.description)}</div>` : ''}
      ${mf.routes_prefix ? `<div style="font-size:11px;opacity:0.6;margin-bottom:8px"><code>${esc(mf.routes_prefix)}</code></div>` : ''}
      ${entry.error ? `<div style="font-size:12px;color:#ff6d5a;margin-bottom:8px">Error: ${esc(entry.error)}</div>` : ''}
      ${secretsHtml ? `<div style="border-top:1px solid var(--border-dim);padding-top:6px;margin-top:6px"><div style="font-size:11px;opacity:0.5;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">Declared secrets</div>${secretsHtml}</div>` : ''}
      ${provHtml}
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
    const lock = data.lock || {};

    el.innerHTML = `
      <div style="max-width:900px">
        <div style="margin-bottom:20px;padding:14px;background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius)">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
            <div>
              <strong style="font-size:14px">Install a community module</strong>
              <div style="font-size:12px;opacity:0.6;margin-top:4px">Inspect runs a static scan and shows what the module declares vs what its code does. Heuristic review, not a sandbox: an installed module runs in-process with full data access and its frontend runs in this app's page (it can break the UI). Only install modules you trust.</div>
            </div>
          </div>
          <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
            <input id="module-install-repo" class="input" placeholder="owner/repo or https://github.com/owner/repo" style="flex:2;min-width:260px">
            <input id="module-install-ref" class="input" placeholder="tag, branch, or SHA (default: main)" style="flex:1;min-width:180px">
            <button id="module-install-btn" class="btn btn-primary">Discover</button>
          </div>
          <div id="module-install-msg" style="margin-top:8px;font-size:12px"></div>
          <div id="module-discover-results" style="margin-top:10px"></div>
        </div>

        ${community.length ? `
          <h3 style="font-size:12px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.6;margin:16px 0 10px">Community modules (${community.length})</h3>
          ${community.map(m => moduleCard(m, knownRefs, lock)).join('')}
        ` : ''}

        <h3 style="font-size:12px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.6;margin:16px 0 10px">Built-in modules (${builtins.length})</h3>
        ${builtins.map(m => moduleCard(m, knownRefs, lock)).join('')}

        <div style="margin-top:16px;padding:10px;font-size:11px;opacity:0.5;text-align:center">
          AgeniusDesk v${esc(data.app_version)} · ${data.count} modules registered
        </div>
      </div>
    `;

    wireInstall(el);
    wireUninstall(el);
    wireNavToggles(el);
  } catch (e) {
    el.innerHTML = `<div class="error-banner">Failed to load modules: ${esc(e.message)}</div>`;
  }
}

// ── Consent modal ─────────────────────────────────────────────────────────────

function capabilityList(caps) {
  if (!caps) {
    return `<div style="font-size:12px;opacity:0.7">This module declares <strong>no capabilities</strong>. Any capability the scan detects below is therefore undeclared.</div>`;
  }
  const net = caps.network || {};
  const fs = caps.filesystem || {};
  const rows = [];
  rows.push(`<li><strong>Network:</strong> ${net.enabled ? `yes — ${net.hosts && net.hosts.length ? net.hosts.map(esc).join(', ') : '<span style="color:' + SEV_COLOR.HIGH + '">any host</span>'}` : 'no'}</li>`);
  rows.push(`<li><strong>Filesystem writes:</strong> ${fs.write_paths && fs.write_paths.length ? fs.write_paths.map(esc).join(', ') : 'none declared'}</li>`);
  rows.push(`<li><strong>Subprocess:</strong> ${caps.subprocess ? 'yes' : 'no'}</li>`);
  rows.push(`<li><strong>Env vars:</strong> ${caps.env && caps.env.length ? caps.env.map(esc).join(', ') : 'none declared'}</li>`);
  return `<ul style="margin:4px 0 0;padding-left:18px;font-size:12px;line-height:1.7">${rows.join('')}</ul>`;
}

function findingsList(report) {
  const fs = report.findings || [];
  if (!fs.length) {
    return `<div style="font-size:12px;color:#34d399">No findings.</div>`;
  }
  return fs.map(f => `
    <div style="display:grid;grid-template-columns:78px 1fr;gap:8px;align-items:start;font-size:12px;padding:4px 0;border-bottom:1px solid var(--border-dim)">
      <span class="badge" style="background:${SEV_COLOR[f.severity]}22;color:${SEV_COLOR[f.severity]};border:1px solid ${SEV_COLOR[f.severity]}55;font-size:10px;justify-self:start">${esc(f.severity)}</span>
      <div>
        <div>${esc(f.detail)}</div>
        <div style="opacity:0.55;font-size:11px"><code>${esc(f.category)}</code> · ${esc(f.file)}${f.line ? ':' + f.line : ''}</div>
      </div>
    </div>
  `).join('');
}

function diffRow(label, declared, detected, mismatch) {
  const color = mismatch ? SEV_COLOR.HIGH : 'inherit';
  return `
    <tr style="border-bottom:1px solid var(--border-dim)">
      <td style="padding:4px 8px 4px 0;opacity:0.7">${esc(label)}</td>
      <td style="padding:4px 8px;color:${color}">${esc(declared)}</td>
      <td style="padding:4px 0;color:${color}">${esc(detected)}</td>
    </tr>`;
}

function diffTable(d) {
  if (!d) return '';
  const net = d.network || {};
  const sub = d.subprocess || {};
  const fsd = d.filesystem || {};
  const env = d.env || {};
  const netMismatch = net.detected && !net.declared;
  const subMismatch = sub.detected && !sub.declared;
  const rows = [
    diffRow('network', net.declared ? (net.declared_hosts || []).join(', ') || 'any host' : 'no', net.detected ? (net.detected_hosts || []).join(', ') || 'yes' : 'no', netMismatch),
    diffRow('subprocess', sub.declared ? 'yes' : 'no', sub.detected ? 'yes' : 'no', subMismatch),
    diffRow('fs writes', (fsd.declared_write_paths || []).join(', ') || 'none', (fsd.detected_writes || []).join(', ') || 'none', false),
    diffRow('env', (env.declared || []).join(', ') || 'none', (env.detected || []).join(', ') || 'none', false),
  ];
  return `
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:4px">
      <thead><tr style="opacity:0.5;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">
        <th style="text-align:left;padding:2px 8px 2px 0"></th><th style="text-align:left;padding:2px 8px">declared</th><th style="text-align:left;padding:2px 0">detected</th>
      </tr></thead>
      <tbody>${rows.join('')}</tbody>
    </table>`;
}

function section(title, inner) {
  return `<div style="margin-bottom:14px">
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.55;margin-bottom:6px">${esc(title)}</div>
    ${inner}
  </div>`;
}

/**
 * Show the consent modal for an inspect result. Resolves with a consent payload
 * { acknowledged, typed_id } on approve, or null on cancel.
 */
function consentModal(inspect) {
  return new Promise((resolve) => {
    const mf = inspect.manifest || {};
    const report = inspect.scan_report || {};
    const counts = report.summary || {};
    const hasCritical = (counts.CRITICAL || 0) > 0;
    const hasHigh = (counts.HIGH || 0) > 0;
    const compatible = inspect.compatible !== false;

    const root = document.createElement('div');
    root.className = 'modal';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-label', `Install ${mf.name || mf.id}`);

    const parseErr = (report.parse_errors || []).length
      ? `<div style="font-size:12px;color:${SEV_COLOR.MEDIUM};margin-top:6px">${report.parse_errors.length} file(s) could not be parsed and were not scanned.</div>`
      : '';

    const consentControls = [];
    if (hasCritical) {
      consentControls.push(`
        <label style="display:block;font-size:12px;margin:10px 0 4px">This module has <span style="color:${SEV_COLOR.CRITICAL}">CRITICAL</span> findings. Type the module id <code style="color:${SEV_COLOR.CRITICAL}">${esc(mf.id)}</code> to confirm you understand the risk.</label>
        <input id="consent-typed" class="input" autocomplete="off" spellcheck="false" placeholder="${esc(mf.id)}" style="width:100%">
      `);
    }
    if (hasHigh) {
      consentControls.push(`
        <label style="display:flex;gap:8px;align-items:flex-start;font-size:12px;margin-top:10px;cursor:pointer">
          <input id="consent-ack" type="checkbox" style="margin-top:2px">
          <span>I acknowledge the <span style="color:${SEV_COLOR.HIGH}">HIGH</span> findings above (elevated or undeclared capabilities) and choose to install anyway.</span>
        </label>
      `);
    }
    const incompatHtml = compatible ? '' : `<div style="font-size:12px;color:${SEV_COLOR.CRITICAL};margin-bottom:10px">Incompatible: requires app version ≥ ${esc(inspect.min_app_version)}. Install is blocked.</div>`;

    root.innerHTML = `
      <div class="modal-content" tabindex="-1" style="max-width:680px;width:92vw;max-height:86vh;overflow:auto">
        <h2 style="margin-bottom:4px">Install ${esc(mf.name || mf.id)}</h2>
        <div style="font-size:12px;opacity:0.6;margin-bottom:12px">
          <code>${esc(mf.id)}</code> v${esc(mf.version || '?')} · <code>${esc((inspect.resolved_sha || '').slice(0, 12))}</code>
          ${sevChip('CRITICAL', counts.CRITICAL)}${sevChip('HIGH', counts.HIGH)}${sevChip('MEDIUM', counts.MEDIUM)}${sevChip('INFO', counts.INFO)}
        </div>

        <div style="background:${SEV_COLOR.MEDIUM}11;border:1px solid ${SEV_COLOR.MEDIUM}44;border-radius:var(--radius);padding:10px 12px;font-size:12px;line-height:1.5;margin-bottom:14px">
          <strong>Heuristic review, not a sandbox.</strong> This is a static scan of the module's code. It cannot follow obfuscation, runtime-fetched code, or dynamic imports. Once installed, a community module's backend runs <strong>in-process with full access to your data and credentials</strong>, and its frontend runs <strong>inside this app's page</strong>, so it can read, change, or break the UI. Absence of findings is not a safety guarantee. <strong>Only install modules you trust.</strong>
        </div>

        ${incompatHtml}
        ${section('Declared capabilities', capabilityList(inspect.capabilities))}
        ${section('Declared vs detected', diffTable(report.declared_vs_detected))}
        ${section(`Scan findings (${(report.findings || []).length})`, findingsList(report) + parseErr)}

        <div id="consent-controls">${consentControls.join('')}</div>

        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">
          <button type="button" class="btn btn-sm" data-action="cancel">Cancel</button>
          <button type="button" class="btn btn-sm btn-primary" data-action="confirm">Install</button>
        </div>
      </div>
    `;

    const confirmBtn = root.querySelector('[data-action="confirm"]');
    const cancelBtn = root.querySelector('[data-action="cancel"]');
    const typedInput = root.querySelector('#consent-typed');
    const ackInput = root.querySelector('#consent-ack');

    const refresh = () => {
      const typedOk = !hasCritical || (typedInput && typedInput.value.trim() === mf.id);
      const ackOk = !hasHigh || (ackInput && ackInput.checked);
      confirmBtn.disabled = !compatible || !typedOk || !ackOk;
    };
    refresh();

    typedInput?.addEventListener('input', refresh);
    ackInput?.addEventListener('change', refresh);

    const cleanup = (result) => {
      document.removeEventListener('keydown', onKey);
      root.remove();
      resolve(result);
    };
    const onKey = (e) => { if (e.key === 'Escape') cleanup(null); };

    confirmBtn.addEventListener('click', () => {
      if (confirmBtn.disabled) return;
      cleanup({
        acknowledged: hasHigh ? !!ackInput?.checked : false,
        typed_id: hasCritical ? (typedInput?.value.trim() || null) : null,
      });
    });
    cancelBtn.addEventListener('click', () => cleanup(null));
    root.addEventListener('click', (e) => { if (e.target === root) cleanup(null); });
    document.addEventListener('keydown', onKey);

    document.body.appendChild(root);
    setTimeout(() => (typedInput || confirmBtn).focus(), 0);
  });
}

// Run the inspect -> consent -> install flow for one module (by repo/ref/path).
async function inspectAndInstall(el, repo, ref, path, msg) {
  msg.style.color = 'var(--text-secondary)';
  msg.textContent = 'Inspecting (downloading + scanning)…';
  let inspect;
  try {
    inspect = await post('/api/modules/inspect', { repo, ref, path });
  } catch (e) {
    msg.style.color = '#ff6d5a';
    msg.textContent = `Inspect failed: ${e.message}`;
    return;
  }
  msg.textContent = '';

  const consent = await consentModal(inspect);
  if (!consent) {
    msg.style.color = 'var(--text-secondary)';
    msg.textContent = 'Install cancelled.';
    return;
  }

  msg.style.color = 'var(--text-secondary)';
  msg.textContent = 'Installing…';
  try {
    const result = await post('/api/modules/install', {
      repo,
      ref,
      path,
      resolved_sha: inspect.resolved_sha,
      consent,
    });
    msg.style.color = '#34d399';
    msg.innerHTML = `Installed <code>${esc(result.id)}</code> v${esc(result.version)} (scan ${esc(result.scan_max_severity)}). It activates on restart.
      <button id="module-restart-btn" class="btn btn-sm btn-primary" style="margin-left:8px">Restart now</button>
      <span style="opacity:0.65"> or restart AgeniusDesk later.</span>`;
    msg.querySelector('#module-restart-btn')?.addEventListener('click', restartApp);
    toast.success(`Installed ${result.name}. Restart to activate.`);
    // Do not re-render: a community module is not in the registry until the
    // restart, so re-rendering would just hide this message with no card to show.
  } catch (e) {
    msg.style.color = '#ff6d5a';
    msg.textContent = `Install failed: ${e.message}`;
  }
}

// ── App restart (activate installed/removed modules) ──────────────────────────

async function restartApp() {
  try {
    await post('/api/admin/restart', {});
  } catch {
    // The server may drop the connection as it goes down; that is expected.
  }
  showRestartOverlay();
}

function showRestartOverlay() {
  if (document.getElementById('agd-restart-overlay')) return;
  const o = document.createElement('div');
  o.id = 'agd-restart-overlay';
  o.className = 'modal';
  o.innerHTML = `
    <div class="modal-content" style="text-align:center">
      <h2 style="margin-bottom:8px">Restarting AgeniusDesk…</h2>
      <div style="color:var(--text-secondary);font-size:13px;margin-bottom:14px">Activating modules. This page reloads automatically when it is back.</div>
      <div class="spinner" style="margin:0 auto"></div>
    </div>`;
  document.body.appendChild(o);
  // Wait for the process to go down, then poll until it answers again, then reload.
  const start = Date.now();
  const tick = async () => {
    try {
      await fetch('/api/health', { cache: 'no-store' });
      location.reload();  // any response means the server is back up
      return;
    } catch {
      if (Date.now() - start < 90000) setTimeout(tick, 1500);
      else location.reload();
    }
  };
  setTimeout(tick, 3000);
}

function renderDiscovered(el, repo, ref, modules, results, msg) {
  if (!modules.length) {
    results.innerHTML = `<div style="font-size:12px;color:#ff6d5a">No installable modules found in this repo (no manifest.json at the root or under modules/).</div>`;
    return;
  }
  results.innerHTML = `
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.55;margin-bottom:6px">Found ${modules.length} module${modules.length > 1 ? 's' : ''}</div>
    ${modules.map((m, i) => `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;border:1px solid var(--border-dim);border-radius:var(--radius);margin-bottom:6px">
        <div style="min-width:0">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <strong style="font-size:13px">${esc(m.name)}</strong>
            <code style="font-size:11px;opacity:0.6">${esc(m.id)}</code>
            <span style="opacity:0.6;font-size:11px">v${esc(m.version)}</span>
            ${m.path ? `<code style="font-size:10px;opacity:0.45">${esc(m.path)}</code>` : ''}
            ${m.compatible ? '' : `<span class="badge" style="background:#ff6d5a22;color:#ff6d5a;font-size:10px">incompatible</span>`}
          </div>
          ${m.description ? `<div style="font-size:11px;opacity:0.65;margin-top:2px">${esc(m.description)}</div>` : ''}
        </div>
        <button class="btn btn-sm btn-primary module-inspect-btn" data-idx="${i}">Inspect</button>
      </div>
    `).join('')}
  `;
  results.querySelectorAll('.module-inspect-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const m = modules[Number(btn.dataset.idx)];
      inspectAndInstall(el, repo, ref, m.path || '', msg);
    });
  });
}

function wireInstall(el) {
  const btn = el.querySelector('#module-install-btn');
  const repoInput = el.querySelector('#module-install-repo');
  const refInput = el.querySelector('#module-install-ref');
  const msg = el.querySelector('#module-install-msg');
  const results = el.querySelector('#module-discover-results');

  btn?.addEventListener('click', async () => {
    const repo = repoInput.value.trim();
    if (!repo) {
      msg.style.color = '#ff6d5a';
      msg.textContent = 'Repo is required.';
      return;
    }
    const ref = refInput.value.trim() || 'main';
    btn.disabled = true;
    results.innerHTML = '';
    msg.style.color = 'var(--text-secondary)';
    msg.textContent = 'Discovering modules (downloading repo)…';
    try {
      const data = await post('/api/modules/discover', { repo, ref });
      msg.textContent = '';
      renderDiscovered(el, data.repo || repo, data.ref || ref, data.modules || [], results, msg);
    } catch (e) {
      msg.style.color = '#ff6d5a';
      msg.textContent = `Discover failed: ${e.message}`;
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
        toast.success(`Uninstalled ${id}.`);
        // It is removed from disk but stays mounted until a restart.
        const msg = el.querySelector('#module-install-msg');
        if (msg) {
          msg.style.color = '#34d399';
          msg.innerHTML = `Uninstalled <code>${esc(id)}</code>. It stays active until restart.
            <button id="module-restart-btn" class="btn btn-sm btn-primary" style="margin-left:8px">Restart now</button>`;
          msg.querySelector('#module-restart-btn')?.addEventListener('click', restartApp);
        }
        btn.disabled = false;
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
