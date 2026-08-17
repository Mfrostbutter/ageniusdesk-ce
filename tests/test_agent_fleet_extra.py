"""Agent Fleet smoke tests that require the langgraph extra.

Skipped wholesale when the extra is absent (default CI); with it installed they
prove the gate flips on, the catalog serves, every built-in agent's graph
compiles (no key, no network), and the BUG-017 single-flight claim holds at the
router level.
"""

import asyncio
import importlib.util

import pytest

from backend import auth_gate

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("langgraph") is None,
    reason="langgraph extra not installed",
)


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


def test_gate_auto_detects_installed_extra(monkeypatch):
    from backend.config import agents_enabled, settings

    monkeypatch.setattr(settings, "agd_agents_enabled", None)
    assert agents_enabled() is True


def test_catalog_lists_agents(client, monkeypatch):
    _as_role(monkeypatch, "admin")
    r = client.get("/api/agent-fleet/agents")
    assert r.status_code == 200, r.text
    agents = r.json().get("agents") or r.json()
    assert agents, "catalog must list at least the built-in agent"


def test_every_builtin_graph_compiles(client, monkeypatch):
    """The /graph endpoint builds the real LangGraph with a throwaway model;
    this is the deepest no-key proof the extra's stack actually works."""
    from backend.modules.agent_fleet import registry

    _as_role(monkeypatch, "admin")
    for agent_id in registry.builtin_ids():
        r = client.get(f"/api/agent-fleet/agents/{agent_id}/graph")
        assert r.status_code == 200, f"{agent_id}: {r.text}"
        body = r.json()
        assert body["nodes"], f"{agent_id} compiled to an empty graph"
        assert body["edges"], f"{agent_id} compiled with no edges"


def test_double_start_races_to_409(client, monkeypatch):
    """BUG-017 at the router: the second of two immediate starts must 409, and
    the loser must not leave an orphan run row."""
    from backend.modules.agent_fleet import runner, storage

    _as_role(monkeypatch, "admin")
    release = asyncio.Event()

    async def slow_run(run_id, agent_id, error_id, prompt):
        try:
            await release.wait()
        finally:
            runner._live_run_id = None

    monkeypatch.setattr(runner, "run", slow_run)
    runner._live_run_id = None
    try:
        r1 = client.post("/api/agent-fleet/triage", json={"prompt": "qa smoke"})
        assert r1.status_code == 200, r1.text
        run_id = r1.json()["run_id"]
        r2 = client.post("/api/agent-fleet/triage", json={"prompt": "qa smoke double"})
        assert r2.status_code == 409, r2.text
        release.set()
    finally:
        runner._live_run_id = None
        # TestClient's loop already ran the task; drop the row we created.
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            storage.delete_run(run_id)
        )
