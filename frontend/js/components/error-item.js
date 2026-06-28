/**
 * Shared error-item renderer: ONE error item, identical behavior everywhere it
 * appears (Overview Recent Errors, Executions/Errors view, Fleet Health errors).
 *
 * Same expected actions in every context: Ask AI, Trace, View Workflow, Open in
 * n8n, Delete, Clear All for Workflow. The behaviors are global window functions
 * (window.__askErrorAI / __observeError / __deleteExecution /
 * __clearWorkflowErrors / __nav), registered app-wide at boot; this module only
 * RENDERS, and resolves the per-instance n8n URL + badge from `ctx.instanceMap`
 * so "Open in n8n" points at the error's own instance (correct cross-instance).
 *
 * ctx.instanceMap: { [instance_id]: { name, color, n8nUrl? } } (optional)
 */

function esc(s) {
  const el = document.createElement('span');
  el.textContent = s == null ? '' : String(s);
  return el.innerHTML;
}

function attr(s) {
  return esc(s).replace(/"/g, '&quot;');
}

function jsStr(s) {
  return String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function instanceBadge(id, map) {
  const inst = map && map[id];
  const name = inst ? inst.name : (id ? 'unknown' : 'no instance');
  const color = inst && inst.color ? inst.color : '#888';
  return `<span class="instance-badge" title="${attr(id)}" style="display:inline-flex;align-items:center;gap:4px;font-size:10px;padding:2px 6px;border-radius:var(--radius);background:var(--bg-input);color:var(--text-secondary);font-family:var(--font-mono)">`
    + `<span style="width:6px;height:6px;border-radius:50%;background:${esc(color)}"></span>${esc(name)}</span>`;
}

function formatTime(iso) {
  if (!iso) return '';
  try {
    return new Date(iso.replace(' ', 'T') + 'Z').toLocaleString(undefined,
      { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  } catch { return iso; }
}

export function renderErrorItem(e, ctx = {}) {
  const map = ctx.instanceMap || {};
  const inst = map[e.instance_id] || {};
  const n8nBase = (inst.n8nUrl || window.__n8nUrl || '').replace(/\/$/, '');
  const n8nExecUrl = e.execution_id && e.workflow_id && n8nBase
    ? `${n8nBase}/workflow/${esc(e.workflow_id)}/executions/${esc(e.execution_id)}`
    : '';
  return `
    <div class="error-item" onclick="this.classList.toggle('expanded')">
      <div class="error-item-header" style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="error-item-workflow" style="display:flex;align-items:center;gap:8px;flex:1;min-width:0">
          ${instanceBadge(e.instance_id, map)}
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.workflow_name)}</span>
        </span>
        <span class="error-item-time">${esc(formatTime(e.occurred_at))}</span>
      </div>
      <div class="error-item-message">${esc(e.error_message)}</div>
      <div class="error-item-detail">
        <div><strong>Workflow ID:</strong> <code style="display:inline;padding:2px 6px;font-size:11px">${esc(e.workflow_id)}</code></div>
        ${e.execution_id ? `<div><strong>Execution:</strong> <code style="display:inline;padding:2px 6px;font-size:11px">${esc(e.execution_id)}</code></div>` : ''}
        <div><strong>Node:</strong> ${esc(e.node_name || 'N/A')}</div>
        <div><strong>Type:</strong> ${esc(e.error_type)}</div>
        <code>${esc(e.error_message)}</code>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap" onclick="event.stopPropagation()">
          <button class="btn btn-sm btn-ghost err-ai-btn" data-wf="${attr(e.workflow_name)}" data-node="${attr(e.node_name || '')}" data-type="${attr(e.error_type)}" data-msg="${attr(e.error_message || '')}" data-exec="${attr(e.execution_id || '')}" data-wfid="${attr(e.workflow_id)}" onclick="event.stopPropagation();window.__askErrorAI(this)" title="Ask AI to analyze this error">&#10022; Ask AI</button>
          ${e.execution_id ? `<button class="btn btn-sm btn-ghost" data-wf="${attr(e.workflow_name)}" data-exec="${attr(e.execution_id)}" onclick="event.stopPropagation();window.__observeError(this)" title="View this execution's OpenTelemetry trace">&#128202; Trace</button>` : ''}
          <button class="btn btn-sm btn-primary" onclick="window.__nav('workflows',{selectId:'${jsStr(e.workflow_id)}'})">View Workflow</button>
          ${n8nExecUrl ? `<a class="btn btn-sm btn-ghost" href="${n8nExecUrl}" target="_blank" rel="noopener">Open in n8n</a>` : ''}
          ${e.execution_id ? `<button class="btn btn-sm btn-danger" onclick="window.__deleteExecution('${jsStr(e.execution_id)}', this)">Delete This Error</button>` : ''}
          <button class="btn btn-sm btn-danger" style="opacity:0.7" onclick="window.__clearWorkflowErrors('${jsStr(e.workflow_id)}', this)">Clear All for Workflow</button>
        </div>
        <div class="err-ai-result" style="display:none;margin-top:10px;padding:10px 12px;background:var(--bg-void);border:1px solid var(--border-dim);border-radius:var(--radius);font-size:12px;line-height:1.6" onclick="event.stopPropagation()"></div>
      </div>
    </div>
  `;
}
