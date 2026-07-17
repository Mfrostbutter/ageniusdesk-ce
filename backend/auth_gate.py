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

# Role ranking for coarse RBAC (viewer < operator < admin).
_ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}

# Headers a trusted edge proxy injects to assert an authenticated user. These
# are ignored unless AGD_TRUST_EDGE_AUTH=true.
_EDGE_HEADERS = ("cf-access-authenticated-user-email", "x-forwarded-user")


def edge_identity(request: Request) -> str:
    """Return the edge-authenticated user id from trusted headers, or ""."""
    if not settings.agd_trust_edge_auth:
        return ""
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


async def current_user(request: Request) -> dict | None:
    """Resolve the request's identity into one normalized shape, or None.

    Precedence: local session cookie, then trusted edge header, then admin token.
    The returned dict is uniform so callers never branch on a bare string vs a
    dict:

        {"username": str, "source": "session"|"edge"|"token",
         "role": "admin"|"operator"|"viewer", "email": str|None}
    """
    # 1) Local login session.
    from backend.modules.auth import service as auth_service
    raw = auth_service.session_cookie_value(request.cookies)
    if raw:
        user = await auth_service.session_user(raw)
        if user:
            return {
                "username": user["username"],
                "source": "session",
                "role": user.get("role", "viewer"),
                "email": None,
            }
    # 2) Trusted edge identity (proxy is the boundary, so admin).
    email = edge_identity(request)
    if email:
        return {"username": email, "source": "edge", "role": "admin", "email": email}
    # 3) Admin token bearer (break-glass / automation).
    if _admin_token_ok(request):
        return {"username": "admin-token", "source": "token", "role": "admin", "email": None}
    return None


def login_enforced() -> bool:
    """Whether a logged-in identity is required to use the app.

    True by default; an operator opts out with AGD_DISABLE_LOGIN=true.
    """
    return not settings.agd_disable_login


async def require_trusted_request(request: Request) -> None:
    """FastAPI dependency gating a privileged route.

    Legacy gate kept for compatibility. No-op when both `AGD_REQUIRE_AUTH` is
    false and login is disabled. Otherwise require some recognized identity.
    """
    if not settings.agd_require_auth and not login_enforced():
        return
    if await current_user(request) is not None:
        return
    raise HTTPException(status_code=401, detail="Authentication required")


def role_at_least(user: dict | None, min_role: str) -> bool:
    """True if `user` carries at least `min_role`. None never qualifies.

    Helper for gates that resolve the identity themselves (e.g. the internal-API
    middleware) instead of using the `require_role` dependency.
    """
    if user is None:
        return False
    threshold = _ROLE_ORDER.get(min_role, 3)
    return _ROLE_ORDER.get(user.get("role", "viewer"), 0) >= threshold


def require_role(min_role: str):
    """Build a dependency that requires an authenticated identity of at least
    `min_role`. When login is disabled (open install) it is a no-op.
    """
    threshold = _ROLE_ORDER.get(min_role, 3)

    async def _dep(request: Request) -> dict | None:
        user = await current_user(request)
        if user is None:
            if not login_enforced():
                return None  # open install; operator opted out of auth
            raise HTTPException(status_code=401, detail="Authentication required")
        if _ROLE_ORDER.get(user.get("role", "viewer"), 0) < threshold:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user

    return _dep


def edge_auth_present_anywhere() -> bool:
    """Best-effort hint for the startup warning: is an edge-auth env configured?

    We can't know per-request at startup, so this is intentionally conservative —
    it only reports whether the operator wired an admin token as a fallback.
    """
    return bool(settings.agd_trust_edge_auth or settings.agd_admin_token)
