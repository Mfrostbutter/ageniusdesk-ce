/**
 * Onboarding coordinator — the single entry point app.js wires up.
 *
 * Owns the "should this view's tour auto-run now?" decision and exposes replay /
 * reset helpers. Re-exports the journey status used by the get-started card.
 */

import { TOURS } from './tours.js';
import { run as runTour, tourSeen, clearSeen } from '../components/coachmarks.js';

export { status } from './journey.js';

const TIPS_ENABLED_KEY = 'agd_tips_enabled';
const GETSTARTED_DISMISSED_KEY = 'agd_getstarted_dismissed';

export function tipsEnabled() {
  try { return localStorage.getItem(TIPS_ENABLED_KEY) !== '0'; } catch { return true; }
}

export function setTipsEnabled(on) {
  try { localStorage.setItem(TIPS_ENABLED_KEY, on ? '1' : '0'); } catch { /* ignore */ }
}

// A tour must never fight a blocking overlay: the login gate, the setup wizard,
// or any visible modal (pre-rendered modals toggle `.hidden`; dynamic ones are
// only in the DOM while open).
function blockingOverlayOpen() {
  return !!document.querySelector('.modal:not(.hidden), .agd-auth-overlay, .agd-coach-overlay');
}

/**
 * Auto-run the tour for `view` if all conditions hold. Polls a few animation
 * frames for anchors because `render()` is async and may have just resolved.
 */
export function maybeRunTour(view) {
  if (!tipsEnabled()) return;
  const steps = TOURS[view];
  if (!steps) return;
  if (tourSeen(view)) return;
  if (blockingOverlayOpen()) return;

  let frames = 0;
  const MAX_FRAMES = 45; // ~750ms budget — some views (Code Lab's Monaco) render slowly
  const tick = () => {
    // Conditions can change while we wait (a modal opened, user navigated away).
    if (window.__currentView && window.__currentView !== view) return;
    if (blockingOverlayOpen()) return;
    const present = steps.some(s => document.querySelector(s.anchor));
    if (present) { runTour(view, steps); return; }
    if (++frames < MAX_FRAMES) requestAnimationFrame(tick);
    // Give up silently if no anchor ever appears; the view simply has none.
  };
  requestAnimationFrame(tick);
}

/** Replay the current view's tour (clears its seen flag, then runs it). */
export function replayTour(view) {
  const v = view || window.__currentView;
  if (!v || !TOURS[v]) return false;
  clearSeen(v);
  if (!tipsEnabled()) setTipsEnabled(true);
  maybeRunTour(v);
  return true;
}

/** Forget every seen tour and the get-started dismissal. */
export function resetAllTips() {
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (k && k.startsWith('agd_tour_seen:')) localStorage.removeItem(k);
    }
    localStorage.removeItem(GETSTARTED_DISMISSED_KEY);
  } catch { /* ignore */ }
}
