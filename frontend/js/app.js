/**
 * AgeniusDesk — Main application entry point.
 */

import { get, post, connectWS, onEvent } from './api.js';
import { requireAuth } from './views/login.js';
import { loadTheme, setActiveTheme, getCurrentTheme } from './themes.js';
import * as modal from './components/modal.js';
import * as toast from './components/toast.js';
import * as player from './components/player.js';
import { secretField, invalidateRefsCache } from './components/secretfield.js';

import * as wizard from './wizard.js';
import * as dashboardView from './views/dashboard.js';
import * as workflowsView from './views/workflows.js';
import * as errorsView from './views/errors.js';
import * as importView from './views/import.js';
import * as backupView from './views/backup.js';
import * as codelabView from './views/codelab.js';
import * as assistantView from './views/assistant.js';
import * as settingsView from './views/settings.js';
import * as adminView from './views/admin.js';
import * as musicView from './views/music.js';
import * as secretsView from './views/secrets.js';
import * as notesView from './views/notes.js';
import * as insightsView from './views/insights.js';
import * as observabilityView from './views/observability.js';
import * as knowledgeView from './views/knowledge.js';
import * as knowledgeConnectorsView from './views/knowledge-connectors.js';
import * as knowledgeInstructionsView from './views/knowledge-instructions.js';
import * as containersView from './views/containers.js';
import * as instancesView from './views/instances.js';
import * as modelsView from './views/models.js';
import * as mcpView from './views/mcp.js';

import { loadCommunityModules, installHostContext } from './community-modules.js';
import * as onboarding from './onboarding/index.js';

// Expose window.AgeniusDesk early so any module script injected during
// boot can assume it's present.
installHostContext();

// Dev convenience: visiting with `?fresh=1` clears all local UI state — coachmark
// "seen" flags, the get-started/connect/error-handler dismissals, dashboards,
// and prefs — for a true first-run walkthrough. Runs before any module reads
// localStorage, then strips the param so a reload doesn't wipe again.
if (new URLSearchParams(location.search).has('fresh')) {
  try { localStorage.clear(); } catch { /* ignore */ }
  try { history.replaceState({}, '', location.pathname); } catch { /* ignore */ }
}

const views = {
  dashboard: dashboardView,
  workflows: workflowsView,
  errors: errorsView,
  import: importView,
  backup: backupView,
  codelab: codelabView,
  assistant: assistantView,
  music: musicView,
  settings: settingsView,
  admin: adminView,
  secrets: secretsView,
  notes: notesView,
  insights: insightsView,
  observe: observabilityView,
  knowledge: knowledgeView,
  'knowledge-connectors': knowledgeConnectorsView,
  'knowledge-instructions': knowledgeInstructionsView,
  containers: containersView,
  // Sidebar drill-downs that used to deep-link into Settings tabs. Now first-
  // class focused views (no Settings tab strip). data-view names in index.html:
  // instances / ai-settings / mcp-servers.
  instances: instancesView,
  'ai-settings': modelsView,
  'mcp-servers': mcpView,
};

// Community modules are loaded async on boot; their views merge into the
// `views` object below. Keys are namespaced as `community:{module_id}` so
// they can't collide with built-ins.
loadCommunityModules().then(communityViews => {
  Object.assign(views, communityViews);
});

let currentView = 'dashboard';

// ── Navigation ──────────────────────────────────────────────────────────────

async function navigate(viewName, opts) {
  const view = views[viewName];
  if (!view) return;
  // Remember where we came from so "Replay tips on this page" (invoked from the
  // Settings view) can target the view the user was actually on.
  if (currentView && currentView !== viewName) window.__priorView = currentView;
  currentView = viewName;
  window.__currentView = viewName;
  // Stash opts for the target view to read on mount. Always reset (so stale
  // opts from a previous navigate call never leak into a sidebar click).
  window.__viewOpts = opts || null;
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === viewName);
  });
  // First-visit marker (used by the "Meet the harness" onboarding milestone and
  // any future per-view first-run logic).
  try { localStorage.setItem(`agd_seen:${viewName}`, '1'); } catch { /* ignore */ }
  document.dispatchEvent(new CustomEvent('agd:view-changed', { detail: { view: viewName } }));
  await view.render(document.getElementById('app-content'));
  // Fire AFTER render resolves so the coachmark engine sees the new view's DOM,
  // not the previous view's stale content. (agd:view-changed stays pre-render
  // for listeners that must react immediately, e.g. closing an open drawer.)
  document.dispatchEvent(new CustomEvent('agd:view-rendered', { detail: { view: viewName } }));
}

// Auto-run a view's coachmark tour on first visit (engine self-guards on
// tips-enabled, seen-flag, blocking overlays, and anchor presence).
document.addEventListener('agd:view-rendered', e => onboarding.maybeRunTour(e.detail.view));
window.__replayTour = onboarding.replayTour;
window.__resetTips = onboarding.resetAllTips;
window.__setTipsEnabled = onboarding.setTipsEnabled;
window.__tipsEnabled = onboarding.tipsEnabled;

window.__nav = navigate;
window.__currentView = currentView;

// Navigate to settings and open a specific tab. Returns a promise so
// shortcut-button click handlers can restore their own .active highlight
// after navigate() has flipped it to the Settings button.
window.__goSettings = (tab) => {
  return navigate('settings').then(() => {
    if (tab && window.__settingsTab) window.__settingsTab(tab);
  });
};

// Right-click "Open in new tab" context menu for nav buttons.
(function () {
  let _menu = null;
  function _removeMenu() { if (_menu) { _menu.remove(); _menu = null; } }
  document.addEventListener('click', _removeMenu);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') _removeMenu(); });

  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('contextmenu', e => {
      e.preventDefault();
      _removeMenu();
      const viewName = btn.dataset.view;
      if (!viewName) return;
      const url = `${location.origin}${location.pathname}#${viewName}`;
      _menu = document.createElement('div');
      _menu.style.cssText = `position:fixed;z-index:9999;left:${e.clientX}px;top:${e.clientY}px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:6px;padding:4px 0;box-shadow:0 4px 16px rgba(0,0,0,.4);min-width:160px`;
      _menu.innerHTML = `
        <div class="nav-ctx-item" style="padding:7px 14px;font-size:13px;cursor:pointer;display:flex;align-items:center;gap:8px;color:var(--text-primary)">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          Open in new tab
        </div>`;
      _menu.querySelector('.nav-ctx-item').addEventListener('click', () => {
        window.open(url, '_blank');
        _removeMenu();
      });
      document.body.appendChild(_menu);
      // Keep menu on screen.
      const r = _menu.getBoundingClientRect();
      if (r.right > window.innerWidth) _menu.style.left = `${e.clientX - r.width}px`;
      if (r.bottom > window.innerHeight) _menu.style.top = `${e.clientY - r.height}px`;
    });
  });
})();

document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const viewName = btn.dataset.view;
    // A few sidebar items remain Settings deep-links (panels with no dedicated
    // view of their own). Instances / Models / MCP are now first-class views in
    // the `views` map above, so they fall through to navigate() and render
    // focused, tab-strip-free pages.
    const SETTINGS_SHORTCUTS = {
      'plugins': 'modules',
    };
    if (SETTINGS_SHORTCUTS[viewName]) {
      if (window.__goSettings) {
        window.__goSettings(SETTINGS_SHORTCUTS[viewName]).then(() => {
          // navigate('settings') set .active on the Settings gear; override
          // it so the shortcut the user actually clicked stays highlighted.
          document.querySelectorAll('.nav-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.view === viewName);
          });
        });
      }
      return;
    }
    navigate(viewName);
  });
});

// ── Nav: collapsible groups ──────────────────────────────────────────────────

function initNavCollapse() {
  const stored = JSON.parse(localStorage.getItem('nav-collapsed') || '{}');
  document.querySelectorAll('.nav-group-label[data-collapses]').forEach(label => {
    const group = label.dataset.collapses;
    const el = label.closest('.nav-group');
    if (stored[group]) el.classList.add('collapsed');
    label.addEventListener('click', () => {
      const collapsed = el.classList.toggle('collapsed');
      stored[group] = collapsed;
      localStorage.setItem('nav-collapsed', JSON.stringify(stored));
    });
  });
}

// ── Nav: Knowledge → Sources child toggle ───────────────────────────────────

function initKnowledgeChildren() {
  const group = document.querySelector('.nav-item-group[data-item-group="knowledge"]');
  if (!group) return;
  const parent = group.querySelector('.nav-parent');
  const stored = localStorage.getItem('knowledge-children-open');
  if (stored !== 'false') group.classList.add('open');
  parent.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = group.classList.toggle('open');
    localStorage.setItem('knowledge-children-open', open);
    navigate(parent.dataset.view);
  });
}

// ── Nav: drag-to-reorder ─────────────────────────────────────────────────────

function initNavDrag() {
  let dragSrc = null;

  document.querySelectorAll('.nav-group[data-group]').forEach(group => {
    const groupId = group.dataset.group;

    function applyStoredOrder() {
      const order = JSON.parse(localStorage.getItem(`nav-order-${groupId}`) || 'null');
      if (!order) return;
      const btns = Array.from(group.querySelectorAll(':scope > .nav-btn[draggable], :scope > .nav-item-group'));
      order.forEach(view => {
        const el = btns.find(b => (b.dataset.view || b.dataset.itemGroup) === view);
        if (el) group.appendChild(el);
      });
    }
    applyStoredOrder();

    function saveOrder() {
      const items = Array.from(group.querySelectorAll(':scope > .nav-btn[draggable], :scope > .nav-item-group'))
        .map(el => el.dataset.view || el.dataset.itemGroup);
      localStorage.setItem(`nav-order-${groupId}`, JSON.stringify(items));
    }

    group.addEventListener('dragstart', e => {
      const btn = e.target.closest('.nav-btn[draggable="true"], .nav-item-group');
      if (!btn || !group.contains(btn)) return;
      dragSrc = btn;
      e.dataTransfer.effectAllowed = 'move';
    });

    group.addEventListener('dragover', e => {
      e.preventDefault();
      const target = e.target.closest('.nav-btn[draggable="true"], .nav-item-group');
      if (!target || target === dragSrc || !group.contains(target)) return;
      document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
      target.classList.add('drag-over');
    });

    group.addEventListener('dragleave', e => {
      const target = e.target.closest('.nav-btn, .nav-item-group');
      if (target) target.classList.remove('drag-over');
    });

    group.addEventListener('drop', e => {
      e.preventDefault();
      const target = e.target.closest('.nav-btn[draggable="true"], .nav-item-group');
      document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
      if (!target || target === dragSrc || !group.contains(target)) return;
      group.insertBefore(dragSrc, target);
      saveOrder();
      dragSrc = null;
    });

    group.addEventListener('dragend', () => {
      document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
      dragSrc = null;
    });
  });
}

// ── Nav: community module visibility toggles ─────────────────────────────────

export function applyModuleVisibility() {
  const hidden = JSON.parse(localStorage.getItem('nav-modules-hidden') || '[]');
  document.querySelectorAll('.nav-btn[data-module-nav]').forEach(btn => {
    btn.style.display = hidden.includes(btn.dataset.moduleNav) ? 'none' : '';
  });
}

export function setModuleNavVisible(moduleId, visible) {
  const hidden = new Set(JSON.parse(localStorage.getItem('nav-modules-hidden') || '[]'));
  visible ? hidden.delete(moduleId) : hidden.add(moduleId);
  localStorage.setItem('nav-modules-hidden', JSON.stringify([...hidden]));
  applyModuleVisibility();
}

window.__setModuleNavVisible = setModuleNavVisible;

// Boot all nav enhancements
initNavCollapse();
initKnowledgeChildren();
initNavDrag();

// ── WebSocket status ────────────────────────────────────────────────────────

const statusDot = document.getElementById('connection-status');
const connLabel = document.getElementById('conn-label');

onEvent('ws:connected', () => {
  statusDot.className = 'status-dot online';
  if (connLabel) connLabel.textContent = 'Connected';
});

onEvent('ws:disconnected', () => {
  statusDot.className = 'status-dot offline';
  if (connLabel) connLabel.textContent = 'Reconnecting...';
});

onEvent('error', () => {
  const badge = document.getElementById('error-badge');
  if (badge) {
    badge.textContent = (parseInt(badge.textContent) || 0) + 1;
    badge.classList.remove('hidden');
  }
});

onEvent('message', (data) => {
  const level = ['info', 'success', 'warning', 'error'].includes(data?.level) ? data.level : 'info';
  const head = data?.title?.trim();
  const tail = data?.body?.trim();
  const text = head && tail ? `${head}: ${tail}` : (head || tail || 'New message');
  const fn = toast[level] || toast.info;
  fn(text);
});

// ── Add-instance modal (subsequent adds — first-run uses the wizard) ────────

window.__hasInstances = false;

// SecretField for the API key in the setup modal. Created lazily on first show.
let setupKeyField = null;

function closeSetupModal() {
  modal.hide('setup-modal');
}

window.__closeSetupModal = closeSetupModal;

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  const setupModal = document.getElementById('setup-modal');
  if (setupModal && !setupModal.classList.contains('hidden')) {
    closeSetupModal();
  }
});

function ensureSetupKeyField(initialValue = '') {
  const container = document.getElementById('setup-key-field');
  if (!container) return null;
  if (setupKeyField) setupKeyField.destroy();
  setupKeyField = secretField({
    container,
    label: 'n8n API Key',
    prefix: 'N8N_KEY',
    context: document.getElementById('setup-name')?.value.trim() || '',
    initialValue,
    placeholder: 'Paste your API key',
  });
  const nameEl = document.getElementById('setup-name');
  if (nameEl) {
    nameEl.addEventListener('input', () => {
      setupKeyField && setupKeyField.setContext(nameEl.value.trim());
    });
  }
  return setupKeyField;
}

document.getElementById('setup-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('setup-name').value.trim();
  const url = document.getElementById('setup-url').value.trim();
  let key = setupKeyField ? setupKeyField.getValue() : '';
  const errEl = document.getElementById('setup-error');
  const submitBtn = document.getElementById('setup-submit');

  errEl.classList.add('hidden');

  if (!name || !url || !key) {
    errEl.textContent = 'Fill in all fields';
    errEl.classList.remove('hidden');
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = 'Connecting...';

  try {
    // Promote raw key to secrets store first so instance stores a $VAR ref.
    if (!key.startsWith('$')) {
      try {
        const r = await post('/api/admin/secrets/promote', {
          value: key,
          prefix: 'N8N_KEY',
          context: name,
        });
        if (r && r.ref) {
          key = r.ref;
          invalidateRefsCache();
        }
      } catch (err) {
        // Fall back to inline storage if promote fails.
        toast.error(`Could not save to secrets store: ${err.message}. Storing inline.`);
      }
    }
    await post('/api/n8n/instances', { name, url, api_key: key });
    closeSetupModal();
    toast.success(`Connected to ${name}`);
    window.__hasInstances = true;
    await loadInstances();
    navigate('dashboard');
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Connect';
  }
});

// ── Instance selector (sidebar) ─────────────────────────────────────────────

window.__refreshInstances = loadInstances;

async function loadInstances() {
  const el = document.getElementById('instance-selector');
  if (!el) return;

  try {
    const data = await get('/api/n8n/instances');
    const instances = data.instances || [];
    window.__hasInstances = instances.length > 0;

    if (!instances.length) { el.innerHTML = ''; return; }

    el.innerHTML = `
      <div class="instance-label">Instances</div>
      ${instances.map(inst => `
        <button class="instance-item ${inst.active ? 'active' : ''}" onclick="window.__switchInstance('${jsStr(inst.id)}')">
          <span class="instance-dot" style="background:${inst.color || '#ff6d5a'}"></span>
          ${esc(inst.name)}
        </button>
      `).join('')}
      <button class="instance-add" onclick="window.__addInstance()">+ Add</button>
    `;

    const active = instances.find(i => i.active);
    // Prefer login_url — `url` can be a compose-internal hostname unreachable
    // from the browser. window.__n8nUrl is used in href/window.open, so it
    // must always be browser-reachable.
    if (active) window.__n8nUrl = (active.login_url || active.url).replace(/\/$/, '');
  } catch { el.innerHTML = ''; }
}

window.__switchInstance = async (id) => {
  try {
    await post(`/api/n8n/instances/${id}/activate`);
    await loadInstances();
    toast.success('Switched');
    navigate(currentView);
  } catch (e) { toast.error(e.message); }
};

window.__addInstance = () => {
  document.getElementById('setup-name').value = '';
  document.getElementById('setup-url').value = '';
  ensureSetupKeyField('');
  _initSetupUrlHint();
  modal.show('setup-modal');
};

// Cached docker-env result so we only fetch once per page load.
let _inDocker = null;

async function _initSetupUrlHint() {
  if (_inDocker === null) {
    try {
      const data = await get('/api/health/docker-env');
      _inDocker = data.in_docker === true;
    } catch {
      _inDocker = false;
    }
  }
  const urlInput = document.getElementById('setup-url');
  const hint = document.getElementById('setup-url-hint');
  if (!urlInput || !hint) return;
  function _toggleHint() {
    const val = urlInput.value;
    const isLocal = val.includes('localhost') || val.includes('127.0.0.1');
    hint.style.display = (_inDocker && isLocal) ? 'block' : 'none';
  }
  // Remove any prior listener to avoid stacking on repeated modal opens.
  if (urlInput.__hintHandler) urlInput.removeEventListener('input', urlInput.__hintHandler);
  urlInput.__hintHandler = _toggleHint;
  urlInput.addEventListener('input', _toggleHint);
  _toggleHint();
}

function esc(s) { const d = document.createElement('span'); d.textContent = s || ''; return d.innerHTML; }


function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

// ── Theme dropdown ──────────────────────────────────────────────────────────

async function loadThemeDropdown() {
  const select = document.getElementById('theme-select');
  if (!select) return;
  try {
    const data = await get('/api/themes');
    const themes = data.themes || [];
    const active = getCurrentTheme();
    select.innerHTML = themes.map(t =>
      `<option value="${t.id}" ${t.id === active ? 'selected' : ''}>${esc(t.name)}</option>`
    ).join('');
    select.onchange = async () => {
      try { await setActiveTheme(select.value); } catch { /* ignore */ }
    };
  } catch { select.innerHTML = '<option>Default</option>'; }
}

// ── Init ────────────────────────────────────────────────────────────────────

window.__n8nUrl = '';

async function init() {
  try {
    // Auth gate first: blocks here (rendering setup/login) until the browser
    // holds a session, unless login is disabled or an edge identity is present.
    const proceed = await requireAuth();
    if (!proceed) return;

    const status = await get('/api/status');
    window.__n8nUrl = (status.n8n_url || '').replace(/\/$/, '');
    window.__appVersion = status.version || '';

    if (status.theme) await loadTheme(status.theme);
    await Promise.all([loadInstances(), loadThemeDropdown()]);

    // Init music player banner
    await player.init();
    const playerEl = document.getElementById('player-banner');
    if (playerEl) playerEl.innerHTML = player.renderBanner();

    if (!status.configured || new URLSearchParams(location.search).has('wizard')) {
      wizard.open();
    }
    // Debug hook: preview the wizard anytime from DevTools with __openWizard()
    window.__openWizard = wizard.open;

    connectWS();
    // Honor hash-based deep link (e.g. opened via right-click "Open in new tab").
    const hashView = location.hash.replace('#', '').trim();
    navigate(hashView && views[hashView] ? hashView : 'dashboard');
  } catch {
    wizard.open();
  }
}

init();
