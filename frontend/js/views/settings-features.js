/**
 * Settings › Features: pick a profile, then switch single features on or off.
 * Core features stay on. Changes apply to the sidebar at once.
 */

import { get, put } from '../api.js';
import * as toast from '../components/toast.js';

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

const GROUPS = { core: 'Always on', operate: 'Operate', build: 'Build', admin: 'Admin' };

export async function renderFeatures(el) {
  // Whichever ancestor is actually scrolled owns the position we restore after the re-render.
  let scrollEl = el.parentElement;
  while (scrollEl && scrollEl.scrollTop === 0) scrollEl = scrollEl.parentElement;
  scrollEl = scrollEl || document.scrollingElement;
  const scrollTop = scrollEl ? scrollEl.scrollTop : 0;
  el.innerHTML = '<div class="spinner"></div>';
  let s;
  try { s = await get('/api/features'); }
  catch (e) { el.innerHTML = `<div class="empty-state"><p>Failed to load: ${esc(e.message)}</p></div>`; return; }
  const isAdmin = window.__role ? window.__role === 'admin' : true;
  const byId = Object.fromEntries(s.features.map(f => [f.id, f]));
  const groups = Object.keys(GROUPS).map(g => [g, s.features.filter(f => f.group === g)]).filter(([, fs]) => fs.length);
  el.innerHTML = `
    <div style="max-width:900px">
      <div class="card" style="margin-bottom:16px">
        <div class="card-header"><span class="card-title">Profile</span>
          <span style="margin-left:auto;font-size:11px;color:var(--text-dim)">${s.profile ? `Current: ${esc(s.profiles.find(p => p.id === s.profile)?.label || s.profile)}` : 'No profile chosen: everything is on'}</span></div>
        <p style="font-size:12px;color:var(--text-secondary);margin:0 0 10px">A profile is a starting set. The switches below adjust it. Switched-off features leave the sidebar and answer 404 on their API.</p>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px">
          ${s.profiles.map(p => `
            <label class="card" style="display:flex;gap:10px;align-items:flex-start;cursor:${isAdmin ? 'pointer' : 'default'};border-color:${p.id === s.profile ? 'var(--accent)' : 'var(--border-dim)'};padding:10px 12px;margin:0">
              <input type="radio" name="feature-profile" value="${esc(p.id)}" ${p.id === s.profile ? 'checked' : ''} ${isAdmin ? '' : 'disabled'} style="margin-top:3px;accent-color:var(--accent);flex-shrink:0">
              <span>
                <span style="display:block;font-weight:600;font-size:13px">${esc(p.label)}</span>
                <span style="display:block;font-size:11px;color:var(--text-secondary);margin-top:4px">${esc(p.description)}</span>
                <span style="display:block;font-size:11px;color:var(--text-dim);margin-top:6px">${p.on.filter(id => !byId[id]?.core).length} optional features on</span>
              </span>
            </label>`).join('')}
        </div>
      </div>
      ${groups.map(([g, fs]) => `
        <div class="card" style="margin-bottom:12px">
          <div class="card-header"><span class="card-title">${GROUPS[g]}</span></div>
          ${fs.map(f => `
            <div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-top:1px solid var(--border-dim)">
              <div style="flex:1">
                <div style="font-size:13px;font-weight:500">${esc(f.label)}
                  <span class="pill ${f.enabled ? 'pill-info' : 'pill-neutral'}" style="font-size:9px;margin-left:6px">${f.enabled ? 'on' : 'off'}</span>
                  ${f.requires.length && !f.requires.every(r => byId[r]?.enabled) ? `<span class="pill pill-neutral" style="font-size:9px;margin-left:6px">needs ${esc(f.requires.join(', '))}</span>` : ''}
                </div>
                <div style="font-size:11px;color:var(--text-secondary)">${esc(f.description)}</div>
              </div>
              ${f.core ? '<span style="font-size:11px;color:var(--text-dim)">always on</span>' : `
              <div style="display:flex;align-items:center;gap:10px;white-space:nowrap">
                ${f.override !== null && f.override !== undefined ? `<button class="btn btn-sm btn-ghost" data-reset="${esc(f.id)}" title="Back to what the profile says" style="font-size:11px">overridden · reset</button>` : ''}
                <label class="toggle-switch" title="${f.enabled ? 'On' : 'Off'}">
                  <input type="checkbox" data-feature="${esc(f.id)}" ${f.enabled ? 'checked' : ''} ${isAdmin ? '' : 'disabled'}>
                  <span class="toggle-slider"></span>
                </label>
              </div>`}
            </div>`).join('')}
        </div>`).join('')}
    </div>`;
  if (scrollEl) scrollEl.scrollTop = scrollTop;
  const apply = async (body, msg) => {
    try {
      await put('/api/features', body);
      toast.success(msg);
      if (window.__reloadFeatures) await window.__reloadFeatures();
      if (window.__applySettingsTabFeatures) window.__applySettingsTabFeatures();
      renderFeatures(el);
    } catch (e) { toast.error(e.message); renderFeatures(el); }
  };
  el.querySelectorAll('input[name="feature-profile"]').forEach(r => r.addEventListener('change', () => {
    const p = s.profiles.find(x => x.id === r.value);
    const hasOverrides = s.features.some(f => f.override !== null && f.override !== undefined);
    apply({ profile: p.id, reset_overrides: hasOverrides ? window.confirm('Drop the per-feature overrides too?') : false }, `Profile: ${p.label}`);
  }));
  const profileOn = new Set((s.profiles.find(p => p.id === (s.profile || 'everything')) || { on: [] }).on);
  el.querySelectorAll('input[data-feature]').forEach(t => t.addEventListener('change', () => {
    const id = t.dataset.feature;
    // Back at the profile's own value means no override is needed.
    const val = t.checked === profileOn.has(id) ? null : t.checked;
    apply({ overrides: { [id]: val } }, `${byId[id].label} ${t.checked ? 'on' : 'off'}`);
  }));
  el.querySelectorAll('[data-reset]').forEach(b => b.addEventListener('click', () => {
    apply({ overrides: { [b.dataset.reset]: null } }, `${byId[b.dataset.reset].label} back to the profile default`);
  }));
}
