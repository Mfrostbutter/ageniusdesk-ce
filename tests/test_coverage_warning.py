"""Successful-execution data-save coverage detection (trace-backfill Decision 3).

An instance/workflow with saveDataSuccessExecution=none can never be
trace-backfilled for its successful runs. These tests stub the n8n Public API
and cover the three detection outcomes: a workflow-level "none" override, a
fully-covered instance, and an unreachable instance.
"""

import httpx
import respx

from backend.modules.n8n_proxy import coverage

INST = {"id": "a", "name": "Instance A", "url": "http://a.test", "api_key": "k"}


@respx.mock
async def test_workflow_level_none_detected():
    respx.get("http://a.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "w1", "name": "Silent Workflow", "active": True, "settings": {"saveDataSuccessExecution": "none"}},
        {"id": "w2", "name": "Normal Workflow", "active": True, "settings": {"saveDataSuccessExecution": "all"}},
    ]}))

    result = await coverage.check_data_save_coverage(INST)

    assert result["status"] == "degraded"
    assert result["affected_workflows"] == [{"id": "w1", "name": "Silent Workflow"}]
    assert "none" in result["reason"]


@respx.mock
async def test_all_saved_detected_via_execution_probe():
    respx.get("http://a.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "w1", "name": "Normal Workflow", "active": True, "settings": {"saveDataSuccessExecution": "all"}},
    ]}))
    respx.get("http://a.test/api/v1/executions").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "e1", "workflowId": "w1", "status": "success"},
    ]}))
    respx.get("http://a.test/api/v1/executions/e1").mock(return_value=httpx.Response(200, json={
        "id": "e1",
        "data": {"resultData": {"runData": {"Manual Trigger": [{"executionStatus": "success"}]}}},
    }))

    result = await coverage.check_data_save_coverage(INST)

    assert result == {"status": "ok", "affected_workflows": [], "reason": ""}


@respx.mock
async def test_instance_default_none_detected_via_empty_run_data():
    """No workflow overrides saveDataSuccessExecution, but the instance-level
    default discards it: the probe's runData comes back empty."""
    respx.get("http://a.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "w1", "name": "Normal Workflow", "active": True},
    ]}))
    respx.get("http://a.test/api/v1/executions").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "e1", "workflowId": "w1", "status": "success"},
    ]}))
    respx.get("http://a.test/api/v1/executions/e1").mock(return_value=httpx.Response(200, json={
        "id": "e1",
        "data": {"resultData": {"runData": {}}},
    }))

    result = await coverage.check_data_save_coverage(INST)

    assert result["status"] == "degraded"
    assert result["affected_workflows"] == []
    assert "instance default" in result["reason"]


@respx.mock
async def test_unreachable_instance_yields_unknown_without_raising():
    respx.get("http://a.test/api/v1/workflows").mock(side_effect=httpx.ConnectError("nope"))

    result = await coverage.check_data_save_coverage(INST)

    assert result["status"] == "unknown"
    assert result["affected_workflows"] == []


@respx.mock
async def test_no_recent_executions_yields_unknown():
    """Nothing to override AND nothing to probe -- can't conclude coverage is ok."""
    respx.get("http://a.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get("http://a.test/api/v1/executions").mock(return_value=httpx.Response(200, json={"data": []}))

    result = await coverage.check_data_save_coverage(INST)

    assert result["status"] == "unknown"


# ── Wired into fleet_health (where the Instances view reads coverage from) ────


@respx.mock
async def test_fleet_health_carries_coverage_fields(monkeypatch):
    monkeypatch.setattr("backend.config.get_instances", lambda: [INST])
    monkeypatch.setattr("backend.config.get_active_instance_id", lambda: "a")

    respx.get("http://a.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "w1", "name": "Silent Workflow", "active": True, "settings": {"saveDataSuccessExecution": "none"}},
    ]}))
    respx.get("http://a.test/api/v1/executions").mock(return_value=httpx.Response(200, json={"data": []}))

    from backend.modules.n8n_proxy import client
    data = await client.fleet_health()

    inst = data["instances"][0]
    assert inst["data_save_coverage"] == "degraded"
    assert inst["data_save_affected"] == [{"id": "w1", "name": "Silent Workflow"}]
