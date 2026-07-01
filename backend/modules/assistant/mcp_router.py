"""MCP Server management API routes."""

import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.config import encrypt_value
from backend.modules.assistant import mcp_client

# Operator-only: every server add/update/test/discover triggers a server-side
# fetch to an operator-supplied URL (SSRF surface), and the responses are
# reflected back to the caller. Keep this off the read-only `viewer` role. The
# n8n-mcp subroutes below already re-assert operator; the router-level floor
# covers /servers* which previously had none.
router = APIRouter(
    prefix="/api/mcp",
    tags=["mcp"],
    dependencies=[Depends(require_role("operator"))],
)


class AddServer(BaseModel):
    name: str
    url: str
    token: str = ""
    description: str = ""
    instances: list[str] = []  # Instance IDs this server is available to (empty = all)


class UpdateServer(BaseModel):
    name: str = ""
    url: str = ""
    token: str = ""
    description: str = ""
    enabled: bool = True
    instances: list[str] = []


@router.get("/servers")
async def list_servers():
    """List configured MCP servers (tokens masked)."""
    servers = mcp_client.get_mcp_servers()
    safe = []
    for s in servers:
        token = s.get("token", "")
        if token and not token.startswith("$"):
            token_hint = "configured"
        elif token.startswith("$"):
            token_hint = token
        else:
            token_hint = ""

        safe.append({
            "id": s["id"],
            "name": s.get("name", ""),
            "url": s.get("url", ""),
            "description": s.get("description", ""),
            "token_hint": token_hint,
            "enabled": s.get("enabled", True),
            "instances": s.get("instances", []),
        })
    return {"servers": safe}


@router.post("/servers")
async def add_server(req: AddServer):
    """Add a new MCP server."""
    server = {
        "id": secrets.token_hex(8),
        "name": req.name,
        "url": req.url.rstrip("/"),
        "token": encrypt_value(req.token) if req.token and not req.token.startswith("$") else req.token,
        "description": req.description,
        "enabled": True,
        "instances": req.instances,
    }

    result = await mcp_client.add_server(server)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to connect"))

    return {"success": True, "id": server["id"], "tools_count": result.get("tools_count", 0)}


@router.put("/servers/{server_id}")
async def update_server(server_id: str, req: UpdateServer):
    """Update an MCP server."""
    servers = mcp_client.get_mcp_servers()
    for s in servers:
        if s["id"] == server_id:
            if req.name:
                s["name"] = req.name
            if req.url:
                s["url"] = req.url.rstrip("/")
            if req.token:
                s["token"] = (
                    encrypt_value(req.token)
                    if not req.token.startswith("$")
                    else req.token
                )
            if req.description:
                s["description"] = req.description
            s["enabled"] = req.enabled
            s["instances"] = req.instances
            mcp_client.save_mcp_servers(servers)
            return {"success": True}
    raise HTTPException(status_code=404, detail="Server not found")


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str):
    """Remove an MCP server."""
    removed = await mcp_client.remove_server(server_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Server not found")
    return {"success": True}


@router.post("/servers/{server_id}/test")
async def test_server(server_id: str):
    """Test connection to an MCP server."""
    servers = mcp_client.get_mcp_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return await mcp_client.test_server(server)


@router.get("/servers/{server_id}/tools")
async def list_server_tools(server_id: str):
    """List tools available on an MCP server."""
    servers = mcp_client.get_mcp_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    tools = await mcp_client.discover_tools(server)
    return {
        "tools": [{"name": t["function"]["name"], "description": t["function"]["description"]} for t in tools],
        "count": len(tools),
    }


# ── Built-in n8n-mcp (node intelligence) ─────────────────────────────────────


@router.get("/n8n-mcp/status")
async def n8n_mcp_status():
    """State of the built-in n8n-mcp integration (docker available, registered,
    running, mode). Drives the 'Enable n8n intelligence' card."""
    from backend.modules.assistant import n8n_mcp_provision
    return await n8n_mcp_provision.status()


@router.post("/n8n-mcp/enable", dependencies=[Depends(require_role("operator"))])
async def n8n_mcp_enable():
    """One-click: start n8n-mcp in docs mode and register it. Best-effort."""
    from backend.modules.assistant import n8n_mcp_provision
    return await n8n_mcp_provision.enable()


@router.post("/n8n-mcp/upgrade", dependencies=[Depends(require_role("operator"))])
async def n8n_mcp_upgrade():
    """Recreate n8n-mcp wired to the active instance (full workflow tools)."""
    from backend.modules.assistant import n8n_mcp_provision
    return await n8n_mcp_provision.upgrade()


@router.post("/n8n-mcp/disable", dependencies=[Depends(require_role("operator"))])
async def n8n_mcp_disable():
    """Stop + remove the managed n8n-mcp container and unregister it."""
    from backend.modules.assistant import n8n_mcp_provision
    return await n8n_mcp_provision.disable()


@router.get("/tools")
async def list_all_tools():
    """List all tools across MCP servers visible to the active instance + built-in tools."""
    from backend.config import get_active_instance_id
    from backend.modules.assistant.tools import TOOL_DEFINITIONS

    active_instance_id = get_active_instance_id()
    mcp_tools, _ = await mcp_client.get_all_mcp_tools(instance_id=active_instance_id)

    builtin = [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "source": "built-in",
        }
        for t in TOOL_DEFINITIONS
    ]
    mcp = [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "source": "mcp",
        }
        for t in mcp_tools
    ]

    return {"tools": builtin + mcp, "builtin_count": len(builtin), "mcp_count": len(mcp)}
