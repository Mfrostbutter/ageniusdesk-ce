/**
 * Music Player Banner — prominent top-of-viewport player with large EQ visualizer.
 * Supports: Spotify, YouTube, SoundCloud, Apple Music, YouTube Music, Tidal, direct audio.
 */

const SERVICES = [
  {
    name: 'Spotify', icon: '🎵', color: '#1DB954',
    match: /open\.spotify\.com\/(track|album|playlist|episode|show)\/([a-zA-Z0-9]+)/,
    embed: (m) => `https://open.spotify.com/embed/${m[1]}/${m[2]}?theme=0&utm_source=generator`,
    height: (m) => m[1] === 'track' ? 80 : 152,
    title: (m) => m[1].charAt(0).toUpperCase() + m[1].slice(1),
  },
  {
    name: 'YouTube', icon: '▶', color: '#FF0000',
    match: /(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|music\.youtube\.com\/watch\?v=)([a-zA-Z0-9_-]{11})/,
    embed: (m) => `https://www.youtube.com/embed/${m[1]}?autoplay=0`,
    height: () => 200,
    title: () => 'Video',
  },
  {
    name: 'YouTube', icon: '▶', color: '#FF0000',
    match: /youtube\.com\/.*[?&]list=([a-zA-Z0-9_-]+)/,
    embed: (m) => `https://www.youtube.com/embed/videoseries?list=${m[1]}`,
    height: () => 200,
    title: () => 'Playlist',
  },
  {
    name: 'SoundCloud', icon: '☁', color: '#FF5500',
    match: /soundcloud\.com\/([^/]+)\/?([^/?]*)/,
    embed: (m, url) => `https://w.soundcloud.com/player/?url=${encodeURIComponent(url)}&color=%23ff6d5a&auto_play=false&hide_related=true&show_comments=false&show_user=true&show_reposts=false&show_teaser=false&visual=false`,
    height: () => 166,
    title: (m) => decodeURIComponent(m[2] || m[1]).replace(/-/g, ' '),
  },
  {
    name: 'Apple Music', icon: '♫', color: '#FC3C44',
    match: /music\.apple\.com\/([a-z]{2})\/(album|playlist|station)\/([^/]+)\/([a-z0-9.]+)/i,
    embed: (m) => `https://embed.music.apple.com/${m[1]}/${m[2]}/${m[3]}/${m[4]}`,
    height: (m) => m[2] === 'album' || m[2] === 'playlist' ? 175 : 150,
    title: (m) => decodeURIComponent(m[3]).replace(/-/g, ' '),
  },
  {
    name: 'Tidal', icon: '🌊', color: '#00FFFF',
    match: /tidal\.com\/(?:browse\/)?(track|album|playlist|mix)\/([a-zA-Z0-9-]+)/,
    embed: (m) => `https://embed.tidal.com/${m[1]}s/${m[2]}?layout=gridify&disableAnalytics=true`,
    height: (m) => m[1] === 'track' ? 96 : 300,
    title: (m) => m[1].charAt(0).toUpperCase() + m[1].slice(1),
  },
];

let currentUrl = '';
let currentIndex = 0;
let isPlaying = false;
let savedUrls = [];
let currentCustomEmbed = null;  // { id, name, icon, color, html, src, host } when playing a custom embed
let vibeQueue = [];             // urls queued from a vibe launch

// Music config (loaded from /api/music/config, falls back to defaults)
let musicConfig = {
  appearance: { eq_enabled: true, eq_bars: 16, eq_style: 'bars', banner_height: 'normal', show_album_art: true, show_progress: true, show_controls: true, accent_override: null },
  behavior:   { default_service: null, autoplay_on_paste: true, auto_advance: false, persist_across_reload: true, auto_pause_on_error: false },
};

// Spotify state
let spotifyConnected = false;
let spotifyState = null;
let spotifyPollTimer = null;

export async function init() {
  // Load music config from backend (with defaults fallback)
  try {
    const cfg = await fetch('/api/music/config').then(r => r.json());
    if (cfg?.appearance) musicConfig = cfg;
  } catch {}

  // Restore playing state from localStorage
  try { savedUrls = JSON.parse(localStorage.getItem('flow-player-urls') || '[]'); }
  catch { savedUrls = []; }
  currentUrl = savedUrls[0] || '';
  currentIndex = 0;
  isPlaying = !!currentUrl;

  // Check Spotify connection
  try {
    const status = await fetch('/api/spotify/status').then(r => r.json());
    spotifyConnected = status.connected;
    if (spotifyConnected) startSpotifyPoll();
  } catch {}

  // ── Cross-component events from Your Vibe page ──────────────────────
  window.addEventListener('music:config-changed', (e) => {
    if (e.detail) musicConfig = e.detail;
    refresh();
  });

  window.addEventListener('music:play-url', (e) => {
    const url = e?.detail?.url;
    if (url) playTrack(url);
  });

  window.addEventListener('music:play-custom', (e) => {
    const item = e?.detail;
    if (item) playCustomEmbed(item);
  });

  window.addEventListener('music:play-vibe', (e) => {
    const vibe = e?.detail;
    if (!vibe?.urls?.length) return;
    vibeQueue = [...vibe.urls];
    playTrack(vibeQueue.shift());
  });
}

export function renderBanner() {
  // ── Custom embed mode ─────────────────────────────────────────────────
  if (currentCustomEmbed) {
    return renderCustomEmbedBanner();
  }

  // ── Spotify API mode ──────────────────────────────────────────────────
  if (spotifyConnected) {
    return renderSpotifyBanner();
  }

  const detected = currentUrl ? detectService(currentUrl) : null;

  // ── Empty state — inviting prompt ─────────────────────────────────────
  if (!currentUrl) {
    return `
      <div class="player-banner player-banner--empty">
        <div class="player-eq-large player-eq--idle">
          ${eqBars(12)}
        </div>
        <div style="flex:1;display:flex;align-items:center;gap:12px">
          <div>
            <div style="font-size:14px;font-weight:600;color:var(--text-primary)">🎧 Start Vibing</div>
            <div style="font-size:11px;color:var(--text-dim);margin-top:2px">Open a service below or paste a URL</div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex:1;max-width:400px">
          <input type="text" id="player-url" class="player-url-input player-url-input--large" placeholder="Paste music URL here..."
            onkeydown="if(event.key==='Enter'){event.preventDefault();window.__playUrl()}">
          <button class="btn btn-primary" onclick="window.__playUrl()" style="padding:8px 16px;white-space:nowrap">▶ Play</button>
        </div>
      </div>
      <div class="player-history" style="justify-content:space-between">
        <div style="display:flex;align-items:center;gap:6px">
          <span style="font-size:10px;color:var(--text-dim);font-weight:700;letter-spacing:1px;padding-right:8px;border-right:1px solid var(--border-dim)">VIBE</span>
          ${serviceLauncherButtons(true)}
        </div>
      </div>
    `;
  }

  // ── Playing state — full banner ───────────────────────────────────────
  const svc = detected?.service || { name: 'Audio', icon: '🎵', color: 'var(--accent)' };
  const title = detected ? detected.service.title(detected.match) : 'Audio';

  return `
    <div class="player-banner player-banner--playing" style="--svc-color:${svc.color}">
      <!-- Left: EQ + info -->
      <div class="player-banner-left">
        <div class="player-eq-large player-eq--active" style="--eq-color:${svc.color}">
          ${eqBars(16)}
        </div>
        <div class="player-now-playing">
          <div class="player-np-label">NOW PLAYING</div>
          <div class="player-np-service" style="color:${svc.color}">${svc.icon} ${svc.name}</div>
          <div class="player-np-title">${esc(title)}</div>
          <!-- Skip controls (only show with 2+ tracks) -->
          ${savedUrls.length > 1 ? `
          <div class="player-controls">
            <button class="player-ctrl-btn ${currentIndex >= savedUrls.length - 1 ? 'disabled' : ''}" onclick="window.__skipTrack(1)" title="Previous track (older)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg>
            </button>
            <span class="player-track-pos">${currentIndex + 1} / ${savedUrls.length}</span>
            <button class="player-ctrl-btn ${currentIndex <= 0 ? 'disabled' : ''}" onclick="window.__skipTrack(-1)" title="Next track (newer)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg>
            </button>
          </div>
          ` : '<div style="font-size:10px;color:var(--text-dim);margin-top:4px">Add more tracks to skip between them</div>'}

        </div>
      </div>

      <!-- Center: Embed -->
      <div class="player-banner-center">
        ${renderEmbed(currentUrl)}
      </div>

      <!-- Right: Controls -->
      <div class="player-banner-right">
        <div style="display:flex;gap:6px;align-items:center">
          <input type="text" id="player-url" class="player-url-input" value="${esc(currentUrl)}" placeholder="Change URL..."
            onkeydown="if(event.key==='Enter'){event.preventDefault();window.__playUrl()}">
          <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__clearPlayer()" title="Close" style="padding:4px 8px">✕</button>
        </div>
      </div>
    </div>

    <div class="player-history" style="--svc-color:${svc.color};justify-content:space-between">
      <!-- Left: Vibe + service launchers -->
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:10px;color:var(--text-dim);font-weight:700;letter-spacing:1px;padding-right:8px;border-right:1px solid var(--border-dim)">VIBE</span>
        ${serviceLauncherButtons(true)}
      </div>
      <!-- Right: Recent history -->
      ${savedUrls.length > 1 ? `
        <div style="display:flex;align-items:center;gap:6px">
          <span style="font-size:10px;color:var(--text-dim);font-weight:700;letter-spacing:1px;padding-right:8px;border-right:1px solid var(--border-dim)">RECENT</span>
          ${savedUrls.slice(1, 8).map(u => {
            const s = detectService(u);
            const c = s?.service.color || 'var(--text-dim)';
            return `<button class="player-history-btn" onclick="window.__playDirect('${jsStr(u)}')" title="${esc(u)}" style="--svc-color:${c}">
              ${s?.service.icon || '🎵'} ${s?.service.name || 'Audio'}
            </button>`;
          }).join('')}
        </div>
      ` : ''}
    </div>
  `;
}

const LAUNCHERS = [
  { name: 'Spotify',      color: '#1DB954', bg: 'rgba(29,185,84,.15)',  url: 'https://open.spotify.com',       icon: `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/></svg>` },
  { name: 'SoundCloud',   color: '#FF5500', bg: 'rgba(255,85,0,.15)',   url: 'https://soundcloud.com/discover', icon: `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M1.175 12.225c-.017 0-.034.002-.051.005-.102.02-.191.1-.206.204-.02.13.076.248.207.248.034 0 .068-.009.098-.026.155-.086.175-.308.066-.42a.198.198 0 0 0-.114-.011zM0 13.293c0 .28.228.508.508.508.28 0 .508-.228.508-.508V12.84c0-.28-.228-.508-.508-.508-.28 0-.508.228-.508.508v.453zm2.116-1.52c-.028 0-.056.004-.083.012-.17.05-.283.22-.238.395.044.175.22.275.393.238.226-.05.38-.274.334-.502-.038-.196-.213-.143-.406-.143zm-.424 2.035c0 .28.228.508.508.508.28 0 .508-.228.508-.508v-1.36c0-.28-.228-.508-.508-.508-.28 0-.508.228-.508.508v1.36zm2.26-2.78c-.057 0-.113.01-.166.03-.26.097-.402.38-.305.642.097.26.38.402.642.305.295-.11.452-.44.342-.735a.508.508 0 0 0-.513-.242zm-.512 2.78c0 .28.228.508.508.508.28 0 .508-.228.508-.508V11.33c0-.28-.228-.508-.508-.508-.28 0-.508.228-.508.508v2.478zm2.3-3.547c-.09 0-.18.02-.263.06-.34.165-.482.576-.317.917.165.34.576.482.917.317.372-.18.53-.633.35-1.006a.765.765 0 0 0-.688-.288zm-.51 3.547c0 .28.228.508.508.508.28 0 .508-.228.508-.508V10.26c0-.28-.228-.508-.508-.508-.28 0-.508.228-.508.508v3.548zm2.305-4.09c-.12 0-.24.03-.348.09-.43.22-.6.75-.38 1.18.22.43.75.6 1.18.38.458-.235.643-.8.408-1.258a.95.95 0 0 0-.86-.392zm-.51 4.09c0 .28.228.508.508.508.28 0 .508-.228.508-.508V9.198c0-.28-.228-.508-.508-.508-.28 0-.508.228-.508.508v4.61zm9.913-4.61c-1.47 0-2.695.987-3.046 2.347-.388-.254-.85-.403-1.347-.403-1.36 0-2.462 1.102-2.462 2.462 0 1.36 1.102 2.462 2.462 2.462h4.393c1.36 0 2.462-1.102 2.462-2.462 0-1.272-.965-2.32-2.208-2.45a3.09 3.09 0 0 0-.254-1.956z"/></svg>` },
  { name: 'Pandora',      color: '#005483', bg: 'rgba(0,84,131,.15)',   url: 'https://www.pandora.com',         icon: `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M0 0v24h5.4V0H0zm6.6 0v15.83c1.8.52 3.29 1.35 4.47 2.52 1.42 1.42 2.13 3.23 2.13 5.65H18c0-3.37-.86-6.23-2.57-8.58C13.67 12.92 11.27 11.32 8.08 10.5V0H6.6z"/></svg>` },
  { name: 'YouTube Music',color: '#FF0000', bg: 'rgba(255,0,0,.12)',    url: 'https://music.youtube.com',      icon: `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.376 0 0 5.376 0 12s5.376 12 12 12 12-5.376 12-12S18.624 0 12 0zm0 19.104c-3.924 0-7.104-3.18-7.104-7.104S8.076 4.896 12 4.896s7.104 3.18 7.104 7.104-3.18 7.104-7.104 7.104zm0-13.332c-3.432 0-6.228 2.796-6.228 6.228S8.568 18.228 12 18.228s6.228-2.796 6.228-6.228S15.432 5.772 12 5.772zM9.684 15.54V8.46L16.2 12l-6.516 3.54z"/></svg>` },
  { name: 'Tidal',        color: '#00FFFF', bg: 'rgba(0,255,255,.1)',   url: 'https://listen.tidal.com',       icon: `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M12.012 3.992L8.008 7.996 4.004 3.992 0 7.996l4.004 4.004 4.004-4.004 4.004 4.004 4.004-4.004L12.012 3.992zM8.008 16.004l-4.004-4.004L0 16.004l4.004 4.004 4.004-4.004zm7.996 0l-4.004-4.004-4.004 4.004 4.004 4.004 4.004-4.004zM16.004 7.996l-4.004 4.004 4.004 4.004 4.004-4.004-4.004-4.004z"/></svg>` },
];

function serviceLauncherButtons(compact = false) {
  return LAUNCHERS.map(s => `
    <a href="${s.url}" target="_blank" rel="noopener"
      style="display:inline-flex;align-items:center;gap:4px;padding:${compact ? '3px 7px' : '4px 9px'};border-radius:20px;background:${s.bg};color:${s.color};font-size:${compact ? '10px' : '11px'};font-weight:600;text-decoration:none;border:1px solid ${s.color}33;white-space:nowrap;transition:background .15s"
      onmouseover="this.style.background='${s.bg.replace('.15', '.3').replace('.12', '.25').replace('.1', '.2')}'"
      onmouseout="this.style.background='${jsStr(s.bg)}'"
      title="Open ${s.name}"
    >${s.icon} ${s.name}</a>
  `).join('');
}

function eqBars(count) {
  return Array.from({ length: count }, (_, i) =>
    `<div class="eq-bar" style="--bar-delay:${i * 0.08}s;--bar-height:${30 + Math.random() * 70}%"></div>`
  ).join('');
}

function detectService(url) {
  for (const service of SERVICES) {
    const match = url.match(service.match);
    if (match) return { service, match };
  }
  if (/\.(mp3|wav|ogg|flac|m4a|aac)(\?|$)/i.test(url)) {
    return { service: { name: 'Audio', icon: '🎵', color: 'var(--accent)', title: () => 'Audio File' }, match: [url], isAudio: true };
  }
  return null;
}

function renderEmbed(url) {
  const detected = detectService(url);
  if (!detected) return '';
  if (detected.isAudio) return `<audio controls style="width:100%;height:40px" src="${esc(url)}"></audio>`;
  const embedUrl = detected.service.embed(detected.match, url);
  const height = detected.service.height(detected.match);
  return `<iframe src="${embedUrl}" width="100%" height="${height}" frameborder="0"
    allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"
    loading="lazy" style="border-radius:8px;border:none"></iframe>`;
}

// ── Handlers ────────────────────────────────────────────────────────────────

window.__playUrl = () => {
  const input = document.getElementById('player-url');
  if (!input || !input.value.trim()) return;
  playTrack(input.value.trim());
};

window.__playDirect = (url) => playTrack(url);

window.__skipTrack = (direction) => {
  const newIndex = currentIndex + direction;
  if (newIndex < 0 || newIndex >= savedUrls.length) return;
  currentIndex = newIndex;
  currentUrl = savedUrls[currentIndex];
  isPlaying = true;
  refresh();
};

window.__clearPlayer = () => {
  currentUrl = '';
  currentIndex = 0;
  isPlaying = false;
  currentCustomEmbed = null;
  vibeQueue = [];
  refresh();
};

function playTrack(url) {
  currentCustomEmbed = null;
  currentUrl = url;
  isPlaying = true;
  // Add to front of list, dedupe
  savedUrls = [url, ...savedUrls.filter(u => u !== url)].slice(0, 20);
  currentIndex = 0;
  localStorage.setItem('flow-player-urls', JSON.stringify(savedUrls));
  refresh();

  // Sync to server history (best-effort, non-blocking)
  const detected = detectService(url);
  const title = detected ? detected.service.title(detected.match) : url;
  fetch('/api/music/history', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, title: String(title) }),
  }).catch(() => {});
}

function playCustomEmbed(item) {
  currentCustomEmbed = item;
  currentUrl = item.src || '';
  isPlaying = true;
  refresh();
}

function renderCustomEmbedBanner() {
  const e = currentCustomEmbed;
  const color = e.color || 'var(--accent)';
  return `
    <div class="player-banner player-banner--playing" style="--svc-color:${color}">
      <div class="player-banner-left">
        <div class="player-eq-large player-eq--active" style="--eq-color:${color}">
          ${eqBars(musicConfig?.appearance?.eq_bars || 16)}
        </div>
        <div class="player-now-playing">
          <div class="player-np-label">NOW PLAYING</div>
          <div class="player-np-service" style="color:${color}">${esc(e.icon || '🎵')} Custom</div>
          <div class="player-np-title">${esc(e.name || 'Embed')}</div>
          <div style="font-size:10px;color:var(--text-dim);margin-top:2px">${esc(e.host || '')}</div>
        </div>
      </div>
      <div class="player-banner-center">
        ${e.html || ''}
      </div>
      <div class="player-banner-right">
        <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__clearPlayer()" title="Close" style="padding:4px 8px">✕</button>
      </div>
    </div>
  `;
}

function refresh() {
  const el = document.getElementById('player-banner');
  if (el) el.innerHTML = renderBanner();
}

document.addEventListener('keydown', (e) => {
  if (e.target?.id === 'player-url' && e.key === 'Enter') {
    e.preventDefault();
    window.__playUrl();
  }
});

// ── Spotify API player ───────────────────────────────────────────────────────

function renderSpotifyBanner() {
  const sp = spotifyState;
  const isActive = sp?.active && sp?.data?.is_playing !== undefined;
  const track = sp?.data?.item;
  const isNowPlaying = sp?.data?.is_playing;
  const progress = sp?.data?.progress_ms || 0;
  const duration = track?.duration_ms || 1;
  const pct = Math.min(100, (progress / duration) * 100).toFixed(1);
  const albumArt = track?.album?.images?.[0]?.url || '';
  const trackName = track?.name || '';
  const artists = track?.artists?.map(a => a.name).join(', ') || '';
  const album = track?.album?.name || '';
  const volume = sp?.data?.device?.volume_percent ?? 100;
  const shuffle = sp?.data?.shuffle_state;

  if (!track) {
    const isPremiumError = sp?.error === 'premium_required';
    const idleMsg = isPremiumError
      ? `<div style="font-size:12px;color:var(--warning)">⚠ Spotify Premium required for playback, or add your account to the app in <a href="https://developer.spotify.com/dashboard" target="_blank" style="color:#1DB954">Spotify Developer Dashboard</a> → User Management</div>`
      : `<div style="font-size:11px;color:var(--text-dim)">Open Spotify and start playing something</div>`;
    return `
      <div class="player-banner" style="background:linear-gradient(90deg,#191414,#1a1a2e);--svc-color:#1DB954">
        <div class="player-eq-large player-eq--idle">${eqBars(12)}</div>
        <div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:4px">
          <div style="font-size:13px;font-weight:600;color:#1DB954">● Spotify Connected</div>
          ${idleMsg}
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn btn-sm" style="background:#1DB954;color:#000;font-weight:600;border:none" onclick="window.__spPlay()">▶ Play</button>
          <button class="btn btn-sm btn-ghost" onclick="window.__spSearch()" style="font-size:11px">🔍 Search</button>
          <button class="btn btn-sm btn-ghost" onclick="window.__spPlaylists()" style="font-size:11px">📋 Playlists</button>
        </div>
      </div>
    `;
  }

  return `
    <div class="player-banner player-banner--playing" style="--svc-color:#1DB954;${albumArt ? `background:linear-gradient(90deg,#191414 0%,#1a1a2e 100%)` : ''}">
      <!-- Album art -->
      ${albumArt ? `<div style="flex-shrink:0;width:64px;height:64px;border-radius:6px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.5)"><img src="${esc(albumArt)}" style="width:100%;height:100%;object-fit:cover"></div>` : ''}

      <!-- EQ + track info -->
      <div class="player-banner-left" style="gap:12px">
        <div class="player-eq-large ${isNowPlaying ? 'player-eq--active' : 'player-eq--idle'}" style="--eq-color:#1DB954">${eqBars(12)}</div>
        <div class="player-now-playing">
          <div class="player-np-label">NOW PLAYING <span style="color:#1DB954;font-size:9px">● SPOTIFY</span></div>
          <div class="player-np-title" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(trackName)}">${esc(trackName)}</div>
          <div style="font-size:11px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(artists)}</div>
          <div style="font-size:10px;color:var(--text-dim);margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(album)}</div>
        </div>
      </div>

      <!-- Progress + controls -->
      <div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:8px;min-width:0">
        <!-- Progress bar -->
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:10px;color:var(--text-dim);flex-shrink:0">${fmtMs(progress)}</span>
          <div style="flex:1;height:4px;background:rgba(255,255,255,.15);border-radius:2px;cursor:pointer;position:relative" onclick="window.__spSeek(event,this,${duration})">
            <div style="height:100%;width:${pct}%;background:#1DB954;border-radius:2px;transition:width .5s linear"></div>
          </div>
          <span style="font-size:10px;color:var(--text-dim);flex-shrink:0">${fmtMs(duration)}</span>
        </div>

        <!-- Buttons -->
        <div style="display:flex;align-items:center;gap:6px;justify-content:center">
          <button class="player-ctrl-btn ${shuffle ? 'active' : ''}" onclick="window.__spShuffle()" title="Shuffle" style="${shuffle ? 'color:#1DB954' : ''}">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg>
          </button>
          <button class="player-ctrl-btn" onclick="window.__spPrev()" title="Previous">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg>
          </button>
          <button onclick="window.__spTogglePlay()" style="background:#1DB954;border:none;border-radius:50%;width:34px;height:34px;display:flex;align-items:center;justify-content:center;cursor:pointer;color:#000;flex-shrink:0" title="${isNowPlaying ? 'Pause' : 'Play'}">
            ${isNowPlaying
              ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`
              : `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`
            }
          </button>
          <button class="player-ctrl-btn" onclick="window.__spNext()" title="Next">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg>
          </button>
          <button class="player-ctrl-btn" onclick="window.__spSearch()" title="Search">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
          </button>
        </div>

        <!-- Volume -->
        <div style="display:flex;align-items:center;gap:6px">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="var(--text-dim)"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>
          <input type="range" min="0" max="100" value="${volume}" style="flex:1;height:3px;accent-color:#1DB954;cursor:pointer" oninput="window.__spVolume(this.value)" title="Volume ${volume}%">
          <span style="font-size:10px;color:var(--text-dim);width:24px;text-align:right">${volume}</span>
        </div>
      </div>

      <!-- Right: search / playlists -->
      <div style="display:flex;flex-direction:column;gap:6px;flex-shrink:0">
        <button class="btn btn-sm btn-ghost" onclick="window.__spSearch()" style="font-size:11px;white-space:nowrap">🔍 Search</button>
        <button class="btn btn-sm btn-ghost" onclick="window.__spPlaylists()" style="font-size:11px;white-space:nowrap">📋 Playlists</button>
        <button class="btn btn-sm btn-ghost btn-danger" onclick="window.__spSearchClose()" style="font-size:10px" title="Disconnect Spotify view">embed ↩</button>
      </div>
    </div>
    <div id="sp-panel" style="display:none"></div>
  `;
}

function fmtMs(ms) {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// ── Spotify polling ──────────────────────────────────────────────────────────

function startSpotifyPoll() {
  stopSpotifyPoll();
  pollSpotify();
  spotifyPollTimer = setInterval(pollSpotify, 5000);
}

function stopSpotifyPoll() {
  if (spotifyPollTimer) { clearInterval(spotifyPollTimer); spotifyPollTimer = null; }
}

async function pollSpotify() {
  try {
    const data = await fetch('/api/spotify/player').then(r => r.json());
    spotifyState = data;
    const el = document.getElementById('player-banner');
    if (el && spotifyConnected) el.innerHTML = renderBanner();
  } catch {}
}

// ── Spotify controls ─────────────────────────────────────────────────────────

window.__spTogglePlay = async () => {
  try {
    const playing = spotifyState?.data?.is_playing;
    await fetch(`/api/spotify/${playing ? 'pause' : 'play'}`, { method: 'POST' });
    setTimeout(pollSpotify, 300);
  } catch {}
};

window.__spPlay  = () => fetch('/api/spotify/play',     { method: 'POST' }).then(() => setTimeout(pollSpotify, 300)).catch(() => {});
window.__spNext  = () => fetch('/api/spotify/next',     { method: 'POST' }).then(() => setTimeout(pollSpotify, 800)).catch(() => {});
window.__spPrev  = () => fetch('/api/spotify/previous', { method: 'POST' }).then(() => setTimeout(pollSpotify, 800)).catch(() => {});

window.__spShuffle = async () => {
  const current = spotifyState?.data?.shuffle_state;
  await fetch(`/api/spotify/shuffle/${!current}`, { method: 'POST' }).catch(() => {});
  setTimeout(pollSpotify, 400);
};

window.__spVolume = async (val) => {
  await fetch(`/api/spotify/volume/${val}`, { method: 'POST' }).catch(() => {});
};

window.__spSeek = async (event, bar, duration) => {
  const rect = bar.getBoundingClientRect();
  const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  const ms = Math.floor(pct * duration);
  await fetch(`/api/spotify/seek/${ms}`, { method: 'POST' }).catch(() => {});
  setTimeout(pollSpotify, 400);
};

window.__spSearch = async () => {
  const panel = document.getElementById('sp-panel');
  if (!panel) return;
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  panel.innerHTML = `
    <div style="background:var(--bg-panel);border-top:1px solid var(--border-dim);padding:12px;display:flex;gap:8px;align-items:center">
      <input id="sp-search-input" type="text" placeholder="Search tracks, artists, playlists..." style="flex:1;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:7px 10px;color:var(--text-primary);font-size:13px" autofocus
        onkeydown="if(event.key==='Enter'){window.__spDoSearch()}">
      <button class="btn btn-sm" style="background:#1DB954;color:#000;border:none;font-weight:600" onclick="window.__spDoSearch()">Search</button>
      <button class="btn btn-sm btn-ghost" onclick="document.getElementById('sp-panel').style.display='none'">✕</button>
    </div>
    <div id="sp-search-results" style="background:var(--bg-panel);border-top:1px solid var(--border-dim);padding:0 12px 12px;max-height:220px;overflow-y:auto"></div>
  `;
  document.getElementById('sp-search-input')?.focus();
};

window.__spSearchClose = () => {
  spotifyConnected = false;
  stopSpotifyPoll();
  refresh();
};

window.__spDoSearch = async () => {
  const q = document.getElementById('sp-search-input')?.value.trim();
  if (!q) return;
  const el = document.getElementById('sp-search-results');
  if (!el) return;
  el.innerHTML = '<div style="padding:8px;color:var(--text-secondary);font-size:12px">Searching...</div>';
  try {
    const data = await fetch(`/api/spotify/search?q=${encodeURIComponent(q)}&type=track,playlist&limit=8`).then(r => r.json());
    const tracks = data.tracks?.items || [];
    const playlists = data.playlists?.items || [];
    const items = [...tracks.slice(0, 5), ...playlists.slice(0, 3)];
    el.innerHTML = items.map(item => {
      const isPlaylist = item.type === 'playlist';
      const name = item.name || '';
      const sub = isPlaylist ? `${item.tracks?.total || 0} tracks` : item.artists?.map(a => a.name).join(', ');
      const art = item.images?.[0]?.url || item.album?.images?.[0]?.url || '';
      const id = item.id;
      const fn = isPlaylist ? `window.__spPlayPlaylist('${id}')` : `window.__spPlayTrack('${id}')`;
      return `<div onclick="${fn}" style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border-dim);cursor:pointer;border-radius:4px" onmouseover="this.style.background='var(--bg-hover)'" onmouseout="this.style.background=''">
        ${art ? `<img src="${esc(art)}" style="width:36px;height:36px;border-radius:4px;flex-shrink:0;object-fit:cover">` : `<div style="width:36px;height:36px;background:var(--bg-input);border-radius:4px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:16px">${isPlaylist ? '📋' : '🎵'}</div>`}
        <div style="min-width:0">
          <div style="font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(name)}</div>
          <div style="font-size:11px;color:var(--text-secondary)">${esc(sub)}</div>
        </div>
      </div>`;
    }).join('') || '<div style="padding:8px;color:var(--text-secondary);font-size:12px">No results</div>';
  } catch(e) { el.innerHTML = `<div style="padding:8px;color:var(--error);font-size:12px">${e.message}</div>`; }
};

window.__spPlayPlaylist = async (id) => {
  await fetch(`/api/spotify/play/playlist/${id}`, { method: 'POST' }).catch(() => {});
  document.getElementById('sp-panel').style.display = 'none';
  setTimeout(pollSpotify, 800);
};

window.__spPlayTrack = async (id) => {
  await fetch(`/api/spotify/play/track/${id}`, { method: 'POST' }).catch(() => {});
  document.getElementById('sp-panel').style.display = 'none';
  setTimeout(pollSpotify, 800);
};

window.__spPlaylists = async () => {
  const panel = document.getElementById('sp-panel');
  if (!panel) return;
  if (panel.style.display !== 'none' && panel.dataset.mode === 'playlists') { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  panel.dataset.mode = 'playlists';
  panel.innerHTML = '<div style="background:var(--bg-panel);border-top:1px solid var(--border-dim);padding:12px;color:var(--text-secondary);font-size:12px">Loading playlists...</div>';
  try {
    const data = await fetch('/api/spotify/playlists?limit=20').then(r => r.json());
    const items = data.items || [];
    panel.innerHTML = `
      <div style="background:var(--bg-panel);border-top:1px solid var(--border-dim);padding:8px 12px;display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:12px;font-weight:600;color:var(--text-secondary)">YOUR PLAYLISTS</span>
        <button class="btn btn-sm btn-ghost" onclick="document.getElementById('sp-panel').style.display='none'">✕</button>
      </div>
      <div style="background:var(--bg-panel);border-top:1px solid var(--border-dim);padding:0 12px 12px;max-height:220px;overflow-y:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;padding-top:8px">
        ${items.map(p => {
          const art = p.images?.[0]?.url || '';
          return `<div onclick="window.__spPlayPlaylist('${jsStr(p.id)}')" style="cursor:pointer;border-radius:6px;overflow:hidden;background:var(--bg-input);padding:8px;display:flex;gap:8px;align-items:center" onmouseover="this.style.background='var(--bg-hover)'" onmouseout="this.style.background='var(--bg-input)'">
            ${art ? `<img src="${esc(art)}" style="width:32px;height:32px;border-radius:3px;flex-shrink:0;object-fit:cover">` : `<div style="width:32px;height:32px;background:var(--bg-void);border-radius:3px;flex-shrink:0;display:flex;align-items:center;justify-content:center">📋</div>`}
            <div style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500">${esc(p.name)}</div>
          </div>`;
        }).join('')}
      </div>
    `;
  } catch(e) { panel.innerHTML = `<div style="padding:12px;color:var(--error);font-size:12px">${e.message}</div>`; }
};

function esc(s) { const el = document.createElement('span'); el.textContent = s || ''; return el.innerHTML; }


function jsStr(s) {
  // Escape for a JS single-quoted string literal inside an HTML double-quoted attribute.
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}
