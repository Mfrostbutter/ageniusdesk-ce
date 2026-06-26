/**
 * Get-started card — the Setup Journey surface on the Dashboard.
 *
 * Renders the milestone checklist derived live by onboarding/journey.js. Shows
 * only while a core milestone is still incomplete and the user has not dismissed
 * it; auto-hides once the core path is done. Re-openable from Settings.
 */

import { status } from '../onboarding/index.js';

const DISMISSED_KEY = 'agd_getstarted_dismissed';

function dismissed() {
  try { return localStorage.getItem(DISMISSED_KEY) === '1'; } catch { return false; }
}

function esc(s) { const d = document.createElement('span'); d.textContent = s == null ? '' : s; return d.innerHTML; }

/**
 * Mount the card into `slot`. `opts.force` shows it even past dismissal /
 * completion (used by "Reopen setup checklist"). Returns true if rendered.
 */
export async function mount(slot, opts = {}) {
  if (!slot) return false;
  if (!opts.force && dismissed()) { slot.innerHTML = ''; return false; }

  let data;
  try { data = await status(); } catch { slot.innerHTML = ''; return false; }

  const coreComplete = data.coreDone >= data.coreTotal;
  if (!opts.force && coreComplete) { slot.innerHTML = ''; return false; }

  const rows = data.milestones.map((m, i) => {
    const check = m.done
      ? `<span class="gs-check gs-check--done" aria-hidden="true">&#10003;</span>`
      : `<span class="gs-check" aria-hidden="true">${i + 1}</span>`;
    const action = m.done
      ? `<span class="gs-done-label">Done</span>`
      : `<button class="btn btn-sm btn-ghost gs-cta" data-mi="${esc(m.id)}">${esc(m.cta.label)}</button>`;
    const tag = m.optional && !m.done ? `<span class="gs-opt">recommended</span>` : '';
    return `
      <div class="gs-row ${m.done ? 'gs-row--done' : ''}">
        ${check}
        <div class="gs-row-text">
          <div class="gs-row-title">${esc(m.title)} ${tag}</div>
          <div class="gs-row-desc">${esc(m.desc)}</div>
        </div>
        <div class="gs-row-action">${action}</div>
      </div>`;
  }).join('');

  slot.innerHTML = `
    <div class="card gs-card">
      <div class="card-header">
        <span class="card-title">Get started</span>
        <span class="gs-progress">Setup ${data.coreDone} of ${data.coreTotal}</span>
        <button class="btn btn-sm btn-ghost gs-dismiss" title="Dismiss">&times;</button>
      </div>
      <div class="gs-rows">${rows}</div>
    </div>`;

  // Wire CTAs to the milestone actions.
  slot.querySelectorAll('.gs-cta').forEach(btn => {
    const m = data.milestones.find(x => x.id === btn.dataset.mi);
    if (m && m.cta && typeof m.cta.run === 'function') {
      btn.addEventListener('click', () => m.cta.run());
    }
  });
  slot.querySelector('.gs-dismiss')?.addEventListener('click', () => {
    try { localStorage.setItem(DISMISSED_KEY, '1'); } catch { /* ignore */ }
    slot.innerHTML = '';
  });
  return true;
}
