# Frontend Architecture

The AgeniusDesk CE frontend is vanilla JavaScript ES modules with zero build step. The browser loads `/js/app.js` as a `<script type="module">`, and every other module is fetched on demand by native `import`. There is no bundler, no transpiler, and no package install. The backend (`backend/main.py`) serves each `.js` file through a custom route that rewrites relative imports with a per-process cache-busting query string, so a redeploy invalidates the entire module graph without renaming files.

See also: [Architecture Overview](overview.md), [API Reference](api.md), [Security Posture](security.md), [Authentication & RBAC](auth.md), [Module System](modules.md), [Configuration](../CONFIG.md).

## Zero-build ES-module model

There is no compilation. The single entry tag is in `frontend/index.html`:

```html
<script type="module" src="/js/app.js"></script>
```

Two backend handlers in `backend/main.py` make this work and keep deploys fresh:

- `index()` serves `index.html` and rewrites the entry tag to `src="/js/app.js?v=BUILD_ID"`, where `BUILD_ID` is `str(int(time.time()))` captured at process start.
- `serve_js(full_path)` serves any file under `frontend/js/`. Before returning, it runs `_bust_imports()`, a regex (`_IMPORT_RE`) that appends `?v=BUILD_ID` to every relative `.js` import statement in the file body. So `import { get } from './api.js'` is rewritten to `import { get } from './api.js?v=1719...'` on the wire. The query string changes every restart, so browsers (Safari in particular) cannot reuse a stale module after a deploy.

`serve_js` also enforces a path-traversal guard: the requested path is resolved against the `frontend/js` root and rejected with 404 if it escapes the root or is not a `.js` file (see [Security Posture](security.md)). The `no_cache_static` middleware additionally sets `Cache-Control: no-cache, must-revalidate` on `.js`, `.css`, `.html`, `/`, `/js/*`, and `/css/*`.

App version is read separately from `GET /api/status` (`status.version`) and stashed on `window.__appVersion`; it is not the same value as `BUILD_ID`.

The same `GET /api/status` carries `agents_enabled` (the backend's auto-detect of the agent extra, overridable by `AGD_AGENTS_ENABLED`). On boot `app.js` stashes it on `window.__agentsEnabled`; when it is `false` it removes the Agent Fleet nav button and deletes its entry from the `views` map (so a deep-link falls back to the dashboard), and Code Lab drops its Agent Builder mode. A missing field (older backend) is treated as enabled. This is how a default, n8n-only install presents without the agent surface.

## View contract

Every view is an ES module under `frontend/js/views/` that exports an async `render(container)` function. The router calls it with the `#app-content` element and awaits it. The view owns everything inside that container: it writes `innerHTML`, wires its own listeners, and subscribes to WebSocket events as needed.

```js
// frontend/js/views/<name>.js
import { get, post, onEvent } from '../api.js';

export async function render(container) {
  container.innerHTML = `<div class="section-header">...</div>`;
  // wire listeners, fetch data, subscribe to events
}
```

`app.js` imports every built-in view module statically and registers it in the `views` map keyed by its `data-view` name:

| data-view | module |
|---|---|
| `dashboard` | `views/dashboard.js` |
| `workflows` | `views/workflows.js` |
| `errors` | `views/errors.js` |
| `import` | `views/import.js` |
| `backup` | `views/backup.js` |
| `codelab` | `views/codelab.js` |
| `assistant` | `views/assistant.js` |
| `music` | `views/music.js` |
| `settings` | `views/settings.js` |
| `admin` | `views/admin.js` |
| `secrets` | `views/secrets.js` |
| `notes` | `views/notes.js` |
| `insights` | `views/insights.js` |
| `knowledge` | `views/knowledge.js` |
| `knowledge-connectors` | `views/knowledge-connectors.js` |
| `knowledge-instructions` | `views/knowledge-instructions.js` |
| `containers` | `views/containers.js` |
| `instances` | `views/instances.js` |
| `ai-settings` | `views/models.js` |
| `mcp-servers` | `views/mcp.js` |

Note the last three keys map sidebar drill-downs that used to be Settings tabs to dedicated focused views. Community module views are merged in asynchronously after boot (see below) under namespaced keys `community:{module_id}`.

There is no global teardown lifecycle. Views that subscribe to WebSocket events keep their own unsubscribe handle (for example `errors.js` holds `let unsub = null`) and are responsible for cleanup on re-render. State that should persist across navigations (range filters, view modes, dashboard layouts) is kept in `localStorage` / `sessionStorage` by the view itself.

## Navigation and routing

Routing is in-memory, driven by `navigate(viewName, opts)` in `app.js`. There is no history-based router; deep links use the URL hash only on initial load.

`navigate()`:

1. Looks up `views[viewName]`; returns silently if unknown.
2. Records the prior view on `window.__priorView` (so "Replay tips on this page" can target the page the user was on).
3. Sets `currentView`, `window.__currentView`, and stashes `opts` on `window.__viewOpts` for the target view to read on mount.
4. Toggles `.active` on the matching `.nav-btn`.
5. Writes a first-visit marker `agd_seen:{viewName}` to `localStorage`.
6. Dispatches `agd:view-changed` (pre-render, for listeners that must react immediately, e.g. closing an open drawer).
7. `await view.render(...)`.
8. Dispatches `agd:view-rendered` (post-render, so the coachmark engine sees the new view's DOM rather than the previous view's stale content).

Sidebar `.nav-btn` clicks call `navigate(btn.dataset.view)`. A small `SETTINGS_SHORTCUTS` map still routes a few items (`plugins` -> Settings `modules` tab) through `window.__goSettings(tab)` rather than a dedicated view. The hash is honored once at boot: `init()` reads `location.hash`, and navigates there if it names a known view, else to `dashboard`.

Global navigation hooks exposed on `window` (used by inline `onclick` handlers and other modules):

| Global | Purpose |
|---|---|
| `window.__nav` | `navigate()` |
| `window.__currentView` | active view name |
| `window.__priorView` | previously active view |
| `window.__viewOpts` | opts passed to the current `navigate()` call |
| `window.__goSettings(tab)` | navigate to Settings and open a tab |
| `window.__n8nUrl` | browser-reachable URL of the active n8n instance |
| `window.__switchInstance(id)`, `window.__addInstance()`, `window.__refreshInstances` | instance selector |

The sidebar also supports collapsible groups, a Knowledge -> Sources child toggle, drag-to-reorder (persisted to `nav-order-{groupId}`), and per-module visibility toggles (`nav-modules-hidden`), all persisted to `localStorage`.

## api.js: HTTP helper and the CSRF fetch shim

`frontend/js/api.js` is the only HTTP layer. It exports `api(path, options)` plus the verb shorthands `get`, `post`, `put`, `patch`, `del`. All requests are same-origin (`BASE = ''`).

Two things make it load-bearing:

1. **Per-call CSRF on mutations.** `api()` reads the readable `agd_csrf` cookie and echoes it as the `X-AGD-CSRF` header on any non-GET/HEAD request. The backend's `csrf_protect` middleware 403s a cookie-authenticated mutation whose header does not match the cookie (double-submit pattern).
2. **A global `window.fetch` shim.** `patchFetchForCsrf()` monkey-patches `window.fetch` exactly once (guarded by `window.__agdFetchPatched`). For same-origin, non-GET/HEAD requests it injects the `X-AGD-CSRF` header if not already present. This covers raw `fetch()` callers that bypass `api()` (workflow delete, container actions, the player). Without it, anything not routed through `api()` would break under the CSRF middleware.

`api()` also handles **mid-session expiry**: on a `401` for a non-auth path, when the `agd_csrf` cookie is present (meaning a session was once issued), it reloads the page once (`_authRedirecting` guard) to bounce back to the auth gate. Pre-login boot, where many background calls 401 by design, never reloads because the cookie is absent. Errors are normalized: the thrown `Error` carries `.status` and, when the server returns a structured `detail` object, `.errorClass`.

`api()` also **self-heals CSRF**: on a `403` `CSRF check failed` for a non-auth mutation, it re-fetches `GET /api/auth/status` once (the backend re-mints `agd_csrf` for a valid session) and retries the original call once (`_retried` guard). This recovers the cross-port cookie-clobber case with no reload. See [Authentication & RBAC](auth.md#csrf).

## WebSocket client

`api.js` owns a single WebSocket to `/ws` (`wss` under HTTPS). `connectWS()` opens it, auto-reconnects on close after 3s, and dispatches lifecycle pseudo-events `ws:connected` / `ws:disconnected`. Incoming frames are JSON `{event, data}`; the client dispatches `data` to listeners registered for `event`.

Subscription API:

```js
import { onEvent } from '../api.js';
const off = onEvent('error', (data) => { /* ... */ });
// off() to unsubscribe
```

`onEvent(event, cb)` registers a callback and returns an unsubscribe function. `app.js` wires the chrome-level reactions:

| Event | UI reaction |
|---|---|
| `ws:connected` | connection dot -> online, label "Connected" |
| `ws:disconnected` | connection dot -> offline, label "Reconnecting..." |
| `error` | increment and reveal the sidebar error badge |
| `message` | render a toast at the event's `level` (info/success/warning/error) |

Views subscribe to the events they care about (for example the Errors view and dashboard widgets listen for `error`). The backend broadcasts via the `ConnectionManager` (see [Architecture Overview](overview.md)); the WS upgrade itself is auth-gated in `main.py` (see [Authentication & RBAC](auth.md)).

## Coachmark and onboarding engine

The onboarding layer is dependency-free and makes no backend calls, consistent with the no-build frontend. It has three pieces:

- `components/coachmarks.js` — the rendering engine.
- `onboarding/tours.js` — the `TOURS` data: a per-view array of steps `{anchor, title, body, placement}`.
- `onboarding/index.js` — the coordinator that decides whether a tour auto-runs and exposes replay/reset.

**Engine (`run(viewName, steps, opts)`).** It dims the page with an SVG mask, cuts a transparent "spotlight" hole around each step's anchor element, and floats a bubble (title, body, step counter, Back / Next / Skip) beside it. Key behaviors:

- **isVisible filtering.** Before running, steps are filtered to those whose `anchor` selector currently resolves to a visible element. `isVisible()` rejects zero-size elements, `display:none` / `visibility:hidden` / `opacity:0`, and elements inside a closed `<details>` body (`inClosedDetails()` handles the Chrome quirk where a collapsed disclosure still reports a non-zero rect). If no steps survive, the tour marks itself seen and resolves immediately, so empty states never break a tour.
- **body-zoom handling.** The app applies `zoom` to `<body>`. A body-level overlay would inherit that scale and push the spotlight off-canvas. The engine reads the factor with `bodyZoom()` and cancels it on the overlay (`overlay.style.zoom = String(1 / bodyZoom())`) so `getBoundingClientRect`, `window.innerWidth`, and the bubble's measured size all share one coordinate space.
- **Anchor tracking.** `place()` re-reads the anchor rect on every step and on `resize` / `scroll`. If an anchor vanishes mid-tour it advances. `positionBubble()` keeps the bubble inside the viewport (honoring the project viewport-constraint rule) with a correction pass that nudges it fully on-screen.
- **Single tour at a time** via the module-level `_active` guard. `Esc` or Skip ends the tour; `Enter`/arrows navigate. On completion or skip it records `agd_tour_seen:{viewName}=1`.

**Coordinator (`onboarding/index.js`).** `app.js` listens for `agd:view-rendered` and calls `onboarding.maybeRunTour(view)`. `maybeRunTour` self-guards on:

- tips enabled (`agd_tips_enabled` localStorage flag, default on),
- a tour exists for the view in `TOURS`,
- the tour is not already seen,
- no blocking overlay is open (`blockingOverlayOpen()` checks for a visible `.modal`, the auth overlay, or an active coachmark overlay).

Because `render()` is async, it polls up to ~45 animation frames (~750ms) waiting for a step anchor to appear before running, giving slow views (Code Lab's Monaco) time to mount. Replay/reset are exposed on `window` (`window.__replayTour`, `window.__resetTips`, `window.__setTipsEnabled`, `window.__tipsEnabled`); `resetAllTips()` clears every `agd_tour_seen:*` key plus the get-started dismissal.

## Theming via CSS custom properties

Themes are runtime CSS variable overrides, no rebuild. `frontend/js/themes.js`:

- `loadTheme(themeId)` fetches `GET /api/themes/{id}` and calls `applyTheme()`.
- `applyTheme(theme)` writes `theme.colors` entries as `--{key}` custom properties on `document.documentElement`, and `theme.fonts.body` / `theme.fonts.mono` as `--font-body` / `--font-mono`. An optional `theme.effects['matrix-rain']` flag starts/stops the matrix-rain background effect.
- `setActiveTheme(themeId)` POSTs `/api/themes/active/{id}` then re-applies.

On boot, `app.js` reads the active theme from `GET /api/status` (`status.theme`), loads it, and populates the theme `<select>` from `GET /api/themes`. Base variable definitions live in the stylesheet's `:root`; a theme JSON only overrides the subset it names, so a partial theme still renders.

## Adding a new view

1. Create `frontend/js/views/<name>.js` exporting `async function render(container)`. Use `api.js` helpers for I/O and `onEvent` for live updates; keep all DOM inside `container`. Respect the viewport-constraint rule (fixed heights with internal scroll, no page overflow).
2. Import it in `app.js` and add it to the `views` map under its `data-view` key.
3. Add a sidebar button in `frontend/index.html` with `class="nav-btn" data-view="<name>"` (the generic click handler in `app.js` routes it through `navigate()`).
4. Optional: add a `TOURS['<name>']` entry in `onboarding/tours.js` with stable anchor selectors that exist in your rendered markup. The engine skips missing anchors, so partial coverage is fine.
5. Optional: if the view needs persistent UI state, namespace your `localStorage` keys (the codebase uses prefixes like `ageniusdesk:` and `agd-`).

No backend change is required for the view to be served: `serve_js` already serves anything under `frontend/js/` with import cache-busting applied.
