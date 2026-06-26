/**
 * Page coachmarks — a dependency-free spotlight + bubble walkthrough.
 *
 * run(viewName, steps, opts) dims the page, cuts a transparent "spotlight" hole
 * around each step's anchor element, and floats a bubble (title, body, step
 * counter, Back / Next / Skip) beside it. Steps whose anchor is missing or
 * off-screen are skipped, so a tour tolerates views whose optional elements are
 * absent (empty states). Esc or Skip ends the tour. On completion or skip it
 * records `agd_tour_seen:{viewName}=1`.
 *
 * Zero dependencies, no backend calls — consistent with the no-build frontend.
 */

const PAD = 6; // spotlight padding around the anchor rect
const BUBBLE_GAP = 14; // gap between spotlight and bubble
const seenKey = (view) => `agd_tour_seen:${view}`;

export function tourSeen(view) {
  try { return localStorage.getItem(seenKey(view)) === '1'; } catch { return false; }
}

export function markSeen(view) {
  try { localStorage.setItem(seenKey(view), '1'); } catch { /* ignore */ }
}

export function clearSeen(view) {
  try { localStorage.removeItem(seenKey(view)); } catch { /* ignore */ }
}

let _active = null; // guard: only one tour at a time

const reducedMotion = () =>
  window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// The app applies `zoom` to <body>; a body-level overlay inherits that scale,
// which makes getBoundingClientRect (post-zoom) and CSS coords (re-zoomed)
// disagree and pushes the spotlight off-canvas. Reading the factor lets us
// cancel it on the overlay so everything lives in plain screen pixels.
function bodyZoom() {
  const z = parseFloat(getComputedStyle(document.body).zoom);
  return z && z > 0 ? z : 1;
}

/**
 * Run a tour. Returns a Promise that resolves when the tour ends (completed or
 * skipped). `opts.onEnd(completed)` is also invoked.
 */
export function run(viewName, steps, opts = {}) {
  if (_active) return Promise.resolve(false);
  // Keep only steps whose anchor is currently present and visible.
  const live = (steps || []).filter(s => {
    const elx = document.querySelector(s.anchor);
    return elx && isVisible(elx);
  });
  if (!live.length) { markSeen(viewName); return Promise.resolve(false); }

  return new Promise((resolve) => {
    let idx = 0;
    let done = false;

    const overlay = document.createElement('div');
    overlay.className = 'agd-coach-overlay';
    overlay.innerHTML = `
      <svg class="agd-coach-mask" width="100%" height="100%">
        <defs>
          <mask id="agd-coach-cut">
            <rect width="100%" height="100%" fill="#fff"/>
            <rect class="agd-coach-hole" rx="8" ry="8" fill="#000"/>
          </mask>
        </defs>
        <rect class="agd-coach-dim" width="100%" height="100%" mask="url(#agd-coach-cut)"></rect>
      </svg>
      <div class="agd-coach-ring"></div>
      <div class="agd-coach-bubble" role="dialog" aria-modal="true" aria-label="Page tips" tabindex="-1">
        <div class="agd-coach-title"></div>
        <div class="agd-coach-body"></div>
        <div class="agd-coach-foot">
          <span class="agd-coach-count" aria-live="polite"></span>
          <div class="agd-coach-btns">
            <button type="button" class="btn btn-sm btn-ghost agd-coach-skip">Skip</button>
            <button type="button" class="btn btn-sm btn-ghost agd-coach-back">Back</button>
            <button type="button" class="btn btn-sm btn-primary agd-coach-next">Next</button>
          </div>
        </div>
      </div>`;
    if (reducedMotion()) overlay.classList.add('agd-coach-no-motion');
    // Cancel the inherited body zoom so the overlay maps 1:1 to the screen.
    overlay.style.zoom = String(1 / bodyZoom());
    document.body.appendChild(overlay);
    _active = overlay;

    const hole = overlay.querySelector('.agd-coach-hole');
    const ring = overlay.querySelector('.agd-coach-ring');
    const bubble = overlay.querySelector('.agd-coach-bubble');
    const titleEl = overlay.querySelector('.agd-coach-title');
    const bodyEl = overlay.querySelector('.agd-coach-body');
    const countEl = overlay.querySelector('.agd-coach-count');
    const backBtn = overlay.querySelector('.agd-coach-back');
    const nextBtn = overlay.querySelector('.agd-coach-next');
    const skipBtn = overlay.querySelector('.agd-coach-skip');

    function anchorRect() {
      const elx = document.querySelector(live[idx].anchor);
      if (!elx || !isVisible(elx)) return null;
      elx.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      // The overlay's zoom is neutralized (see run()), so it renders 1:1 with the
      // screen — getBoundingClientRect (screen px), window.innerWidth, and the
      // bubble's offsetWidth all share one coordinate space. No conversion needed.
      return elx.getBoundingClientRect();
    }

    function place() {
      const r = anchorRect();
      if (!r) { next(); return; } // anchor vanished mid-tour: advance
      const x = Math.max(0, r.left - PAD);
      const y = Math.max(0, r.top - PAD);
      const w = r.width + PAD * 2;
      const h = r.height + PAD * 2;
      hole.setAttribute('x', x); hole.setAttribute('y', y);
      hole.setAttribute('width', w); hole.setAttribute('height', h);
      ring.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px`;

      const step = live[idx];
      titleEl.textContent = step.title || '';
      bodyEl.textContent = step.body || '';
      countEl.textContent = `${idx + 1} of ${live.length}`;
      backBtn.style.visibility = idx === 0 ? 'hidden' : 'visible';
      nextBtn.textContent = idx === live.length - 1 ? 'Got it' : 'Next';

      positionBubble(bubble, r, step.placement || 'auto');
      bubble.focus();
    }

    function next() {
      if (idx >= live.length - 1) { end(true); return; }
      idx++; place();
    }
    function back() { if (idx > 0) { idx--; place(); } }

    function end(completed) {
      if (done) return;
      done = true;
      markSeen(viewName);
      window.removeEventListener('resize', place, true);
      window.removeEventListener('scroll', place, true);
      document.removeEventListener('keydown', onKey, true);
      overlay.remove();
      _active = null;
      if (typeof opts.onEnd === 'function') opts.onEnd(completed);
      resolve(completed);
    }

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); end(false); }
      else if (e.key === 'Enter') { e.preventDefault(); next(); }
      else if (e.key === 'ArrowRight') { next(); }
      else if (e.key === 'ArrowLeft') { back(); }
    }

    nextBtn.addEventListener('click', next);
    backBtn.addEventListener('click', back);
    skipBtn.addEventListener('click', () => end(false));
    window.addEventListener('resize', place, true);
    window.addEventListener('scroll', place, true);
    document.addEventListener('keydown', onKey, true);

    place();
  });
}

function isVisible(elx) {
  const r = elx.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return false;
  const st = window.getComputedStyle(elx);
  if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
  // Collapsed <details>: a closed disclosure still reports a non-zero rect for
  // its (hidden) body content in Chrome, which would spotlight a ghost region.
  // Treat any element inside a closed <details> body as not visible.
  return !inClosedDetails(elx);
}

function inClosedDetails(elx) {
  let d = elx.closest('details:not([open])');
  while (d) {
    // A closed <details> still shows its <summary>, so the details element
    // itself (and anything inside its summary) is visible. Only its body is
    // hidden — flag elx when it lives in that hidden body.
    if (d !== elx) {
      const summary = d.querySelector(':scope > summary');
      if (!summary || !summary.contains(elx)) return true;
    }
    d = d.parentElement && d.parentElement.closest('details:not([open])');
  }
  return false;
}

// Keep the bubble inside the viewport (project viewport-constraint rule).
function positionBubble(bubble, r, placement) {
  const M = 10; // viewport margin
  // Use the layout viewport (excludes scrollbar) and measure the bubble's true
  // rendered size via getBoundingClientRect — offsetWidth can disagree with the
  // actual painted box under transforms/zoom.
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  bubble.style.left = '0px';
  bubble.style.top = '0px';
  const m = bubble.getBoundingClientRect();
  const bw = m.width;
  const bh = m.height;

  const fits = {
    bottom: r.bottom + BUBBLE_GAP + bh <= vh,
    top: r.top - BUBBLE_GAP - bh >= 0,
    right: r.right + BUBBLE_GAP + bw <= vw,
    left: r.left - BUBBLE_GAP - bw >= 0,
  };
  let place = placement;
  if (place === 'auto' || !fits[place]) {
    place = ['bottom', 'top', 'right', 'left'].find(p => fits[p]) || 'bottom';
  }

  let left, top;
  if (place === 'bottom' || place === 'top') {
    left = r.left + r.width / 2 - bw / 2;
    top = place === 'bottom' ? r.bottom + BUBBLE_GAP : r.top - BUBBLE_GAP - bh;
  } else {
    top = r.top + r.height / 2 - bh / 2;
    left = place === 'right' ? r.right + BUBBLE_GAP : r.left - BUBBLE_GAP - bw;
  }
  left = Math.min(Math.max(M, left), vw - bw - M);
  top = Math.min(Math.max(M, top), vh - bh - M);
  bubble.style.left = `${left}px`;
  bubble.style.top = `${top}px`;
  bubble.dataset.place = place;

  // Correction pass: re-read the real box and nudge it fully on-screen. Robust
  // to any width/height mismatch in the math above (sub-pixel, scaling, etc.).
  const rr = bubble.getBoundingClientRect();
  let dx = 0, dy = 0;
  if (rr.right > vw - M) dx = (vw - M) - rr.right;
  if (rr.left + dx < M) dx = M - rr.left;
  if (rr.bottom > vh - M) dy = (vh - M) - rr.bottom;
  if (rr.top + dy < M) dy = M - rr.top;
  if (dx || dy) {
    bubble.style.left = `${left + dx}px`;
    bubble.style.top = `${top + dy}px`;
  }
}
