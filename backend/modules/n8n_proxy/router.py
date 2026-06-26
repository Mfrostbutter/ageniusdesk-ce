"""n8n proxy API routes — multi-instance support."""

import json
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.config import (
    add_instance,
    decrypt_value,
    get_active_instance,
    get_active_instance_id,
    get_instances,
    is_configured,
    remove_instance,
    set_active_instance,
    update_instance,
)
from backend.modules.n8n_proxy import client

router = APIRouter(prefix="/api/n8n", tags=["n8n"], dependencies=[Depends(require_role("operator"))])


# ── Models ───────────────────────────────────────────────────────────────────


class InstanceRequest(BaseModel):
    name: str
    url: str
    api_key: str
    color: str = ""
    # Optional UI login for this n8n instance, for setups that provision n8n with
    # a known owner account and want to surface those creds in the dashboard.
    # Empty string or omitted = no stored login.
    owner_email: str = ""
    owner_password: str = ""
    # Optional public-facing URL used by the browser when the stored `url` is
    # a compose-internal hostname (e.g. http://n8n-prod:5678 is only reachable
    # from the dashboard container — testers need the mapped host port).
    login_url: str = ""


class TriggerRequest(BaseModel):
    payload: Optional[dict] = None


class ActiveRequest(BaseModel):
    active: bool


def _check_configured():
    if not is_configured():
        raise HTTPException(status_code=503, detail="No n8n instances configured. Add one first.")


# ── Instance management ──────────────────────────────────────────────────────


@router.get("/instances")
async def list_instances():
    """List all configured n8n instances."""
    instances = get_instances()
    active_id = get_active_instance_id()
    safe = []
    for inst in instances:
        # Build a safe hint for the API key
        raw_key = inst.get("api_key", "")
        if raw_key.startswith("$"):
            key_hint = raw_key  # Show $VAR_NAME reference
        else:
            resolved = decrypt_value(raw_key)
            key_hint = resolved[:4] + "..." + resolved[-3:] if len(resolved) > 8 else "configured"
        # `url` is what the dashboard's backend uses to talk to n8n; it can be a
        # compose-internal hostname (e.g. http://n8n:5678) that a browser cannot
        # resolve. `login_url`, when present, is the browser-reachable URL.
        # Frontends should prefer login_url for any href/window.open.
        safe.append({
            "id": inst["id"],
            "name": inst["name"],
            "url": inst["url"],
            "login_url": inst.get("login_url", ""),
            "color": inst.get("color", ""),
            "active": inst["id"] == active_id,
            "key_hint": key_hint,
            "has_login": bool(inst.get("owner_email") and inst.get("owner_password")),
        })
    return {"instances": safe, "active_id": active_id}


@router.post("/instances")
async def create_instance(req: InstanceRequest):
    """Add a new n8n instance and test the connection."""
    browser_url = req.url.rstrip("/")
    # When the dashboard runs in Docker, a localhost URL must be reached via
    # host.docker.internal from inside the container. Store that as the backend
    # `url`, and keep the original localhost URL as the browser-facing login_url.
    backend_url = client.dockerize_url(browser_url)
    login_url = req.login_url.rstrip("/") or (browser_url if backend_url != browser_url else "")
    inst = {
        "id": secrets.token_hex(8),
        "name": req.name,
        "url": backend_url,
        "api_key": req.api_key,
        "color": req.color,
        "owner_email": req.owner_email,
        "owner_password": req.owner_password,
        "login_url": login_url,
    }

    # Test connection before saving
    result = await client.test_connection_with(inst["url"], inst["api_key"])
    if not result["connected"]:
        raise HTTPException(
            status_code=400,
            detail={"message": result.get("message") or "Could not connect to n8n.", "error_class": result.get("error_class", "generic")},
        )

    add_instance(inst)
    return {"success": True, "instance": {"id": inst["id"], "name": inst["name"], "url": inst["url"]}}


@router.put("/instances/{instance_id}")
async def edit_instance(instance_id: str, req: InstanceRequest):
    """Update an existing instance."""
    browser_url = req.url.rstrip("/")
    backend_url = client.dockerize_url(browser_url)
    updates = {
        "name": req.name,
        "url": backend_url,
        "api_key": req.api_key,
        "color": req.color,
        "owner_email": req.owner_email,
        "owner_password": req.owner_password,
        "login_url": req.login_url.rstrip("/") or (browser_url if backend_url != browser_url else ""),
    }
    if not update_instance(instance_id, updates):
        raise HTTPException(status_code=404, detail="Instance not found")
    return {"success": True}


@router.get("/instances/{instance_id}/login")
async def get_instance_login(instance_id: str):
    """Return the stored n8n owner login (URL / email / password) for the given
    instance. Used by the dashboard's 'Sign in to n8n' affordance so beta
    testers don't have to read data/n8n-credentials.txt by hand.

    404 if the instance doesn't exist. 404 if no login is stored — callers use
    `has_login` on the list endpoint to decide whether to show the affordance.
    """
    for inst in get_instances():
        if inst["id"] != instance_id:
            continue
        email = inst.get("owner_email", "")
        stored_pw = inst.get("owner_password", "")
        if not email or not stored_pw:
            raise HTTPException(status_code=404, detail="No stored login for this instance")
        # Prefer the explicit login_url (browser-reachable) over url (which
        # may be a compose-internal hostname like http://n8n-prod:5678).
        login_url = inst.get("login_url") or inst["url"]
        return {
            "url": decrypt_value(login_url),
            "email": email,
            "password": decrypt_value(stored_pw),
        }
    raise HTTPException(status_code=404, detail="Instance not found")


@router.delete("/instances/{instance_id}")
async def delete_instance(instance_id: str):
    """Remove an n8n instance."""
    if not remove_instance(instance_id):
        raise HTTPException(status_code=404, detail="Instance not found")
    return {"success": True}


@router.post("/instances/{instance_id}/activate")
async def activate_instance(instance_id: str):
    """Set an instance as the active one."""
    if not set_active_instance(instance_id):
        raise HTTPException(status_code=404, detail="Instance not found")
    return {"success": True, "active_id": instance_id}


# Legacy setup endpoint — creates first instance
@router.post("/setup")
async def setup(req: InstanceRequest):
    """Legacy setup — adds as first instance."""
    inst = {
        "id": secrets.token_hex(8),
        "name": req.name if req.name else req.url.split("//")[-1].split(".")[0],
        "url": req.url.rstrip("/"),
        "api_key": req.api_key,
        "color": req.color,
    }
    result = await client.test_connection_with(inst["url"], inst["api_key"])
    if not result["connected"]:
        raise HTTPException(
            status_code=400,
            detail={"message": result.get("message") or "Could not connect to n8n.", "error_class": result.get("error_class", "generic")},
        )
    add_instance(inst)
    return {"success": True, "n8n_url": inst["url"]}


@router.get("/test")
async def test_connection():
    """Test active n8n connection."""
    _check_configured()
    return await client.test_connection()


class TestCredsRequest(BaseModel):
    url: str
    api_key: str


@router.post("/test-creds")
async def test_creds(req: TestCredsRequest):
    """Test a URL+API-key pair without saving. Used by the setup wizard."""
    return await client.test_connection_with(req.url.rstrip("/"), req.api_key)


# ── Workflow & execution proxies ─────────────────────────────────────────────


@router.get("/workflows")
async def list_workflows(active_only: bool = False, name_contains: str = "", limit: int = 50, cursor: str = ""):
    _check_configured()
    return await client.list_workflows(active_only, name_contains, limit, cursor)


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str):
    _check_configured()
    result = await client.get_workflow(workflow_id)
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@router.post("/workflows/{workflow_id}/trigger")
async def trigger_workflow(workflow_id: str, req: TriggerRequest = TriggerRequest()):
    _check_configured()
    return await client.trigger_workflow(workflow_id, req.payload)


@router.post("/workflows/{workflow_id}/inject-trigger")
async def inject_dashboard_trigger(workflow_id: str):
    _check_configured()
    return await client.inject_dashboard_trigger(workflow_id)


@router.delete("/workflows/{workflow_id}/inject-trigger")
async def remove_dashboard_trigger(workflow_id: str):
    _check_configured()
    return await client.remove_dashboard_trigger(workflow_id)


@router.post("/workflows/{workflow_id}/active")
async def set_active(workflow_id: str, req: ActiveRequest):
    _check_configured()
    return await client.set_workflow_active(workflow_id, req.active)


@router.delete("/workflows/archived")
async def delete_archived_workflows():
    """Hard-delete every workflow flagged isArchived=true on the active n8n instance.

    Returns {success, deleted, failed, errors[], items[]}. Partial failures don't abort.
    """
    _check_configured()
    return await client.delete_archived_workflows()


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str):
    """Hard-delete a workflow from the active n8n instance."""
    _check_configured()
    return await client.delete_workflow(workflow_id)


@router.get("/executions")
async def list_executions(workflow_id: str = "", status: str = "", limit: int = 20, cursor: str = ""):
    _check_configured()
    return await client.list_executions(workflow_id, status, limit, cursor)


@router.get("/executions/{execution_id}")
async def get_execution(execution_id: str):
    _check_configured()
    result = await client.get_execution(execution_id)
    if not result:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result


@router.post("/import")
async def import_workflow(body: dict):
    """Import a workflow JSON into the active n8n instance.

    Accepts either:
      - A raw workflow JSON (legacy shape)
      - `{workflow: {...}, name_override?: str, tags?: [str]}` (new shape)

    The new shape is distinguished by presence of a `workflow` key containing
    a dict with `nodes` (which is the actual workflow structure).
    """
    _check_configured()
    name_override = None
    tags: list[str] = []
    workflow = body
    inner = body.get("workflow") if isinstance(body, dict) else None
    if isinstance(inner, dict) and "nodes" in inner:
        workflow = inner
        name_override = body.get("name_override")
        raw_tags = body.get("tags") or []
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    return await client.import_workflow(workflow, name_override=name_override, tags=tags)


# ── n8n User Management ─────────────────────────────────────────────────────


class InviteN8nUser(BaseModel):
    email: str
    role: str = "global:member"


@router.get("/users")
async def list_n8n_users():
    """List users on the active n8n instance."""
    _check_configured()
    users = await client.list_n8n_users()
    return {"users": users}


@router.post("/users/invite")
async def invite_n8n_user(req: InviteN8nUser):
    """Invite a user to the active n8n instance."""
    _check_configured()
    return await client.invite_n8n_user(req.email, req.role)


@router.delete("/users/{user_id}")
async def delete_n8n_user(user_id: str, transfer_to: str = ""):
    """Delete a user from the active n8n instance."""
    _check_configured()
    return await client.delete_n8n_user(user_id, transfer_to)


# ── Export & Backup ──────────────────────────────────────────────────────────


@router.get("/workflows/{workflow_id}/export")
async def export_workflow(workflow_id: str):
    """Export a single workflow as JSON."""
    _check_configured()
    result = await client.export_workflow(workflow_id)
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@router.get("/export/all")
async def export_all(active_only: bool = False):
    """Export all workflows as a JSON array."""
    _check_configured()
    workflows = await client.export_all_workflows(active_only)
    return {"workflows": workflows, "count": len(workflows)}


@router.get("/backup")
async def backup(active_only: bool = False):
    """Download a full backup as a JSON file with metadata."""
    _check_configured()
    workflows = await client.export_all_workflows(active_only)
    active_inst = get_active_instance()

    backup_data = {
        "backup_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instance": {
            "name": active_inst["name"] if active_inst else "unknown",
            "url": active_inst["url"] if active_inst else "",
        },
        "workflow_count": len(workflows),
        "active_only": active_only,
        "workflows": workflows,
    }

    instance_name = (active_inst["name"] if active_inst else "n8n").replace(" ", "-").lower()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"n8n-backup-{instance_name}-{timestamp}.json"

    return Response(
        content=json.dumps(backup_data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
