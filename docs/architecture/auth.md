# Authentication and RBAC

AgeniusDesk CE ships a local-account login layer (owner account keyed by email, DB-backed sessions, optional TOTP 2FA, password recovery) layered on top of an internal-API auth gate that can also trust an edge proxy or a break-glass admin token. Identity resolves through a fixed precedence chain, roles are a coarse three-tier ranking, and browser mutations are protected by a double-submit CSRF check. This page documents each piece against the code in `backend/auth_gate.py`, `backend/modules/auth/`, `backend/totp.py`, and the auth middleware in `backend/main.py`. See also [Architecture Overview](overview.md), [Module System](modules.md), [Data Model](data-model.md), [Security](security.md), and [API Reference](api.md).

## Identity model and precedence

`current_user(request)` in `backend/auth_gate.py` resolves any request to one normalized dict (`{"username", "source", "role", "email"}`) or `None`, in strict precedence order:

1. **Local session cookie** (`agd_session`). Validated via `service.session_user(raw)`. `source = "session"`, role taken from the user record.
2. **Trusted edge identity.** When `AGD_TRUST_EDGE_AUTH=true`, the email from `Cf-Access-Authenticated-User-Email` or `X-Forwarded-User` is trusted. `source = "edge"`, `role = "admin"` (the proxy is the boundary).
3. **Admin token bearer.** A constant-time match of `Authorization: Bearer <token>` against `AGD_ADMIN_TOKEN`. `source = "token"`, `role = "admin"`. Break-glass / automation path.

Edge headers are read only when `AGD_TRUST_EDGE_AUTH=true` (`edge_identity()`); they are otherwise ignored, because a client on a directly reachable port could spoof them. The admin-token compare uses `hmac.compare_digest` to avoid a timing side channel.

## Local accounts

The first browser visit on a fresh install forces creation of an **owner** account (`POST /api/auth/setup`, rejected with 409 once any account exists). The owner is created with `role = "admin"`. The login identity **is** the email: `create_owner(email, password, display_name, email)` sets `username == email`, so sessions, lookups, and the TOTP label all key off the email without special-casing.

Password hashing (`backend/modules/auth/service.py`): PBKDF2-HMAC-SHA256, `600_000` iterations (`PBKDF2_ITERATIONS`), 16-byte hex salt, stored as `{password_hash, salt, algo: "pbkdf2_sha256", iterations}` in `users.json`. Legacy accounts default to `100_000` iterations on verify. `verify_password` is constant-time (`hmac.compare_digest`). On a successful login, if `needs_rehash` reports the stored params are below current defaults, the password is transparently re-hashed at login time.

Password composition is enforced on setup, reset, and change by `_validate_password` against these knobs: min length, and require-upper/lower/number/symbol. Email format is a pragmatic shape check (`_EMAIL_RE`: one `@`, a dot in the domain, no spaces), not full RFC 5322; deliverability is proven by the recovery flow.

## RBAC

Roles are ranked `viewer < operator < admin` (`_ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}`). `require_role(min_role)` builds a FastAPI dependency:

- If no identity resolves and login is enforced -> 401.
- If no identity resolves and login is **disabled** (`AGD_DISABLE_LOGIN=true`) -> pass through (open install; the operator opted out).
- If the resolved role ranks below the threshold -> 403.

`require_trusted_request` is the older coarse gate kept for compatibility: a no-op when both `AGD_REQUIRE_AUTH` is false and login is disabled, otherwise it requires any recognized identity (no role check).

The auth router adds `require_session`, a stricter dependency than `current_user`: the identity must exist **and** have `source == "session"`. Edge and token identities manage their credentials out of band, so they get 403 (`"Local account required for this action"`) on password/2FA/session-management endpoints.

## The internal-API auth gate

`require_internal_api_auth` (middleware in `backend/main.py`) is the single auditable default: every `/api/*` route is private unless it is explicitly allowlisted or carries its own machine-token scheme. Non-`/api/` paths pass straight through. The decision flow:

1. Path in `_PUBLIC_API_EXACT` or under a `_PUBLIC_API_PREFIXES` prefix -> allow.
2. Path in `_LEGACY_WEBHOOK_EXACT` -> allow if `_legacy_webhook_ok` (open when `AGD_WEBHOOK_TOKEN` unset; otherwise a constant-time match on `X-Agd-Webhook-Token` or bearer).
3. Path in `_SELF_AUTHENTICATING_EXACT` -> allow (the route authenticates itself).
4. Path under `/api/mcp-dashboard` with a valid `DASHBOARD_MCP_TOKEN` bearer -> allow.
5. Otherwise: if login is not enforced and `AGD_REQUIRE_AUTH` is false -> allow (open install); else require `current_user` to resolve, 401 if not.

### Public-route allowlist

| Entry | Kind | Why open |
|---|---|---|
| `/api/status` | exact | First-paint setup/state probe |
| `/api/health/docker-env` | exact | Frontend container-detection hint |
| `/api/auth/status` | exact | Bootstrap: choose setup vs login vs app |
| `/api/auth/setup` | exact | First-run owner creation |
| `/api/auth/login` | exact | Login |
| `/api/auth/login/totp` | exact | Second-factor step of login |
| `/api/auth/forgot` | exact | Begin password recovery |
| `/api/auth/reset` | exact | Complete password recovery |
| `/api/v1/*` | prefix | Versioned public API; authenticated separately by `X-API-Key` |
| `/api/errors/webhook` | legacy webhook | Machine ingest; gated by optional `AGD_WEBHOOK_TOKEN` |
| `/api/messages/webhook` | legacy webhook | Machine ingest; gated by optional `AGD_WEBHOOK_TOKEN` |
| `/api/music/triggers/fire` | self-authenticating | Route enforces its own auth |
| `/api/mcp-dashboard/*` | machine token | Gated by `DASHBOARD_MCP_TOKEN` bearer |

The WebSocket upgrade at `/ws` is gated the same way as the HTTP boundary: a valid session cookie or an edge identity. Browsers cannot set an `Authorization` header on a WS handshake, so token-only mode does not gate `/ws`. The gate is skipped when login is disabled.

## Sessions

DB-backed in `auth_sessions` (see [Data Model](data-model.md)); only `SHA-256(raw_token)` is stored, so a DB leak cannot be replayed.

- **Issue:** `create_session` mints `secrets.token_urlsafe(32)`, stores its hash plus username, timestamps, truncated user-agent, and client IP, with `expires_at = now + AGD_SESSION_TTL_DAYS`.
- **Validate + slide:** `session_user` rejects expired tokens (deleting them), then slides `expires_at` forward to `min(now + ttl, created_at + AGD_SESSION_ABSOLUTE_DAYS)`. The sliding TTL keeps active sessions alive; the absolute cap measured from creation is a hard ceiling.
- **Cookies:** `set_session_cookies` writes `agd_session` (HttpOnly, `SameSite=Strict`, `Secure` when HTTPS) and a readable `agd_csrf` (non-HttpOnly, same flags). HTTPS is detected via `X-Forwarded-Proto` falling back to the URL scheme, honoring a TLS-terminating proxy.
- **Revocation:** `revoke_session` (one), `revoke_all_for_user` (all, with optional keep-current), `revoke_session_by_id` (by the 12-char id prefix surfaced to the UI). Listing via `GET /api/auth/sessions` flags the current session. Changing the password revokes all **other** sessions; completing a reset revokes **every** session for the account.

## CSRF

`csrf_protect` (middleware in `backend/main.py`) is a double-submit check that fires **only** on a cookie-authenticated browser mutation: a non-safe method (`SAFE_METHODS = {GET, HEAD, OPTIONS}`), an internal `/api/` path that is not the `/api/v1/` public API, and an `agd_session` cookie present. When those hold and the request carries neither a bearer `Authorization` nor an `X-API-Key` (so it is genuinely cookie-driven), the `agd_csrf` cookie must equal the `X-Agd-Csrf` header, else 403.

Bootstrap endpoints are exempt because there is no valid session yet and a stale/foreign `agd_session` cookie must not block first-run setup: `/api/auth/setup`, `/api/auth/login`, `/api/auth/login/totp`, `/api/auth/forgot`, `/api/auth/reset`. Bearer/API-key callers and unauthenticated/edge-only requests are never cookie-CSRF exposed, so they are skipped.

## TOTP 2FA and recovery codes

Pure stdlib RFC 6238 (`backend/totp.py`): HMAC-SHA1, 30-second step, 6 digits, base32 secret. `verify` accepts +/- one step for clock skew and compares every candidate without an early break to keep timing uniform. The QR code is rendered browser-side from the `otpauth://` URI; there is no server-side image library.

Enrollment is a two-step flow keyed to a local session:

1. `POST /api/auth/totp/enroll` -> `totp_enroll` generates a secret, stores it Fernet-encrypted (`secret_enc`) with `enabled=false`, returns the secret + provisioning URI.
2. `POST /api/auth/totp/activate` -> `totp_activate` verifies a code against the pending secret; on success flips `enabled=true` and returns ten recovery codes (shown once, stored as SHA-256 hashes).

At login, if `totp_enabled`, the password step returns `{totp_required, pending_token}` instead of a session. The pending token is in-memory, single-use, 5-minute TTL (`_pending`/`_PENDING_TTL`). `POST /api/auth/login/totp` consumes it and calls `verify_second_factor`, which accepts either a live TOTP code or a recovery code; a matched recovery code is consumed by deletion from the stored array. Disabling 2FA (`POST /api/auth/totp/disable`) requires the password and, when 2FA is active, a valid second factor.

## Forgot / reset flow

`POST /api/auth/forgot` always returns `{"ok": true}` regardless of whether the email exists, so it never reveals account existence. A per-IP rate limit (`forgot_blocked`/`forgot_record`, keyed `forgot-ip:<ip>` so it can neither lock a victim's login nor lock all IPs at once) guards against inbox flooding and account mining; blocked callers still get the uniform 200. The SMTP send is fired with `asyncio.create_task` only when the account exists, because awaiting it conditionally would be a timing oracle.

Reset tokens (`auth_resets`) are DB-backed, single-use, and short-lived (`AGD_PASSWORD_RESET_TTL_MINUTES`). `create_reset_token` issues one active token per user (deleting any prior). `consume_reset_token` deletes the row on first touch regardless of expiry outcome, then enforces expiry. `POST /api/auth/reset` validates the new password, sets it, and revokes every session for the account.

Email delivery (`backend/modules/auth/mailer.py`) uses stdlib SMTP. When `AGD_SMTP_HOST` is unset, the reset link is logged at WARNING instead of sent, so a self-hosted operator without a mail server can still recover access from container logs. SMTP errors are swallowed (never surfaced to the caller) and logged with the link.

## Login throttling

In-memory, per-process (lost on restart, acceptable for a single node). `throttle_record_failure` tracks failures in a sliding window keyed on both `u:<username>` and `ip:<ip>`; reaching `AGD_LOGIN_MAX_ATTEMPTS` within the window sets a lockout for `AGD_LOGIN_LOCKOUT_MINUTES`. `throttle_blocked` short-circuits login with 429, and `throttle_reset` clears both keys on a successful login.

## Environment knobs

| Env var | Default | Effect |
|---|---|---|
| `AGD_DISABLE_LOGIN` | `false` | Skip browser login entirely (dev/localhost only); logged loudly. Makes `require_role`/the API gate no-ops when no identity resolves. |
| `AGD_REQUIRE_AUTH` | `false` | Token/edge hard gate for automation/proxy-fronted installs. |
| `AGD_ADMIN_TOKEN` | `""` | Break-glass bearer token granting admin via `current_user`. |
| `AGD_TRUST_EDGE_AUTH` | `false` | Trust `Cf-Access-Authenticated-User-Email` / `X-Forwarded-User` as identity. Never enable on a directly reachable port. |
| `AGD_TRUST_FORWARDED_FOR` | `false` | Trust the first `X-Forwarded-For` hop as the client IP (sessions, throttle). |
| `AGD_WEBHOOK_TOKEN` | `""` | When set, gates the legacy `/api/errors/webhook` and `/api/messages/webhook` endpoints. |
| `AGD_SESSION_TTL_DAYS` | `14` | Sliding session lifetime; also the session/CSRF cookie max-age. |
| `AGD_SESSION_ABSOLUTE_DAYS` | `30` | Hard cap from creation regardless of activity. |
| `AGD_LOGIN_MAX_ATTEMPTS` | `8` | Failures (per user or per IP) before lockout. |
| `AGD_LOGIN_LOCKOUT_MINUTES` | `15` | Lockout duration and sliding-window length. |
| `AGD_PASSWORD_MIN_LENGTH` | `12` | Minimum password length. |
| `AGD_PASSWORD_REQUIRE_UPPER` | `true` | Require an uppercase letter. |
| `AGD_PASSWORD_REQUIRE_LOWER` | `true` | Require a lowercase letter. |
| `AGD_PASSWORD_REQUIRE_NUMBER` | `true` | Require a digit. |
| `AGD_PASSWORD_REQUIRE_SYMBOL` | `true` | Require a symbol. |
| `AGD_PASSWORD_RESET_TTL_MINUTES` | `30` | Reset-link lifetime. |
| `AGD_SMTP_HOST` | `""` | SMTP server. Unset -> reset links are logged, not emailed. |
| `AGD_SMTP_PORT` | `587` | SMTP port. |
| `AGD_SMTP_USER` | `""` | SMTP auth user (login skipped when blank). |
| `AGD_SMTP_PASSWORD` | `""` | SMTP auth password. |
| `AGD_SMTP_FROM` | `""` | From address; defaults to `AGD_SMTP_USER` when blank. |
| `AGD_SMTP_STARTTLS` | `true` | Issue STARTTLS before auth. |
| `AGD_PUBLIC_URL` | `""` | Base URL for links in emails; falls back to `AGD_PUBLIC_HOST`, then the request origin. |
| `DASHBOARD_MCP_TOKEN` | `""` (env) | Bearer token gating `/api/mcp-dashboard/*`. |
