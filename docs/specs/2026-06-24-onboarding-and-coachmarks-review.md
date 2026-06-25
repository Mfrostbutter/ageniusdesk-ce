# Spec Review: Onboarding Journey and Page Coachmarks

**Reviewer:** Claude (automated)
**Date:** 2026-06-25
**Spec:** `docs/specs/2026-06-24-onboarding-and-coachmarks.md`
**Status:** Draft

---

## Overall assessment

The spec is well-structured and the design decisions are sound for v1. The
client-side-only approach with localStorage persistence is the right call for a
first cut. The main concerns are around timing, cross-spec dependency, and a few
missing implementation details.

---

## Findings

### 1. Race condition: `agd:view-changed` fires before render completes (HIGH)

**Section 5.2** says to subscribe to `agd:view-changed` and poll frames for
anchors. In `app.js` line 79, the event is dispatched **before** `await
view.render(...)`. This means when the coachmark engine runs its anchor
existence check, the DOM will be stale (the previous view's content).

The spec acknowledges this by saying "poll a couple of animation frames," but
does not define:
- Max retries / timeout before giving up
- What happens if anchors never appear (error state, empty state in the view)

**Recommendation:** Either (a) move the `agd:view-changed` dispatch to **after**
the `await view.render(...)` call, or (b) define a clear polling contract with a
timeout (e.g., 10 frames / ~160ms) and a fallback behavior.

### 2. Dependency on unimplemented auth endpoints (HIGH)

**Section 4.2** references `GET /api/auth/status` and `GET /api/auth/me` for
milestone completion detection. The companion auth spec
(`2026-06-24-authorization-and-accounts.md`) is also **Draft**. These endpoints
do not exist yet.

The Setup Journey cannot function until the auth spec ships. This should be
called out as an explicit prerequisite in Section 2 (Current State) or in a
"Dependencies" section. Without it, the `account` and `secure` milestones are
dead code.

### 3. `account` milestone is always done (MEDIUM)

**Section 4.1** — The `account` milestone checks `auth/status.accounts_exist`,
and the auth gate forces account creation before the app is usable. This means
the `account` milestone will **always** show as complete by the time the user
sees the Get Started card. It's purely decorative.

**Options:** Remove it from the card display, or make it a permanent checkmark
with no CTA (the spec already says it's "always done once here," but rendering a
row that is always checked is noise).

### 4. `mcp` tour references a view that doesn't exist (MEDIUM)

**Section 5.3** defines a tour for the `mcp` view. **Section 2** lists `mcp` in
the available views. But in `frontend/js/app.js` (the `views` object, lines
31–51), there is **no `mcp` key**. The MCP UI lives as a tab inside the
`settings` view (settings.js line 23: `data-tab="mcp"`).

Either the `mcp` view needs to be registered in `app.js`, or the tour should run
on the `settings` view with MCP-specific steps when the MCP tab is active.
Running `maybeRunTour('mcp')` would resolve to `views['mcp']` which is
`undefined`.

### 5. `.modal:not(.hidden)` selector may produce false positives (MEDIUM)

**Section 5.2** — The guard against running tours when "any `.modal:not(.hidden)`"
is open depends on how modals are structured in the DOM. From `modal.js`:

- `modal.show(id)` targets a pre-rendered element by id (used by wizard,
  `wizard-modal`)
- `openModal()` dynamically creates elements with `className = 'modal'`

The selector `.modal:not(.hidden)` would match **all** dynamically created
modals from `openModal()` even when they're visible, because those elements
never receive a `.hidden` class — they're removed from DOM on close. The
selector would also need to match `wizard-modal` — but that element may not have
the `modal` CSS class (it's referenced by id, not class).

**Recommendation:** Use a more specific guard, e.g., check
`document.querySelector('.modal:not(.hidden), #wizard-modal:not(.hidden)')` or
use a shared `data-modal-open` attribute on `<body>`.

### 6. Missing onboarding coordinator module (MEDIUM)

**Section 5.2** references `onboarding.maybeRunTour(e.detail.view)`. **Section
7** lists new files but does not include an `onboarding/index.js` or equivalent
coordinator that would re-export `maybeRunTour` from `tours.js` and wire the
coachmark engine.

The file list should include `frontend/js/onboarding/index.js` (or the import
path in `app.js` should directly reference `tours.js` or `coachmarks.js`).

### 7. `__replayTour` mechanism underspecified (LOW)

**Section 5.4** says "Replay tips on this page" clears `agd_tour_seen:{current}`
and runs the tour. This implies a `window.__replayTour(viewName)` function, but
the spec doesn't define its signature or whether it lives in `app.js` or
`coachmarks.js`. The settings view needs a concrete function to call.

### 8. localStorage key naming collision risk (LOW)

**Section 6** uses two key patterns:
- `agd_tour_seen:<view>` — coachmark tours
- `agd_seen:<view>` — first-visit marker for the `explore` milestone

These are distinct but semantically overlapping. A future view named with a
prefix that matches could cause confusion. Consider namespacing:
`agd_tour_seen:<view>` and `agd_first_visit:<view>`.

### 9. Spotlights + scroll: underspecified interaction (LOW)

**Section 5.1** says the bubble "repositions on `resize` and `scroll`." But if
the dim backdrop prevents background scrolling (as most spotlight
implementations do), `scroll` events won't fire for user-initiated scroll.
Clarify whether the backdrop freezes scroll, and if so whether scroll-based
repositioning is only for programmatic scroll.

### 10. `aria-modal` missing from coachmark overlay (LOW)

**Section 8** specifies `role="dialog"` and `aria-live` on the step counter. A
full-screen dim backdrop that blocks interaction should also have
`aria-modal="true"` on the dialog element to inform assistive technology that
content behind the overlay is inert.

### 11. `explore` milestone uses Knowledge view visit as proxy (LOW)

**Section 4.1** — The `explore` milestone marks complete when the user has
opened the Knowledge view once. This is a thin proxy for "meet the harness." If
the Knowledge view gains tabs or sub-sections, a single visit may not be
meaningful. Not a blocker for v1, but worth flagging.

---

## Things done well

- **Client-side only, zero dependencies** — exactly right for v1 and consistent
  with the no-build frontend philosophy.
- **Derived state, not stored step number** — the milestone model that queries
  live endpoints is honest and resumable by design. Strong architectural choice.
- **Degrade gracefully on failed probes** — Section 4.2 correctly specifies that
  errored milestones are treated as not-done, never throw.
- **Tours tolerate missing anchors** — Section 5.1 handles empty states and
  optional elements by skipping steps. This prevents broken tours when views
  change.
- **Esc always available, never trap the user** — good accessibility posture.
- **Wizard and Get Started card coexist** — Section 4.4 correctly avoids the
  "two onboarding flows at once" UX trap.

---

## Suggested spec revisions

1. **Add a Dependencies section** listing the auth spec
   (`2026-06-24-authorization-and-accounts.md`) as a prerequisite, specifically
   the `GET /api/auth/status` and `GET /api/auth/me` endpoints.
2. **Move `agd:view-changed` dispatch** to after `render()` completes in
   `app.js`, or define a concrete polling timeout + fallback.
3. **Resolve the `mcp` view question** — either register it in `app.js` or move
   its tour steps into the `settings` tour with MCP-tab awareness.
4. **Add `frontend/js/onboarding/index.js`** to the file list in Section 7.
5. **Define `window.__replayTour(viewName)`** explicitly — signature, where it
   lives, what it clears.
6. **Clarify the `.modal:not(.hidden)` guard** with a concrete selector that
   accounts for both pre-rendered and dynamically-created modals.
7. **Add `aria-modal="true"`** to the coachmark accessibility section.
8. **Clarify scroll behavior** — does the backdrop freeze scroll? If yes, remove
   scroll-based repositioning from Section 5.1.
