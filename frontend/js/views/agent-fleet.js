/**
 * LangGraph view — a managed fleet of LangGraph agents.
 *
 * The headline frame: AgeniusDesk operates LangGraph agents the way it operates
 * n8n instances. The top of the view is a catalog of registered agents (cards);
 * selecting one scopes the run composer and the run history to that agent.
 *
 * Each run streams live: tool calls tick in as timeline steps (langgraph:run
 * WebSocket events, phase discriminator), then the final answer renders as
 * markdown with a LangSmith trace link. Past runs replay their persisted event
 * log on click, so the timeline survives a refresh.
 *
 * Layout: header + catalog strip (fixed), then run-list / detail (flex, internal
 * scroll) — all viewport-constrained.
 */

import { get, post, del, onEvent } from '../api.js';
import * as toast from '../components/toast.js';
import { renderGraphSvg } from './agent-fleet-graph.js';

// Markdown rendering (marked from CDN), inlined so the view is self-contained.
let _marked = null;
async function renderMd(text) {
  if (!text) return '<p style="color:var(--text-muted)">(empty)</p>';
  if (!_marked) {
    try { const m = await import('https://esm.sh/marked'); _marked = m.marked || m.default; }
    catch { const e = document.createElement('span'); e.textContent = String(text); return `<pre style="white-space:pre-wrap;font-family:inherit">${e.innerHTML}</pre>`; }
  }
  return _marked.parse(text, { breaks: true });
}

// LangGraph Studio UI (hosted) pointed at a local `langgraph dev` server on :2024.
const STUDIO_URL = 'https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024';

let _container = null;
let _agents = [];
let _selectedAgentId = null;
let _runs = [];
let _errors = [];          // recent errors for the target picker
let _selectedId = null;
let _unsub = null;
let _graphs = {};          // agent_id -> {nodes, edges} topology (cached)
let _poll = null;          // reconcile timer: catches a missed `final` WS event
let _serverLive = null;    // the SERVER's live_run_id: authoritative "is anything running"
const _expandedCards = new Set();  // agent ids whose catalog card is expanded

// ── Helpers ───────────────────────────────────────────────────────────────────

function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : String(s); return el.innerHTML; }

const STATUS = {
  running: { label: 'Running',           color: '#38bdf8', spin: true },
  paused:  { label: 'Awaiting approval', color: '#f59e0b', spin: false },
  done:    { label: 'Done',              color: '#34d399', spin: false },
  error:   { label: 'Error',             color: '#ef4444', spin: false },
};

function statusBadge(status) {
  const s = STATUS[status] || STATUS.running;
  const dot = s.spin
    ? `<span class="lg-spin" style="display:inline-block;width:9px;height:9px;border:2px solid ${s.color};border-right-color:transparent;border-radius:50%"></span>`
    : `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${s.color}"></span>`;
  return `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:${s.color}">${dot}${esc(s.label)}</span>`;
}

function fmtWhen(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso.replace(' ', 'T') + 'Z');
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  } catch { return iso; }
}

function fmtArgs(args) {
  if (!args || !Object.keys(args).length) return '';
  return Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ');
}

function runTitle(run) {
  if (run.prompt) return run.prompt;
  // Content agents are not error-based: title by the agent, not "Most recent error".
  const agent = _agents.find((a) => a.id === (run.agent_id || _selectedAgentId));
  if (agent && agent.uses_errors === false) return `${agent.name} run`;
  if (run.target && run.target !== 'latest') return `Error ${run.target}`;
  return 'Most recent error';
}

function fmtTokens(n) {
  if (!n) return '';
  return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
}
function fmtCost(c) {
  if (!c) return '';
  return c < 0.01 ? '<$0.01' : '$' + c.toFixed(c < 1 ? 3 : 2);
}
function usageChip(run) {
  if (!run.total_tokens) return '';
  const parts = [`${fmtTokens(run.total_tokens)} tok`];
  const cost = fmtCost(run.total_cost);
  if (cost) parts.push(cost);
  return `<button class="lg-usage" title="Full input/output token + per-call breakdown" style="display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;color:#a78bfa;background:transparent;border:1px solid rgba(167,139,250,0.4);border-radius:10px;padding:2px 9px;cursor:pointer">⛁ ${parts.join(' · ')} ▾</button>`;
}

function fmtCost6(c) { return c ? '$' + Number(c).toFixed(6) : '$0'; }

function openUsageModal(run) {
  const d = run.usage_detail || {};
  const inTok = d.input_tokens || 0, outTok = d.output_tokens || 0;
  const totTok = d.total_tokens || run.total_tokens || 0;
  const steps = Array.isArray(d.steps) ? d.steps : [];

  const stat = (label, val, color) =>
    `<div style="flex:1;min-width:90px;background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px">
       <div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted)">${label}</div>
       <div style="font-size:18px;font-weight:700;color:${color||'var(--text-primary)'};margin-top:2px">${val}</div></div>`;

  const rows = steps.map((s, i) => `
    <tr style="border-top:1px solid var(--border)">
      <td style="padding:6px 8px;color:var(--text-muted)">${i + 1}</td>
      <td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px">${esc(s.name || 'llm')}</td>
      <td style="padding:6px 8px;text-align:right">${(s.input || 0).toLocaleString()}</td>
      <td style="padding:6px 8px;text-align:right">${(s.output || 0).toLocaleString()}</td>
      <td style="padding:6px 8px;text-align:right;font-weight:600">${(s.total || 0).toLocaleString()}</td>
      <td style="padding:6px 8px;text-align:right;color:#a78bfa">${fmtCost6(s.cost)}</td>
    </tr>`).join('');

  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9999;display:flex;align-items:center;justify-content:center;padding:24px';
  // Opaque card: paint a solid base (--bg-void) then the (possibly translucent)
  // --bg-surface tint on top, so nothing behind the modal bleeds through.
  overlay.innerHTML = `
    <div style="background-color:var(--bg-void);background-image:linear-gradient(var(--bg-surface),var(--bg-surface));border:1px solid var(--border);border-radius:var(--radius);max-width:680px;width:100%;max-height:82vh;overflow-y:auto;padding:20px 22px;box-shadow:0 20px 60px rgba(0,0,0,0.6)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div style="font-size:16px;font-weight:700">Token &amp; cost breakdown</div>
        <button class="lg-usage-close" style="background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-muted);cursor:pointer;padding:4px 10px;font-size:13px">Close</button>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
        ${stat('Input', inTok.toLocaleString() + ' tok', '#38bdf8')}
        ${stat('Output', outTok.toLocaleString() + ' tok', '#34d399')}
        ${stat('Total', totTok.toLocaleString() + ' tok')}
        ${stat('Cost', fmtCost6(run.total_cost), '#a78bfa')}
      </div>
      ${(d.input_cost || d.output_cost) ? `<div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">Input ${fmtCost6(d.input_cost)} · Output ${fmtCost6(d.output_cost)}</div>` : ''}
      <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin:10px 0 6px">Per model call through the run (${steps.length})</div>
      ${steps.length ? `
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr style="color:var(--text-muted);font-size:10px;text-transform:uppercase">
            <th style="padding:4px 8px;text-align:left">#</th><th style="padding:4px 8px;text-align:left">Call</th>
            <th style="padding:4px 8px;text-align:right">Input</th><th style="padding:4px 8px;text-align:right">Output</th>
            <th style="padding:4px 8px;text-align:right">Total</th><th style="padding:4px 8px;text-align:right">Cost</th>
          </tr></thead><tbody>${rows}</tbody>
        </table>` : `<div style="color:var(--text-muted);font-size:12px;padding:8px 0">Per-call detail not available (LangSmith returned only the aggregate).</div>`}
      <div style="font-size:11px;color:var(--text-muted);margin-top:12px">Figures from the LangSmith trace for this run.${run.trace_url ? ` <a href="${esc(run.trace_url)}" target="_blank" style="color:var(--accent)">Open full trace ↗</a>` : ''}</div>
    </div>`;
  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('.lg-usage-close').addEventListener('click', close);
  document.body.appendChild(overlay);
}

function selectedAgent() { return _agents.find(a => a.id === _selectedAgentId) || null; }
function liveRun() { return _runs.find(r => r.status === 'running'); }

const BADGE_COLORS = {
  'human-in-the-loop': '#f59e0b',
  'checkpointer':      '#f59e0b',
  'parallel':          '#a78bfa',
  'fan-out':           '#a78bfa',
};
function badgeChip(b) {
  const c = BADGE_COLORS[b] || 'var(--text-muted)';
  return `<span style="font-size:10px;font-weight:700;color:${c};border:1px solid ${c};border-radius:10px;padding:1px 7px;opacity:.9">${esc(b)}</span>`;
}

// Short, friendly model label + tier color (Haiku=cheap/green, Sonnet=mid, Opus=top).
function modelChip(model) {
  const m = String(model || '');
  let label = m, color = 'var(--text-muted)';
  if (m.includes('haiku')) { label = 'Haiku'; color = '#34d399'; }
  else if (m.includes('sonnet')) { label = 'Sonnet'; color = '#38bdf8'; }
  else if (m.includes('opus')) { label = 'Opus'; color = '#f59e0b'; }
  return `<span title="${esc(m)}" style="font-size:10px;font-weight:700;color:${color};border:1px solid ${color};border-radius:10px;padding:1px 7px">${esc(label)}</span>`;
}

// ── Render ────────────────────────────────────────────────────────────────────

export async function render(container) {
  _container = container;
  console.log('[langgraph] view loaded · run-button driven by server live_run_id');
  // Diagnostic: run __lgDebug() in the console when the button looks stuck.
  window.__lgDebug = async () => {
    let serverLive = '(fetch failed)';
    try {
      const res = await get(`/api/agent-fleet/runs?agent_id=${encodeURIComponent(_selectedAgentId || '')}`);
      serverLive = res.live_run_id;
    } catch {}
    console.log('[lg-debug]', {
      clientServerLive: _serverLive,
      reconcilerPolling: !!_poll,
      serverLiveRunId: serverLive,
      runs: _runs.map(r => ({ id: String(r.id).slice(0, 8), status: r.status })),
    });
  };
  container.innerHTML = `
    <div style="padding:20px 24px;max-width:1500px;margin:0 auto;display:flex;flex-direction:column;height:calc(100vh - 40px);box-sizing:border-box">
      <div style="margin-bottom:12px;flex-shrink:0;display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
        <div>
          <h1 style="font-size:20px;font-weight:700;margin:0 0 4px">LangGraph Agents</h1>
          <p style="color:var(--text-muted);font-size:13px;margin:0">
            A managed fleet of LangGraph agents. AgeniusDesk operates them the way it operates n8n instances: pick an agent, run it, watch every step, replay any past run.
          </p>
        </div>
        <a href="${STUDIO_URL}" target="_blank" title="Opens LangGraph Studio. Requires \`langgraph dev\` running locally."
          style="font-size:12px;color:var(--text-muted);text-decoration:none;border:1px solid var(--border);border-radius:var(--radius);padding:8px 12px;white-space:nowrap;flex-shrink:0">Open in Studio ↗</a>
      </div>

      <div id="lg-catalog" style="display:flex;gap:10px;flex-wrap:wrap;flex-shrink:0;margin-bottom:12px"></div>

      <div id="lg-composer" style="display:none;gap:10px;flex-wrap:wrap;align-items:center;background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;margin-bottom:14px;flex-shrink:0">
        <select id="lg-target" title="What to act on. Defaults to the most recent error AgeniusDesk has collected."
          style="min-width:240px;max-width:420px;padding:9px 10px;background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary);font-size:12px">
          <option value="">Most recent error</option>
        </select>
        <input id="lg-prompt" type="text" placeholder="Optional: free-form request (overrides the picker)"
          style="flex:1;min-width:260px;padding:9px 12px;background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary);font-size:13px" />
        <button id="lg-run" style="padding:9px 18px;background:var(--accent);color:#fff;border:none;border-radius:var(--radius);font-size:13px;font-weight:700;cursor:pointer">Run agent</button>
      </div>

      <!-- Run history is a compact horizontal strip so the detail (graph + timeline)
           gets the full width below it. -->
      <div id="lg-list" style="display:flex;gap:8px;overflow-x:auto;flex-shrink:0;margin-bottom:12px;padding-bottom:2px"></div>
      <div id="lg-detail" style="background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;overflow-y:auto;flex:1;min-height:0"></div>
    </div>
    <style>
      @keyframes lg-spin { to { transform: rotate(360deg); } }
      .lg-spin { animation: lg-spin 0.8s linear infinite; }
      @keyframes lg-pulse { 0%,100% { opacity:1 } 50% { opacity:.5 } }
      .lg-node-cur rect { animation: lg-pulse 1.1s ease-in-out infinite; }
      .lg-node-cur { filter: drop-shadow(0 0 5px rgba(56,189,248,0.75)); }
      .lg-agent-card { cursor:pointer; flex:0 0 auto; width:190px; background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius); padding:7px 10px; transition:border-color .12s; }
      .lg-agent-card:hover { border-color:var(--text-muted); }
      .lg-card-head { display:flex; align-items:center; gap:6px; }
      .lg-card-exp { background:none; border:none; color:var(--text-muted); cursor:pointer; padding:0 1px; font-size:11px; line-height:1; }
      .lg-card-exp:hover { color:var(--text-primary); }
      .lg-card-body { margin-top:7px; padding-top:7px; border-top:1px solid var(--border); }
      .lg-step { display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-bottom:1px solid var(--border); }
      .lg-step:last-child { border-bottom:none; }
      .lg-chip { font-family:var(--font-mono);font-size:12px;background:var(--bg-void);border:1px solid var(--border);border-radius:5px;padding:4px 8px;word-break:break-all; }
      #lg-detail details > summary { cursor:pointer;font-size:11px;color:var(--text-muted); }
      #lg-detail details pre { background:var(--bg-void);padding:8px 10px;border-radius:6px;overflow-x:auto;font-size:11px;white-space:pre-wrap;word-break:break-all;margin:6px 0 0; }
      #lg-triage h1 { font-size:18px; margin:14px 0 8px; }
      #lg-triage h2 { font-size:15px; margin:16px 0 6px; border-bottom:1px solid var(--border); padding-bottom:4px; }
      #lg-triage code { font-family:var(--font-mono); font-size:12px; }
      #lg-triage a { color:var(--accent); }
    </style>
  `;

  container.querySelector('#lg-run').addEventListener('click', startRun);
  container.querySelector('#lg-prompt').addEventListener('keydown', (e) => { if (e.key === 'Enter') startRun(); });

  if (_unsub) _unsub();
  _unsub = onEvent('langgraph:run', onRunEvent);

  await Promise.all([loadAgents(), loadErrorPicker()]);
}

export function cleanup() {
  if (_unsub) { _unsub(); _unsub = null; }
  stopReconcile();
}

// ── Catalog ─────────────────────────────────────────────────────────────────

async function loadAgents() {
  try {
    const res = await get('/api/agent-fleet/agents');
    _agents = res.agents || [];
    // Honor a pre-selected agent passed via nav opts (from the dashboard widget).
    const wanted = window.__viewOpts?.agentId;
    if (wanted && _agents.some(a => a.id === wanted)) _selectedAgentId = wanted;
    else if (!_selectedAgentId && _agents.length) _selectedAgentId = res.default || _agents[0].id;
  } catch (e) {
    toast.error(`Could not load agents: ${e.message}`);
    _agents = [];
  }
  renderCatalog();
  await selectAgent(_selectedAgentId);
  // Auto-run when navigated here with run:true (dashboard "Run" button). One-shot.
  const opts = window.__viewOpts;
  if (opts?.run && _selectedAgentId) startRun();
  if (opts) window.__viewOpts = null;
}

function renderCatalog() {
  const cat = _container.querySelector('#lg-catalog');
  if (!cat) return;
  if (!_agents.length) {
    cat.innerHTML = `<div style="color:var(--text-muted);font-size:13px">No agents registered.</div>`;
    return;
  }
  cat.innerHTML = _agents.map(a => {
    const active = a.id === _selectedAgentId;
    const open = _expandedCards.has(a.id);
    const badges = (a.badges || []).map(badgeChip).join(' ');
    return `<div class="lg-agent-card" data-id="${esc(a.id)}" style="border-color:${active ? 'var(--accent)' : 'var(--border)'};${active ? 'box-shadow:0 0 0 1px var(--accent) inset' : ''}">
      <div class="lg-card-head">
        <span style="font-size:13px;font-weight:600;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(a.name)}</span>
        ${a.hitl ? '<span title="Pauses for human approval" style="font-size:10px">⏸️</span>' : ''}
        ${modelChip(a.model)}
        <button class="lg-card-exp" data-exp="${esc(a.id)}" title="${open ? 'Hide details' : 'Show details'}" aria-label="toggle details">${open ? '▴' : '▾'}</button>
      </div>
      ${open ? `<div class="lg-card-body">
        <div style="font-size:11.5px;color:var(--text-muted);line-height:1.45;margin-bottom:7px">${esc(a.tagline)}</div>
        <div style="display:flex;gap:5px;flex-wrap:wrap">${badges}</div>
      </div>` : ''}
    </div>`;
  }).join('');
  // Card click selects the agent; the caret toggles the description (stops the
  // click from also selecting).
  cat.querySelectorAll('.lg-agent-card').forEach(c => c.addEventListener('click', () => selectAgent(c.dataset.id)));
  cat.querySelectorAll('.lg-card-exp').forEach(b => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const id = b.dataset.exp;
    if (_expandedCards.has(id)) _expandedCards.delete(id); else _expandedCards.add(id);
    renderCatalog();
  }));
}

async function selectAgent(agentId) {
  if (!agentId) return;
  _selectedAgentId = agentId;
  _selectedId = null;
  renderCatalog();

  const composer = _container.querySelector('#lg-composer');
  const agent = selectedAgent();
  if (composer) composer.style.display = agent ? 'flex' : 'none';
  if (agent) {
    const prompt = _container.querySelector('#lg-prompt');
    if (prompt) prompt.placeholder = agent.run_hint || 'Optional: free-form request';
    const runBtn = _container.querySelector('#lg-run');
    if (runBtn) runBtn.textContent = 'Run agent';
    // Content agents (uses_errors === false) have nothing to do with errors:
    // hide the error picker so the composer stops saying "Most recent error".
    const target = _container.querySelector('#lg-target');
    if (target) target.style.display = agent.uses_errors === false ? 'none' : '';
  }
  loadGraph(agentId);   // fire-and-forget; detail re-renders when it lands
  await loadRuns();
}

// Topology for the live graph panel. Cached per agent; a failure just hides the
// panel (the timeline still works).
async function loadGraph(agentId) {
  if (!agentId || _graphs[agentId]) return;
  try {
    const topo = await get(`/api/agent-fleet/agents/${encodeURIComponent(agentId)}/graph`);
    if (topo && Array.isArray(topo.nodes)) {
      _graphs[agentId] = topo;
      if (_selectedId) renderDetail();
    }
  } catch { /* no panel; timeline still renders */ }
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function loadRuns() {
  try {
    const res = await get(`/api/agent-fleet/runs?agent_id=${encodeURIComponent(_selectedAgentId || '')}`);
    _runs = res.runs || [];
    _serverLive = res.live_run_id || null;
    renderList();
    updateRunButton();
    if (!_selectedId && _runs.length) selectRun(_runs[0].id);
    else renderDetail();
  } catch (e) {
    toast.error(`Could not load runs: ${e.message}`);
  }
}

async function loadErrorPicker() {
  const sel = _container?.querySelector('#lg-target');
  if (!sel) return;
  try {
    // Internal route (the public /api/v1 variant needs an X-API-Key).
    const res = await get('/api/errors?limit=15');
    _errors = res.errors || [];
  } catch { _errors = []; }
  const opts = ['<option value="">Most recent error</option>'];
  for (const err of _errors) {
    const label = `#${err.id} · ${err.workflow_name || err.workflow_id} · ${(err.error_message || '').slice(0, 60)}`;
    opts.push(`<option value="${esc(err.id)}">${esc(label)}</option>`);
  }
  sel.innerHTML = opts.join('');
}

// ── Run lifecycle ─────────────────────────────────────────────────────────────

async function startRun() {
  if (!_selectedAgentId) { toast.error('Pick an agent first.'); return; }
  const prompt = _container.querySelector('#lg-prompt').value.trim();
  const targetVal = _container.querySelector('#lg-target').value;
  const body = { agent_id: _selectedAgentId };
  if (prompt) body.prompt = prompt;
  else if (targetVal) body.error_id = parseInt(targetVal, 10);

  const btn = _container.querySelector('#lg-run');
  btn.disabled = true; btn.textContent = 'Starting...';
  try {
    const res = await post('/api/agent-fleet/triage', body);
    if (res.run) {
      // The WS `started` event can land BEFORE this response and already create the
      // tile. Merge into it instead of unshifting a twin — otherwise one copy strands
      // on "running" (it never gets the later awaiting_approval/final), which shows up
      // as redundant boxes stuck on Running.
      const dup = _runs.find(r => r.id === res.run.id);
      if (dup) Object.assign(dup, res.run);
      else _runs.unshift(res.run);
      _serverLive = res.run.id;   // optimistic; the reconciler corrects from the server
      selectRun(res.run.id);
    }
    toast.success('Run started.');
  } catch (e) {
    toast.error(e.message);
  } finally {
    updateRunButton();
  }
}

function updateRunButton() {
  const btn = _container?.querySelector('#lg-run');
  if (!btn) return;
  // The Run BUTTON is gated on the SERVER's live_run_id (the single-flight slot,
  // cleared in a finally when any run ends), NOT a client status a dropped event
  // can strand.
  const busy = !!_serverLive;
  btn.disabled = busy;
  btn.textContent = busy ? 'Run in progress…' : 'Run agent';
  // The reconcile POLLER, however, must run whenever ANY tile is still 'running'
  // client-side, even when the server slot is free (e.g. a HITL run resumed and
  // its post-resume `final`/`resumed` event was dropped, or landed on a re-created
  // run object). The server is authoritative; the poller reconciles the stranded
  // tile to 'done' and then self-stops once nothing is running.
  if (busy || _runs.some(r => r.status === 'running')) ensureReconcile();
  else stopReconcile();
}

// Safety net for a dropped `final` WebSocket event: while a run still shows
// "running" client-side, poll the server; the moment it reports the run terminal
// (or paused), apply it. Never regresses a run, and stops itself when idle.
function ensureReconcile() {
  if (_poll) return;
  _poll = setInterval(reconcileLive, 4000);
}
function stopReconcile() {
  if (_poll) { clearInterval(_poll); _poll = null; }
}
async function reconcileLive() {
  // Poll the list endpoint: it returns the authoritative server live_run_id plus
  // fresh per-run statuses. This is the safety net that frees the UI when terminal
  // WS events are dropped, because the server clears live_run_id no matter what.
  try {
    const res = await get(`/api/agent-fleet/runs?agent_id=${encodeURIComponent(_selectedAgentId || '')}`);
    _serverLive = res.live_run_id || null;
    for (const fr of (res.runs || [])) {
      const idx = _runs.findIndex(r => r.id === fr.id);
      if (idx < 0) continue;
      // Take the server status only when it does not regress what the WS stream
      // already advanced (avoids a mid-commit snapshot clobbering done).
      if ((_STATUS_RANK[fr.status] ?? 0) >= (_STATUS_RANK[_runs[idx].status] ?? 0)) {
        _runs[idx] = { ..._runs[idx], status: fr.status };
      }
    }
    renderList();
    updateRunButton();         // frees the button + stops polling when _serverLive is null
    if (_selectedId) renderDetail();
  } catch { /* try again next tick */ }
}

function onRunEvent(event) {
  if (!event || !event.run_id) return;
  let run = _runs.find(r => r.id === event.run_id);
  if (!run) {
    // A run not in the current (agent-filtered) list. Only adopt it if it
    // belongs to the selected agent; the started event carries agent_id.
    if (event.agent_id && event.agent_id !== _selectedAgentId) return;
    run = { id: event.run_id, agent_id: event.agent_id || _selectedAgentId, status: 'running', target: '', prompt: '', events: [], created_at: '' };
    _runs.unshift(run);
  }
  if (!Array.isArray(run.events)) run.events = [];
  run.events.push(event);

  if (event.phase === 'started') {
    run.status = 'running';
    _serverLive = event.run_id;
    if (event.model) run.model = event.model;
  } else if (event.phase === 'awaiting_approval') {
    run.status = 'paused';
    run.proposal_md = event.proposal_md || '';
    run.choices = event.choices || null;   // optional pick-list -> one approve button each
    if (_serverLive === event.run_id) _serverLive = null;   // pause releases the server slot
  } else if (event.phase === 'resumed') {
    run.status = 'running';
    _serverLive = event.run_id;
  } else if (event.phase === 'final') {
    run.status = 'done';
    run.triage_md = event.triage_md || '';
    run.trace_url = event.trace_url || '';
    if (event.total_tokens != null) run.total_tokens = event.total_tokens;
    if (event.total_cost != null) run.total_cost = event.total_cost;
    if (event.usage_detail) run.usage_detail = event.usage_detail;
    if (_serverLive === event.run_id) _serverLive = null;
  } else if (event.phase === 'error') {
    run.status = 'error';
    run.error = event.message || '';
    if (_serverLive === event.run_id) _serverLive = null;
  }

  renderList();
  updateRunButton();
  if (_selectedId === event.run_id) renderDetail({ stickToBottom: true });
}

// Status progression rank: a fetched snapshot must never pull a run BACKWARD.
const _STATUS_RANK = { running: 0, paused: 1, done: 2, error: 2 };

async function selectRun(id) {
  _selectedId = id;
  renderList();
  // Pull the full row (list payload omits events + triage markdown).
  try {
    const full = await get(`/api/agent-fleet/runs/${id}`);
    const idx = _runs.findIndex(r => r.id === id);
    if (idx >= 0) {
      const local = _runs[idx] || {};
      const merged = { ..._runs[idx], ...full };
      // A detail fetch issued while the run was live can resolve AFTER the WS
      // `final` event, with a stale "running" snapshot. Never let it regress the
      // status, the event log, or the final fields the live stream already set,
      // or the tile + Run button stay stuck on "running" forever.
      if ((_STATUS_RANK[local.status] ?? 0) > (_STATUS_RANK[full.status] ?? 0)) {
        merged.status = local.status;
      }
      if ((local.events?.length || 0) > (full.events?.length || 0)) merged.events = local.events;
      for (const k of ['triage_md', 'trace_url', 'total_tokens', 'total_cost', 'usage_detail']) {
        if ((full[k] == null || full[k] === '') && local[k] != null) merged[k] = local[k];
      }
      _runs[idx] = merged;
    } else {
      _runs.unshift(full);
    }
  } catch { /* keep the list version */ }
  renderList();
  renderDetail();
  updateRunButton();
}

// ── List pane ─────────────────────────────────────────────────────────────────

function renderList() {
  const list = _container.querySelector('#lg-list');
  if (!_runs.length) {
    const agent = selectedAgent();
    list.innerHTML = `<div style="color:var(--text-muted);font-size:13px;padding:20px;text-align:center">No runs yet for ${esc(agent ? agent.name : 'this agent')}. Hit Run agent above.</div>`;
    return;
  }
  // Defensive: collapse any duplicate run ids, keeping the most-advanced status so a
  // stale 'running' phantom can never sit next to the real paused/done run.
  if (_runs.length > 1) {
    const byId = new Map();
    for (const r of _runs) {
      const prev = byId.get(r.id);
      if (!prev) { byId.set(r.id, r); continue; }
      byId.set(r.id, (_STATUS_RANK[r.status] ?? 0) >= (_STATUS_RANK[prev.status] ?? 0)
        ? { ...prev, ...r } : { ...r, ...prev });
    }
    if (byId.size !== _runs.length) _runs.splice(0, _runs.length, ...byId.values());
  }
  list.innerHTML = _runs.map(r => {
    const active = r.id === _selectedId;
    return `<div class="lg-card" data-id="${esc(r.id)}" style="cursor:pointer;flex-shrink:0;min-width:210px;max-width:248px;background:var(--bg-surface);border:1px solid ${active ? 'var(--accent)' : 'var(--border)'};border-radius:var(--radius);padding:12px 14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:4px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(runTitle(r))}</div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        ${statusBadge(r.status)}
        <span style="font-size:10px;color:var(--text-muted)">${fmtWhen(r.created_at)}</span>
      </div>
    </div>`;
  }).join('');
  list.querySelectorAll('.lg-card').forEach(c => c.addEventListener('click', () => selectRun(c.dataset.id)));
}

// ── Detail pane ───────────────────────────────────────────────────────────────

function timelineHtml(events) {
  const steps = [];
  for (const ev of events || []) {
    if (ev.phase === 'started') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">▶</span>
        <div style="font-size:13px"><strong>${esc(ev.task || 'Run')}</strong>
          <span style="color:var(--text-muted);font-size:11px"> · ${esc(ev.model || '')}</span></div></div>`);
    } else if (ev.phase === 'thinking') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">💭</span>
        <div style="font-size:12px;color:var(--text-secondary);font-style:italic;white-space:pre-wrap">${esc(ev.text)}</div></div>`);
    } else if (ev.phase === 'node') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">◆</span>
        <div style="font-size:12px"><span style="font-family:var(--font-mono);font-size:11px;color:#a78bfa;font-weight:700">${esc(ev.label)}</span>
          <div style="color:var(--text-secondary);margin-top:2px;white-space:pre-wrap">${esc(ev.text)}</div></div></div>`);
    } else if (ev.phase === 'tool_call') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">→</span>
        <span class="lg-chip">${esc(ev.tool)}(${esc(fmtArgs(ev.args))})</span></div>`);
    } else if (ev.phase === 'tool_result') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">←</span>
        <details><summary>${esc(ev.tool)} result</summary><pre>${esc(ev.preview || '(empty)')}</pre></details></div>`);
    } else if (ev.phase === 'awaiting_approval') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">⏸️</span>
        <div style="font-size:12px;color:#f59e0b;font-weight:600">Paused for human approval (interrupt)</div></div>`);
    } else if (ev.phase === 'resumed') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">▶</span>
        <div style="font-size:12px;color:#38bdf8">Resumed — operator chose <strong>${esc(ev.action || 'approve')}</strong></div></div>`);
    } else if (ev.phase === 'error') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">✕</span>
        <div style="font-size:12px;color:#fca5a5">${esc(ev.message)}</div></div>`);
    }
  }
  return steps.join('');
}

function wireApproval(pane, run, proposalMd) {
  const editBox = pane.querySelector('#lg-edit');
  const toggle = pane.querySelector('.lg-edit-toggle');
  const approve = pane.querySelector('.lg-approve');
  const reject = pane.querySelector('.lg-reject');

  // Write-path agents (Remediator): dry-run vs live.
  const dryrun = pane.querySelector('.lg-dryrun');
  const live = pane.querySelector('.lg-live');
  if (dryrun) dryrun.addEventListener('click', () => submitResume(run.id, 'approve', '', 'dry_run'));
  if (live) live.addEventListener('click', () => {
    if (!confirm('Apply this fix to the live n8n workflow? A snapshot is saved first for rollback.')) return;
    submitResume(run.id, 'approve', '', 'live');
  });

  // Choice gate (e.g. Blog Writer idea pick): one approve button per option; the
  // 1-based value rides back as `choice`.
  pane.querySelectorAll('.lg-choice').forEach(b => b.addEventListener('click', () =>
    submitResume(run.id, 'approve', '', undefined, parseInt(b.dataset.choice, 10))));

  if (toggle) toggle.addEventListener('click', () => {
    const editing = editBox.style.display !== 'none';
    if (editing) {
      editBox.style.display = 'none';
      approve.textContent = 'Approve';
    } else {
      editBox.value = proposalMd || '';
      editBox.style.display = 'block';
      editBox.focus();
      approve.textContent = 'Approve edited';
    }
  });

  if (approve) approve.addEventListener('click', () => {
    const editing = editBox && editBox.style.display !== 'none';
    const edited = editing ? editBox.value.trim() : '';
    submitResume(run.id, edited ? 'edit' : 'approve', edited);
  });
  if (reject) reject.addEventListener('click', () => submitResume(run.id, 'reject', ''));
}

async function submitResume(runId, action, edited, mode, choice) {
  const run = _runs.find(r => r.id === runId);
  if (run) run.status = 'running';   // optimistic; WS will confirm
  _serverLive = runId;               // approve restarts the run server-side
  renderList(); renderDetail();
  updateRunButton();                 // reflect running + START the reconciler on the approve path
  try {
    await post(`/api/agent-fleet/runs/${runId}/resume`, { action, edited, mode, choice });
    toast.success(action === 'reject' ? 'Rejected.' : (mode === 'live' ? 'Applying live…' : 'Approved — resuming.'));
  } catch (e) {
    if (run) run.status = 'paused';
    renderList(); renderDetail();
    toast.error(e.message);
  }
}

async function renderDetail({ stickToBottom = false } = {}) {
  const pane = _container.querySelector('#lg-detail');
  const run = _runs.find(r => r.id === _selectedId);
  if (!run) {
    const agent = selectedAgent();
    pane.innerHTML = agent
      ? `<div style="color:var(--text-muted);font-size:13px"><div style="font-size:15px;font-weight:700;color:var(--text-primary);margin-bottom:6px">${esc(agent.name)}</div>${esc(agent.description)}<div style="margin-top:12px">Kick off a run above, or select a past run.</div></div>`
      : `<div style="color:var(--text-muted);font-size:13px">Select an agent.</div>`;
    return;
  }

  const header = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:10px">
      <div style="min-width:0">
        <div style="font-size:16px;font-weight:700;line-height:1.3">${esc(runTitle(run))}</div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:3px">
          ${statusBadge(run.status)}${run.model ? ' · ' + esc(run.model) : ''}${run.created_at ? ' · ' + fmtWhen(run.created_at) : ''}
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">
        ${usageChip(run)}
        ${run.trace_url ? `<a href="${esc(run.trace_url)}" target="_blank" style="padding:7px 12px;background:rgba(52,211,153,0.12);color:#34d399;border:1px solid rgba(52,211,153,0.4);border-radius:var(--radius);font-size:12px;font-weight:700;text-decoration:none">View trace in LangSmith ↗</a>` : ''}
        <button class="lg-del" style="padding:7px 10px;background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);font-size:12px;color:var(--text-muted);cursor:pointer">Delete</button>
      </div>
    </div>`;

  const events = Array.isArray(run.events) ? run.events : [];
  const timeline = timelineHtml(events);
  const isPaused = run.status === 'paused';

  // Live graph: nodes light up from the same event stream as the timeline.
  const topo = _graphs[run.agent_id || _selectedAgentId];
  const graphPanel = topo ? `
    <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Live graph</div>
    <div id="lg-graph" style="border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-elevated, var(--bg-void));padding:14px 12px;max-height:58vh;overflow:auto">
      ${renderGraphSvg(topo, events)}
    </div>` : '';

  // The proposal awaiting approval (from the live event, a replayed event, or the
  // persisted triage_md, which holds the proposal while paused).
  let proposalMd = run.proposal_md || '';
  if (!proposalMd) {
    for (const ev of events) if (ev.phase === 'awaiting_approval' && ev.proposal_md) proposalMd = ev.proposal_md;
  }
  if (!proposalMd && isPaused) proposalMd = run.triage_md || '';

  // The finalized result (only once done; while paused triage_md is the proposal).
  const triageMd = (!isPaused && run.triage_md) ? run.triage_md : '';
  const triageHtml = triageMd ? await renderMd(triageMd) : '';
  const proposalHtml = (isPaused && proposalMd) ? await renderMd(proposalMd) : '';
  const writesToN8n = !!(selectedAgent()?.badges || []).includes('writes-to-n8n');

  // Graph + timeline sit side by side (two columns) when a topology exists, so the
  // tall graph never pushes the timeline (or the approval panel) off-screen.
  const timelineCol = `
    <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Investigation timeline</div>
    <div id="lg-timeline" style="border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-elevated, var(--bg-void));padding:6px 14px;max-height:58vh;overflow-y:auto">
      ${timeline || '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">Waiting for the first event…</div>'}
    </div>`;
  const topRow = graphPanel
    ? `<div style="display:grid;grid-template-columns:minmax(340px,1fr) minmax(0,1fr);gap:16px;align-items:start;margin-bottom:14px">
         <div style="min-width:0">${graphPanel}</div>
         <div style="min-width:0">${timelineCol}</div>
       </div>`
    : `<div style="margin-bottom:14px">${timelineCol}</div>`;

  pane.innerHTML = `
    ${header}
    ${topRow}
    ${run.status === 'error' && run.error ? `
      <div style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:var(--radius);padding:12px;color:#fca5a5;font-size:13px;margin-top:12px">
        <strong>Failed:</strong> ${esc(run.error)}
      </div>` : ''}
    ${isPaused ? `
      <div style="border:1px solid rgba(245,158,11,0.45);border-radius:var(--radius);background:rgba(245,158,11,0.06);padding:14px;margin-top:14px">
        <div style="font-size:11px;font-weight:700;color:#f59e0b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">⏸️ Awaiting your approval</div>
        <div id="lg-proposal" style="font-size:14px;line-height:1.6">${proposalHtml || '<em style="color:var(--text-muted)">No proposal text.</em>'}</div>
        ${(run.choices && run.choices.length) ? `
        <div style="display:flex;flex-direction:column;gap:8px;margin-top:12px">
          ${run.choices.map(c => `<button class="lg-choice" data-choice="${esc(String(c.value))}" style="text-align:left;padding:10px 14px;background:#34d399;color:#04231a;border:none;border-radius:var(--radius);font-size:13px;font-weight:700;cursor:pointer">✓ ${esc(c.label)}</button>`).join('')}
          <button class="lg-reject" style="align-self:flex-start;padding:8px 14px;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.4);border-radius:var(--radius);font-size:13px;color:#fca5a5;cursor:pointer">Reject all</button>
        </div>` : (writesToN8n ? `
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center">
          <button class="lg-dryrun" title="Compute the exact write, change nothing" style="padding:8px 16px;background:#38bdf8;color:#04222e;border:none;border-radius:var(--radius);font-size:13px;font-weight:700;cursor:pointer">Approve · dry run</button>
          <button class="lg-live" title="Snapshot, then write the fix to n8n" style="padding:8px 16px;background:#f59e0b;color:#2a1a02;border:none;border-radius:var(--radius);font-size:13px;font-weight:700;cursor:pointer">⚠ Apply live</button>
          <button class="lg-reject" style="padding:8px 14px;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.4);border-radius:var(--radius);font-size:13px;color:#fca5a5;cursor:pointer">Reject</button>
        </div>` : `
        <textarea id="lg-edit" placeholder="Edit the fix, then Approve edited…" style="display:none;width:100%;box-sizing:border-box;margin-top:10px;min-height:120px;padding:10px;background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary);font-size:13px;font-family:var(--font-mono)"></textarea>
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <button class="lg-approve" style="padding:8px 16px;background:#34d399;color:#04231a;border:none;border-radius:var(--radius);font-size:13px;font-weight:700;cursor:pointer">Approve</button>
          <button class="lg-edit-toggle" style="padding:8px 14px;background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);font-size:13px;color:var(--text-primary);cursor:pointer">Edit</button>
          <button class="lg-reject" style="padding:8px 14px;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.4);border-radius:var(--radius);font-size:13px;color:#fca5a5;cursor:pointer">Reject</button>
        </div>`)}
      </div>` : ''}
    ${triageHtml ? `
      <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin:14px 0 4px">Result</div>
      <div id="lg-triage" style="font-size:14px;line-height:1.6">${triageHtml}</div>` : ''}
  `;

  if (stickToBottom) {
    const tl = pane.querySelector('#lg-timeline');
    if (tl) tl.scrollTop = tl.scrollHeight;
  }

  if (isPaused) wireApproval(pane, run, proposalMd);

  const usageBtn = pane.querySelector('.lg-usage');
  if (usageBtn) usageBtn.addEventListener('click', () => openUsageModal(run));

  const delBtn = pane.querySelector('.lg-del');
  if (delBtn) delBtn.addEventListener('click', async () => {
    if (!confirm('Delete this run?')) return;
    try {
      await del(`/api/agent-fleet/runs/${run.id}`);
      _runs = _runs.filter(r => r.id !== run.id);
      _selectedId = _runs[0]?.id || null;
      renderList(); renderDetail();
    } catch (e) { toast.error(e.message); }
  });
}
