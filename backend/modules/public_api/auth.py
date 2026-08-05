"""Public API authentication — X-API-Key header verification.

Usage in route handlers:

    # Any valid key:
    @router.get("/foo")
    async def foo(key: dict = Depends(verify_api_key)): ...

    # Require a specific scope (trigger keys satisfy read routes too):
    from .auth import require_scope
    @router.post("/trigger")
    async def trigger(key: dict = Depends(require_scope("trigger"))): ...

    # Restrict to a workflow the key is allowed to touch:
    from .auth import assert_resource_allowed
    assert_resource_allowed(key, "allowed_workflows", workflow_id)

Beyond the header check, every request through here is expiry-checked,
IP-checked, rate-limited per key, and written to the audit log. The public API is
the one surface reachable by a long-lived bearer credential with no session and
no human in front of it, so it gets the strictest accounting.
"""

import hashlib

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from backend import audit
from backend.config import settings
from backend.ratelimit import TokenBucket, client_ip

from .api_keys import ip_allowed, lookup_by_hash, resource_allowed

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

# trigger keys satisfy both scopes; read keys satisfy only read.
_SCOPE_SATISFIES: dict[str, set[str]] = {
    "read":    {"read", "trigger"},
    "trigger": {"trigger"},
}

# Per-key request budget. Built lazily so AGD_PUBLIC_API_RATE is read after the
# config overlay has been applied at startup, not at import time.
_limiter: TokenBucket | None = None
_limiter_rate: int | None = None


def _get_limiter() -> TokenBucket:
    global _limiter, _limiter_rate
    rate = int(settings.agd_public_api_rate or 0)
    if _limiter is None or _limiter_rate != rate:
        _limiter = TokenBucket(rate)
        _limiter_rate = rate
    return _limiter


async def verify_api_key(request: Request, key: str = Depends(_api_key_header)) -> dict:
    """FastAPI dependency — validates X-API-Key and returns the key record.

    Order matters: identity, then the key's own restrictions (expiry is folded
    into the lookup, then source IP), then the rate limit. A request that fails
    identity must not be able to spend a valid key's budget.
    """
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    record = lookup_by_hash(key_hash)
    if record is None:
        # Covers unknown and expired alike — same answer, no oracle.
        audit.record("public_api.auth", outcome="denied", reason="invalid_or_expired_key",
                     path=request.url.path, ip=client_ip(request))
        raise HTTPException(status_code=401, detail="Invalid API key")

    ip = client_ip(request)
    if not ip_allowed(record, ip):
        audit.record("public_api.auth", outcome="denied", reason="ip_not_allowed",
                     key_id=record["id"], name=record.get("name", ""),
                     path=request.url.path, ip=ip)
        raise HTTPException(status_code=403, detail="This API key is not permitted from this address")

    if not _get_limiter().allow(record["id"]):
        audit.record("public_api.auth", outcome="denied", reason="rate_limited",
                     key_id=record["id"], name=record.get("name", ""),
                     path=request.url.path, ip=ip)
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded for this API key",
            headers={"Retry-After": "60"},
        )

    audit.record("public_api.request", key_id=record["id"], name=record.get("name", ""),
                 scope=record.get("scope", ""), method=request.method,
                 path=request.url.path, ip=ip)
    return record


def require_scope(scope: str):
    """Dependency factory: returns a dep that requires a minimum scope.

    trigger scope is a superset — trigger keys may call read endpoints.
    Apply per-route so the constraint appears in /api/v1/docs.
    """
    satisfying = _SCOPE_SATISFIES[scope]

    async def _dep(key_info: dict = Depends(verify_api_key)) -> dict:
        if key_info.get("scope") not in satisfying:
            audit.record("public_api.auth", outcome="denied", reason="insufficient_scope",
                         key_id=key_info["id"], required=scope, has=key_info.get("scope", ""))
            raise HTTPException(
                status_code=403,
                detail=f"This endpoint requires scope '{scope}'",
            )
        return key_info

    return _dep


def assert_resource_allowed(key_info: dict, field: str, value: str) -> None:
    """Enforce a key's per-resource allowlist, or raise 403.

    Called from handlers rather than a dependency because the resource id only
    exists once the path/body is bound. An empty allowlist permits everything, so
    this is a no-op for keys that do not use the restriction.
    """
    if resource_allowed(key_info, field, value):
        return
    audit.record("public_api.auth", outcome="denied", reason=f"{field}_not_allowed",
                 key_id=key_info["id"], value=value)
    raise HTTPException(status_code=403, detail="This API key is not permitted for that resource")


def _reset_limiter() -> None:
    """Drop the rate-limit state. Test hook."""
    global _limiter, _limiter_rate
    _limiter = None
    _limiter_rate = None
