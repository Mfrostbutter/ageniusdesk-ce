"""Insights API — execution analytics endpoints.

Single endpoint pattern: GET /api/insights with `range` and `instance_id`
query params. Returns the full payload in one round-trip so the frontend
doesn't have to chain four fetches.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from backend.auth_gate import require_role
from backend.config import get_active_instance_id
from backend.modules.insights.aggregator import get_insights, invalidate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/insights", tags=["insights"])


def _resolve_instance(scope: str) -> str:
    """Same convention as errors router: 'active' resolves to current; 'all' is empty."""
    if not scope or scope == "active":
        return get_active_instance_id() or ""
    if scope == "all":
        return ""
    return scope


@router.get("")
async def get_insights_payload(
    range: str = Query("24h", regex="^(24h|7d|30d)$"),
    instance_id: str = Query("active"),
):
    """Full insights payload for a single (range, instance_id)."""
    inst = _resolve_instance(instance_id)
    return await get_insights(inst, range_key=range)


@router.post("/refresh", dependencies=[Depends(require_role("operator"))])
async def refresh_insights(instance_id: str = Query(""), range: str = Query("")):
    """Drop the cache so the next GET re-fetches from n8n. Used by Refresh button."""
    inst = _resolve_instance(instance_id) if instance_id else ""
    n = invalidate(inst, range)
    return {"dropped": n, "ok": True}
