"""HA summary aggregation, shared by the public API route and in-process callers.

The /api/v1/ha/summary route is a thin auth wrapper around build_ha_summary().
"""

from __future__ import annotations

from typing import Optional


async def build_ha_summary() -> dict:
    """Aggregated fleet status: one call, every sensor the HA coordinator needs.

    Returns workflow_count, error_count_24h, last_execution_at, health_status,
    version, and the active instance. health_status only reports healthy on a
    successful upstream read; a failed/timed-out n8n call reports degraded so an
    outage is never masked by the workflow_count default (the F6 fix).
    """
    from backend.config import get_active_instance, get_active_instance_id, is_configured
    from backend.modules.errors import collector as err_collector
    from backend.modules.n8n_proxy import client

    active = get_active_instance()
    active_id = get_active_instance_id() or ""

    # Workflow count
    workflow_count = 0
    last_execution_at: Optional[str] = None
    health_status = "unknown"

    if is_configured():
        wf_ok = False
        try:
            wf_data = await client.list_workflows(limit=1)
            # n8n returns {data: [...], nextCursor} or {workflows: [...]}
            data_list = wf_data.get("data") or wf_data.get("workflows") or []
            # For count we need a broader call; use the count heuristic from the data shape.
            # NOTE: list_workflows(limit=1) makes this a heuristic — it reports 1 when the
            # instance has many workflows unless the response carries an explicit `count`.
            workflow_count = wf_data.get("count") or len(data_list)
            wf_ok = True
        except Exception:
            pass

        try:
            exec_data = await client.list_executions(limit=1)
            execs = exec_data.get("data") or []
            if execs:
                last_execution_at = execs[0].get("startedAt") or execs[0].get("stoppedAt")
        except Exception:
            pass

        # Only report healthy on a successful upstream read. A failed/timed-out n8n
        # call leaves workflow_count at its 0 default, which previously read as
        # "healthy" (workflow_count >= 0 is always true) and masked the outage.
        health_status = "healthy" if wf_ok else "degraded"

    error_count_24h = await err_collector.get_error_count_24h(active_id)

    return {
        "workflow_count": workflow_count,
        "error_count_24h": error_count_24h,
        "last_execution_at": last_execution_at,
        "health_status": health_status,
        "version": "0.2.0",
        "instance": {
            "id": active["id"],
            "name": active["name"],
        } if active else None,
    }
