"""Trusted identity + route policy for community-module routes.

Runs as host middleware for every /api/{community_id}/... request in every
isolation mode. It strips any inbound X-AGD-* header (a browser cannot spoof the
actor), authorizes the request against the module's route classes, and injects
the trusted identity headers the module reads for audit and defense-in-depth.
The reverse proxy forwards those headers to an isolated worker; an in-process
router sees them on request.headers. One policy, both modes.
"""

from __future__ import annotations

import fnmatch
import logging
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from backend import module_registry
from backend.module_registry import ModuleManifest

logger = logging.getLogger(__name__)

_ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_TRUSTED_PREFIX = b"x-agd-"

HDR_USER = "x-agd-user"
HDR_USER_ID = "x-agd-user-id"
HDR_ROLE = "x-agd-role"
HDR_SOURCE = "x-agd-auth-source"
IDENTITY_HEADERS = frozenset({HDR_USER, HDR_USER_ID, HDR_ROLE, HDR_SOURCE})
# Host-owned X-AGD-* headers that are NOT identity and must survive the strip.
_KEEP = frozenset({b"x-agd-csrf"})


def default_min_role(method: str) -> str:
    return "viewer" if method.upper() in _READ_METHODS else "operator"


def min_role_for(manifest: ModuleManifest | None, method: str, path: str) -> str:
    """Host floor (reads viewer, writes operator), raised by any matching
    manifest route class. Never lowered."""
    role = default_min_role(method)
    for decl in (manifest.routes if manifest else []):
        if decl.methods and method.upper() not in decl.methods:
            continue
        if not fnmatch.fnmatchcase(path, decl.pattern):
            continue
        if _ROLE_ORDER.get(decl.min_role, 0) > _ROLE_ORDER.get(role, 0):
            role = decl.min_role
    return role


def community_module_id(path: str) -> str | None:
    """Module id when `path` targets a registered COMMUNITY module, else None."""
    if not path.startswith("/api/"):
        return None
    seg = path[5:].split("/", 1)[0]
    if not seg:
        return None
    entry = module_registry.get_registry().get(seg)
    if entry is None or entry.source != "community":
        return None
    return seg


def strip_trusted_headers(scope: dict) -> None:
    scope["headers"] = [
        (k, v) for k, v in scope.get("headers", [])
        if not k.lower().startswith(_TRUSTED_PREFIX) or k.lower() in _KEEP
    ]


def inject_identity(scope: dict, user: dict) -> None:
    def enc(v: str) -> bytes:
        return str(v or "").encode("latin-1", "replace")

    scope["headers"] = list(scope.get("headers", [])) + [
        (HDR_USER.encode(), enc(user.get("username", ""))),
        (HDR_USER_ID.encode(), enc(user.get("user_id") or user.get("username", ""))),
        (HDR_ROLE.encode(), enc(user.get("role", "viewer"))),
        (HDR_SOURCE.encode(), enc(user.get("source", ""))),
    ]


# A per-process secret an in-process host collector puts on its own self-call
# (see modules/_runtime/contrib.py) to authorize the read as a non-interactive
# system actor. It never leaves the process and is never sent to a browser; the
# middleware reads it before stripping X-AGD-* and drops it, so a worker never
# sees it. A browser cannot forge it — it does not know the value.
INTERNAL_HEADER = "x-agd-internal-token"
_INTERNAL_TOKEN = secrets.token_urlsafe(32)
_SYSTEM_ACTOR = {"username": "agd-system", "user_id": "agd-system", "source": "internal"}


def internal_headers(role: str = "viewer") -> dict[str, str]:
    """Headers a host self-call sets to read a community route as the system
    actor at `role`. In-process use only."""
    return {INTERNAL_HEADER: _INTERNAL_TOKEN, HDR_ROLE: role}


def has_internal_token(request: Request) -> bool:
    """True if this request carries the process internal token. Lets the generic
    internal-api gate pass a host self-call through regardless of middleware
    order; the community identity middleware still strips it and injects the
    system actor."""
    return secrets.compare_digest(request.headers.get(INTERNAL_HEADER, ""), _INTERNAL_TOKEN)


def _internal_role(request: Request) -> str | None:
    """The system-actor role if this request carries the process internal token,
    else None. Read before the X-AGD-* strip."""
    if not secrets.compare_digest(request.headers.get(INTERNAL_HEADER, ""), _INTERNAL_TOKEN):
        return None
    role = request.headers.get(HDR_ROLE, "viewer").strip().lower()
    return role if role in _ROLE_ORDER else "viewer"


async def resolve_identity(request: Request, internal_role: str | None = None) -> dict | None:
    """Host identity, an internal system actor, the open-install synthetic admin,
    or None."""
    from backend.auth_gate import current_user, login_enforced
    from backend.config import settings

    user = await current_user(request)
    if user is not None:
        return user
    if internal_role is not None:
        return {**_SYSTEM_ACTOR, "role": internal_role}
    if not login_enforced() and not settings.agd_require_auth:
        return {"username": "anonymous", "source": "open", "role": "admin", "email": None}
    return None


async def apply(request: Request) -> JSONResponse | None:
    """Middleware body. Returns a response to short-circuit, else None after
    mutating the request scope (headers stripped + trusted identity added)."""
    module_id = community_module_id(request.url.path)
    if module_id is None:
        return None
    # Read the process internal token (if any) BEFORE the strip removes it, so a
    # host self-call is recognized and the worker never sees the token.
    internal_role = _internal_role(request)
    strip_trusted_headers(request.scope)
    entry = module_registry.get_registry().get(module_id)
    manifest = entry.manifest if entry else None
    required = min_role_for(manifest, request.method, request.url.path)
    user = await resolve_identity(request, internal_role=internal_role)
    if user is None:
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    if _ROLE_ORDER.get(user.get("role", "viewer"), 0) < _ROLE_ORDER.get(required, 3):
        return JSONResponse({"detail": f"{required} role required"}, status_code=403)
    inject_identity(request.scope, user)
    request.scope["agd_identity"] = user
    return None
