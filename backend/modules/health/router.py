"""Fleet Health aggregator endpoint.

Merges the n8n-native roll-up (`n8n_proxy.fleet_health`, unchanged) with the rows
contributed by registered fleet sources (aggregator.collect), so the Fleet Health
view renders module health next to the n8n instance cards. Backward-compatible:
`{instances, totals}` is untouched; this ADDS `sources` + `summary`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from backend.auth_gate import require_role
from backend.modules.health import aggregator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["health"], dependencies=[Depends(require_role("viewer"))])

_EMPTY_TOTALS = {
    "instances": 0, "reachable": 0, "workflows_total": 0,
    "workflows_active": 0, "exec_total": 0, "exec_error": 0, "error_rate": 0,
}


@router.get("/fleet")
async def fleet(exec_limit: int = 50):
    """Merged roll-up: n8n instances (native) + contributed sources."""
    from backend.modules.n8n_proxy import client as n8n

    try:
        n8n_roll = await n8n.fleet_health(exec_limit=max(1, min(exec_limit, 250)))
    except Exception as e:   # n8n roll-up is degraded-not-fatal like every source
        logger.warning("fleet-health: n8n roll-up failed: %s", e)
        n8n_roll = {"instances": [], "totals": dict(_EMPTY_TOTALS)}

    contributed = await aggregator.collect()
    return {
        "instances": n8n_roll.get("instances", []),
        "totals": n8n_roll.get("totals", dict(_EMPTY_TOTALS)),
        "sources": contributed["sources"],
        "summary": contributed["summary"],
    }
