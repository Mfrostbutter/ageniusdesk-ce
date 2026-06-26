/**
 * Trace waterfall — render OTLP spans as nested horizontal timing bars.
 *
 * Shared by the Observe view (inline) and the per-execution popup
 * (openTraceModal). A span is one bar positioned by its start/duration relative
 * to the whole trace; depth comes from the parent chain. Click a row to reveal
 * its attributes. Pure DOM, no deps beyond api.get.
 */

import { get } from '../api.js';

function esc(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function _depthMap(spans) {
  const byId = {};
  spans.forEach(s => { byId[s.span_id] = s; });
  const cache = {};
  function depth(s) {
    if (cache[s.span_id] != null) return cache[s.span_id];
    let d = 0;
    let cur = s;
    let guard = 0;
    while (cur && cur.parent_id && byId[cur.parent_id] && guard < 64) {
      d += 1;
      cur = byId[cur.parent_id];
      guard += 1;
    }
    cache[s.span_id] = d;
    return d;
  }
  const m = {};
  spans.forEach(s => { m[s.span_id] = depth(s); });
  return m;
}

/** Build the waterfall element for an array of span rows. */
export function buildWaterfall(spans) {
  const wrap = document.createElement('div');
  if (!spans || !spans.length) {
    wrap.innerHTML = '<div class="empty-state"><p>No spans for this trace.</p></div>';
    return wrap;
  }
  const t0 = Math.min(...spans.map(s => s.start_ns));
  const t1 = Math.max(...spans.map(s => s.end_ns));
  const total = Math.max(t1 - t0, 1);
  const dmap = _depthMap(spans);
  const ordered = [...spans].sort((a, b) => (a.start_ns - b.start_ns) || (dmap[a.span_id] - dmap[b.span_id]));

  const head = document.createElement('div');
  head.style.cssText = 'display:flex;justify-content:space-between;font-size:12px;color:var(--text-secondary);margin-bottom:10px';
  head.innerHTML = `<span>${spans.length} spans</span><span>total ${((t1 - t0) / 1e6).toFixed(1)} ms</span>`;
  wrap.appendChild(head);

  ordered.forEach(s => {
    const leftPct = ((s.start_ns - t0) / total) * 100;
    const widthPct = Math.max(((s.end_ns - s.start_ns) / total) * 100, 0.6);
    const isErr = s.status === 'ERROR';
    const barColor = isErr ? 'var(--error)' : 'var(--accent)';
    const indent = (dmap[s.span_id] || 0) * 14;

    // n8n names every node span "node.execute"; the useful label is in the
    // attributes (n8n.node.name / n8n.workflow.name). Fall back to the span name.
    const a = s.attributes || {};
    const label = a['n8n.node.name'] || a['n8n.workflow.name'] || s.name;
    const kindHint = a['n8n.node.type'] || s.name;

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:3px 0;cursor:pointer';
    row.innerHTML = `
      <div style="flex:0 0 230px;min-width:0;padding-left:${indent}px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px;color:var(--text-primary)" title="${esc(label)} (${esc(kindHint)})">
        ${isErr ? '<span style="color:var(--error)">●</span> ' : ''}${esc(label)}
      </div>
      <div style="flex:1;position:relative;height:16px;background:var(--bg-input,rgba(255,255,255,.04));border-radius:3px">
        <div style="position:absolute;left:${leftPct}%;width:${widthPct}%;top:2px;height:12px;background:${barColor};border-radius:3px;min-width:2px"></div>
      </div>
      <div style="flex:0 0 70px;text-align:right;font-size:11px;color:var(--text-secondary);font-family:var(--font-mono)">${(s.duration_ms || 0).toFixed(1)}ms</div>
    `;

    const detail = document.createElement('div');
    detail.style.cssText = 'display:none;margin:2px 0 8px 12px;padding:8px 10px;background:var(--bg-void,rgba(0,0,0,.2));border-left:2px solid var(--border-dim);border-radius:4px;font-size:11px;font-family:var(--font-mono);color:var(--text-secondary);max-height:220px;overflow:auto';
    const attrs = s.attributes || {};
    const attrRows = Object.keys(attrs).sort().map(k => {
      const v = typeof attrs[k] === 'object' ? JSON.stringify(attrs[k]) : attrs[k];
      return `<div><span style="color:var(--text-dim)">${esc(k)}</span> = ${esc(v)}</div>`;
    }).join('') || '<div style="color:var(--text-dim)">no attributes</div>';
    detail.innerHTML = `<div style="margin-bottom:6px">status <strong style="color:${isErr ? 'var(--error)' : 'var(--success,#34d399)'}">${esc(s.status || 'UNSET')}</strong> · span ${esc((s.span_id || '').slice(0, 8))} · parent ${esc((s.parent_id || '—').slice(0, 8))}</div>${attrRows}`;

    row.addEventListener('click', () => { detail.style.display = detail.style.display === 'none' ? '' : 'none'; });
    wrap.appendChild(row);
    wrap.appendChild(detail);
  });
  return wrap;
}

/**
 * Open a trace in a modal. Pass `execId` to resolve via the execution, or
 * `traceId` directly. Handles the "no trace captured yet" case gracefully.
 */
export async function openTraceModal({ execId = '', traceId = '', title = 'Execution trace' } = {}) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.55);padding:24px';
  const card = document.createElement('div');
  card.style.cssText = 'width:100%;max-width:840px;max-height:82vh;overflow:auto;background:var(--bg-elevated,#1a1d24);border:1px solid var(--border,#2a2f3a);border-radius:12px;padding:20px 22px;box-shadow:0 16px 56px rgba(0,0,0,.5)';
  card.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <h2 style="margin:0;font-size:15px">${esc(title)}</h2>
      <button class="btn btn-sm btn-ghost agd-trace-close" style="font-size:16px;padding:2px 9px">&times;</button>
    </div>
    <div class="agd-trace-body"><div class="spinner"></div></div>`;
  overlay.appendChild(card);

  const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  card.querySelector('.agd-trace-close').addEventListener('click', close);
  document.addEventListener('keydown', onKey);
  document.body.appendChild(overlay);

  const bodyEl = card.querySelector('.agd-trace-body');
  try {
    const path = execId
      ? `/api/otel/by-execution/${encodeURIComponent(execId)}`
      : `/api/otel/traces/${encodeURIComponent(traceId)}`;
    const data = await get(path);
    const spans = data.spans || [];
    if (!spans.length) {
      bodyEl.innerHTML = `
        <div class="empty-state">
          <p>No trace captured for this execution yet.</p>
          <p style="font-size:12px;color:var(--text-dim)">Traces appear once n8n's OpenTelemetry export is on and the run executes. See the Observe view for setup.</p>
        </div>`;
      return;
    }
    bodyEl.innerHTML = '';
    bodyEl.appendChild(buildWaterfall(spans));
  } catch (e) {
    bodyEl.innerHTML = `<div class="empty-state"><p>Failed to load trace: ${esc(e.message)}</p></div>`;
  }
}
