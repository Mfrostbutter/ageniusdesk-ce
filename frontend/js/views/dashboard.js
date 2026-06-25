/**
 * Dashboard view — widget-based monitoring overview.
 * Widgets are DnD-reorderable, add/remove via modal.
 * Named dashboards stored in localStorage.
 */

import { get, post, del, onEvent } from '../api.js';
import * as toast from '../components/toast.js';
import * as assistantDock from '../components/assistant-dock.js';
import { WorkflowDetailPanel } from '../components/workflow-detail-panel.js';

// ── Widget registry ──────────────────────────────────────────────────────────

const WIDGETS = {
  stats:     { id: 'stats',     title: 'Stats',              size: 'full', render: mountStats },
  timeline:  { id: 'timeline',  title: 'Execution Timeline', size: 'half', render: mountTimeline },
  errors:    { id: 'errors',    title: 'Recent Errors',      size: 'half', render: mountErrors },
  health:    { id: 'health',    title: 'Workflow Health',    size: 'full', render: mountHealth },
  instances: { id: 'instances', title: 'Instances',          size: 'full', render: mountInstances },
  assistant: { id: 'assistant', title: 'Assistant',          size: 'full', render: mountAssistant },
};

const DEFAULT_LAYOUT = ['stats', 'timeline', 'errors', 'health', 'instances'];
const STORAGE_KEY = 'agd-dashboards';

// ── Dashboard storage ────────────────────────────────────────────────────────

export function getDashboards() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return [{ id: 'main', name: 'Overview', layout: [...DEFAULT_LAYOUT] }];
}

function saveDashboards(dbs) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(dbs));
  if (window.__refreshDashboardNav) window.__refreshDashboardNav();
}

function getDash(id) {
  const dbs = getDashboards();
  return dbs.find(d => d.id === id) || dbs[0];
}

// Layout is row-based: dash.rows is an ordered list of rows, each holding 1 or 2
// widget ids. A solo widget fills its row; two split it 50/50. This makes
// side-by-side pairing explicit and position-driven instead of depending on
// auto-flow parity, so a tile can be dropped beside any other tile anywhere.
const ROW_MAX = 2;

function getRows(dash) {
  if (Array.isArray(dash.rows)) {
    const rows = dash.rows
      .map(r => (Array.isArray(r) ? r : [r]).filter(id => WIDGETS[id]).slice(0, ROW_MAX))
      .filter(r => r.length);
    if (rows.length) return rows;
  }
  // Migrate a legacy flat layout (+ optional sizes) into rows: pair consecutive halves.
  const layout = (dash.layout || []).filter(id => WIDGETS[id]);
  const sizeOf = id => (dash.sizes && dash.sizes[id]) || WIDGETS[id]?.size || 'full';
  const rows = [];
  for (let i = 0; i < layout.length; ) {
    const id = layout[i], nid = layout[i + 1];
    if (sizeOf(id) === 'half' && nid && sizeOf(nid) === 'half') { rows.push([id, nid]); i += 2; }
    else { rows.push([id]); i++; }
  }
  return rows;
}

function saveRows(dashId, rows) {
  const dbs = getDashboards();
  const d = dbs.find(d => d.id === dashId);
  if (!d) return;
  const clean = rows.map(r => r.filter(id => WIDGETS[id])).filter(r => r.length);
  d.rows = clean;
  d.layout = clean.flat(); // keep flat layout in sync for any legacy reader
  delete d.sizes;          // width is now implied by row occupancy
  saveDashboards(dbs);
}

export function deleteDashboard(id) {
  if (id === 'main') return;
  saveDashboards(getDashboards().filter(d => d.id !== id));
}

// ── Main render entry point ──────────────────────────────────────────────────

let unsub = null;
let _executions = [];
// Populated by loadDashboardData so the errors widget can surface an instance
// pill on every row — the raw error payload only carries instance_id, not a
// display name or color.
let _instanceMap = {};

export function cleanup() {
  if (unsub) { unsub(); unsub = null; }
  document.getElementById('agd-step-panel')?.remove();
  document.getElementById('agd-drawer-scrim')?.remove();
  if (_wfDrawerEscHandler) { document.removeEventListener('keydown', _wfDrawerEscHandler); _wfDrawerEscHandler = null; }
}

export async function render(container) {
  if (unsub) unsub();

  const dashId = window.__viewOpts?.dashboardId || 'main';
  const dash = getDash(dashId);

  // Notify nav which dashboard is active
  if (window.__setActiveDashboard) window.__setActiveDashboard(dashId);

  container.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
      <h2 style="margin:0;font-size:16px;font-weight:600">${esc(dash.name)}</h2>
      <div style="display:flex;gap:8px">
        <button id="dash-add-widget-btn" class="btn btn-sm btn-ghost">+ Widget</button>
        ${dash.id !== 'main' ? `<button id="dash-delete-btn" class="btn btn-sm btn-ghost" style="color:var(--error)">Delete</button>` : ''}
      </div>
    </div>
    <div id="welcome-n8n-slot"></div>
    <div id="widget-grid"></div>
  `;

  const grid = container.querySelector('#widget-grid');
  renderWidgetGrid(grid, dashId);
  initWidgetDnD(grid, dashId);

  container.querySelector('#dash-add-widget-btn')?.addEventListener('click', () => {
    openAddWidgetModal(dashId);
  });
  container.querySelector('#dash-delete-btn')?.addEventListener('click', () => {
    if (!confirm(`Delete dashboard "${dash.name}"?`)) return;
    deleteDashboard(dashId);
    window.__nav('dashboard');
  });

  unsub = onEvent('error', (data) => {
    const dot = document.getElementById('error-pulse-dot');
    if (dot) { dot.classList.remove('hidden'); dot.classList.add('error-pulse'); }
    prependError(data);
  });

  loadDashboardData();
  loadInstances();
}

// ── Widget grid rendering ────────────────────────────────────────────────────

function renderWidgetGrid(grid, dashId) {
  grid.innerHTML = '';
  const rows = getRows(getDash(dashId));

  rows.forEach((row, ri) => {
    const rowEl = document.createElement('div');
    rowEl.className = 'widget-row';
    rowEl.dataset.row = String(ri);
    row.forEach(id => rowEl.appendChild(makeWidgetEl(id, dashId)));
    grid.appendChild(rowEl);
  });

  // Mount widget body content after DOM is in place
  rows.flat().forEach(id => {
    const w = WIDGETS[id];
    const body = document.getElementById(`widget-body-${id}`);
    if (w && body) w.render(body);
  });
}

function makeWidgetEl(id, dashId) {
  const w = WIDGETS[id];
  const el = document.createElement('div');
  el.className = 'widget';
  el.dataset.widgetId = id;
  // Default non-draggable. The drag handle toggles this on mousedown so tile
  // drag only initiates from the handle. Prevents inner widgets (e.g., timeline
  // drag-to-pan) from accidentally firing tile reorder on body interactions.
  el.draggable = false;
  el.innerHTML = `
    <div class="card">
      <div class="card-header widget-grip">
        <span class="widget-drag-handle" title="Drag to move or pair side by side" style="cursor:grab">
          <svg width="10" height="14" viewBox="0 0 10 14" fill="currentColor"><circle cx="3" cy="2" r="1.2"/><circle cx="7" cy="2" r="1.2"/><circle cx="3" cy="7" r="1.2"/><circle cx="7" cy="7" r="1.2"/><circle cx="3" cy="12" r="1.2"/><circle cx="7" cy="12" r="1.2"/></svg>
        </span>
        <span class="card-title">${esc(w.title)}</span>
        <button class="btn btn-sm btn-ghost widget-remove-btn" title="Remove widget" style="margin-left:auto;opacity:0;padding:2px 6px;font-size:15px;line-height:1;color:var(--text-dim)">&times;</button>
      </div>
      <div id="widget-body-${id}"></div>
    </div>
  `;

  // Enable native drag only while the handle is held.
  const handle = el.querySelector('.widget-drag-handle');
  handle.addEventListener('mousedown', () => { el.draggable = true; });
  handle.addEventListener('mouseup', () => { el.draggable = false; });
  el.addEventListener('dragend', () => { el.draggable = false; });

  el.querySelector('.widget-remove-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    const rows = getRows(getDash(dashId)).map(r => r.filter(x => x !== id)).filter(r => r.length);
    saveRows(dashId, rows);
    const grid = document.getElementById('widget-grid');
    if (grid) { renderWidgetGrid(grid, dashId); loadDashboardData(); loadInstances(); }
  });

  // Show remove button on hover
  el.addEventListener('mouseenter', () => {
    const btn = el.querySelector('.widget-remove-btn');
    if (btn) btn.style.opacity = '1';
  });
  el.addEventListener('mouseleave', () => {
    const btn = el.querySelector('.widget-remove-btn');
    if (btn) btn.style.opacity = '0';
  });

  return el;
}

// ── Widget DnD ───────────────────────────────────────────────────────────────

// Clear every drop indicator.
function clearDropMarks(grid) {
  grid.querySelectorAll('.dz-left, .dz-right').forEach(el => el.classList.remove('dz-left', 'dz-right'));
  grid.querySelectorAll('.dz-above, .dz-below').forEach(el => el.classList.remove('dz-above', 'dz-below'));
}

// Resolve where a dropped tile would land given the pointer position:
//   { kind:'side',  side:'left'|'right', tileId }  → pair beside that tile
//   { kind:'row',   where:'above'|'below', tileId } → its own full-width row
//   { kind:'end' }                                  → append at the bottom
// Decision is on the NEAREST tile: left/right thirds pair to that side, the
// middle band makes a new row above/below depending on vertical position.
function resolveDrop(grid, dragEl, x, y) {
  const tiles = [...grid.querySelectorAll('.widget[data-widget-id]')].filter(t => t !== dragEl);
  if (!tiles.length) return { kind: 'end' };

  // Target the tile directly under the pointer. (Nearest-by-center is wrong for
  // full-width tiles: their right edge can sit closer to a tile in the row below
  // than to their own center, which would mis-target every side drop.)
  let t = tiles.find(tile => {
    const r = tile.getBoundingClientRect();
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  });
  // Not over any tile (a gutter or past the last row): fall back to nearest.
  if (!t) {
    let best = Infinity;
    for (const tile of tiles) {
      const r = tile.getBoundingClientRect();
      const d = Math.hypot(x - (r.left + r.width / 2), y - (r.top + r.height / 2));
      if (d < best) { best = d; t = tile; }
    }
  }
  const r = t.getBoundingClientRect();
  const fx = (x - r.left) / r.width;   // 0..1 across the tile
  const id = t.dataset.widgetId;
  if (fx <= 0.4) return { kind: 'side', side: 'left', tileId: id };
  if (fx >= 0.6) return { kind: 'side', side: 'right', tileId: id };
  const above = (y - r.top) / r.height < 0.5;
  return { kind: 'row', where: above ? 'above' : 'below', tileId: id };
}

function paintDrop(grid, dragEl, x, y) {
  clearDropMarks(grid);
  const res = resolveDrop(grid, dragEl, x, y);
  if (res.tileId) {
    const tile = grid.querySelector(`.widget[data-widget-id="${res.tileId}"]`);
    if (res.kind === 'side' && tile) {
      tile.classList.add(res.side === 'left' ? 'dz-left' : 'dz-right');
    } else if (res.kind === 'row' && tile) {
      const row = tile.closest('.widget-row');
      if (row) row.classList.add(res.where === 'above' ? 'dz-above' : 'dz-below');
    }
  }
  return res;
}

// Mutate the row model for a drop and persist it.
function applyDrop(dashId, dragId, res) {
  let rows = getRows(getDash(dashId)).map(r => r.slice());
  // Pull the dragged tile out of wherever it currently sits.
  rows = rows.map(r => r.filter(id => id !== dragId)).filter(r => r.length);

  if (res.kind === 'end' || !res.tileId || res.tileId === dragId) {
    rows.push([dragId]);
    saveRows(dashId, rows);
    return;
  }

  let tr = -1, tc = -1;
  rows.forEach((r, ri) => { const ci = r.indexOf(res.tileId); if (ci >= 0) { tr = ri; tc = ci; } });
  if (tr === -1) { rows.push([dragId]); saveRows(dashId, rows); return; }

  if (res.kind === 'side') {
    const row = rows[tr];
    if (row.length < ROW_MAX) {
      row.splice(res.side === 'left' ? tc : tc + 1, 0, dragId);
    } else {
      // Row already full: pair the dragged tile with the targeted one and evict
      // the other occupant to its own new row just below.
      const other = row[tc === 0 ? 1 : 0];
      const paired = res.side === 'left' ? [dragId, res.tileId] : [res.tileId, dragId];
      rows.splice(tr, 1, paired, [other]);
    }
  } else {
    rows.splice(res.where === 'above' ? tr : tr + 1, 0, [dragId]);
  }
  saveRows(dashId, rows);
}

function initWidgetDnD(grid, dashId) {
  // Bind listeners once per grid node. renderWidgetGrid only swaps innerHTML, so
  // the grid element persists across re-renders within a view; re-binding would
  // stack duplicate handlers and double-apply drops. The grid node is recreated
  // (fresh, unbound) whenever the whole view re-renders.
  if (grid.dataset.dndBound === '1') return;
  grid.dataset.dndBound = '1';

  let dragEl = null;

  grid.addEventListener('dragstart', e => {
    const w = e.target.closest('.widget[data-widget-id]');
    if (!w) return;
    dragEl = w;
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', w.dataset.widgetId); } catch {}
    setTimeout(() => w.classList.add('widget--dragging'), 0);
  });

  grid.addEventListener('dragend', () => {
    clearDropMarks(grid);
    grid.querySelectorAll('.widget--dragging').forEach(el => el.classList.remove('widget--dragging'));
    grid.querySelectorAll('.widget').forEach(el => { el.draggable = false; });
    dragEl = null;
  });

  grid.addEventListener('dragover', e => {
    if (!dragEl) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    paintDrop(grid, dragEl, e.clientX, e.clientY);
  });

  grid.addEventListener('dragleave', e => {
    if (e.target === grid && !grid.contains(e.relatedTarget)) clearDropMarks(grid);
  });

  grid.addEventListener('drop', e => {
    if (!dragEl) return;
    e.preventDefault();
    const dragId = dragEl.dataset.widgetId;
    const res = resolveDrop(grid, dragEl, e.clientX, e.clientY);
    clearDropMarks(grid);
    dragEl = null;

    applyDrop(dashId, dragId, res);
    renderWidgetGrid(grid, dashId);
    loadDashboardData();
    loadInstances();
  });
}

// ── Add widget modal ─────────────────────────────────────────────────────────

function openAddWidgetModal(dashId) {
  document.getElementById('add-widget-modal')?.remove();

  const dash = getDash(dashId);
  const current = getRows(dash).flat();

  const modal = document.createElement('div');
  modal.id = 'add-widget-modal';
  modal.className = 'modal';
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });

  modal.innerHTML = `
    <div class="modal-content" style="max-width:400px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="margin:0;font-size:15px">Manage Widgets</h2>
        <button class="btn btn-sm btn-ghost" id="add-widget-close" style="font-size:18px;padding:2px 8px">&times;</button>
      </div>
      <div id="widget-checklist" style="display:flex;flex-direction:column;gap:6px">
        ${Object.values(WIDGETS).map(w => `
          <label style="display:flex;align-items:center;gap:10px;cursor:pointer;padding:9px 12px;background:var(--bg-input);border-radius:var(--radius);border:1px solid var(--border-dim)">
            <input type="checkbox" value="${w.id}" ${current.includes(w.id) ? 'checked' : ''} style="width:14px;height:14px;accent-color:var(--accent);flex-shrink:0">
            <span style="font-size:13px;font-weight:500">${esc(w.title)}</span>
          </label>
        `).join('')}
      </div>
      <p style="font-size:11px;color:var(--text-dim);margin:12px 0 0">Drag a widget's grip to a tile's left or right edge to pair them side by side.</p>
      <div style="display:flex;gap:8px;margin-top:16px">
        <button id="add-widget-apply" class="btn btn-primary" style="flex:1">Apply</button>
        <button id="add-widget-cancel" class="btn btn-ghost" style="flex:1">Cancel</button>
      </div>
    </div>
  `;

  document.body.appendChild(modal);

  modal.querySelector('#add-widget-close').addEventListener('click', () => modal.remove());
  modal.querySelector('#add-widget-cancel').addEventListener('click', () => modal.remove());
  modal.querySelector('#add-widget-apply').addEventListener('click', () => {
    const checked = Array.from(modal.querySelectorAll('#widget-checklist input:checked')).map(cb => cb.value);
    // Keep existing rows minus removed widgets; append newly-checked widgets as
    // their own full-width rows.
    let rows = getRows(getDash(dashId)).map(r => r.filter(id => checked.includes(id))).filter(r => r.length);
    const present = new Set(rows.flat());
    checked.filter(id => !present.has(id)).forEach(id => rows.push([id]));
    saveRows(dashId, rows);
    const grid = document.getElementById('widget-grid');
    if (grid) renderWidgetGrid(grid, dashId);
    loadDashboardData();
    loadInstances();
    modal.remove();
  });
}

// ── Widget mount functions ───────────────────────────────────────────────────

function mountStats(el) {
  el.innerHTML = `<div class="grid-4" id="stats-grid"></div>`;
}

function mountTimeline(el) {
  el.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <span class="time-relative" id="timeline-label"></span>
    </div>
    <div class="exec-timeline-wrap" id="exec-timeline-wrap">
      <div id="exec-timeline" class="exec-timeline"></div>
    </div>
    <div id="timeline-tooltip" class="tooltip" style="position:fixed;opacity:0;pointer-events:none;z-index:901;background:var(--bg-elevated);border:1px solid var(--border);box-shadow:0 4px 12px rgba(0,0,0,0.45);border-radius:5px;padding:6px 8px;min-width:112px;max-width:192px;font-size:11px"></div>
    <div style="display:flex;gap:14px;margin-top:10px">
      <span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--text-dim)"><span class="exec-block exec-block--success" style="width:10px;height:10px;display:inline-block"></span> Success</span>
      <span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--text-dim)"><span class="exec-block exec-block--error" style="width:10px;height:10px;display:inline-block"></span> Error</span>
      <span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--text-dim)"><span class="exec-block exec-block--running" style="width:10px;height:10px;display:inline-block"></span> Running</span>
    </div>
  `;
}

function mountErrors(el) {
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:10px">
      <span id="error-pulse-dot" class="status-dot offline hidden" style="width:6px;height:6px"></span>
      <span id="error-count-badge" class="badge hidden">0</span>
      <div style="margin-left:auto;display:flex;gap:6px">
        <button id="sync-errors-btn" class="btn btn-sm btn-ghost" onclick="window.__syncErrors(this)">Sync from n8n</button>
        <button class="btn btn-sm btn-ghost" onclick="window.__nav('errors')">View All</button>
      </div>
    </div>
    <!-- Bounded so a noisy workflow cannot stretch the widget to fill the
         viewport. 40vh caps it to under half a laptop screen; inner scroll
         surfaces older rows without pushing every dashboard widget below. -->
    <div id="dashboard-errors" style="max-height:40vh;overflow-y:auto;padding-right:4px"></div>
  `;
}

function mountHealth(el) {
  el.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <span class="card-subtitle" id="health-subtitle"></span>
      <button class="btn btn-sm btn-ghost" onclick="window.__nav('workflows')">All Workflows →</button>
    </div>
    <div id="health-grid" class="health-grid"></div>
  `;
}

function mountInstances(el) {
  el.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <span class="card-subtitle" id="instances-subtitle"></span>
      <button class="btn btn-sm btn-ghost" onclick="window.__nav('settings')">Manage →</button>
    </div>
    <div id="instances-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px"></div>
  `;
}

function mountAssistant(el) {
  assistantDock.mount(el);
}

// ── Data loading ─────────────────────────────────────────────────────────────

async function loadDashboardData() {
  try {
    const [wfData, errData, execData, statusData, instData] = await Promise.all([
      get('/api/n8n/workflows?limit=250'),
      get('/api/errors?limit=5'),
      get('/api/n8n/executions?limit=50'),
      get('/api/status'),
      get('/api/n8n/instances').catch(() => ({ instances: [] })),
    ]);

    _instanceMap = Object.fromEntries(
      (instData.instances || []).map(i => [i.id, { name: i.name || i.id, color: i.color || '#888' }])
    );

    const workflows = wfData.workflows || [];
    const errors = errData.errors || [];
    const executions = execData.executions || [];
    const activeCount = workflows.filter(w => w.active).length;
    const successCount = executions.filter(e => e.status === 'success').length;
    const errorCount = executions.filter(e => e.status === 'error').length;
    const totalExec = executions.length;
    const failRate = totalExec ? Math.round((errorCount / totalExec) * 100) : 0;
    const successRate = totalExec ? Math.round((successCount / totalExec) * 100) : 0;

    _executions = executions;
    renderStats(workflows.length, activeCount, errData.count_24h || 0, totalExec, successRate, failRate, statusData);
    renderTimeline(executions.slice(0, 30));
    renderHealthGrid(workflows, executions);
    renderErrors(errors, errData.count_24h || 0);
  } catch (e) {
    const sg = document.getElementById('stats-grid');
    if (sg) sg.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><p>Failed to load dashboard: ${esc(e.message)}</p></div>`;
  }
  // Auto-sync errors from n8n in the background; reload only the errors widget if new ones found
  post('/api/errors/sync').then(async res => {
    if (res.synced > 0) {
      const errData = await get('/api/errors?limit=5');
      renderErrors(errData.errors || [], errData.count_24h || 0);
    }
  }).catch(() => {});
}

async function loadInstances() {
  const el = document.getElementById('instances-grid');
  const sub = document.getElementById('instances-subtitle');
  if (!el) return;
  try {
    const data = await get('/api/n8n/instances');
    const instances = data.instances || [];
    if (sub) sub.textContent = `${instances.length} configured`;

    renderWelcomeSignIn(instances);

    if (!instances.length) {
      el.innerHTML = `
        <div style="grid-column:1/-1;padding:24px;text-align:center">
          <div style="color:var(--text-secondary);font-size:13px;margin-bottom:12px">No n8n instances connected yet.</div>
          <button class="btn btn-primary" onclick="window.__addInstance()">+ Add Instance</button>
        </div>`;
      return;
    }

    el.innerHTML = instances.map(inst => {
      const color = inst.color || '#ff6d5a';
      const openUrl = inst.login_url || inst.url;
      const host = (() => { try { return new URL(openUrl).hostname; } catch { return openUrl; } })();
      return `
        <div style="display:flex;align-items:center;gap:12px;padding:14px 16px;background:var(--bg-input);border-radius:var(--radius);border:1px solid ${inst.active ? color + '44' : 'var(--border-dim)'};transition:border-color .15s">
          <span style="width:10px;height:10px;border-radius:50%;background:${color};flex-shrink:0;box-shadow:0 0 6px ${color}44"></span>
          <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
              <span style="font-weight:600;font-size:13px">${esc(inst.name)}</span>
              ${inst.active ? `<span class="pill pill-success" style="font-size:9px">ACTIVE</span>` : ''}
            </div>
            <div style="font-size:11px;color:var(--text-dim);font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(host)}</div>
          </div>
          <div style="display:flex;gap:6px;flex-shrink:0">
            ${!inst.active ? `<button class="btn btn-sm btn-ghost" onclick="window.__dashSwitch('${jsStr(inst.id)}')">Switch</button>` : ''}
            <a href="${esc(openUrl)}" target="_blank" rel="noopener" class="btn btn-sm btn-ghost">Open ↗</a>
          </div>
        </div>
      `;
    }).join('');
  } catch (e) {
    if (el) el.innerHTML = `<div style="grid-column:1/-1" class="empty-state"><p>${esc(e.message)}</p></div>`;
  }
}

const WELCOME_KEY = 'agd_welcome_n8n_dismissed';
const WELCOME_EMPTY_KEY = 'agd_welcome_empty_dismissed';

function renderWelcomeSignIn(instances) {
  const slot = document.getElementById('welcome-n8n-slot');
  if (!slot) return;

  // Zero-instances state: show a one-click n8n deploy CTA.
  if (!instances.length) {
    if (localStorage.getItem(WELCOME_EMPTY_KEY) === '1') { slot.innerHTML = ''; return; }
    slot.innerHTML = `
      <div class="card" style="margin-bottom:20px;border-color:var(--accent)44">
        <div class="card-header">
          <span class="card-title">Welcome to AgeniusDesk</span>
          <button class="btn btn-sm btn-ghost" id="welcome-empty-dismiss" title="Dismiss">&times;</button>
        </div>
        <p style="font-size:12px;color:var(--text-secondary);margin:0 0 14px;line-height:1.5">
          No automation instances connected yet. Stand up n8n in Docker with one click — pre-configured admin account, ready in about a minute.
        </p>
        <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
          <button class="btn btn-primary" id="welcome-quickdeploy-n8n" style="display:inline-flex;align-items:center;gap:8px;font-weight:600">
            <span style="font-size:16px">⚡</span> Set up n8n
          </button>
          <button class="btn btn-sm btn-ghost" id="welcome-browse-templates">Browse all templates →</button>
        </div>
        <p style="font-size:11px;color:var(--text-dim);margin:12px 0 0">
          Already have an n8n running somewhere? <a href="#" id="welcome-add-existing" style="color:var(--accent)">Add it manually →</a>
        </p>
      </div>
    `;
    document.getElementById('welcome-quickdeploy-n8n').addEventListener('click', () => {
      window.__nav('containers', { quickdeploy: 'n8n' });
    });
    document.getElementById('welcome-browse-templates').addEventListener('click', () => {
      window.__nav('containers', { openDeploy: true });
    });
    document.getElementById('welcome-add-existing').addEventListener('click', (e) => {
      e.preventDefault();
      if (window.__addInstance) window.__addInstance();
    });
    document.getElementById('welcome-empty-dismiss').addEventListener('click', () => {
      localStorage.setItem(WELCOME_EMPTY_KEY, '1');
      slot.innerHTML = '';
    });
    return;
  }

  const withLogin = instances.filter(i => i.has_login);
  if (!withLogin.length) { slot.innerHTML = ''; return; }

  const currentKey = withLogin.map(i => i.id).sort().join(',');
  if (localStorage.getItem(WELCOME_KEY) === currentKey) { slot.innerHTML = ''; return; }

  slot.innerHTML = `
    <div class="card" style="margin-bottom:20px;border-color:var(--accent)44">
      <div class="card-header">
        <span class="card-title">Welcome — sign in to n8n</span>
        <button class="btn btn-sm btn-ghost" id="welcome-dismiss" title="Dismiss">&times;</button>
      </div>
      <p style="font-size:12px;color:var(--text-secondary);margin:0 0 12px">
        Your n8n sandboxes were provisioned with a pre-built owner account. Click a button below to see the URL, email, and password, then sign in.
      </p>
      <div id="welcome-buttons" style="display:flex;flex-wrap:wrap;gap:8px"></div>
    </div>
  `;

  const btnRow = document.getElementById('welcome-buttons');
  withLogin.forEach(inst => {
    const color = inst.color || '#ff6d5a';
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm';
    btn.style.cssText = 'display:inline-flex;align-items:center;gap:8px';
    btn.innerHTML = `<span class="instance-dot" style="background:${color}"></span>`;
    btn.appendChild(document.createTextNode(`Sign in to ${inst.name}`));
    btn.addEventListener('click', () => {
      if (window.__instLogin) window.__instLogin(inst.id, inst.name, color);
    });
    btnRow.appendChild(btn);
  });

  document.getElementById('welcome-dismiss').addEventListener('click', () => {
    localStorage.setItem(WELCOME_KEY, currentKey);
    slot.innerHTML = '';
  });
}

window.__deleteExecution = window.__deleteExecution || (async (executionId, btn) => {
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Deleting...';
  try {
    await del(`/api/errors/${encodeURIComponent(executionId)}`);
    btn.closest('.error-item')?.remove();
  } catch (e) {
    alert('Error: ' + e.message);
    btn.disabled = false;
    btn.textContent = orig;
  }
});

window.__syncErrors = async (btn) => {
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Syncing...';
  try {
    const res = await post('/api/errors/sync');
    if (res.synced > 0) {
      toast.success(`Synced ${res.synced} error${res.synced === 1 ? '' : 's'} from n8n`);
      loadDashboardData();
    } else {
      toast.success('Already up to date');
    }
  } catch (e) { toast.error(e.message); }
  finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
};

window.__dashSwitch = async (id) => {
  try {
    await post(`/api/n8n/instances/${id}/activate`);
    if (window.__refreshInstances) window.__refreshInstances();
    loadDashboardData();
    loadInstances();
    toast.success('Switched instance');
  } catch (e) { toast.error(e.message); }
};

// ── Stats ────────────────────────────────────────────────────────────────────

function renderStats(total, active, errors24h, runs, successRate, failRate, status) {
  const el = document.getElementById('stats-grid');
  if (!el) return;
  const instanceName = status.active_instance ? status.active_instance.name : '';
  el.innerHTML = `
    <div class="stat-card-accent stat-card-accent--info" style="cursor:pointer" onclick="window.__nav('workflows', { filter: 'all' })" title="View all workflows">
      <div class="stat-value">${total}</div>
      <div class="stat-label">Workflows</div>
      ${instanceName ? `<div class="stat-trend stat-trend--neutral">${esc(instanceName)}</div>` : ''}
    </div>
    <div class="stat-card-accent stat-card-accent--success" style="cursor:pointer" onclick="window.__nav('workflows', { filter: 'active' })" title="View active workflows">
      <div class="stat-value" style="color:var(--success)">${active}</div>
      <div class="stat-label">Active</div>
      <div class="stat-trend stat-trend--neutral">${total ? Math.round((active / total) * 100) : 0}% of total</div>
    </div>
    <div class="stat-card-accent stat-card-accent--error" style="cursor:pointer" onclick="window.__nav('errors')" title="View errors">
      <div class="stat-value" style="color:${failRate > 0 ? 'var(--error)' : 'var(--text-primary)'}">${failRate}%</div>
      <div class="stat-label">Failure Rate</div>
      <div class="stat-trend ${(errors24h > 0 || failRate > 20) ? 'stat-trend--down' : 'stat-trend--up'}">${errors24h > 0 ? `${errors24h} errors (24h)` : failRate > 20 ? `${failRate}% of recent runs failed` : 'All clear'}</div>
    </div>
    <div class="stat-card-accent stat-card-accent--warning" style="cursor:pointer" onclick="window.__nav('workflows')" title="View executions">
      <div class="stat-value">${runs}</div>
      <div class="stat-label">Executions</div>
      <div class="stat-trend ${successRate >= 80 ? 'stat-trend--up' : successRate >= 50 ? 'stat-trend--neutral' : 'stat-trend--down'}">${successRate}% success</div>
    </div>
  `;
}

// ── Execution Timeline ───────────────────────────────────────────────────────

function renderTimeline(executions) {
  const el = document.getElementById('exec-timeline');
  const label = document.getElementById('timeline-label');
  if (!el) return;

  if (!el.dataset.wheelWired) {
    const wrap = document.getElementById('exec-timeline-wrap');

    const updateScrollShadows = () => {
      if (!wrap) return;
      const overflow = el.scrollWidth - el.clientWidth;
      if (overflow <= 0) { delete wrap.dataset.scrollLeft; delete wrap.dataset.scrollRight; return; }
      const atStart = el.scrollLeft <= 1;
      const atEnd   = el.scrollLeft >= overflow - 1;
      if (atStart) delete wrap.dataset.scrollLeft; else wrap.dataset.scrollLeft = '1';
      if (atEnd)   delete wrap.dataset.scrollRight; else wrap.dataset.scrollRight = '1';
    };

    el.addEventListener('wheel', (ev) => {
      if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) return;
      if (el.scrollWidth <= el.clientWidth) return;
      ev.preventDefault();
      el.scrollLeft += ev.deltaY;
    }, { passive: false });

    let dragState = null;
    el.addEventListener('mousedown', (ev) => {
      if (ev.button !== 0) return;
      dragState = { startX: ev.clientX, startScroll: el.scrollLeft };
      el.classList.add('is-dragging');
    });
    window.addEventListener('mousemove', (ev) => {
      if (!dragState) return;
      el.scrollLeft = dragState.startScroll + (dragState.startX - ev.clientX);
    });
    const endDrag = () => { if (!dragState) return; dragState = null; el.classList.remove('is-dragging'); };
    window.addEventListener('mouseup', endDrag);
    window.addEventListener('mouseleave', endDrag);

    el.addEventListener('scroll', updateScrollShadows, { passive: true });
    window.addEventListener('resize', updateScrollShadows);

    el.dataset.wheelWired = '1';
    el.__updateNav = updateScrollShadows;
  }

  if (!executions.length) {
    el.innerHTML = '<div class="empty-state" style="padding:20px 0"><p>No recent executions</p></div>';
    el.__updateNav?.();
    return;
  }

  const ordered = [...executions].reverse();
  el.innerHTML = ordered.map((e, i) => {
    const execId = e.id || e.execution_id;
    const url = (e.workflow_id && execId && window.__n8nUrl)
      ? `${window.__n8nUrl}/workflow/${e.workflow_id}/executions/${execId}`
      : (window.__n8nUrl ? `${window.__n8nUrl}/executions` : '');
    return `<div class="exec-block exec-block--${statusClass(e.status)}"
         onclick="if('${jsStr(url)}')window.open('${jsStr(url)}','_blank')"
         data-idx="${executions.length - 1 - i}"
         data-name="${esc(e.workflow_name)}"
         data-status="${e.status}"
         data-time="${relativeTime(e.started_at)}">
    </div>`;
  }).join('');

  if (label) label.textContent = `Last ${executions.length} runs`;
  el.__updateNav?.();

  const tooltip = document.getElementById('timeline-tooltip');
  if (!tooltip) return;

  el.addEventListener('mouseover', (ev) => {
    const block = ev.target.closest('.exec-block');
    if (!block) return;
    tooltip.innerHTML = `
      <div style="font-weight:600;margin-bottom:1px">${block.dataset.name}</div>
      <div style="display:flex;gap:6px;align-items:center">
        <span class="pill pill-${statusClass(block.dataset.status)}" style="font-size:8px">${block.dataset.status}</span>
        <span class="time-relative">${block.dataset.time}</span>
      </div>
      <div style="font-size:8px;color:rgba(255,255,255,.5);margin-top:2px">Click to open in n8n ↗</div>
    `;
    const rect = block.getBoundingClientRect();
    const gap = 24;
    tooltip.style.left = `${rect.left + rect.width / 2}px`;
    tooltip.style.transform = 'translateX(-50%)';
    tooltip.style.top = '-9999px'; // off-screen while browser lays out new content
    requestAnimationFrame(() => {
      const ttH = tooltip.getBoundingClientRect().height;
      tooltip.style.top = rect.top - gap >= ttH
        ? `${rect.top - gap - ttH}px`
        : `${rect.bottom + gap}px`;
      tooltip.style.opacity = '1';
    });
  }, true);

  el.addEventListener('mouseout', (ev) => {
    if (ev.target.closest('.exec-block')) tooltip.style.opacity = '0';
  }, true);
  el.addEventListener('click', () => { tooltip.style.opacity = '0'; });
}

// ── Workflow Health Grid ─────────────────────────────────────────────────────

function renderHealthGrid(workflows, executions) {
  const subtitle = document.getElementById('health-subtitle');
  const el = document.getElementById('health-grid');
  if (!el) return;

  const active = workflows.filter(w => w.active);
  if (subtitle) subtitle.textContent = `${active.length} active / ${workflows.length} total`;

  if (!active.length) {
    el.innerHTML = '<div class="empty-state"><p>No active workflows</p></div>';
    return;
  }

  const lastStatus = {};
  for (const e of executions) {
    if (!lastStatus[e.workflow_name]) lastStatus[e.workflow_name] = e.status;
  }

  const display = active.slice(0, 12);
  const remaining = active.length - display.length;

  el.innerHTML = display.map(w => {
    const status = lastStatus[w.name] || 'active';
    const dotClass = status === 'error' ? 'offline' : status === 'running' ? 'checking' : 'online';
    return `
      <div class="health-card" data-wf-id="${jsStr(w.id)}" data-wf-name="${jsStr(w.name)}" data-wf-status="${jsStr(status)}" style="cursor:pointer">
        <div class="health-card-name" title="${esc(w.name)}">${esc(w.name)}</div>
        <div class="health-card-meta">
          <span class="health-card-status"><span class="status-dot ${dotClass}"></span> ${status}</span>
          <span class="health-card-trigger">${triggerIcon(w.trigger_type)}</span>
        </div>
      </div>
    `;
  }).join('') + (remaining > 0 ? `<div class="health-card" style="display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--text-dim)" onclick="window.__nav('workflows')">+${remaining} more</div>` : '');

  el.querySelectorAll('.health-card[data-wf-id]').forEach(card => {
    card.addEventListener('click', () => {
      openWorkflowDrawer(card.dataset.wfId, card.dataset.wfName, card.dataset.wfStatus);
    });
  });
}

// ── Error Feed ───────────────────────────────────────────────────────────────

function renderErrors(errors, count24h) {
  const el = document.getElementById('dashboard-errors');
  const badge = document.getElementById('error-count-badge');
  if (!el) return;

  if (badge && count24h > 0) {
    badge.textContent = count24h;
    badge.classList.remove('hidden');
  }

  el.innerHTML = errors.length
    ? errors.map(renderErrorItem).join('')
    : '<div class="empty-state" style="padding:20px 0"><p>No recent errors</p></div>';
}

function prependError(error) {
  const el = document.getElementById('dashboard-errors');
  if (!el) return;
  el.querySelector('.empty-state')?.remove();
  const div = document.createElement('div');
  div.innerHTML = renderErrorItem(error);
  el.prepend(div.firstElementChild);
  const badge = document.getElementById('error-count-badge');
  if (badge) {
    badge.textContent = (parseInt(badge.textContent) || 0) + 1;
    badge.classList.remove('hidden');
  }
}

function _instanceBadge(id) {
  const inst = _instanceMap[id];
  const name = inst ? inst.name : (id ? 'unknown' : 'no instance');
  const color = inst && inst.color ? inst.color : '#888';
  return `<span class="instance-badge" title="${esc(id)}" style="display:inline-flex;align-items:center;gap:4px;font-size:10px;padding:2px 6px;border-radius:var(--radius);background:var(--bg-input);color:var(--text-secondary);font-family:var(--font-mono)">`
    + `<span style="width:6px;height:6px;border-radius:50%;background:${esc(color)}"></span>`
    + `${esc(name)}</span>`;
}

function renderErrorItem(e) {
  const n8nBase = (window.__n8nUrl || '').replace(/\/$/, '');
  const n8nExecUrl = e.execution_id && e.workflow_id && n8nBase
    ? `${n8nBase}/workflow/${esc(e.workflow_id)}/executions/${esc(e.execution_id)}`
    : '';
  return `
    <div class="error-item" onclick="this.classList.toggle('expanded')">
      <div class="error-item-header" style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="error-item-workflow" style="display:flex;align-items:center;gap:8px;flex:1;min-width:0">
          ${_instanceBadge(e.instance_id)}
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.workflow_name)}</span>
        </span>
        <span class="time-relative">${relativeTime(e.occurred_at)}</span>
      </div>
      <div class="error-item-message">${esc(e.error_message)}</div>
      <div class="error-item-detail">
        <div><strong>Node:</strong> ${esc(e.node_name || 'N/A')}</div>
        <div><strong>Type:</strong> ${esc(e.error_type)}</div>
        ${e.execution_id ? `<div><strong>Execution:</strong> <code style="display:inline;padding:2px 6px;font-size:11px">${esc(e.execution_id)}</code></div>` : ''}
        <code>${esc(e.error_message)}</code>
        <div style="display:flex;gap:8px;margin-top:10px" onclick="event.stopPropagation()">
          <button class="btn btn-sm btn-primary" onclick="window.__nav('workflows',{selectId:'${jsStr(e.workflow_id)}'})">View Workflow</button>
          ${n8nExecUrl ? `<a class="btn btn-sm btn-ghost" href="${n8nExecUrl}" target="_blank" rel="noopener">Open in n8n</a>` : ''}
          ${e.execution_id ? `<button class="btn btn-sm btn-danger" onclick="window.__deleteExecution('${jsStr(e.execution_id)}', this)">Delete This Error</button>` : ''}
        </div>
      </div>
    </div>
  `;
}

// ── Execution Detail Modal ───────────────────────────────────────────────────

async function openExecModal(exec) {
  document.getElementById('exec-detail-modal')?.remove();

  const statusCls = statusClass(exec.status);
  const modal = document.createElement('div');
  modal.id = 'exec-detail-modal';
  modal.className = 'modal';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };

  const errorSection = exec.error_message ? `
    <div style="margin-top:12px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <span style="font-size:12px;font-weight:600;color:var(--error)">Error</span>
        <button class="btn btn-sm btn-ghost" onclick="window.__copyExecError('${jsStr(exec.execution_id || '')}')">Copy Error</button>
      </div>
      <pre id="exec-error-${esc(exec.execution_id || 'x')}" style="background:var(--bg-void);border:1px solid var(--border-dim);border-radius:var(--radius);padding:10px 12px;font-size:11px;font-family:var(--font-mono);color:var(--error);overflow-x:auto;white-space:pre-wrap;max-height:200px;overflow-y:auto">${esc(exec.error_message)}</pre>
    </div>
  ` : '';

  modal.innerHTML = `
    <div class="modal-content" style="max-width:560px">
      <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:16px">
        <div>
          <h2 style="margin:0 0 4px;font-size:16px">${esc(exec.workflow_name)}</h2>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="pill pill-${statusCls}" style="font-size:10px">${exec.status}</span>
            <span style="font-size:12px;color:var(--text-secondary)">${relativeTime(exec.started_at)}</span>
          </div>
        </div>
        <button class="btn btn-sm btn-ghost" onclick="document.getElementById('exec-detail-modal').remove()" style="font-size:18px;padding:2px 8px">&times;</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:12px">
        <div style="background:var(--bg-input);border-radius:var(--radius);padding:8px 10px">
          <div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">STARTED</div>
          <div style="font-size:12px">${exec.started_at ? new Date(exec.started_at).toLocaleTimeString() : '—'}</div>
        </div>
        <div style="background:var(--bg-input);border-radius:var(--radius);padding:8px 10px">
          <div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">DURATION</div>
          <div style="font-size:12px" id="exec-duration">—</div>
        </div>
        <div style="background:var(--bg-input);border-radius:var(--radius);padding:8px 10px">
          <div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">EXECUTION ID</div>
          <div style="font-size:11px;font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis">${esc(exec.execution_id || '—')}</div>
        </div>
      </div>
      ${errorSection}
      <div id="exec-detail-loading" style="margin-top:12px;font-size:12px;color:var(--text-dim)">Loading full details...</div>
    </div>
  `;

  document.body.appendChild(modal);

  window.__execErrors = window.__execErrors || {};
  window.__execErrors[exec.execution_id] = exec.error_message || '';

  if (exec.execution_id) {
    try {
      const detail = await get(`/api/n8n/executions/${exec.execution_id}`);
      const loadingEl = document.getElementById('exec-detail-loading');
      if (!loadingEl) return;

      const duration = detail.started_at && detail.finished_at
        ? `${((new Date(detail.finished_at) - new Date(detail.started_at)) / 1000).toFixed(1)}s`
        : '—';
      const durEl = document.getElementById('exec-duration');
      if (durEl) durEl.textContent = duration;

      const nodes = detail.data?.resultData?.runData || {};
      const failedNodes = Object.entries(nodes)
        .filter(([, runs]) => runs?.some(r => r.error))
        .map(([name, runs]) => ({ name, error: runs.find(r => r.error)?.error?.message || 'Unknown error' }));

      if (failedNodes.length) {
        const fullError = failedNodes.map(n => `[${n.name}] ${n.error}`).join('\n');
        window.__execErrors[exec.execution_id] = fullError;
        const errEl = document.getElementById(`exec-error-${exec.execution_id}`);
        if (errEl) errEl.textContent = fullError;
        loadingEl.innerHTML = `<span style="color:var(--error)">${failedNodes.length} failed node${failedNodes.length > 1 ? 's' : ''}</span>`;
      } else {
        loadingEl.innerHTML = `<span style="color:var(--success)">✓ ${Object.keys(nodes).length} nodes completed</span>`;
      }

      if (window.__n8nUrl && exec.execution_id) {
        loadingEl.innerHTML += ` &nbsp;<a href="${window.__n8nUrl}/workflow/${detail.workflowId || ''}/executions/${exec.execution_id}" target="_blank" rel="noopener" style="color:var(--accent);font-size:12px">Open in n8n ↗</a>`;
      }
    } catch {
      const loadingEl = document.getElementById('exec-detail-loading');
      if (loadingEl) loadingEl.innerHTML = '';
    }
  } else {
    const loadingEl = document.getElementById('exec-detail-loading');
    if (loadingEl) loadingEl.innerHTML = '';
  }
}

window.__copyExecError = (execId) => {
  const text = (window.__execErrors || {})[execId] || '';
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => toast.success('Error copied!')).catch(() => {
    document.getElementById(`exec-error-${execId}`)?.select?.();
  });
};

// ── Workflow detail drawer ───────────────────────────────────────────────────

let _wfDrawerEscHandler = null;

async function openWorkflowDrawer(workflowId, workflowName, wfStatus) {
  // Close any existing drawer first.
  const prev = document.getElementById('agd-step-panel');
  if (prev) { prev.remove(); }
  if (_wfDrawerEscHandler) { document.removeEventListener('keydown', _wfDrawerEscHandler); _wfDrawerEscHandler = null; }
  document.getElementById('agd-drawer-scrim')?.remove();

  const scrim = document.createElement('div');
  scrim.id = 'agd-drawer-scrim';
  scrim.className = 'drawer-scrim';
  document.body.appendChild(scrim);

  const panel = document.createElement('div');
  panel.id = 'agd-step-panel';
  panel.className = 'agd-step-panel agd-step-panel--wide';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'false');
  panel.setAttribute('aria-label', `Workflow: ${workflowName}`);

  panel.innerHTML = `
    <div class="agd-step-panel-header">
      <span class="agd-step-panel-title">${esc(workflowName)}</span>
      <div style="display:flex;gap:8px;align-items:center;flex-shrink:0">
        <button class="btn btn-sm btn-ghost" id="agd-wf-nav-btn" style="font-size:12px">View in Workflows →</button>
        <button class="agd-step-panel-close" id="agd-panel-close" aria-label="Close">&#x2715;</button>
      </div>
    </div>
    <div class="agd-step-panel-body" id="agd-wf-drawer-body">
      <div class="spinner"></div>
    </div>
  `;

  document.body.appendChild(panel);
  requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.add('is-open')));

  function closeDrawer() {
    panel.classList.remove('is-open');
    scrim.remove();
    panel.addEventListener('transitionend', () => panel.remove(), { once: true });
    if (_wfDrawerEscHandler) { document.removeEventListener('keydown', _wfDrawerEscHandler); _wfDrawerEscHandler = null; }
    document.removeEventListener('agd:view-changed', navHandler);
  }

  scrim.addEventListener('click', closeDrawer);
  const navHandler = () => closeDrawer();
  document.addEventListener('agd:view-changed', navHandler);

  document.getElementById('agd-panel-close').addEventListener('click', closeDrawer);
  document.getElementById('agd-wf-nav-btn').addEventListener('click', () => {
    closeDrawer();
    window.__nav('workflows', { selectId: workflowId });
  });

  _wfDrawerEscHandler = (e) => { if (e.key === 'Escape') closeDrawer(); };
  document.addEventListener('keydown', _wfDrawerEscHandler);

  // Fetch full workflow data and render the feature-complete detail panel.
  const bodyEl = document.getElementById('agd-wf-drawer-body');
  try {
    const [wf, execData] = await Promise.all([
      get(`/api/n8n/workflows/${workflowId}`),
      get(`/api/n8n/executions?workflow_id=${workflowId}&limit=15`),
    ]);
    const executions = execData.executions || [];
    if (!bodyEl) return;
    bodyEl.innerHTML = '';
    bodyEl.appendChild(WorkflowDetailPanel(wf, executions, {
      onActivate: async (id, active) => {
        try {
          const result = await post(`/api/n8n/workflows/${id}/active`, { active });
          if (result.success) {
            toast.success(`Workflow ${active ? 'activated' : 'deactivated'}`);
            loadDashboardData();
            openWorkflowDrawer(id, wf.name, active ? 'success' : 'active');
          } else {
            toast.error(result.error || 'Failed');
          }
        } catch (e) { toast.error(e.message); }
      },
      onInject: (id) => {
        if (window.__injectDashboardTrigger) window.__injectDashboardTrigger(id);
      },
      onRemove: (id) => {
        if (window.__removeDashboardTrigger) window.__removeDashboardTrigger(id);
      },
      onDelete: async (id) => {
        if (!confirm(`Delete workflow "${wf.name}"? This cannot be undone.`)) return;
        try {
          const result = await fetch(`/api/n8n/workflows/${id}`, { method: 'DELETE' }).then(r => r.json());
          if (result.success) {
            toast.success(`Deleted: ${wf.name}`);
            closeDrawer();
            loadDashboardData();
          } else {
            toast.error(result.error || 'Delete failed');
          }
        } catch (e) { toast.error(e.message); }
      },
      onAnalyze: (execId, wfName, wfId) => {
        if (window.__analyzeExec) window.__analyzeExec(execId, wfName, wfId);
      },
    }));
  } catch (e) {
    if (bodyEl) bodyEl.innerHTML = `<div class="empty-state"><p style="color:var(--error)">Failed to load: ${esc(e.message)}</p></div>`;
  }
}

// ── Utilities ────────────────────────────────────────────────────────────────

function relativeTime(iso) {
  if (!iso) return 'just now';
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function statusClass(s) {
  if (s === 'success') return 'success';
  if (s === 'error') return 'error';
  if (s === 'running') return 'running';
  if (s === 'waiting') return 'waiting';
  return 'success';
}

function triggerIcon(t) {
  if (t === 'webhook') return 'webhook';
  if (t === 'schedule') return 'schedule';
  if (t === 'manual') return 'manual';
  if (t === 'error') return 'error';
  return t || 'other';
}

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }


function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}
