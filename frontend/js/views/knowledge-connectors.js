/**
 * Knowledge › Connectors — toggle which MCP servers are available to
 * Knowledge queries (sources, search fan-out, and agent context).
 */

import { get, put } from '../api.js';
import * as toast from '../components/toast.js';

export async function render(container) {
  container.innerHTML = `
    <div class="section-header" style="margin-bottom:20px">
      <div>
        <h2 class="section-title">Knowledge Connectors</h2>
        <span class="card-subtitle" id="kc-stats"></span>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div style="padding:14px 16px;color:var(--text-secondary);font-size:13px;line-height:1.6;border-bottom:1px solid var(--border-dim)">
        Toggle which MCP servers are accessible when an agent or search query uses Knowledge.
        Enabled connectors are surfaced in the Instructions document so agents know what tools are available.
        Register new MCP servers in <a href="#" onclick="window.__nav('assistant');return false" style="color:var(--accent)">Assistant</a>.
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">MCP Servers</span></div>
      <div id="kc-list" style="padding:0"></div>
    </div>
  `;

  await loadList();
}

async function loadList() {
  const listEl = document.getElementById('kc-list');
  const statsEl = document.getElementById('kc-stats');
  try {
    const data = await get('/api/knowledge/connectors');
    const connectors = data.connectors || [];
    const enabledCount = connectors.filter(c => c.knowledge_enabled).length;
    if (statsEl) statsEl.textContent = `${connectors.length} server${connectors.length === 1 ? '' : 's'} · ${enabledCount} in Knowledge`;

    if (connectors.length === 0) {
      listEl.innerHTML = `
        <div class="empty-state" style="padding:24px">
          <p>No MCP servers registered yet.</p>
          <p><a href="#" onclick="window.__nav('assistant');return false" style="color:var(--accent)">Add one in Assistant settings.</a></p>
        </div>`;
      return;
    }

    listEl.innerHTML = connectors.map(c => `
      <div class="kc-row" data-id="${escapeAttr(c.id)}"
           style="display:flex;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid var(--border-dim)">
        <label class="kc-toggle" style="display:flex;align-items:center;gap:0;cursor:pointer;flex-shrink:0" title="${c.knowledge_enabled ? 'Remove from Knowledge' : 'Add to Knowledge'}">
          <input type="checkbox" class="kc-check" data-id="${escapeAttr(c.id)}" ${c.knowledge_enabled ? 'checked' : ''} style="display:none" />
          <span class="kc-pill ${c.knowledge_enabled ? 'kc-pill-on' : 'kc-pill-off'}">
            ${c.knowledge_enabled ? 'In Knowledge' : 'Off'}
          </span>
        </label>
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;font-size:13px">${escapeHtml(c.name)}</div>
          <div style="font-size:11px;font-family:var(--font-mono);color:var(--text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml(c.url)}</div>
          ${c.description ? `<div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${escapeHtml(c.description)}</div>` : ''}
        </div>
        <span class="${c.enabled ? 'pill pill-neutral' : 'pill'}" style="${c.enabled ? '' : 'opacity:0.45'}" title="${c.enabled ? 'MCP server is enabled' : 'MCP server is disabled globally'}">${c.enabled ? 'enabled' : 'disabled'}</span>
      </div>
    `).join('');

    listEl.querySelectorAll('.kc-check').forEach(cb => {
      cb.addEventListener('change', () => onToggle(cb.dataset.id, cb.checked));
    });

  } catch (e) {
    listEl.innerHTML = `<div style="padding:16px;color:var(--error)">${escapeHtml(e.message || String(e))}</div>`;
  }
}

async function onToggle(serverId, enabled) {
  const row = document.querySelector(`.kc-row[data-id="${serverId}"]`);
  const pill = row?.querySelector('.kc-pill');
  if (pill) {
    pill.textContent = '…';
    pill.className = 'kc-pill kc-pill-off';
  }
  try {
    await put(`/api/knowledge/connectors/${serverId}`, { knowledge_enabled: enabled });
    if (pill) {
      pill.textContent = enabled ? 'In Knowledge' : 'Off';
      pill.className = `kc-pill ${enabled ? 'kc-pill-on' : 'kc-pill-off'}`;
    }
    const statsEl = document.getElementById('kc-stats');
    if (statsEl) {
      const checks = document.querySelectorAll('.kc-check');
      const on = [...checks].filter(c => c.checked).length;
      const total = checks.length;
      statsEl.textContent = `${total} server${total === 1 ? '' : 's'} · ${on} in Knowledge`;
    }
    toast.success(enabled ? 'Connector added to Knowledge' : 'Connector removed from Knowledge');
  } catch (e) {
    if (pill) {
      pill.textContent = enabled ? 'Off' : 'In Knowledge';
      pill.className = `kc-pill ${enabled ? 'kc-pill-off' : 'kc-pill-on'}`;
      const cb = row?.querySelector('.kc-check');
      if (cb) cb.checked = !enabled;
    }
    toast.error('Update failed: ' + (e.message || e));
  }
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function escapeAttr(s) { return String(s || '').replace(/"/g, '&quot;'); }

export function teardown() {}
