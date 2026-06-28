/**
 * Fleet Health — workflow health + errors aggregated across ALL n8n instances.
 *
 * Two tabs:
 *  - Health: live parallel fan-out per instance (workflows, error rate, unhealthy
 *    workflows) with a combined roll-up. A degraded/unreachable instance is shown,
 *    not fatal.
 *  - Errors: every collected error across all instances in one list
 *    (GET /api/errors?instance_id=all), badged by client. The errors store is
 *    already cross-instance, so this is a unified view, not per-instance.
 *
 * Read-only operator convenience for the "one client becomes ten" case; the
 * per-client access/audit governance layer is an enterprise concern, not this.
 */

import { get } from '../api.js';
import { renderErrorItem } from '../components/error-item.js';

let _tab = 'health';
let _instMap = {};

function esc(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function rateColor(rate) {
  if (rate >= 20) return '#ff6d5a';
  if (rate >= 5) return '#fbbf24';
  return '#34d399';
}

// ── Health tab ────────────────────────────────────────────────────────────────

function instanceCard(inst) {
  if (!inst.reachable) {
    return `
      <div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-left:3px solid #ff6d5a;border-radius:var(--radius);padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <strong style="font-size:14px">${esc(inst.name || inst.id)}</strong>
          <span class="badge" style="background:#ff6d5a22;color:#ff6d5a;border:1px solid #ff6d5a55;font-size:11px">${esc(inst.error || 'unreachable')}</span>
        </div>
        <div style="font-size:12px;opacity:0.6;margin-top:6px">No data. Check this instance's URL and API key under Instances.</div>
      </div>`;
  }
  const color = inst.color || '#60a5fa';
  const rc = rateColor(inst.error_rate);
  const unhealthy = (inst.unhealthy || []).map(w =>
    `<div style="display:flex;justify-content:space-between;gap:8px;font-size:12px;padding:2px 0">
       <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(w.name)}</span>
       <span style="color:#ff6d5a;flex-shrink:0">${esc(w.errors)} err</span>
     </div>`).join('');
  return `
    <div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-left:3px solid ${esc(color)};border-radius:var(--radius);padding:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px">
        <strong style="font-size:14px">${esc(inst.name || inst.id)}${inst.active ? ' <span style="font-size:10px;opacity:0.55;font-weight:400">active</span>' : ''}</strong>
        ${inst.login_url ? `<a href="${esc(inst.login_url)}" target="_blank" style="font-size:11px;color:var(--accent,#60a5fa)">open ↗</a>` : ''}
      </div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center">
        <div><div style="font-size:20px;font-weight:700">${esc(inst.workflows_active)}/${esc(inst.workflows_total)}</div><div style="font-size:11px;opacity:0.6">active</div></div>
        <div><div style="font-size:20px;font-weight:700;color:${rc}">${esc(inst.error_rate)}%</div><div style="font-size:11px;opacity:0.6">error rate</div></div>
        <div><div style="font-size:20px;font-weight:700">${esc(inst.exec_total)}</div><div style="font-size:11px;opacity:0.6">recent runs</div></div>
      </div>
      ${unhealthy ? `<div style="border-top:1px solid var(--border-dim);margin-top:10px;padding-top:8px"><div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.5;margin-bottom:4px">Unhealthy workflows</div>${unhealthy}</div>` : ''}
    </div>`;
}

async function loadHealth(content) {
  content.innerHTML = '<div class="spinner"></div>';
  try {
    const data = await get('/api/n8n/fleet/health');
    const t = data.totals || {};
    const insts = data.instances || [];
    _instMap = Object.fromEntries(insts.map(i => [i.id, { name: i.name || i.id, color: i.color || '#60a5fa' }]));
    if (!insts.length) {
      content.innerHTML = `<div style="opacity:0.6;font-size:13px">No instances configured. Add one under Instances.</div>`;
      return;
    }
    const trc = rateColor(t.error_rate || 0);
    const cells = [
      ['Instances', `${t.reachable}/${t.instances}`, 'reachable'],
      ['Workflows', `${t.workflows_active}/${t.workflows_total}`, 'active'],
      ['Error rate', `<span style="color:${trc}">${t.error_rate}%</span>`, 'recent runs'],
      ['Runs', `${t.exec_total}`, 'sampled'],
    ];
    content.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px">
        ${cells.map(([k, v, sub]) => `
          <div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:12px;text-align:center">
            <div style="font-size:22px;font-weight:700">${v}</div>
            <div style="font-size:11px;opacity:0.6">${esc(k)} · ${esc(sub)}</div>
          </div>`).join('')}
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px">${insts.map(instanceCard).join('')}</div>`;
  } catch (e) {
    content.innerHTML = `<div class="error-banner">Failed to load fleet health: ${esc(e.message)}</div>`;
  }
}

// ── Errors tab (shared error-item, identical to Overview + the Errors view) ───

async function loadErrors(content) {
  content.innerHTML = '<div class="spinner"></div>';
  try {
    // Build the instance map with per-instance n8n URL so the shared item's
    // "Open in n8n" points at each error's OWN instance (correct cross-instance).
    const inst = await get('/api/n8n/instances').catch(() => ({ instances: [] }));
    _instMap = Object.fromEntries((inst.instances || []).map(i =>
      [i.id, { name: i.name || i.id, color: i.color || '#60a5fa', n8nUrl: i.login_url || i.url || '' }]));
    const data = await get('/api/errors?instance_id=all&limit=100');
    const errors = data.errors || [];
    const header = `<div style="font-size:12px;opacity:0.65;margin-bottom:10px">${errors.length} recent error${errors.length === 1 ? '' : 's'} across all instances · ${esc(data.count_24h || 0)} in the last 24h</div>`;
    if (!errors.length) {
      content.innerHTML = header + `<div style="opacity:0.6;font-size:13px">No errors collected. Errors flow in once an instance's Global Error Handler is installed (auto-installed on connect).</div>`;
      return;
    }
    content.innerHTML = header
      + `<div class="errors-list" style="display:flex;flex-direction:column;gap:8px">${errors.map(e => renderErrorItem(e, { instanceMap: _instMap })).join('')}</div>`;
  } catch (e) {
    content.innerHTML = `<div class="error-banner">Failed to load errors: ${esc(e.message)}</div>`;
  }
}

// ── View shell ─────────────────────────────────────────────────────────────────

export async function render(container) {
  container.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;gap:12px;flex-wrap:wrap">
      <div>
        <h2 style="margin:0">Fleet Health</h2>
        <div style="font-size:13px;color:var(--text-secondary);margin-top:2px">Workflow health and errors across every connected n8n instance.</div>
      </div>
      <button id="fleet-refresh" class="btn btn-sm">Refresh</button>
    </div>
    <div style="display:flex;gap:6px;border-bottom:1px solid var(--border-dim);margin-bottom:14px">
      <button class="fleet-tab" data-tab="health" style="background:none;border:none;border-bottom:2px solid transparent;color:var(--text-muted);padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer">Health</button>
      <button class="fleet-tab" data-tab="errors" style="background:none;border:none;border-bottom:2px solid transparent;color:var(--text-muted);padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer">Errors</button>
    </div>
    <div id="fleet-content"><div class="spinner"></div></div>
  `;
  const content = container.querySelector('#fleet-content');

  const paint = () => {
    container.querySelectorAll('.fleet-tab').forEach(b => {
      const on = b.dataset.tab === _tab;
      b.style.borderBottomColor = on ? 'var(--accent,#60a5fa)' : 'transparent';
      b.style.color = on ? 'var(--text-primary)' : 'var(--text-muted)';
    });
  };
  const load = () => (_tab === 'errors' ? loadErrors(content) : loadHealth(content));

  container.querySelectorAll('.fleet-tab').forEach(b => {
    b.addEventListener('click', () => { _tab = b.dataset.tab; paint(); load(); });
  });
  container.querySelector('#fleet-refresh').addEventListener('click', load);

  paint();
  await load();
}
