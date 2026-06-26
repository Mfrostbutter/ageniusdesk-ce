/**
 * WorkflowDetailPanel — shared workflow detail element used by both the
 * Workflows view (right-hand panel) and the Dashboard workflow health drawer.
 *
 * Usage:
 *   const el = WorkflowDetailPanel(wf, executions, { n8nUrl, onAction });
 *   container.appendChild(el);
 *
 * wf         — workflow object from GET /api/n8n/workflows/{id}
 * executions — array from GET /api/n8n/executions?workflow_id={id}
 * opts.n8nUrl      — base URL for "Open in n8n" links (optional)
 * opts.onActivate  — callback(id, active) for toggle action (optional)
 * opts.onInject    — callback(id) for inject-trigger action (optional)
 * opts.onRemove    — callback(id) for remove-trigger action (optional)
 * opts.onDelete    — callback(id) for delete workflow action (optional)
 * opts.onAnalyze   — callback(execId, wfName, wfId) for AI analysis (optional)
 * opts.onObserve   — callback(execId, wfName) to open the execution trace (optional)
 */

function _esc(s) {
  const el = document.createElement('span');
  el.textContent = s == null ? '' : String(s);
  return el.innerHTML;
}

function _jsStr(s) {
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

function _formatTime(iso) {
  if (!iso) return '-';
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function _statusClass(s) {
  if (s === 'success') return 'success';
  if (s === 'error') return 'error';
  if (s === 'running') return 'warning';
  return 'neutral';
}

function _triggerClass(t) {
  if (t === 'webhook') return 'info';
  if (t === 'schedule') return 'warning';
  if (t === 'error') return 'error';
  return 'neutral';
}

export function WorkflowDetailPanel(wf, executions, opts = {}) {
  const { n8nUrl = window.__n8nUrl || '', onActivate, onInject, onRemove, onDelete, onAnalyze, onObserve } = opts;

  const isArchived = !!wf.is_archived;
  const el = document.createElement('div');

  // ── Header ──
  const header = document.createElement('div');
  header.style.cssText = 'margin-bottom:16px';
  header.innerHTML = `
    <h3 style="font-size:16px;margin-bottom:4px">${_esc(wf.name)}</h3>
    <div style="font-size:12px;color:var(--text-secondary);font-family:var(--font-mono)">ID: ${_esc(wf.id)}</div>
  `;
  el.appendChild(header);

  // ── Status chips ──
  const chips = document.createElement('div');
  chips.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px';
  chips.innerHTML = `
    <span class="pill pill-${wf.active ? 'success' : 'neutral'}">${wf.active ? 'Active' : 'Inactive'}</span>
    <span class="pill pill-${_triggerClass(wf.trigger_type)}">${_esc(wf.trigger_type)}</span>
    <span class="pill pill-neutral">${wf.node_count} nodes</span>
    ${isArchived ? '<span class="pill pill-warning" title="Archived in n8n (use n8n UI to unarchive)">Archived in n8n</span>' : ''}
  `;
  el.appendChild(chips);

  // ── Dashboard trigger info banner ──
  if (wf.trigger_type === 'webhook') {
    const banner = document.createElement('div');
    banner.style.cssText = 'margin-bottom:16px;padding:10px 12px;border-radius:6px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);font-size:12px;color:var(--text-secondary);line-height:1.5';
    banner.innerHTML = `<strong>Webhook-triggered workflow.</strong> n8n can't register two webhook triggers for the same workflow, and these workflows typically expect a specific payload. To fire it, call the existing webhook URL directly.${wf.dashboard_trigger_enabled ? ` <br><br><strong style="color:var(--error)">Orphaned dashboard trigger detected.</strong> Click <em>Remove Orphaned Dashboard Trigger</em> to clean it up.` : ''}`;
    el.appendChild(banner);
  } else if (wf.dashboard_trigger_url && wf.trigger_type !== 'webhook') {
    const urlBanner = document.createElement('div');
    urlBanner.style.cssText = 'font-size:12px;margin-bottom:12px';
    urlBanner.innerHTML = `<strong>Dashboard Trigger:</strong> <code style="font-size:11px">${_esc(wf.dashboard_trigger_url)}</code>`;
    el.appendChild(urlBanner);
  } else if (!wf.dashboard_trigger_enabled && wf.trigger_type !== 'webhook') {
    const noTriggerBanner = document.createElement('div');
    noTriggerBanner.style.cssText = 'margin-bottom:16px;padding:10px 12px;border-radius:6px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);font-size:12px;color:var(--text-secondary);line-height:1.5';
    noTriggerBanner.innerHTML = `<strong>Dashboard Trigger not enabled.</strong> Clicking <em>Enable Dashboard Trigger</em> adds a webhook node named <code>__dashboard_trigger</code> to your workflow and wires it in parallel with the existing trigger.`;
    el.appendChild(noTriggerBanner);
  }

  // ── Action row ──
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap';

  if (n8nUrl) {
    const openBtn = document.createElement('a');
    openBtn.className = 'btn btn-sm';
    openBtn.href = `${n8nUrl}/workflow/${wf.id}`;
    openBtn.target = '_blank';
    openBtn.rel = 'noopener';
    openBtn.textContent = 'Open in n8n';
    actions.appendChild(openBtn);
  }

  if (wf.trigger_type === 'webhook') {
    if (wf.dashboard_trigger_enabled) {
      const removeBtn = document.createElement('button');
      removeBtn.className = 'btn btn-sm btn-ghost';
      removeBtn.title = 'Remove the orphaned dashboard webhook node — it won\'t fire on this workflow';
      removeBtn.textContent = 'Remove Orphaned Dashboard Trigger';
      removeBtn.addEventListener('click', () => onRemove && onRemove(wf.id));
      actions.appendChild(removeBtn);
    }
  } else {
    if (wf.dashboard_trigger_enabled) {
      const triggerBtn = document.createElement('button');
      triggerBtn.className = 'btn btn-sm btn-primary';
      triggerBtn.textContent = 'Trigger';
      triggerBtn.addEventListener('click', () => {
        if (window.__triggerWorkflow) window.__triggerWorkflow(wf.id);
      });
      actions.appendChild(triggerBtn);

      const removeBtn = document.createElement('button');
      removeBtn.className = 'btn btn-sm btn-ghost';
      removeBtn.title = 'Remove the dashboard-owned webhook node from this workflow';
      removeBtn.textContent = 'Remove Dashboard Trigger';
      removeBtn.addEventListener('click', () => onRemove && onRemove(wf.id));
      actions.appendChild(removeBtn);
    } else {
      const injectBtn = document.createElement('button');
      injectBtn.className = 'btn btn-sm btn-primary';
      injectBtn.title = 'Add a webhook node to this workflow so the dashboard can fire it on demand';
      injectBtn.textContent = 'Enable Dashboard Trigger';
      injectBtn.addEventListener('click', () => onInject && onInject(wf.id));
      actions.appendChild(injectBtn);
    }
  }

  const toggleBtn = document.createElement('button');
  toggleBtn.className = 'btn btn-sm';
  toggleBtn.textContent = wf.active ? 'Deactivate' : 'Activate';
  toggleBtn.addEventListener('click', () => onActivate && onActivate(wf.id, !wf.active));
  actions.appendChild(toggleBtn);

  const deleteBtn = document.createElement('button');
  deleteBtn.className = 'btn btn-sm btn-ghost';
  deleteBtn.style.cssText = 'color:var(--error);border-color:var(--error)';
  deleteBtn.title = 'Permanently delete this workflow from n8n';
  deleteBtn.textContent = 'Delete';
  deleteBtn.addEventListener('click', () => onDelete && onDelete(wf.id));
  actions.appendChild(deleteBtn);

  el.appendChild(actions);

  // ── Recent Executions ──
  const execHeader = document.createElement('h4');
  execHeader.style.cssText = 'font-size:13px;font-weight:600;margin-bottom:8px';
  execHeader.textContent = 'Recent Executions';
  el.appendChild(execHeader);

  if (executions.length) {
    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    const tbody = executions.map(e => {
      const n8nExecUrl = n8nUrl ? `${n8nUrl}/workflow/${wf.id}/executions/${e.id}` : '';
      return `
        <tr style="cursor:pointer" onclick="if(!event.target.closest('button'))${n8nExecUrl ? `window.open('${_jsStr(n8nExecUrl)}','_blank')` : ''}">
          <td style="font-family:var(--font-mono);font-size:12px">${_esc(e.id)}</td>
          <td><span class="pill pill-${_statusClass(e.status)}">${_esc(e.status)}</span></td>
          <td>${_esc(e.mode)}</td>
          <td style="font-family:var(--font-mono);font-size:12px">${_formatTime(e.started_at)}</td>
          <td style="white-space:nowrap">
            ${onObserve
              ? `<button class="btn btn-sm btn-ghost wdp-observe-btn" data-exec-id="${_esc(e.id)}" style="font-size:10px;padding:2px 8px" title="View this execution's OpenTelemetry trace">&#128202; Observe</button>`
              : ''}
            ${e.status === 'error' && onAnalyze
              ? `<button class="btn btn-sm btn-ghost wdp-analyze-btn" data-exec-id="${_esc(e.id)}" style="font-size:10px;padding:2px 8px" title="Ask AI to analyze this failure">&#10022; Ask AI</button>`
              : ''}
          </td>
        </tr>
        <tr id="ai-row-${_esc(e.id)}" style="display:none">
          <td colspan="5" style="padding:0">
            <div id="ai-result-${_esc(e.id)}" style="padding:10px 12px;background:var(--bg-void);border-top:1px solid var(--border-dim);font-size:12px;line-height:1.6"></div>
          </td>
        </tr>
      `;
    }).join('');
    wrap.innerHTML = `<table>
      <thead><tr><th>ID</th><th>Status</th><th>Mode</th><th>Started</th><th></th></tr></thead>
      <tbody>${tbody}</tbody>
    </table>`;

    if (onAnalyze) {
      wrap.querySelectorAll('.wdp-analyze-btn').forEach(btn => {
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          const execId = btn.dataset.execId;
          onAnalyze(execId, wf.name, wf.id);
        });
      });
    }

    if (onObserve) {
      wrap.querySelectorAll('.wdp-observe-btn').forEach(btn => {
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          onObserve(btn.dataset.execId, wf.name);
        });
      });
    }

    el.appendChild(wrap);
  } else {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.innerHTML = '<p>No executions yet</p>';
    el.appendChild(empty);
  }

  return el;
}
