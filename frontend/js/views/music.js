/**
 * Your Vibe — dedicated music settings page.
 *
 * Tabs: Sources | Library | Vibes | Appearance | Behavior | n8n Triggers | Spotify | Data
 * All state (embeds, vibes, history, preferences, triggers) is backend-synced
 * via /api/music/*. Player component (components/player.js) listens for the
 * `music:config-changed` window event to refresh live.
 */

import { get, post, put, patch, del } from '../api.js';
import * as toast from '../components/toast.js';

// ── Module state ────────────────────────────────────────────────────────────

let config = null;          // full music config from /api/music/config
let spotifyStatus = null;   // /api/spotify/status cache
let currentTab = 'sources';

function emitConfigChanged() {
  window.dispatchEvent(new CustomEvent('music:config-changed', { detail: config }));
}

async function reloadConfig() {
  try {
    config = await get('/api/music/config');
    emitConfigChanged();
  } catch (e) { toast.error('Could not load music config: ' + e.message); }
}

// ── Entry point ─────────────────────────────────────────────────────────────

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">🔊 Your Vibe</h2>
      <div style="color:var(--text-dim);font-size:12px">Music player settings, custom embeds, vibes, and n8n triggers</div>
    </div>

    <div style="display:flex;gap:2px;margin-bottom:20px;border-bottom:1px solid var(--border-dim);flex-wrap:wrap">
      ${tabButton('sources',    '🎧 Sources')}
      ${tabButton('library',    '📚 Library')}
      ${tabButton('vibes',      '✨ Vibes')}
      ${tabButton('appearance', '🎨 Appearance')}
      ${tabButton('behavior',   '⚙ Behavior')}
      ${tabButton('triggers',   '⚡ n8n Triggers')}
      ${tabButton('spotify',    '● Spotify')}
      ${tabButton('data',       '💾 Data')}
    </div>

    <div id="music-tab-content"><div class="spinner"></div></div>
  `;

  window.__musicTab = switchTab;
  await reloadConfig();
  switchTab(currentTab);
}

function tabButton(id, label) {
  return `<button class="tab-btn ${currentTab === id ? 'active' : ''}" data-tab="${id}" onclick="window.__musicTab('${jsStr(id)}')">${label}</button>`;
}

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  const el = document.getElementById('music-tab-content');
  if (!el) return;
  if (tab === 'sources')     renderSources(el);
  else if (tab === 'library')    renderLibrary(el);
  else if (tab === 'vibes')      renderVibes(el);
  else if (tab === 'appearance') renderAppearance(el);
  else if (tab === 'behavior')   renderBehavior(el);
  else if (tab === 'triggers')   renderTriggers(el);
  else if (tab === 'spotify')    renderSpotify(el);
  else if (tab === 'data')       renderData(el);
}

// ── SOURCES tab (custom embeds) ─────────────────────────────────────────────

async function renderSources(el) {
  el.innerHTML = `<div class="spinner"></div>`;
  let data;
  try { data = await get('/api/music/embeds'); }
  catch (e) { el.innerHTML = `<div class="card"><p style="color:var(--error)">Failed to load embeds: ${esc(e.message)}</p></div>`; return; }

  const { items = [], templates = [] } = data;

  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">Add custom embed</span>
      </div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px">
        Paste an <code>&lt;iframe&gt;</code> snippet or a direct URL from any service (Bandcamp, Mixcloud,
        Radio Garden, your self-hosted music server, internet radio, etc.). Input is sanitized — only
        safe iframe attributes are preserved.
      </p>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
        <div>
          <label class="form-label">Name</label>
          <input id="emb-name" type="text" class="form-input" placeholder="e.g. SomaFM Groove Salad">
        </div>
        <div>
          <label class="form-label">Icon / color</label>
          <div style="display:flex;gap:6px">
            <input id="emb-icon" type="text" class="form-input" style="width:60px;text-align:center" maxlength="2" value="🎵">
            <input id="emb-color" type="color" value="#ff6d5a" style="width:44px;height:36px;background:none;border:1px solid var(--border-dim);border-radius:var(--radius);cursor:pointer">
          </div>
        </div>
      </div>

      <label class="form-label">Embed HTML or URL</label>
      <textarea id="emb-raw" rows="4" class="form-input" style="font-family:var(--font-mono);font-size:12px" placeholder='<iframe src="https://..."></iframe>  — or —  https://bandcamp.com/EmbeddedPlayer/...'></textarea>

      <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
        <button class="btn btn-sm btn-ghost" onclick="window.__embPreview()">🔍 Preview</button>
        <button class="btn btn-primary" onclick="window.__embSave()">💾 Save embed</button>
        <span id="emb-status" style="font-size:12px;color:var(--text-dim);align-self:center"></span>
      </div>

      <div id="emb-preview" style="margin-top:12px"></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-header"><span class="card-title">Templates</span></div>
      <p style="font-size:12px;color:var(--text-secondary);margin-bottom:10px">Click a template to pre-fill the form with an example.</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px">
        ${templates.map(t => `
          <button onclick='window.__embTemplate(${JSON.stringify(t).replace(/'/g, "&apos;")})'
            style="text-align:left;background:var(--bg-input);border:1px solid var(--border-dim);border-left:3px solid ${t.color};border-radius:var(--radius);padding:10px;cursor:pointer;color:var(--text-primary)">
            <div style="font-size:13px;font-weight:600">${t.icon} ${esc(t.name)}</div>
            <div style="font-size:11px;color:var(--text-dim);margin-top:3px;line-height:1.4">${esc(t.hint)}</div>
          </button>
        `).join('')}
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">Saved embeds (${items.length})</span></div>
      ${items.length === 0 ? '<p style="font-size:13px;color:var(--text-dim)">No custom embeds yet. Add one above.</p>' : `
        <div style="display:grid;gap:8px">
          ${items.map(renderEmbedRow).join('')}
        </div>
      `}
    </div>
  `;
}

function renderEmbedRow(item) {
  return `
    <div style="display:flex;align-items:center;gap:10px;padding:10px;background:var(--bg-input);border:1px solid var(--border-dim);border-left:3px solid ${esc(item.color || '#888')};border-radius:var(--radius)">
      <div style="font-size:20px;flex-shrink:0">${esc(item.icon || '🎵')}</div>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600">${esc(item.name)}</div>
        <div style="font-size:11px;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(item.host || item.src || '')}</div>
      </div>
      <button class="btn btn-sm btn-ghost" onclick="window.__embPlay('${jsStr(item.id)}')" title="Play now">▶</button>
      <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__embDelete('${jsStr(item.id)}')" title="Delete">✕</button>
    </div>
  `;
}

window.__embTemplate = (t) => {
  document.getElementById('emb-name').value = t.name;
  document.getElementById('emb-icon').value = t.icon || '🎵';
  document.getElementById('emb-color').value = t.color || '#ff6d5a';
  document.getElementById('emb-raw').value = t.example || '';
  toast.success(`Loaded ${t.name} template`);
};

window.__embPreview = async () => {
  const raw = document.getElementById('emb-raw')?.value.trim();
  if (!raw) { toast.error('Paste an iframe or URL first'); return; }
  const previewEl = document.getElementById('emb-preview');
  const statusEl = document.getElementById('emb-status');
  try {
    const result = await post('/api/music/embeds/preview', { name: 'preview', raw });
    statusEl.textContent = `✓ Clean. Host: ${result.host || 'unknown'}${result.known_host ? ' (known)' : ''}`;
    statusEl.style.color = 'var(--success)';
    previewEl.innerHTML = `<div style="border:1px solid var(--border-dim);border-radius:var(--radius);padding:8px">${result.html}</div>`;
  } catch (e) {
    statusEl.textContent = `✗ ${e.message}`;
    statusEl.style.color = 'var(--error)';
    previewEl.innerHTML = '';
  }
};

window.__embSave = async () => {
  const name = document.getElementById('emb-name')?.value.trim();
  const raw = document.getElementById('emb-raw')?.value.trim();
  const icon = document.getElementById('emb-icon')?.value.trim() || '🎵';
  const color = document.getElementById('emb-color')?.value || '#ff6d5a';
  if (!name) { toast.error('Name is required'); return; }
  if (!raw)  { toast.error('Embed HTML or URL is required'); return; }
  try {
    await post('/api/music/embeds', { name, raw, icon, color });
    toast.success('Embed saved');
    renderSources(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

window.__embDelete = async (id) => {
  if (!confirm('Delete this embed?')) return;
  try {
    await del(`/api/music/embeds/${id}`);
    toast.success('Deleted');
    renderSources(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

window.__embPlay = async (id) => {
  try {
    const data = await get('/api/music/embeds');
    const item = (data.items || []).find(e => e.id === id);
    if (!item) { toast.error('Embed not found'); return; }
    // Signal player via window event
    window.dispatchEvent(new CustomEvent('music:play-custom', { detail: item }));
    toast.success(`Now playing: ${item.name}`);
  } catch (e) { toast.error(e.message); }
};

// ── LIBRARY tab (history) ───────────────────────────────────────────────────

async function renderLibrary(el) {
  el.innerHTML = `<div class="spinner"></div>`;
  let data;
  try { data = await get('/api/music/history'); }
  catch (e) { el.innerHTML = `<div class="card"><p style="color:var(--error)">Failed to load history: ${esc(e.message)}</p></div>`; return; }

  const items = data.items || [];
  const cap = data.cap || 100;
  const pinnedCount = items.filter(h => h.pinned).length;

  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">Playback history</span>
        <span style="font-size:11px;color:var(--text-dim)">${items.length} / ${cap}  ·  ${pinnedCount} pinned</span>
      </div>

      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
        <input id="lib-search" type="text" class="form-input" placeholder="Search URL or title..." style="flex:1;min-width:200px" oninput="window.__libSearch()">
        <select id="lib-filter" class="form-input" style="width:140px" onchange="window.__libSearch()">
          <option value="">All</option>
          <option value="pinned">Pinned only</option>
          <option value="unpinned">Unpinned only</option>
        </select>
        <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__libClear()">Clear unpinned</button>
        <button class="btn btn-sm btn-ghost" onclick="window.__libExport()">⬇ Export JSON</button>
      </div>

      <div id="lib-list">${renderHistoryList(items)}</div>
    </div>
  `;
}

function renderHistoryList(items) {
  if (!items.length) return '<p style="font-size:13px;color:var(--text-dim)">No history yet.</p>';
  return `
    <div style="display:grid;gap:6px">
      ${items.map(h => `
        <div style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--bg-input);border:1px solid var(--border-dim);${h.pinned ? 'border-left:3px solid var(--accent)' : ''};border-radius:var(--radius)">
          <button class="btn btn-sm btn-ghost" onclick="window.__libPlay('${jsStr(h.url)}')" title="Play" style="padding:2px 6px">▶</button>
          <div style="flex:1;min-width:0">
            <div style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.title || h.url)}</div>
            <div style="font-size:10px;color:var(--text-dim)">${fmtTime(h.last_played || h.added_at)} · ${h.play_count || 1} plays${h.tags?.length ? ' · ' + h.tags.map(esc).join(', ') : ''}</div>
          </div>
          <button class="btn btn-sm btn-ghost" onclick="window.__libPin('${jsStr(h.id)}', ${!h.pinned})" title="${h.pinned ? 'Unpin' : 'Pin'}" style="padding:2px 6px">${h.pinned ? '📌' : '📍'}</button>
          <button class="btn btn-sm btn-ghost" onclick="window.__libTag('${jsStr(h.id)}')" title="Edit tags" style="padding:2px 6px">🏷</button>
          <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__libDel('${jsStr(h.id)}')" title="Delete" style="padding:2px 6px">✕</button>
        </div>
      `).join('')}
    </div>
  `;
}

window.__libSearch = async () => {
  const q = document.getElementById('lib-search')?.value || '';
  const filter = document.getElementById('lib-filter')?.value || '';
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (filter === 'pinned')   params.set('pinned', 'true');
  if (filter === 'unpinned') params.set('pinned', 'false');
  try {
    const data = await get(`/api/music/history?${params}`);
    const listEl = document.getElementById('lib-list');
    if (listEl) listEl.innerHTML = renderHistoryList(data.items || []);
  } catch (e) { toast.error(e.message); }
};

window.__libPlay = (url) => {
  window.dispatchEvent(new CustomEvent('music:play-url', { detail: { url } }));
  toast.success('Playing');
};

window.__libPin = async (id, pinned) => {
  try {
    await patch(`/api/music/history/${id}`, { pinned });
    renderLibrary(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

window.__libTag = async (id) => {
  const tags = prompt('Tags (comma-separated):', '');
  if (tags === null) return;
  try {
    await patch(`/api/music/history/${id}`, { tags: tags.split(',').map(s => s.trim()).filter(Boolean) });
    renderLibrary(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

window.__libDel = async (id) => {
  try {
    await del(`/api/music/history/${id}`);
    renderLibrary(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

window.__libClear = async () => {
  if (!confirm('Clear all unpinned history items?')) return;
  try {
    await del('/api/music/history?keep_pinned=true');
    toast.success('Unpinned history cleared');
    renderLibrary(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

window.__libExport = async () => {
  try {
    const data = await get('/api/music/history/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `your-vibe-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) { toast.error(e.message); }
};

// ── VIBES tab ───────────────────────────────────────────────────────────────

async function renderVibes(el) {
  el.innerHTML = `<div class="spinner"></div>`;
  let data;
  try { data = await get('/api/music/vibes'); }
  catch (e) { el.innerHTML = `<div class="card"><p style="color:var(--error)">${esc(e.message)}</p></div>`; return; }

  const items = data.items || [];

  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header"><span class="card-title">Create a vibe</span></div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px">
        A vibe is a named bundle of URLs. One-click launch queues all of them into the player.
      </p>
      <div style="display:grid;grid-template-columns:2fr 1fr 70px 50px;gap:8px;margin-bottom:8px">
        <input id="vibe-name" class="form-input" placeholder="Name (e.g. Deep Work)">
        <input id="vibe-desc" class="form-input" placeholder="Description (optional)">
        <input id="vibe-icon" class="form-input" placeholder="🧠" maxlength="2" style="text-align:center">
        <input id="vibe-color" type="color" value="#a78bfa" style="width:100%;height:36px;background:none;border:1px solid var(--border-dim);border-radius:var(--radius)">
      </div>
      <label class="form-label">URLs (one per line)</label>
      <textarea id="vibe-urls" rows="5" class="form-input" style="font-family:var(--font-mono);font-size:12px" placeholder="https://open.spotify.com/playlist/..."></textarea>
      <button class="btn btn-primary" style="margin-top:10px" onclick="window.__vibeSave()">💾 Save vibe</button>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">Your vibes (${items.length})</span></div>
      ${items.length === 0 ? '<p style="font-size:13px;color:var(--text-dim)">No vibes yet. Create one above.</p>' : `
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px">
          ${items.map(v => `
            <div style="background:var(--bg-input);border:1px solid var(--border-dim);border-left:4px solid ${esc(v.color || '#a78bfa')};border-radius:var(--radius);padding:12px">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                <span style="font-size:20px">${esc(v.icon || '🎵')}</span>
                <div style="flex:1;min-width:0">
                  <div style="font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(v.name)}</div>
                  <div style="font-size:10px;color:var(--text-dim)">${(v.urls || []).length} tracks</div>
                </div>
              </div>
              ${v.description ? `<div style="font-size:11px;color:var(--text-secondary);margin-bottom:8px">${esc(v.description)}</div>` : ''}
              <div style="display:flex;gap:6px">
                <button class="btn btn-sm btn-primary" onclick="window.__vibeLaunch('${jsStr(v.id)}')" style="flex:1">▶ Launch</button>
                <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__vibeDel('${jsStr(v.id)}')">✕</button>
              </div>
            </div>
          `).join('')}
        </div>
      `}
    </div>
  `;
}

window.__vibeSave = async () => {
  const name = document.getElementById('vibe-name')?.value.trim();
  const description = document.getElementById('vibe-desc')?.value.trim();
  const icon = document.getElementById('vibe-icon')?.value.trim() || '🎵';
  const color = document.getElementById('vibe-color')?.value || '#a78bfa';
  const urls = (document.getElementById('vibe-urls')?.value || '').split('\n').map(s => s.trim()).filter(Boolean);
  if (!name) { toast.error('Name is required'); return; }
  if (!urls.length) { toast.error('Add at least one URL'); return; }
  try {
    await post('/api/music/vibes', { name, description, icon, color, urls });
    toast.success('Vibe saved');
    renderVibes(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

window.__vibeLaunch = async (id) => {
  try {
    const data = await get('/api/music/vibes');
    const v = (data.items || []).find(x => x.id === id);
    if (!v || !v.urls?.length) { toast.error('Vibe is empty'); return; }
    window.dispatchEvent(new CustomEvent('music:play-vibe', { detail: v }));
    toast.success(`Launched: ${v.name}`);
  } catch (e) { toast.error(e.message); }
};

window.__vibeDel = async (id) => {
  if (!confirm('Delete this vibe?')) return;
  try {
    await del(`/api/music/vibes/${id}`);
    renderVibes(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

// ── APPEARANCE tab ──────────────────────────────────────────────────────────

function renderAppearance(el) {
  const a = config?.appearance || {};
  el.innerHTML = `
    <div class="card">
      <div class="card-header"><span class="card-title">EQ visualizer</span></div>
      ${toggleRow('eq_enabled', 'Show EQ visualizer', a.eq_enabled)}
      ${selectRow('eq_style', 'Style', a.eq_style, [['bars','Bars'],['wave','Wave'],['circular','Circular'],['off','Hidden']])}
      ${rangeRow('eq_bars', 'Bar count', a.eq_bars, 4, 32, 1)}
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-header"><span class="card-title">Banner</span></div>
      ${selectRow('banner_height', 'Height', a.banner_height, [['compact','Compact'],['normal','Normal'],['tall','Tall']])}
      ${selectRow('banner_position', 'Position', a.banner_position, [['top','Top'],['bottom','Bottom'],['floating','Floating']])}
      ${toggleRow('show_album_art', 'Show album art (Spotify)', a.show_album_art)}
      ${toggleRow('show_progress', 'Show progress bar', a.show_progress)}
      ${toggleRow('show_controls', 'Show controls', a.show_controls)}
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-header"><span class="card-title">Accent color</span></div>
      <div style="display:flex;align-items:center;gap:10px">
        <input type="color" id="f-accent_override" value="${esc(a.accent_override || '#ff6d5a')}" style="width:50px;height:36px;background:none;border:1px solid var(--border-dim);border-radius:var(--radius)" oninput="window.__musicSetAppearance('accent_override', this.value)">
        <button class="btn btn-sm btn-ghost" onclick="window.__musicSetAppearance('accent_override', null)">Reset to theme</button>
        <span style="font-size:11px;color:var(--text-dim)">Overrides the theme accent used for the player banner.</span>
      </div>
    </div>
  `;
}

function toggleRow(key, label, value) {
  return `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border-dim)">
      <span style="font-size:13px">${esc(label)}</span>
      <label class="switch">
        <input type="checkbox" ${value ? 'checked' : ''} onchange="window.__musicSetAppearance('${jsStr(key)}', this.checked)">
        <span class="slider"></span>
      </label>
    </div>
  `;
}

function selectRow(key, label, value, options) {
  return `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border-dim)">
      <span style="font-size:13px">${esc(label)}</span>
      <select class="form-input" style="width:160px" onchange="window.__musicSetAppearance('${jsStr(key)}', this.value)">
        ${options.map(([v, l]) => `<option value="${esc(v)}" ${v === value ? 'selected' : ''}>${esc(l)}</option>`).join('')}
      </select>
    </div>
  `;
}

function rangeRow(key, label, value, min, max, step) {
  return `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border-dim);gap:12px">
      <span style="font-size:13px">${esc(label)}</span>
      <div style="display:flex;align-items:center;gap:8px;flex:1;max-width:260px">
        <input type="range" min="${min}" max="${max}" step="${step}" value="${value}" style="flex:1" oninput="window.__musicSetAppearance('${jsStr(key)}', parseInt(this.value)); this.nextElementSibling.textContent=this.value">
        <span style="font-size:11px;color:var(--text-dim);width:28px;text-align:right">${value}</span>
      </div>
    </div>
  `;
}

window.__musicSetAppearance = async (key, value) => {
  if (!config) return;
  config.appearance = { ...config.appearance, [key]: value };
  try {
    await put('/api/music/config', { appearance: { [key]: value } });
    emitConfigChanged();
  } catch (e) { toast.error(e.message); }
};

// ── BEHAVIOR tab ────────────────────────────────────────────────────────────

function renderBehavior(el) {
  const b = config?.behavior || {};
  el.innerHTML = `
    <div class="card">
      <div class="card-header"><span class="card-title">Playback behavior</span></div>
      ${behToggle('autoplay_on_paste', 'Autoplay when pasting a URL', b.autoplay_on_paste)}
      ${behToggle('auto_advance', 'Auto-advance to next track in history', b.auto_advance)}
      ${behToggle('persist_across_reload', 'Persist playing state across page reloads', b.persist_across_reload)}
      ${behToggle('auto_pause_on_error', 'Auto-pause when an n8n workflow error fires', b.auto_pause_on_error)}
      <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border-dim)">
        <span style="font-size:13px">Default service launcher</span>
        <select class="form-input" style="width:200px" onchange="window.__musicSetBehavior('default_service', this.value || null)">
          <option value="">(none)</option>
          ${['spotify','youtube','soundcloud','apple','tidal','youtubemusic'].map(s =>
            `<option value="${s}" ${b.default_service === s ? 'selected' : ''}>${s}</option>`
          ).join('')}
        </select>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0">
        <span style="font-size:13px">Global hotkey to toggle player</span>
        <input type="text" class="form-input" style="width:140px" value="${esc(b.hotkey_toggle || '')}" placeholder="e.g. alt+m"
          onblur="window.__musicSetBehavior('hotkey_toggle', this.value.trim() || null)">
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-header"><span class="card-title">History cap</span></div>
      <div style="display:flex;align-items:center;gap:12px">
        <input type="range" min="10" max="500" step="10" value="${config?.history_cap || 100}" style="flex:1"
          oninput="this.nextElementSibling.textContent=this.value" onchange="window.__musicSetCap(parseInt(this.value))">
        <span style="font-size:12px;color:var(--text-dim);width:40px;text-align:right">${config?.history_cap || 100}</span>
      </div>
      <div style="font-size:11px;color:var(--text-dim);margin-top:6px">Pinned items never count against the cap.</div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-header"><span class="card-title">Reset</span></div>
      <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__musicReset()">Reset appearance + behavior to defaults</button>
    </div>
  `;
}

function behToggle(key, label, value) {
  return `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border-dim)">
      <span style="font-size:13px">${esc(label)}</span>
      <label class="switch">
        <input type="checkbox" ${value ? 'checked' : ''} onchange="window.__musicSetBehavior('${jsStr(key)}', this.checked)">
        <span class="slider"></span>
      </label>
    </div>
  `;
}

window.__musicSetBehavior = async (key, value) => {
  if (!config) return;
  config.behavior = { ...config.behavior, [key]: value };
  try {
    await put('/api/music/config', { behavior: { [key]: value } });
    emitConfigChanged();
  } catch (e) { toast.error(e.message); }
};

window.__musicSetCap = async (cap) => {
  try {
    await put('/api/music/config', { history_cap: cap });
    config.history_cap = cap;
    emitConfigChanged();
  } catch (e) { toast.error(e.message); }
};

window.__musicReset = async () => {
  if (!confirm('Reset appearance and behavior to defaults?')) return;
  try {
    await post('/api/music/config/reset', {});
    await reloadConfig();
    switchTab(currentTab);
    toast.success('Reset');
  } catch (e) { toast.error(e.message); }
};

// ── TRIGGERS tab ────────────────────────────────────────────────────────────

async function renderTriggers(el) {
  el.innerHTML = `<div class="spinner"></div>`;
  let t;
  try { t = await get('/api/music/triggers'); }
  catch (e) { el.innerHTML = `<div class="card"><p style="color:var(--error)">${esc(e.message)}</p></div>`; return; }

  const origin = location.origin;
  const webhookUrl = `${origin}/api/music/triggers/fire`;

  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">n8n → Your Vibe webhook</span>
        ${t.enabled ? '<span class="pill pill-success">Enabled</span>' : '<span class="pill">Disabled</span>'}
      </div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px">
        Let n8n workflows control the player. Useful for soundtracking deploys, playing a jingle
        when a workflow succeeds, or auto-pausing on critical errors.
      </p>

      <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border-dim)">
        <span style="font-size:13px">Enable trigger webhook</span>
        <label class="switch">
          <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="window.__trigEnable(this.checked)">
          <span class="slider"></span>
        </label>
      </div>

      ${t.enabled ? `
        <div style="margin-top:14px">
          <label class="form-label">Webhook URL</label>
          <div style="display:flex;gap:6px;align-items:center">
            <code style="flex:1;background:var(--bg-input);padding:8px 10px;border-radius:var(--radius);font-size:12px;word-break:break-all">${esc(webhookUrl)}</code>
            <button class="btn btn-sm btn-ghost" onclick="navigator.clipboard.writeText('${jsStr(webhookUrl)}').then(()=>window.__vibeCopied('URL'))">Copy</button>
          </div>

          <label class="form-label" style="margin-top:12px">Auth token</label>
          <div style="display:flex;gap:6px;align-items:center">
            <code id="trig-token" style="flex:1;background:var(--bg-input);padding:8px 10px;border-radius:var(--radius);font-size:12px;word-break:break-all;font-family:var(--font-mono)">${esc(t.token || '(none)')}</code>
            <button class="btn btn-sm btn-ghost" onclick="navigator.clipboard.writeText(document.getElementById('trig-token').textContent).then(()=>window.__vibeCopied('Token'))">Copy</button>
            <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__trigRotate()">Rotate</button>
          </div>
          <div style="font-size:11px;color:var(--text-dim);margin-top:4px">Pass as <code>Authorization: Bearer &lt;token&gt;</code> or <code>X-Vibe-Token: &lt;token&gt;</code>.</div>
        </div>

        <div class="card" style="margin-top:14px;background:var(--bg-void)">
          <div class="card-header"><span class="card-title" style="font-size:12px">Example n8n HTTP Request node</span></div>
          <pre style="font-size:11px;color:var(--text-secondary);white-space:pre-wrap;margin:0">Method: POST
URL: ${esc(webhookUrl)}
Headers:
  Authorization: Bearer ${esc(t.token || '<token>')}
Body (JSON):
  {
    "action": "play",
    "url": "https://open.spotify.com/track/xxxx",
    "workflow_id": "{{ $workflow.id }}",
    "instance_id": "prod"
  }

Actions: play | pause | next | prev | stop</pre>
        </div>
      ` : ''}
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">Default reactions</span></div>
      <p style="font-size:12px;color:var(--text-dim);margin-bottom:10px">These run automatically (via the error-handler stream) when any workflow errors or completes successfully.</p>
      ${reactionRow('on_workflow_error',   'On workflow error',   t.on_workflow_error || {})}
      ${reactionRow('on_workflow_success', 'On workflow success', t.on_workflow_success || {})}
    </div>
  `;
}

function reactionRow(key, label, val) {
  return `
    <div style="display:grid;grid-template-columns:1fr 140px 2fr;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--border-dim)">
      <span style="font-size:13px">${esc(label)}</span>
      <select class="form-input" onchange="window.__trigReaction('${jsStr(key)}', 'action', this.value)">
        ${[['none','(nothing)'],['pause','Pause'],['play','Play URL'],['stop','Stop']].map(([v,l]) =>
          `<option value="${v}" ${val.action === v ? 'selected' : ''}>${l}</option>`
        ).join('')}
      </select>
      <input type="text" class="form-input" value="${esc(val.url || '')}" placeholder="URL (only for Play)" onblur="window.__trigReaction('${jsStr(key)}', 'url', this.value.trim())">
    </div>
  `;
}

window.__trigEnable = async (enabled) => {
  try {
    await put('/api/music/triggers', { enabled });
    renderTriggers(document.getElementById('music-tab-content'));
  } catch (e) { toast.error(e.message); }
};

window.__trigRotate = async () => {
  if (!confirm('Rotate webhook token? Existing callers will stop working.')) return;
  try {
    await post('/api/music/triggers/token/rotate', {});
    renderTriggers(document.getElementById('music-tab-content'));
    toast.success('Token rotated');
  } catch (e) { toast.error(e.message); }
};

window.__trigReaction = async (key, field, value) => {
  try {
    const current = await get('/api/music/triggers');
    const reaction = { ...(current[key] || { action: 'none', url: '' }), [field]: value };
    await put('/api/music/triggers', { [key]: reaction });
  } catch (e) { toast.error(e.message); }
};

window.__vibeCopied = (what) => toast.success(`${what} copied`);

// ── SPOTIFY tab (migrated from settings.js) ─────────────────────────────────

async function renderSpotify(el) {
  el.innerHTML = `<div class="spinner"></div>`;
  let status = { connected: false, has_credentials: false, client_id: '', display_name: '' };
  try { status = await get('/api/spotify/status'); } catch {}
  spotifyStatus = status;

  const origin = location.origin.replace('://localhost:', '://127.0.0.1:').replace('://localhost', '://127.0.0.1');
  const redirectUri = `${origin}/api/spotify/callback`;

  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title" style="display:flex;align-items:center;gap:8px">
          <span style="color:#1DB954;font-size:18px">●</span> Spotify Integration
        </span>
        ${status.connected ? `<span class="pill pill-success">Connected as ${esc(status.display_name)}</span>` : '<span class="pill">Not connected</span>'}
      </div>

      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:16px">
        Connect Spotify for full playback control — album art, search, playlists, skip, seek, volume.
        Requires <strong>Spotify Premium</strong> for playback control.
        <a href="https://developer.spotify.com/dashboard" target="_blank" style="color:var(--accent)">Create a Spotify app →</a>
      </p>

      ${!status.has_credentials ? `
        <div style="background:var(--bg-input);border-radius:var(--radius);padding:12px;font-size:12px;color:var(--text-secondary);margin-bottom:16px;line-height:1.8">
          <strong>Setup steps:</strong><br>
          1. Go to <a href="https://developer.spotify.com/dashboard" target="_blank" style="color:var(--accent)">developer.spotify.com/dashboard</a> → Create App<br>
          2. Under <strong>Redirect URIs</strong>, add: <code style="background:var(--bg-void);padding:2px 6px;border-radius:3px">${esc(redirectUri)}</code><br>
          &nbsp;&nbsp;&nbsp;<span style="color:var(--warning)">⚠ Use <code>127.0.0.1</code>, not <code>localhost</code> (Spotify policy, April 2025)</span><br>
          3. Copy your Client ID and Client Secret below
        </div>
      ` : ''}

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
        <div>
          <label class="form-label">Client ID</label>
          <input id="sp-client-id" type="text" class="form-input" value="${esc(status.client_id)}" placeholder="Spotify App Client ID">
        </div>
        <div>
          <label class="form-label">Client Secret</label>
          <input id="sp-client-secret" type="password" class="form-input" placeholder="${status.has_credentials ? '(saved — enter to update)' : 'Client Secret'}">
        </div>
      </div>

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-primary" onclick="window.__saveSpotifyAndConnect()">
          ${status.has_credentials ? 'Save & Reconnect' : 'Save & Connect Spotify'}
        </button>
        ${status.connected ? `<button class="btn btn-sm btn-ghost btn-danger" onclick="window.__disconnectSpotify()">Disconnect</button>` : ''}
        ${status.connected ? `<button class="btn btn-sm btn-ghost" onclick="window.__checkSpotifyDevices()">Check Devices</button>` : ''}
      </div>
      <div id="sp-devices" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">Redirect URI</span></div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:8px">Add this exact URI to your Spotify app's Redirect URIs list:</p>
      <div style="display:flex;align-items:center;gap:8px">
        <code style="flex:1;background:var(--bg-input);border-radius:var(--radius);padding:8px 12px;font-size:12px;word-break:break-all">${esc(redirectUri)}</code>
        <button class="btn btn-sm btn-ghost" onclick="navigator.clipboard.writeText('${jsStr(redirectUri)}').then(()=>window.__vibeCopied('URI'))">Copy</button>
      </div>
    </div>
  `;
}

window.__saveSpotifyAndConnect = async () => {
  const clientId = document.getElementById('sp-client-id')?.value.trim();
  const clientSecret = document.getElementById('sp-client-secret')?.value.trim();
  if (!clientId) { toast.error('Client ID is required'); return; }
  try {
    const body = { client_id: clientId, client_secret: clientSecret || '' };
    if (!clientSecret) delete body.client_secret;
    await post('/api/spotify/setup', body);
    window.location.href = '/api/spotify/auth';
  } catch(e) { toast.error(e.message); }
};

window.__disconnectSpotify = async () => {
  if (!confirm('Disconnect Spotify?')) return;
  try {
    await post('/api/spotify/disconnect', {});
    toast.success('Spotify disconnected');
    renderSpotify(document.getElementById('music-tab-content'));
  } catch(e) { toast.error(e.message); }
};

window.__checkSpotifyDevices = async () => {
  const el = document.getElementById('sp-devices');
  if (!el) return;
  try {
    const data = await get('/api/spotify/devices');
    const devices = data.devices || [];
    el.innerHTML = devices.length
      ? `<div style="font-size:12px;color:var(--text-secondary)"><strong>Active devices:</strong> ${devices.map(d => `${esc(d.name)} (${esc(d.type)}${d.is_active ? ' — <span style="color:var(--success)">active</span>' : ''})`).join(', ')}</div>`
      : '<p style="font-size:12px;color:var(--text-secondary)">No active devices. Open Spotify on any device first.</p>';
  } catch(e) { el.innerHTML = `<p style="font-size:12px;color:var(--error)">${esc(e.message)}</p>`; }
};

// ── DATA tab ────────────────────────────────────────────────────────────────

function renderData(el) {
  el.innerHTML = `
    <div class="card">
      <div class="card-header"><span class="card-title">Export all Your Vibe data</span></div>
      <p style="font-size:13px;color:var(--text-secondary);margin-bottom:10px">Downloads a JSON file with history, vibes, and custom embeds. Useful for backup or moving to another dashboard instance.</p>
      <button class="btn btn-primary" onclick="window.__libExport()">⬇ Export JSON</button>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-header"><span class="card-title">Danger zone</span></div>
      <div style="display:flex;flex-direction:column;gap:8px;align-items:flex-start">
        <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__libClear()">Clear unpinned history</button>
        <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__musicReset()">Reset appearance + behavior</button>
      </div>
      <div style="font-size:11px;color:var(--text-dim);margin-top:10px">Custom embeds and vibes can be deleted individually in their tabs.</div>
    </div>
  `;
}

// ── Utilities ───────────────────────────────────────────────────────────────

function esc(s) {
  if (s === null || s === undefined) return '';
  const el = document.createElement('span');
  el.textContent = String(s);
  return el.innerHTML;
}


function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = (now - d.getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff/86400)}d ago`;
  return d.toLocaleDateString();
}
