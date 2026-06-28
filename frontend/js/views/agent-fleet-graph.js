/**
 * In-app live graph visualization for a LangGraph run.
 *
 * Renders an agent's topology (from /api/langgraph/agents/{id}/graph) as a small
 * top-to-bottom SVG DAG and lights up nodes from the run's langgraph:run event
 * stream: pending -> current (pulsing) -> done. No dependencies; the SVG is a
 * string the detail pane drops in and re-renders on every event.
 */

const NODE_W = 132;
const NODE_H = 34;
const ROW_H = 68;   // center-to-center vertical spacing between depth rows
const COL_W = 152;  // horizontal spacing between sibling nodes in a row
const PAD = 18;

// Fixed palette (not theme vars): the dark tokens render invisibly on the panel
// background, so pending nodes must use explicit, legible colors.
const C = {
  pendingStroke: '#5a6678', pendingFill: 'rgba(148,163,184,0.08)', pendingText: '#aeb8c7',
  doneStroke: '#34d399', doneFill: 'rgba(52,211,153,0.10)', doneText: '#34d399',
  curStroke: '#38bdf8', curFill: 'rgba(56,189,248,0.16)', curText: '#e5e7eb',
  errStroke: '#ef4444', errFill: 'rgba(239,68,68,0.12)', errText: '#fca5a5',
  edge: '#5a6678', edgeHot: '#38bdf8',
};

function esc(s) { const e = document.createElement('span'); e.textContent = s == null ? '' : String(s); return e.innerHTML; }

function pretty(id) {
  if (id === '__start__') return 'START';
  if (id === '__end__') return 'END';
  return id;
}

/**
 * Derive which nodes have run from the event log. Returns
 * { done:Set, current:string|null, errored:bool, reachedEnd:bool }.
 *
 * The runner stamps the real LangGraph node id on every event (`ev.node`), so we
 * light the EXACT node regardless of the message's name. For runs recorded before
 * that field existed (`ev.node` absent) we fall back to the old name/phase
 * heuristics so historical runs still replay.
 */
export function computeStates(events, nodeIds) {
  const idSet = new Set(nodeIds);
  const lower = nodeIds.map((n) => n.toLowerCase());
  const findLike = (subs) => nodeIds.find((_, i) => subs.some((s) => lower[i].includes(s))) || null;
  const seq = [];
  const push = (n) => { if (n && idSet.has(n) && seq[seq.length - 1] !== n) seq.push(n); };
  let errored = false;

  for (const ev of events || []) {
    if (!ev) continue;
    // The authoritative node id, when present (new runs). Null for legacy events.
    const node = ev.node && idSet.has(ev.node) ? ev.node : null;
    switch (ev.phase) {
      case 'started': push('__start__'); break;
      case 'node': push(node || ev.label); break;
      case 'node_light': push(node); break;                 // unnamed message -> light its node only
      case 'tool_call': push(node || findLike(['tool'])); break;
      case 'tool_result': push(node || findLike(['tool'])); break;
      case 'thinking': push(node || findLike(['investigate', 'triage', 'ingest', 'plan'])); break;
      case 'awaiting_approval': push(node || findLike(['review', 'approval', 'approve'])); break;
      case 'resumed': push(node || findLike(['stage', 'apply', 'finalize', 'remediate'])); break;
      case 'final': push(node); push('__end__'); break;
      case 'error': errored = true; if (node) push(node); break;
      default: break;
    }
  }
  const current = seq.length ? seq[seq.length - 1] : null;
  const done = new Set(seq.slice(0, -1));
  return { done, current, errored, reachedEnd: current === '__end__' };
}

/** Shortest-path (BFS) layering from __start__. Cycle-safe: first visit wins, so
 * a back-edge (e.g. a revise->assemble loop) never inflates depths the way a
 * longest-path relaxation does. */
function layout(nodes, edges) {
  const adj = {};
  nodes.forEach((n) => { adj[n] = []; });
  for (const e of edges) if (adj[e.source]) adj[e.source].push(e.target);

  const depth = {};
  depth['__start__'] = 0;
  const queue = ['__start__'];
  while (queue.length) {
    const n = queue.shift();
    for (const t of adj[n] || []) {
      if (depth[t] === undefined) { depth[t] = depth[n] + 1; queue.push(t); }
    }
  }
  // Any node not reachable from start (shouldn't happen) lands at the top.
  nodes.forEach((n) => { if (depth[n] === undefined) depth[n] = 0; });

  const rows = {};
  for (const n of nodes) { (rows[depth[n]] = rows[depth[n]] || []).push(n); }
  const depths = Object.keys(rows).map(Number).sort((a, b) => a - b);
  const maxRow = Math.max(...depths.map((d) => rows[d].length));
  const width = Math.max(maxRow * COL_W, COL_W) + PAD * 2;
  const height = depths.length * ROW_H + PAD * 2;

  const pos = {};
  for (const d of depths) {
    const row = rows[d];
    row.forEach((n, i) => {
      const x = width / 2 + (i - (row.length - 1) / 2) * COL_W;
      const y = PAD + d * ROW_H + NODE_H / 2 + 6;
      pos[n] = { x, y };
    });
  }
  return { pos, width, height, depth };
}

function edgePath(s, t, back) {
  const h2 = NODE_H / 2;
  if (!back) {
    const sy = s.y + h2, ty = t.y - h2, my = (sy + ty) / 2;
    return `M ${s.x} ${sy} C ${s.x} ${my}, ${t.x} ${my}, ${t.x} ${ty}`;
  }
  // back / same-row edge: bow out to the right
  const sx = s.x + NODE_W / 2, tx = t.x + NODE_W / 2, bow = 46;
  return `M ${sx} ${s.y} C ${sx + bow} ${s.y}, ${tx + bow} ${t.y}, ${tx} ${t.y}`;
}

/** Build the full SVG string for one run. */
export function renderGraphSvg(topology, events) {
  if (!topology || !Array.isArray(topology.nodes) || !topology.nodes.length) return '';
  const { nodes, edges } = topology;
  const { pos, width, height, depth } = layout(nodes, edges);
  const { done, current, errored } = computeStates(events, nodes);

  const reached = (n) => done.has(n) || n === current;

  const edgeSvg = edges.map((e) => {
    const s = pos[e.source], t = pos[e.target];
    if (!s || !t) return '';
    const back = depth[e.target] <= depth[e.source];
    const hot = reached(e.source) && reached(e.target);
    const color = hot ? C.edgeHot : C.edge;
    return `<path d="${edgePath(s, t, back)}" fill="none" stroke="${color}" stroke-width="${hot ? 2 : 1.2}"
      marker-end="url(#lg-arrow${hot ? '-hot' : ''})" opacity="${hot ? 0.95 : 0.75}" />`;
  }).join('');

  const nodeSvg = nodes.map((n) => {
    const p = pos[n];
    let stroke = C.pendingStroke, fill = C.pendingFill, text = C.pendingText, cls = '';
    if (n === current) {
      if (errored) { stroke = C.errStroke; fill = C.errFill; text = C.errText; }
      else { stroke = C.curStroke; fill = C.curFill; text = C.curText; cls = 'lg-node-cur'; }
    } else if (done.has(n)) { stroke = C.doneStroke; fill = C.doneFill; text = C.doneText; }
    const term = (n === '__start__' || n === '__end__');
    const w = term ? 78 : NODE_W;
    const x = p.x - w / 2, y = p.y - NODE_H / 2;
    const rx = term ? NODE_H / 2 : 9;
    const checkmark = done.has(n) && !term
      ? `<text x="${x + w - 12}" y="${p.y + 4}" font-size="12" fill="${C.doneStroke}">✓</text>` : '';
    return `<g class="${cls}">
      <rect x="${x}" y="${y}" width="${w}" height="${NODE_H}" rx="${rx}" ry="${rx}"
        fill="${fill}" stroke="${stroke}" stroke-width="${n === current ? 2 : 1.2}" />
      <text x="${p.x}" y="${p.y + 4}" text-anchor="middle" font-size="12"
        font-family="var(--font-mono)" font-weight="${term ? 700 : 600}" fill="${text}">${esc(pretty(n))}</text>
      ${checkmark}
    </g>`;
  }).join('');

  return `
    <svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}"
         style="max-width:100%;height:auto;display:block;margin:0 auto" preserveAspectRatio="xMidYMin meet">
      <defs>
        <marker id="lg-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="#5a6678" /></marker>
        <marker id="lg-arrow-hot" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="${C.edgeHot}" /></marker>
      </defs>
      ${edgeSvg}
      ${nodeSvg}
    </svg>`;
}
