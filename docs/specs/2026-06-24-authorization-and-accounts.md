# Spec: Authorization, Accounts, and 2FA

Status: Draft
Date: 2026-06-24
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition (`M:\Code\ageniusdesk-ce`)
Release gate: yes (must ship before public release)

## 1. Goal

Give every AgeniusDesk install a real authentication boundary that works even on
a localhost bind. On first run the operator must create an owner account
(username + password). After that, the browser app requires login. Each account
may optionally enable TOTP two-factor. This replaces "trust the edge proxy" as
the only protection while keeping edge auth working for deployments that use it.

Non-goals (this spec): SSO / OAuth / SAML, email-based password reset, org/team
multi-tenancy, the onboarding tour (separate spec).

## 2. Current state (analysis)

What exists today:

- `backend/auth_gate.py`: an opt-in gate. When `AGD_REQUIRE_AUTH=true`, privileged
  routers require either a trusted edge header
  (`Cf-Access-Authenticated-User-Email` / `X-Forwarded-User`) or the shared
  `AGD_ADMIN_TOKEN` as a bearer. Default OFF (no auth at all). There is no concept
  of a logged-in user.
- `backend/modules/admin/router.py`: user CRUD only. `_hash_password` uses
  PBKDF2-HMAC-SHA256, 100k iterations, hex salt; users persist to
  `data/users.json` with roles `viewer | operator | admin`. There is no
  password-verify, no login, no session. Routes are gated by
  `require_trusted_request`.
- `backend/main.py`: CORS, request-size, security-header, and no-cache
  middlewares; SPA served at `/` and `/js/*`; `/ws` is closed (code 1008) unless
  an edge identity is present when `agd_require_auth` is on; public API sub-app at
  `/api/v1` authenticated by `X-API-Key`.
- `backend/config.py`: `Settings` (`agd_require_auth`, `agd_admin_token`, CORS,
  CSP, max request bytes); Fernet `encrypt_value` / `decrypt_value`; `DATA_DIR`;
  `harden_file_permissions()` chmod 600 on sensitive files at startup;
  `SECRET_KEY` persisted to `data/.secret_key`.
- `frontend/js/app.js` `init()`: `GET /api/status`; if `!configured` open the n8n
  setup wizard; then `navigate('dashboard')`.

Gap: no authentication of the human using the browser. Anyone who can reach the
port has full control.

## 3. Design overview

Add a session-based login layer with three identity sources accepted by the gate,
in this precedence:

1. Valid `agd_session` cookie (a logged-in local account). New.
2. Trusted edge identity header (Cloudflare Access / reverse proxy). Existing.
3. `AGD_ADMIN_TOKEN` bearer (automation / break-glass). Existing.

Login enforcement rule (browser):

- Login is REQUIRED unless an edge identity is present, or `AGD_DISABLE_LOGIN=true`
  (dev/localhost escape hatch, logged loudly at startup).
- On first browser visit with no accounts, no edge identity, and login not
  disabled, the SPA shows "Create owner account" and blocks everything else until
  it is created.

This keeps existing Cloudflare Access deployments working untouched (edge identity
satisfies the gate; creating a local account is optional), while a naked-port or
localhost install now gets a mandatory account.

Zero new runtime dependencies. TOTP is implemented in a small audited stdlib
module (`hmac`, `hashlib`, `base64`, `struct`); the QR code is rendered
client-side from an `otpauth://` URI using a vendored generator, so no Pillow and
no CDN call.

## 4. Data model

### 4.1 `data/users.json` (extended, backward compatible)

Each user object:

```json
{
  "username": "michael",
  "display_name": "Michael",
  "role": "admin",
  "password_hash": "<hex>",
  "salt": "<hex>",
  "algo": "pbkdf2_sha256",
  "iterations": 600000,
  "created_at": "2026-06-24T00:00:00Z",
  "password_changed_at": "2026-06-24T00:00:00Z",
  "totp": {
    "enabled": false,
    "secret_enc": "fernet:...",        // base32 TOTP secret, Fernet-encrypted at rest
    "recovery_codes": ["<sha256>", ...] // one-time codes, stored hashed
  }
}
```

Migration: legacy users have no `algo`/`iterations`; treat missing values as
`pbkdf2_sha256` / `100000`. On a successful login where stored params differ from
current defaults, transparently rehash and resave (login-time upgrade).

### 4.2 SQLite table `auth_sessions` (new, in `backend/database.py`)

```sql
CREATE TABLE IF NOT EXISTS auth_sessions (
  id_hash     TEXT PRIMARY KEY,   -- sha256 of the random session token; raw token only in the cookie
  username    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  user_agent  TEXT,
  ip          TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(username);
```

Storing only the hash means a database leak cannot be replayed as a live session.
Sliding expiry: `last_seen` updated on use; session valid while
`now < expires_at`, with `expires_at` extended on activity up to an absolute cap.

### 4.3 Login throttle

In-memory per-username + per-IP counter with lockout. Lost on restart, which is
acceptable for a single-node self-host. Keys: `username`, `client ip`. After
`agd_login_max_attempts` failures, reject for `agd_login_lockout_minutes`.

## 5. Settings (additions to `backend/config.py`)

```
agd_disable_login: bool = False          # AGD_DISABLE_LOGIN: skip browser login (dev/localhost only)
agd_session_ttl_days: int = 14           # sliding session lifetime
agd_session_absolute_days: int = 30      # hard cap regardless of activity
agd_login_max_attempts: int = 8          # failures before lockout
agd_login_lockout_minutes: int = 15
agd_password_min_length: int = 10        # raised from the legacy 6 in admin CRUD
```

Cookie `Secure` flag is derived per response from `x-forwarded-proto` / scheme
(same approach as the existing HSTS header), not a setting.

These are ordinary `Settings` fields, so they participate in the existing
`config_overlay.py` mechanism with no special-casing (the lifespan already calls
`apply_overlay_to_settings`); an operator can set them via env, `.env`, or the
runtime overlay like any other knob. Every new variable above is added to
`.env.example` (with a one-line comment each) and documented in `docs/CONFIG.md`,
both of which are currently near-empty and must be filled as part of this work.

## 6. Backend modules

### 6.1 New `backend/totp.py` (stdlib only)

- `generate_secret() -> str`: 20 random bytes, base32 (no padding).
- `provisioning_uri(secret, account, issuer="AgeniusDesk") -> str`: builds
  `otpauth://totp/...`.
- `verify(secret, code, window=1) -> bool`: RFC 6238, 30s step, 6 digits, SHA-1
  (authenticator-app standard), constant-time compare, accepts +/- one step for
  clock skew.
- `generate_recovery_codes(n=10) -> list[str]` and `hash_recovery_code(code)`.

### 6.2 New `backend/modules/auth/` (router + service)

`auth/service.py` holds password hashing/verify, session create/lookup/revoke,
throttle, and TOTP orchestration. `auth/router.py` exposes:

Unauthenticated:

- `GET  /api/auth/status` -> `{accounts_exist, authenticated, user|null, totp_required, login_disabled, edge_identity|null}`
- `POST /api/auth/setup` (allowed only when `accounts_exist == false`) ->
  body `{username, password, display_name?}`; creates the owner (role `admin`),
  starts a session, sets the cookie; returns `{user}`. 409 if any account exists.
- `POST /api/auth/login` -> body `{username, password}`.
  - success, no 2FA: set session cookie; return `{user}`.
  - success, 2FA enabled: return `{totp_required: true, pending_token}` (a
    short-lived, single-use token, 5 min, bound to the username; NOT a session).
  - failure: generic 401 `{detail: "Invalid username or password"}`; throttle.
- `POST /api/auth/login/totp` -> body `{pending_token, code}`; verifies a TOTP or
  recovery code; on success set session cookie, return `{user}`. Recovery codes
  are single-use: on a successful recovery-code login the matched hash is removed
  from `totp.recovery_codes` and `users.json` is resaved (consumption = deletion
  from the array, not a separate "consumed" list). The response includes
  `recovery_codes_remaining` so the UI can warn when the user is running low and
  prompt regeneration.

Authenticated (session required):

- `POST /api/auth/logout` -> revoke current session, clear cookie.
- `GET  /api/auth/me` -> `{user}`.
- `POST /api/auth/password` -> `{current_password, new_password}`; rehash; revoke
  all other sessions for the user.
- `POST /api/auth/totp/enroll` -> generates a secret (not yet enabled), returns
  `{secret, otpauth_uri}`. Secret stored Fernet-encrypted with `enabled=false`.
- `POST /api/auth/totp/activate` -> `{code}`; verifies against the pending secret,
  sets `enabled=true`, returns `{recovery_codes}` (shown once).
- `POST /api/auth/totp/disable` -> `{password}` (and a code if enabled); clears
  TOTP.

Admin-only account management stays in `admin/router.py` (list/create/delete
users), but `create_user` is updated to use the new hashing params and min length.

### 6.3 Session cookie

- Name `agd_session`; value = random `secrets.token_urlsafe(32)`; DB stores
  `sha256(value)`.
- Attributes: `HttpOnly`, `SameSite=Strict`, `Path=/`, `Secure` when the request
  is HTTPS, `Max-Age` from `agd_session_ttl_days`.
- Rotated on every privilege transition (login, setup, password change) to prevent
  session fixation.

### 6.4 CSRF

Because auth is cookie-based, state-changing API calls need CSRF protection.
Approach: double-submit token.

- On session creation also set a readable (non-HttpOnly) `agd_csrf` cookie with a
  random value.
- The frontend `api.js` sends it back as the `X-AGD-CSRF` header on every
  non-GET request.
- A middleware rejects mutating requests whose header does not match the cookie.

Middleware ordering and exemptions (this is fiddly, so it is pinned here). The
CSRF check is registered as its own `@app.middleware("http")` and, because
Starlette runs HTTP middleware in reverse registration order, it must be added so
it runs after `limit_request_size` and before the route handler; it sits
alongside the existing `security_headers` / `no_cache_static` middlewares in
`main.py`. It enforces ONLY when all of:

- the method is not a safe method (`GET`, `HEAD`, `OPTIONS`), and
- the path starts with `/api/` but not `/api/v1/` (the public API authenticates
  with `X-API-Key` and is exempt), and
- the request carries an `agd_session` cookie (a cookie-authenticated browser
  call is exactly the CSRF-exposed case).

It is skipped when the request authenticates by `Authorization: Bearer
<AGD_ADMIN_TOKEN>` or `X-API-Key` (non-browser callers), or when there is no
session cookie (edge-identity-only or unauthenticated calls, which the gate
handles separately). On mismatch it returns `403 {"detail": "CSRF check failed"}`.

### 6.5 Gate integration (`backend/auth_gate.py`)

Extend `require_trusted_request` so a valid session cookie counts as
authenticated, in addition to edge identity and admin token. Keep the
`agd_require_auth` flag meaning the same thing for the non-session sources; the
session source is always honored when present.

Add a helper `current_user(request) -> dict | None` that returns a single
normalized shape regardless of how the request authenticated, so downstream code
never has to branch on a bare string vs a dict:

```python
{
  "username": str,            # local username, or the edge email, or "admin-token"
  "source": "session" | "edge" | "token",
  "role": "admin" | "operator" | "viewer",
  "email": str | None,        # set for the edge source
}
```

Resolution order matches the gate precedence (session, then edge, then token).
For the `edge` source the role is `admin` (the trusted proxy is the boundary);
for the `token` source the role is `admin` (break-glass). Local sessions carry
the account's stored role.

### 6.6 Role-based access control

The model already names three roles; this defines what they mean and how they are
enforced. Ordering: `viewer < operator < admin`.

| Role | Can |
|------|-----|
| `viewer` | Read-only: dashboard, workflows/executions, errors, insights, status. No mutations. |
| `operator` | Everything viewer can, plus day-to-day actions: activate/deactivate/run/delete workflows, sync/delete errors, switch the active instance, deploy and manage containers, use Code Lab and the assistant. |
| `admin` | Everything, plus the privileged surface: user management, the secrets store, public API keys, env/config reset, vault, themes/settings, and other accounts' security. |

Enforcement: a `require_role(min_role)` dependency built on `current_user`. It is
attached at the router level (the same place `require_trusted_request` is attached
today), so each router declares its minimum role once:

- `admin`: `/api/admin/*`, `/api/vault/*`, settings/theme writes, auth account
  management for other users.
- `operator`: mutating routes under `/api/n8n/*`, `/api/containers/*`,
  `/api/errors/*` (sync/delete), `/api/assistant/*`, `/api/langgraph` if present,
  Code Lab execution.
- `viewer`: GET/read routes.

Practical note for v1: enforcement is coarse and applied per router group, not per
individual route. Read routers stay open to `viewer`; mutating routers require
`operator`; privileged routers require `admin`. When a router mixes reads and
mutations, it requires the stricter role and finer splitting is a follow-up. The
owner account created at first run is `admin`, so a solo operator is unaffected.

### 6.7 WebSocket (`/ws`)

Accept a valid `agd_session` cookie (Starlette exposes `websocket.cookies` on the
handshake) in addition to edge identity. When login is enforced and neither is
present, close with 1008. Note today's `/ws` only checks edge identity, so this is
a real change, not just a config flip.

## 7. Frontend

### 7.1 Boot gate (`frontend/js/app.js`)

`init()` first calls `GET /api/auth/status`:

- `login_disabled` or `edge_identity` present -> proceed to current init.
- `!accounts_exist` -> render the "Create owner account" screen; on success,
  continue to current init (which then runs the existing n8n setup wizard).
- `accounts_exist && !authenticated` -> render the login screen; password step,
  then a TOTP step when `totp_required`.
- `authenticated` -> current init unchanged.

These screens render into the app root before the nav/views mount, so no
privileged view or data load happens pre-auth.

### 7.2 New views/components

- `frontend/js/views/login.js`: owner-setup form, login form, TOTP step. Minimal,
  self-contained, theme-aware. No nav chrome.
- Account section inside the Settings view (not a new nav item): change password,
  enable/disable 2FA (QR from `otpauth_uri` rendered by a vendored
  `frontend/js/vendor/qrcode.min.js`), show/regenerate recovery codes, list and
  revoke active sessions, logout.
- `frontend/js/api.js`: read `agd_csrf` cookie and attach `X-AGD-CSRF` to non-GET
  requests; on any `401` from a privileged call, redirect to the login screen.

### 7.3 Logout affordance and login branding (walkthrough follow-ups)

Two gaps surfaced walking the running app; both are small frontend additions on
top of the auth backend above.

**Logout button in the app chrome.** Section 7.2 puts logout inside the Settings
-> Account section, which is too buried for a routine action. Add a visible
logout control in the main app chrome: a user affordance in the sidebar footer
(showing the logged-in `display_name`/username) with a logout action that calls
`POST /api/auth/logout`, clears local state, and returns to the login screen. The
Settings -> Account logout stays as-is. When the request authenticated via edge
identity or admin token (no local session), the control is hidden, since there is
no session to end.

**Login splash branding.** The owner-setup and login screens (`login.js`) render
a bare card with no branding. Add the AgeniusDesk logo centered above the login
form: the brand mark (image) stacked over the `Agenius`/`Desk` text wordmark
(reusing the existing `.logo` / `.logo-accent` treatment from `index.html`), both
centered above the card's form. The repo has no image mark today, so this work
adds one self-contained vector asset (`frontend/assets/logo.svg`, no CDN/network
fetch) and references it from the auth overlay. Same lockup appears on both the
owner-setup and login states. Keep it theme-aware (inherits the auth overlay's
CSS variables).

## 8. Security checklist

- Password hashing PBKDF2-HMAC-SHA256, 600k iterations, 16-byte random salt,
  per-user params stored, login-time rehash on param drift.
- Constant-time comparison for password hashes, session lookups, TOTP, and
  recovery codes.
- Generic auth errors (never reveal whether the username exists).
- Login throttling + lockout; pending-2FA tokens single-use and short-lived.
- Session token only in an HttpOnly cookie; DB stores only its hash; rotate on
  login/setup/password-change; server-side revocation (logout, logout-all,
  password change).
- `SameSite=Strict` + double-submit CSRF for mutations.
- TOTP secret and recovery codes encrypted/hashed at rest; recovery codes shown
  once.
- Extend `harden_file_permissions()` to also chmod 600 `data/users.json` and
  `data/dashboard.db` (it currently covers only the secret key, secrets, config,
  and scope files; neither the user store nor the session DB is included today).
- No secrets in logs; do not log passwords, tokens, codes, or session values.

## 9. Backward compatibility and rollout

- Fresh install: first browser visit forces owner-account creation, then login.
- Existing Cloudflare Access install (edge identity present): unchanged; no forced
  account; may optionally create accounts.
- Existing naked-port install relying on `AGD_REQUIRE_AUTH`/`AGD_ADMIN_TOKEN`:
  browser now prompts to create an owner account on first visit. Operators who
  truly want no login set `AGD_DISABLE_LOGIN=true`. Automation continues to use
  the admin token bearer.
- Public API (`/api/v1`, `X-API-Key`) is unaffected.

## 10. Testing

- Unit: TOTP vectors (RFC 6238), password hash/verify + rehash, recovery-code
  one-time use, session expiry/sliding/absolute cap, throttle/lockout.
- Integration: setup -> login -> access; wrong password; 2FA enroll/activate/
  login/disable; logout revokes; CSRF rejection on missing/mismatched header;
  edge-identity bypass; `AGD_DISABLE_LOGIN` bypass; `/ws` gating; recovery-code
  single-use (second use rejected); role gating (a `viewer` session is 403'd on a
  mutating route, an `operator` is 403'd on `/api/admin/*`).
- Manual: localhost first-run, QR scan with an authenticator app, recovery-code
  login, session survives container restart (DB-backed), cookie `Secure` behind
  HTTPS proxy.

## 11. File touch list

New:
- `backend/totp.py`
- `backend/modules/auth/__init__.py`, `auth/router.py`, `auth/service.py`,
  `auth/manifest.json`
- `frontend/js/views/login.js`
- `frontend/js/vendor/qrcode.min.js`
- `frontend/assets/logo.svg` (brand mark for the login splash; per Section 7.3)

Changed:
- `backend/config.py` (new settings + `harden_file_permissions` adds
  `users.json` and `dashboard.db`), `backend/database.py` (`auth_sessions`),
  `backend/auth_gate.py` (session source + `current_user` + `require_role`),
  `backend/main.py` (CSRF middleware, `/ws` cookie gate), `backend/modules/admin/
  router.py` (hash params 100k->600k, min length 6->10), router-level
  `require_role` attachments across `n8n_proxy`, `errors`, `containers`,
  `assistant`, `admin`, `vault` (per Section 6.6), `frontend/js/app.js` (boot
  gate), `frontend/js/api.js` (CSRF header + 401 handling), Settings view
  (Account section), `frontend/js/app.js` + sidebar chrome (logout affordance,
  per Section 7.3), `frontend/js/views/login.js` (logo lockup, per Section 7.3),
  `.env.example`, `docs/CONFIG.md`, `CHANGELOG.md`.

## 12. Resolved decisions

1. Account home: a section inside the Settings view, not a new nav item.
2. Session lifetime: 14 days sliding, 30 days absolute cap.
3. Lockout scope: both per-username and per-IP.
4. 2FA stays optional, with a gentle nudge to enable it during onboarding; the
   nudge's placement and copy are owned by the onboarding-tour spec.
5. `current_user()` returns one normalized dict (`username`, `source`, `role`,
   `email`) for all three identity sources; callers never branch on shape.
6. RBAC is enforced coarsely per router group at v1 (`viewer < operator <
   admin`) via a `require_role` dependency; finer per-route splits are a
   follow-up. The first-run owner is `admin`.
7. Recovery codes are consumed by deletion from the stored array (no separate
   "consumed" list); the API returns the remaining count to drive a low-codes
   warning.
