"""Async MCP client for the itops-mcp tool plane.

Streamable HTTP, JSON-RPC. Session-caching, with the two failure modes the
platform gate surfaced handled explicitly:
  - FastMCP reports tool failures in-band (result.isError + text), not as
    JSON-RPC errors; missing that turns a failed write into a silent no-op.
  - A restarted server answers a cached session id with 404; re-init and
    retry once.
"""

import asyncio
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 30.0


class McpError(RuntimeError):
    """A tool call failed, or the transport did."""


class StaleSession(McpError):
    """The server no longer recognizes our session id (it restarted)."""


def _verify() -> bool:
    """TLS cert verification flag (see AGD_TLS_VERIFY). Default on."""
    val = os.environ.get("AGD_TLS_VERIFY", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


class ItopsMcpClient:
    """One client per process; session id cached across calls."""

    def __init__(self, base_url: str, bearer_token: str = "", timeout: float = TIMEOUT):
        self._url = base_url.rstrip("/")
        if not self._url.endswith("/mcp"):
            self._url += "/mcp"
        self._token = bearer_token
        self._timeout = timeout
        self._session_id: str = ""
        self._id = 0
        self._lock = asyncio.Lock()

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _post(self, client: httpx.AsyncClient, payload: dict, expect_result: bool = True) -> dict:
        try:
            resp = await client.post(self._url, json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            raise McpError(f"transport failed: {type(e).__name__}") from None

        if resp.status_code == 401:
            raise McpError("unauthorized: MCP bearer token missing or wrong")
        if resp.status_code == 404 and self._session_id:
            raise StaleSession()
        if resp.status_code >= 400:
            raise McpError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if not expect_result:
            return {}

        # Streamable HTTP answers as SSE or plain JSON.
        body = resp.text or ""
        for line in body.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(body) if body.strip() else {}

    async def _ensure_session(self, client: httpx.AsyncClient) -> None:
        if self._session_id:
            return
        await self._post(client, {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agd-ticket-sink", "version": "0.1"},
            },
        })
        await self._post(
            client,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_result=False,
        )

    async def call(self, tool: str, arguments: dict | None = None):
        """Call a tool and return its decoded payload. Raises McpError on any failure."""
        async with self._lock:
            async with httpx.AsyncClient(timeout=self._timeout, verify=_verify()) as client:
                await self._ensure_session(client)
                payload = {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments or {}},
                }
                try:
                    resp = await self._post(client, payload)
                except StaleSession:
                    # One retry on a fresh session: an MCP restart costs one
                    # round trip instead of failing the request.
                    self._session_id = ""
                    await self._ensure_session(client)
                    payload["id"] = self._next_id()
                    resp = await self._post(client, payload)
        return decode_result(tool, resp)


def decode_result(tool: str, resp: dict):
    """Decode a tools/call response, surfacing every failure shape as McpError."""
    if "error" in resp:
        raise McpError(f"{tool}: {resp['error'].get('message', 'unknown error')}")

    result = resp.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    # In-band FastMCP failure: isError + plain-text message.
    if result.get("isError"):
        raise McpError(f"{tool}: {text or 'tool reported an error'}")
    if not content:
        raise McpError(f"{tool}: empty response")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(decoded, dict) and decoded.get("error"):
        raise McpError(f"{tool}: {decoded['error']}")
    return decoded
