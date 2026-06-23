"""FastMCP server exposing dashboard read APIs. Phase 4 MVP — read-only.

The server uses FastMCP's streamable HTTP transport. Tools are thin wrappers
over existing internal helpers rather than HTTP calls to self, to avoid loop
overhead and so they work correctly inside the same event loop.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from backend.config import get_instances, load_config, load_secrets
from backend.database import get_db
from backend.modules.n8n_proxy import client as n8n_client

logger = logging.getLogger(__name__)

MCP_PATH = "/api/mcp-dashboard"
MCP_TOKEN_ENV = "DASHBOARD_MCP_TOKEN"

mcp = FastMCP(
    name="ageniusdesk-dashboard",
    instructions=(
        "Read-only tools for inspecting an AgeniusDesk instance: n8n workflows "
        "and executions, recent errors, configured n8n instances, secrets "
        "metadata (names only, never values), MCP servers, and messages."
    ),
    # Serve the JSON-RPC endpoint at the root of this mount so clients point
    # at /api/mcp-dashboard directly (FastMCP's default "/mcp" suffix would
    # force them to /api/mcp-dashboard/mcp).
    streamable_http_path="/",
    # DNS-rebinding protection rejects Host headers not on its allowlist.
    # Add the compose service name + the usual localhost variants so in-network
    # callers (e.g. a sibling container reaching us as `dashboard:3000`) work.
    # Override with DASHBOARD_MCP_ALLOWED_HOSTS="a,b,c" for custom deployments.
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            h.strip()
            for h in os.environ.get(
                "DASHBOARD_MCP_ALLOWED_HOSTS",
                "dashboard:3000,localhost:3000,127.0.0.1:3000,localhost,127.0.0.1",
            ).split(",")
            if h.strip()
        ],
    ),
)


# ── Tools ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_workflows(
    active_only: bool = False,
    name_contains: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List n8n workflows on the active instance. Returns id, name, active,
    tags, updatedAt for each. Use active_only=True to see running ones only
    or name_contains to search by substring."""
    result = await n8n_client.list_workflows(active_only, name_contains, limit, "")
    return result.get("workflows", [])


@mcp.tool()
async def get_workflow(workflow_id: str) -> dict[str, Any]:
    """Fetch full workflow definition (nodes + connections) by id."""
    result = await n8n_client.get_workflow(workflow_id)
    if result is None:
        return {"error": f"workflow {workflow_id!r} not found"}
    return result


@mcp.tool()
async def list_executions(
    workflow_id: str = "",
    status: str = "",
    limit: int = 30,
) -> list[dict[str, Any]]:
    """List recent n8n executions. Optional filters: workflow_id (exact) and
    status (one of: success, error, running, canceled, waiting)."""
    result = await n8n_client.list_executions(
        workflow_id=workflow_id,
        status=status,
        limit=limit,
    )
    return result.get("executions", [])


@mcp.tool()
async def list_errors(limit: int = 20, hours: int = 24) -> list[dict[str, Any]]:
    """Recent webhook-reported errors, newest first. `hours` filters to the
    last N hours; 0 disables the time filter."""
    db = await get_db()
    if hours > 0:
        cursor = await db.execute(
            "SELECT * FROM errors WHERE occurred_at > datetime('now', ?) "
            "ORDER BY occurred_at DESC LIMIT ?",
            (f"-{hours} hours", limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM errors ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
async def list_n8n_instances() -> list[dict[str, Any]]:
    """All configured n8n instances. Omits the encrypted api_key but includes
    url, login_url, and a key_hint for identification."""
    out = []
    for inst in get_instances():
        raw_key = inst.get("api_key", "")
        hint = raw_key if raw_key.startswith("$") else "<encrypted>"
        out.append({
            "id": inst["id"],
            "name": inst["name"],
            "url": inst["url"],
            "login_url": inst.get("login_url", ""),
            "color": inst.get("color", ""),
            "key_hint": hint,
            "has_login": bool(inst.get("owner_email") and inst.get("owner_password")),
        })
    return out


@mcp.tool()
async def list_secrets_metadata() -> list[dict[str, Any]]:
    """Names and types of stored secrets. Never returns actual values; use
    the Secrets UI for that."""
    secrets = load_secrets()
    out = []
    for name in sorted(secrets):
        entry = secrets[name]
        if isinstance(entry, dict) and "fields" in entry:
            out.append({
                "name": name,
                "kind": "compound",
                "type": entry.get("type", ""),
                "fields": sorted(entry.get("fields", {}).keys()),
            })
        else:
            out.append({"name": name, "kind": "string"})
    return out


@mcp.tool()
async def list_mcp_servers() -> list[dict[str, Any]]:
    """Configured MCP servers on this AgeniusDesk instance."""
    cfg = load_config()
    servers = cfg.get("mcp_servers", []) or []
    out = []
    for s in servers:
        out.append({
            "name": s.get("name"),
            "url": s.get("url"),
            "description": s.get("description", ""),
            "instances": s.get("instances", []),
            "has_token": any(s.get(k) for k in ("auth_token", "api_key", "token")),
        })
    return out


@mcp.tool()
async def list_messages(limit: int = 20) -> list[dict[str, Any]]:
    """Recent webhook-posted dashboard messages (toasts). Same data the
    `messages` WebSocket event carries."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, title, body, level, source, occurred_at FROM messages "
        "ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
async def get_status() -> dict[str, Any]:
    """Dashboard's own status — configured, active_instance, version."""
    from backend.main import status as _status  # late import: avoid cycle
    return await _status()


# ── Notes vault tools ──────────────────────────────────────────────────────
#
# The dashboard ships an Obsidian-compatible note vault at data/notes/. These
# tools give `claude` first-class access to search, read, and write notes so
# it can treat the vault as durable memory across sessions. Agents are
# encouraged to write to the `agent/` or `sessions/` folders for their own
# scratchpads, and to read from `shared/` for canonical knowledge. `user/`
# is the operator's own space — read freely, write only on explicit request.


@mcp.tool()
async def search_notes(
    query: str = "",
    tag: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search the notes vault by full text and/or tag. Empty query returns
    recent notes ordered by mtime. Tag match is case-insensitive and exact.
    Results include a highlighted snippet around the matched term."""
    from backend.modules.notes import index as _idx
    from backend.modules.notes.storage import ensure_vault
    ensure_vault()
    return await _idx.search(query, tag=tag or None, limit=min(max(limit, 1), 200))


@mcp.tool()
async def read_note(path: str) -> dict[str, Any]:
    """Read a single note's raw markdown by its vault-relative path (e.g.
    'user/runbook.md'). Returns {path, content} or an error message."""
    from backend.modules.notes import storage
    storage.ensure_vault()
    try:
        vp = storage.resolve(path)
        return {"path": vp.rel, "content": storage.read(path)}
    except FileNotFoundError:
        return {"error": f"no note at {path!r}"}
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def write_note(
    path: str,
    content: str,
) -> dict[str, Any]:
    """Create or overwrite a note at the given vault-relative path. Parents
    created as needed. Content should be markdown with optional YAML
    frontmatter and `[[wikilinks]]`. Prefer the `agent/` folder for your
    own memories and `sessions/YYYY-MM-DD.md` for daily transcripts —
    operator's `user/` folder is off-limits unless explicitly requested.
    Returns parsed metadata (title, tags, links)."""
    from backend.modules.notes import storage
    storage.ensure_vault()
    try:
        return await storage.write(path, content)
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def append_note(path: str, content: str) -> dict[str, Any]:
    """Append to an existing note (creates it if missing). Ideal for
    scratchpad-style accumulation — e.g., logging each incident you
    investigate into a running `agent/incidents.md` file."""
    from backend.modules.notes import storage
    storage.ensure_vault()
    try:
        return await storage.append(path, content)
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def list_backlinks(path: str) -> list[dict[str, Any]]:
    """All notes that wikilink to the given note (by its basename)."""
    from backend.modules.notes import index as _idx
    from backend.modules.notes import storage
    storage.ensure_vault()
    try:
        vp = storage.resolve(path)
    except ValueError as e:
        return [{"error": str(e)}]
    return await _idx.backlinks(vp.rel)


@mcp.tool()
async def list_note_tags() -> list[dict[str, Any]]:
    """All unique tags across the vault with occurrence counts. Useful for
    discovering existing taxonomy before writing new notes."""
    from backend.modules.notes import index as _idx
    from backend.modules.notes.storage import ensure_vault
    ensure_vault()
    return await _idx.list_tags()


# ── Unified context store ──────────────────────────────────────────────────
#
# ── Knowledge sources (router pattern A) ───────────────────────────────────
#
# The operator registers external stores (Qdrant collections like company-docs,
# product-kb, runbooks) in the knowledge_sources table, each with a plain-
# English `description`. Claude reads list_knowledge_sources to see what's
# available, then picks which to hit via search_knowledge(query, source_names).
# This is Claude-as-router: no backend scoring, the LLM reads descriptions
# and routes by intent.


@mcp.tool()
async def list_knowledge_sources() -> list[dict[str, Any]]:
    """List every registered external knowledge source the operator has
    connected to this dashboard. Each entry includes a `description` — read
    them and pick which sources match the user's question before calling
    search_knowledge. Only `enabled` sources actually respond to searches."""
    from backend.modules.knowledge import storage as _k
    sources = await _k.list_sources()
    return [
        {
            "name": s["name"],
            "kind": s["kind"],
            "description": s["description"],
            "enabled": s["enabled"],
            "collection": (s.get("config") or {}).get("collection", ""),
        }
        for s in sources
    ]


@mcp.tool()
async def search_knowledge(
    query: str,
    sources: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Semantic search across one or more registered knowledge sources.
    Pass `sources` as a list of source names from list_knowledge_sources;
    omit it to fan out across every enabled source. Results come back grouped
    by source name so you can reason about provenance. Prefer targeted
    queries (pick 1-2 sources whose description matches intent) over
    broadcasting — cheaper and higher signal."""
    from backend.modules.knowledge import backends as _kb
    from backend.modules.knowledge import storage as _k
    import asyncio as _asyncio

    all_sources = await _k.list_sources(enabled_only=True)
    wanted = set(sources or [])
    selected = [s for s in all_sources if not wanted or s["name"] in wanted]
    if not selected:
        return {"query": query, "results_by_source": {}, "sources_queried": []}

    async def _one(s):
        try:
            return s["name"], await _kb.search_source(s, query, limit)
        except Exception as e:
            return s["name"], {"results": [], "error": str(e)}

    pairs = await _asyncio.gather(*[_one(s) for s in selected])
    return {
        "query": query,
        "results_by_source": dict(pairs),
        "sources_queried": [n for n, _ in pairs],
    }


# ── Mount wrapper ───────────────────────────────────────────────────────────
#
# The module's `router` is a dummy APIRouter whose only job is to trigger
# mounting the FastMCP Starlette app on the parent FastAPI. register_modules
# auto-includes this router; the real MCP endpoint lives at MCP_PATH.


router = APIRouter(
    prefix="/api/mcp-dashboard/_meta",
    tags=["dashboard-mcp"],
)


@router.get("/ping")
async def ping(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Health + auth probe. Returns `{ok: true, auth: 'ok'|'open'}`.

    The streamable HTTP MCP transport itself is mounted at MCP_PATH and
    handles auth on each JSON-RPC call. This endpoint is a convenience for
    operators debugging token setup.
    """
    token = os.environ.get(MCP_TOKEN_ENV, "")
    if not token:
        return {"ok": True, "auth": "open", "endpoint": MCP_PATH}
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    if authorization.removeprefix("Bearer ").strip() != token:
        raise HTTPException(status_code=403, detail="Invalid token")
    return {"ok": True, "auth": "ok", "endpoint": MCP_PATH}


def mount_on(app) -> None:
    """Attach the FastMCP streamable-HTTP app to a FastAPI instance.

    Call once from main.py after register_modules. Safe to re-call (the
    mount will just replace the previous one). MCP is always enabled.
    """
    inner = mcp.streamable_http_app()
    app.mount(MCP_PATH, inner)
    logger.info("Dashboard MCP server mounted at %s", MCP_PATH)
