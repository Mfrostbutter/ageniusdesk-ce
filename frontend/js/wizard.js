/**
 * First-run setup wizard. Steps: welcome, stack, secrets, n8n, ai, mirror, done.
 * Modal overlay, renders into #wizard-body.
 *
 * Shown when no n8n instances are configured. After finishing, writes:
 *   - Reusable secrets          (POST /api/admin/secrets)       [optional, each]
 *   - n8n instance              (POST /api/n8n/instances)       [required]
 *   - AI provider config        (POST /api/assistant/config)    [optional]
 *
 * Credentials (n8n API key, LLM key) are promoted to the secrets store on
 * save via POST /api/admin/secrets/promote unless the user already picked a
 * $VAR ref from the Use-existing-secret dropdown.
 */

import { get, post } from './api.js';
import * as modal from './components/modal.js';
import * as toast from './components/toast.js';
import { secretField, invalidateRefsCache } from './components/secretfield.js';
import * as connectN8nGuide from './components/connect-n8n-guide.js';
import * as errorHandlerPrompt from './components/error-handler-prompt.js';

const STEPS = [
  { id: 'welcome',  label: 'Welcome'       },
  { id: 'stack',    label: 'Stand Up Stack' },
  { id: 'secrets',  label: 'Secrets'       },
  { id: 'n8n',      label: 'Connect n8n'   },
  { id: 'ai',       label: 'AI Assistant'  },
  { id: 'done',     label: 'Done'          },
];

// The "stand-up-stack" path skips the in-wizard "Connect n8n" step: a
// freshly-deployed n8n has no owner account or API key yet, so it can't be
// connected mid-wizard. Instead, after the wizard the dashboard pops a guided
// "connect your n8n" flow (see components/connect-n8n-guide.js) that walks
// through n8n's own setup and registers the instance. Secrets and AI Assistant
// DO still apply, so the stack path continues through those:
//   Welcome -> Stand Up Stack -> Secrets -> AI Assistant -> Done.
const STACK_PATH_SKIPS = new Set(['n8n']);

// And the stack step itself only applies to the stack path.
function _stepAppliesToPath(stepId, path) {
  if (path === 'stand-up-stack') return !STACK_PATH_SKIPS.has(stepId);
  if (stepId === 'stack') return false;
  return true;
}

function _nextApplicableStep(from) {
  for (let i = from + 1; i < STEPS.length; i++) {
    if (_stepAppliesToPath(STEPS[i].id, state.data.path)) return i;
  }
  return STEPS.length - 1;
}

function _prevApplicableStep(from) {
  for (let i = from - 1; i >= 0; i--) {
    if (_stepAppliesToPath(STEPS[i].id, state.data.path)) return i;
  }
  return 0;
}

// Default rows pre-populated in the Secrets step — names only, no values.
const DEFAULT_SECRET_ROWS = [
  { name: 'ANTHROPIC_KEY',  value: '' },
  { name: 'OPEN_AI_KEY',     value: '' },
  { name: 'OPEN_ROUTER_KEY', value: '' },
];

// Holders for SecretField instances so we can read getValue() at save time.
let n8nKeyField = null;
let aiKeyField = null;

// Cache of provider -> model list, keyed by provider id. Populated lazily by
// fetchModels() on first open of each provider in the AI step.
const modelCache = {};

const state = {
  step: 0,
  data: {
    path: null,         // 'have-n8n' | 'cloud' | 'walk-through' | 'stand-up-stack'
    ai: null,           // { provider, api_key, model } or null
    n8n: null,          // { name, url, api_key, tested } or null
    secrets: [],        // [ { name, value }, ... ]
    aiTestResult: null,  // { ok, message }
    n8nTestResult: null,
    mirror: null,        // { instanceId, instanceName, items: [...], results: [...] } — populated by the Sync step
    // Stand-up-stack step state.
    stack: {
      templates: [],        // catalogue from GET /api/containers/templates
      selected: {},         // template_id -> { selected: bool, fields: {field_id: value} }
      deploying: false,
      progress: [],         // [{template_id, status: 'pending'|'running'|'ok'|'failed', message}]
      attempted: false,     // true once a deploy run has finished (even with failures)
      done: false,          // true only if every member of the queue succeeded
    },
  },
};

// ── Entry point ─────────────────────────────────────────────────────────────

export function open() {
  state.step = 0;
  state.data = {
    path: null,
    ai: null,
    n8n: null,
    secrets: DEFAULT_SECRET_ROWS.map(r => ({ ...r })),
    aiTestResult: null,
    n8nTestResult: null,
    mirror: null,
    stack: { templates: [], selected: {}, deploying: false, progress: [], attempted: false, done: false },
  };
  n8nKeyField = null;
  aiKeyField = null;
  render();
  modal.show('wizard-modal');
  wireFooter();
}

export function close() {
  modal.hide('wizard-modal');
}

// ── Footer navigation ───────────────────────────────────────────────────────

function wireFooter() {
  const back = document.getElementById('wizard-back');
  const skip = document.getElementById('wizard-skip');
  const next = document.getElementById('wizard-next');
  back.onclick = () => {
    if (state.step > 0) {
      state.step = _prevApplicableStep(state.step);
      render();
    }
  };
  skip.onclick = () => {
    const id = STEPS[state.step].id;
    if (id === 'ai') state.data.ai = null;
    if (id === 'secrets') state.data.secrets = [];
    if (id === 'n8n') state.data.n8n = null;
    if (id === 'stack') {
      // Skipping stand-up-stack means: deploy nothing, fall through to the
      // legacy n8n/AI/mirror path. Flip the user's path so subsequent steps
      // include the manual configuration UI.
      state.data.path = 'have-n8n';
    }
    state.step = _nextApplicableStep(state.step);
    render();
  };
  next.onclick = () => advance();
}

async function advance() {
  const id = STEPS[state.step].id;
  if (id === 'welcome') {
    if (!state.data.path) { toast.error('Pick an option to continue'); return; }
    state.step = _nextApplicableStep(state.step);
    return render();
  }
  if (id === 'stack') {
    // First press of Next deploys; subsequent press (after the loop finishes,
    // success OR partial failure) advances. Failed members are not auto-retried
    // by Next — the user can click "Retry failed" inline first.
    if (!state.data.stack.attempted) {
      if (state.data.stack.deploying) return;
      await deployStack();
      return;
    }
    state.step = _nextApplicableStep(state.step);
    return render();
  }
  if (id === 'secrets') {
    await saveSecrets();
    invalidateRefsCache();
    state.step = _nextApplicableStep(state.step);
    return render();
  }
  if (id === 'n8n') {
    const ok = await saveN8n();
    if (!ok) return;
    state.step = _nextApplicableStep(state.step);
    return render();
  }
  if (id === 'ai') {
    // Read the live key from the SecretField / Ollama input at save time.
    // state.data.ai.api_key is not kept in sync by the SecretField (it exposes
    // getValue() instead of writing back to state), so checking state.data.ai
    // .api_key here would always see "" and skip the save. Bug: post-wizard
    // /api/assistant/config would return api_key_display: "" even after the
    // user picked $ANTHROPIC_KEY from the dropdown.
    if (state.data.ai && readAIKey()) {
      await saveAI();
    }
    state.step = _nextApplicableStep(state.step);
    return render();
  }
  if (id === 'done') {
    try { await post('/api/admin/setup-complete', {}); } catch {}
    // The stand-up-stack path deploys n8n but can't register it in-wizard (no
    // API key until n8n's own first-run setup). Hand the deployed URL to the
    // post-dashboard guide so we can walk the user through connecting it.
    const deployedN8n = (state.data.path === 'stand-up-stack' && state.data.n8n && state.data.n8n.url)
      ? state.data.n8n.url : '';
    // Non-stack paths register n8n in-wizard; offer error reporting straight away.
    const registeredN8n = (state.data.path !== 'stand-up-stack' && state.data.n8n && state.data.n8n.url)
      ? state.data.n8n.url : '';
    close();
    if (window.__refreshInstances) window.__refreshInstances();
    if (window.__nav) window.__nav('dashboard');
    if (deployedN8n) {
      // Stack path: connect n8n first; the connect guide chains the error-handler prompt.
      setTimeout(() => connectN8nGuide.open(deployedN8n, { force: true }), 400);
    } else if (registeredN8n) {
      let host = '';
      try { host = new URL(registeredN8n).hostname; } catch { /* ignore */ }
      setTimeout(() => errorHandlerPrompt.open({ n8nHost: host, force: true }), 400);
    }
  }
}

// ── Rendering ───────────────────────────────────────────────────────────────

function render() {
  renderStepIndicator();
  renderBody();
  renderFooter();
}

function renderStepIndicator() {
  const el = document.getElementById('wizard-steps');
  // Show only the steps that apply to the chosen path, so skipped steps never
  // appear as completed and the strip stays narrow enough to fit the modal.
  const applicable = STEPS.filter(s => _stepAppliesToPath(s.id, state.data.path));
  const currentId = STEPS[state.step].id;
  const curIdx = applicable.findIndex(s => s.id === currentId);
  el.innerHTML = applicable.map((s, i) => {
    const cls = i < curIdx ? 'done' : i === curIdx ? 'active' : '';
    return `<div class="wizard-step ${cls}"><span class="wizard-step-num"><span class="wizard-step-num-text">${i + 1}</span></span><span class="wizard-step-label">${s.label}</span></div>`;
  }).join('<div class="wizard-step-sep"></div>');
}

function renderFooter() {
  const id = STEPS[state.step].id;
  const back = document.getElementById('wizard-back');
  const skip = document.getElementById('wizard-skip');
  const next = document.getElementById('wizard-next');
  back.style.visibility = state.step === 0 ? 'hidden' : 'visible';
  const skippable = new Set(['ai', 'secrets', 'n8n', 'stack']);
  skip.style.display = skippable.has(id) ? '' : 'none';
  if (id === 'stack') {
    skip.textContent = state.data.stack.deploying ? 'Skip' : 'Skip and connect existing n8n';
    next.textContent = state.data.stack.attempted ? 'Next \u2192' : 'Deploy stack';
    next.disabled = state.data.stack.deploying;
  } else {
    skip.textContent = 'Skip';
    next.disabled = false;
    next.textContent = id === 'done' ? 'Enter Dashboard' : id === 'n8n' ? 'Connect & Continue' : 'Next \u2192';
  }
}

function renderBody() {
  const el = document.getElementById('wizard-body');
  const id = STEPS[state.step].id;
  if (id === 'welcome') el.innerHTML = renderWelcome();
  else if (id === 'stack') el.innerHTML = renderStack();
  else if (id === 'secrets') el.innerHTML = renderSecrets();
  else if (id === 'n8n') el.innerHTML = renderN8n();
  else if (id === 'ai') el.innerHTML = renderAI();
  else if (id === 'done') el.innerHTML = renderDone();
  bindBody(id);
}

// ── Step: Welcome ───────────────────────────────────────────────────────────

function renderWelcome() {
  const opt = (id, title, desc, tag) => `
    <label class="wizard-card ${state.data.path === id ? 'selected' : ''}">
      <input type="radio" name="wizard-path" value="${id}" ${state.data.path === id ? 'checked' : ''}>
      <div class="wizard-card-body">
        <div class="wizard-card-title">${title}${tag ? ` <span class="pill pill-info">${tag}</span>` : ''}</div>
        <div class="wizard-card-desc">${desc}</div>
      </div>
      <div class="wizard-card-check" aria-hidden="true"></div>
    </label>`;
  return `
    <h2>Welcome to AgeniusDesk</h2>
    <p class="wizard-subtitle">
      Pick how you want to get started. Every step after this one can be skipped.
    </p>
    ${opt('stand-up-stack', 'Stand up my stack on this host', 'One-click deploy for n8n, Infisical, MongoDB, Postgres, and the rest, right into the Docker daemon running this dashboard. Best for a fresh self-host install.', 'recommended')}
    ${opt('have-n8n', 'I already have n8n running', 'Self-hosted on a VPS, Docker, bare metal, or anywhere else. You just need the URL and an API key.')}
    ${opt('walk-through', 'Walk me through self-hosting', 'I will show you short guides for Docker Compose, DigitalOcean, Hostinger, and Railway on the n8n step.')}
    ${opt('cloud', 'n8n Cloud account', 'Paid account at n8n.cloud. Not yet tested. You can still try: use your workspace URL and an API key from the n8n Cloud UI.', 'beta')}
  `;
}

// ── Step: Stand Up Stack ────────────────────────────────────────────────────

// Default shown pre-checked on the stack picker: the automation engine itself,
// the headline service for a fresh self-host install.
const STACK_DEFAULT_SELECTED = new Set(['n8n']);

// Per-template auto-mint policy for fields the wizard does not surface.
// Returns a value for a field when the wizard wants to fill it server-side
// rather than ask the user. Empty string leaves it blank.
function _stackAutoFill(templateId, fieldId) {
  if (fieldId === 'password' || fieldId === 'root_password') {
    return _genStackPassword(16);
  }
  return '';
}

function _genStackPassword(length) {
  const lower = 'abcdefghijkmnopqrstuvwxyz';
  const upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
  const digits = '23456789';
  const all = lower + upper + digits;
  const buf = new Uint32Array(length);
  crypto.getRandomValues(buf);
  const out = [upper[buf[0] % upper.length], digits[buf[1] % digits.length]];
  for (let i = 2; i < length; i++) out.push(all[buf[i] % all.length]);
  const shuffle = new Uint32Array(length);
  crypto.getRandomValues(shuffle);
  for (let i = length - 1; i > 0; i--) {
    const j = shuffle[i] % (i + 1);
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out.join('');
}

function _ensureStackHydrated() {
  // Lazy fetch on first render of the step.
  if (state.data.stack.templates.length || state.data.stack._loading) return;
  state.data.stack._loading = true;
  get('/api/containers/templates').then(res => {
    state.data.stack.templates = (res.templates || []);
    // Pre-build the selection map for every template so re-renders keep
    // user edits across check/uncheck toggles.
    for (const t of state.data.stack.templates) {
      if (state.data.stack.selected[t.id]) continue;
      const fields = {};
      for (const f of t.fields) {
        if (f.default !== undefined && f.default !== null && f.default !== '') {
          fields[f.id] = String(f.default);
        } else {
          fields[f.id] = _stackAutoFill(t.id, f.id);
        }
      }
      state.data.stack.selected[t.id] = {
        selected: STACK_DEFAULT_SELECTED.has(t.id),
        fields,
      };
    }
    state.data.stack._loading = false;
    // Re-render only if the user is still on the stack step.
    if (STEPS[state.step].id === 'stack') render();
  }).catch(err => {
    state.data.stack._loading = false;
    state.data.stack._error = err.message || 'Failed to load templates';
    if (STEPS[state.step].id === 'stack') render();
  });
}

// Lazy-fetch the host ports already published by a running container, plus the
// browser-unsafe set, so the picker can warn about a collision before deploy.
function _ensurePortsHydrated() {
  const s = state.data.stack;
  if (s._portsLoaded || s._portsLoading) return;
  s._portsLoading = true;
  get('/api/containers/ports-in-use').then(res => {
    s.portsInUse = res.in_use || {};
    s.portsUnsafe = res.unsafe || [];
    s._portsLoaded = true;
    s._portsLoading = false;
    if (STEPS[state.step].id === 'stack') _refreshPortWarnings();
  }).catch(() => { s._portsLoading = false; });
}

// The host ports a template will actually bind for the given field values.
// Covers the plain `port`, MinIO's `console_port`, and Qdrant's implicit gRPC
// port (base + 1), which are the multi-port cases in the built-in catalog.
function _requestedPortsFor(t, fields) {
  const out = [];
  const add = (label, v) => { const n = parseInt(v, 10); if (Number.isFinite(n)) out.push({ label, val: n }); };
  if (fields.port !== undefined && fields.port !== '') add('Host port', fields.port);
  if (fields.console_port !== undefined && fields.console_port !== '') add('Console port', fields.console_port);
  if (t.id === 'qdrant' && fields.port !== undefined && fields.port !== '') add('gRPC port', parseInt(fields.port, 10) + 1);
  return out;
}

// Human-readable warnings for a template's chosen ports: browser-unsafe,
// collides with another selected service in this same stack, or already bound
// by a running container on the host.
function _portWarnings(t, fields) {
  const s = state.data.stack;
  const inUse = s.portsInUse || {};
  const unsafe = new Set((s.portsUnsafe || []).map(Number));
  const others = {};  // port -> owning template name, across the OTHER selected services
  for (const [tid, sel] of Object.entries(s.selected)) {
    if (tid === t.id || !sel.selected) continue;
    const ot = s.templates.find(x => x.id === tid);
    if (!ot) continue;
    for (const p of _requestedPortsFor(ot, sel.fields)) {
      if (others[p.val] === undefined) others[p.val] = ot.name || tid;
    }
  }
  const warns = [];
  const seenSelf = new Set();
  for (const p of _requestedPortsFor(t, fields)) {
    if (unsafe.has(p.val)) { warns.push(`${p.label} ${p.val} is blocked by browsers (ERR_UNSAFE_PORT).`); continue; }
    if (seenSelf.has(p.val)) { warns.push(`${p.label} ${p.val} collides with another port on this service.`); continue; }
    seenSelf.add(p.val);
    if (others[p.val] !== undefined) { warns.push(`${p.label} ${p.val} collides with "${others[p.val]}" in this stack.`); continue; }
    const owner = inUse[String(p.val)];
    if (owner) warns.push(`${p.label} ${p.val} is already used by "${owner}". Pick a free port.`);
  }
  return warns;
}

// Repaint every card's warning line in place (no re-render, so a field the user
// is typing in keeps focus).
function _refreshPortWarnings() {
  document.querySelectorAll('.wizard-stack-portwarn').forEach(elw => {
    const tid = elw.dataset.tid;
    const t = (state.data.stack.templates || []).find(x => x.id === tid);
    const sel = state.data.stack.selected[tid];
    if (!t || !sel || !sel.selected) { elw.innerHTML = ''; return; }
    elw.innerHTML = _portWarnings(t, sel.fields).map(w =>
      `<div style="display:flex;gap:6px;align-items:flex-start;margin-top:6px;font-size:11px;color:#f59e0b"><span aria-hidden="true">⚠</span><span>${esc(w)}</span></div>`
    ).join('');
  });
}

function renderStack() {
  _ensureStackHydrated();
  _ensurePortsHydrated();
  const s = state.data.stack;

  if (s._error) {
    return `<h2>Stand up your stack</h2><p class="wizard-subtitle" style="color:var(--error)">${esc(s._error)}</p>`;
  }
  if (s._loading || !s.templates.length) {
    return `<h2>Stand up your stack</h2><p class="wizard-subtitle">Loading template catalog…</p>`;
  }

  if (s.deploying || s.attempted) {
    // Progress / result UI. Shown during the loop and after, regardless of
    // whether every member succeeded. A partial failure must not bounce the
    // user back to the picker (would obscure what just happened and re-arm
    // the Deploy stack button, looking like a UI loop).
    const rows = s.progress.map(p => {
      const icon = p.status === 'ok' ? '<span style="color:#34d399">✓</span>'
        : p.status === 'failed' ? '<span style="color:#ff6d5a">✕</span>'
        : p.status === 'running' ? '<span style="color:#60a5fa">⏳</span>'
        : '<span style="color:var(--text-dim)">○</span>';
      const msgColor = p.status === 'failed' ? '#ff6d5a' : 'var(--text-dim)';
      return `
        <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:var(--bg-input);border-radius:var(--radius);margin-bottom:6px">
          <span style="width:14px;text-align:center">${icon}</span>
          <code style="font-size:12px;flex:1">${esc(p.template_name || p.template_id)}</code>
          <span style="font-size:11px;color:${msgColor}">${esc(p.message || '')}</span>
        </div>
      `;
    }).join('');
    const okItems = s.progress.filter(p => p.status === 'ok' && p.url);
    const failedItems = s.progress.filter(p => p.status === 'failed');
    const summaryRows = okItems.map(p => `
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
        <span style="color:#34d399;flex-shrink:0">→</span>
        <code>${esc(p.template_name || p.template_id)}</code>
        <a href="${esc(p.url)}" target="_blank" style="color:var(--accent);font-size:11px">${esc(p.url)}</a>
      </div>
    `).join('');

    const banner = !s.deploying && failedItems.length ? `
      <div style="margin-top:12px;padding:12px;border:1px solid #ff6d5a;background:rgba(255,109,90,0.08);border-radius:var(--radius);font-size:13px">
        <div style="font-weight:600;color:#ff6d5a;margin-bottom:4px">${failedItems.length} service${failedItems.length === 1 ? '' : 's'} did not deploy</div>
        <div style="color:var(--text-secondary);font-size:12px;line-height:1.5;margin-bottom:8px">
          Common cause: the host port is already in use. You can retry just the failed ones after stopping whatever owns those ports, or continue to the dashboard and finish those deploys from the Containers view.
        </div>
        <button class="btn btn-sm" id="wizard-stack-retry" type="button">Retry failed</button>
      </div>
    ` : '';

    const header = s.deploying ? 'Standing up your stack'
      : failedItems.length ? `Stack mostly deployed (${okItems.length}/${s.progress.length})`
      : 'Stack deployed';
    const subtitle = s.deploying
      ? 'This may take a few minutes on first run while images pull.'
      : failedItems.length
      ? `${okItems.length} service${okItems.length === 1 ? '' : 's'} ready below. Open each one to finish its first-run setup.`
      : 'Deploy complete. Open each service below to finish its one-time setup.';

    return `
      <h2>${esc(header)}</h2>
      <p class="wizard-subtitle">${esc(subtitle)}</p>
      <div style="margin-top:12px">${rows}</div>
      ${banner}
      ${!s.deploying && okItems.length ? `<div style="margin-top:16px;padding:12px;border:1px solid var(--border-dim);border-radius:var(--radius)"><div style="font-size:11px;color:var(--text-dim);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Open in browser</div>${summaryRows}</div>` : ''}
    `;
  }

  // Picker UI.
  const cardFor = (t) => {
    const sel = s.selected[t.id] || { selected: false, fields: {} };
    const checked = sel.selected ? 'checked' : '';
    // Show instance_name + port first (name it, choose the port), then the rest.
    // Auto-minted secrets stay hidden. Ordering makes the two most-edited fields
    // land at the top of the expanded card.
    const visible = (t.fields || []).filter(f => !(t.auto_secrets || []).includes(f.id));
    const userFields = [
      ...visible.filter(f => f.id === 'instance_name'),
      ...visible.filter(f => f.id === 'port'),
      ...visible.filter(f => f.id !== 'instance_name' && f.id !== 'port'),
    ];
    return `
      <label class="wizard-stack-card ${sel.selected ? 'selected' : ''}" data-tid="${esc(t.id)}" style="display:flex;gap:12px;padding:12px;border:1px solid var(--border-dim);border-radius:var(--radius);margin-bottom:8px;cursor:pointer;background:${sel.selected ? 'rgba(96,165,250,0.05)' : 'var(--bg-input)'};transition:background 0.1s">
        <input type="checkbox" class="wizard-stack-check" data-tid="${esc(t.id)}" ${checked} style="margin-top:2px;flex-shrink:0">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px;font-weight:600;font-size:14px">
            <span style="font-size:18px">${esc(t.icon)}</span>
            <span>${esc(t.name)}</span>
            ${t.bundle ? '<span class="pill" style="background:rgba(96,165,250,0.15);color:#60a5fa;font-size:9px;padding:1px 6px;border-radius:8px">bundle</span>' : ''}
            ${t.community ? '<span class="pill" style="background:rgba(255,255,255,0.05);color:var(--text-dim);font-size:9px;padding:1px 6px;border-radius:8px">community</span>' : ''}
          </div>
          <div style="font-size:12px;color:var(--text-secondary);margin-top:3px;line-height:1.5">${esc(t.description)}</div>
          ${sel.selected && userFields.length ? `
            <div class="wizard-stack-fields" style="margin-top:10px;display:grid;grid-template-columns:120px 1fr;gap:6px 10px;font-size:12px">
              ${userFields.map(f => {
                const val = sel.fields[f.id] != null ? sel.fields[f.id] : '';
                const type = f.type === 'password' ? 'text' : (f.type === 'number' ? 'number' : 'text');
                return `
                  <label style="color:var(--text-dim);align-self:center">${esc(f.label)}</label>
                  <input type="${type}" class="wizard-stack-field" data-tid="${esc(t.id)}" data-fid="${esc(f.id)}" value="${esc(String(val))}" placeholder="${esc(f.placeholder || '')}" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:5px 8px;color:var(--text-primary);font-family:var(--font-mono);font-size:12px">
                `;
              }).join('')}
            </div>
            <div class="wizard-stack-portwarn" data-tid="${esc(t.id)}"></div>
          ` : ''}
        </div>
      </label>
    `;
  };

  const selectedCount = Object.values(s.selected).filter(v => v.selected).length;

  return `
    <h2>Stand up your stack</h2>
    <p class="wizard-subtitle">
      Pick the services you want this dashboard to manage. Each one deploys into the local Docker daemon. n8n and Infisical are pre-selected because they cover the headline use case: automation plus secure credential storage. You can change anything later from the Containers view.
    </p>
    <div id="wizard-stack-grid" style="margin-top:12px">
      ${s.templates.map(cardFor).join('')}
    </div>
    <div style="margin-top:14px;display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--text-dim)">
      <div>${selectedCount} selected. Passwords are auto-generated and shown after deploy.</div>
    </div>
  `;
}

async function deployStack() {
  const s = state.data.stack;
  if (s.deploying) return;
  const queue = Object.entries(s.selected)
    .filter(([, v]) => v.selected)
    .map(([tid, v]) => ({
      template_id: tid,
      template_name: (s.templates.find(t => t.id === tid) || {}).name || tid,
      fields: v.fields,
    }));
  if (!queue.length) {
    toast.error('Pick at least one service to deploy');
    return;
  }
  s.deploying = true;
  s.attempted = false;
  s.done = false;
  s.progress = queue.map(q => ({
    template_id: q.template_id,
    template_name: q.template_name,
    status: 'pending',
    message: '',
    url: '',
  }));
  render();

  for (let i = 0; i < queue.length; i++) {
    const item = queue[i];
    s.progress[i].status = 'running';
    s.progress[i].message = 'Pulling image…';
    render();
    try {
      const res = await post('/api/containers/deploy', {
        template_id: item.template_id,
        fields: item.fields,
      });
      await _streamStackDeploy(res.deploy_id, s.progress[i]);
    } catch (err) {
      s.progress[i].status = 'failed';
      s.progress[i].message = err.message || 'deploy failed';
      render();
      // Continue with the remaining items rather than halt. n8n failing because
      // its port is taken should not prevent Infisical from coming up. Failed
      // items can be retried via the inline Retry failed button.
    }
  }

  s.deploying = false;
  s.attempted = true;
  s.done = s.progress.every(p => p.status === 'ok' || p.status === 'skipped');
  // If the deploy queue included n8n, capture its URL into n8n state so the
  // post-stack flow can offer auto-registration once the user completes the
  // owner setup. Tracked as task #16.
  const n8n = s.progress.find(p => p.template_id === 'n8n' && p.status === 'ok');
  if (n8n && n8n.url) {
    state.data.n8n = { name: 'n8n', url: n8n.url, api_key: '', tested: false };
  }
  render();
}

async function retryFailedStack() {
  const s = state.data.stack;
  if (s.deploying) return;
  const failedIdx = s.progress
    .map((p, i) => p.status === 'failed' ? i : -1)
    .filter(i => i >= 0);
  if (!failedIdx.length) return;

  s.deploying = true;
  for (const i of failedIdx) {
    s.progress[i].status = 'running';
    s.progress[i].message = 'Pulling image…';
    s.progress[i].url = '';
  }
  render();

  for (const i of failedIdx) {
    const slot = s.progress[i];
    const sel = s.selected[slot.template_id];
    if (!sel) continue;
    try {
      const res = await post('/api/containers/deploy', {
        template_id: slot.template_id,
        fields: sel.fields,
      });
      await _streamStackDeploy(res.deploy_id, slot);
    } catch (err) {
      slot.status = 'failed';
      slot.message = err.message || 'deploy failed';
      render();
    }
  }

  s.deploying = false;
  s.done = s.progress.every(p => p.status === 'ok' || p.status === 'skipped');
  const n8n = s.progress.find(p => p.template_id === 'n8n' && p.status === 'ok');
  if (n8n && n8n.url) {
    state.data.n8n = { name: 'n8n', url: n8n.url, api_key: '', tested: false };
  }
  render();
}

function _streamStackDeploy(deployId, slot) {
  return new Promise((resolve, reject) => {
    const src = new EventSource(`/api/containers/deploy/${deployId}/progress`);
    let lastMessage = '';
    src.onmessage = (e) => {
      let item;
      try { item = JSON.parse(e.data); } catch { return; }
      if (item === null) { src.close(); return; }
      if (item.event === 'step') {
        lastMessage = item.message;
        slot.message = item.message;
        render();
        return;
      }
      if (item.event === 'bundle_step') {
        slot.message = `Container ${item.current}/${item.total}: ${item.container_name}`;
        render();
        return;
      }
      if (item.event === 'done') {
        slot.status = 'ok';
        slot.message = item.bundle ? `Bundle ready (${(item.containers || []).length} containers)` : 'Container started';
        slot.url = item.primary_url || item.url || '';
        render();
        src.close();
        resolve();
        return;
      }
      if (item.event === 'error') {
        slot.status = 'failed';
        slot.message = item.message || lastMessage || 'deploy failed';
        render();
        src.close();
        reject(new Error(slot.message));
      }
    };
    src.onerror = () => {
      slot.status = 'failed';
      slot.message = slot.message || 'lost progress stream';
      render();
      src.close();
      reject(new Error(slot.message));
    };
  });
}

// ── Step: Secrets (moved to position 3) ─────────────────────────────────────

function renderSecrets() {
  const rows = state.data.secrets.length ? state.data.secrets : [{ name: '', value: '' }];
  return `
    <h2>Store your API keys securely <span style="color:var(--text-dim);font-size:14px;font-weight:400;margin-left:6px">optional</span></h2>
    <p class="wizard-subtitle">
      Keep credentials in one encrypted place. Reference them anywhere with <code>$NAME</code>. Fill in what you have now; leave the rest blank. You can always add more later.
    </p>
    <div id="wizard-secrets-rows">
      ${rows.map((r, i) => `
        <div class="wizard-secret-row" data-i="${i}" style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
          <input type="text" class="wizard-secret-name" placeholder="NAME (e.g. ANTHROPIC_KEY)" value="${esc(r.name)}" style="flex:1;text-transform:uppercase;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:8px 10px;color:var(--text-primary);font-family:var(--font-mono);font-size:13px">
          <input type="password" class="wizard-secret-value" placeholder="value (paste your key)" value="${esc(r.value)}" style="flex:2;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:8px 10px;color:var(--text-primary);font-family:var(--font-mono);font-size:13px">
          <button class="btn btn-sm btn-ghost wizard-secret-remove" type="button" title="Remove">&times;</button>
        </div>
      `).join('')}
    </div>
    <button class="btn btn-sm" id="wizard-secret-add" type="button" style="margin-top:8px">+ Add another</button>
    <p style="margin-top:16px;font-size:12px;color:var(--text-dim)">
      Saved with authenticated Fernet encryption. Reference with <code>$NAME</code> in instance and assistant settings.
    </p>
  `;
}

// ── Step: Connect n8n ───────────────────────────────────────────────────────

const DOCKER_PLATFORMS = [
  {
    id: 'macos',
    name: 'macOS',
    blurb: 'Docker Desktop handles the daemon + CLI. Works on Intel and Apple Silicon.',
    steps: [
      'Install <a href="https://www.docker.com/products/docker-desktop/" target="_blank" rel="noopener">Docker Desktop for Mac</a> and open it once so the whale icon appears in the menu bar.',
      'In Terminal: <code>mkdir n8n &amp;&amp; cd n8n</code>',
      'Create <code>docker-compose.yml</code> with the content from the official guide below.',
      'Run <code>docker compose up -d</code>.',
      'Open <code>http://localhost:5678</code>, create an admin account, go to Settings &rarr; API, generate an API key.',
      'Paste <code>http://localhost:5678</code> and the API key below.',
    ],
  },
  {
    id: 'windows',
    name: 'Windows',
    blurb: 'Docker Desktop installs WSL 2 automatically if it is not already enabled.',
    steps: [
      'Install <a href="https://www.docker.com/products/docker-desktop/" target="_blank" rel="noopener">Docker Desktop for Windows</a>. On first run it will ask to enable WSL 2 — accept.',
      'Open PowerShell (or Windows Terminal): <code>mkdir n8n; cd n8n</code>',
      'Create <code>docker-compose.yml</code> with the content from the official guide below (Notepad or VS Code works).',
      'Run <code>docker compose up -d</code>.',
      'Open <code>http://localhost:5678</code>, create an admin account, go to Settings &rarr; API, generate an API key.',
      'Paste <code>http://localhost:5678</code> and the API key below.',
    ],
  },
  {
    id: 'linux',
    name: 'Linux / VPS',
    blurb: 'Any Linux box with Docker + Docker Compose installed. Typical for a VPS or home server.',
    steps: [
      'Install Docker Engine + Compose plugin (<a href="https://docs.docker.com/engine/install/" target="_blank" rel="noopener">official install</a>).',
      'On your server: <code>mkdir n8n &amp;&amp; cd n8n</code>',
      'Create <code>docker-compose.yml</code> with the content from the official guide below.',
      'Run <code>docker compose up -d</code>.',
      'Open <code>http://&lt;your-server-ip&gt;:5678</code>, create an admin account, go to Settings &rarr; API, generate an API key.',
      'Paste the URL and API key below.',
    ],
  },
];

const PLATFORM_GUIDES = [
  {
    id: 'docker',
    name: 'Docker Compose',
    difficulty: 'easy',
    blurb: 'Recommended for most self-hosters. Pick your OS below for the right install steps.',
    platforms: DOCKER_PLATFORMS,
    link: { label: 'Official Docker guide', url: 'https://docs.n8n.io/hosting/installation/docker/' },
  },
  {
    id: 'digitalocean',
    name: 'DigitalOcean',
    difficulty: 'easy',
    blurb: 'One-click marketplace droplet. Ships with Docker + n8n pre-configured.',
    steps: [
      'Create a DigitalOcean account (free credit often available).',
      'Marketplace &rarr; search "n8n" &rarr; Create Droplet (cheapest tier fine to start).',
      'Follow the post-install wizard to set a domain and admin email.',
      'In n8n: Settings &rarr; API &rarr; Create API Key.',
      'Paste the URL and API key below.',
    ],
    link: { label: 'DigitalOcean n8n marketplace listing', url: 'https://marketplace.digitalocean.com/apps/n8n' },
  },
  {
    id: 'hostinger',
    name: 'Hostinger',
    difficulty: 'easy',
    blurb: 'VPS template with n8n pre-installed. Low monthly cost, beginner-friendly dashboard.',
    steps: [
      'Buy any Hostinger VPS plan (KVM 1 or higher).',
      'During VPS setup, pick the "n8n" template from the OS/application list.',
      'Hostinger provisions it and gives you the URL + first admin credentials.',
      'In n8n: Settings &rarr; API &rarr; Create API Key.',
      'Paste the URL and API key below.',
    ],
    link: { label: 'Hostinger n8n VPS setup', url: 'https://www.hostinger.com/tutorials/how-to-install-n8n' },
  },
  {
    id: 'railway',
    name: 'Railway',
    difficulty: 'easy',
    blurb: 'Deploy from a template in one click. Great if you do not want to manage a server.',
    steps: [
      'Sign up at railway.app (GitHub OAuth works).',
      'New Project &rarr; Deploy a Template &rarr; search "n8n" &rarr; pick the official template.',
      'Wait 1-2 minutes for the build. Railway assigns you a public URL.',
      'Open the URL, create an admin account, go to Settings &rarr; API, generate an API key.',
      'Paste the URL and API key below.',
    ],
    link: { label: 'Railway n8n template', url: 'https://railway.app/template/n8n' },
  },
];

function _detectDefaultDockerPlatform() {
  const ua = (navigator.userAgent || '').toLowerCase();
  if (ua.includes('mac')) return 'macos';
  if (ua.includes('win')) return 'windows';
  return 'linux';
}

function renderN8n() {
  const n = state.data.n8n || {};
  const showGuides = state.data.path === 'walk-through';
  const activeGuide = n.guideTab || 'docker';
  const guide = PLATFORM_GUIDES.find(g => g.id === activeGuide);
  // Pre-select a sensible OS sub-tab for docker based on the user's own OS.
  const activeDockerOs = n.dockerOs || _detectDefaultDockerPlatform();
  const activePlatform = guide.platforms
    ? (guide.platforms.find(p => p.id === activeDockerOs) || guide.platforms[0])
    : null;
  const displayBlurb = activePlatform ? activePlatform.blurb : guide.blurb;
  const displaySteps = activePlatform ? activePlatform.steps : guide.steps;
  return `
    <h2>Connect your n8n instance</h2>
    <p class="wizard-subtitle">
      ${showGuides
        ? 'Pick a platform on the left to follow a quick-start guide. Once n8n is running somewhere, fill out the form on the right.'
        : 'Paste the URL you use to open n8n in your browser, plus an API key.'}
    </p>
    <div style="${showGuides ? 'display:grid;grid-template-columns:280px 1fr;gap:20px' : ''}">
      ${showGuides ? `
        <div>
          <div class="wizard-platform-tabs">
            ${PLATFORM_GUIDES.map(g => `
              <button class="wizard-platform-tab ${g.id === activeGuide ? 'active' : ''}" data-guide="${g.id}" type="button">
                <span>${g.name}</span>
                <span class="pill pill-neutral" style="font-size:10px">${g.difficulty}</span>
              </button>
            `).join('')}
          </div>
          <div class="wizard-platform-body" style="margin-top:12px">
            ${guide.platforms ? `
              <div class="wizard-docker-os-tabs" style="display:flex;gap:6px;margin-bottom:10px">
                ${guide.platforms.map(p => `
                  <button type="button" class="wizard-docker-os-tab ${p.id === activePlatform.id ? 'active' : ''}" data-os="${p.id}"
                          style="font-size:11px;padding:4px 10px;border-radius:var(--radius);border:1px solid var(--border-dim);background:${p.id === activePlatform.id ? 'var(--accent-bg, var(--bg-input))' : 'transparent'};color:var(--text-primary);cursor:pointer">
                    ${p.name}
                  </button>
                `).join('')}
              </div>
            ` : ''}
            <p style="font-size:13px;color:var(--text-secondary);margin-bottom:10px">${displayBlurb}</p>
            <ol style="font-size:13px;padding-left:18px;line-height:1.6;color:var(--text-primary)">
              ${displaySteps.map(s => `<li style="margin-bottom:4px">${s}</li>`).join('')}
            </ol>
            <p style="margin-top:10px;font-size:12px">
              <a href="${guide.link.url}" target="_blank" rel="noopener">${guide.link.label} &rarr;</a>
            </p>
          </div>
        </div>
      ` : ''}
      <div>
        <label>
          Instance Name
          <input type="text" id="wizard-n8n-name" placeholder="e.g. My n8n, Production, Dev" value="${esc(n.name || '')}">
        </label>
        <label>
          n8n URL
          <input type="url" id="wizard-n8n-url" placeholder="https://your-n8n.example.com" value="${esc(n.url || '')}">
        </label>
        <div style="margin-top:10px">
          <div id="wizard-n8n-key-field"></div>
          <small style="display:block;margin-top:4px;color:var(--text-dim)"><a href="https://docs.n8n.io/api/authentication/" target="_blank" rel="noopener">How to create an n8n API key &rarr;</a></small>
        </div>
        <div id="wizard-n8n-test-result" style="margin-top:10px;font-size:12px"></div>
        <button class="btn btn-sm" id="wizard-n8n-test" type="button" style="margin-top:4px">Test connection</button>
      </div>
    </div>
  `;
}

// ── Step: AI Assistant (now at position 4) ──────────────────────────────────

const AI_PROVIDERS = [
  {
    id: 'openrouter',
    name: 'OpenRouter',
    tag: 'recommended',
    blurb: 'One key, 11+ models (Claude, GPT-4o, Gemini, Llama). Easiest to get started.',
    keyUrl: 'https://openrouter.ai/keys',
    keyLabel: 'openrouter.ai/keys',
    placeholder: 'sk-or-...',
  },
  {
    id: 'openai',
    name: 'OpenAI',
    blurb: 'Direct API access to GPT-4o, GPT-4.1, and o-series reasoning models.',
    keyUrl: 'https://platform.openai.com/api-keys',
    keyLabel: 'platform.openai.com/api-keys',
    placeholder: 'sk-proj-...',
  },
  {
    id: 'anthropic',
    name: 'Anthropic',
    blurb: 'Direct API access to the Claude model family.',
    keyUrl: 'https://console.anthropic.com/settings/keys',
    keyLabel: 'console.anthropic.com/settings/keys',
    placeholder: 'sk-ant-...',
  },
  {
    id: 'ollama',
    name: 'Ollama',
    blurb: 'Run open-source models locally. No API key needed, just a URL.',
    keyUrl: 'https://ollama.com/download',
    keyLabel: 'ollama.com/download',
    placeholder: 'http://localhost:11434',
  },
];

function renderAI() {
  const ai = state.data.ai || {};
  const cur = AI_PROVIDERS.find(p => p.id === ai.provider);
  const isOllama = cur && cur.id === 'ollama';
  return `
    <h2>AI Assistant <span style="color:var(--text-dim);font-size:14px;font-weight:400;margin-left:6px">optional</span></h2>
    <p class="wizard-subtitle">
      Unlocks in-dashboard chat, error diagnosis, and AI-assisted Code Lab. Pick a provider below or skip for now. You can change this anytime in Settings.
    </p>
    <div class="wizard-provider-grid">
      ${AI_PROVIDERS.map(p => `
        <label class="wizard-card ${ai.provider === p.id ? 'selected' : ''}">
          <input type="radio" name="wizard-ai" value="${p.id}" ${ai.provider === p.id ? 'checked' : ''}>
          <div class="wizard-card-body">
            <div class="wizard-card-title">${p.name}${p.tag ? ` <span class="pill pill-success">${p.tag}</span>` : ''}</div>
            <div class="wizard-card-desc">${p.blurb}</div>
          </div>
          <div class="wizard-card-check" aria-hidden="true"></div>
        </label>
      `).join('')}
    </div>
    ${cur ? `
      <div class="wizard-ai-config" style="display:flex;flex-direction:column;gap:10px;margin-top:14px">
        ${isOllama ? `
          <label>
            Ollama URL
            <input type="url" id="wizard-ai-key" placeholder="${cur.placeholder}" value="${esc(ai.api_key || '')}">
            <small>Don't have one? <a href="${cur.keyUrl}" target="_blank" rel="noopener">${cur.keyLabel} &rarr;</a></small>
          </label>
        ` : `
          <div>
            <div id="wizard-ai-key-field"></div>
            <small style="display:block;margin-top:4px;color:var(--text-dim)">Don't have one? <a href="${cur.keyUrl}" target="_blank" rel="noopener">${cur.keyLabel} &rarr;</a></small>
          </div>
        `}
        <label>
          Model <span style="font-size:11px;color:var(--text-dim);font-weight:400;margin-left:4px">optional, auto-picks a default if blank</span>
          <select id="wizard-ai-model"><option value="">Loading&hellip;</option></select>
          <input type="text" id="wizard-ai-model-custom" placeholder="${modelHint(cur.id)}" style="display:none;margin-top:6px" value="${esc(ai.model || '')}">
        </label>
        <div id="wizard-ai-test-result" style="font-size:12px"></div>
        <button class="btn btn-sm" id="wizard-ai-test" type="button" style="align-self:flex-start">Test connection</button>
      </div>
    ` : ''}
  `;
}

function modelHint(provider) {
  return {
    openrouter: 'anthropic/claude-sonnet-4',
    openai: 'gpt-4o',
    anthropic: 'claude-sonnet-4-20250514',
    ollama: 'llama3.1',
  }[provider] || '';
}

// ── Step: Done ──────────────────────────────────────────────────────────────

function renderDone() {
  const ai = state.data.ai;
  const n8n = state.data.n8n;
  const secrets = state.data.secrets.filter(s => s.name && s.value);
  const stackDeployed = state.data.path === 'stand-up-stack' && n8n && n8n.url;
  const n8nLine = stackDeployed
    ? `running at ${esc(n8n.url)} <span style="color:var(--text-dim)">— we'll connect it next</span>`
    : (n8n ? `${esc(n8n.name)} (${esc(n8n.url)})` : '<span style="color:var(--text-dim)">not configured yet</span>');
  const nextUp = stackDeployed
    ? `Next up: we'll walk you through connecting your new n8n — open it, create your account, make an API key, and register it here.`
    : `Next up: the Workflows tab lists everything on your n8n instance. Errors tab shows recent failures. Code Lab is where you can write and test n8n Code nodes with AI help.`;
  return `
    <h2>You're all set</h2>
    <p class="wizard-subtitle">
      Here's what we configured. You can change any of this from Settings later.
    </p>
    <ul class="wizard-summary">
      <li><strong>Secrets:</strong> ${secrets.length ? secrets.map(s => '$' + esc(s.name)).join(', ') : '<span style="color:var(--text-dim)">none</span>'}</li>
      <li><strong>n8n:</strong> ${n8nLine}</li>
      <li><strong>AI Assistant:</strong> ${ai ? `${ai.provider}${ai.model ? ` / ${esc(ai.model)}` : ''}` : '<span style="color:var(--text-dim)">not configured (skipped)</span>'}</li>
    </ul>
    <p style="margin-top:20px;font-size:13px;color:var(--text-secondary)">
      ${nextUp}
    </p>
  `;
}

// ── Step body bindings ──────────────────────────────────────────────────────

function bindBody(id) {
  if (id === 'welcome') {
    document.querySelectorAll('input[name="wizard-path"]').forEach(el => {
      el.addEventListener('change', () => { state.data.path = el.value; render(); });
    });
    return;
  }
  if (id === 'stack') {
    document.querySelectorAll('.wizard-stack-check').forEach(cb => {
      cb.addEventListener('change', () => {
        const tid = cb.dataset.tid;
        if (!state.data.stack.selected[tid]) state.data.stack.selected[tid] = { selected: false, fields: {} };
        state.data.stack.selected[tid].selected = cb.checked;
        render();
      });
    });
    document.querySelectorAll('.wizard-stack-field').forEach(input => {
      input.addEventListener('input', () => {
        const tid = input.dataset.tid;
        const fid = input.dataset.fid;
        if (!state.data.stack.selected[tid]) state.data.stack.selected[tid] = { selected: false, fields: {} };
        state.data.stack.selected[tid].fields[fid] = input.value;
        // Port edits change the conflict picture for every card (an intra-stack
        // collision is symmetric), so repaint all warnings, not just this one.
        if (fid === 'port' || fid === 'console_port') _refreshPortWarnings();
      });
    });
    // Clicking the card body (outside the checkbox) toggles the checkbox.
    // Without this, only the tiny checkbox is interactive — the rest of the
    // card looks clickable but does nothing.
    document.querySelectorAll('.wizard-stack-card').forEach(card => {
      card.addEventListener('click', (e) => {
        if (e.target.matches('input,label,a,button')) return;
        const cb = card.querySelector('.wizard-stack-check');
        if (!cb) return;
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      });
    });
    const retryBtn = document.getElementById('wizard-stack-retry');
    if (retryBtn) {
      retryBtn.addEventListener('click', () => { retryFailedStack(); });
    }
    _refreshPortWarnings();  // paint warnings for whatever is selected on entry
    return;
  }
  if (id === 'secrets') {
    const readRows = () => [...document.querySelectorAll('.wizard-secret-row')].map(r => ({
      name: r.querySelector('.wizard-secret-name').value.trim().toUpperCase().replace(/\s+/g, '_'),
      value: r.querySelector('.wizard-secret-value').value,
    }));
    document.getElementById('wizard-secret-add').addEventListener('click', () => {
      state.data.secrets = readRows();
      state.data.secrets.push({ name: '', value: '' });
      render();
    });
    document.querySelectorAll('.wizard-secret-remove').forEach(btn => {
      btn.addEventListener('click', () => {
        state.data.secrets = readRows();
        const i = parseInt(btn.closest('.wizard-secret-row').dataset.i, 10);
        state.data.secrets.splice(i, 1);
        render();
      });
    });
    document.querySelectorAll('.wizard-secret-name, .wizard-secret-value').forEach(el => {
      el.addEventListener('input', () => { state.data.secrets = readRows(); });
    });
    return;
  }
  if (id === 'n8n') {
    document.querySelectorAll('.wizard-platform-tab').forEach(el => {
      el.addEventListener('click', () => {
        state.data.n8n = state.data.n8n || {};
        state.data.n8n.guideTab = el.dataset.guide;
        render();
      });
    });
    document.querySelectorAll('.wizard-docker-os-tab').forEach(el => {
      el.addEventListener('click', () => {
        state.data.n8n = state.data.n8n || {};
        state.data.n8n.dockerOs = el.dataset.os;
        render();
      });
    });
    const nameEl = document.getElementById('wizard-n8n-name');
    const urlEl = document.getElementById('wizard-n8n-url');
    const keyContainer = document.getElementById('wizard-n8n-key-field');

    const n = state.data.n8n || {};
    n8nKeyField = secretField({
      container: keyContainer,
      label: 'API Key',
      prefix: 'N8N_KEY',
      context: (n.name || '').trim(),
      initialValue: n.api_key || '',
      placeholder: 'Paste your n8n API key',
    });

    const store = () => {
      state.data.n8n = {
        ...state.data.n8n,
        name: nameEl.value.trim(),
        url: urlEl.value.trim(),
        api_key: n8nKeyField ? n8nKeyField.getValue() : '',
      };
      if (n8nKeyField) n8nKeyField.setContext(nameEl.value.trim());
    };
    [nameEl, urlEl].forEach(el => el && el.addEventListener('input', store));

    const testBtn = document.getElementById('wizard-n8n-test');
    if (testBtn) testBtn.addEventListener('click', () => testN8n());
    return;
  }
  if (id === 'ai') {
    document.querySelectorAll('input[name="wizard-ai"]').forEach(el => {
      el.addEventListener('change', () => {
        state.data.ai = { provider: el.value, api_key: '', model: '' };
        render();
      });
    });

    const ai = state.data.ai;
    if (ai) {
      const modelSel = document.getElementById('wizard-ai-model');
      const customEl = document.getElementById('wizard-ai-model-custom');
      const isOllama = ai.provider === 'ollama';

      if (isOllama) {
        const keyEl = document.getElementById('wizard-ai-key');
        if (keyEl) keyEl.addEventListener('input', () => { state.data.ai.api_key = keyEl.value; });
      } else {
        const keyContainer = document.getElementById('wizard-ai-key-field');
        if (keyContainer) {
          const provPrefix = `${(ai.provider || 'LLM').toUpperCase()}_KEY`;
          aiKeyField = secretField({
            container: keyContainer,
            label: 'API Key',
            prefix: provPrefix,
            context: '',
            initialValue: ai.api_key || '',
            placeholder: AI_PROVIDERS.find(p => p.id === ai.provider)?.placeholder || 'sk-...',
          });
        }
      }

      if (modelSel) {
        populateModelSelect(modelSel, customEl, ai.provider, ai.model).catch(() => {});
        modelSel.addEventListener('change', () => {
          if (modelSel.value === '__custom__') {
            if (customEl) {
              customEl.style.display = '';
              customEl.focus();
              state.data.ai.model = customEl.value || '';
            }
          } else {
            if (customEl) customEl.style.display = 'none';
            state.data.ai.model = modelSel.value;
          }
        });
      }
      if (customEl) {
        customEl.addEventListener('input', () => {
          if (modelSel && modelSel.value === '__custom__') {
            state.data.ai.model = customEl.value;
          }
        });
      }

      const testBtn = document.getElementById('wizard-ai-test');
      if (testBtn) testBtn.addEventListener('click', () => testAI());
    }
    return;
  }
}

// ── API actions ─────────────────────────────────────────────────────────────

async function fetchModels(provider, ollamaUrl = '') {
  if (modelCache[provider] && provider !== 'ollama') return modelCache[provider];
  try {
    const qs = new URLSearchParams({ provider });
    if (provider === 'ollama' && ollamaUrl) qs.set('ollama_url', ollamaUrl);
    const data = await get(`/api/assistant/models?${qs.toString()}`);
    const models = Array.isArray(data.models) ? data.models : [];
    modelCache[provider] = models;
    return models;
  } catch {
    modelCache[provider] = [];
    return [];
  }
}

async function populateModelSelect(modelSel, customEl, provider, currentModel) {
  const ollamaUrl = provider === 'ollama' ? (state.data.ai?.api_key || '') : '';
  const models = await fetchModels(provider, ollamaUrl);
  const cur = currentModel || '';
  const inList = cur && models.some(m => m.id === cur);

  if (provider === 'ollama' && !models.length) {
    // No Ollama running — show placeholder, leave field empty.
    modelSel.innerHTML = `
      <option value="">Start Ollama and refresh</option>
      <option value="__custom__">&mdash; custom model &mdash;</option>
    `;
    if (customEl) customEl.style.display = 'none';
    state.data.ai.model = '';
    return;
  }

  const options = models.map(m =>
    `<option value="${esc(m.id)}" ${m.id === cur ? 'selected' : ''}>${esc(m.name || m.id)}</option>`
  );
  options.push(`<option value="__custom__" ${cur && !inList ? 'selected' : ''}>&mdash; custom model &mdash;</option>`);
  modelSel.innerHTML = options.join('');

  if (cur && !inList) {
    // Current model is not in the list — treat as custom.
    modelSel.value = '__custom__';
    if (customEl) {
      customEl.style.display = '';
      customEl.value = cur;
    }
    state.data.ai.model = cur;
  } else if (!cur && models.length) {
    // Nothing chosen yet — pick the first model.
    modelSel.value = models[0].id;
    if (customEl) customEl.style.display = 'none';
    state.data.ai.model = models[0].id;
  } else {
    if (customEl) customEl.style.display = 'none';
    state.data.ai.model = modelSel.value;
  }
}

function readAIKey() {
  const ai = state.data.ai;
  if (!ai) return '';
  if (ai.provider === 'ollama') {
    const keyEl = document.getElementById('wizard-ai-key');
    return keyEl ? keyEl.value : ai.api_key || '';
  }
  return aiKeyField ? aiKeyField.getValue() : (ai.api_key || '');
}

async function testAI() {
  const ai = state.data.ai;
  if (!ai) return;
  const key = readAIKey();
  if (!key) { toast.error(ai.provider === 'ollama' ? 'Enter your Ollama URL first' : 'Enter your API key first'); return; }
  const resultEl = document.getElementById('wizard-ai-test-result');
  resultEl.innerHTML = '<span style="color:var(--text-dim)">Testing...</span>';
  try {
    const body = { provider: ai.provider };
    if (ai.provider === 'ollama') body.ollama_url = key;
    else body.api_key = key;
    if (ai.model) body.model = ai.model;
    const r = await post('/api/admin/assistant/test', body);
    if (r.ok) {
      resultEl.innerHTML = `<span style="color:var(--success)">\u2713 Connected${r.model ? ` (${esc(r.model)})` : ''}</span>`;
    } else {
      resultEl.innerHTML = `<span style="color:var(--error)">${esc(r.error || 'Test failed')}</span>`;
    }
  } catch (e) {
    resultEl.innerHTML = `<span style="color:var(--error)">${esc(e.message)}</span>`;
  }
}

async function saveAI() {
  const ai = state.data.ai;
  if (!ai) return;
  let key = readAIKey();
  ai.api_key = key;

  // Promote raw keys to secrets store (not Ollama URLs).
  if (key && !key.startsWith('$') && ai.provider !== 'ollama') {
    try {
      const r = await post('/api/admin/secrets/promote', {
        value: key,
        prefix: `${ai.provider.toUpperCase()}_KEY`,
        context: '',
      });
      if (r && r.ref) {
        key = r.ref;
        ai.api_key = key;
        invalidateRefsCache();
      }
    } catch (e) {
      toast.error(`Could not save API key to secrets: ${e.message}. Storing inline instead.`);
    }
  }

  const body = { provider: ai.provider, api_key: key };
  if (ai.model) body.model = ai.model;
  if (ai.provider === 'ollama') body.ollama_url = key;
  await post('/api/assistant/config', body).catch(() => {});
}

async function testN8n() {
  const n = state.data.n8n || {};
  const name = document.getElementById('wizard-n8n-name')?.value.trim() || n.name;
  const url = document.getElementById('wizard-n8n-url')?.value.trim() || n.url;
  const api_key = n8nKeyField ? n8nKeyField.getValue() : (n.api_key || '');
  if (!url || !api_key) { toast.error('Fill URL and API key first'); return; }
  state.data.n8n = { ...n, name, url, api_key };
  const resultEl = document.getElementById('wizard-n8n-test-result');
  resultEl.innerHTML = '<span style="color:var(--text-dim)">Testing...</span>';
  try {
    const r = await fetch('/api/n8n/test-creds', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, api_key }),
    }).catch(() => null);
    if (r) {
      const data = await r.json().catch(() => ({}));
      if (r.ok) {
        if (data.connected) {
          resultEl.innerHTML = '<span style="color:var(--success)">\u2713 Connected</span>';
          state.data.n8nTestResult = { ok: true };
          return;
        }
        resultEl.innerHTML = `<span style="color:var(--error)">${esc(data.message || 'Connection failed')}</span>`;
        return;
      }
      resultEl.innerHTML = `<span style="color:var(--error)">${esc(data.detail || data.message || `HTTP ${r.status}`)}</span>`;
      return;
    }
    resultEl.innerHTML = '<span style="color:var(--text-dim)">Will validate when you click "Connect & Continue"</span>';
  } catch (e) {
    resultEl.innerHTML = `<span style="color:var(--error)">${esc(e.message)}</span>`;
  }
}

async function saveN8n() {
  const nameEl = document.getElementById('wizard-n8n-name');
  const urlEl = document.getElementById('wizard-n8n-url');
  const name = nameEl ? nameEl.value.trim() : '';
  const url = urlEl ? urlEl.value.trim() : '';
  let api_key = n8nKeyField ? n8nKeyField.getValue() : '';

  if (!name || !url || !api_key) {
    toast.error('Fill in all three fields');
    return false;
  }

  // Promote raw key to secrets store.
  if (!api_key.startsWith('$')) {
    try {
      const r = await post('/api/admin/secrets/promote', {
        value: api_key,
        prefix: 'N8N_KEY',
        context: name,
      });
      if (r && r.ref) {
        api_key = r.ref;
        invalidateRefsCache();
      }
    } catch (e) {
      toast.error(`Could not save key to secrets: ${e.message}. Storing inline instead.`);
    }
  }

  state.data.n8n = { ...(state.data.n8n || {}), name, url, api_key };

  try {
    await post('/api/n8n/instances', { name, url, api_key });
    toast.success(`Connected to ${name}`);
    return true;
  } catch (e) {
    toast.error(`Could not connect: ${e.message}`);
    return false;
  }
}

async function saveSecrets() {
  const toSave = state.data.secrets.filter(s => s.name && s.value);
  for (const s of toSave) {
    try {
      await post('/api/admin/secrets', { name: s.name, value: s.value });
    } catch (e) {
      toast.error(`Failed to save $${s.name}: ${e.message}`);
    }
  }
  if (toSave.length) toast.success(`Saved ${toSave.length} secret${toSave.length === 1 ? '' : 's'}`);
}

// ── util ────────────────────────────────────────────────────────────────────

function esc(s) { const d = document.createElement('span'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
