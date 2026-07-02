"""MCP Client — connects to external MCP servers, discovers tools, executes them.

Supports two protocols:
  1. MCP Streamable HTTP (FastMCP 3.x) — initialize session, then call with session ID
  2. Legacy REST — GET /tools, POST /tools/{name}
"""

import json
import logging
import os

import httpx

from backend.config import decrypt_value, load_config, save_config
from backend.module_registry import APP_VERSION
from backend.net import UnsafeProbeURL, assert_safe_probe_url

logger = logging.getLogger(__name__)

TIMEOUT = 30.0


def _verify() -> bool:
    """TLS cert verification flag (see AGD_TLS_VERIFY). Default on."""
    val = os.environ.get("AGD_TLS_VERIFY", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def get_mcp_servers() -> list[dict]:
    """Get configured MCP servers from config."""
    config = load_config()
    return config.get("mcp_servers", [])


def save_mcp_servers(servers: list[dict]) -> None:
    config = load_config()
    config["mcp_servers"] = servers
    save_config(config)


def _resolve_server(server: dict) -> dict:
    """Resolve $VAR references in server config."""
    return {
        **server,
        "url": decrypt_value(server.get("url", "")),
        "token": decrypt_value(server.get("token", "")),
    }


def _headers(server: dict, session_id: str = "") -> dict:
    """Build headers for an MCP server request."""
    resolved = _resolve_server(server)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if resolved.get("token"):
        headers["Authorization"] = f"Bearer {resolved['token']}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _parse_sse_json(text: str) -> dict | None:
    """Extract JSON from an SSE response body (event: message\\ndata: {...})."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    # Fallback: try parsing the whole body as plain JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ── MCP Session Protocol ────────────────────────────────────────────────────


def _normalize_mcp_urls(raw: str) -> tuple[str, str]:
    """Return (base, mcp_url) from a user-entered server URL.

    Treats `http://host/mcp` and `http://host` as equivalent — callers get a
    `base` for legacy /tools|/health probes and a `mcp_url` for the MCP
    streamable-HTTP endpoint so we never double-append `/mcp`.

    SSRF guard: the URL is operator-supplied and fetched server-side, so reject
    cloud-metadata / link-local / reserved targets before any request goes out.
    Self-hosted MCP on loopback or a LAN/Docker host stays allowed (the same
    posture as the Ollama probe). Raises UnsafeProbeURL on a blocked target.
    """
    assert_safe_probe_url(raw)
    raw = raw.rstrip("/")
    if raw.endswith("/mcp"):
        return raw[: -len("/mcp")], raw
    return raw, f"{raw}/mcp"


async def _mcp_initialize(client: httpx.AsyncClient, url: str, server: dict) -> str | None:
    """Initialize an MCP session, return the session ID or None.

    `url` is the `/mcp` endpoint itself — callers pass the endpoint directly so
    we don't double-append paths when the user already entered `.../mcp`.
    """
    resp = await client.post(
        url,
        headers=_headers(server),
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ageniusdesk", "version": APP_VERSION},
            },
            "id": 1,
        },
    )
    if resp.status_code == 200:
        session_id = resp.headers.get("mcp-session-id", "")
        if session_id:
            return session_id
    # Raise on auth/other errors so the caller can surface the status.
    resp.raise_for_status()
    return None


async def _mcp_request(client: httpx.AsyncClient, url: str, server: dict,
                       session_id: str, method: str, params: dict | None = None,
                       req_id: int = 2) -> dict | None:
    """Send a JSON-RPC request to an MCP server with session context."""
    body: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params:
        body["params"] = params

    resp = await client.post(
        url,
        headers=_headers(server, session_id),
        json=body,
    )
    if resp.status_code == 200:
        return _parse_sse_json(resp.text)
    return None


# ── Server Management ────────────────────────────────────────────────────────


async def add_server(server: dict) -> dict:
    """Add a new MCP server and test the connection."""
    test = await test_server(server)
    if not test.get("connected"):
        return {"success": False, "error": test.get("error", "Connection failed")}

    servers = get_mcp_servers()
    servers.append(server)
    save_mcp_servers(servers)

    return {"success": True, "tools_count": test.get("tools_count", 0)}


async def remove_server(server_id: str) -> bool:
    servers = get_mcp_servers()
    before = len(servers)
    servers = [s for s in servers if s.get("id") != server_id]
    if len(servers) == before:
        return False
    save_mcp_servers(servers)
    return True


async def test_server(server: dict) -> dict:
    """Test connection to an MCP server by listing tools.

    Walks several common MCP transports. Preserves the highest-signal failure
    across attempts so the frontend can show something actionable (auth
    needed, wrong path, connection refused) instead of a generic "could not
    connect" toast.
    """
    resolved = _resolve_server(server)
    try:
        base, mcp_url = _normalize_mcp_urls(resolved["url"])
    except UnsafeProbeURL as e:
        return {"connected": False, "error": f"Blocked URL: {e}"}

    # Track the best-signal error we see so the UI can suggest a remediation.
    best_status: int | None = None
    best_url: str = mcp_url
    last_exc: str = ""

    # Try MCP streamable HTTP — initialize session, then list tools.
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            session_id = await _mcp_initialize(client, mcp_url, server)
            if session_id:
                data = await _mcp_request(client, mcp_url, server, session_id, "tools/list")
                if data:
                    tools = data.get("result", {}).get("tools", [])
                    return {"connected": True, "tools_count": len(tools), "protocol": "mcp"}
    except httpx.HTTPStatusError as e:
        best_status, best_url, last_exc = e.response.status_code, mcp_url, str(e)
    except Exception as e:
        last_exc = str(e)
        logger.debug("MCP streamable HTTP failed for %s: %s", server.get("name"), e)

    # Try stateless POST (older MCP servers without session requirement).
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.post(
                mcp_url,
                headers=_headers(server),
                json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            )
            if resp.status_code == 200:
                data = _parse_sse_json(resp.text) or resp.json()
                tools = data.get("result", {}).get("tools", [])
                return {"connected": True, "tools_count": len(tools), "protocol": "mcp-stateless"}
            # 401/403 are the highest-signal errors — prefer them over 404s.
            if best_status is None or resp.status_code in (401, 403):
                best_status, best_url = resp.status_code, mcp_url
    except Exception as e:
        last_exc = last_exc or str(e)

    # Try legacy /tools endpoint (older FastMCP / custom servers).
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.get(f"{base}/tools", headers=_headers(server))
            if resp.status_code == 200:
                data = resp.json()
                tools = data if isinstance(data, list) else data.get("tools", [])
                return {"connected": True, "tools_count": len(tools), "protocol": "legacy"}
            if best_status is None:
                best_status, best_url = resp.status_code, f"{base}/tools"
    except Exception as e:
        last_exc = last_exc or str(e)

    # Try /health endpoint.
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.get(f"{base}/health", headers=_headers(server))
            if resp.status_code == 200:
                return {"connected": True, "tools_count": 0, "protocol": "health-only"}
            if best_status is None:
                best_status, best_url = resp.status_code, f"{base}/health"
    except Exception as e:
        last_exc = last_exc or str(e)

    # Distill an actionable message from whatever we saw.
    if best_status in (401, 403):
        return {
            "connected": False,
            "error": f"Auth required at {best_url} (HTTP {best_status}). "
                     "Paste a Bearer token in the Auth Token field.",
        }
    if best_status == 404:
        return {
            "connected": False,
            "error": f"No MCP endpoints found at {mcp_url}. Common suffixes: "
                     "append /mcp or /sse to the Server URL.",
        }
    if best_status is not None:
        return {"connected": False, "error": f"HTTP {best_status} from {best_url}."}
    if last_exc:
        return {"connected": False, "error": f"Could not reach {mcp_url}: {last_exc}"}
    return {"connected": False, "error": f"Could not reach {mcp_url}."}


# ── Tool Discovery ───────────────────────────────────────────────────────────


async def discover_tools(server: dict) -> list[dict]:
    """Discover available tools from an MCP server."""
    resolved = _resolve_server(server)
    try:
        base, mcp_url = _normalize_mcp_urls(resolved["url"])
    except UnsafeProbeURL:
        logger.warning("MCP discover blocked unsafe URL for %s", server.get("name"))
        return []

    # Try MCP streamable HTTP
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            session_id = await _mcp_initialize(client, mcp_url, server)
            if session_id:
                data = await _mcp_request(client, mcp_url, server, session_id, "tools/list")
                if data:
                    tools = data.get("result", {}).get("tools", [])
                    return [_normalize_tool(t, server) for t in tools]
    except Exception as e:
        logger.debug("MCP streamable discover failed for %s: %s", server.get("name"), e)

    # Try stateless MCP
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.post(
                mcp_url,
                headers=_headers(server),
                json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            )
            if resp.status_code == 200:
                data = _parse_sse_json(resp.text) or resp.json()
                tools = data.get("result", {}).get("tools", [])
                return [_normalize_tool(t, server) for t in tools]
    except Exception:
        pass

    # Try legacy endpoint
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.get(f"{base}/tools", headers=_headers(server))
            if resp.status_code == 200:
                data = resp.json()
                tools = data if isinstance(data, list) else data.get("tools", [])
                return [_normalize_tool(t, server) for t in tools]
    except Exception:
        pass

    return []


def _normalize_tool(tool: dict, server: dict) -> dict:
    """Normalize a tool definition to OpenAI function calling format."""
    # MCP format
    if "inputSchema" in tool:
        return {
            "type": "function",
            "function": {
                "name": f"mcp_{server['id']}_{tool['name']}",
                "description": f"[{server.get('name', 'MCP')}] {tool.get('description', tool['name'])}",
                "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
            },
            "_mcp_server_id": server["id"],
            "_mcp_tool_name": tool["name"],
        }

    # Already in OpenAI format
    name = tool.get("function", {}).get("name", tool.get("name", "unknown"))
    return {
        "type": "function",
        "function": {
            "name": f"mcp_{server['id']}_{name}",
            "description": f"[{server.get('name', 'MCP')}] {tool.get('function', {}).get('description', name)}",
            "parameters": tool.get("function", {}).get("parameters", {"type": "object", "properties": {}}),
        },
        "_mcp_server_id": server["id"],
        "_mcp_tool_name": name,
    }


# ── Tool Execution ───────────────────────────────────────────────────────────


async def execute_tool(server_id: str, tool_name: str, arguments: dict) -> str:
    """Execute a tool on an MCP server."""
    servers = get_mcp_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        return f"MCP server '{server_id}' not found"

    resolved = _resolve_server(server)
    try:
        base, mcp_url = _normalize_mcp_urls(resolved["url"])
    except UnsafeProbeURL as e:
        return f"Error: blocked MCP server URL ({e})"

    # Try MCP streamable HTTP — initialize session, then call
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            session_id = await _mcp_initialize(client, mcp_url, server)
            if session_id:
                data = await _mcp_request(
                    client, mcp_url, server, session_id, "tools/call",
                    params={"name": tool_name, "arguments": arguments},
                )
                if data:
                    result = data.get("result", {})
                    content = result.get("content", [])
                    if content:
                        texts = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
                        return "\n".join(texts) if texts else json.dumps(result)
                    return json.dumps(result)
    except Exception as e:
        logger.warning("MCP streamable tool/call failed for %s/%s: %s", server_id, tool_name, e)

    # Try stateless MCP
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.post(
                mcp_url,
                headers=_headers(server),
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                    "id": 1,
                },
            )
            if resp.status_code == 200:
                data = _parse_sse_json(resp.text) or resp.json()
                result = data.get("result", {})
                content = result.get("content", [])
                if content:
                    texts = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
                    return "\n".join(texts) if texts else json.dumps(result)
                return json.dumps(result)
    except Exception as e:
        logger.warning("MCP stateless tool/call failed for %s/%s: %s", server_id, tool_name, e)

    # Try legacy POST /tools/{name}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.post(
                f"{base}/tools/{tool_name}",
                headers=_headers(server),
                json=arguments,
            )
            if resp.status_code == 200:
                return resp.text[:2000]
    except Exception as e:
        return f"Tool execution failed: {e}"

    return "Tool execution failed — no compatible endpoint found"


# ── Get All Tools (for AI assistant) ─────────────────────────────────────────


def _server_visible_to_instance(server: dict, instance_id: str | None) -> bool:
    """Return True if server should be visible to the given instance.
    Empty instances list = global (visible to all). Otherwise must match."""
    assigned = server.get("instances", [])
    if not assigned:
        return True
    if not instance_id:
        return True
    return instance_id in assigned


async def get_all_mcp_tools(instance_id: str | None = None) -> tuple[list[dict], dict]:
    """Discover tools from configured MCP servers visible to instance_id.
    Returns (tool_definitions, tool_map) where tool_map maps function name to (server_id, tool_name).
    If instance_id is None, returns tools from all enabled servers."""
    servers = get_mcp_servers()
    all_tools = []
    tool_map = {}

    for server in servers:
        if not server.get("enabled", True):
            continue
        if not _server_visible_to_instance(server, instance_id):
            continue
        try:
            tools = await discover_tools(server)
            for t in tools:
                fname = t["function"]["name"]
                tool_map[fname] = (t.get("_mcp_server_id", server["id"]), t.get("_mcp_tool_name", ""))
                # Remove internal keys before passing to LLM
                clean = {"type": t["type"], "function": t["function"]}
                all_tools.append(clean)
        except Exception as e:
            logger.warning("Failed to discover tools from %s: %s", server.get("name"), e)

    return all_tools, tool_map
