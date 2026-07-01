"""Regression coverage for the 2026-07-01 full-review High-severity findings.

#1 — Agent Fleet registered + ran operator-authored graph.py IN-PROCESS
     (exec_module) behind only "any authenticated identity". A viewer could
     author and execute arbitrary Python. The router now has an admin floor.

#2 — /api/mcp/* (add/test/discover) had no role floor and fetched an
     operator-supplied URL server-side with no SSRF guard, reflecting the body.
     The router now requires operator, and the fetch path validates the URL
     through assert_safe_probe_url (metadata / link-local blocked).

#3 — Community-template field substitution wrote operator field values into
     serialized JSON then re-parsed it, so a value with a quote could inject
     arbitrary HostConfig (Privileged / host bind mounts). Substitution now
     runs on the parsed object's string leaves, so a value can only ever land
     as a string.

Role checks are exercised by patching backend.auth_gate.current_user (which
require_role resolves at call time), matching test_assistant_authz_ssrf.py.
"""

import pytest

import backend.auth_gate as auth_gate
from backend.modules.assistant import mcp_client
from backend.modules.docker_mgr.templates import _apply_subs


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


# ── #1 Agent Fleet: admin floor ──────────────────────────────────────────────


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_below_admin_cannot_reach_agent_fleet(anon, monkeypatch, role):
    _as_role(monkeypatch, role)
    # Even the read catalog builds/imports vault agents, so the whole router is
    # admin-gated. A sub-admin identity is rejected before any handler runs.
    assert anon.get("/api/agent-fleet/agents").status_code == 403
    assert anon.post("/api/agent-fleet/agents", json={
        "name": "pwn", "code": "def build(llm, tools, checkpointer=None):\n    pass\n",
    }).status_code == 403


def test_admin_can_reach_agent_fleet(anon, monkeypatch):
    _as_role(monkeypatch, "admin")
    assert anon.get("/api/agent-fleet/agents").status_code == 200


# ── #2 MCP: operator floor + SSRF guard ──────────────────────────────────────


def test_viewer_cannot_add_mcp_server(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    r = anon.post("/api/mcp/servers", json={"name": "x", "url": "http://localhost:9000"})
    assert r.status_code == 403


def test_operator_add_mcp_server_blocks_metadata_ssrf(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    r = anon.post("/api/mcp/servers", json={
        "name": "meta", "url": "http://169.254.169.254/latest/meta-data/",
    })
    # Reaches the handler (not 403), but the fetch is refused before any request.
    assert r.status_code == 400
    assert "Blocked URL" in r.json().get("detail", "")


@pytest.mark.parametrize("blocked", [
    "http://169.254.169.254/mcp",       # cloud metadata (link-local)
    "http://[fe80::1]/mcp",             # IPv6 link-local
    "http://0.0.0.0:8088",              # unspecified
])
def test_mcp_normalize_blocks_unsafe(blocked):
    with pytest.raises(mcp_client.UnsafeProbeURL):
        mcp_client._normalize_mcp_urls(blocked)


@pytest.mark.parametrize("ok", [
    "http://localhost:8088/mcp",
    "http://10.10.0.61:8088",           # legit self-hosted MCP on the LAN
])
def test_mcp_normalize_allows_self_hosted(ok):
    base, mcp_url = mcp_client._normalize_mcp_urls(ok)
    assert mcp_url.endswith("/mcp")


# ── #3 Docker community-template JSON injection ───────────────────────────────


def test_template_field_value_cannot_inject_hostconfig():
    template_config = {"Image": "redis", "Env": ["PW={password}"]}
    payload = 'x"],"HostConfig":{"Privileged":true,"Binds":["/:/host:rw"]},"z":["'
    out = _apply_subs(template_config, {"password": payload})
    # The payload lands as a plain string; no structure is injected.
    assert "HostConfig" not in out
    assert out["Env"][0].startswith("PW=x")
    assert out["Image"] == "redis"


def test_template_substitution_preserves_normal_values():
    template_config = {"Env": ["HOST={instance_name}", "PORT={port}"], "Image": "n8nio/n8n"}
    out = _apply_subs(template_config, {"instance_name": "prod", "port": "5678"})
    assert out["Env"] == ["HOST=prod", "PORT=5678"]
    assert out["Image"] == "n8nio/n8n"
