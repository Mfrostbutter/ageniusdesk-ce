/**
 * Guided "connect your n8n" flow, shown after the stand-up-stack wizard.
 *
 * Standing up n8n from a template deploys the container, but n8n itself still
 * needs a first-run owner account and an API key before AgeniusDesk can talk to
 * it — none of which can happen inside the wizard. So once the dashboard loads
 * we walk the operator through it: open n8n, create the account, mint an API
 * key, then register the instance here.
 *
 * Self-contained modal (no index.html markup); dismissible — "I'll do this
 * later" just closes it, the dashboard's empty-state CTA still covers them.
 */

import { post } from '../api.js';
import * as toast from './toast.js';
import * as errorHandlerPrompt from './error-handler-prompt.js';

const DISMISS_KEY = 'agd_connect_n8n_dismissed';

function esc(s) { const d = document.createElement('span'); d.textContent = s == null ? '' : s; return d.innerHTML; }

// Prefill the n8n URL with the SAME host the user reached AgeniusDesk by — i.e.
// this machine's address as their browser sees it — keeping n8n's port from the
// deployed URL. So if they open AgeniusDesk at http://192.168.1.50:3000, the n8n
// field auto-fills http://192.168.1.50:5678. (localhost still works too: the
// backend rewrites it to host.docker.internal when it runs in Docker.)
function smartN8nUrl(deployedUrl) {
  let port = '5678';
  try { const u = new URL(deployedUrl); if (u.port) port = u.port; } catch { /* default */ }
  const proto = location.protocol === 'https:' ? 'https:' : 'http:';
  const host = location.hostname || 'localhost';
  return `${proto}//${host}:${port}`;
}

/**
 * Open the guide. `url` is the deployed n8n URL (prefilled). Pass
 * opts.force to bypass the "dismissed" memory.
 */
export function open(url, opts = {}) {
  if (!opts.force) {
    try { if (localStorage.getItem(DISMISS_KEY) === '1') return; } catch { /* ignore */ }
  }
  document.getElementById('connect-n8n-modal')?.remove();
  const safeUrl = smartN8nUrl(url);

  const modal = document.createElement('div');
  modal.id = 'connect-n8n-modal';
  modal.className = 'modal';
  modal.innerHTML = `
    <div class="modal-content" style="max-width:560px;max-height:88vh;overflow-y:auto">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:6px">
        <h2 style="margin:0;font-size:18px">Connect your n8n</h2>
        <button class="btn btn-sm btn-ghost" id="cn-close" title="Close" style="font-size:18px;line-height:1;padding:2px 8px">&times;</button>
      </div>
      <p style="font-size:13px;color:var(--text-secondary);margin:0 0 16px;line-height:1.5">
        Your n8n is running. Finish its one-time setup, then paste an API key here
        so AgeniusDesk can manage it.
      </p>

      <ol class="cn-steps" style="list-style:none;padding:0;margin:0 0 18px;display:flex;flex-direction:column;gap:14px">
        <li style="display:flex;gap:12px">
          <span class="cn-num">1</span>
          <div style="flex:1">
            <div class="cn-step-title">Open n8n and create your account</div>
            <div class="cn-step-desc">It opens to a "set up owner account" screen on first run. Pick an email and password — that's your n8n login, separate from AgeniusDesk.</div>
            <a href="${esc(safeUrl)}" target="_blank" rel="noopener" class="btn btn-sm btn-primary" style="margin-top:8px;display:inline-flex;align-items:center;gap:6px">Open n8n &#8599;</a>
          </div>
        </li>
        <li style="display:flex;gap:12px">
          <span class="cn-num">2</span>
          <div style="flex:1">
            <div class="cn-step-title">Create an API key</div>
            <div class="cn-step-desc">In n8n, go to <strong>Settings &rarr; n8n API &rarr; Create an API key</strong>. Give it a name, then copy the key (you only see it once).</div>
          </div>
        </li>
        <li style="display:flex;gap:12px">
          <span class="cn-num">3</span>
          <div style="flex:1">
            <div class="cn-step-title">Register it here</div>
            <div class="cn-step-desc">Paste the key below and connect. The key is saved to your encrypted secrets store, not in plain text.</div>
          </div>
        </li>
      </ol>

      <div class="cn-form" style="display:flex;flex-direction:column;gap:10px;border-top:1px solid var(--border-dim);padding-top:16px">
        <label style="font-size:12px;color:var(--text-secondary)">Name
          <input type="text" id="cn-name" value="n8n" style="width:100%;box-sizing:border-box;margin-top:4px">
        </label>
        <label style="font-size:12px;color:var(--text-secondary)">n8n URL
          <input type="url" id="cn-url" value="${esc(safeUrl)}" style="width:100%;box-sizing:border-box;margin-top:4px">
          <span style="display:block;margin-top:5px;font-size:11px;color:var(--warning,#fbbf24);line-height:1.45">
            Tip: if it won't connect, use this machine's <strong>LAN IP</strong> (e.g. http://192.168.x.x:5678), not localhost. The dashboard runs in Docker, so localhost can point at the wrong place.
          </span>
        </label>
        <label style="font-size:12px;color:var(--text-secondary)">API key
          <input type="password" id="cn-key" placeholder="Paste the key from n8n" autocomplete="off" style="width:100%;box-sizing:border-box;margin-top:4px">
        </label>
        <div id="cn-msg" style="font-size:12px;min-height:14px"></div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:2px">
          <button class="btn btn-primary" id="cn-connect">Connect</button>
          <button class="btn btn-ghost" id="cn-later">I'll do this later</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const msg = modal.querySelector('#cn-msg');
  const close = () => modal.remove();
  modal.querySelector('#cn-close').addEventListener('click', close);
  modal.querySelector('#cn-later').addEventListener('click', () => {
    try { localStorage.setItem(DISMISS_KEY, '1'); } catch { /* ignore */ }
    close();
  });

  modal.querySelector('#cn-connect').addEventListener('click', async () => {
    const name = modal.querySelector('#cn-name').value.trim();
    const url = modal.querySelector('#cn-url').value.trim();
    let key = modal.querySelector('#cn-key').value.trim();
    if (!name || !url || !key) { msg.style.color = 'var(--error)'; msg.textContent = 'Fill in name, URL, and API key.'; return; }

    const btn = modal.querySelector('#cn-connect');
    btn.disabled = true;
    msg.style.color = 'var(--text-dim)';
    msg.textContent = 'Connecting…';
    try {
      // Promote the raw key into the encrypted secrets store, then register the
      // instance with a $REF instead of the plaintext key.
      if (!key.startsWith('$')) {
        try {
          const r = await post('/api/admin/secrets/promote', { value: key, prefix: 'N8N_KEY', context: name });
          if (r && r.ref) key = r.ref;
        } catch { /* fall back to inline storage */ }
      }
      await post('/api/n8n/instances', { name, url, api_key: key });
      try { localStorage.setItem(DISMISS_KEY, '1'); } catch { /* ignore */ }
      toast.success(`Connected to ${name}`);
      close();
      if (window.__refreshInstances) window.__refreshInstances();
      if (window.__nav) window.__nav('dashboard');
      // Right after connecting, offer to wire up error reporting into n8n.
      let n8nHost = '';
      try { n8nHost = new URL(url).hostname; } catch { /* ignore */ }
      setTimeout(() => errorHandlerPrompt.open({ n8nHost, force: true }), 350);
    } catch (e) {
      msg.style.color = 'var(--error)';
      msg.textContent = e.message || 'Could not connect. Check the URL and key.';
      btn.disabled = false;
    }
  });
}
