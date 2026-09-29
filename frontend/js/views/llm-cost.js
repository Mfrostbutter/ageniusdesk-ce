// LLM Cost view: spend, quotas, history, attribution, sources, devices, pricing, settings.
import { get, post, put, del, onEvent } from '../api.js';
import { esc, attr } from '../lib/html.js';
import { success, error as toastError } from '../components/toast.js';

const TABS = [
  ['pulse', 'Pulse'], ['quota', 'Quota'], ['spend', 'Spend'], ['heat', 'Heat'], ['attributed', 'Attributed'],
  ['sources', 'Sources'], ['devices', 'Devices'], ['pricing', 'Pricing'], ['settings', 'Settings'],
];
const TAB_KEY = 'agd_llm_cost_tab';
const TZ_MIN = -new Date().getTimezoneOffset();
const LIVE_TABS = new Set(['pulse', 'quota', 'spend']);

let root = null;
let tab = 'pulse';
let timer = null;
let unsubs = [];
let reloadPending = null;
let heatPick = null;
let attrDays = 30;
let types = [];

// ── formatting ──────────────────────────────────────────────────────────────

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
function money(v, digits) {
  if (!isNum(v)) return '--';
  const d = digits ?? (Math.abs(v) >= 100 ? 0 : 2);
  return '$' + v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}
function compact(v) {
  if (!isNum(v)) return '--';
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (a >= 1e3) return (v / 1e3).toFixed(1) + 'k';
  return String(Math.round(v));
}
function duration(sec) {
  if (!isNum(sec)) return '';
  if (sec >= 86400) return `${Math.floor(sec / 86400)}d ${Math.floor((sec % 86400) / 3600)}h`;
  if (sec >= 3600) return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
  if (sec >= 60) return `${Math.floor(sec / 60)}m`;
  return `${Math.max(0, Math.floor(sec))}s`;
}
function localTime(epoch) {
  return isNum(epoch) && epoch > 0 ? new Date(epoch * 1000).toLocaleString() : '--';
}
function severity(pct) {
  if (!isNum(pct)) return 'none';
  return pct >= 90 ? 'crit' : pct >= 75 ? 'warn' : 'ok';
}
const est = (flag) => (flag ? ' <span class="lc-tag" title="Estimated from tokens x price book">EST</span>' : '');
const spendOf = (src, w) => (src.spend || []).find(s => s.window === w) || null;
const counterOf = (src, k) => (src.counters || []).find(c => c.key === k) || null;

// ── chrome ──────────────────────────────────────────────────────────────────

const STYLE = `
.lc-tabs{display:flex;gap:2px;border-bottom:1px solid var(--border-dim);margin-bottom:16px;flex-wrap:wrap}
.lc-grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(180px,1fr))}
.lc-tile{background:var(--bg-panel);border:1px solid var(--border-dim);border-left:3px solid var(--lc-acc,var(--accent));border-radius:var(--radius);padding:12px 14px;cursor:default}
.lc-tile.click{cursor:pointer}.lc-tile.click:hover{border-color:var(--border-mid)}
.lc-tile.dim{opacity:.55}
.lc-tile .n{font-size:11px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.5px}
.lc-tile .v{font-size:24px;font-weight:700;margin:4px 0;font-family:var(--font-mono)}
.lc-tile .s{font-size:11px;color:var(--text-dim)}
.lc-tag{display:inline-block;font-size:9px;font-weight:700;letter-spacing:.5px;padding:1px 5px;border-radius:4px;background:var(--info-glow);color:var(--info);vertical-align:middle}
.lc-tag.stale{background:var(--warning-glow);color:var(--warning)}
.lc-tag.bad{background:var(--error-glow);color:var(--error)}
.lc-pulse{display:grid;grid-template-columns:260px 1fr;gap:16px}
@media (max-width:860px){.lc-pulse{grid-template-columns:1fr}}
.lc-gauge text{font-family:var(--font-mono)}
.lc-ring-bg{stroke:var(--border-mid)}.lc-ring{stroke:var(--success);transition:stroke-dashoffset .6s ease}
.lc-ring.warn{stroke:var(--warning)}.lc-ring.crit{stroke:var(--error)}
.lc-qrow{display:grid;grid-template-columns:minmax(160px,1.2fr) 3fr 90px;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--border-dim)}
.lc-qrow.stale{opacity:.6}
.lc-track{height:10px;background:var(--bg-input);border-radius:5px;overflow:hidden}
.lc-fill{height:100%;background:var(--success);border-radius:5px}
.lc-fill.warn{background:var(--warning)}.lc-fill.crit{background:var(--error)}
.lc-num{font-family:var(--font-mono);font-size:18px;font-weight:700;text-align:right}
.lc-sub{font-size:11px;color:var(--text-dim)}
.lc-led{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--text-dim);margin-right:6px}
.lc-led.ok{background:var(--success)}.lc-led.stale{background:var(--warning)}.lc-led.error{background:var(--error)}
.lc-table{width:100%;border-collapse:collapse;font-size:12px}
.lc-table th{text-align:left;color:var(--text-dim);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--border-dim)}
.lc-table td{padding:6px 8px;border-bottom:1px solid var(--border-dim)}
.lc-table td.r,.lc-table th.r{text-align:right;font-family:var(--font-mono)}
.lc-alert{padding:8px 12px;border-radius:var(--radius);margin-bottom:6px;font-size:13px;background:var(--warning-glow);border-left:3px solid var(--warning)}
.lc-alert.critical,.lc-alert.error{background:var(--error-glow);border-left-color:var(--error)}
.lc-empty{padding:28px;text-align:center;color:var(--text-dim);font-size:13px}
.lc-form{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px 14px;align-items:end}
.lc-form label{display:block;font-size:11px;color:var(--text-secondary);margin-bottom:4px}
.lc-form .full{grid-column:1/-1}
.lc-code{font-family:var(--font-mono);font-size:11px;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:6px;padding:8px 10px;white-space:pre-wrap;word-break:break-all}
.lc-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.lc-chart text{font-family:var(--font-mono);fill:var(--text-dim);font-size:10px}
.lc-heat-cell{fill:var(--bg-input)}
`;

function ensureStyle() {
  if (document.getElementById('lc-style')) return;
  const s = document.createElement('style');
  s.id = 'lc-style';
  s.textContent = STYLE;
  document.head.appendChild(s);
}

function stopTimers() {
  if (timer) { clearInterval(timer); timer = null; }
  unsubs.forEach(u => u());
  unsubs = [];
}

function mounted() {
  return root && document.body.contains(root);
}

function scheduleReload() {
  if (reloadPending || !LIVE_TABS.has(tab)) return;
  reloadPending = setTimeout(() => { reloadPending = null; if (mounted()) renderTab(); }, 800);
}

export async function render(container) {
  stopTimers();
  ensureStyle();
  try { tab = localStorage.getItem(TAB_KEY) || 'pulse'; } catch { /* private mode */ }
  if (!TABS.some(([id]) => id === tab)) tab = 'pulse';
  const opt = (window.__viewOpts || {}).tab;
  if (opt && TABS.some(([id]) => id === opt)) tab = opt;
  container.innerHTML = `
    <div class="section-header">
      <div>
        <h2 class="section-title">LLM Cost</h2>
        <span style="font-size:12px;color:var(--text-secondary)">Billed spend, quotas, and burn across providers and workstations, plus what n8n and agents consumed. <span style="color:var(--text-dim)">-- means unknown, never zero. EST is estimated from tokens.</span></span>
      </div>
    </div>
    <div class="lc-tabs">${TABS.map(([id, label]) => `<button class="tab-btn" data-lc-tab="${id}">${label}</button>`).join('')}</div>
    <div id="lc-body"></div>`;
  root = container.querySelector('#lc-body');
  container.querySelectorAll('[data-lc-tab]').forEach(b => {
    b.onclick = () => { tab = b.dataset.lcTab; try { localStorage.setItem(TAB_KEY, tab); } catch { /* private mode */ } renderTab(); };
  });
  unsubs.push(onEvent('llm_cost:update', scheduleReload));
  unsubs.push(onEvent('llm_cost:alerts', scheduleReload));
  timer = setInterval(() => { if (!mounted()) { stopTimers(); return; } if (LIVE_TABS.has(tab)) renderTab(); }, 30000);
  await renderTab();
}

async function renderTab() {
  if (!root) return;
  document.querySelectorAll('[data-lc-tab]').forEach(b => b.classList.toggle('active', b.dataset.lcTab === tab));
  const fn = { pulse: renderPulse, quota: renderQuota, spend: renderSpend, heat: renderHeat,
    attributed: renderAttributed, sources: renderSources, devices: renderDevices, pricing: renderPricing,
    settings: renderSettings }[tab];
  try {
    await fn(root);
  } catch (e) {
    root.innerHTML = `<div class="card" style="color:var(--error)">Failed to load: ${esc(e.message)}</div>`;
  }
}

// ── pulse ───────────────────────────────────────────────────────────────────

function gauge(worst) {
  const r = 78, c = 2 * Math.PI * r;
  const pct = worst && isNum(worst.pct) ? Math.max(0, Math.min(100, worst.pct)) : null;
  const off = pct === null ? c : c * (1 - pct / 100);
  const label = worst ? `${worst.source} · ${worst.label}` : 'No quota data';
  const bits = [];
  if (worst && isNum(worst.remainingUsd)) bits.push(`${money(worst.remainingUsd)} left`);
  if (worst && worst.resetsInSec) bits.push(`resets in ${duration(worst.resetsInSec)}`);
  return `
    <svg class="lc-gauge" viewBox="0 0 200 200" width="220" height="220" role="img" aria-label="Worst quota">
      <circle cx="100" cy="100" r="${r}" fill="none" stroke-width="14" class="lc-ring-bg"/>
      <circle cx="100" cy="100" r="${r}" fill="none" stroke-width="14" stroke-linecap="round"
        class="lc-ring ${severity(pct)}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"
        transform="rotate(-90 100 100)"/>
      <text x="100" y="104" text-anchor="middle" font-size="40" font-weight="700" fill="var(--text-primary)">${pct === null ? '--' : Math.round(pct) + '%'}</text>
      <text x="100" y="130" text-anchor="middle" font-size="10" fill="var(--text-secondary)">${esc(label.toUpperCase().slice(0, 30))}</text>
    </svg>
    <div class="lc-sub" style="text-align:center">${esc(bits.join(' · '))}${worst && worst.stale ? ' <span class="lc-tag stale">STALE</span>' : ''}</div>`;
}

async function renderPulse(el) {
  const state = await get('/api/llm-cost/state');
  const ov = state.overview;
  const burn = ov.burnPerHour;
  const alerts = ov.alerts || [];
  el.innerHTML = `
    <div class="lc-pulse">
      <div class="card" style="display:flex;flex-direction:column;align-items:center;justify-content:center">${gauge(ov.worstQuota)}</div>
      <div>
        <div class="lc-grid">
          <div class="lc-tile"><div class="n">Spend today</div><div class="v">${money(ov.spend.today)}</div><div class="s">UTC day${est(ov.spendEstimated.today)}</div></div>
          <div class="lc-tile"><div class="n">Month to date</div><div class="v">${money(ov.spend.mtd)}</div><div class="s">UTC month${est(ov.spendEstimated.mtd)}</div></div>
          <div class="lc-tile"><div class="n">Burn / hour</div><div class="v">${burn === null ? '--' : burn > 0 ? compact(burn) : 'IDLE'}</div><div class="s">tokens, trailing 60 min</div></div>
          <div class="lc-tile"><div class="n">Sources healthy</div><div class="v">${ov.healthyCount}/${ov.sourceCount}</div><div class="s">${state.leader ? 'this process polls' : 'polled by another process'}</div></div>
        </div>
        <div class="card" style="margin-top:12px">
          <div class="card-title" style="margin-bottom:8px">Alerts</div>
          ${alerts.length ? alerts.map(a => `<div class="lc-alert ${attr(a.level)}"><strong>${esc(a.level.toUpperCase())}</strong> ${esc(a.title)}${a.detail ? ` <span class="lc-sub">${esc(a.detail)}</span>` : ''}</div>`).join('') : '<div class="lc-sub">Nothing needs attention.</div>'}
        </div>
      </div>
    </div>
    <div class="card" style="margin-top:12px">
      <div class="card-title" style="margin-bottom:8px">Sources</div>
      ${state.sources.length ? state.sources.map(s => `
        <div class="lc-row" style="padding:6px 0;border-bottom:1px solid var(--border-dim)">
          <span class="lc-led ${attr(s.health)}"></span><strong style="min-width:160px">${esc(s.displayName)}</strong>
          <span class="lc-sub">${esc(s.health.toUpperCase())}${s.error ? ' · ' + esc(s.error) : s.meta && s.meta.topModel ? ' · ' + esc(s.meta.topModel) : ''}</span>
          <span class="lc-sub" style="margin-left:auto">${s.health === 'unconfigured' || s.health === 'disabled' ? '' : duration(s.ageSec) + ' ago'}</span>
        </div>`).join('') : `<div class="lc-empty">No sources yet. Add one under Sources, or create a device token under Devices to push Claude Code usage.</div>`}
    </div>`;
}

// ── quota ───────────────────────────────────────────────────────────────────

async function renderQuota(el) {
  const state = await get('/api/llm-cost/state');
  const rows = [];
  state.sources.forEach(s => (s.quotas || []).forEach(q => rows.push({ s, q })));
  rows.sort((a, b) => (b.q.pct ?? -1) - (a.q.pct ?? -1));
  if (!rows.length) { el.innerHTML = '<div class="card lc-empty">No quota-capable sources. Budgets, key limits, credits, and Claude plan limits show here.</div>'; return; }
  el.innerHTML = `<div class="card">${rows.map(({ s, q }) => {
    const pct = q.pct;
    let extra = '';
    if (isNum(q.used) && isNum(q.limit)) {
      extra = (q.unit === 'usd' || q.unit === 'credits') ? `${money(q.limit - q.used)} left of ${money(q.limit)}`
        : q.unit !== 'pct' ? `${compact(q.used)} / ${compact(q.limit)}` : '';
    }
    return `<div class="lc-qrow ${q.stale ? 'stale' : ''}">
      <div><strong>${esc(s.displayName)}</strong><div class="lc-sub">${esc(q.label)}${q.stale ? ' <span class="lc-tag stale">STALE</span>' : ''}${extra ? ' · ' + esc(extra) : ''}</div></div>
      <div class="lc-track"><div class="lc-fill ${severity(pct)}" style="width:${isNum(pct) ? Math.max(0, Math.min(100, pct)) : 0}%"></div></div>
      <div><div class="lc-num">${isNum(pct) ? Math.round(pct) + '%' : '--'}</div><div class="lc-sub" style="text-align:right">${q.resetsInSec ? 'resets ' + duration(q.resetsInSec) : ''}</div></div>
    </div>`;
  }).join('')}</div>`;
}

// ── spend ───────────────────────────────────────────────────────────────────

function barChart(series, endDay, slots = 30) {
  const w = 600, h = 140, gap = 3;
  const bw = (w - gap * (slots - 1)) / slots;
  const by = {};
  (series || []).forEach(p => { by[p.day] = p; });
  const end = endDay ? new Date(endDay + 'T00:00:00Z') : new Date();
  const days = [];
  for (let i = slots - 1; i >= 0; i--) {
    const d = new Date(end.getTime() - i * 86400000);
    days.push(d.toISOString().slice(0, 10));
  }
  const max = Math.max(0.0001, ...days.map(d => (by[d] ? by[d].amount : 0)));
  const bars = days.map((d, i) => {
    const x = i * (bw + gap);
    const p = by[d];
    if (!p) return `<rect x="${x.toFixed(1)}" y="${h - 2}" width="${bw.toFixed(1)}" height="2" rx="1" fill="var(--border-mid)"><title>${d} · no data</title></rect>`;
    const bh = Math.max(2, (p.amount / max) * (h - 16));
    return `<rect x="${x.toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="var(--accent)" opacity="${(0.45 + 0.55 * p.amount / max).toFixed(2)}"><title>${d} · ${money(p.amount)}${p.estimated ? ' (est)' : ''}</title></rect>`;
  }).join('');
  return `<svg class="lc-chart" viewBox="0 0 ${w} ${h + 14}" width="100%" preserveAspectRatio="none" style="max-height:180px">${bars}
    <text x="0" y="${h + 12}">${days[0].slice(5)}</text><text x="${w}" y="${h + 12}" text-anchor="end">${days[days.length - 1].slice(5)}</text></svg>`;
}

async function renderSpend(el) {
  const [state, daily] = await Promise.all([get('/api/llm-cost/state'),
    get(`/api/llm-cost/spend/daily?days=30&tz_offset_min=${TZ_MIN}`)]);
  const sources = [...state.sources].sort((a, b) => ((spendOf(b, 'today') || {}).amount || 0) - ((spendOf(a, 'today') || {}).amount || 0));
  const total = (daily.series || []).reduce((s, p) => s + p.amount, 0);
  el.innerHTML = `
    <div class="lc-grid">${sources.map(s => {
      const t = spendOf(s, 'today'), m = spendOf(s, 'mtd'), tok = counterOf(s, 'total_tokens');
      const sub = [m ? 'MTD ' + money(m.amount) : '', tok ? compact(tok.value) + ' tok' : ''].filter(Boolean).join(' · ');
      return `<div class="lc-tile click ${s.health !== 'ok' ? 'dim' : ''}" style="--lc-acc:${attr(s.accent)}" data-drill="${attr(s.sourceId)}">
        <div class="n">${esc(s.displayName)}</div><div class="v">${t ? money(t.amount) : '--'}</div>
        <div class="s">${esc(sub || s.health.toUpperCase())}${est(t && t.estimated)}${s.health === 'stale' ? ' <span class="lc-tag stale">STALE</span>' : ''}</div></div>`;
    }).join('') || '<div class="lc-empty">No sources configured.</div>'}</div>
    <div class="card" style="margin-top:12px">
      <div class="card-header"><div class="card-title">Last 30 days</div><div class="lc-sub">${money(total)} sampled · last reading per day of each source's daily spend</div></div>
      ${daily.series && daily.series.length ? barChart(daily.series, daily.endDay) : '<div class="lc-empty">Collecting. History builds as sources are polled.</div>'}
    </div>
    <div id="lc-drill"></div>`;
  el.querySelectorAll('[data-drill]').forEach(t => { t.onclick = () => drill(t.dataset.drill); });
}

function modelTable(models) {
  if (!models || !models.length) return '<div class="lc-sub">No per-model data.</div>';
  return `<table class="lc-table"><thead><tr><th>Model</th><th>Window</th><th class="r">Input</th><th class="r">Output</th><th class="r">Cache</th><th class="r">Cost</th><th>Basis</th></tr></thead><tbody>
    ${models.map(m => `<tr><td>${esc(m.model)}</td><td>${esc(m.window)}</td><td class="r">${compact(m.inputTokens)}</td><td class="r">${compact(m.outputTokens)}</td><td class="r">${compact(m.cacheTokens)}</td><td class="r">${money(m.cost, 4)}</td><td>${m.costBasis === 'actual' ? 'actual' : m.costBasis === 'unpriced' ? '<span class="lc-tag bad">UNPRICED</span>' : '<span class="lc-tag">EST</span>'}</td></tr>`).join('')}
  </tbody></table>`;
}

async function drill(sourceId) {
  const host = document.getElementById('lc-drill');
  if (!host) return;
  host.innerHTML = '<div class="card lc-sub">Loading…</div>';
  const d = await get(`/api/llm-cost/sources/${encodeURIComponent(sourceId)}/detail`);
  const detail = d.detail || {};
  const dims = detail.groupDims || [];
  const trend = (detail.trend || []).map(t => ({ day: t.date, amount: t.amount }));
  host.innerHTML = `<div class="card" style="margin-top:12px">
    <div class="card-header"><div class="card-title">${esc(d.displayName)}</div><button class="btn btn-sm btn-ghost" id="lc-drill-close">Close</button></div>
    ${d.detailStale ? '<div class="lc-sub" style="margin-bottom:8px"><span class="lc-tag stale">STALE</span> breakdown from the last good poll</div>' : ''}
    ${d.meta && d.meta.activityThrough ? `<div class="lc-sub" style="margin-bottom:8px">Account activity covers completed UTC days through ${esc(d.meta.activityThrough)}.</div>` : ''}
    ${trend.length ? `<div class="lc-sub">Daily cost (${esc(detail.trendBasis || 'actual')})</div>${barChart(trend, trend[trend.length - 1].day, Math.min(31, Math.max(7, trend.length)))}` : ''}
    <div style="margin-top:12px">${modelTable(detail.models)}</div>
    ${dims.length ? `<div class="lc-row" style="margin-top:14px">${dims.map((g, i) => `<button class="tab-btn ${i === 0 ? 'active' : ''}" data-dim="${attr(g.key)}">${esc(g.label)} <span class="lc-sub">(${esc(g.basis)})</span></button>`).join('')}</div><div id="lc-dim"></div>` : ''}
  </div>`;
  document.getElementById('lc-drill-close').onclick = () => { host.innerHTML = ''; };
  const showDim = (key) => {
    host.querySelectorAll('[data-dim]').forEach(b => b.classList.toggle('active', b.dataset.dim === key));
    const rows = (detail.groups || {})[key] || [];
    document.getElementById('lc-dim').innerHTML = `<table class="lc-table"><thead><tr><th>Name</th><th class="r">Cost</th><th>Basis</th><th class="r">Limit</th><th></th></tr></thead><tbody>
      ${rows.map((g, i) => `<tr><td>${esc(g.name)}${g.disabled ? ' <span class="lc-tag bad">DISABLED</span>' : ''}</td><td class="r">${money(g.cost, 4)}</td><td>${esc(g.costBasis)}</td><td class="r">${isNum(g.limit) ? money(g.limit) : ''}</td><td>${(g.models || []).length ? `<button class="btn btn-sm btn-ghost" data-gm="${i}">Models</button>` : ''}</td></tr>
        <tr id="lc-gm-${i}" style="display:none"><td colspan="5">${modelTable(g.models)}</td></tr>`).join('')}
    </tbody></table>`;
    document.querySelectorAll('[data-gm]').forEach(b => { b.onclick = () => { const r = document.getElementById('lc-gm-' + b.dataset.gm); r.style.display = r.style.display === 'none' ? '' : 'none'; }; });
  };
  host.querySelectorAll('[data-dim]').forEach(b => { b.onclick = () => showDim(b.dataset.dim); });
  if (dims.length) showDim(dims[0].key);
  host.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── heat ────────────────────────────────────────────────────────────────────

async function renderHeat(el) {
  const q = new URLSearchParams({ tz_offset_min: String(TZ_MIN), days: '140' });
  if (heatPick) { q.set('source_id', heatPick.sourceId); q.set('scope', heatPick.scope); q.set('label', heatPick.label); }
  const data = await get(`/api/llm-cost/heatmap?${q}`);
  const cands = data.candidates || [];
  const by = {};
  (data.days || []).forEach(d => { by[d.day] = d.peak; });
  const weeks = 20, cell = 30, gap = 4;
  const end = new Date((data.endDay || new Date().toISOString().slice(0, 10)) + 'T00:00:00Z');
  end.setUTCDate(end.getUTCDate() + (6 - end.getUTCDay()));
  let active = 0, peak = 0;
  const cells = [];
  for (let w = 0; w < weeks; w++) {
    for (let d = 0; d < 7; d++) {
      const cur = new Date(end.getTime() - ((weeks - 1 - w) * 7 + (6 - d)) * 86400000);
      const key = cur.toISOString().slice(0, 10);
      const p = by[key];
      let fill = 'var(--bg-input)', op = 1;
      if (isNum(p)) {
        active++; peak = Math.max(peak, p);
        fill = p >= 90 ? 'var(--error)' : 'var(--accent)';
        op = p >= 90 ? 1 : 0.2 + 0.8 * (p / 90);
      }
      cells.push(`<rect x="${w * (cell + gap)}" y="${d * (cell + gap)}" width="${cell}" height="${cell}" rx="4" fill="${fill}" fill-opacity="${op.toFixed(2)}"><title>${key}${isNum(p) ? ' · ' + Math.round(p) + '%' : ''}</title></rect>`);
    }
  }
  el.innerHTML = `<div class="card">
    <div class="card-header"><div class="card-title">Daily peak quota, 20 weeks</div>
      <div class="lc-row">${cands.slice(0, 8).map((c, i) => `<button class="btn btn-sm ${c.sourceId === data.sourceId && c.scope === data.scope && c.label === data.label ? 'btn-primary' : 'btn-ghost'}" data-heat="${i}">${esc(c.sourceId)} · ${esc(c.label || c.scope)}</button>`).join('')}</div></div>
    ${cands.length ? `<svg viewBox="0 0 ${weeks * (cell + gap) - gap} ${7 * (cell + gap) - gap}" width="100%" style="max-height:300px">${cells.join('')}</svg>
      <div class="lc-sub" style="margin-top:8px">${active} active day${active === 1 ? '' : 's'} · peak ${Math.round(peak)}% · local days</div>`
      : '<div class="lc-empty">No quota history yet. History builds as sources are polled.</div>'}
  </div>`;
  el.querySelectorAll('[data-heat]').forEach(b => { b.onclick = () => { heatPick = cands[Number(b.dataset.heat)]; renderHeat(el); }; });
}

// ── attributed ──────────────────────────────────────────────────────────────

function rollTable(rows, cols) {
  if (!rows || !rows.length) return '<div class="lc-sub">Nothing in this window.</div>';
  return `<table class="lc-table"><thead><tr>${cols.map(c => `<th class="${c.r ? 'r' : ''}">${c.h}</th>`).join('')}</tr></thead><tbody>
    ${rows.map(r => `<tr>${cols.map(c => `<td class="${c.r ? 'r' : ''}">${c.f(r)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

async function renderAttributed(el) {
  const d = await get(`/api/llm-cost/attributed?days=${attrDays}&tz_offset_min=${TZ_MIN}`);
  const o = d.otel;
  const costCell = (r) => money(r.cost, 4) + (r.estimated ? ' <span class="lc-tag">EST</span>' : '') + (r.unpricedCalls ? ` <span class="lc-tag bad" title="calls with no price">${r.unpricedCalls} unpriced</span>` : '');
  const lg = {};
  (d.internal.langgraph.rows || []).forEach(r => {
    const k = r.agentId + '|' + r.model;
    lg[k] = lg[k] || { agent: r.agentId, model: r.model, cost: 0, tokens: 0, runs: 0, unpriced: 0 };
    lg[k].cost += r.cost; lg[k].tokens += r.tokens; lg[k].runs += r.runs; lg[k].unpriced += r.unpricedRuns;
  });
  const ag = {};
  (d.internal.agentSessions.rows || []).forEach(r => {
    const k = r.agent + '|' + r.model;
    ag[k] = ag[k] || { agent: r.agent, model: r.model, cost: null, tokens: 0, runs: 0 };
    ag[k].tokens += r.tokens; ag[k].runs += r.runs;
    if (isNum(r.cost)) ag[k].cost = (ag[k].cost || 0) + r.cost;
  });
  el.innerHTML = `
    <div class="card lc-row"><span class="lc-sub">Window</span>
      <select id="lc-attr-days">${[7, 14, 30, 60, 90].map(n => `<option value="${n}" ${n === attrDays ? 'selected' : ''}>${n} days</option>`).join('')}</select>
      <span class="lc-sub">Scope: ${esc(d.workspace === 'all' ? 'fleet-wide' : d.workspace)}</span></div>
    <div class="card"><div class="card-header"><div class="card-title">n8n workflows (Observe traces)</div><div class="lc-sub">${o.available ? money(o.total, 2) + ' attributed' : esc(o.reason || 'unavailable')}</div></div>
      ${o.available ? `<div class="grid-2" style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div><div class="lc-sub">By instance</div>${rollTable(o.byInstance, [{ h: 'Instance', f: r => esc(r.name) }, { h: 'Calls', r: 1, f: r => r.calls }, { h: 'Cost', r: 1, f: costCell }])}</div>
        <div><div class="lc-sub">By model</div>${rollTable(o.byModel, [{ h: 'Model', f: r => esc(r.key) }, { h: 'Tokens', r: 1, f: r => compact(r.tokensIn + r.tokensOut) }, { h: 'Cost', r: 1, f: costCell }])}</div>
      </div>
      <div class="lc-sub" style="margin-top:12px">By workflow</div>${rollTable(o.byWorkflow.slice(0, 50), [{ h: 'Workflow', f: r => esc(r.workflowName) }, { h: 'Instance', f: r => esc(r.instanceName) }, { h: 'Calls', r: 1, f: r => r.calls }, { h: 'Tokens', r: 1, f: r => compact(r.tokensIn + r.tokensOut) }, { h: 'Cost', r: 1, f: costCell }])}` : ''}
    </div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">AGD agents</div>
      <div class="lc-sub">LangGraph fleet runs</div>${rollTable(Object.values(lg).sort((a, b) => b.cost - a.cost), [{ h: 'Agent', f: r => esc(r.agent) }, { h: 'Model', f: r => esc(r.model || '--') }, { h: 'Runs', r: 1, f: r => r.runs }, { h: 'Tokens', r: 1, f: r => compact(r.tokens) }, { h: 'Cost', r: 1, f: r => (r.cost > 0 ? money(r.cost, 4) : '--') + (r.unpriced ? ` <span class="lc-tag bad">${r.unpriced} unpriced</span>` : '') }])}
      <div class="lc-sub" style="margin-top:12px">Captured Claude Code agent sessions</div>${rollTable(Object.values(ag), [{ h: 'Agent', f: r => esc(r.agent) }, { h: 'Model', f: r => esc(r.model || '--') }, { h: 'Runs', r: 1, f: r => r.runs }, { h: 'Tokens', r: 1, f: r => compact(r.tokens) }, { h: 'Cost', r: 1, f: r => (isNum(r.cost) ? money(r.cost, 4) + ' <span class="lc-tag">EST</span>' : '--') }])}
    </div>
    <div class="card"><div class="card-header"><div class="card-title">Billed vs attributed</div><div class="lc-sub">${esc(d.reconciliationNote || '')}</div></div>
      ${rollTable(d.reconciliation, [{ h: 'Day', f: r => esc(r.day) }, { h: 'Provider', f: r => esc(r.provider) }, { h: 'Billed', r: 1, f: r => money(r.billed, 2) }, { h: 'Attributed', r: 1, f: r => money(r.attributed, 2) }, { h: 'Unattributed', r: 1, f: r => money(r.unattributed, 2) }, { h: 'Coverage', r: 1, f: r => (isNum(r.coveragePct) ? r.coveragePct + '%' : '--') }])}
    </div>`;
  el.querySelector('#lc-attr-days').onchange = (e) => { attrDays = Number(e.target.value); renderAttributed(el); };
}

// ── sources ─────────────────────────────────────────────────────────────────

function fieldInput(f, value) {
  const id = `lc-f-${f.key}`;
  const v = value !== undefined ? value : f.default;
  if (f.type === 'bool') return `<label><input type="checkbox" id="${id}" ${v ? 'checked' : ''}> ${esc(f.label)}</label>`;
  if (f.type === 'json') return `<div class="full"><label for="${id}">${esc(f.label)} (JSON)</label><textarea id="${id}" rows="3" style="width:100%;font-family:var(--font-mono);font-size:11px">${esc(JSON.stringify(v ?? (f.default ?? null), null, 0))}</textarea></div>`;
  const val = f.type === 'list' ? (Array.isArray(v) ? v.join(', ') : (v || '')) : (v ?? '');
  const ph = f.type === 'secret' ? 'placeholder="$SECRET_NAME"' : '';
  const hint = f.type === 'secret' ? '<div class="lc-sub">A $reference to Secrets or env. Raw keys are refused.</div>' : '';
  return `<div><label for="${id}">${esc(f.label)}</label><input id="${id}" type="${f.type === 'number' ? 'number' : 'text'}" value="${attr(val)}" ${ph} style="width:100%">${hint}</div>`;
}

function readFields(fields) {
  const out = {};
  for (const f of fields) {
    const node = document.getElementById(`lc-f-${f.key}`);
    if (!node) continue;
    if (f.type === 'bool') out[f.key] = node.checked;
    else if (f.type === 'json') {
      try { out[f.key] = JSON.parse(node.value || 'null') ?? f.default; } catch { throw new Error(`${f.label} is not valid JSON`); }
    } else out[f.key] = node.value.trim();
  }
  return out;
}

async function renderSources(el) {
  const [srcs, state, ev, t] = await Promise.all([get('/api/llm-cost/sources'), get('/api/llm-cost/state'),
    get('/api/llm-cost/events?limit=30'), get('/api/llm-cost/types')]);
  types = t.types || [];
  const snap = {};
  state.sources.forEach(s => { snap[s.sourceId] = s; });
  el.innerHTML = `
    <div class="card">
      <div class="card-header"><div class="card-title">Sources</div>
        <div class="lc-row"><button class="btn btn-sm" id="lc-refresh-all">Refresh all</button><button class="btn btn-sm btn-primary" id="lc-add">Add source</button></div></div>
      <div id="lc-editor"></div>
      ${srcs.sources.length ? `<table class="lc-table"><thead><tr><th>Source</th><th>Type</th><th>Status</th><th>Credentials</th><th class="r">Interval</th><th></th></tr></thead><tbody>
        ${srcs.sources.map(s => {
          const cur = snap[s.id] || {};
          const creds = Object.entries(s.secrets || {}).map(([, v]) => v.ref ? `${esc(v.ref)} ${v.resolved ? '<span class="lc-tag">OK</span>' : '<span class="lc-tag bad">UNSET</span>'}` : '').filter(Boolean).join('<br>') || '<span class="lc-sub">none</span>';
          return `<tr><td><span class="lc-led ${attr(cur.health || '')}"></span><strong>${esc(s.display_name)}</strong><div class="lc-sub">${esc(s.id)}</div></td>
            <td>${esc(s.type)}</td><td>${esc((cur.health || '').toUpperCase())}<div class="lc-sub">${esc(cur.error || '')}</div></td>
            <td>${creds}</td><td class="r">${s.type === 'push' ? 'push' : (s.interval_sec || 'default')}</td>
            <td style="white-space:nowrap"><button class="btn btn-sm btn-ghost" data-act="test" data-id="${attr(s.id)}">Test</button>
              <button class="btn btn-sm btn-ghost" data-act="refresh" data-id="${attr(s.id)}">Refresh</button>
              <button class="btn btn-sm btn-ghost" data-act="edit" data-id="${attr(s.id)}">Edit</button>
              <button class="btn btn-sm btn-ghost" data-act="toggle" data-id="${attr(s.id)}">${s.enabled ? 'Disable' : 'Enable'}</button>
              <button class="btn btn-sm btn-danger" data-act="delete" data-id="${attr(s.id)}">Delete</button></td></tr>`;
        }).join('')}</tbody></table>` : '<div class="lc-empty">No sources. Keys set as ANTHROPIC_ADMIN_KEY, OPENAI_ADMIN_KEY, OPENROUTER_KEY, or OPENROUTER_MANAGEMENT_KEY seed sources once on startup.</div>'}
    </div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">Health transitions</div>
      ${rollTable(ev.events, [{ h: 'When', f: r => esc(localTime(r.at)) }, { h: 'Source', f: r => esc(r.sourceId) }, { h: 'Health', f: r => `<span class="lc-led ${attr(r.health)}"></span>${esc(r.health)}` }, { h: 'Detail', f: r => esc(r.detail || '') }])}
      <div class="lc-sub" style="margin-top:8px">History: ${srcs.store.spendSamples} spend and ${srcs.store.quotaSamples} quota samples.</div></div>`;
  el.querySelector('#lc-add').onclick = () => editor(null);
  el.querySelector('#lc-refresh-all').onclick = async () => { await post('/api/llm-cost/refresh', {}); success('Refresh requested'); };
  el.querySelectorAll('[data-act]').forEach(b => {
    b.onclick = async () => {
      const s = srcs.sources.find(x => x.id === b.dataset.id);
      try {
        if (b.dataset.act === 'edit') return editor(s);
        if (b.dataset.act === 'refresh') { await post('/api/llm-cost/refresh', { source_id: s.id }); success('Refresh requested'); return; }
        if (b.dataset.act === 'test') {
          b.disabled = true;
          const r = await post(`/api/llm-cost/sources/${encodeURIComponent(s.id)}/test`, {});
          b.disabled = false;
          r.ok ? success(`${s.display_name}: OK`) : toastError(`${s.display_name}: ${r.error || r.detail || 'failed'}`);
          return;
        }
        if (b.dataset.act === 'toggle') await put(`/api/llm-cost/sources/${encodeURIComponent(s.id)}`, { enabled: !s.enabled });
        if (b.dataset.act === 'delete') {
          if (!confirm(`Delete source ${s.display_name}? History samples are kept until retention prunes them.`)) return;
          await del(`/api/llm-cost/sources/${encodeURIComponent(s.id)}`);
        }
        renderSources(el);
      } catch (e) { b.disabled = false; toastError(e.message); }
    };
  });
}

function editor(source) {
  const host = document.getElementById('lc-editor');
  const creatable = types.filter(t => t.type !== 'push');
  const typeId = source ? source.type : (creatable[0] || {}).type;
  const draw = (tid) => {
    const ty = types.find(t => t.type === tid) || { fields: [] };
    host.innerHTML = `<div class="card" style="background:var(--bg-input)">
      <div class="lc-form">
        <div><label>Type</label><select id="lc-type" ${source ? 'disabled' : ''}>${(source ? types : creatable).map(t => `<option value="${attr(t.type)}" ${t.type === tid ? 'selected' : ''}>${esc(t.displayName)}</option>`).join('')}</select></div>
        <div><label>Display name</label><input id="lc-name" value="${attr(source ? source.display_name : ty.displayName || '')}"></div>
        ${source ? '' : '<div><label>Id (optional)</label><input id="lc-id" placeholder="auto from name"></div>'}
        ${ty.mode === 'push' ? '' : `<div><label>Interval seconds (0 = ${ty.defaultInterval}, min ${ty.minInterval})</label><input id="lc-int" type="number" value="${source ? source.interval_sec : 0}"></div>`}
        <div class="full lc-sub">${esc(ty.description || '')}</div>
        ${ty.fields.map(f => fieldInput(f, source ? source.options[f.key] : undefined)).join('')}
        <div class="full lc-row"><button class="btn btn-primary btn-sm" id="lc-save">${source ? 'Save' : 'Create'}</button><button class="btn btn-ghost btn-sm" id="lc-cancel">Cancel</button></div>
      </div></div>`;
    if (!source) host.querySelector('#lc-type').onchange = (e) => draw(e.target.value);
    host.querySelector('#lc-cancel').onclick = () => { host.innerHTML = ''; };
    host.querySelector('#lc-save').onclick = async () => {
      try {
        const options = readFields(ty.fields);
        const intNode = host.querySelector('#lc-int');
        const body = { display_name: host.querySelector('#lc-name').value.trim(), options, interval_sec: intNode ? Number(intNode.value || 0) : 0 };
        if (source) await put(`/api/llm-cost/sources/${encodeURIComponent(source.id)}`, body);
        else await post('/api/llm-cost/sources', { ...body, type: tid, id: (host.querySelector('#lc-id').value || '').trim() });
        success(source ? 'Source saved' : 'Source created');
        renderSources(root);
      } catch (e) { toastError(e.message); }
    };
  };
  draw(typeId);
}

// ── devices ─────────────────────────────────────────────────────────────────

async function renderDevices(el) {
  const d = await get('/api/llm-cost/devices');
  el.innerHTML = `
    <div class="card"><div class="card-title" style="margin-bottom:6px">Add a workstation</div>
      <div class="lc-sub" style="margin-bottom:10px">A forwarder on each machine that runs Claude Code pushes token usage (priced here) and Claude plan limits. Each device gets its own token, so machines add up instead of overwriting each other.</div>
      <div class="lc-form"><div><label>Device name</label><input id="lc-dev-name" placeholder="macbook"></div>
        <div><label>Push source id</label><input id="lc-dev-src" value="claude-code"></div>
        <div><button class="btn btn-primary btn-sm" id="lc-dev-create">Create device token</button></div></div>
      <div id="lc-dev-out"></div></div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">Devices</div>
      ${rollTable(d.devices, [{ h: 'Name', f: r => esc(r.name) }, { h: 'Source', f: r => esc(r.source_id) }, { h: 'Token', f: r => `<code>${esc(r.token_prefix)}…</code>` },
        { h: 'Last push', f: r => esc(localTime(r.last_seen_at)) + (r.last_host ? ` <span class="lc-sub">${esc(r.last_host)}</span>` : '') },
        { h: 'Status', f: r => (r.revoked_at ? '<span class="lc-tag bad">REVOKED</span>' : '<span class="lc-tag">ACTIVE</span>') },
        { h: '', f: r => (r.revoked_at ? '' : `<button class="btn btn-sm btn-danger" data-revoke="${attr(r.id)}">Revoke</button>`) }])}</div>`;
  el.querySelector('#lc-dev-create').onclick = async () => {
    const name = el.querySelector('#lc-dev-name').value.trim();
    if (!name) { toastError('Name the device'); return; }
    try {
      const r = await post('/api/llm-cost/devices', { name, source_id: el.querySelector('#lc-dev-src').value.trim() || 'claude-code' });
      const blocks = [['macOS (LaunchAgent)', r.install.macos], ['Windows (scheduled task, PowerShell)', r.install.windows], ['Any OS, foreground', r.install.manual]];
      el.querySelector('#lc-dev-out').innerHTML = `<div class="card" style="background:var(--bg-input);margin-top:12px">
        <div style="color:var(--warning);font-size:13px;margin-bottom:8px">Copy this now. The token is shown once; only its hash is stored.</div>
        <div class="lc-sub">Token</div><div class="lc-code">${esc(r.token)}</div>
        ${blocks.map(([label, cmd], i) => `<div class="lc-row" style="margin-top:10px"><span class="lc-sub">${esc(label)}</span><button class="btn btn-sm btn-ghost" data-copy="${i}">Copy</button></div><div class="lc-code">${esc(cmd)}</div>`).join('')}
      </div>`;
      el.querySelectorAll('[data-copy]').forEach(b => { b.onclick = () => navigator.clipboard.writeText(blocks[Number(b.dataset.copy)][1]).then(() => success('Copied')); });
      const out = el.querySelector('#lc-dev-out').innerHTML;
      await renderDevices(el);
      el.querySelector('#lc-dev-out').innerHTML = out;
      el.querySelectorAll('[data-copy]').forEach(b => { b.onclick = () => navigator.clipboard.writeText(blocks[Number(b.dataset.copy)][1]).then(() => success('Copied')); });
    } catch (e) { toastError(e.message); }
  };
  el.querySelectorAll('[data-revoke]').forEach(b => {
    b.onclick = async () => {
      if (!confirm('Revoke this device token? Its forwarder stops being accepted immediately.')) return;
      try { await del(`/api/llm-cost/devices/${encodeURIComponent(b.dataset.revoke)}`); renderDevices(el); } catch (e) { toastError(e.message); }
    };
  });
}

// ── pricing ─────────────────────────────────────────────────────────────────

async function renderPricing(el) {
  const p = await get('/api/llm-cost/pricing');
  const overrides = Object.entries(p.overrides || {}).map(([model, v]) => ({ model, ...v }));
  el.innerHTML = `
    <div class="card"><div class="card-header"><div class="card-title">Price book</div><button class="btn btn-sm" id="lc-pb-refresh">Refresh from OpenRouter</button></div>
      <div class="lc-sub">USD per 1M tokens. Resolution: operator override, then the bundled current-generation table (cache-aware), then OpenRouter's public list. Shared with Observe trace costing. Every estimate is labelled EST.</div>
      <div class="lc-grid" style="margin-top:12px">
        <div class="lc-tile"><div class="n">Bundled models</div><div class="v">${p.bundled_models}</div></div>
        <div class="lc-tile"><div class="n">Fetched models</div><div class="v">${p.fetched_models}</div><div class="s">${esc(p.fetched_at ? new Date(p.fetched_at).toLocaleString() : 'never fetched')}</div></div>
        <div class="lc-tile"><div class="n">Overrides</div><div class="v">${p.override_models}</div></div>
      </div></div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">Overrides</div>
      <div class="lc-form"><div><label>Model id</label><input id="lc-ov-model" placeholder="claude-sonnet-5"></div>
        <div><label>Input $/1M</label><input id="lc-ov-in" type="number" step="0.001"></div>
        <div><label>Output $/1M</label><input id="lc-ov-out" type="number" step="0.001"></div>
        <div><button class="btn btn-primary btn-sm" id="lc-ov-save">Save override</button></div></div>
      <div style="margin-top:12px">${rollTable(overrides, [{ h: 'Model', f: r => esc(r.model) }, { h: 'Input', r: 1, f: r => money(r.in, 3) }, { h: 'Output', r: 1, f: r => money(r.out, 3) }, { h: '', f: r => `<button class="btn btn-sm btn-danger" data-ov="${attr(r.model)}">Remove</button>` }])}</div></div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">Look up a model</div>
      <div class="lc-row"><input id="lc-lookup" placeholder="anthropic/claude-sonnet-4.5" style="min-width:280px"><button class="btn btn-sm" id="lc-lookup-go">Look up</button></div>
      <div id="lc-lookup-out" class="lc-sub" style="margin-top:8px"></div></div>`;
  el.querySelector('#lc-pb-refresh').onclick = async () => { try { await post('/api/llm-cost/pricing/refresh', {}); success('Price book refreshed'); renderPricing(el); } catch (e) { toastError(e.message); } };
  el.querySelector('#lc-ov-save').onclick = async () => {
    try {
      await put('/api/llm-cost/pricing/overrides', { model: el.querySelector('#lc-ov-model').value.trim(), in: Number(el.querySelector('#lc-ov-in').value), out: Number(el.querySelector('#lc-ov-out').value) });
      success('Override saved'); renderPricing(el);
    } catch (e) { toastError(e.message); }
  };
  el.querySelectorAll('[data-ov]').forEach(b => { b.onclick = async () => { try { await del(`/api/llm-cost/pricing/overrides/${encodeURIComponent(b.dataset.ov)}`); renderPricing(el); } catch (e) { toastError(e.message); } }; });
  el.querySelector('#lc-lookup-go').onclick = async () => {
    const model = el.querySelector('#lc-lookup').value.trim();
    if (!model) return;
    const r = await get(`/api/llm-cost/pricing/lookup?model=${encodeURIComponent(model)}`);
    const pr = r.price;
    el.querySelector('#lc-lookup-out').innerHTML = pr ? `${esc(model)}: in ${money(pr.in, 3)} · out ${money(pr.out, 3)}${isNum(pr.cache_read) ? ' · cache read ' + money(pr.cache_read, 3) : ''} · source ${esc(pr.source)}${pr.estimate ? ' <span class="lc-tag">EST</span>' : ''}` : `${esc(model)}: unpriced (estimates skip it rather than guess)`;
  };
}

// ── settings ────────────────────────────────────────────────────────────────

async function renderSettings(el) {
  const s = await get('/api/llm-cost/settings');
  const m = s.mqtt || {};
  const num = (id, label, v, extra = '') => `<div><label for="${id}">${label}</label><input id="${id}" type="number" value="${attr(v)}" ${extra}></div>`;
  const txt = (id, label, v, ph = '') => `<div><label for="${id}">${label}</label><input id="${id}" value="${attr(v || '')}" placeholder="${attr(ph)}"></div>`;
  el.innerHTML = `
    <div class="card"><div class="card-title" style="margin-bottom:8px">Alerts and staleness</div>
      <div class="lc-form">
        ${num('lc-s-warn', 'Quota warn %', s.quota_warn_pct)}${num('lc-s-crit', 'Quota critical %', s.quota_critical_pct)}
        ${num('lc-s-daily', 'Daily spend warn $ (0 = off)', s.spend_daily_warn, 'step="0.01"')}
        ${num('lc-s-stale', 'Stale ceiling seconds', s.stale_after_sec)}${num('lc-s-push', 'Push stale after seconds', s.push_stale_sec)}
        <div><label><input type="checkbox" id="lc-s-notify" ${s.notify ? 'checked' : ''}> Notify on alert transitions</label><div class="lc-sub">Toast + Messages, and the llm_cost.alert notification route.</div></div>
      </div></div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">MQTT / Home Assistant</div>
      <div class="lc-sub" style="margin-bottom:8px">Publishes retained per-source state and Home Assistant discovery. Credentials are $SECRET references.</div>
      <div class="lc-form">
        <div><label><input type="checkbox" id="lc-m-en" ${m.enabled ? 'checked' : ''}> Enabled</label></div>
        ${txt('lc-m-host', 'Broker host', m.host, 'mqtt.lan')}${num('lc-m-port', 'Port', m.port || 1883)}
        ${txt('lc-m-user', 'Username ref', m.username_ref, '$MQTT_USER')}${txt('lc-m-pass', 'Password ref', m.password_ref, '$MQTT_PASSWORD')}
        ${txt('lc-m-base', 'Base topic', m.base_topic)}${txt('lc-m-disc', 'Discovery prefix', m.discovery_prefix)}
        ${num('lc-m-int', 'Publish interval seconds', m.publish_interval_sec || 60)}
        <div class="lc-sub">${s.mqttSecrets && m.username_ref ? (s.mqttSecrets.username_ref ? 'username resolves' : 'username ref unset') : ''} ${s.mqttSecrets && m.password_ref ? (s.mqttSecrets.password_ref ? '· password resolves' : '· password ref unset') : ''}</div>
      </div></div>
    <div class="lc-row"><button class="btn btn-primary" id="lc-s-save">Save settings</button><button class="btn" id="lc-m-test">Test MQTT</button></div>`;
  const val = (id) => el.querySelector('#' + id).value;
  el.querySelector('#lc-s-save').onclick = async () => {
    try {
      await put('/api/llm-cost/settings', {
        quota_warn_pct: Number(val('lc-s-warn')), quota_critical_pct: Number(val('lc-s-crit')),
        spend_daily_warn: Number(val('lc-s-daily')), stale_after_sec: Number(val('lc-s-stale')),
        push_stale_sec: Number(val('lc-s-push')), notify: el.querySelector('#lc-s-notify').checked,
        mqtt: { enabled: el.querySelector('#lc-m-en').checked, host: val('lc-m-host').trim(), port: Number(val('lc-m-port')),
          username_ref: val('lc-m-user').trim(), password_ref: val('lc-m-pass').trim(), base_topic: val('lc-m-base').trim(),
          discovery_prefix: val('lc-m-disc').trim(), publish_interval_sec: Number(val('lc-m-int')) },
      });
      success('Settings saved');
    } catch (e) { toastError(e.message); }
  };
  el.querySelector('#lc-m-test').onclick = async () => {
    try { const r = await post('/api/llm-cost/mqtt/test', {}); r.ok ? success('MQTT broker accepted a publish') : toastError(r.error || 'MQTT publish failed'); } catch (e) { toastError(e.message); }
  };
}
