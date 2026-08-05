"""Public API v1 router — versioned, auth-gated endpoints.

Included into a FastAPI sub-app mounted at /api/v1/ in main.py.
Docs auto-generated at /api/v1/docs by FastAPI.

Scope rules:
  read    — GET endpoints (instances, workflows, executions, errors, status)
  trigger — POST /n8n/workflows/{id}/trigger (trigger keys also satisfy read)

Webhook endpoints (/errors/webhook, /messages/webhook) accept any valid key
so n8n global error handlers can call them with a read-scoped key.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import assert_resource_allowed, require_scope, verify_api_key

# No prefix — the sub-app is mounted at /api/v1 which provides it.
router = APIRouter(tags=["public-api-v1"])


def _assert_instance(key: dict) -> str:
    """Enforce the key's allowed_instances against the instance it would hit.

    Every n8n route here resolves to the currently active instance, so a key
    scoped to one instance must not act when a different one happens to be
    active. Returns the resolved instance id.
    """
    from backend.config import get_active_instance_id

    active = get_active_instance_id() or ""
    assert_resource_allowed(key, "allowed_instances", active)
    return active


# ── Read endpoints ────────────────────────────────────────────────────────────


@router.get("/status")
async def v1_status(_key: dict = Depends(require_scope("read"))):
    """System status and version."""
    from backend.config import get_active_instance, is_setup_complete, load_config
    config = load_config()
    active = get_active_instance()
    return {
        "configured": is_setup_complete(),
        "version": "0.2.0",
        "active_instance": {
            "id": active["id"],
            "name": active["name"],
        } if active else None,
        "health_endpoints": config.get("health_endpoints", []),
    }


@router.get("/n8n/instances")
async def v1_list_instances(key: dict = Depends(require_scope("read"))):
    """List configured n8n instances (no API keys exposed)."""
    from backend.config import get_active_instance_id, get_instances
    instances = get_instances()
    active_id = get_active_instance_id()
    allowed = key.get("allowed_instances") or []
    return {
        "instances": [
            {
                "id": i["id"],
                "name": i["name"],
                "active": i["id"] == active_id,
            }
            for i in instances
            if not allowed or i["id"] in allowed
        ]
    }


@router.get("/n8n/workflows")
async def v1_list_workflows(
    active_only: bool = False,
    name_contains: str = "",
    limit: int = 50,
    cursor: str = "",
    key: dict = Depends(require_scope("read")),
):
    """List workflows from the active n8n instance."""
    from backend.config import is_configured
    from backend.modules.n8n_proxy import client

    if not is_configured():
        raise HTTPException(status_code=503, detail="No n8n instances configured")
    _assert_instance(key)
    result = await client.list_workflows(active_only, name_contains, limit, cursor)
    # A workflow-scoped key must not enumerate the rest of the fleet's workflows.
    allowed = key.get("allowed_workflows") or []
    if allowed:
        result["workflows"] = [
            w for w in result.get("workflows", []) if str(w.get("id", "")) in allowed
        ]
    return result


@router.get("/n8n/workflows/{workflow_id}")
async def v1_get_workflow(
    workflow_id: str,
    key: dict = Depends(require_scope("read")),
):
    """Get a single workflow by ID."""
    from backend.config import is_configured
    from backend.modules.n8n_proxy import client

    if not is_configured():
        raise HTTPException(status_code=503, detail="No n8n instances configured")
    _assert_instance(key)
    assert_resource_allowed(key, "allowed_workflows", workflow_id)
    result = await client.get_workflow(workflow_id)
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@router.get("/n8n/executions")
async def v1_list_executions(
    workflow_id: str = "",
    status: str = "",
    limit: int = 20,
    cursor: str = "",
    key: dict = Depends(require_scope("read")),
):
    """List recent workflow executions."""
    from backend.config import is_configured
    from backend.modules.n8n_proxy import client

    if not is_configured():
        raise HTTPException(status_code=503, detail="No n8n instances configured")
    _assert_instance(key)
    allowed = key.get("allowed_workflows") or []
    if allowed:
        # An unfiltered listing would return every workflow's executions, so a
        # workflow-scoped key must name one of its own.
        if not workflow_id:
            raise HTTPException(
                status_code=400,
                detail="This API key is workflow-scoped; pass workflow_id",
            )
        assert_resource_allowed(key, "allowed_workflows", workflow_id)
    return await client.list_executions(workflow_id, status, limit, cursor)


@router.get("/n8n/executions/{execution_id}")
async def v1_get_execution(
    execution_id: str,
    key: dict = Depends(require_scope("read")),
):
    """Get a single execution by ID."""
    from backend.config import is_configured
    from backend.modules.n8n_proxy import client

    if not is_configured():
        raise HTTPException(status_code=503, detail="No n8n instances configured")
    _assert_instance(key)
    result = await client.get_execution(execution_id)
    if not result:
        raise HTTPException(status_code=404, detail="Execution not found")
    # Execution ids are opaque, so the workflow check can only happen after the
    # fetch. A 404 rather than a 403: a workflow-scoped key should not learn that
    # an execution it may not read exists.
    allowed = key.get("allowed_workflows") or []
    if allowed and str(result.get("workflow_id", "")) not in allowed:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result


@router.get("/errors")
async def v1_list_errors(
    limit: int = 50,
    offset: int = 0,
    workflow_id: str = "",
    range: str = "",
    key: dict = Depends(require_scope("read")),
):
    """List recent workflow errors (paginated)."""
    from backend.modules.errors import collector

    scope = _assert_instance(key)
    allowed = key.get("allowed_workflows") or []
    if allowed:
        if not workflow_id:
            raise HTTPException(
                status_code=400,
                detail="This API key is workflow-scoped; pass workflow_id",
            )
        assert_resource_allowed(key, "allowed_workflows", workflow_id)
    errors = await collector.get_errors(limit, offset, workflow_id, range, scope)
    count_24h = await collector.get_error_count_24h(scope)
    return {"errors": errors, "count_24h": count_24h}


# ── Trigger endpoint ──────────────────────────────────────────────────────────


class _TriggerRequest(BaseModel):
    payload: Optional[dict] = None


@router.post("/n8n/workflows/{workflow_id}/trigger")
async def v1_trigger_workflow(
    workflow_id: str,
    req: _TriggerRequest = _TriggerRequest(),
    key: dict = Depends(require_scope("trigger")),
):
    """Trigger a workflow by ID. Requires trigger-scoped API key."""
    from backend.config import is_configured
    from backend.modules.n8n_proxy import client

    if not is_configured():
        raise HTTPException(status_code=503, detail="No n8n instances configured")
    _assert_instance(key)
    assert_resource_allowed(key, "allowed_workflows", workflow_id)
    return await client.trigger_workflow(workflow_id, req.payload)


# ── Webhook endpoints (accept any valid key) ──────────────────────────────────


class _ErrorPayload(BaseModel):
    workflow_id: str = "unknown"
    workflow_name: str = "Unknown Workflow"
    execution_id: str = ""
    node_name: str = ""
    error_message: str = "Unknown error"
    error_type: str = "Error"


@router.post("/errors/webhook")
async def v1_receive_error(
    payload: _ErrorPayload,
    _key: dict = Depends(verify_api_key),
):
    """Receive an error from an n8n global error handler workflow."""
    from backend.config import get_active_instance_id
    from backend.modules.errors import collector

    data = payload.model_dump()
    data.setdefault("instance_id", get_active_instance_id() or "")
    error_id = await collector.store_error(data)
    return {"success": True, "error_id": error_id}


class _MessagePayload(BaseModel):
    title: str = ""
    body: str = ""
    level: str = "info"
    source: str = ""


@router.post("/messages/webhook")
async def v1_receive_message(
    payload: _MessagePayload,
    _key: dict = Depends(verify_api_key),
):
    """Receive a message from any external source."""
    from backend.modules.messages import collector

    message_id = await collector.store_message(payload.model_dump())
    return {"success": True, "message_id": message_id}


# ── HA summary (S2.3) ─────────────────────────────────────────────────────────


@router.get("/ha/summary")
async def v1_ha_summary(_key: dict = Depends(require_scope("read"))):
    """Aggregated status for the Home Assistant coordinator.

    One call returns every sensor the HA DataUpdateCoordinator needs per poll.
    The aggregation lives in summary.build_ha_summary().
    """
    from .summary import build_ha_summary

    return await build_ha_summary()
