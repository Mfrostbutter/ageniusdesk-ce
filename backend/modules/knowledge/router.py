"""Knowledge-source registry HTTP API.

Endpoints:
  GET    /api/knowledge/sources                list registered sources
  POST   /api/knowledge/sources                create a new source
  GET    /api/knowledge/sources/{id}           fetch a source
  PUT    /api/knowledge/sources/{id}           update a source
  DELETE /api/knowledge/sources/{id}           remove a source
  POST   /api/knowledge/sources/{id}/test      probe connectivity + auth
  GET    /api/knowledge/search?q=&sources=a,b  fan-out search; omit `sources`
                                                to hit every enabled source

  GET    /api/knowledge/connectors             MCP servers with knowledge_enabled flag
  PUT    /api/knowledge/connectors/{id}        set knowledge_enabled on an MCP server

  GET    /api/knowledge/instructions           get the routing-guide markdown document
  PUT    /api/knowledge/instructions           save the routing-guide markdown document

Contract designed for the MCP tools in dashboard_mcp/server.py: each source
exposes its `description` string as the routing signal for Claude.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config import DATA_DIR
from backend.modules.knowledge import backends, storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class SourceIn(BaseModel):
    name: str
    kind: str = "qdrant"
    description: str = ""
    config: dict[str, Any] = {}
    enabled: bool = True


class SourcePatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


@router.get("/sources")
async def list_sources() -> dict[str, Any]:
    return {"sources": await storage.list_sources()}


@router.post("/sources")
async def create_source(payload: SourceIn) -> dict[str, Any]:
    if payload.kind not in backends.DISPATCH:
        raise HTTPException(400, f"unsupported kind {payload.kind!r}; known: {list(backends.DISPATCH)}")
    existing = await storage.get_source_by_name(payload.name)
    if existing:
        raise HTTPException(409, f"source {payload.name!r} already exists")
    return await storage.create_source(
        payload.name, payload.kind, payload.description, payload.config, payload.enabled
    )


@router.get("/sources/{source_id}")
async def get_source(source_id: int) -> dict[str, Any]:
    s = await storage.get_source(source_id)
    if not s:
        raise HTTPException(404, "source not found")
    return s


@router.put("/sources/{source_id}")
async def update_source(source_id: int, patch: SourcePatch) -> dict[str, Any]:
    if patch.kind and patch.kind not in backends.DISPATCH:
        raise HTTPException(400, f"unsupported kind {patch.kind!r}")
    updated = await storage.update_source(source_id, **patch.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(404, "source not found")
    return updated


@router.delete("/sources/{source_id}")
async def delete_source(source_id: int) -> dict[str, Any]:
    ok = await storage.delete_source(source_id)
    if not ok:
        raise HTTPException(404, "source not found")
    return {"deleted": True, "id": source_id}


@router.post("/sources/{source_id}/test")
async def test_source(source_id: int) -> dict[str, Any]:
    s = await storage.get_source(source_id)
    if not s:
        raise HTTPException(404, "source not found")
    return await backends.probe(s)


@router.get("/search")
async def search(q: str, sources: str = "", limit: int = 10) -> dict[str, Any]:
    """Fan-out search. `sources` is a comma-separated list of source names
    (empty = all enabled). Each source runs concurrently with its own error
    isolation; one bad source never kills the response."""
    wanted = {s.strip() for s in sources.split(",") if s.strip()}
    all_sources = await storage.list_sources(enabled_only=True)
    selected = [s for s in all_sources if not wanted or s["name"] in wanted]

    if not selected:
        return {"query": q, "results_by_source": {}, "sources_queried": []}
    if not q.strip():
        return {
            "query": q,
            "results_by_source": {s["name"]: {"results": []} for s in selected},
            "sources_queried": [s["name"] for s in selected],
        }

    async def _one(s: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            return s["name"], await backends.search_source(s, q, limit)
        except Exception as e:
            return s["name"], {"results": [], "error": str(e)}

    pairs = await asyncio.gather(*[_one(s) for s in selected])
    return {
        "query": q,
        "results_by_source": dict(pairs),
        "sources_queried": [name for name, _ in pairs],
    }


# ── Connectors (MCP servers available to Knowledge) ───────────────────────────

class ConnectorPatch(BaseModel):
    knowledge_enabled: bool


@router.get("/connectors")
async def list_connectors() -> dict[str, Any]:
    """Return all MCP servers with their knowledge_enabled flag."""
    from backend.modules.assistant.mcp_client import get_mcp_servers
    servers = get_mcp_servers()
    return {
        "connectors": [
            {
                "id": s["id"],
                "name": s.get("name", ""),
                "url": s.get("url", ""),
                "description": s.get("description", ""),
                "enabled": s.get("enabled", True),
                "knowledge_enabled": s.get("knowledge_enabled", False),
            }
            for s in servers
        ]
    }


@router.put("/connectors/{server_id}")
async def update_connector(server_id: str, patch: ConnectorPatch) -> dict[str, Any]:
    """Toggle knowledge_enabled on an MCP server."""
    from backend.modules.assistant.mcp_client import get_mcp_servers, save_mcp_servers
    servers = get_mcp_servers()
    for s in servers:
        if s["id"] == server_id:
            s["knowledge_enabled"] = patch.knowledge_enabled
            save_mcp_servers(servers)
            return {"success": True, "id": server_id, "knowledge_enabled": patch.knowledge_enabled}
    raise HTTPException(status_code=404, detail="MCP server not found")


# ── Instructions (routing-guide document) ─────────────────────────────────────

_INSTRUCTIONS_FILE = DATA_DIR / "knowledge_instructions.md"

_DEFAULT_INSTRUCTIONS = """\
# Knowledge Routing Guide

This document tells any agent or model using this knowledge base where to look for what.
Edit it to match the sources and connectors you have registered.

---

## Sources

| Source name | What it contains | When to query it |
|-------------|-----------------|------------------|
| *(add a row per registered source)* | | |

## MCP Connectors

| Connector | What it provides | When to use it |
|-----------|-----------------|----------------|
| *(add a row per enabled connector)* | | |

## Start here

- For general background and project context: query the primary vector source
- For live data, actions, or tool calls: use the appropriate connector
- When unsure: query all enabled sources, then decide

## Notes for AI agents

- Read this document first on every session that involves Knowledge
- Prefer the most specific source over the broadest
- If a query spans multiple sources, fan out and merge results
"""


@router.get("/instructions")
async def get_instructions() -> dict[str, Any]:
    """Return the routing-guide markdown document."""
    if not _INSTRUCTIONS_FILE.exists():
        return {"content": _DEFAULT_INSTRUCTIONS, "exists": False}
    return {"content": _INSTRUCTIONS_FILE.read_text(encoding="utf-8"), "exists": True}


class InstructionsPut(BaseModel):
    content: str


@router.put("/instructions")
async def save_instructions(body: InstructionsPut) -> dict[str, Any]:
    """Save the routing-guide markdown document."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _INSTRUCTIONS_FILE.write_text(body.content, encoding="utf-8")
    return {"success": True, "bytes": len(body.content.encode())}
