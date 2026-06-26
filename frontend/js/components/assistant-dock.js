/**
 * Assistant dock — compact chat panel intended to occupy the bottom ~1/3 of
 * a host view (currently the Dashboard). Left column is the message log +
 * input; right column is a narrow provider/model picker.
 *
 * Talks to the same endpoints as the full Assistant view:
 *   GET  /api/assistant/config      — saved provider + model defaults
 *   GET  /api/assistant/models      — available models per provider
 *   POST /api/assistant/chat        — send { messages, context, override }
 *
 * History survives view re-renders via module-level state. Clears when the
 * whole app reloads — that's intentional; the dock is for quick questions,
 * the full Assistant view still exists (unlinked in the sidebar) for
 * anything long-lived.
 */

import { get, post } from '../api.js';
import * as toast from '../components/toast.js';

const PROVIDER_KEY_CONVENTIONS = {
  anthropic: 'ANTHROPIC_KEY',
  openai: 'OPEN_AI_KEY',
  openrouter: 'OPEN_ROUTER_KEY',
};

const PROVIDERS = [
  { id: 'anthropic',  label: 'Anthropic' },
  { id: 'openai',     label: 'OpenAI' },
  { id: 'openrouter', label: 'OpenRouter' },
  { id: 'ollama',     label: 'Ollama' },
];

const FALLBACK_MODELS = {
  anthropic: [
    'claude-opus-4-7',
    'claude-sonnet-4-6',
    'claude-haiku-4-5-20251001',
  ],
  openai: ['gpt-4o', 'gpt-4o-mini', 'o1-mini'],
  openrouter: ['anthropic/claude-sonnet-4.6', 'openai/gpt-4o', 'meta-llama/llama-3.3-70b'],
  ollama: ['llama3.1', 'qwen2.5'],
};

// History is session-scoped (sessionStorage). Provider + model are NOT
// stored locally — they live in the saved config (/api/assistant/config)
// so the dock and the Settings → Models panel are always the same source
// of truth. Changing the picker in the dock persists back to config.
const STATE_KEY = 'ageniusdesk:assistant_dock_history';

let _configChangedHandler = null;

let state = {
  history: [],
  provider: '',  // loaded from /api/assistant/config on mount
  model: '',
};

function loadState() {
  try {
    const raw = sessionStorage.getItem(STATE_KEY);
    if (raw) state.history = JSON.parse(raw) || [];
  } catch { /* ignore */ }
}

function saveState() {
  try {
    sessionStorage.setItem(STATE_KEY, JSON.stringify(state.history));
  } catch { /* ignore */ }
}

function esc(s) {
  return String(s ?? '').replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
}

// Attribute-safe escaping (esc leaves quotes intact, unsafe in value="...").
function attr(s) {
  return String(s ?? '').replace(/[<>&"']/g, c => (
    { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtMarkdown(text) {
  // Minimal markdown: bold + inline code + newlines. Full view has the
  // heavy renderer; dock doesn't need it.
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br>');
}

export function mount(container) {
  loadState();
  container.innerHTML = `
    <div class="card" style="padding:0;margin-bottom:20px;overflow:hidden">
      <div style="display:grid;grid-template-columns:1fr 220px;height:420px">
        <!-- Chat column. min-height:0 is the magic that lets the inner
             overflow:auto actually clip and scroll inside a flex child. -->
        <div style="display:flex;flex-direction:column;border-right:1px solid var(--border-dim);min-height:0">
          <div style="padding:10px 14px;border-bottom:1px solid var(--border-dim);display:flex;justify-content:space-between;align-items:center">
            <span style="font-size:12px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-dim);font-weight:600">Ask the assistant</span>
            <button class="btn btn-sm btn-ghost" id="dock-clear-btn" title="Clear chat history" style="font-size:11px">Clear</button>
          </div>
          <div id="dock-messages" style="flex:1;overflow-y:auto;padding:12px 14px;font-size:13px;line-height:1.5">
            ${renderMessages()}
          </div>
          <div style="display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--border-dim)">
            <input id="dock-input" class="input" placeholder="Ask anything — press Enter to send" style="flex:1;margin:0" autocomplete="off">
            <button class="btn btn-primary" id="dock-send-btn">Send</button>
          </div>
        </div>

        <!-- Right column: model picker -->
        <div style="padding:12px 14px;background:rgba(255,255,255,0.02);display:flex;flex-direction:column;gap:8px;overflow-y:auto">
          <div style="display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-dim);font-weight:600">Model</span>
            <button class="btn btn-sm btn-ghost" id="dock-fallback-toggle" style="font-size:10px;padding:2px 6px">+ Fallback</button>
          </div>
          <!-- Primary card -->
          <div style="display:flex;flex-direction:column;gap:6px;padding:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border-dim);border-radius:6px">
            <div>
              <label style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:4px">Provider</label>
              <select id="dock-provider" class="input" style="width:100%;margin:0;padding:6px 10px;font-size:12px"></select>
            </div>
            <div>
              <label style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:4px">Model</label>
              <select id="dock-model" class="input" style="width:100%;margin:0;padding:6px 10px;font-size:12px"><option>Loading…</option></select>
            </div>
            <button class="btn btn-sm" id="dock-test-btn" style="width:100%;font-size:11px">Test connection</button>
            <div id="dock-test-result" style="font-size:10px;line-height:1.3;min-height:12px;text-align:center"></div>
          </div>
          <!-- Fallback card (hidden until toggled) -->
          <div id="dock-fallback-card" style="display:none;flex-direction:column;gap:6px;padding:8px;background:rgba(255,255,255,0.02);border:1px dashed var(--border-dim);border-radius:6px">
            <div style="font-size:10px;color:var(--text-dim);font-weight:600;text-transform:uppercase;letter-spacing:0.4px">Fallback</div>
            <div>
              <label style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:4px">Provider</label>
              <select id="dock-fallback-provider" class="input" style="width:100%;margin:0;padding:6px 10px;font-size:12px">
                <option value="">(none)</option>
                <option value="anthropic">Anthropic</option>
                <option value="openai">OpenAI</option>
                <option value="openrouter">OpenRouter</option>
                <option value="ollama">Ollama</option>
              </select>
            </div>
            <div>
              <label style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:4px">Model</label>
              <select id="dock-fallback-model" class="input" style="width:100%;margin:0;padding:6px 10px;font-size:12px"><option value="">(choose provider first)</option></select>
            </div>
            <button class="btn btn-sm" id="dock-fallback-test-btn" style="width:100%;font-size:11px">Test connection</button>
            <div id="dock-fallback-test-result" style="font-size:10px;line-height:1.3;min-height:12px;text-align:center"></div>
          </div>
          <div style="margin-top:auto;padding-top:8px;border-top:1px solid var(--border-dim)">
            <button class="btn btn-sm btn-ghost" id="dock-open-full" style="width:100%;font-size:11px">Open full settings →</button>
          </div>
        </div>
      </div>
    </div>
  `;

  wire(container);
  initModelPicker();

  if (_configChangedHandler) {
    window.removeEventListener('agd:config-changed', _configChangedHandler);
  }
  _configChangedHandler = () => { initModelPicker(); };
  window.addEventListener('agd:config-changed', _configChangedHandler);
}

export function cleanup() {
  if (_configChangedHandler) {
    window.removeEventListener('agd:config-changed', _configChangedHandler);
    _configChangedHandler = null;
  }
}

function renderMessages() {
  if (!state.history.length) {
    return `
      <div style="text-align:center;color:var(--text-dim);padding:40px 20px;font-size:12px">
        Dock chat. Uses the same assistant as the full view — just compact and always visible.<br>
        Try: <em>"What errors happened today?"</em> or <em>"Summarize my workflow health"</em>
      </div>
    `;
  }
  return state.history.map(renderMessage).join('');
}

function renderMessage(m) {
  const mine = m.role === 'user';
  const bg = mine ? 'rgba(96,165,250,0.10)' : 'rgba(255,255,255,0.03)';
  const label = mine ? 'You' : (m.served_by || 'Assistant');
  return `
    <div style="margin-bottom:10px">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-dim);margin-bottom:2px">${esc(label)}</div>
      <div style="background:${bg};padding:8px 10px;border-radius:6px">${fmtMarkdown(m.content)}</div>
    </div>
  `;
}

function wire(container) {
  const input = container.querySelector('#dock-input');
  const sendBtn = container.querySelector('#dock-send-btn');
  const clearBtn = container.querySelector('#dock-clear-btn');
  const openFull = container.querySelector('#dock-open-full');

  function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    sendMessage(text);
  }

  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  clearBtn.addEventListener('click', () => {
    state.history = [];
    saveState();
    document.getElementById('dock-messages').innerHTML = renderMessages();
  });

  openFull.addEventListener('click', () => {
    if (window.__goSettings) window.__goSettings('assistant');
  });

  // Test buttons
  async function runTest(providerSel, modelSel, btn, resultEl) {
    const provider = providerSel?.value || '';
    const model = modelSel?.value || '';
    if (!provider) {
      resultEl.innerHTML = `<span style="color:var(--warning,#fbbf24)">Pick a provider first</span>`;
      return;
    }
    btn.disabled = true;
    const orig = btn.textContent;
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
        resultEl.innerHTML = `<span style="color:var(--success,#34d399)">✓ Connected (${esc(r.model || model)})</span>`;
      } else {
        resultEl.innerHTML = `<span style="color:var(--error,#ff6d5a)">✗ ${esc((r.error||'failed').toString().slice(0,120))}</span>`;
      }
    } catch (e) {
      resultEl.innerHTML = `<span style="color:var(--error,#ff6d5a)">✗ ${esc((e.message||'failed').slice(0,120))}</span>`;
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
      setTimeout(() => { resultEl.textContent = ''; }, 10000);
    }
  }

  const testBtn = container.querySelector('#dock-test-btn');
  const testResult = container.querySelector('#dock-test-result');
  if (testBtn) {
    testBtn.addEventListener('click', () => runTest(
      container.querySelector('#dock-provider'),
      container.querySelector('#dock-model'),
      testBtn, testResult
    ));
  }

  const fbTestBtn = container.querySelector('#dock-fallback-test-btn');
  const fbTestResult = container.querySelector('#dock-fallback-test-result');
  if (fbTestBtn) {
    fbTestBtn.addEventListener('click', () => runTest(
      container.querySelector('#dock-fallback-provider'),
      container.querySelector('#dock-fallback-model'),
      fbTestBtn, fbTestResult
    ));
  }

  // Fallback toggle
  const fallbackToggle = container.querySelector('#dock-fallback-toggle');
  const fallbackCard = container.querySelector('#dock-fallback-card');
  if (fallbackToggle && fallbackCard) {
    fallbackToggle.addEventListener('click', () => {
      const visible = fallbackCard.style.display !== 'none';
      fallbackCard.style.display = visible ? 'none' : 'flex';
      fallbackToggle.textContent = visible ? '+ Fallback' : '– Fallback';
    });
  }

  // Fallback provider change loads models
  const fbProvSel = container.querySelector('#dock-fallback-provider');
  const fbModelSel = container.querySelector('#dock-fallback-model');
  if (fbProvSel && fbModelSel) {
    fbProvSel.addEventListener('change', async () => {
      const p = fbProvSel.value;
      if (!p) { fbModelSel.innerHTML = '<option value="">(choose provider first)</option>'; return; }
      fbModelSel.innerHTML = '<option>Loading…</option>';
      try {
        const r = await get(`/api/assistant/models?provider=${encodeURIComponent(p)}`);
        const models = r.models || FALLBACK_MODELS[p] || [];
        fbModelSel.innerHTML = models.map(m => {
          const id = typeof m === 'string' ? m : (m.id || '');
          const label = typeof m === 'string' ? m : (m.label || m.id || '');
          return `<option value="${attr(id)}">${esc(label)}</option>`;
        }).join('');
      } catch {
        fbModelSel.innerHTML = (FALLBACK_MODELS[p] || []).map(m => `<option value="${attr(m)}">${esc(m)}</option>`).join('');
      }
    });
  }
}

async function initModelPicker() {
  const providerSel = document.getElementById('dock-provider');
  const modelSel = document.getElementById('dock-model');
  if (!providerSel || !modelSel) return;

  providerSel.innerHTML = PROVIDERS.map(p =>
    `<option value="${p.id}">${p.label}</option>`).join('');

  // Load saved config — this is the source of truth, shared with
  // Settings → Models. The dock mirrors whatever is saved.
  let cfg = {};
  try {
    cfg = await get('/api/assistant/config');
  } catch { /* proceed with defaults */ }
  state.provider = cfg.provider || 'anthropic';
  state.model = cfg.model || '';
  providerSel.value = state.provider;

  await loadModels(state.provider, state.model);

  providerSel.addEventListener('change', async () => {
    state.provider = providerSel.value;
    state.model = '';
    await loadModels(state.provider, '');
    // Capture the first model now showing in the select before persisting.
    state.model = modelSel.value;
    await persistConfig();
  });
  modelSel.addEventListener('change', async () => {
    state.model = modelSel.value;
    await persistConfig();
  });
}

async function persistConfig() {
  // Mirror the picker back into saved config. This is the same endpoint
  // Settings → Models writes to, so both views stay in sync.
  try {
    // Fetch current config so we don't clobber unrelated fields
    // (system_prompt, ollama_url, fallback, etc.)
    const current = await get('/api/assistant/config');
    const update = {
      ...current,
      provider: state.provider,
      model: state.model,
    };
    // When switching providers, explicitly set the conventional key reference
    // so the saved api_key stays aligned with the selected provider. Without
    // this, changing from (say) OpenAI → Anthropic leaves $OPEN_AI_KEY in the
    // config, and subsequent chats fail with 401 when provider_changed=False in
    // _resolve_override (same provider in override + saved config, old key used).
    const convention = PROVIDER_KEY_CONVENTIONS[state.provider];
    if (convention) update.api_key = `$${convention}`;
    await post('/api/assistant/config', update);
  } catch (e) {
    toast.error(`Could not save model choice: ${e.message}`);
  }
}

async function loadModels(provider, preferred) {
  const modelSel = document.getElementById('dock-model');
  if (!modelSel) return;
  modelSel.innerHTML = '<option>Loading…</option>';
  let models = [];
  try {
    const r = await get(`/api/assistant/models?provider=${encodeURIComponent(provider)}`);
    models = r.models || [];
  } catch {
    models = FALLBACK_MODELS[provider] || [];
  }
  if (!models.length) models = FALLBACK_MODELS[provider] || [];
  modelSel.innerHTML = models.map(m => {
    const id = typeof m === 'string' ? m : (m.id || m.name || '');
    const label = typeof m === 'string' ? m : (m.label || m.id || m.name || '');
    return `<option value="${attr(id)}">${esc(label)}</option>`;
  }).join('');
  if (preferred && models.some(m => (typeof m === 'string' ? m : m.id) === preferred)) {
    modelSel.value = preferred;
  }
}

async function sendMessage(text) {
  const messagesEl = document.getElementById('dock-messages');
  state.history.push({ role: 'user', content: text });
  saveState();
  messagesEl.innerHTML = renderMessages();
  messagesEl.scrollTop = messagesEl.scrollHeight;

  // Typing indicator
  const typingId = 'dock-typing-' + Date.now();
  messagesEl.insertAdjacentHTML('beforeend', `
    <div id="${typingId}" style="margin-bottom:10px">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-dim);margin-bottom:2px">Assistant</div>
      <div style="background:rgba(255,255,255,0.03);padding:8px 10px;border-radius:6px"><div class="spinner" style="margin:0;width:14px;height:14px"></div></div>
    </div>
  `);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  const provider = document.getElementById('dock-provider')?.value || state.provider;
  const model = document.getElementById('dock-model')?.value || state.model;

  try {
    // Strip any non-wire fields (e.g. served_by) before posting — provider
    // APIs (Anthropic in particular) reject unknown keys on message objects
    // with HTTP 400, which the backend bubbles up as a 502.
    const wireMessages = state.history.map(m => ({
      role: m.role,
      content: m.content,
    }));
    const override = provider ? { provider, model } : null;
    if (override && provider !== 'ollama') {
      const convention = PROVIDER_KEY_CONVENTIONS[provider];
      if (convention) override.api_key = `$${convention}`;
    }
    const result = await post('/api/assistant/chat', {
      messages: wireMessages,
      context: '',
      override,
    });
    document.getElementById(typingId)?.remove();
    const response = result.response || 'No response.';
    state.history.push({
      role: 'assistant',
      content: response,
      served_by: result.served_by || '',
    });
    saveState();
    messagesEl.innerHTML = renderMessages();
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } catch (e) {
    document.getElementById(typingId)?.remove();
    toast.error(`Assistant error: ${e.message}`);
    state.history.push({
      role: 'assistant',
      content: `⚠ ${e.message}`,
    });
    messagesEl.innerHTML = renderMessages();
  }
}
