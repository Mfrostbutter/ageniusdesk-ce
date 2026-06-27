/**
 * Community module loader — renders sidebar nav entries and loads community
 * views inside a SANDBOXED IFRAME.
 *
 * Isolation model (v0.3): a community module's frontend runs in an
 * <iframe sandbox="allow-scripts ..."> WITHOUT allow-same-origin. That opaque
 * origin is the boundary: the module's script cannot read or change the host
 * page's DOM, window, cookies, or storage, so a buggy or hostile module can no
 * longer break the AgeniusDesk UI. Because the frame is opaque-origin, its own
 * fetch() cannot carry the session cookie, so all host interaction goes through
 * a postMessage RPC bridge to a whitelisted host API (fetch a same-origin /api/
 * path with auth + CSRF, notify, navigate, openInHarness).
 *
 * The bridge reimplements window.AgeniusDesk inside the iframe over postMessage,
 * so module scripts that already use AgeniusDesk.fetch/notify/navigate/
 * openInHarness keep working unchanged. The host also pushes the active theme's
 * CSS variables into the iframe and auto-resizes it to its content height.
 *
 * Built-in module nav lives in index.html (hardcoded). Community modules are
 * fetched from /api/modules/nav on boot and appended to a dedicated sidebar
 * group. Their assets are served from /modules/{id}/static/ (see static_router).
 */

import { get } from './api.js';
import * as toast from './components/toast.js';

// ── Bridge registry + dispatcher (host side) ─────────────────────────────────

// channel token → { iframe, moduleId }. One live entry per mounted view.
const _bridges = new Map();

/** Collect the active theme's CSS custom properties (the inline overrides that
 *  themes.js applyTheme() sets on <html>) so the iframe can mirror them on top
 *  of its own copy of base.css. Captures custom themes without a hardcoded list. */
function collectThemeVars() {
  const out = {};
  const s = document.documentElement.style;
  for (let i = 0; i < s.length; i++) {
    const prop = s[i];
    if (prop && prop.startsWith('--')) out[prop] = s.getPropertyValue(prop);
  }
  return out;
}

function postTheme(iframe, channel) {
  try {
    iframe.contentWindow?.postMessage({ __agd: 1, ch: channel, type: 'theme', vars: collectThemeVars() }, '*');
  } catch { /* iframe gone */ }
}

/** Perform a host-side fetch on behalf of a module. Restricted to same-origin
 *  /api/ paths; the global fetch shim (app.js) attaches the CSRF token, and the
 *  session cookie rides along because this runs in the host (real) origin. */
async function hostFetch(args) {
  const path = args && args.path;
  if (typeof path !== 'string' || !path.startsWith('/api/') || path.startsWith('//') || path.includes('://')) {
    throw new Error('blocked: community modules may only call same-origin /api/ paths');
  }
  const opts = args.opts || {};
  const init = {};
  if (typeof opts.method === 'string') init.method = opts.method;
  if (opts.headers && typeof opts.headers === 'object') init.headers = opts.headers;
  if (typeof opts.body === 'string') init.body = opts.body;
  const r = await window.fetch(path, init);
  const body = await r.text();
  return {
    ok: r.ok,
    status: r.status,
    statusText: r.statusText || '',
    headers: { 'content-type': r.headers.get('content-type') || '' },
    body,
  };
}

/** The whitelist. Any method not here is rejected. */
async function dispatch(method, args) {
  if (method === 'fetch') return hostFetch(args);
  if (method === 'notify') {
    const level = ['info', 'success', 'warning', 'error'].includes(args?.level) ? args.level : 'info';
    (toast[level] || toast.info)(String(args?.message ?? ''));
    return true;
  }
  if (method === 'navigate') {
    if (window.__nav && typeof args?.view === 'string') window.__nav(args.view);
    return true;
  }
  if (method === 'openInHarness') {
    if (window.__harnessOpenPath && typeof args?.path === 'string') window.__harnessOpenPath(args.path);
    return true;
  }
  throw new Error(`unknown bridge method: ${method}`);
}

let _dispatcherInstalled = false;
function installBridgeDispatcher() {
  if (_dispatcherInstalled) return;
  _dispatcherInstalled = true;

  window.addEventListener('message', async (e) => {
    const d = e.data;
    if (!d || d.__agd !== 1 || !d.ch) return;
    const entry = _bridges.get(d.ch);
    if (!entry) return;
    // Authoritative origin check: the message must come from THIS iframe's window.
    if (e.source !== entry.iframe.contentWindow) return;

    if (d.type === 'ready') { postTheme(entry.iframe, d.ch); return; }
    if (d.type === 'resize') {
      const h = Math.max(400, Math.min(20000, Number(d.height) || 0));
      entry.iframe.style.height = `${h}px`;
      return;
    }
    if (d.type === 'call') {
      const reply = (ok, value, error) => {
        try {
          entry.iframe.contentWindow?.postMessage(
            { __agd: 1, ch: d.ch, id: d.id, type: 'result', ok, value, error }, '*');
        } catch { /* iframe gone */ }
      };
      try { reply(true, await dispatch(d.method, d.args)); }
      catch (err) { reply(false, undefined, err?.message || String(err)); }
    }
  });

  // Re-push theme vars to every live iframe when the host theme changes.
  window.addEventListener('agd:theme-changed', () => {
    for (const [ch, entry] of _bridges) postTheme(entry.iframe, ch);
  });
}

// ── Iframe document construction ─────────────────────────────────────────────

// The bridge runs INSIDE the iframe. Classic (non-module) inline script so it
// runs during head parse and defines window.AgeniusDesk before the module's
// deferred type=module script executes. No backticks / ${} here — this is
// embedded inside a template literal below; CH is substituted via JSON.
function bridgeSource(channel) {
  const CH = JSON.stringify(channel);
  return [
    '(function(){',
    '  var CH=' + CH + ';',
    '  var P=window.parent;',
    '  var seq=0, pending={};',
    '  function call(method,args){',
    '    return new Promise(function(resolve,reject){',
    '      var id=++seq; pending[id]={resolve:resolve,reject:reject};',
    '      P.postMessage({__agd:1,ch:CH,id:id,type:"call",method:method,args:args},"*");',
    '    });',
    '  }',
    '  function serializeOpts(opts){',
    '    if(!opts) return undefined;',
    '    var o={method:opts.method,headers:opts.headers,body:undefined};',
    '    if(typeof opts.body==="string") o.body=opts.body;',
    '    else if(opts.body!=null){ try{o.body=JSON.stringify(opts.body);}catch(e){} }',
    '    return o;',
    '  }',
    '  function makeResponse(r){',
    '    return {',
    '      ok:r.ok, status:r.status, statusText:r.statusText||"",',
    '      headers:{get:function(n){return (r.headers&&r.headers[String(n).toLowerCase()])||null;}},',
    '      json:function(){return Promise.resolve().then(function(){return JSON.parse(r.body||"null");});},',
    '      text:function(){return Promise.resolve(r.body||"");}',
    '    };',
    '  }',
    '  window.AgeniusDesk={',
    '    fetch:function(path,opts){return call("fetch",{path:path,opts:serializeOpts(opts)}).then(makeResponse);},',
    '    notify:function(msg,level){return call("notify",{message:String(msg),level:level||"info"});},',
    '    navigate:function(view){return call("navigate",{view:String(view)});},',
    '    openInHarness:function(p){return call("openInHarness",{path:String(p)});},',
    '    version:null',
    '  };',
    '  window.addEventListener("message",function(e){',
    '    var d=e.data; if(!d||d.__agd!==1||d.ch!==CH) return;',
    '    if(d.type==="result"){',
    '      var pr=pending[d.id]; if(!pr) return; delete pending[d.id];',
    '      if(d.ok) pr.resolve(d.value); else pr.reject(new Error(d.error||"bridge error"));',
    '    } else if(d.type==="theme" && d.vars){',
    '      var root=document.documentElement;',
    '      for(var k in d.vars){ if(Object.prototype.hasOwnProperty.call(d.vars,k)) root.style.setProperty(k,d.vars[k]); }',
    '    }',
    '  });',
    '  function reportHeight(){',
    '    var h=Math.max(document.documentElement.scrollHeight, document.body?document.body.scrollHeight:0);',
    '    P.postMessage({__agd:1,ch:CH,type:"resize",height:h},"*");',
    '  }',
    '  function start(){',
    '    try{ var ro=new ResizeObserver(reportHeight); ro.observe(document.body); }catch(e){}',
    '    reportHeight();',
    '    P.postMessage({__agd:1,ch:CH,type:"ready"},"*");',
    '  }',
    '  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",start);',
    '  else start();',
    '})();',
  ].join('\n');
}

function buildSrcdoc({ staticBase, viewHtml, channel, hasJs }) {
  const moduleScript = hasJs
    ? `<script type="module" src="${staticBase}module.js"></script>`
    : '';
  return [
    '<!doctype html>',
    '<html lang="en"><head>',
    '<meta charset="UTF-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
    '<link rel="stylesheet" href="/css/base.css">',
    '<link rel="stylesheet" href="/css/components.css">',
    // Override two base.css body rules that misbehave inside the frame:
    //  - min-height:100vh would make the body fill the iframe, so the resize
    //    observer chases its own height up to the clamp (height:auto/min-height:0).
    //  - zoom:1.07 would double-apply (the host body already zooms the iframe box),
    //    so neutralize it here (zoom:1) to match the host's net scale.
    '<style>html,body{margin:0;padding:0;height:auto;min-height:0;zoom:1;background:var(--bg-void);color:var(--text-primary);font-family:var(--font-body)}</style>',
    `<script>${bridgeSource(channel)}</script>`,
    '</head><body>',
    viewHtml,
    moduleScript,
    '</body></html>',
  ].join('\n');
}

// ── Dynamic views registry ───────────────────────────────────────────────────

const communityViews = {};  // viewName → { moduleId, staticBase, viewPath }

/**
 * View-loader shim. app.js's `views` object expects each view module to expose
 * `render(el)`. For a community module we fetch its view HTML (host-side, so
 * the session cookie applies), build a full sandboxed-iframe document around it,
 * and mount the iframe into #app-content. A fresh iframe per render means the
 * module's JS, timers, and listeners all die when the user navigates away.
 */
function makeCommunityView({ moduleId, staticBase, viewPath }) {
  return {
    async render(el) {
      installBridgeDispatcher();
      el.innerHTML = '';

      let viewHtml = '';
      let hasJs = false;
      try {
        const resp = await fetch(`${staticBase}${viewPath}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        viewHtml = await resp.text();
        // The loader probes module.js with HEAD; static_router serves HEAD too.
        hasJs = (await fetch(`${staticBase}module.js`, { method: 'HEAD' })).ok;
      } catch (e) {
        el.innerHTML = `<div class="error-banner">Failed to load module "${moduleId}": ${e.message}</div>`;
        return;
      }

      const channel = (window.crypto?.randomUUID && window.crypto.randomUUID())
        || `ch-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;

      const iframe = document.createElement('iframe');
      iframe.title = `Community module: ${moduleId}`;
      // No allow-same-origin (the isolation boundary). allow-popups* for download
      // / external links, allow-modals for prompt/confirm, allow-downloads for
      // artifact downloads.
      iframe.setAttribute('sandbox',
        'allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads');
      iframe.style.cssText = 'width:100%;border:0;display:block;min-height:600px;background:transparent';
      iframe.srcdoc = buildSrcdoc({ staticBase, viewHtml, channel, hasJs });

      // One live bridge per module: drop any stale channel before registering.
      for (const [ch, entry] of _bridges) if (entry.moduleId === moduleId) _bridges.delete(ch);
      _bridges.set(channel, { iframe, moduleId });

      el.appendChild(iframe);
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
