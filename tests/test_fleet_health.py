"""Fleet Health: workflow health rolled up across all instances.

Fans out per instance over httpx; here we mock two instances (one healthy, one
unreachable) and assert the roll-up math and graceful degradation.
"""

import httpx
import respx

from backend.modules.n8n_proxy import client


@respx.mock
async def test_fleet_health_rollup(monkeypatch):
    instances = [
        {"id": "a", "name": "Client A", "url": "http://a.test", "api_key": "k", "color": "#f00"},
        {"id": "b", "name": "Client B", "url": "http://b.test", "api_key": "k"},
    ]
    monkeypatch.setattr("backend.config.get_instances", lambda: instances)
    monkeypatch.setattr("backend.config.get_active_instance_id", lambda: "a")

    # A reachable: 3 workflows (2 active); 4 recent runs, 2 errors both on w2.
    respx.get("http://a.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "w1", "name": "WF1", "active": True},
        {"id": "w2", "name": "WF2", "active": True},
        {"id": "w3", "name": "WF3", "active": False},
    ]}))
    respx.get("http://a.test/api/v1/executions").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "e1", "workflowId": "w1", "status": "success"},
        {"id": "e2", "workflowId": "w2", "status": "error"},
        {"id": "e3", "workflowId": "w2", "status": "error"},
        {"id": "e4", "workflowId": "w1", "status": "success"},
    ]}))
    # B unreachable.
    respx.get("http://b.test/api/v1/workflows").mock(side_effect=httpx.ConnectError("nope"))

    data = await client.fleet_health(exec_limit=50)

    t = data["totals"]
    assert t["instances"] == 2
    assert t["reachable"] == 1
    assert t["workflows_total"] == 3
    assert t["workflows_active"] == 2
    assert t["exec_total"] == 4
    assert t["exec_error"] == 2
    assert t["error_rate"] == 50

    by_id = {i["id"]: i for i in data["instances"]}
    assert by_id["a"]["reachable"] is True
    assert by_id["a"]["active"] is True
    assert by_id["a"]["unhealthy"][0] == {"id": "w2", "name": "WF2", "errors": 2}
    assert by_id["b"]["reachable"] is False
    assert by_id["b"]["error"]  # carries a reason, not fatal
    assert by_id["b"]["active"] is False


@respx.mock
async def test_fleet_health_auth_failure_is_degraded(monkeypatch):
    monkeypatch.setattr("backend.config.get_instances",
                        lambda: [{"id": "x", "name": "X", "url": "http://x.test", "api_key": "bad"}])
    monkeypatch.setattr("backend.config.get_active_instance_id", lambda: "x")
    respx.get("http://x.test/api/v1/workflows").mock(return_value=httpx.Response(401))

    data = await client.fleet_health()
    inst = data["instances"][0]
    assert inst["reachable"] is False
    assert inst["error"] == "auth"
    assert data["totals"]["reachable"] == 0


async def test_fleet_health_no_instances(monkeypatch):
    monkeypatch.setattr("backend.config.get_instances", lambda: [])
    monkeypatch.setattr("backend.config.get_active_instance_id", lambda: "")
    data = await client.fleet_health()
    assert data["instances"] == []
    assert data["totals"]["instances"] == 0
    assert data["totals"]["error_rate"] == 0
