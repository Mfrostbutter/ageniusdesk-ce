"""Public API authentication — X-API-Key header verification.

Usage in route handlers:

    # Any valid key:
    @router.get("/foo")
    async def foo(key: dict = Depends(verify_api_key)): ...

    # Require a specific scope (trigger keys satisfy read routes too):
    from .auth import require_scope
    @router.post("/trigger")
    async def trigger(key: dict = Depends(require_scope("trigger"))): ...
"""

import hashlib

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from .api_keys import lookup_by_hash

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

# trigger keys satisfy both scopes; read keys satisfy only read.
_SCOPE_SATISFIES: dict[str, set[str]] = {
    "read":    {"read", "trigger"},
    "trigger": {"trigger"},
}


async def verify_api_key(key: str = Depends(_api_key_header)) -> dict:
    """FastAPI dependency — validates X-API-Key and returns the key record."""
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    record = lookup_by_hash(key_hash)
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return record


def require_scope(scope: str):
    """Dependency factory: returns a dep that requires a minimum scope.

    trigger scope is a superset — trigger keys may call read endpoints.
    Apply per-route so the constraint appears in /api/v1/docs.
    """
    satisfying = _SCOPE_SATISFIES[scope]

    async def _dep(key_info: dict = Depends(verify_api_key)) -> dict:
        if key_info.get("scope") not in satisfying:
            raise HTTPException(
                status_code=403,
                detail=f"This endpoint requires scope '{scope}'",
            )
        return key_info

    return _dep
