/**
 * AI Assistant view — chat stream on the LEFT, config card on the RIGHT.
 *
 * Persistent config (API keys, system prompt, knowledge files) lives in
 * Settings -> AI Settings. Everything in the right-column card here is
 * session-scoped — it writes to sessionStorage and never calls
 * `POST /api/assistant/config`.
 *
 * Right-column card contents:
 *   - Title
 *   - Provider + Model dropdowns (primary)
 *   - "+ Fallback" toggle: expands Provider + Model (fallback) inline
 *   - Test connection button (calls /api/admin/assistant/test)
 *   - Context toggle (Recent errors / Workflow list)
 *   - Quick Prompt chips (fill the input, user can edit before sending)
 */

import { get, post } from '../api.js';
import * as toast from '../components/toast.js';
import { attachApprovals, renderPendingActions } from '../components/tool-approval.js';

let chatHistory = [];
let isConfigured = false;

const CONTEXT_STORAGE_KEY = 'ageniusdesk:assistant_context';
const OVERRIDE_STORAGE_KEY = 'ageniusdesk:assistant_override';
const FALLBACK_STORAGE_KEY = 'ageniusdesk:assistant_fallback';

const DEFAULT_CONTEXT = { recent_errors: true, workflow_list: false };

const QUICK_PROMPTS = [
  { label: 'Diagnose recent errors',    text: 'What errors happened recently and how do I fix them?' },
  { label: 'Find failing workflows',    text: 'Which workflows are failing most often?' },
  { label: 'Optimization tips',         text: 'Suggest optimizations for my active workflows' },
  { label: 'Error handling guide',      text: 'How do I set up error handling in n8n?' },
];

const PROVIDER_KEY_CONVENTIONS = {
  anthropic: 'ANTHROPIC_KEY',
  openai: 'OPEN_AI_KEY',
  openrouter: 'OPEN_ROUTER_KEY',
};

// Cached set of existing secret names (uppercase). Refreshed on demand so the
// provider picker can warn when a chosen provider has no key configured yet.
let _secretNamesCache = null;
async function _loadSecretNames(force = false) {
  if (_secretNamesCache && !force) return _secretNamesCache;
  try {
    const r = await get('/api/admin/secrets/refs');
    _secretNamesCache = new Set((r.refs || []).map(x => (x.name || '').toUpperCase()));
  } catch {
    _secretNamesCache = new Set();
  }
  return _secretNamesCache;
}
export function invalidateAssistantSecretsCache() { _secretNamesCache = null; }

async function providerMissingKey(provider) {
  // Ollama doesn't need an API key; everything else uses $PROVIDER_KEY by convention.
  if (provider === 'ollama') return false;
  const needed = PROVIDER_KEY_CONVENTIONS[provider];
  if (!needed) return false;
  const names = await _loadSecretNames();
  return !names.has(needed);
}

function showPrimaryKeyHint(provider, missing) {
  const hintEl = document.getElementById('test-connection-result');
  const btn = document.getElementById('test-connection-btn');
  if (!hintEl || !btn) return;
  const needed = PROVIDER_KEY_CONVENTIONS[provider] || '';
  if (missing && needed) {
    hintEl.innerHTML = `
      <span style="color:var(--warning, #fbbf24)">No <code>$${esc(needed)}</code> in your secrets store.</span>
      <a href="#secrets" style="color:var(--accent);text-decoration:underline;font-size:11px;margin-left:4px">Add it →</a>
    `;
    btn.disabled = true;
    btn.title = `Add $${needed} in Secrets, then test`;
  } else {
    // Clear a prior warning so it doesn't persist after the user switches
    // to a provider whose key is present. Leave user-facing success/error
    // toasts from a real test alone — those are set later in initTestConnection.
    if (hintEl.querySelector('a[href="#secrets"]')) {
      hintEl.innerHTML = '';
    }
    btn.disabled = false;
    btn.title = '';
  }
}

function readContext() {
  try {
    const raw = sessionStorage.getItem(CONTEXT_STORAGE_KEY);
    if (!raw) return { ...DEFAULT_CONTEXT };
    const parsed = JSON.parse(raw);
    return {
      recent_errors: parsed.recent_errors !== undefined ? !!parsed.recent_errors : DEFAULT_CONTEXT.recent_errors,
      workflow_list: parsed.workflow_list !== undefined ? !!parsed.workflow_list : DEFAULT_CONTEXT.workflow_list,
    };
  } catch {
    return { ...DEFAULT_CONTEXT };
  }
}

function writeContext(ctx) {
  try {
    sessionStorage.setItem(CONTEXT_STORAGE_KEY, JSON.stringify(ctx));
  } catch { /* sessionStorage may be blocked */ }
}

export async function render(container) {
  try {
    const cfg = await get('/api/assistant/config');
    isConfigured = cfg.configured;
  } catch { isConfigured = false; }

  const ctx = readContext();

  container.innerHTML = `
    <div style="display:flex;gap:0;height:calc(100vh - 260px);min-height:400px;border-radius:var(--radius-lg);overflow:hidden;border:1px solid var(--border-dim)">
      <!-- LEFT: chat panel -->
      <div style="flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg-panel-solid)">

        <!-- Minimal top strip: just a clear button. The Provider/Model/Fallback
             pickers moved to the right-column config card. -->
        <div class="codelab-toolbar">
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:13px;font-weight:600;color:var(--text-primary)">AI Assistant</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <button class="btn btn-sm btn-ghost" onclick="window.__clearChat()" title="Clear chat" style="font-size:11px">Clear</button>
          </div>
        </div>

        <div id="assistant-override-warn" style="display:none;padding:6px 12px;font-size:11px;color:var(--warning, #fbbf24);border-bottom:1px solid var(--border-dim);background:var(--bg-void)"></div>

        <!-- Messages -->
        <div id="chat-messages" style="flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px">
          ${isConfigured ? '<div class="empty-state"><p>Ask me about your n8n workflows, errors, or anything automation-related.</p></div>' : renderSetupPrompt()}
        </div>

        <!-- Input row -->
        <div style="flex-shrink:0;padding:8px 12px 12px;position:relative;border-top:1px solid var(--border-dim)">
          <form id="chat-form" style="display:flex;gap:6px;align-items:center">
            <input type="text" id="chat-input" placeholder="${isConfigured ? 'Ask anything about your n8n workflows...' : 'Configure the assistant in Settings \u2192 AI Settings'}"
              style="flex:1;margin:0;padding:10px 14px;font-size:13px" ${!isConfigured ? 'disabled' : ''}>
            <button type="submit" class="btn btn-primary" ${!isConfigured ? 'disabled' : ''}>Send</button>
          </form>
        </div>
      </div>

      <!-- RIGHT: config card. Matches Code Lab's right panel shape. -->
      ${renderConfigCard(ctx)}
    </div>
  `;

  setupHandlers();
  initProviderPicker();
  initFallbackPicker();
  initTestConnection();
  initFallbackTestConnection();
}

function renderConfigCard(ctx) {
  return `
    <aside style="width:340px;flex-shrink:0;border-left:1px solid var(--border-dim);display:flex;flex-direction:column;background:var(--bg-panel);overflow-y:auto">
      <div style="padding:14px;display:flex;flex-direction:column;gap:10px">

        <!-- Title + Fallback toggle -->
        <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
          <span style="font-size:13px;font-weight:600;color:var(--text-primary)">AI Assistant</span>
          <button type="button" class="btn btn-sm btn-ghost" id="fallback-toggle" style="font-size:10px;padding:2px 8px" title="Configure a fallback provider for this session">+ Fallback</button>
        </div>

        <!-- Primary provider + model rows + Test primary button -->
        <div style="display:flex;flex-direction:column;gap:6px">
          <label style="font-size:10px;color:var(--text-dim);margin:0;display:flex;align-items:center;gap:6px">
            <span style="flex-shrink:0;min-width:56px">Provider</span>
            <select id="provider-select" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:11px;padding:4px 8px;font-family:var(--font-mono);margin:0;flex:1;min-width:0" title="Select provider (session only)">
              <option value="openrouter">OpenRouter</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="ollama">Ollama</option>
            </select>
          </label>
          <label style="font-size:10px;color:var(--text-dim);margin:0;display:flex;align-items:center;gap:6px">
            <span style="flex-shrink:0;min-width:56px">Model</span>
            <select id="model-select" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:11px;padding:4px 8px;font-family:var(--font-mono);margin:0;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">
              <option>Loading...</option>
            </select>
          </label>
          <div id="model-source-hint" style="font-size:10px;color:var(--text-dim);padding-left:62px;line-height:1.3"></div>
          <button type="button" class="btn btn-sm" id="test-connection-btn" style="font-size:11px;padding:6px 10px;justify-content:center;margin-top:4px">Test primary</button>
          <div id="test-connection-result" style="font-size:11px;line-height:1.3;min-height:14px"></div>
        </div>

        <!-- Fallback (collapsible, drops in inline). Test fallback button lives
             inside the row so it hides + shows with the fallback section. -->
        <div id="fallback-row" style="display:none;flex-direction:column;gap:6px;padding-top:8px;border-top:1px dashed var(--border-dim)">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-size:10px;color:var(--text-dim)">Fallback (used if primary fails)</div>
            <button type="button" id="fallback-clear" style="font-size:10px;padding:2px 6px;background:transparent;border:none;color:var(--text-dim);cursor:pointer;text-decoration:underline" title="Clear fallback">Clear</button>
          </div>
          <label style="font-size:10px;color:var(--text-dim);margin:0;display:flex;align-items:center;gap:6px">
            <span style="flex-shrink:0;min-width:56px">Provider</span>
            <select id="fallback-provider" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:11px;padding:4px 8px;font-family:var(--font-mono);margin:0;flex:1;min-width:0">
              <option value="">(none)</option>
              <option value="openrouter">OpenRouter</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="ollama">Ollama</option>
            </select>
          </label>
          <label style="font-size:10px;color:var(--text-dim);margin:0;display:flex;align-items:center;gap:6px">
            <span style="flex-shrink:0;min-width:56px">Model</span>
            <select id="fallback-model" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:11px;padding:4px 8px;font-family:var(--font-mono);margin:0;flex:1;min-width:0">
              <option value="">(choose provider first)</option>
            </select>
          </label>
          <button type="button" class="btn btn-sm" id="fallback-test-btn" style="font-size:11px;padding:6px 10px;justify-content:center;margin-top:4px">Test fallback</button>
          <div id="fallback-test-result" style="font-size:11px;line-height:1.3;min-height:14px"></div>
        </div>

        <!-- Context toggle -->
        <div style="padding-top:8px;border-top:1px solid var(--border-dim);display:flex;flex-direction:column;gap:6px">
          <div style="font-size:10px;font-weight:600;color:var(--text-secondary);letter-spacing:.3px;text-transform:uppercase">Include in request</div>
          <label style="display:flex;align-items:center;gap:6px;margin:0;font-size:12px;cursor:pointer">
            <input type="checkbox" id="ctx-errors" ${ctx.recent_errors ? 'checked' : ''}> Recent errors
          </label>
          <label style="display:flex;align-items:center;gap:6px;margin:0;font-size:12px;cursor:pointer">
            <input type="checkbox" id="ctx-workflows" ${ctx.workflow_list ? 'checked' : ''}> Workflow list
          </label>
        </div>

        <!-- Quick Prompts -->
        <div style="padding-top:8px;border-top:1px solid var(--border-dim);display:flex;flex-direction:column;gap:6px">
          <div style="font-size:10px;font-weight:600;color:var(--text-secondary);letter-spacing:.3px;text-transform:uppercase">Quick prompts</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">
            ${QUICK_PROMPTS.map((p, i) => `
              <button type="button" class="quick-prompt-chip" data-qp="${i}" title="${esc(p.text)}"
                style="background:transparent;border:1px solid var(--border-dim);color:var(--text-secondary);border-radius:999px;padding:3px 8px;font-size:10px;cursor:pointer;font-family:var(--font-body);transition:all .15s"
                onmouseover="this.style.borderColor='var(--accent)';this.style.color='var(--text-primary)'"
                onmouseout="this.style.borderColor='var(--border-dim)';this.style.color='var(--text-secondary)'"
              >${esc(p.label)}</button>
            `).join('')}
          </div>
        </div>
      </div>
    </aside>
  `;
}

// ── Provider + Model picker (session-scoped override) ──────────────────────

function readOverride() {
  try {
    const raw = sessionStorage.getItem(OVERRIDE_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function writeOverride(provider, model) {
  try {
    sessionStorage.setItem(OVERRIDE_STORAGE_KEY, JSON.stringify({ provider, model }));
  } catch { /* sessionStorage may be blocked */ }
}

function readFallback() {
  try {
    const raw = sessionStorage.getItem(FALLBACK_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function writeFallback(provider, model) {
  try {
    if (!provider) sessionStorage.removeItem(FALLBACK_STORAGE_KEY);
    else sessionStorage.setItem(FALLBACK_STORAGE_KEY, JSON.stringify({ provider, model }));
  } catch { /* sessionStorage may be blocked */ }
}

// Persist widget dropdown changes back to /api/assistant/config so the compact
// AI Assistant widget and the AI Settings page stay in sync. Merge-style update
// (read current config, apply patch, POST) because the backend overwrites every
// non-api_key field with whatever ConfigRequest defaults to.
async function persistAssistantConfig(patch) {
  try {
    const cur = await get('/api/assistant/config');
    const body = {
      provider: cur.provider || 'openrouter',
      model: cur.model || '',
      ollama_url: cur.ollama_url || 'http://localhost:11434',
      qdrant_url: cur.qdrant_url || '',
      qdrant_collection: cur.qdrant_collection || '',
      system_prompt: cur.system_prompt || '',
      fallback_provider: cur.fallback_provider || '',
      fallback_model: cur.fallback_model || '',
      // api_key intentionally omitted — backend only updates it when non-empty
      ...patch,
    };
    await post('/api/assistant/config', body);
  } catch (e) {
    console.warn('[assistant] persistAssistantConfig failed:', e);
  }
}

function getCurrentOverride() {
  const provSel = document.getElementById('provider-select');
  const modelSel = document.getElementById('model-select');
  const provider = provSel?.value || '';
  const model = modelSel?.value || '';
  if (!provider && !model) return null;
  return { provider, model };
}

function getCurrentFallback() {
  const provSel = document.getElementById('fallback-provider');
  const modelSel = document.getElementById('fallback-model');
  const provider = provSel?.value || '';
  if (!provider) return null;
  const model = modelSel?.value || '';
  return { provider, model };
}

function showOverrideWarn(msg) {
  const el = document.getElementById('assistant-override-warn');
  if (!el) return;
  el.textContent = msg || '';
  el.style.display = msg ? 'block' : 'none';
}

// Amber inline notice for stale saved models. Mirrors the Settings version so
// users see the same explanation anywhere the picker renders. Auto-dismisses
// after 30s; the view's re-render on navigation also removes the node.
let _assistantStaleTimer = null;
function dismissAssistantStaleNotice() {
  if (_assistantStaleTimer) { clearTimeout(_assistantStaleTimer); _assistantStaleTimer = null; }
  const n = document.getElementById('assistant-stale-model-notice');
  if (n) n.remove();
}
function showAssistantStaleNotice(oldModel, provider, newName) {
  dismissAssistantStaleNotice();
  const warn = document.getElementById('assistant-override-warn');
  if (!warn) return;
  const n = document.createElement('div');
  n.id = 'assistant-stale-model-notice';
  n.style.cssText = [
    'background:#fbbf2422',
    'border-bottom:1px solid #fbbf2488',
    'color:#b45309',
    'padding:8px 12px',
    'font-size:11px',
    'line-height:1.4',
  ].join(';');
  n.innerHTML = `&#9888; Saved model &lsquo;${esc(oldModel)}&rsquo; is not available for ${esc(provider)}. Updated to &lsquo;${esc(newName)}&rsquo;. Save it in <a href="#" id="assistant-stale-goto" style="color:#b45309;text-decoration:underline">AI Settings</a> to confirm.`;
  warn.parentNode?.insertBefore(n, warn);
  document.getElementById('assistant-stale-goto')?.addEventListener('click', (e) => {
    e.preventDefault();
    if (window.__goSettings) window.__goSettings('assistant');
  });
  _assistantStaleTimer = setTimeout(dismissAssistantStaleNotice, 30000);
}

function formatAgo(ts) {
  if (!ts) return '';
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  return `${Math.floor(diffSec / 3600)}h ago`;
}

function updateModelSourceHint(source, cachedAt) {
  const el = document.getElementById('model-source-hint');
  if (!el) return;
  if (source === 'live') {
    el.textContent = 'Models synced just now';
  } else if (source === 'cached' && cachedAt) {
    el.textContent = `Models synced ${formatAgo(cachedAt)}`;
  } else if (source === 'fallback') {
    el.textContent = 'Default list (no live sync)';
  } else {
    el.textContent = '';
  }
}

async function initProviderPicker() {
  const provSel = document.getElementById('provider-select');
  const modelSel = document.getElementById('model-select');
  if (!provSel || !modelSel) return;

  let defaultProvider = 'openrouter';
  let defaultModel = '';
  try {
    const cfg = await get('/api/assistant/config');
    // Seed from the General Assistant job (Settings > Models), falling back to
    // the legacy global provider/model for older configs.
    const sd = (cfg.jobs || {}).assistant || {};
    defaultProvider = sd.provider || cfg.provider || 'openrouter';
    defaultModel = sd.model || cfg.model || '';
  } catch { /* use fallbacks */ }

  const saved = readOverride();
  const startProvider = saved?.provider || defaultProvider;
  provSel.value = startProvider;

  await populateModels(modelSel, startProvider, saved?.model || defaultModel);
  showPrimaryKeyHint(startProvider, await providerMissingKey(startProvider));

  // Auto-repair notice: if no session override AND the saved persistent model
  // is missing from the live list for the saved provider, warn inline on mount.
  if (!saved && defaultModel) {
    const inList = !!modelSel.querySelector(`option[value="${CSS.escape(defaultModel)}"]`);
    if (!inList && modelSel.options.length && modelSel.options[0].value) {
      const newModel = modelSel.options[0].value;
      const newName = modelSel.options[0].textContent || newModel;
      showAssistantStaleNotice(defaultModel, defaultProvider, newName);
    }
  }

  provSel.addEventListener('change', async () => {
    showOverrideWarn('');
    const newProvider = provSel.value;
    modelSel.value = '';
    modelSel.innerHTML = '<option>Loading...</option>';
    await populateModels(modelSel, newProvider, '');
    writeOverride(newProvider, modelSel.value || '');
    // Persist to backend so AI Settings reflects the change.
    persistAssistantConfig({ provider: newProvider, model: modelSel.value || '' });
    // Refresh the "key missing?" hint with a fresh secrets list so users see
    // newly added keys without reloading.
    invalidateAssistantSecretsCache();
    showPrimaryKeyHint(newProvider, await providerMissingKey(newProvider));
  });

  modelSel.addEventListener('change', () => {
    writeOverride(provSel.value, modelSel.value || '');
    persistAssistantConfig({ provider: provSel.value, model: modelSel.value || '' });
  });

  // Live-update when the General Assistant area default is saved in Settings.
  // Registered once at module scope; a no-op when the picker isn't mounted.
  if (!_areaDefaultListenerBound) {
    window.addEventListener('agd:area-defaults-saved', (e) => applyAreaDefaultLive(e.detail || {}));
    _areaDefaultListenerBound = true;
  }
}

let _areaDefaultListenerBound = false;

async function applyAreaDefaultLive(detail) {
  const provSel = document.getElementById('provider-select');
  const modelSel = document.getElementById('model-select');
  if (!provSel || !modelSel) return; // Assistant picker not currently mounted.
  try { sessionStorage.removeItem(OVERRIDE_STORAGE_KEY); } catch { /* ignore */ }
  showOverrideWarn('');
  let provider = '';
  let model = '';
  const sd = detail && detail.assistant;
  if (sd && sd.provider && sd.model) {
    provider = sd.provider;
    model = sd.model;
  } else {
    try {
      const cfg = await get('/api/assistant/config');
      provider = cfg.provider || 'openrouter';
      model = cfg.model || '';
    } catch { provider = 'openrouter'; }
  }
  provSel.value = provider;
  await populateModels(modelSel, provider, model);
}

async function initFallbackPicker() {
  const toggleBtn = document.getElementById('fallback-toggle');
  const row = document.getElementById('fallback-row');
  const provSel = document.getElementById('fallback-provider');
  const modelSel = document.getElementById('fallback-model');
  const clearBtn = document.getElementById('fallback-clear');
  if (!toggleBtn || !row || !provSel || !modelSel) return;

  // Prefer backend-persisted fallback, fall back to sessionStorage for the
  // migration window where old sessions still carry a sessionStorage value.
  let savedProvider = '';
  let savedModel = '';
  try {
    const cfg = await get('/api/assistant/config');
    savedProvider = cfg.fallback_provider || '';
    savedModel = cfg.fallback_model || '';
  } catch { /* ignore */ }
  if (!savedProvider) {
    const ss = readFallback();
    if (ss?.provider) { savedProvider = ss.provider; savedModel = ss.model || ''; }
  }

  // Show/hide the fallback section. Test fallback button + result live inside
  // the row now, so they follow visibility automatically.
  function setFallbackVisible(visible) {
    row.style.display = visible ? 'flex' : 'none';
    toggleBtn.textContent = visible ? '\u2212 Fallback' : '+ Fallback';
  }

  if (savedProvider) {
    setFallbackVisible(true);
    provSel.value = savedProvider;
    await populateModels(modelSel, savedProvider, savedModel);
  }

  toggleBtn.addEventListener('click', () => {
    setFallbackVisible(row.style.display === 'none');
  });

  provSel.addEventListener('change', async () => {
    const newProvider = provSel.value;
    if (!newProvider) {
      modelSel.innerHTML = '<option value="">(choose provider first)</option>';
      writeFallback('', '');
      persistAssistantConfig({ fallback_provider: '', fallback_model: '' });
      setFallbackVisible(false);
      return;
    }
    modelSel.value = '';
    modelSel.innerHTML = '<option>Loading...</option>';
    await populateModels(modelSel, newProvider, '');
    writeFallback(newProvider, modelSel.value || '');
    persistAssistantConfig({ fallback_provider: newProvider, fallback_model: modelSel.value || '' });
  });

  modelSel.addEventListener('change', () => {
    writeFallback(provSel.value, modelSel.value || '');
    persistAssistantConfig({ fallback_provider: provSel.value, fallback_model: modelSel.value || '' });
  });

  clearBtn?.addEventListener('click', () => {
    provSel.value = '';
    modelSel.innerHTML = '<option value="">(choose provider first)</option>';
    writeFallback('', '');
    persistAssistantConfig({ fallback_provider: '', fallback_model: '' });
    setFallbackVisible(false);
  });
}

async function populateModels(modelSel, provider, preferredModel) {
  if (!modelSel) return;
  if (!provider) {
    modelSel.innerHTML = '<option value="">(choose provider first)</option>';
    return;
  }
  try {
    const data = await get(`/api/assistant/models?provider=${encodeURIComponent(provider)}`);
    const models = data.models || [];
    const isPrimary = modelSel.id === 'model-select';
    if (isPrimary) updateModelSourceHint(data.source, data.cached_at);
    if (!models.length) {
      modelSel.innerHTML = '<option value="">No models</option>';
      return;
    }
    modelSel.innerHTML = models.map(m =>
      `<option value="${esc(m.id)}">${esc(m.name)}${m.provider ? ` (${esc(m.provider)})` : ''}</option>`
    ).join('');
    if (preferredModel && modelSel.querySelector(`option[value="${CSS.escape(preferredModel)}"]`)) {
      modelSel.value = preferredModel;
    } else {
      modelSel.selectedIndex = 0;
    }
  } catch {
    modelSel.innerHTML = '<option value="">Failed to load</option>';
  }
}

async function fallbackToConfigDefault(warnMsg) {
  showOverrideWarn(warnMsg);
  sessionStorage.removeItem(OVERRIDE_STORAGE_KEY);
  try {
    const cfg = await get('/api/assistant/config');
    const defProv = cfg.provider || 'openrouter';
    const defModel = cfg.model || '';
    const provSel = document.getElementById('provider-select');
    const modelSel = document.getElementById('model-select');
    if (provSel) provSel.value = defProv;
    if (modelSel) await populateModels(modelSel, defProv, defModel);
  } catch { /* best-effort */ }
}

// ── Test connection ─────────────────────────────────────────────────────────

function initTestConnection() {
  const btn = document.getElementById('test-connection-btn');
  const resultEl = document.getElementById('test-connection-result');
  if (!btn || !resultEl) return;

  let clearTimer = null;

  btn.addEventListener('click', async () => {
    const provSel = document.getElementById('provider-select');
    const modelSel = document.getElementById('model-select');
    const provider = provSel?.value || '';
    const model = modelSel?.value || '';

    if (!provider) {
      resultEl.innerHTML = `<span style="color:var(--warning, #fbbf24)">Pick a provider first</span>`;
      return;
    }

    // Bail before the request if the convention-named key is missing — a 401
    // is guaranteed and the error surface is noisy. Invalidate the cache here
    // in case the user just added the secret in another tab.
    invalidateAssistantSecretsCache();
    if (await providerMissingKey(provider)) {
      showPrimaryKeyHint(provider, true);
      return;
    }

    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = 'Testing...';
    resultEl.textContent = '';
    if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }

    try {
      const body = { provider, model };
      if (provider === 'ollama') {
        // Ollama does not need a key; backend uses the configured ollama_url.
        body.api_key = '';
      } else {
        const convention = PROVIDER_KEY_CONVENTIONS[provider];
        if (convention) body.api_key = `$${convention}`;
      }

      const r = await post('/api/admin/assistant/test', body);
      if (r.ok) {
        const label = r.model || model || 'connected';
        resultEl.innerHTML = `<span style="color:var(--success, #34d399)">\u2713 Connected (${esc(label)})</span>`;
      } else {
        const msg = (r.error || 'failed').toString().slice(0, 160);
        resultEl.innerHTML = `<span style="color:var(--error, #ff6d5a)">\u2717 ${esc(msg)}</span>`;
      }
    } catch (e) {
      const msg = (e.message || 'request failed').toString().slice(0, 160);
      resultEl.innerHTML = `<span style="color:var(--error, #ff6d5a)">\u2717 ${esc(msg)}</span>`;
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
      clearTimer = setTimeout(() => { resultEl.textContent = ''; }, 10000);
    }
  });
}

function initFallbackTestConnection() {
  const btn = document.getElementById('fallback-test-btn');
  const resultEl = document.getElementById('fallback-test-result');
  if (!btn || !resultEl) return;

  let clearTimer = null;

  btn.addEventListener('click', async () => {
    const provSel = document.getElementById('fallback-provider');
    const modelSel = document.getElementById('fallback-model');
    const provider = provSel?.value || '';
    const model = modelSel?.value || '';

    if (!provider) {
      resultEl.innerHTML = `<span style="color:var(--warning, #fbbf24)">Pick a fallback provider first</span>`;
      return;
    }

    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = 'Testing...';
    resultEl.textContent = '';
    if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }

    try {
      const body = { provider, model };
      if (provider === 'ollama') {
        body.api_key = '';
      } else {
        const convention = PROVIDER_KEY_CONVENTIONS[provider];
        if (convention) body.api_key = `$${convention}`;
      }

      const r = await post('/api/admin/assistant/test', body);
      if (r.ok) {
        const label = r.model || model || 'connected';
        resultEl.innerHTML = `<span style="color:var(--success, #34d399)">\u2713 Fallback OK (${esc(label)})</span>`;
      } else {
        const msg = (r.error || 'failed').toString().slice(0, 160);
        resultEl.innerHTML = `<span style="color:var(--error, #ff6d5a)">\u2717 ${esc(msg)}</span>`;
      }
    } catch (e) {
      const msg = (e.message || 'request failed').toString().slice(0, 160);
      resultEl.innerHTML = `<span style="color:var(--error, #ff6d5a)">\u2717 ${esc(msg)}</span>`;
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
      clearTimer = setTimeout(() => { resultEl.textContent = ''; }, 10000);
    }
  });
}

function renderSetupPrompt() {
  return `
    <div class="empty-state">
      <h3>Set up AI Assistant</h3>
      <p>Head to <a href="#" id="go-ai-settings" style="color:var(--accent)">Settings &rarr; AI Settings</a> to pick a provider and add an API key. Free keys are available at <a href="https://openrouter.ai/keys" target="_blank">openrouter.ai/keys</a>.</p>
    </div>
  `;
}

// ── Handlers ────────────────────────────────────────────────────────────────

function setupHandlers() {
  // Deep link from the setup prompt empty-state into AI Settings.
  const goLink = document.getElementById('go-ai-settings');
  if (goLink) {
    goLink.addEventListener('click', (e) => {
      e.preventDefault();
      if (window.__goSettings) window.__goSettings('assistant');
      else if (window.__nav) window.__nav('settings');
    });
  }

  // Context checkbox persistence
  const ctxErrors = document.getElementById('ctx-errors');
  const ctxWorkflows = document.getElementById('ctx-workflows');
  const persistContext = () => {
    writeContext({
      recent_errors: !!ctxErrors?.checked,
      workflow_list: !!ctxWorkflows?.checked,
    });
  };
  ctxErrors?.addEventListener('change', persistContext);
  ctxWorkflows?.addEventListener('change', persistContext);

  // Quick Prompt chips: fill input, focus, let user edit before sending.
  document.querySelectorAll('.quick-prompt-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!isConfigured) { toast.error('Configure AI assistant first in Settings \u2192 AI Settings'); return; }
      const idx = parseInt(btn.dataset.qp, 10);
      const prompt = QUICK_PROMPTS[idx];
      if (!prompt) return;
      const input = document.getElementById('chat-input');
      if (input) {
        input.value = prompt.text;
        input.focus();
        try { input.setSelectionRange(input.value.length, input.value.length); } catch { /* ignore */ }
      }
    });
  });

  // Chat form
  document.getElementById('chat-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = document.getElementById('chat-input');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    await sendMessage(msg);
  });
}

// ── Chat ────────────────────────────────────────────────────────────────────

async function sendMessage(text) {
  const messagesEl = document.getElementById('chat-messages');

  // Clear empty state
  const empty = messagesEl.querySelector('.empty-state');
  if (empty) empty.remove();

  // Add user message
  chatHistory.push({ role: 'user', content: text });
  messagesEl.innerHTML += renderMessage('user', text);

  // Show typing indicator
  const typingId = 'typing-' + Date.now();
  messagesEl.innerHTML += `<div id="${typingId}" class="chat-msg chat-msg--assistant"><div class="chat-msg-content"><div class="spinner" style="margin:0;width:16px;height:16px"></div></div></div>`;
  messagesEl.scrollTop = messagesEl.scrollHeight;

  // Build context
  let context = '';
  try {
    if (document.getElementById('ctx-errors')?.checked) {
      const errData = await get('/api/errors?limit=5');
      if (errData.errors?.length) {
        context += '## Recent Errors (Dashboard Log)\n';
        for (const e of errData.errors) {
          context += `- **${e.workflow_name}** (${e.node_name || 'unknown node'}): ${e.error_message}\n`;
        }
      }
      try {
        const execData = await get('/api/n8n/executions?limit=10&status=error');
        if (execData.executions?.length) {
          context += '\n## Recent Failed Executions (from n8n)\n';
          for (const ex of execData.executions) {
            context += `- Execution ${ex.id}: **${ex.workflow_name || ex.workflowId || 'unknown'}** \u2014 ${ex.status} (${ex.started_at || ex.startedAt || 'unknown time'})\n`;
          }
        }
      } catch { /* n8n may not be reachable */ }
    }
    if (document.getElementById('ctx-workflows')?.checked) {
      const wfData = await get('/api/n8n/workflows?limit=50&active_only=true');
      if (wfData.workflows?.length) {
        context += '\n## Active Workflows\n';
        for (const w of wfData.workflows) {
          context += `- ${w.name} (${w.trigger_type})\n`;
        }
      }
    }
  } catch { /* context is optional */ }

  // Send to API
  try {
    const override = getCurrentOverride();
    const fallback = getCurrentFallback();
    const result = await post('/api/assistant/chat', {
      messages: chatHistory,
      context,
      override,
      fallback,
      surface: 'assistant',
    });

    document.getElementById(typingId)?.remove();

    const response = result.response || 'No response';
    chatHistory.push({ role: 'assistant', content: response });

    if (result.served_by === 'fallback') {
      const fbMeta = `${result.provider || ''} / ${result.model || ''}`.trim();
      const primaryErr = result.primary_error || 'primary failed';
      messagesEl.innerHTML += `
        <div style="padding:6px 10px;font-size:11px;color:var(--text-dim);border-left:2px solid var(--warning, #fbbf24);background:var(--bg-void);border-radius:4px;margin:2px 0">
          Answered by fallback: <strong>${esc(fbMeta)}</strong>. Primary failed: ${esc(primaryErr)}
        </div>
      `;
    }

    messagesEl.innerHTML += renderMessage('assistant', response, result.model);
    // Any state-changing tool the model picked did NOT run — it is proposed here
    // for the operator to approve. Rendering these is not optional: a surface
    // that drops them leaves the assistant claiming actions that never happened.
    messagesEl.innerHTML += renderPendingActions(result.pending_actions);
    attachApprovals(messagesEl);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } catch (e) {
    document.getElementById(typingId)?.remove();
    const msg = e.message || 'Request failed';
    if (/is not configured/i.test(msg)) {
      await fallbackToConfigDefault(msg);
    }
    messagesEl.innerHTML += renderMessage('assistant', `Error: ${msg}`, '', true);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
}

window.__clearChat = () => {
  chatHistory = [];
  const el = document.getElementById('chat-messages');
  if (el) el.innerHTML = '<div class="empty-state"><p>Ask me about your n8n workflows, errors, or anything automation-related.</p></div>';
};

function renderMessage(role, content, model = '', isError = false) {
  const isUser = role === 'user';
  return `
    <div class="chat-msg chat-msg--${role}">
      <div class="chat-msg-content ${isError ? 'chat-msg-error' : ''}">
        ${isUser ? esc(content) : renderMarkdown(content)}
      </div>
      ${model ? `<div class="chat-msg-meta">${esc(model)}</div>` : ''}
    </div>
  `;
}

function renderMarkdown(text) {
  const codeBlocks = [];
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    codeBlocks.push(`<pre style="background:var(--bg-void);padding:10px 12px;border-radius:var(--radius);overflow-x:auto;margin:8px 0;font-size:12px"><code>${code.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</code></pre>`);
    return `\x00CB${codeBlocks.length - 1}\x00`;
  });

  const lines = text.split('\n');
  let html = '';
  let inUl = false, inOl = false;

  for (const line of lines) {
    const trimmed = line.trim();

    if (trimmed.startsWith('### ')) { closeLists(); html += `<h4 style="font-size:13px;font-weight:600;margin:12px 0 4px">${inline(trimmed.slice(4))}</h4>`; continue; }
    if (trimmed.startsWith('## '))  { closeLists(); html += `<h3 style="font-size:14px;font-weight:600;margin:14px 0 6px">${inline(trimmed.slice(3))}</h3>`; continue; }
    if (trimmed.startsWith('# '))   { closeLists(); html += `<h2 style="font-size:15px;font-weight:700;margin:14px 0 6px">${inline(trimmed.slice(2))}</h2>`; continue; }

    if (/^[-*] /.test(trimmed)) {
      if (!inUl) { closeLists(); html += '<ul style="padding-left:18px;margin:4px 0">'; inUl = true; }
      html += `<li style="margin:2px 0">${inline(trimmed.replace(/^[-*] /, ''))}</li>`;
      continue;
    }

    if (/^\d+\. /.test(trimmed)) {
      if (!inOl) { closeLists(); html += '<ol style="padding-left:18px;margin:4px 0">'; inOl = true; }
      html += `<li style="margin:2px 0">${inline(trimmed.replace(/^\d+\. /, ''))}</li>`;
      continue;
    }

    if (!trimmed) { closeLists(); html += '<br>'; continue; }

    closeLists();
    html += `<p style="margin:4px 0">${inline(trimmed)}</p>`;
  }
  closeLists();

  html = html.replace(/\x00CB(\d+)\x00/g, (_, i) => codeBlocks[parseInt(i)]);

  return html;

  function closeLists() {
    if (inUl) { html += '</ul>'; inUl = false; }
    if (inOl) { html += '</ol>'; inOl = false; }
  }

  function inline(s) {
    // esc() FIRST: assistant output is attacker-influenceable (prompt injection
    // via RAG / MCP / n8n error text), so HTML must be neutralized before the
    // markdown regexes run. Markdown syntax chars aren't HTML-special, so
    // escaping doesn't disturb the transforms below.
    return esc(s)
      .replace(/`([^`]+)`/g, '<code style="padding:1px 4px;background:var(--bg-void);border-radius:3px;font-size:12px;font-family:var(--font-mono)">$1</code>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      // Only emit a link for http(s) hrefs — blocks javascript:/data: URIs.
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, text, href) =>
        /^https?:\/\//i.test(href.trim()) ? `<a href="${href}" target="_blank" style="color:var(--accent)">${text}</a>` : text);
  }
}

function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : String(s); return el.innerHTML; }
