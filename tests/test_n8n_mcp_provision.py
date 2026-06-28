"""Built-in n8n-mcp provisioning: URL resolution, idempotent registration, and
the best-effort/opt-out boot behavior. Docker itself is never touched here — the
container ops are stubbed; we test the orchestration logic around them.
"""

import pytest

import backend.modules.assistant.mcp_client as mcp_client
import backend.modules.assistant.n8n_mcp_provision as prov


@pytest.fixture
def mem_servers(monkeypatch):
    """In-memory mcp_servers store so tests don't touch the shared config."""
    store: list[dict] = []
    monkeypatch.setattr(mcp_client, "get_mcp_servers", lambda: list(store))

    def _save(servers):
        store.clear()
        store.extend(servers)

    monkeypatch.setattr(mcp_client, "save_mcp_servers", _save)
    return store


@pytest.fixture
def no_sleep(monkeypatch):
    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(prov.asyncio, "sleep", _noop)


# ── URL resolution ───────────────────────────────────────────────────────────


def test_candidate_urls_in_docker(monkeypatch):
    monkeypatch.delenv("AGD_N8N_MCP_URL", raising=False)
    monkeypatch.setattr(prov, "_in_docker", lambda: True)
    urls = prov._candidate_urls(3456)
    assert urls[0] == "http://host.docker.internal:3456/mcp"
    assert "http://127.0.0.1:3456/mcp" in urls


def test_candidate_urls_bare_metal(monkeypatch):
    monkeypatch.delenv("AGD_N8N_MCP_URL", raising=False)
    monkeypatch.setattr(prov, "_in_docker", lambda: False)
    assert prov._candidate_urls(3456)[0] == "http://localhost:3456/mcp"


def test_candidate_urls_override(monkeypatch):
    monkeypatch.setenv("AGD_N8N_MCP_URL", "http://mcp.lan:9000/mcp/")
    assert prov._candidate_urls(3456) == ["http://mcp.lan:9000/mcp"]


# ── Registration (idempotent upsert, encrypted token) ────────────────────────


async def test_probe_registers_and_upserts(mem_servers, monkeypatch, no_sleep):
    async def fake_test(_server):
        return {"connected": True, "tools_count": 30}

    monkeypatch.setattr(mcp_client, "test_server", fake_test)
    monkeypatch.setattr(prov, "_in_docker", lambda: False)

    r1 = await prov._probe_and_register("tok1", "docs")
    assert r1["ok"] and r1["tools_count"] == 30
    reg = prov.get_registered()
    assert reg and reg["mode"] == "docs" and reg["url"].endswith("/mcp")
    assert reg["token"] and reg["token"] != "tok1"  # stored encrypted, not raw

    # Re-provisioning replaces the entry rather than duplicating it.
    await prov._probe_and_register("tok2", "full")
    matches = [s for s in mcp_client.get_mcp_servers() if s["id"] == prov.SERVER_ID]
    assert len(matches) == 1 and matches[0]["mode"] == "full"


async def test_probe_failure_does_not_register(mem_servers, monkeypatch, no_sleep):
    async def fake_test(_server):
        return {"connected": False, "error": "connection refused"}

    monkeypatch.setattr(mcp_client, "test_server", fake_test)
    monkeypatch.setattr(prov, "_in_docker", lambda: False)

    r = await prov._probe_and_register("tok", "docs")
    assert r["ok"] is False
    assert prov.get_registered() is None


# ── Auto-install gating ──────────────────────────────────────────────────────


async def test_ensure_opt_out(mem_servers, monkeypatch):
    monkeypatch.setenv("AGD_N8N_MCP_AUTO", "false")
    ran = {"v": False}

    async def fake_run(*_a, **_k):
        ran["v"] = True

    monkeypatch.setattr(prov, "_run_container", fake_run)
    await prov.ensure_n8n_mcp()
    assert ran["v"] is False
    assert prov.get_registered() is None


async def test_ensure_skips_without_docker(mem_servers, monkeypatch):
    monkeypatch.delenv("AGD_N8N_MCP_AUTO", raising=False)
    ran = {"v": False}

    async def fake_run(*_a, **_k):
        ran["v"] = True

    async def no_docker():
        return False

    monkeypatch.setattr(prov, "_run_container", fake_run)
    monkeypatch.setattr(prov, "_docker_available", no_docker)
    await prov.ensure_n8n_mcp()
    assert ran["v"] is False


async def test_ensure_skips_when_already_registered(mem_servers, monkeypatch):
    monkeypatch.delenv("AGD_N8N_MCP_AUTO", raising=False)
    prov._register("http://host.docker.internal:3456/mcp", "tok", "docs")
    ran = {"v": False}

    async def fake_run(*_a, **_k):
        ran["v"] = True

    async def docker_ok():
        return True

    monkeypatch.setattr(prov, "_run_container", fake_run)
    monkeypatch.setattr(prov, "_docker_available", docker_ok)
    await prov.ensure_n8n_mcp()
    assert ran["v"] is False  # already registered -> no re-provision


# ── enable / disable ─────────────────────────────────────────────────────────


async def test_enable_without_docker_is_manual(monkeypatch):
    async def no_docker():
        return False

    monkeypatch.setattr(prov, "_docker_available", no_docker)
    r = await prov.enable()
    assert r["ok"] is False and r.get("manual") is True


async def test_disable_unregisters(mem_servers, monkeypatch):
    prov._register("http://host.docker.internal:3456/mcp", "tok", "docs")

    async def no_docker():
        return False

    monkeypatch.setattr(prov, "_docker_available", no_docker)
    r = await prov.disable()
    assert r["ok"] and r["unregistered"] is True
    assert prov.get_registered() is None
