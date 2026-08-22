"""Public API run-start for fleet agents + MCP tools in the fleet registry."""

from typing import Any, Optional

import pytest

from backend.modules.agent_fleet import runner, tools_mcp
from backend.modules.public_api.api_keys import create_api_key, delete_api_key


@pytest.fixture
def trigger_key():
    raw, record = create_api_key("test-trigger", "trigger")
    yield raw
    delete_api_key(record["id"])


@pytest.fixture
def read_key():
    raw, record = create_api_key("test-read", "read")
    yield raw
    delete_api_key(record["id"])


# ── /api/v1/agents surface ────────────────────────────────────────────────────


def test_agents_require_key(client):
    client.cookies.clear()
    assert client.get("/api/v1/agents").status_code in (401, 403)
    assert client.post("/api/v1/agents/ops-triage/runs", json={}).status_code in (401, 403)


def test_list_agents_with_read_key(client, read_key):
    resp = client.get("/api/v1/agents", headers={"X-API-Key": read_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["default"]
    assert any(a["id"] == body["default"] for a in body["agents"])


def test_start_run_needs_trigger_scope(client, read_key):
    resp = client.post(
        "/api/v1/agents/ops-triage/runs", json={}, headers={"X-API-Key": read_key}
    )
    assert resp.status_code == 403


def test_start_run_unknown_agent_404(client, trigger_key):
    resp = client.post(
        "/api/v1/agents/no-such-agent/runs", json={}, headers={"X-API-Key": trigger_key}
    )
    assert resp.status_code == 404


def test_start_run_409_while_live(client, trigger_key, monkeypatch):
    monkeypatch.setattr(runner, "is_live", lambda: "someone-else")
    resp = client.post(
        "/api/v1/agents/ops-triage/runs", json={}, headers={"X-API-Key": trigger_key}
    )
    assert resp.status_code == 409


def test_start_run_launches_and_is_readable(client, trigger_key, read_key, monkeypatch):
    launched = {}

    async def fake_run(run_id, agent_id, error_id, prompt):
        launched.update(run_id=run_id, agent_id=agent_id, error_id=error_id, prompt=prompt)
        runner._live_run_id = None

    monkeypatch.setattr(runner, "run", fake_run)
    resp = client.post(
        "/api/v1/agents/ops-triage/runs",
        json={"prompt": "look at the latest error"},
        headers={"X-API-Key": trigger_key},
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    assert launched["agent_id"] == "ops-triage"
    assert launched["prompt"] == "look at the latest error"

    detail = client.get(f"/api/v1/agents/runs/{run_id}", headers={"X-API-Key": read_key})
    assert detail.status_code == 200
    assert detail.json()["agent_id"] == "ops-triage"
    # Slot must be released for the next test regardless of task timing.
    runner._live_run_id = None


def test_get_unknown_run_404(client, read_key):
    resp = client.get("/api/v1/agents/runs/nope", headers={"X-API-Key": read_key})
    assert resp.status_code == 404


# ── runner.start single-flight ────────────────────────────────────────────────


async def test_runner_start_rejects_unknown_agent():
    with pytest.raises(runner.RunStartError) as exc:
        await runner.start("definitely-not-an-agent", None, "")
    assert exc.value.status == 404


async def test_runner_start_rejects_when_live(monkeypatch):
    monkeypatch.setattr(runner, "is_live", lambda: "busy-run")
    with pytest.raises(runner.RunStartError) as exc:
        await runner.start("", None, "")
    assert exc.value.status == 409


# ── MCP tools in the fleet registry ──────────────────────────────────────────


def _defn(server_id: str, tool: str, schema: Optional[dict] = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": f"mcp_{server_id}_{tool}",
            "description": f"[{server_id}] {tool}",
            "parameters": schema or {"type": "object", "properties": {}},
        },
        "_mcp_server_id": server_id,
        "_mcp_tool_name": tool,
        "_mcp_annotations": {},
    }


@pytest.fixture(autouse=True)
def _clean_mcp_cache():
    tools_mcp._CACHE.clear()
    tools_mcp._CACHE_AT = 0.0
    yield
    tools_mcp._CACHE.clear()
    tools_mcp._CACHE_AT = 0.0


async def test_prefetch_and_resolve(monkeypatch):
    import backend.modules.assistant.mcp_client as amc

    schema = {
        "type": "object",
        "properties": {"ticket_id": {"type": "integer"}, "note": {"type": "string"}},
        "required": ["ticket_id"],
    }

    monkeypatch.setattr(amc, "get_mcp_servers", lambda: [{"id": "itops", "name": "itops-mcp"}])

    async def fake_discover(server):
        return [_defn("itops", "get_ticket", schema)]

    monkeypatch.setattr(amc, "discover_tools", fake_discover)

    n = await tools_mcp.prefetch_all(force=True)
    assert n == 1
    tools = tools_mcp.resolve_cached(["mcp:itops:get_ticket", "mcp:itops:nope"])
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "mcp:itops:get_ticket"

    # Execution routes through the assistant MCP client; None optionals dropped.
    calls: list[Any] = []

    async def fake_execute(server_id, tool_name, args):
        calls.append((server_id, tool_name, args))
        return '{"ticket_id": 7}'

    monkeypatch.setattr(amc, "execute_tool", fake_execute)
    out = await tool.coroutine(ticket_id=7, note=None)
    assert out == '{"ticket_id": 7}'
    assert calls == [("itops", "get_ticket", {"ticket_id": 7})]


async def test_prefetch_survives_a_down_server(monkeypatch):
    import backend.modules.assistant.mcp_client as amc

    monkeypatch.setattr(
        amc, "get_mcp_servers",
        lambda: [{"id": "down", "name": "down"}, {"id": "up", "name": "up"}],
    )

    async def fake_discover(server):
        if server["id"] == "down":
            raise RuntimeError("connect refused")
        return [_defn("up", "ping")]

    monkeypatch.setattr(amc, "discover_tools", fake_discover)
    n = await tools_mcp.prefetch_all(force=True)
    assert n == 1
    assert tools_mcp.resolve_cached(["mcp:up:ping"])


def test_resolve_tools_merges_local_and_mcp():
    from langchain_core.tools import StructuredTool

    from backend.modules.agent_fleet import tools_local

    async def _noop():
        return ""

    fake = StructuredTool.from_function(coroutine=_noop, name="mcp:itops:x", description="x")
    tools_mcp._CACHE["mcp:itops:x"] = fake
    resolved = tools_local.resolve_tools(["list_recent_errors", "mcp:itops:x"])
    names = [t.name for t in resolved]
    assert names == ["list_recent_errors", "mcp:itops:x"]
