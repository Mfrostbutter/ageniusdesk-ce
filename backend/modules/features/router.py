"""Feature switches: read for everyone, change for admins."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import audit, features
from backend.auth_gate import require_role, require_trusted_request

router = APIRouter(prefix="/api/features", tags=["features"])
_READ = [Depends(require_trusted_request), Depends(require_role("viewer"))]
_ADMIN = [Depends(require_trusted_request), Depends(require_role("admin"))]


class FeaturesBody(BaseModel):
    profile: Optional[str] = None
    overrides: Optional[dict[str, Optional[bool]]] = None
    reset_overrides: bool = False


class ToggleBody(BaseModel):
    enabled: Optional[bool]


@router.get("", dependencies=_READ)
async def get_features():
    return features.summary()


@router.put("", dependencies=_ADMIN)
async def put_features(body: FeaturesBody):
    if body.profile is None and not body.overrides:
        raise HTTPException(status_code=400, detail="profile or overrides required")
    try:
        out = features.set_state(body.profile, body.overrides, body.reset_overrides)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.record("features.update", profile=body.profile, overrides=body.overrides or {})
    return out


@router.put("/{feature_id}", dependencies=_ADMIN)
async def toggle_feature(feature_id: str, body: ToggleBody):
    try:
        out = features.set_state(override={feature_id: body.enabled})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.record("features.toggle", feature=feature_id, enabled=body.enabled)
    return out
