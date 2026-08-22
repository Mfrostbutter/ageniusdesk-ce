"""MCP servers as fleet tools.

A vault agent's manifest opts into an MCP server's tools by name
(`mcp:{server_id}:{tool_name}`). The runner pre-warms the cache
(async discovery) before build; `resolve_cached` then hands the sync graph
factory ready @tool wrappers. Execution goes through the assistant module's
MCP client, so server config, `$SECRET` token refs, TLS posture, and audit
all stay in one place.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

MCP_PREFIX = "mcp:"

# name -> StructuredTool, refreshed whole-cache on prefetch
_CACHE: dict[str, Any] = {}
_CACHE_AT: float = 0.0
CACHE_TTL_S = 300

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def is_mcp_name(name: str) -> bool:
    return name.startswith(MCP_PREFIX)


def _args_model(model_name: str, schema: dict):
    """Dynamic pydantic model from a tool's JSON inputSchema."""
    from pydantic import create_model

    props = (schema or {}).get("properties", {}) or {}
    required = set((schema or {}).get("required", []) or [])
    fields = {}
    for key, spec in props.items():
        typ = _JSON_TYPES.get((spec or {}).get("type", ""), Any)
        if key in required:
            fields[key] = (typ, ...)
        else:
            fields[key] = (Optional[typ], None)
    return create_model(model_name, **fields)


def _wrap(defn: dict):
    """Build a StructuredTool that executes through the assistant MCP client."""
    from langchain_core.tools import StructuredTool

    server_id = defn["_mcp_server_id"]
    tool_name = defn["_mcp_tool_name"]
    fn = defn.get("function", {})
    name = f"{MCP_PREFIX}{server_id}:{tool_name}"

    async def _run(**kwargs):
        from backend.modules.assistant.mcp_client import execute_tool

        # None-valued optionals are absent, not null, for the remote tool.
        args = {k: v for k, v in kwargs.items() if v is not None}
        return await execute_tool(server_id, tool_name, args)

    return StructuredTool.from_function(
        coroutine=_run,
        name=name,
        description=fn.get("description", tool_name),
        args_schema=_args_model(f"McpArgs_{server_id}_{tool_name}", fn.get("parameters", {})),
    )


async def prefetch_all(force: bool = False) -> int:
    """Discover tools from every configured MCP server into the cache.

    Failures are per-server and non-fatal: a down PSA must not stop a run
    that never uses it. Returns the cache size.
    """
    global _CACHE_AT
    if not force and _CACHE and time.monotonic() - _CACHE_AT < CACHE_TTL_S:
        return len(_CACHE)

    try:
        from backend.modules.assistant.mcp_client import discover_tools, get_mcp_servers
    except ImportError:
        return 0

    fresh: dict[str, Any] = {}
    for server in get_mcp_servers():
        try:
            for defn in await discover_tools(server):
                tool = _wrap(defn)
                fresh[tool.name] = tool
        except Exception as e:  # noqa: BLE001 - one bad server must not break the rest
            logger.warning("MCP fleet-tool discovery failed for %s: %s", server.get("id"), e)
    _CACHE.clear()
    _CACHE.update(fresh)
    _CACHE_AT = time.monotonic()
    return len(_CACHE)


def resolve_cached(names: list[str]) -> list:
    """Resolve `mcp:server:tool` names against the cache (unknown names skipped)."""
    out = []
    for n in names:
        tool = _CACHE.get(n)
        if tool is not None:
            out.append(tool)
        elif is_mcp_name(n):
            logger.warning("MCP fleet tool '%s' not in cache (server down or tool renamed)", n)
    return out


def catalog() -> list[dict]:
    """[{name, description}] of cached MCP tools for the builder UI."""
    return [
        {"name": t.name, "description": (t.description or "").strip()}
        for t in _CACHE.values()
    ]
