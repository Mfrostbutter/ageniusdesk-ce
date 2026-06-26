/**
 * Code Lab — in-browser code editor for n8n Code node development with AI assist.
 * Uses Monaco Editor (VS Code's editor) loaded from CDN.
 */

import { get, post } from '../api.js';
import * as toast from '../components/toast.js';

let editor = null;
let monacoLoaded = false;
let _mode = 'code';  // 'code' (Code-node snippet) | 'workflow' (workflow JSON builder)

export async function render(container) {
  container.innerHTML = `
    <div style="display:flex;gap:0;height:calc(100vh - 260px);min-height:400px;border-radius:var(--radius-lg);overflow:hidden;border:1px solid var(--border-dim)">
      <!-- Editor panel -->
      <div style="flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg-panel-solid)">
        <!-- Toolbar -->
        <div class="codelab-toolbar">
          <div style="display:flex;align-items:center;gap:8px">
            <div style="display:flex;border:1px solid var(--border-dim);border-radius:4px;overflow:hidden">
              <button id="mode-code" class="btn btn-sm btn-primary" style="border-radius:0;font-size:11px" title="Write n8n Code-node code">Code Node</button>
              <button id="mode-workflow" class="btn btn-sm" style="border-radius:0;font-size:11px" title="Describe a workflow and generate its JSON">Workflow Builder</button>
            </div>
            <span id="code-mode-tools" style="display:flex;align-items:center;gap:8px">
              <select id="code-template" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:11px;padding:4px 8px;font-family:var(--font-mono);width:auto;margin:0">
                <option value="blank">Blank</option>
                <option value="transform">Transform Items</option>
                <option value="filter">Filter Items</option>
                <option value="http">HTTP Request</option>
                <option value="aggregate">Aggregate Data</option>
                <option value="split">Split into Batches</option>
                <option value="webhook-response">Webhook Response</option>
              </select>
              <select id="code-lang" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:11px;padding:4px 8px;font-family:var(--font-mono);width:auto;margin:0">
                <option value="javascript">JavaScript</option>
                <option value="typescript">TypeScript</option>
                <option value="python">Python</option>
              </select>
            </span>
            <button id="codelab-prompt-builder" class="btn btn-sm" onclick="window.__openPromptBuilder()" title="Build a structured agent system prompt" style="font-size:11px">Prompt Builder</button>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <button class="btn btn-sm" onclick="window.__codeFormat()" title="Format code">Format</button>
            <button class="btn btn-sm" onclick="window.__codeCopy()" title="Copy to clipboard">Copy</button>
            <button class="btn btn-sm btn-primary" id="code-send-btn" onclick="window.__codeSendToN8n()" title="Create workflow with this code">Send to n8n</button>
          </div>
        </div>

        <!-- Monaco container -->
        <div id="monaco-container" style="flex:1;min-height:0"></div>

        <!-- Output panel -->
        <div class="codelab-output">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 12px;border-bottom:1px solid var(--border-dim)">
            <span style="font-size:11px;font-weight:600;color:var(--text-secondary)">OUTPUT</span>
            <button class="btn btn-sm btn-ghost" onclick="document.getElementById('code-output').textContent=''" style="font-size:10px;padding:2px 6px">Clear</button>
          </div>
          <pre id="code-output" style="padding:8px 12px;font-size:12px;font-family:var(--font-mono);color:var(--text-secondary);margin:0;max-height:120px;overflow-y:auto;overflow-x:auto;word-break:break-word;white-space:pre-wrap"></pre>
        </div>
      </div>

      <!-- AI sidebar -->
      <div style="width:320px;flex-shrink:0;border-left:1px solid var(--border-dim);display:flex;flex-direction:column;background:var(--bg-panel)">
        <div style="padding:12px;border-bottom:1px solid var(--border-dim);display:flex;flex-direction:column;gap:8px">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
            <span style="font-size:13px;font-weight:600">AI Code Assistant</span>
            <div style="display:flex;gap:4px">
              <button type="button" class="btn btn-sm btn-ghost" onclick="window.__goSettings && window.__goSettings('assistant')" style="font-size:10px;padding:2px 6px" title="Edit the Code Lab instructions (AI Settings)">Instructions</button>
              <button type="button" class="btn btn-sm btn-ghost" id="codelab-fallback-toggle" style="font-size:10px;padding:2px 6px" title="Configure a fallback provider">+ Fallback</button>
            </div>
          </div>
          <!-- Primary provider card -->
          <div style="display:flex;flex-direction:column;gap:4px;padding:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border-dim);border-radius:6px">
            <label style="font-size:10px;color:var(--text-dim);margin:0;display:flex;align-items:center;gap:6px">
              <span style="flex-shrink:0;min-width:52px">Provider</span>
              <select id="codelab-provider" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:10px;padding:3px 6px;font-family:var(--font-mono);margin:0;flex:1;min-width:0" title="Select provider">
                <option value="openrouter">OpenRouter</option>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="ollama">Ollama</option>
              </select>
            </label>
            <label style="font-size:10px;color:var(--text-dim);margin:0;display:flex;align-items:center;gap:6px">
              <span style="flex-shrink:0;min-width:52px">Model</span>
              <select id="codelab-model" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:10px;padding:3px 6px;font-family:var(--font-mono);margin:0;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis" title="Select model">
                <option value="">Loading...</option>
              </select>
            </label>
            <div id="codelab-model-source-hint" style="font-size:10px;color:var(--text-dim);line-height:1.3;padding-left:58px"></div>
            <div id="codelab-override-warn" style="display:none;font-size:10px;color:var(--warning, #fbbf24);line-height:1.3"></div>
            <button type="button" class="btn btn-sm" id="codelab-test-btn" style="font-size:10px;padding:4px 8px;justify-content:center;margin-top:2px">Test connection</button>
            <div id="codelab-test-result" style="font-size:10px;line-height:1.3;min-height:12px"></div>
          </div>
          <!-- Fallback provider card (shown when + Fallback is active) -->
          <div id="codelab-fallback-row" style="display:none;flex-direction:column;gap:4px;padding:8px;background:rgba(255,255,255,0.02);border:1px dashed var(--border-dim);border-radius:6px">
            <div style="font-size:10px;color:var(--text-dim);font-weight:600;text-transform:uppercase;letter-spacing:0.4px">Fallback (used if primary fails)</div>
            <label style="font-size:10px;color:var(--text-dim);margin:0;display:flex;align-items:center;gap:6px">
              <span style="flex-shrink:0;min-width:52px">Provider</span>
              <select id="codelab-fallback-provider" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:10px;padding:3px 6px;font-family:var(--font-mono);margin:0;flex:1;min-width:0">
                <option value="">(none)</option>
                <option value="openrouter">OpenRouter</option>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="ollama">Ollama</option>
              </select>
            </label>
            <label style="font-size:10px;color:var(--text-dim);margin:0;display:flex;align-items:center;gap:6px">
              <span style="flex-shrink:0;min-width:52px">Model</span>
              <select id="codelab-fallback-model" style="background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-secondary);font-size:10px;padding:3px 6px;font-family:var(--font-mono);margin:0;flex:1;min-width:0">
                <option value="">(choose provider first)</option>
              </select>
            </label>
            <button type="button" class="btn btn-sm" id="codelab-fallback-test-btn" style="font-size:10px;padding:4px 8px;justify-content:center;margin-top:2px">Test connection</button>
            <div id="codelab-fallback-test-result" style="font-size:10px;line-height:1.3;min-height:12px"></div>
          </div>
        </div>

        <div id="code-ai-messages" style="flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px">
          <div class="empty-state" style="padding:20px 0">
            <p style="font-size:12px">Ask me to write, explain, or fix n8n code. Or switch to <strong>Workflow Builder</strong> (top left) to describe a whole workflow and generate its JSON to import.</p>
            <p style="font-size:11px;color:var(--text-dim);margin-top:8px;line-height:1.5">
              Tip: add <a href="https://github.com/czlonkowski/n8n-mcp" target="_blank" rel="noopener" style="color:var(--accent)">n8n-mcp</a> (by czlonkowski)
              in Settings under MCP Servers to give this assistant deep n8n node knowledge plus workflow validation and create/update tools.
            </p>
          </div>
        </div>

        <!-- Quick actions -->
        <div id="code-quick-actions" style="padding:8px;border-top:1px solid var(--border-dim);display:flex;flex-wrap:wrap;gap:4px">
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="window.__codeAsk('Explain this code')">Explain</button>
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="window.__codeAsk('Find and fix bugs in this code')">Fix Bugs</button>
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="window.__codeAsk('Optimize this code')">Optimize</button>
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="window.__codeAsk('Add error handling to this code')">Add Error Handling</button>
          <button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="window.__codeAsk('Convert this to use $input.all() properly')">Fix n8n Syntax</button>
        </div>

        <!-- Input -->
        <div style="padding:8px;border-top:1px solid var(--border-dim)">
          <form id="code-ai-form" style="display:flex;gap:4px">
            <input type="text" id="code-ai-input" placeholder="Ask about this code..." style="flex:1;padding:6px 10px;font-size:12px;margin:0">
            <button type="submit" class="btn btn-sm btn-primary" style="padding:6px 10px">Ask</button>
          </form>
        </div>
      </div>
    </div>
  `;

  loadMonaco();
  setupHandlers();
  initProviderPicker();
  initFallbackPicker();
  initTestConnection();
}

// ── Test connection (right-panel) ───────────────────────────────────────────

const PROVIDER_KEY_CONVENTIONS = {
  anthropic: 'ANTHROPIC_KEY',
  openai: 'OPEN_AI_KEY',
  openrouter: 'OPEN_ROUTER_KEY',
};

async function runTestConnection(provider, model, btn, resultEl) {
  if (!provider) {
    resultEl.innerHTML = `<span style="color:var(--warning, #fbbf24)">Pick a provider first</span>`;
    return;
  }
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = 'Testing...';
  resultEl.textContent = '';
  try {
    const body = { provider, model };
    if (provider !== 'ollama') {
      const convention = PROVIDER_KEY_CONVENTIONS[provider];
      if (convention) body.api_key = `$${convention}`;
    }
    const r = await post('/api/admin/assistant/test', body);
    if (r.ok) {
      const label = r.model || model || 'connected';
      resultEl.innerHTML = `<span style="color:var(--success, #34d399)">\u2713 Connected (${esc(label)})</span>`;
    } else {
      const msg = (r.error || 'failed').toString().slice(0, 140);
      resultEl.innerHTML = `<span style="color:var(--error, #ff6d5a)">\u2717 ${esc(msg)}</span>`;
    }
  } catch (e) {
    const msg = (e.message || 'request failed').toString().slice(0, 140);
    resultEl.innerHTML = `<span style="color:var(--error, #ff6d5a)">\u2717 ${esc(msg)}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
    setTimeout(() => { resultEl.textContent = ''; }, 10000);
  }
}

function initTestConnection() {
  const btn = document.getElementById('codelab-test-btn');
  const resultEl = document.getElementById('codelab-test-result');
  if (btn && resultEl) {
    btn.addEventListener('click', () => {
      const provider = document.getElementById('codelab-provider')?.value || '';
      const model = document.getElementById('codelab-model')?.value || '';
      runTestConnection(provider, model, btn, resultEl);
    });
  }

  const fbBtn = document.getElementById('codelab-fallback-test-btn');
  const fbResultEl = document.getElementById('codelab-fallback-test-result');
  if (fbBtn && fbResultEl) {
    fbBtn.addEventListener('click', () => {
      const provider = document.getElementById('codelab-fallback-provider')?.value || '';
      const model = document.getElementById('codelab-fallback-model')?.value || '';
      runTestConnection(provider, model, fbBtn, fbResultEl);
    });
  }
}

function formatAgo(ts) {
  if (!ts) return '';
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  return `${Math.floor(diffSec / 3600)}h ago`;
}

function updateCodelabModelSourceHint(source, cachedAt) {
  const el = document.getElementById('codelab-model-source-hint');
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

// ── Provider + Model picker (session-scoped override) ──────────────────────

const OVERRIDE_STORAGE_KEY = 'ageniusdesk:codelab_override';
const FALLBACK_STORAGE_KEY = 'ageniusdesk:codelab_fallback';

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

function getCurrentOverride() {
  const provSel = document.getElementById('codelab-provider');
  const modelSel = document.getElementById('codelab-model');
  const provider = provSel?.value || '';
  const model = modelSel?.value || '';
  if (!provider && !model) return null;
  return { provider, model };
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

function getCurrentFallback() {
  const provSel = document.getElementById('codelab-fallback-provider');
  const modelSel = document.getElementById('codelab-fallback-model');
  const provider = provSel?.value || '';
  if (!provider) return null;
  const model = modelSel?.value || '';
  return { provider, model };
}

async function populateFallbackModels(provider, preferredModel) {
  const modelSel = document.getElementById('codelab-fallback-model');
  if (!modelSel) return;
  if (!provider) {
    modelSel.innerHTML = '<option value="">(choose provider first)</option>';
    return;
  }
  try {
    const data = await get(`/api/assistant/models?provider=${encodeURIComponent(provider)}`);
    const models = data.models || [];
    if (!models.length) {
      modelSel.innerHTML = '<option value="">No models</option>';
      return;
    }
    modelSel.innerHTML = models.map(m =>
      `<option value="${m.id}">${m.name}${m.provider ? ` (${m.provider})` : ''}</option>`
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

async function initFallbackPicker() {
  const toggleBtn = document.getElementById('codelab-fallback-toggle');
  const row = document.getElementById('codelab-fallback-row');
  const provSel = document.getElementById('codelab-fallback-provider');
  const modelSel = document.getElementById('codelab-fallback-model');
  if (!toggleBtn || !row || !provSel || !modelSel) return;

  const saved = readFallback();
  if (saved?.provider) {
    row.style.display = 'flex';
    toggleBtn.textContent = '\u2212 Fallback';
    provSel.value = saved.provider;
    await populateFallbackModels(saved.provider, saved.model || '');
  }

  toggleBtn.addEventListener('click', () => {
    const visible = row.style.display !== 'none';
    row.style.display = visible ? 'none' : 'flex';
    toggleBtn.textContent = visible ? '+ Fallback' : '\u2212 Fallback';
  });

  provSel.addEventListener('change', async () => {
    const newProvider = provSel.value;
    if (!newProvider) {
      modelSel.innerHTML = '<option value="">(choose provider first)</option>';
      writeFallback('', '');
      return;
    }
    // Reset before refetching so a stale model id never leaks between providers.
    modelSel.value = '';
    modelSel.innerHTML = '<option value="">Loading...</option>';
    await populateFallbackModels(newProvider, '');
    writeFallback(newProvider, modelSel.value || '');
  });

  modelSel.addEventListener('change', () => {
    writeFallback(provSel.value, modelSel.value || '');
  });
}

function showOverrideWarn(msg) {
  const el = document.getElementById('codelab-override-warn');
  if (!el) return;
  el.textContent = msg || '';
  el.style.display = msg ? 'block' : 'none';
}

// Amber inline notice for stale saved models. Same pattern as settings.js and
// assistant.js (auto-dismiss 30s, cleaned up when the view re-renders).
let _codelabStaleTimer = null;
function dismissCodelabStaleNotice() {
  if (_codelabStaleTimer) { clearTimeout(_codelabStaleTimer); _codelabStaleTimer = null; }
  const n = document.getElementById('codelab-stale-model-notice');
  if (n) n.remove();
}
function showCodelabStaleNotice(oldModel, provider, newName) {
  dismissCodelabStaleNotice();
  const warn = document.getElementById('codelab-override-warn');
  if (!warn) return;
  const n = document.createElement('div');
  n.id = 'codelab-stale-model-notice';
  n.style.cssText = [
    'background:#fbbf2422',
    'border:1px solid #fbbf2488',
    'color:#b45309',
    'padding:6px 10px',
    'font-size:10px',
    'line-height:1.4',
    'border-radius:4px',
    'margin-top:6px',
  ].join(';');
  n.innerHTML = `&#9888; Saved model &lsquo;${esc(oldModel)}&rsquo; is not available for ${esc(provider)}. Updated to &lsquo;${esc(newName)}&rsquo;. Save it in <a href="#" id="codelab-stale-goto" style="color:#b45309;text-decoration:underline">AI Settings</a>.`;
  warn.parentNode?.insertBefore(n, warn.nextSibling);
  document.getElementById('codelab-stale-goto')?.addEventListener('click', (e) => {
    e.preventDefault();
    if (window.__goSettings) window.__goSettings('assistant');
  });
  _codelabStaleTimer = setTimeout(dismissCodelabStaleNotice, 30000);
}

async function initProviderPicker() {
  const provSel = document.getElementById('codelab-provider');
  const modelSel = document.getElementById('codelab-model');
  if (!provSel || !modelSel) return;

  let defaultProvider = 'openrouter';
  let defaultModel = '';
  try {
    const cfg = await get('/api/assistant/config');
    // Seed from the Code Lab job (Settings > Models), falling back to the
    // legacy global provider/model for older configs.
    const sd = (cfg.jobs || {}).codelab || {};
    defaultProvider = sd.provider || cfg.provider || 'openrouter';
    defaultModel = sd.model || cfg.model || '';
  } catch { /* use fallbacks */ }

  const saved = readOverride();
  const startProvider = saved?.provider || defaultProvider;
  provSel.value = startProvider;

  await populateModels(startProvider, saved?.model || defaultModel);

  // Auto-repair notice on mount when persistent config has an invalid model.
  if (!saved && defaultModel) {
    const inList = !!modelSel.querySelector(`option[value="${CSS.escape(defaultModel)}"]`);
    if (!inList && modelSel.options.length && modelSel.options[0].value) {
      const newModel = modelSel.options[0].value;
      const newName = modelSel.options[0].textContent || newModel;
      showCodelabStaleNotice(defaultModel, defaultProvider, newName);
    }
  }

  provSel.addEventListener('change', async () => {
    showOverrideWarn('');
    const newProvider = provSel.value;
    // Reset the model select before repopulating so no stale value from the
    // previous provider survives the async refetch.
    modelSel.value = '';
    modelSel.innerHTML = '<option value="">Loading...</option>';
    await populateModels(newProvider, '');
    writeOverride(newProvider, modelSel.value || '');
  });

  modelSel.addEventListener('change', () => {
    writeOverride(provSel.value, modelSel.value || '');
  });

  // Live-update when the Code Lab area default is saved in Settings. Registered
  // once at module scope; a no-op when the Code Lab picker isn't mounted.
  if (!_areaDefaultListenerBound) {
    window.addEventListener('agd:area-defaults-saved', (e) => applyAreaDefaultLive(e.detail || {}));
    _areaDefaultListenerBound = true;
  }
}

let _areaDefaultListenerBound = false;

async function applyAreaDefaultLive(detail) {
  const provSel = document.getElementById('codelab-provider');
  const modelSel = document.getElementById('codelab-model');
  if (!provSel || !modelSel) return; // Code Lab not currently mounted.
  // A saved default supersedes any on-the-fly session pick.
  try { sessionStorage.removeItem(OVERRIDE_STORAGE_KEY); } catch { /* ignore */ }
  showOverrideWarn('');
  let provider = '';
  let model = '';
  const sd = detail && detail.codelab;
  if (sd && sd.provider && sd.model) {
    provider = sd.provider;
    model = sd.model;
  } else {
    // Code Lab set to "Use global default" — follow the primary provider.
    try {
      const cfg = await get('/api/assistant/config');
      provider = cfg.provider || 'openrouter';
      model = cfg.model || '';
    } catch { provider = 'openrouter'; }
  }
  provSel.value = provider;
  await populateModels(provider, model);
}

async function populateModels(provider, preferredModel) {
  const modelSel = document.getElementById('codelab-model');
  if (!modelSel) return;
  try {
    const data = await get(`/api/assistant/models?provider=${encodeURIComponent(provider)}`);
    const models = data.models || [];
    updateCodelabModelSourceHint(data.source, data.cached_at);
    if (!models.length) {
      modelSel.innerHTML = '<option value="">No models</option>';
      return;
    }
    modelSel.innerHTML = models.map(m =>
      `<option value="${m.id}">${m.name}${m.provider ? ` (${m.provider})` : ''}</option>`
    ).join('');
    if (preferredModel && modelSel.querySelector(`option[value="${preferredModel}"]`)) {
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
    const provSel = document.getElementById('codelab-provider');
    if (provSel) provSel.value = defProv;
    await populateModels(defProv, defModel);
  } catch { /* best-effort */ }
}

// ── Monaco Editor ───────────────────────────────────────────────────────────

function loadMonaco() {
  if (monacoLoaded) {
    initEditor();
    return;
  }

  // Load Monaco from CDN
  const script = document.createElement('script');
  script.src = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs/loader.js';
  script.onload = () => {
    window.require.config({
      paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs' }
    });
    window.require(['vs/editor/editor.main'], () => {
      monacoLoaded = true;
      defineN8nTheme();
      registerN8nCompletions();
      initEditor();
    });
  };
  document.head.appendChild(script);
}

function defineN8nTheme() {
  monaco.editor.defineTheme('n8n-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '555568', fontStyle: 'italic' },
      { token: 'keyword', foreground: 'ff6d5a' },
      { token: 'string', foreground: '34d399' },
      { token: 'number', foreground: 'fbbf24' },
      { token: 'type', foreground: '60a5fa' },
    ],
    colors: {
      'editor.background': '#0e0e16',
      'editor.foreground': '#e2e2ec',
      'editor.lineHighlightBackground': '#ffffff06',
      'editor.selectionBackground': '#ff6d5a30',
      'editorCursor.foreground': '#ff6d5a',
      'editorLineNumber.foreground': '#555568',
      'editorLineNumber.activeForeground': '#8888a0',
      'editor.inactiveSelectionBackground': '#ffffff08',
    },
  });
}

function registerN8nCompletions() {
  monaco.languages.registerCompletionItemProvider('javascript', {
    provideCompletionItems: (model, position) => {
      const suggestions = [
        // n8n globals
        { label: '$input', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$input', detail: 'Input data from previous node' },
        { label: '$input.first()', kind: monaco.languages.CompletionItemKind.Method, insertText: '$input.first()', detail: 'Get first input item' },
        { label: '$input.last()', kind: monaco.languages.CompletionItemKind.Method, insertText: '$input.last()', detail: 'Get last input item' },
        { label: '$input.all()', kind: monaco.languages.CompletionItemKind.Method, insertText: '$input.all()', detail: 'Get all input items' },
        { label: '$input.item', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$input.item', detail: 'Current item (Run Once for Each Item mode)' },
        { label: '$json', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$json', detail: 'JSON data of current item' },
        { label: '$binary', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$binary', detail: 'Binary data of current item' },
        { label: '$node', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$node["${1:nodeName}"].json', detail: 'Access output of a specific node', insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet },
        { label: '$workflow', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$workflow', detail: 'Workflow metadata (id, name, active)' },
        { label: '$execution', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$execution', detail: 'Execution metadata (id, mode, resumeUrl)' },
        { label: '$env', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$env["${1:VAR_NAME}"]', detail: 'Environment variable', insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet },
        { label: '$now', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$now', detail: 'Current DateTime (Luxon)' },
        { label: '$today', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$today', detail: 'Start of today (Luxon)' },
        { label: '$runIndex', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$runIndex', detail: 'Index of current run (0-based)' },
        { label: '$itemIndex', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$itemIndex', detail: 'Index of current item' },
        { label: '$prevNode', kind: monaco.languages.CompletionItemKind.Variable, insertText: '$prevNode', detail: 'Previous node metadata' },
        // Common patterns
        { label: 'return items', kind: monaco.languages.CompletionItemKind.Snippet, insertText: 'return $input.all().map(item => {\n  item.json.${1:newField} = ${2:value};\n  return item;\n});', detail: 'Transform all items', insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet },
        { label: 'return filtered', kind: monaco.languages.CompletionItemKind.Snippet, insertText: 'return $input.all().filter(item => {\n  return item.json.${1:field} ${2:=== true};\n});', detail: 'Filter items', insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet },
        { label: 'fetch api', kind: monaco.languages.CompletionItemKind.Snippet, insertText: 'const response = await fetch("${1:https://api.example.com/data}", {\n  method: "${2:GET}",\n  headers: { "Content-Type": "application/json" },\n});\nconst data = await response.json();\nreturn [{ json: data }];', detail: 'HTTP fetch pattern', insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet },
      ];
      return { suggestions };
    },
  });
}

function initEditor() {
  const container = document.getElementById('monaco-container');
  if (!container) return;
  // Dispose stale editor instance from a previous navigation
  if (editor) { editor.dispose(); editor = null; }

  editor = monaco.editor.create(container, {
    value: getTemplate('blank'),
    language: 'javascript',
    theme: 'n8n-dark',
    fontSize: 13,
    fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
    minimap: { enabled: false },
    lineNumbers: 'on',
    renderLineHighlight: 'line',
    scrollBeyondLastLine: false,
    automaticLayout: true,
    tabSize: 2,
    wordWrap: 'on',
    padding: { top: 12, bottom: 12 },
    suggestOnTriggerCharacters: true,
    quickSuggestions: true,
  });
}

// ── Templates ───────────────────────────────────────────────────────────────

function getTemplate(name) {
  const templates = {
    blank: '// n8n Code Node\n// Access input with $input.all(), $input.first(), $json\n\nreturn $input.all();\n',
    transform: `// Transform Items — modify each item's data
return $input.all().map(item => {
  // Add or modify fields
  item.json.processed = true;
  item.json.timestamp = new Date().toISOString();

  return item;
});`,
    filter: `// Filter Items — keep only matching items
return $input.all().filter(item => {
  // Keep items where condition is true
  return item.json.status === 'active';
});`,
    http: `// HTTP Request — fetch data from an API
const response = await fetch("https://api.example.com/data", {
  method: "GET",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer " + $env["API_KEY"],
  },
});

const data = await response.json();
return [{ json: data }];`,
    aggregate: `// Aggregate Data — combine all items into one
const items = $input.all();

const summary = {
  total: items.length,
  values: items.map(i => i.json.value),
  sum: items.reduce((acc, i) => acc + (i.json.value || 0), 0),
};

return [{ json: summary }];`,
    split: `// Split into Batches — process items in groups
const items = $input.all();
const batchSize = 10;
const batches = [];

for (let i = 0; i < items.length; i += batchSize) {
  batches.push({
    json: {
      batch: Math.floor(i / batchSize) + 1,
      items: items.slice(i, i + batchSize).map(item => item.json),
    }
  });
}

return batches;`,
    'webhook-response': `// Webhook Response — format data for webhook output
const input = $input.first().json;

return [{
  json: {
    success: true,
    message: "Processed successfully",
    data: input,
    timestamp: new Date().toISOString(),
  }
}];`,
  };
  return templates[name] || templates.blank;
}

// ── Handlers ────────────────────────────────────────────────────────────────

function setupHandlers() {
  document.getElementById('code-template').addEventListener('change', (e) => {
    if (editor) editor.setValue(getTemplate(e.target.value));
  });

  document.getElementById('code-lang').addEventListener('change', (e) => {
    if (editor) monaco.editor.setModelLanguage(editor.getModel(), e.target.value);
  });

  document.getElementById('mode-code')?.addEventListener('click', () => applyMode('code'));
  document.getElementById('mode-workflow')?.addEventListener('click', () => applyMode('workflow'));

  document.getElementById('code-ai-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = document.getElementById('code-ai-input');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    askAI(msg);
  });
}

// ── Mode: Code Node ↔ Workflow Builder ───────────────────────────────────────

function getWorkflowScaffold() {
  return JSON.stringify({
    name: 'New Workflow',
    nodes: [
      { parameters: {}, id: 'manual-trigger', name: 'Manual Trigger', type: 'n8n-nodes-base.manualTrigger', typeVersion: 1, position: [260, 300] },
    ],
    connections: {},
    settings: { executionOrder: 'v1' },
  }, null, 2);
}

const WORKFLOW_QUICK_PROMPTS = [
  ['Webhook → Slack', 'Build an n8n workflow: receive a webhook POST, then send the payload to a Slack channel.'],
  ['Daily API → Sheet', 'Build an n8n workflow: run on a daily schedule, fetch JSON from an HTTP API, and append rows to Google Sheets.'],
  ['Form → Email', 'Build an n8n workflow: on form submission, send a confirmation email and store the entry.'],
];

const CODE_QUICK_PROMPTS = [
  ['Explain', 'Explain this code'],
  ['Fix Bugs', 'Find and fix bugs in this code'],
  ['Optimize', 'Optimize this code'],
  ['Add Error Handling', 'Add error handling to this code'],
  ['Fix n8n Syntax', 'Convert this to use $input.all() properly'],
];

function renderQuickActions(isWorkflow) {
  const el = document.getElementById('code-quick-actions');
  if (!el) return;
  const set = isWorkflow ? WORKFLOW_QUICK_PROMPTS : CODE_QUICK_PROMPTS;
  el.innerHTML = set.map(([label, prompt]) =>
    `<button class="btn btn-sm btn-ghost" style="font-size:10px" onclick="window.__codeAsk('${prompt.replace(/'/g, "\\'")}')">${label}</button>`
  ).join('');
}

function applyMode(mode) {
  _mode = mode;
  const isWf = mode === 'workflow';
  const tools = document.getElementById('code-mode-tools');
  const sendBtn = document.getElementById('code-send-btn');
  const input = document.getElementById('code-ai-input');
  const mc = document.getElementById('mode-code');
  const mw = document.getElementById('mode-workflow');

  if (tools) tools.style.display = isWf ? 'none' : 'flex';
  if (sendBtn) {
    sendBtn.textContent = isWf ? 'Import to n8n' : 'Send to n8n';
    sendBtn.title = isWf ? 'Import this workflow JSON into n8n' : 'Create a workflow with this code';
  }
  if (input) input.placeholder = isWf ? 'Describe the workflow to build…' : 'Ask about this code...';
  if (mc) mc.classList.toggle('btn-primary', !isWf);
  if (mw) mw.classList.toggle('btn-primary', isWf);

  if (editor) {
    const cur = editor.getValue().trim();
    if (isWf) {
      monaco.editor.setModelLanguage(editor.getModel(), 'json');
      // Swap a leftover code snippet for a scaffold; keep JSON the user is editing.
      if (!cur || !cur.startsWith('{')) editor.setValue(getWorkflowScaffold());
    } else {
      const lang = document.getElementById('code-lang')?.value || 'javascript';
      monaco.editor.setModelLanguage(editor.getModel(), lang);
      // Swap leftover JSON back to a code template.
      if (!cur || cur.startsWith('{')) editor.setValue(getTemplate(document.getElementById('code-template')?.value || 'blank'));
    }
  }
  renderQuickActions(isWf);
}

window.__codeFormat = () => {
  if (editor) editor.getAction('editor.action.formatDocument')?.run();
};

window.__codeCopy = () => {
  if (!editor) return;
  navigator.clipboard.writeText(editor.getValue());
  toast.success('Code copied to clipboard');
};

window.__codeSendToN8n = async () => {
  if (!editor) return;

  // Workflow Builder mode: the editor holds a full workflow JSON; import as-is.
  if (_mode === 'workflow') {
    let wf;
    try {
      wf = JSON.parse(editor.getValue());
    } catch {
      toast.error('Editor does not contain valid workflow JSON.');
      return;
    }
    if (!wf || typeof wf !== 'object' || !Array.isArray(wf.nodes) || !wf.nodes.length) {
      toast.error('Workflow JSON must include a non-empty "nodes" array.');
      return;
    }
    if (!wf.name) wf.name = `Code Lab Workflow (${new Date().toLocaleString()})`;
    if (!wf.connections) wf.connections = {};
    if (!wf.settings) wf.settings = { executionOrder: 'v1' };
    try {
      const result = await post('/api/n8n/import', wf);
      if (result.success) {
        toast.success(`Workflow imported! ID: ${result.workflow_id}`);
        logOutput(`Imported workflow ${result.workflow_id}${result.name ? ` (${result.name})` : ''}`);
      } else {
        toast.error(result.error || 'Import failed');
      }
    } catch (e) {
      toast.error(e.message);
    }
    return;
  }

  const code = editor.getValue();
  try {
    const result = await post('/api/n8n/import', {
      name: `Code Lab — ${new Date().toLocaleString()}`,
      nodes: [
        {
          parameters: {},
          id: 'trigger',
          name: 'Manual Trigger',
          type: 'n8n-nodes-base.manualTrigger',
          typeVersion: 1,
          position: [250, 300],
        },
        {
          parameters: { jsCode: code },
          id: 'code',
          name: 'Code',
          type: 'n8n-nodes-base.code',
          typeVersion: 2,
          position: [470, 300],
        },
      ],
      connections: {
        'Manual Trigger': { main: [[{ node: 'Code', type: 'main', index: 0 }]] },
      },
      settings: { executionOrder: 'v1' },
    });
    if (result.success) {
      toast.success(`Workflow created! ID: ${result.workflow_id}`);
      logOutput(`Sent to n8n as workflow ${result.workflow_id}`);
    } else {
      toast.error(result.error || 'Failed');
    }
  } catch (e) {
    toast.error(e.message);
  }
};

window.__codeAsk = (prompt) => {
  askAI(prompt);
};

// ── AI Chat ─────────────────────────────────────────────────────────────────

const codeAiHistory = [];

async function askAI(question) {
  const messagesEl = document.getElementById('code-ai-messages');
  const empty = messagesEl.querySelector('.empty-state');
  if (empty) empty.remove();

  const code = editor ? editor.getValue() : '';

  // Show user message
  messagesEl.innerHTML += `<div class="chat-msg chat-msg--user"><div class="chat-msg-content">${esc(question)}</div></div>`;

  // Show typing
  const typingId = 't' + Date.now();
  messagesEl.innerHTML += `<div id="${typingId}" class="chat-msg chat-msg--assistant"><div class="chat-msg-content"><div class="spinner" style="margin:0;width:14px;height:14px"></div></div></div>`;
  messagesEl.scrollTop = messagesEl.scrollHeight;

  codeAiHistory.push({ role: 'user', content: question });

  const isWf = _mode === 'workflow';
  try {
    const override = getCurrentOverride();
    const fallback = getCurrentFallback();
    const context = isWf
      ? `## Current workflow JSON in editor\n\`\`\`json\n${code}\n\`\`\`\n\nThe user is building an n8n workflow. Produce a COMPLETE, importable n8n workflow as JSON with top-level keys "name", "nodes" (array), "connections" (object), and "settings". Use real n8n node types and typeVersions and wire the connections correctly. Return ONLY the workflow JSON in a single \`\`\`json code block, with no prose. If MCP tools for n8n node knowledge or workflow validation are available, use them so the node types and parameters are valid.`
      : `## Current Code in Editor\n\`\`\`javascript\n${code}\n\`\`\`\n\nThe user is writing code for an n8n Code node. Help them write, fix, or understand the code. When suggesting code changes, provide the complete updated code so they can copy it directly.`;
    const result = await post('/api/assistant/chat', {
      messages: codeAiHistory,
      context,
      override,
      fallback,
      surface: 'codelab',
    });

    document.getElementById(typingId)?.remove();

    const response = result.response || 'No response';
    codeAiHistory.push({ role: 'assistant', content: response });

    if (result.served_by === 'fallback') {
      const fbMeta = `${result.provider || ''} / ${result.model || ''}`.trim();
      const primaryErr = result.primary_error || 'primary failed';
      messagesEl.innerHTML += `
        <div style="padding:4px 8px;font-size:10px;color:var(--text-dim);border-left:2px solid var(--warning, #fbbf24);background:var(--bg-void);border-radius:4px;margin:2px 0">
          Fallback: <strong>${esc(fbMeta)}</strong>. Primary failed: ${esc(primaryErr)}
        </div>
      `;
    }

    if (isWf) {
      // Workflow mode: extract the JSON and load it straight into the editor.
      const jsonMatch = response.match(/```(?:json)?\n([\s\S]*?)```/);
      if (jsonMatch) {
        const wfJson = jsonMatch[1].trim();
        if (editor) {
          monaco.editor.setModelLanguage(editor.getModel(), 'json');
          editor.setValue(wfJson);
        }
        messagesEl.innerHTML += `<div class="chat-msg chat-msg--assistant"><div class="chat-msg-content">Generated a workflow and loaded it into the editor. Review it, then <strong>Import to n8n</strong>.</div></div>`;
        toast.success('Workflow JSON generated');
      } else {
        // No JSON block: the model likely needs more detail; show its reply.
        messagesEl.innerHTML += `<div class="chat-msg chat-msg--assistant"><div class="chat-msg-content">${renderMd(response)}</div></div>`;
      }
      messagesEl.scrollTop = messagesEl.scrollHeight;
    } else {
      messagesEl.innerHTML += `<div class="chat-msg chat-msg--assistant"><div class="chat-msg-content">${renderMd(response)}</div></div>`;
      messagesEl.scrollTop = messagesEl.scrollHeight;

      // If the response contains a code block, offer to apply it
      const codeMatch = response.match(/```(?:javascript|js|typescript|ts)?\n([\s\S]*?)```/);
      if (codeMatch) {
        messagesEl.innerHTML += `<div style="padding:2px 0"><button class="btn btn-sm btn-primary" style="font-size:10px" onclick="window.__applyCode(\`${btoa(codeMatch[1])}\`)">Apply Code to Editor</button></div>`;
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    }
  } catch (e) {
    document.getElementById(typingId)?.remove();
    const msg = e.message || 'Request failed';
    // If the override's provider isn't configured, surface a fallback warning
    // and revert the picker to the saved config default.
    if (/is not configured/i.test(msg)) {
      await fallbackToConfigDefault(msg);
    }
    messagesEl.innerHTML += `<div class="chat-msg chat-msg--assistant"><div class="chat-msg-content chat-msg-error">${esc(msg)}</div></div>`;
  }
}

window.__applyCode = (b64) => {
  try {
    const code = atob(b64);
    if (editor) editor.setValue(code);
    toast.success('Code applied to editor');
  } catch { toast.error('Failed to apply code'); }
};

// ── Prompt Builder ───────────────────────────────────────────────────────────
// A popup for authoring a structured agent system prompt. Seven sections
// (the agent-prompt-builder convention). Output targets: copy to clipboard or
// push the composed prompt into the editor. Optional one-shot AI draft uses the
// same provider/model configured in the AI Code Assistant panel.

const PB_SECTIONS = [
  { key: 'identity',     label: 'Identity & Role',      hint: 'Who the agent is. e.g. "You are a senior support agent for an online store."' },
  { key: 'objective',    label: 'Objective',            hint: 'The single primary goal the agent works toward.' },
  { key: 'capabilities', label: 'Capabilities & Tools', hint: 'What the agent can do and which tools or data it can reach.' },
  { key: 'workflow',     label: 'Workflow / Process',   hint: 'The step-by-step process the agent should follow.' },
  { key: 'rules',        label: 'Rules & Constraints',  hint: 'Hard rules, guardrails, and things it must never do.' },
  { key: 'output',       label: 'Output Format',        hint: 'How responses should be structured: tone, format, length.' },
  { key: 'examples',     label: 'Examples',             hint: 'Optional few-shot examples of good input and output.' },
];

function pbCompose() {
  const parts = [];
  for (const s of PB_SECTIONS) {
    const ta = document.getElementById(`pb-${s.key}`);
    const val = (ta?.value || '').trim();
    if (val) parts.push(`# ${s.label}\n${val}`);
  }
  return parts.join('\n\n');
}

async function pbDraftWithAI(desc, statusEl, draftBtn) {
  const messages = [{
    role: 'user',
    content:
      `Design a system prompt for an AI agent described as: "${desc}".\n\n` +
      `Return ONLY a JSON object with these string keys: ` +
      PB_SECTIONS.map(s => s.key).join(', ') + `. ` +
      `Each value is the plain-text (markdown allowed) content for that section ` +
      `of an agent system prompt. Keep "examples" short or empty if not useful. ` +
      `Do not wrap the JSON in prose.`,
  }];
  const originalLabel = draftBtn.textContent;
  draftBtn.disabled = true;
  draftBtn.textContent = 'Drafting…';
  statusEl.textContent = '';
  try {
    const result = await post('/api/assistant/chat', {
      messages,
      override: getCurrentOverride(),
      fallback: getCurrentFallback(),
      surface: 'codelab',
    });
    const response = result.response || '';
    const match = response.match(/\{[\s\S]*\}/);
    if (!match) {
      statusEl.innerHTML = `<span style="color:var(--warning, #fbbf24)">Model did not return JSON. Try a more specific description.</span>`;
      return;
    }
    let parsed;
    try { parsed = JSON.parse(match[0]); }
    catch { statusEl.innerHTML = `<span style="color:var(--error, #ff6d5a)">Could not parse the model's JSON.</span>`; return; }
    let filled = 0;
    for (const s of PB_SECTIONS) {
      const v = parsed[s.key];
      if (typeof v === 'string' && v.trim()) {
        const ta = document.getElementById(`pb-${s.key}`);
        if (ta) { ta.value = v.trim(); filled++; }
      }
    }
    statusEl.innerHTML = filled
      ? `<span style="color:var(--success, #34d399)">Drafted ${filled} section${filled === 1 ? '' : 's'}. Review and edit, then copy or push.</span>`
      : `<span style="color:var(--warning, #fbbf24)">No sections were filled.</span>`;
  } catch (e) {
    statusEl.innerHTML = `<span style="color:var(--error, #ff6d5a)">${esc((e.message || 'Request failed').slice(0, 160))}</span>`;
  } finally {
    draftBtn.disabled = false;
    draftBtn.textContent = originalLabel;
  }
}

window.__openPromptBuilder = () => {
  const root = document.createElement('div');
  root.className = 'modal';
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-label', 'Prompt Builder');

  const sectionsHtml = PB_SECTIONS.map(s => `
    <label style="display:block;margin-bottom:10px">
      <span style="display:block;font-size:12px;font-weight:600;margin-bottom:3px">${esc(s.label)}</span>
      <textarea id="pb-${s.key}" rows="3" placeholder="${esc(s.hint)}"
        style="width:100%;padding:7px 9px;font-size:12px;line-height:1.4;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:4px;color:var(--text-primary);resize:vertical;font-family:inherit"></textarea>
    </label>
  `).join('');

  root.innerHTML = `
    <div class="modal-content" tabindex="-1" style="max-width:640px;display:flex;flex-direction:column;max-height:86vh">
      <h2 style="margin-bottom:4px">Prompt Builder</h2>
      <p style="font-size:13px;margin-bottom:14px">Author a structured system prompt for an n8n AI Agent node. Fill the sections, or describe the agent and let the configured model draft them.</p>

      <div style="display:flex;gap:6px;margin-bottom:6px">
        <input type="text" id="pb-desc" placeholder="Describe the agent (e.g. triages inbound support email)…"
          style="flex:1;padding:7px 10px;font-size:12px;margin:0">
        <button type="button" class="btn btn-sm btn-primary" id="pb-draft" style="white-space:nowrap">Draft with AI</button>
      </div>
      <div id="pb-status" style="font-size:11px;line-height:1.3;min-height:14px;margin-bottom:8px"></div>

      <div style="flex:1;overflow-y:auto;padding-right:4px;margin:0 -4px 0 0">
        ${sectionsHtml}
      </div>

      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid var(--border-dim)">
        <button type="button" class="btn btn-sm btn-ghost" data-pb="clear" style="font-size:11px">Clear all</button>
        <div style="display:flex;gap:8px">
          <button type="button" class="btn btn-sm" data-pb="close">Close</button>
          <button type="button" class="btn btn-sm" data-pb="push" title="Load the composed prompt into the editor">Push to Code Lab</button>
          <button type="button" class="btn btn-sm btn-primary" data-pb="copy">Copy prompt</button>
        </div>
      </div>
    </div>
  `;

  const cleanup = () => {
    document.removeEventListener('keydown', onKey);
    root.remove();
  };
  const onKey = (e) => { if (e.key === 'Escape') cleanup(); };

  root.addEventListener('click', (e) => { if (e.target === root) cleanup(); });
  root.querySelector('[data-pb="close"]').addEventListener('click', cleanup);

  root.querySelector('[data-pb="clear"]').addEventListener('click', () => {
    for (const s of PB_SECTIONS) {
      const ta = document.getElementById(`pb-${s.key}`);
      if (ta) ta.value = '';
    }
    const desc = document.getElementById('pb-desc');
    if (desc) desc.value = '';
    const status = document.getElementById('pb-status');
    if (status) status.textContent = '';
  });

  root.querySelector('[data-pb="copy"]').addEventListener('click', () => {
    const prompt = pbCompose();
    if (!prompt) { toast.error('Nothing to copy yet. Fill at least one section.'); return; }
    navigator.clipboard.writeText(prompt);
    toast.success('System prompt copied to clipboard');
  });

  root.querySelector('[data-pb="push"]').addEventListener('click', () => {
    const prompt = pbCompose();
    if (!prompt) { toast.error('Nothing to push yet. Fill at least one section.'); return; }
    if (editor) {
      if (monacoLoaded) monaco.editor.setModelLanguage(editor.getModel(), 'markdown');
      editor.setValue(prompt);
      toast.success('Prompt loaded into the editor');
      cleanup();
    } else {
      toast.error('Editor not ready yet.');
    }
  });

  const draftBtn = root.querySelector('#pb-draft');
  const runDraft = () => {
    const desc = (document.getElementById('pb-desc')?.value || '').trim();
    const statusEl = document.getElementById('pb-status');
    if (!desc) { statusEl.innerHTML = `<span style="color:var(--warning, #fbbf24)">Describe the agent first.</span>`; return; }
    pbDraftWithAI(desc, statusEl, draftBtn);
  };
  draftBtn.addEventListener('click', runDraft);
  root.querySelector('#pb-desc').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runDraft(); }
  });

  document.addEventListener('keydown', onKey);
  document.body.appendChild(root);
  setTimeout(() => root.querySelector('#pb-desc')?.focus(), 0);
};

function logOutput(text) {
  const el = document.getElementById('code-output');
  if (el) el.textContent += text + '\n';
}

function renderMd(text) {
  const codeBlocks = [];
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    codeBlocks.push(`<pre style="background:var(--bg-void);padding:8px;border-radius:4px;font-size:11px;overflow-x:auto;margin:4px 0"><code>${code.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</code></pre>`);
    return `\x00CB${codeBlocks.length - 1}\x00`;
  });

  const lines = text.split('\n');
  let html = '', inUl = false, inOl = false;

  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith('### '))     { cl(); html += `<strong style="display:block;margin:8px 0 2px">${il(t.slice(4))}</strong>`; continue; }
    if (t.startsWith('## '))      { cl(); html += `<strong style="display:block;margin:8px 0 2px">${il(t.slice(3))}</strong>`; continue; }
    if (/^[-*] /.test(t))         { if (!inUl) { cl(); html += '<ul style="padding-left:16px;margin:2px 0">'; inUl = true; } html += `<li>${il(t.replace(/^[-*] /,''))}</li>`; continue; }
    if (/^\d+\. /.test(t))        { if (!inOl) { cl(); html += '<ol style="padding-left:16px;margin:2px 0">'; inOl = true; } html += `<li>${il(t.replace(/^\d+\. /,''))}</li>`; continue; }
    if (!t) { cl(); html += '<br>'; continue; }
    cl(); html += il(t) + '<br>';
  }
  cl();
  html = html.replace(/\x00CB(\d+)\x00/g, (_, i) => codeBlocks[parseInt(i)]);
  return html;

  function cl() { if (inUl) { html += '</ul>'; inUl = false; } if (inOl) { html += '</ol>'; inOl = false; } }
  function il(s) {
    return s
      .replace(/`([^`]+)`/g, '<code style="padding:1px 3px;background:var(--bg-void);border-radius:2px;font-size:11px;font-family:var(--font-mono)">$1</code>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>');
  }
}

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }
