/**
 * Fleet Health — workflow health aggregated across ALL configured n8n instances.
 *
 * One pane for the "one client becomes ten" case. The backend fans out to every
 * instance in parallel (live), so a degraded or unreachable instance is shown as
 * such and never fails the whole view. Read-only operator convenience; the
 * per-client access/audit governance layer is an enterprise concern, not this.
 */

import { get } from '../api.js';

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

export async function render(container) {
  container.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;flex-wrap:wrap">
      <div>
        <h2 style="margin:0">Fleet Health</h2>
        <div style="font-size:13px;color:var(--text-secondary);margin-top:2px">Workflow health across every connected n8n instance, fetched live.</div>
      </div>
      <button id="fleet-refresh" class="btn btn-sm">Refresh</button>
    </div>
    <div id="fleet-totals" style="margin-bottom:16px"></div>
    <div id="fleet-grid"><div class="spinner"></div></div>
  `;

  const load = async () => {
    const grid = container.querySelector('#fleet-grid');
    const totals = container.querySelector('#fleet-totals');
    const btn = container.querySelector('#fleet-refresh');
    grid.innerHTML = '<div class="spinner"></div>';
    if (btn) btn.disabled = true;
    try {
      const data = await get('/api/n8n/fleet/health');
      const t = data.totals || {};
      const insts = data.instances || [];
      if (!insts.length) {
        totals.innerHTML = '';
        grid.innerHTML = `<div style="opacity:0.6;font-size:13px">No instances configured. Add one under Instances.</div>`;
        return;
      }
      const trc = rateColor(t.error_rate || 0);
      const cells = [
        ['Instances', `${t.reachable}/${t.instances}`, 'reachable'],
        ['Workflows', `${t.workflows_active}/${t.workflows_total}`, 'active'],
        ['Error rate', `<span style="color:${trc}">${t.error_rate}%</span>`, 'recent runs'],
        ['Runs', `${t.exec_total}`, 'sampled'],
      ];
      totals.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px">
          ${cells.map(([k, v, sub]) => `
            <div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:12px;text-align:center">
              <div style="font-size:22px;font-weight:700">${v}</div>
              <div style="font-size:11px;opacity:0.6">${esc(k)} · ${esc(sub)}</div>
            </div>`).join('')}
        </div>`;
      grid.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px">${insts.map(instanceCard).join('')}</div>`;
    } catch (e) {
      totals.innerHTML = '';
      grid.innerHTML = `<div class="error-banner">Failed to load fleet health: ${esc(e.message)}</div>`;
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  container.querySelector('#fleet-refresh').addEventListener('click', load);
  await load();
}
