# Spec: Onboarding Journey and Page Coachmarks

Status: Draft
Date: 2026-06-24
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition (`M:\Code\ageniusdesk-ce`)
Release gate: yes (must ship before public release)
Related: `docs/specs/2026-06-24-authorization-and-accounts.md`

## 1. Goal

Help a new operator get oriented and productive without reading docs. Two
coordinated pieces:

1. A Setup Journey that guides people through what to set up and in what order,
   and stays accurate as they go (resumable, not a one-shot modal).
2. Page Coachmarks: small bubble tips that appear the first time someone lands on
   a view, pointing at the key controls and explaining where things are.

Non-goals: video/help center, in-product chat support, server-driven content,
A/B experimentation.

## 2. Current state (analysis)

- `frontend/js/wizard.js`: a first-run modal wizard with steps
  `welcome -> stack -> secrets -> n8n -> ai -> mirror -> done` and three paths
  (`stand-up-stack`, have-n8n, cloud). Opened by `app.js` when
  `GET /api/status` returns `configured == false`, and reopenable via
  `window.__openWizard()`. It writes secrets, the n8n instance, and AI config,
  then marks setup complete (`POST /api/admin/setup-complete`).
- `frontend/js/app.js`: `navigate(view, opts)` swaps the view into
  `#app-content`, toggles `.nav-btn.active`, and dispatches a
  `agd:view-changed` CustomEvent with `{ detail: { view } }`. Views are ES
  modules exporting `async render(container)`. Nav buttons carry `data-view`.
- Views available today: `dashboard, workflows, errors, import, backup, codelab,
  assistant, music, settings, admin, secrets, notes, insights, knowledge,
  knowledge-connectors, knowledge-instructions, containers` plus namespaced
  `community:*` views.

What is missing: nothing persistent tells a user what is done vs not, and no view
explains itself on first visit. The modal wizard is good for the very first run
but disappears afterward.

## 3. Design overview

- The modal wizard stays as the deep first-run flow. We add a Setup Journey
  surface that is always honest because it derives completion from live app
  state, not a stored "step number."
- A new vanilla-JS coachmark engine listens for `agd:view-changed` and, on the
  first visit to a view, runs that view's tour (a spotlight + bubble walk-through
  anchored to real elements).
- Both pieces are client-side, zero new dependencies, consistent with the
  no-build frontend. Persistence is `localStorage` for v1; per-account
  server-side persistence is noted as a future improvement (Section 9).

## 4. Setup Journey

### 4.1 Milestones (ordered)

Each milestone has an id, title, one-line description, a `done()` predicate
computed from live endpoints, and a CTA that navigates (and may open a wizard
step).

| id | Title | Done when | CTA |
|----|-------|-----------|-----|
| `account` | Create your owner account | `auth/status.accounts_exist` | (handled by auth gate; always done once here) |
| `secure` | Turn on two-factor (recommended) | `auth/me.totp.enabled` | Settings > Account |
| `connect_n8n` | Connect or stand up n8n | `status.configured` or `n8n/instances` non-empty | open wizard |
| `secrets` | Add your provider keys | `admin/secrets` non-empty | Secrets view |
| `ai` | Configure the AI assistant | any `assistant/config.jobs[*]` has a provider+model | Settings > Models |
| `explore` | Meet the harness | user opened the Knowledge view once (`agd_seen:knowledge`) | Knowledge view |

`secure`, `secrets`, `ai`, and `explore` are optional (they do not block use);
`account` and `connect_n8n` are the core path. The journey shows optional items
as "recommended," never as errors.

### 4.2 Completion detection

A single `frontend/js/onboarding/journey.js` exposes
`async function status()` that fetches in parallel and returns a normalized list
`[{id, title, desc, done, optional, cta}]`:

- `GET /api/auth/status` (accounts_exist), `GET /api/auth/me` (totp.enabled)
- `GET /api/status` (configured) and/or `GET /api/n8n/instances`
- `GET /api/admin/secrets`
- `GET /api/assistant/config`
- `localStorage` for `explore`

Failures degrade gracefully (a milestone with an errored probe is treated as not
done, never throws).

### 4.3 Surface

A "Get started" card rendered at the top of the Dashboard view, above the widget
grid, only while there is at least one incomplete core milestone and the user has
not dismissed it.

- Shows a progress line (for example "Setup 2 of 4") and each milestone as a row
  with a check or a CTA button.
- Dismiss control sets `agd_getstarted_dismissed=1`. It also auto-hides once the
  core milestones are complete.
- Re-openable from Settings > Help & Tips ("Reopen setup checklist").

The card is its own component (`frontend/js/components/getstarted.js`), mounted by
`dashboard.js` into the existing `#welcome-n8n-slot` area or a sibling slot, so it
coexists with the current welcome/sign-in banners without competing for the same
slot (render order: get-started card, then welcome banner).

### 4.4 Relationship to the modal wizard

- First run with no n8n: the wizard opens as today. The get-started card is
  redundant during that modal and is simply behind it.
- After the wizard (or if the user closes it early), the card persists and points
  at whatever is still incomplete, so a half-finished setup is recoverable.

## 5. Page Coachmarks

### 5.1 Engine (`frontend/js/components/coachmarks.js`)

`run(viewName, steps, opts)`:

- `steps`: `[{ anchor, title, body, placement }]` where `anchor` is a CSS
  selector (prefer a stable `data-tour="..."` attribute), `placement` one of
  `top|bottom|left|right|auto` (default `auto`).
- Renders a full-screen dim backdrop with a transparent cutout around the
  anchor's bounding rect (the "spotlight"), plus a bubble with title, body, a step
  counter ("2 of 4"), and Back / Next / Skip controls. Final step's Next becomes
  "Got it."
- Positioning keeps the bubble inside the viewport (per the project's
  viewport-constraint rule); it repositions on `resize` and `scroll`.
- If an anchor is missing or off-screen, that step is skipped (tours must tolerate
  views whose optional elements are absent, for example empty states).
- Esc or Skip ends the tour; clicking the backdrop advances is NOT enabled
  (avoids accidental dismissal). Clicking the spotlighted element is allowed.
- Emits nothing to the backend; on completion or skip it sets
  `agd_tour_seen:{viewName}=1`.

### 5.2 Trigger

In `app.js`, after wiring navigation, subscribe once to `agd:view-changed`:

```
document.addEventListener('agd:view-changed', e => onboarding.maybeRunTour(e.detail.view));
```

`maybeRunTour(view)` runs only when ALL of:

- tips are globally enabled (`agd_tips_enabled !== '0'`),
- this view's tour exists and `agd_tour_seen:{view}` is unset,
- the user is authenticated and no blocking overlay is open (login screen, setup
  wizard modal, or any `.modal:not(.hidden)`),
- the view's anchors are present (poll a couple of animation frames after
  `view-changed`, since `render` is async and may have just resolved).

### 5.3 Tour content (initial set)

Defined in `frontend/js/onboarding/tours.js` as a map `view -> steps`. Anchors are
added as `data-tour` attributes on the relevant elements in each view/template.
Initial tours:

- `dashboard`: the widget grid (drag a tile's grip to rearrange; drop on a tile's
  left/right edge to pair side by side), the "+ Widget" button, a stat card, the
  Recent Errors widget.
- `workflows`: the instance switcher, the search/filter, a workflow row's detail
  affordance.
- `errors`: the sync-from-n8n button, an error row (click to expand), the "Ask AI"
  action.
- `codelab`: the editor, the run button, the model picker (live override).
- `knowledge`: what the harness is (workspace files, sources, connectors, agent
  instructions) and where the constitution lives.
- `secrets`: the three stores (Local, Infisical, Agent Vault), adding a `$NAME`
  reference.
- `settings`: the tab strip, the Models section (per-job providers), the Account
  section (password + 2FA).
- `containers`: one-click deploy tiles, container lifecycle controls.
- `mcp`: connection list and the tool inventory with harness vs n8n vs MCP tags.

Each step body is one or two short sentences. Tours range 3 to 5 steps.

### 5.4 Controls and reset

Settings gets a "Help & Tips" section:

- Toggle "Show page tips" (`agd_tips_enabled`).
- "Replay tips on this page" (clears `agd_tour_seen:{current}` and runs it).
- "Reset all tips" (clears every `agd_tour_seen:*` and `agd_getstarted_dismissed`).
- "Reopen setup checklist" and "Reopen setup wizard" (`window.__openWizard()`).

## 6. Storage keys (localStorage)

- `agd_tips_enabled` = `'1' | '0'` (default treated as enabled when unset)
- `agd_tour_seen:<view>` = `'1'`
- `agd_seen:<view>` = `'1'` (first-visit marker used by the `explore` milestone)
- `agd_getstarted_dismissed` = `'1'`

## 7. Files

New:
- `frontend/js/components/coachmarks.js` (spotlight + bubble engine)
- `frontend/js/components/getstarted.js` (dashboard setup card)
- `frontend/js/onboarding/journey.js` (milestone status)
- `frontend/js/onboarding/tours.js` (per-view tour definitions)
- CSS for the coachmark overlay/bubble and the get-started card (in
  `frontend/css/components.css`)

Changed:
- `frontend/js/app.js`: subscribe to `agd:view-changed`; set `agd_seen:<view>` on
  navigate; expose `window.__replayTour` / reset helpers.
- `frontend/js/views/dashboard.js`: mount the get-started card.
- `frontend/js/views/settings.js`: add the Help & Tips section.
- Views/templates: add `data-tour` anchors referenced by `tours.js`.
- `CHANGELOG.md`.

No backend changes required for v1.

## 8. Accessibility and UX

- Bubble is keyboard operable (Tab within controls, Enter = Next, Esc = Skip),
  `role="dialog"`, `aria-live` on the step counter.
- Honors `prefers-reduced-motion` (no spotlight transition animation).
- Never traps the user: Skip is always visible; tips can be globally disabled.
- Tours never auto-run on a view that is showing a blocking modal or an error
  state.

## 9. Open decisions

1. Persistence scope: localStorage (per browser) for v1, or persist
   "seen tours" per account on the server so tips do not reappear on a new device?
   Recommend localStorage for v1, server-side as a fast-follow once accounts ship.
2. Should the get-started card live on the Dashboard only, or also as a dismissible
   global banner until core setup is done? Recommend Dashboard-only to avoid
   chrome on every page.
3. First-run order: run the modal wizard first and suppress page coachmarks until
   it is closed and `connect_n8n` is done (recommended), so a brand-new user is
   not hit with both at once.
