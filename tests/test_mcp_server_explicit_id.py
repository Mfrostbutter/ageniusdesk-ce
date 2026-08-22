"""Explicit MCP server ids: fleet tool names embed the id (mcp:{id}:{tool}),
so a vault agent manifest can only target a server whose id is chosen up front."""

import pytest

from backend import auth_gate
from backend.modules.assistant import mcp_client


@pytest.fixture
def operator(anon, monkeypatch):
    async def _fake(_request):
        return {"username": "op", "source": "session", "role": "operator", "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)
    return anon


@pytest.fixture
def connect_ok(monkeypatch):
    """Server add probes the URL; fake a healthy MCP so the handler proceeds."""
    saved = []

    async def fake_add(server):
        saved.append(server)
        return {"success": True, "tools_count": 3}

    monkeypatch.setattr(mcp_client, "add_server", fake_add)
    return saved


def test_explicit_id_is_used(operator, connect_ok):
    r = operator.post("/api/mcp/servers", json={
        "name": "itops-mcp", "url": "http://10.0.7.99:8093/mcp", "id": "itops",
    })
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "itops"
    assert connect_ok[0]["id"] == "itops"


def test_invalid_id_rejected(operator, connect_ok):
    r = operator.post("/api/mcp/servers", json={
        "name": "x", "url": "http://10.0.7.99:8093/mcp", "id": "Bad Id!",
    })
    assert r.status_code == 400
    assert connect_ok == []


def test_duplicate_id_conflicts(operator, connect_ok, monkeypatch):
    monkeypatch.setattr(
        mcp_client, "get_mcp_servers", lambda: [{"id": "itops", "name": "existing"}]
    )
    r = operator.post("/api/mcp/servers", json={
        "name": "x", "url": "http://10.0.7.99:8093/mcp", "id": "itops",
    })
    assert r.status_code == 409
    assert connect_ok == []


def test_blank_id_still_random(operator, connect_ok):
    r = operator.post("/api/mcp/servers", json={
        "name": "x", "url": "http://10.0.7.99:8093/mcp",
    })
    assert r.status_code == 200
    generated = r.json()["id"]
    assert len(generated) == 16
    assert generated != "itops"
