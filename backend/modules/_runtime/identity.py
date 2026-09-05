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


async def resolve_identity(request: Request) -> dict | None:
    """Host identity, or the open-install synthetic admin, or None."""
    from backend.auth_gate import current_user, login_enforced
    from backend.config import settings

    user = await current_user(request)
    if user is not None:
        return user
    if not login_enforced() and not settings.agd_require_auth:
        return {"username": "anonymous", "source": "open", "role": "admin", "email": None}
    return None


async def apply(request: Request) -> JSONResponse | None:
    """Middleware body. Returns a response to short-circuit, else None after
    mutating the request scope (headers stripped + trusted identity added)."""
    module_id = community_module_id(request.url.path)
    if module_id is None:
        return None
    strip_trusted_headers(request.scope)
    entry = module_registry.get_registry().get(module_id)
    manifest = entry.manifest if entry else None
    required = min_role_for(manifest, request.method, request.url.path)
    user = await resolve_identity(request)
    if user is None:
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    if _ROLE_ORDER.get(user.get("role", "viewer"), 0) < _ROLE_ORDER.get(required, 3):
        return JSONResponse({"detail": f"{required} role required"}, status_code=403)
    inject_identity(request.scope, user)
    request.scope["agd_identity"] = user
    return None
