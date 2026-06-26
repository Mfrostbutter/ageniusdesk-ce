# NAN Management Console Code Review Findings

Generated: 2026-06-26

This review focused on public-release hardening for the NAN management console, with emphasis on authentication boundaries, exposed API routes, proxy/header trust, path handling, release configuration, tracked secret exposure, and dependency advisories.

The companion patch artifact is `security-hardening.diff`. It captures the focused working-tree diff for the files touched during this hardening pass. The repository had unrelated uncommitted work before the review began, so this artifact is intentionally scoped.

## Executive Summary

The highest-risk issues found during review were fixed before release:

- API routes that were reachable without a consistent internal authentication gate are now private by default.
- Reverse-proxy identity headers are no longer trusted unless explicitly enabled.
- Client IP forwarding is no longer trusted unless explicitly enabled.
- Legacy webhook endpoints can now be protected with a shared bearer token.
- Custom static JavaScript serving and custom theme handling now reject path traversal.
- Public release documentation and example configuration now match the hardened defaults.

The codebase is in a better public-release posture, but it should still be treated as an admin console: do not expose it without authentication, do not mount a Docker socket unless the deployment fully trusts the console, and treat community modules as trusted code.

## Resolved Findings

### Critical: Spoofable Edge Authentication Headers

Status: Fixed

Previously, requests could present headers such as `Cf-Access-Authenticated-User-Email` or `X-Forwarded-User` and be treated as an authenticated edge identity even when the app was reachable directly.

Changes made:

- Added `AGD_TRUST_EDGE_AUTH=false` default.
- Ignored edge identity headers unless `AGD_TRUST_EDGE_AUTH=true`.
- Updated docs to make reverse-proxy auth an explicit, opt-in deployment mode.

Files:

- `backend/auth_gate.py`
- `backend/config.py`
- `.env.example`
- `docs/CONFIG.md`
- `docs/DEPLOY.md`
- `README.md`

### Critical: Inconsistent Internal API Authentication

Status: Fixed

Several internal `/api/*` route groups had uneven authentication behavior. For a public release, the safer default is that internal API endpoints are private unless explicitly listed as public or self-authenticating.

Changes made:

- Added central internal API middleware in `backend/main.py`.
- Kept setup/login/status endpoints public where required.
- Kept `/api/v1/*` on its existing API-key path.
- Left self-authenticating music trigger behavior intact.
- Added centralized token handling for the dashboard MCP surface.

Files:

- `backend/main.py`

### High: Trusted `X-Forwarded-For` Without Proxy Trust Boundary

Status: Fixed

Login throttling and client IP attribution previously accepted forwarded IP headers without a configured trusted proxy boundary.

Changes made:

- Added `AGD_TRUST_FORWARDED_FOR=false` default.
- Login IP detection now ignores `X-Forwarded-For` unless explicitly enabled.

Files:

- `backend/modules/auth/service.py`
- `backend/config.py`
- `.env.example`
- `docs/CONFIG.md`

### High: Legacy Webhook Endpoints Could Remain Open

Status: Fixed with configurable compatibility mode

Legacy webhook endpoints are kept compatible by default, but public deployments can now require a bearer token.

Changes made:

- Added `AGD_WEBHOOK_TOKEN`.
- When configured, legacy webhook calls require `Authorization: Bearer <token>`.
- Documentation recommends using `/api/v1/*` where possible and setting `AGD_WEBHOOK_TOKEN` for public deployments.

Files:

- `backend/main.py`
- `backend/config.py`
- `.env.example`
- `docs/CONFIG.md`
- `docs/DEPLOY.md`
- `README.md`

### High: Theme Path Traversal / Unsafe Theme File Access

Status: Fixed

Theme IDs and custom theme names could influence file paths too directly.

Changes made:

- Added theme ID validation.
- Resolved theme paths under known theme roots only.
- Slugified custom theme names before saving.
- Required active themes to exist before activation.

Files:

- `backend/modules/themes/router.py`

### Medium: Custom JavaScript Static Route Path Traversal

Status: Fixed

The custom JavaScript static route now resolves requested files beneath the frontend `js` directory and rejects traversal outside that root.

Changes made:

- Resolved requested paths under `FRONTEND_DIR/js`.
- Rejected files outside the JavaScript root.
- Served only `.js` files through this custom path.

Files:

- `backend/main.py`

## Verification

Completed checks:

- `uv run python -m compileall backend` passed.
- Focused Ruff check on touched backend files passed.
- Targeted smoke checks passed:
  - `/api/status` remains public.
  - `/api/themes` requires authentication.
  - Spoofed edge-auth headers are rejected when edge trust is disabled.
  - Edge-auth headers work when edge trust is explicitly enabled.
  - `AGD_WEBHOOK_TOKEN` blocks unauthenticated legacy webhook calls.
  - Theme path traversal attempts are rejected.
  - JavaScript path traversal attempts are rejected.
- `pip-audit` found no known vulnerable Python dependencies using both PyPI and OSV advisory sources.
- Focused tracked-file secret scan found no obvious API key or private key patterns.
- `git diff --check` passed, with only pre-existing line-ending warnings in frontend files.

Checks with residual issues:

- `uv run pytest` collected 0 tests, so there is no automated regression suite currently exercising these paths.
- Full-repo Ruff still reports pre-existing style/import issues outside the focused hardening patch.

## Remaining Release Risks

- Docker socket access remains root-equivalent by design. Only mount it in deployments where users of this console are fully trusted administrators.
- Set `AGD_WEBHOOK_TOKEN` before exposing the app on a public URL if legacy webhook endpoints are enabled.
- Keep `AGD_TRUST_EDGE_AUTH=false` unless the app is reachable only through a trusted reverse proxy that strips client-supplied identity headers.
- Keep `AGD_TRUST_FORWARDED_FOR=false` unless the app is behind a trusted proxy that controls forwarded headers.
- Treat community modules as trusted code. They execute Python in-process and are not a sandbox boundary.
- Add automated tests for auth middleware, edge-auth trust behavior, webhook token enforcement, and path traversal protections before making larger follow-up changes.

## Files Included In Focused Diff

- `.env.example`
- `CHANGELOG.md`
- `README.md`
- `backend/auth_gate.py`
- `backend/config.py`
- `backend/main.py`
- `backend/modules/auth/service.py`
- `backend/modules/dashboard_mcp/__init__.py`
- `backend/modules/themes/router.py`
- `docs/CONFIG.md`
- `docs/DEPLOY.md`

