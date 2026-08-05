/**
 * Tool approval cards — the operator-facing half of the assistant's
 * confirmation gate.
 *
 * The backend does not execute a state-changing assistant tool. It returns the
 * proposal in `pending_actions` on the chat response, and nothing happens until
 * a human clicks Run. That matters because the assistant reads content nobody
 * on your team wrote (n8n error payloads, execution run-data, RAG hits, MCP
 * output), and an instruction hidden in any of it can steer the model into
 * calling a write tool. The model can be fooled; the click cannot.
 *
 * Every chat surface must render these. A surface that ignores pending_actions
 * silently drops the proposal and the assistant looks broken: it says it
 * deactivated the workflow and nothing happened.
 *
 * Cards are self-contained. They own their own resolution state and rewrite
 * themselves in place, so a host that keeps no chat state (the one-shot "Ask AI"
 * panels) needs nothing beyond render + attach:
 *
 *     container.innerHTML = someHtml + renderPendingActions(result.pending_actions);
 *     attachApprovals(container);
 *
 * A host that re-renders from its own state (the dock) gets the same result:
 * resolutions are remembered here and re-applied on the next render.
 */

import { post } from '../api.js';
import * as toast from './toast.js';

// id -> the proposal as the backend described it. Populated on render so a card
// can rebuild itself after the operator acts.
const _proposals = new Map();
// id -> {status: 'done'|'declined'|'failed', result?, error?}
const _resolved = new Map();

function esc(s) {
  return String(s ?? '').replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
}

function attr(s) {
  return String(s ?? '').replace(/[<>&"']/g, c => (
    { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * HTML for a chat turn's pending actions. Safe to call with undefined/[].
 */
export function renderPendingActions(pending) {
  if (!Array.isArray(pending) || !pending.length) return '';
  return pending.map(p => {
    _proposals.set(p.id, p);
    return renderCard(p);
  }).join('');
}

/**
 * True when a chat result carries proposals. For surfaces that have nowhere to
 * put a card and need to say so instead.
 */
export function hasPendingActions(result) {
  return Array.isArray(result?.pending_actions) && result.pending_actions.length > 0;
}

function renderCard(p) {
  const state = _resolved.get(p.id);

  if (state?.status === 'done') {
    // Deliberately neutral, not a green success tick. The tool ran, but the
    // backend hands back the tool's own prose ("Workflow deactivated
    // successfully." / "Failed to deactivate workflow: ..."), and there is no
    // structured status behind it — n8n tools report failure by returning a
    // sentence. Painting every completed call green put a ✓ over "Failed to...",
    // which is the one thing a confirmation UI must never do. Show what the tool
    // said and let it speak for itself.
    return `
      <div class="agd-approval" data-pending-id="${attr(p.id)}" style="margin-top:6px;padding:8px 10px;border-radius:6px;background:rgba(255,255,255,0.03);border:1px solid var(--border-dim);font-size:12px">
        <div style="font-weight:600">Ran <code>${esc(p.tool)}</code></div>
        <div style="margin-top:4px;color:var(--text-secondary);white-space:pre-wrap">${esc(String(state.result ?? '').slice(0, 800))}</div>
      </div>`;
  }
  if (state?.status === 'declined') {
    return `
      <div class="agd-approval" data-pending-id="${attr(p.id)}" style="margin-top:6px;padding:8px 10px;border-radius:6px;background:rgba(255,255,255,0.02);border:1px solid var(--border-dim);font-size:12px;color:var(--text-dim)">
        Declined <code>${esc(p.tool)}</code>. Nothing ran.
      </div>`;
  }
  if (state?.status === 'failed') {
    return `
      <div class="agd-approval" data-pending-id="${attr(p.id)}" style="margin-top:6px;padding:8px 10px;border-radius:6px;background:rgba(255,109,90,0.08);border:1px solid rgba(255,109,90,0.35);font-size:12px">
        <div style="font-weight:600;color:var(--error,#ff6d5a)">✗ ${esc(p.tool)} failed</div>
        <div style="margin-top:4px;color:var(--text-dim)">${esc(String(state.error ?? '').slice(0, 400))}</div>
      </div>`;
  }

  const args = JSON.stringify(p.arguments ?? {}, null, 2);
  const scope = p.is_mcp
    ? `MCP tool · ${esc(p.server_id || 'server')}`
    : 'Dashboard action';
  return `
    <div class="agd-approval" data-pending-id="${attr(p.id)}" style="margin-top:6px;padding:8px 10px;border-radius:6px;background:rgba(251,191,36,0.07);border:1px solid rgba(251,191,36,0.35);font-size:12px">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--warning,#fbbf24);font-weight:600;margin-bottom:4px">Needs your approval — ${scope}</div>
      <div style="margin-bottom:6px">The assistant wants to run <code>${esc(p.tool)}</code>. It has not run.</div>
      <pre style="margin:0 0 8px;padding:6px 8px;background:rgba(0,0,0,0.25);border-radius:4px;overflow-x:auto;font-size:11px;line-height:1.4;max-height:180px">${esc(args)}</pre>
      <div style="display:flex;gap:6px;align-items:center">
        <button class="btn btn-sm btn-primary" data-pending-act="confirm" style="font-size:11px">Run it</button>
        <button class="btn btn-sm btn-ghost" data-pending-act="reject" style="font-size:11px">Decline</button>
        <span style="font-size:10px;color:var(--text-dim);margin-left:auto">Only approve what you asked for</span>
      </div>
    </div>`;
}

/**
 * Wire a container's approval buttons. Idempotent and delegated, so it is safe
 * to call after every render and on containers whose cards do not exist yet.
 */
export function attachApprovals(root) {
  if (!root || root.dataset.agdApprovalsWired === '1') return;
  root.dataset.agdApprovalsWired = '1';
  root.addEventListener('click', onClick);
}

async function onClick(e) {
  const btn = e.target.closest('[data-pending-act]');
  if (!btn) return;
  const card = btn.closest('.agd-approval');
  const id = card?.dataset.pendingId;
  if (!id || _resolved.has(id)) return;

  const action = btn.dataset.pendingAct;
  card.querySelectorAll('button').forEach(b => { b.disabled = true; });

  try {
    if (action === 'confirm') {
      const r = await post('/api/assistant/tools/confirm', { id });
      _resolved.set(id, { status: 'done', result: r.result || '' });
    } else {
      await post('/api/assistant/tools/reject', { id });
      _resolved.set(id, { status: 'declined' });
    }
  } catch (err) {
    _resolved.set(id, { status: 'failed', error: err.message || 'failed' });
    toast.error(`Action failed: ${err.message || 'failed'}`);
  }

  const p = _proposals.get(id);
  if (p) card.outerHTML = renderCard(p);
  window.dispatchEvent(new CustomEvent('agd:approval-resolved', { detail: { id } }));
}

/**
 * The resolution for a proposal, or undefined. Lets a host that persists its own
 * chat history reflect state without duplicating the bookkeeping.
 */
export function approvalState(id) {
  return _resolved.get(id);
}
