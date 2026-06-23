/**
 * Community module loader — renders sidebar nav entries and handles view
 * loading for modules installed via /api/modules/install.
 *
 * Built-in module nav lives in index.html (hardcoded). Community modules are
 * fetched from /api/modules/nav on boot and appended to a dedicated sidebar
 * group. Their views are HTML fetched from /modules/{id}/static/{view} and
 * injected into #app-content, with an optional module.js loaded alongside.
 *
 * The window.AgeniusDesk context object is set up here so community module
 * scripts can call back into the host app (fetch, notify, navigate) without
 * importing anything from the main bundle.
 */

import { get } from './api.js';
import * as toast from './components/toast.js';

// ── Host context exposed to community module scripts ─────────────────────────

export function installHostContext() {
  if (window.AgeniusDesk) return;
  window.AgeniusDesk = {
    // Same-origin fetch. Community scripts should use this rather than raw
    // fetch so we have a single chokepoint if we ever need to add auth.
    fetch: (path, opts) => fetch(path, opts),

    // Toast notifications — thin wrapper over our toast component.
    notify: (message, level = 'info') => {
      const fn = toast[level] || toast.info;
      fn(message);
    },

    // Programmatic navigation between views.
    navigate: (viewName) => {
      if (window.__nav) window.__nav(viewName);
    },

    // Current app version (from /api/modules response). Populated on boot.
    version: null,
  };
}

// ── Dynamic views registry ───────────────────────────────────────────────────

const communityViews = {};  // viewName → { moduleId, staticBase, viewPath }

/**
 * View-loader shim. app.js's `views` object expects each view module to
 * expose `render(el)`. For community modules we synthesize a tiny view
 * shim that fetches the HTML, injects it, and loads any declared script.
 */
function makeCommunityView({ moduleId, staticBase, viewPath }) {
  let scriptLoaded = false;
  return {
    async render(el) {
      try {
        const resp = await fetch(`${staticBase}${viewPath}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        el.innerHTML = await resp.text();

        // Load module.js once per session, if present.
        if (!scriptLoaded) {
          const scriptUrl = `${staticBase}module.js`;
          const probe = await fetch(scriptUrl, { method: 'HEAD' });
          if (probe.ok) {
            const s = document.createElement('script');
            s.type = 'module';
            s.src = scriptUrl;
            document.body.appendChild(s);
            scriptLoaded = true;
          }
        }
      } catch (e) {
        el.innerHTML = `<div class="error-banner">Failed to load module "${moduleId}": ${e.message}</div>`;
      }
    },
  };
}

// ── Sidebar rendering ────────────────────────────────────────────────────────

function renderCommunityNav(entries) {
  if (!entries.length) return;

  const nav = document.querySelector('.sidebar-nav');
  if (!nav) return;

  // Anchor before the bottom-pinned Settings group so community entries
  // sit with the other feature groups, not below Settings.
  const bottomGroup = nav.querySelector('.nav-group-bottom');

  const group = document.createElement('div');
  group.className = 'nav-group';
  group.id = 'community-modules-nav';
  group.innerHTML = '<div class="nav-group-label">Community</div>';

  for (const entry of entries) {
    const btn = document.createElement('button');
    btn.className = 'nav-btn';
    btn.dataset.view = `community:${entry.module_id}`;
    btn.title = `Community module: ${entry.module_id}`;
    btn.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="9"/><line x1="12" y1="7" x2="12" y2="17"/><line x1="7" y1="12" x2="17" y2="12"/>
      </svg>
      ${entry.label}
    `;
    btn.addEventListener('click', () => {
      if (window.__nav) window.__nav(`community:${entry.module_id}`);
      document.querySelectorAll('.nav-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === `community:${entry.module_id}`);
      });
    });
    group.appendChild(btn);
  }

  if (bottomGroup) {
    nav.insertBefore(group, bottomGroup);
  } else {
    nav.appendChild(group);
  }
}

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Load community modules and register their views.
 * Returns an object to merge into app.js's `views` registry:
 *   { "community:my-module": { render(el) } }
 */
export async function loadCommunityModules() {
  installHostContext();

  try {
    const data = await get('/api/modules/nav');
    const entries = (data.entries || []).filter(e => e.source === 'community');
    if (!entries.length) return {};

    renderCommunityNav(entries);

    const views = {};
    for (const e of entries) {
      const key = `community:${e.module_id}`;
      communityViews[key] = {
        moduleId: e.module_id,
        staticBase: e.static_base || `/modules/${e.module_id}/static/`,
        viewPath: e.view || 'index.html',
      };
      views[key] = makeCommunityView(communityViews[key]);
    }
    return views;
  } catch (e) {
    console.warn('Failed to load community modules:', e);
    return {};
  }
}
