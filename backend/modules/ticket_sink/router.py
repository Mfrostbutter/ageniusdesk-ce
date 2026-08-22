"""Ticket sink API routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.config import get_active_instance_id
from backend.modules.ticket_sink import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ticket-sink", tags=["ticket-sink"])


class TestFirePayload(BaseModel):
    workflow_id: str = "ticket-sink-test"
    workflow_name: str = "Ticket Sink Test"
    node_name: str = "Test Node"
    error_type: str = "TestError"
    error_message: str = "Synthetic error fired from the ticket-sink test endpoint."


@router.get("/status")
async def status():
    cfg = service.sink_config()
    mappings = await service.list_mappings(limit=1000)
    return {
        "enabled": cfg["enabled"],
        "mcp_url_set": bool(cfg["mcp_url"]),
        "client_id_set": bool(cfg["client_id"]),
        "reply_interval_min": cfg["reply_interval_min"],
        "groups_tracked": len(mappings),
    }


@router.get("/mappings")
async def mappings(limit: int = 100):
    return await service.list_mappings(limit=min(max(limit, 1), 1000))


@router.post("/test-fire", dependencies=[Depends(require_role("operator"))])
async def test_fire(payload: TestFirePayload):
    """Run a synthetic error through the sink synchronously and report the action.

    Requires the sink enabled and configured; creates a real PSA ticket.
    """
    cfg = service.sink_config()
    if not cfg["enabled"]:
        raise HTTPException(status_code=409, detail="ticket sink is disabled (AGD_TICKET_SINK_ENABLED)")
    if not cfg["mcp_url"] or not cfg["client_id"]:
        raise HTTPException(status_code=409, detail="ITOPS_MCP_URL / AGD_TICKET_SINK_CLIENT_ID not configured")

    from datetime import datetime, timezone

    error = {
        "instance_id": get_active_instance_id() or "",
        "workflow_id": payload.workflow_id,
        "workflow_name": payload.workflow_name,
        "node_name": payload.node_name,
        "error_type": payload.error_type,
        "error_message": payload.error_message,
        "execution_id": "",
        "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        result = await service.handle_error(error)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"sink failed: {e}") from None
    return result or {"action": "skipped"}
