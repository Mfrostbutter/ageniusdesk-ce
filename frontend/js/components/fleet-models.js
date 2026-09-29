/**
 * Agent Fleet models card: the provider, key and per-agent model the managed
 * agents run on. Mounted on the Models / AI Settings page in both editions.
 *
 *   renderFleetModels(container, { apiBase, fetchModels, secretsPath })
 *     apiBase:     '/api/agent-fleet' (CE) or '/api/langgraph' (Enterprise)
 *     fetchModels: (provider, keyRef) => Promise<[{id, name}]>
 */

import { get, put } from '../api.js';
import { attr, esc } from '../lib/html.js';
import * as toast from '../components/toast.js';

const CUSTOM = '__custom__';

export async function renderFleetModels(container, opts) {
  if (!container) return;
  const { apiBase, fetchModels, secretsPath = '/api/admin/secrets/refs' } = opts;
  container.innerHTML = '<div class="card" style="margin-bottom:16px"><div class="spinner"></div></div>';

  let s;
  try { s = await get(`${apiBase}/settings`); }
  catch (e) {
    container.innerHTML = `<div class="card" style="margin-bottom:16px"><div class="card-header"><span class="card-title">Agent Fleet</span></div>
      <p style="font-size:12px;color:var(--text-dim)">Fleet settings unavailable: ${esc(e.message)}</p></div>`;
    return;
  }
  let refs = [];
  try { refs = (await get(secretsPath)).refs || []; } catch { /* offline */ }

  const providerLabel = (id) => (s.providers.find(p => p.id === id) || { label: id }).label;
  const followLabel = () => {
    const src = s.provider_source === 'settings' ? '' : s.provider_source;
    const via = src === 'env' ? 'from LANGGRAPH_PROVIDER' : src === 'assistant' ? 'the AI assistant’s provider' : 'the default';
    const eff = s.provider ? (s.assistant_provider && s.providers.some(p => p.id === s.assistant_provider) ? s.assistant_provider : 'anthropic') : s.provider_effective;
    return `Follow the AI assistant (${providerLabel(eff)}${!s.provider && src && src !== 'assistant' ? `, ${via}` : ''})`;
  };

  container.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header"><span class="card-title">Agent Fleet</span>
        <span id="fleet-key-state" style="margin-left:auto;font-size:11px"></span></div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px">
        The provider and models the managed agents run on. Leave a model on its default to keep the
        agent's own choice. On OpenRouter, vendor ids are translated automatically
        (<code>claude-haiku-4-5</code> becomes <code>anthropic/claude-haiku-4.5</code>).
      </p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px">
        <label>Provider<select id="fleet-provider">
          <option value="">${esc(followLabel())}</option>
          ${s.providers.map(p => `<option value="${attr(p.id)}">${esc(p.label)}</option>`).join('')}
        </select></label>
        <label>API key<select id="fleet-keyref">
          <option value="">Provider default key</option>
          ${refs.map(r => `<option value="${attr(r.ref)}">${esc(r.ref)}${r.hint ? ` — ${esc(r.hint)}` : ''}</option>`).join('')}
          ${s.api_key_ref && !refs.some(r => r.ref === s.api_key_ref) ? `<option value="${attr(s.api_key_ref)}">${esc(s.api_key_ref)} (not found)</option>` : ''}
        </select></label>
      </div>
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr style="color:var(--text-dim);font-size:11px;text-align:left">
            <th style="padding:6px 8px;border-bottom:1px solid var(--border-dim)">Agent</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border-dim)">Default</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border-dim);width:45%">Model</th>
          </tr></thead>
          <tbody id="fleet-agents">
            ${s.agents.map(a => `
              <tr data-agent="${attr(a.id)}">
                <td style="padding:8px;border-bottom:1px solid var(--border-dim)">
                  <div style="font-weight:500">${esc(a.name)}</div>
                  <div style="font-size:11px;color:var(--text-dim)">${esc(a.id)}${a.framework && a.framework !== 'langgraph' ? ` · ${esc(a.framework)}` : ''}</div>
                </td>
                <td style="padding:8px;border-bottom:1px solid var(--border-dim);font-family:var(--font-mono);font-size:12px;color:var(--text-secondary)">${esc(a.default_model)}</td>
                <td style="padding:8px;border-bottom:1px solid var(--border-dim)">
                  <select data-model="${attr(a.id)}" style="width:100%"><option value="">Loading…</option></select>
                  <input type="text" data-custom="${attr(a.id)}" placeholder="model id" style="display:none;width:100%;box-sizing:border-box;margin-top:6px">
                  <div data-effective="${attr(a.id)}" style="font-size:11px;color:var(--text-dim);margin-top:4px;font-family:var(--font-mono)"></div>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
      <div style="display:flex;gap:10px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--border-dim)">
        <button class="btn btn-primary btn-sm" id="fleet-save" type="button">Save</button>
        <span id="fleet-save-result" style="font-size:12px"></span>
      </div>
    </div>`;

  const provSel = container.querySelector('#fleet-provider');
  const keySel = container.querySelector('#fleet-keyref');
  provSel.value = s.provider || '';
  keySel.value = s.api_key_ref || '';

  const keyState = container.querySelector('#fleet-key-state');
  const paintKeyState = () => {
    const prov = provSel.value || s.provider_effective;
    keyState.innerHTML = s.key_present
      ? `<span style="color:var(--success, #34d399)">✓ ${esc(providerLabel(prov))} key resolves</span>`
      : `<span style="color:var(--warning, #fbbf24)">⚠ no ${esc(providerLabel(prov))} key: pick one or add it under Secrets</span>`;
  };
  paintKeyState();

  const effectiveProvider = () => provSel.value || (s.assistant_provider && s.providers.some(p => p.id === s.assistant_provider) ? s.assistant_provider : s.provider_effective);

  async function fillModels(provider, keyRef) {
    let models = [];
    try { models = await fetchModels(provider, keyRef); } catch { models = []; }
    for (const a of s.agents) {
      const sel = container.querySelector(`select[data-model="${a.id}"]`);
      const custom = container.querySelector(`input[data-custom="${a.id}"]`);
      const eff = container.querySelector(`div[data-effective="${a.id}"]`);
      if (!sel) continue;
      const saved = a.model || '';
      let html = `<option value="">Default (${esc(a.default_model)})</option>`;
      if (saved && !models.some(m => m.id === saved)) html += `<option value="${attr(saved)}">${esc(saved)} (saved)</option>`;
      html += models.map(m => `<option value="${attr(m.id)}">${esc(m.name || m.id)}</option>`).join('');
      html += `<option value="${CUSTOM}">Custom id…</option>`;
      sel.innerHTML = html;
      sel.value = saved;
      if (sel.value !== saved) { sel.value = CUSTOM; custom.value = saved; custom.style.display = ''; }
      else custom.style.display = 'none';
      eff.textContent = provider === s.provider_effective && a.model_effective && a.model_effective !== (saved || a.default_model)
        ? `sent as ${a.model_effective}` : '';
      sel.onchange = () => {
        custom.style.display = sel.value === CUSTOM ? '' : 'none';
        if (sel.value === CUSTOM) custom.focus();
      };
    }
  }
  await fillModels(effectiveProvider(), keySel.value);

  provSel.addEventListener('change', async () => {
    keySel.value = '';
    await fillModels(effectiveProvider(), '');
  });
  keySel.addEventListener('change', async () => { await fillModels(effectiveProvider(), keySel.value); });

  container.querySelector('#fleet-save').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const res = container.querySelector('#fleet-save-result');
    const models = {};
    for (const a of s.agents) {
      const sel = container.querySelector(`select[data-model="${a.id}"]`);
      const custom = container.querySelector(`input[data-custom="${a.id}"]`);
      models[a.id] = sel.value === CUSTOM ? (custom.value || '').trim() : sel.value;
    }
    btn.disabled = true;
    try {
      s = await put(`${apiBase}/settings`, { provider: provSel.value, api_key_ref: keySel.value, models });
      paintKeyState();
      await fillModels(effectiveProvider(), keySel.value);
      res.textContent = 'Saved'; res.style.color = 'var(--success, #34d399)';
      toast.success('Agent Fleet models saved');
    } catch (err) {
      res.textContent = err.message; res.style.color = 'var(--error)';
    } finally {
      btn.disabled = false;
    }
  });
}
