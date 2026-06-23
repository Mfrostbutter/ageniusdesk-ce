"""In-app auth gate for privileged routes (F2).

Today AgeniusDesk trusts edge auth (Cloudflare Access) as the boundary —
privileged routes carry no in-app check (see docs/security.md). That is correct
behind the Access-gated tunnel but unsafe on a naked `0.0.0.0:3000` bind.

This module adds an OPT-IN gate, default OFF (zero behavior change). When
`AGD_REQUIRE_AUTH=true`, privileged routes require either:
  - a recognized edge-auth header (`Cf-Access-Authenticated-User-Email` or
    `X-Forwarded-User`, injected by the trusted reverse proxy), OR
  - the configured `AGD_ADMIN_TOKEN` as a `Authorization: Bearer <token>`.

Apply as a router-level dependency:

    from backend.auth_gate import require_trusted_request
    router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_trusted_request)])

When the flag is off the dependency is a no-op, so it is safe to attach
everywhere now and flip enforcement on later with one env var.

"""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request

from backend.config import settings

logger = logging.getLogger(__name__)

# Headers a trusted edge proxy injects to assert an authenticated user. We trust
# these only because, in the supported deployment, the app is reachable solely
# through the Access-gated tunnel that sets them.
_EDGE_HEADERS = ("cf-access-authenticated-user-email", "x-forwarded-user")


def edge_identity(request: Request) -> str:
    """Return the edge-authenticated user id from trusted headers, or ""."""
    for header in _EDGE_HEADERS:
        value = (request.headers.get(header) or "").strip()
        if value:
            return value
    return ""


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _admin_token_ok(request: Request) -> bool:
    token = settings.agd_admin_token
    # Constant-time compare so a timing side-channel can't reveal the token.
    return bool(token) and hmac.compare_digest(_bearer(request), token)


async def require_trusted_request(request: Request) -> None:
    """FastAPI dependency gating a privileged route.

    No-op when `AGD_REQUIRE_AUTH` is false (default). When true, allow the
    request only if it carries a recognized edge-auth header or the admin token;
    otherwise raise 401.
    """
    if not settings.agd_require_auth:
        return  # gate disabled; edge auth is the boundary
    if edge_identity(request) or _admin_token_ok(request):
        return
    raise HTTPException(status_code=401, detail="Authentication required")


def edge_auth_present_anywhere() -> bool:
    """Best-effort hint for the startup warning: is an edge-auth env configured?

    We can't know per-request at startup, so this is intentionally conservative —
    it only reports whether the operator wired an admin token as a fallback.
    """
    return bool(settings.agd_admin_token)
