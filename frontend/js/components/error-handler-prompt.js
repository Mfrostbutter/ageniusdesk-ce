/**
 * "Add error reporting" prompt, shown right after a fresh n8n instance is
 * connected during onboarding. Installs the global error-handler workflow into
 * the active n8n so any workflow failure surfaces in AgeniusDesk.
 *
 * Checks first whether a handler is already present: if so it just notes that
 * (never a second copy). Install is also idempotent server-side.
 */

import { get, post } from '../api.js';
import * as toast from './toast.js';

const DISMISS_KEY = 'agd_error_handler_prompted';

function esc(s) { const d = document.createElement('span'); d.textContent = s == null ? '' : s; return d.innerHTML; }

/**
 * Build a dashboard URL that n8n can actually reach. n8n posts errors back to
 * us, so if the browser reached AgeniusDesk at localhost but n8n lives on a LAN
 * host, n8n's container can't reach our localhost — reuse n8n's own host (it is
 * co-located with the dashboard) with our port.
 */
function reachableDashboardUrl(coLocatedHost) {
  const local = new Set(['localhost', '127.0.0.1', '::1']);
  const host = location.hostname;
  if (local.has(host) && coLocatedHost && !local.has(coLocatedHost)) {
    return `${location.protocol}//${coLocatedHost}${location.port ? ':' + location.port : ''}`;
  }
  return location.origin;
}

const N8N_STEP = `One step left in n8n: <strong>Settings &rarr; Workflows &rarr; Error Workflow</strong>, ` +
  `then pick <em>"Global Error Handler &rarr; AgeniusDesk"</em> so it runs on every failure.`;

function shell(bodyHtml) {
  document.getElementById('error-handler-modal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'error-handler-modal';
  modal.className = 'modal';
  modal.innerHTML = `<div class="modal-content" style="max-width:480px">${bodyHtml}</div>`;
  document.body.appendChild(modal);
  return modal;
}

function markDone() { try { localStorage.setItem(DISMISS_KEY, '1'); } catch { /* ignore */ } }

/**
 * @param {object} opts
 * @param {string} [opts.n8nHost]  - hostname of the connected n8n (for a reachable callback URL)
 * @param {boolean} [opts.force]   - show even if previously prompted
 */
export async function open(opts = {}) {
  if (!opts.force) {
    try { if (localStorage.getItem(DISMISS_KEY) === '1') return; } catch { /* ignore */ }
  }
  const dashboardUrl = reachableDashboardUrl(opts.n8nHost || '');

  // Already installed? Just note it — never create a duplicate.
  let status = null;
  try { status = await get('/api/errors/handler-status'); } catch { /* offline → fall through to install UI */ }
  if (status && status.installed) {
    markDone();
    const activeNote = status.active ? '' : `<br><span style="color:var(--text-dim)">It isn't active yet — ${N8N_STEP}</span>`;
    const modal = shell(`
      <h2 style="margin:0 0 8px;font-size:18px">Error reporting is on</h2>
      <p style="font-size:13px;color:var(--text-secondary);margin:0 0 16px;line-height:1.55">
        Your global error handler is already installed in n8n, so workflow failures
        show up here. Nothing to do.${activeNote}
      </p>
      <div style="display:flex;justify-content:flex-end">
        <button class="btn btn-primary" id="eh-ok">Got it</button>
      </div>`);
    modal.querySelector('#eh-ok').addEventListener('click', () => modal.remove());
    return;
  }

  // Not installed → offer to install.
  const modal = shell(`
    <h2 style="margin:0 0 8px;font-size:18px">Add error reporting</h2>
    <p style="font-size:13px;color:var(--text-secondary);margin:0 0 16px;line-height:1.55">
      Install the global error handler into n8n so any workflow failure shows up
      here automatically, with the workflow, node, and message. Recommended for
      every instance.
    </p>
    <div id="eh-result" style="font-size:12.5px;line-height:1.5;margin-bottom:14px;min-height:16px"></div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn btn-primary" id="eh-install">Install error handler</button>
      <button class="btn btn-ghost" id="eh-skip">Not now</button>
    </div>`);

  const result = modal.querySelector('#eh-result');
  const close = () => modal.remove();
  modal.querySelector('#eh-skip').addEventListener('click', () => { markDone(); close(); });

  modal.querySelector('#eh-install').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Installing…';
    result.textContent = '';
    try {
      const r = await post('/api/errors/install-handler', { dashboard_url: dashboardUrl, activate: true });
      markDone();
      const verb = r.already_existed ? 'was already installed' : (r.activated ? 'imported and activated' : 'imported');
      result.innerHTML =
        `<span style="color:var(--success)">Error handler ${verb} as "${esc(r.name || 'workflow')}".</span><br>` +
        `<span style="color:var(--text-secondary)">${N8N_STEP}</span>`;
      if (!r.activated && r.activation_error) {
        result.innerHTML += `<br><span style="color:var(--text-dim)">Auto-activate failed (${esc(r.activation_error)}); activate it manually in n8n.</span>`;
      }
      toast.success(r.already_existed ? 'Error handler already installed' : 'Error handler installed');
      // Reflect the newly-installed workflow in the dashboard stats/widgets.
      if (window.__refreshDashboard) window.__refreshDashboard();
      else if (window.__refreshInstances) window.__refreshInstances();
      btn.textContent = 'Done';
      btn.disabled = false;
      btn.classList.remove('btn-primary');
      modal.querySelector('#eh-skip').style.display = 'none';
      btn.onclick = close;
    } catch (err) {
      result.innerHTML = `<span style="color:var(--error)">${esc(err.message || 'Install failed')}. You can retry from Settings &rarr; Error Handler.</span>`;
      btn.textContent = orig;
      btn.disabled = false;
    }
  });
}
