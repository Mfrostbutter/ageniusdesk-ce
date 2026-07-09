"""Silent-failure detection: green-but-broken runs surface loud.

Proves the detector keys on run-data + item counts, NOT span status (which the
n8n exporter reports OK for Continue-On-Fail nodes). Mirrors the cost-enrichment
test's OTLP-through-the-receiver pattern.
"""

import pytest

from backend.config import settings
from backend.modules.observability import health

otlp = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest  # noqa: E402
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue  # noqa: E402

OWNER = {"email": "owner@example.com", "password": "Fro5tbutt3r!"}


# ── Unit: error-shape normalization (object vs string vs signals) ──────────────


def test_normalize_object_error_extracts_http_status():
    t, s, http = health._normalize_error({"name": "AxiosError", "status": 503, "message": "boom"})
    assert t == "AxiosError" and s == "boom" and http == 503


def test_normalize_string_error_is_thrown():
    t, s, http = health._normalize_error("silent boom [line 1]")
    assert t == "thrown" and s == "silent boom [line 1]" and http is None


def test_node_error_from_item_level_json_error():
    # Continue-On-Fail demotes the error into the normal output item.
    run = {"executionStatus": "success",
           "data": {"main": [[{"json": {"error": "kaboom"}}]]}}
    assert health._node_error_from_run(run) == ("thrown", "kaboom", None)


def test_node_error_from_run_level_error_object():
    run = {"executionStatus": "error", "error": {"name": "NodeApiError", "httpCode": "500", "message": "x"},
           "data": {"main": [[]]}}
    t, s, http = health._node_error_from_run(run)
    assert t == "NodeApiError" and http == 500


def test_node_error_none_when_clean():
    run = {"executionStatus": "success", "data": {"main": [[{"json": {"ok": 1}}]]}}
    assert health._node_error_from_run(run) is None


# ── End-to-end: green trace, one demoted error + one zero-items node ───────────


def _kv(key, value):
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=str(value)))


SILENT_TRACE_HEX = "55" * 16


def _silent_request() -> ExportTraceServiceRequest:
    """One success execution: 3 node spans, all span-status OK (the lie)."""
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(_kv("service.name", "n8n"))
    ss = rs.scope_spans.add()

    wf = ss.spans.add()
    wf.trace_id = b"\x55" * 16
    wf.span_id = b"\x01" * 8
    wf.name = "workflow.execute"
    wf.start_time_unix_nano = 1_000_000_000
    wf.end_time_unix_nano = 1_500_000_000
    wf.status.code = 1  # OK
    wf.attributes.append(_kv("n8n.workflow.name", "Nightly Sync"))
    wf.attributes.append(_kv("n8n.execution.id", "5555"))
    wf.attributes.append(_kv("n8n.execution.status", "success"))

    for i, (node, out) in enumerate([("HTTP Call", 1), ("Empty Sheet", 0), ("Good Node", 3)], start=2):
        n = ss.spans.add()
        n.trace_id = b"\x55" * 16
        n.span_id = bytes([i]) * 8
        n.parent_span_id = b"\x01" * 8
        n.name = "node.execute"
        n.start_time_unix_nano = 1_000_000_000 + i
        n.end_time_unix_nano = 1_000_000_100 + i
        n.status.code = 1  # OK — the exporter lies for the failed node
        n.attributes.append(_kv("n8n.node.name", node))
        n.attributes.append(_kv("n8n.node.items.output", out))
    return req


def _auth(client):
    client.cookies.clear()
    r = client.post("/api/auth/setup", json=OWNER)
    if r.status_code == 409:
        r = client.post("/api/auth/login", json={"username": OWNER["email"], "password": OWNER["password"]})
    assert r.status_code in (200, 201), r.text
    return client


def test_silent_failure_detected_under_green_run(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)

    # Pin instance identity so the health fetch guard matches.
    from backend.modules.observability import ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "get_active_instance_id", lambda: "test-inst")
    monkeypatch.setattr(health, "get_active_instance_id", lambda: "test-inst")

    async def fake_raw(execution_id):
        assert execution_id == "5555"
        return {"data": {"resultData": {"runData": {
            # HTTP node: error demoted into the normal output (span said OK).
            "HTTP Call": [{"executionStatus": "success",
                           "data": {"main": [[{"json": {"error": {"name": "AxiosError", "status": 503, "message": "boom"}}}]]}}],
            "Empty Sheet": [{"executionStatus": "success", "data": {"main": [[]]}}],
            "Good Node": [{"executionStatus": "success", "data": {"main": [[{"json": {"x": 1}}]]}}],
        }}}}

    monkeypatch.setattr(health.n8n_client, "get_execution_raw", fake_raw)

    r = client.post("/api/otel/v1/traces",
                    content=_silent_request().SerializeToString(),
                    headers={"Content-Type": "application/x-protobuf"})
    assert r.status_code == 200

    # GET triggers lazy enrichment (deterministic; the eager ingest task is best-effort).
    spans = client.get(f"/api/otel/traces/{SILENT_TRACE_HEX}").json()["spans"]
    by = {(s.get("attributes") or {}).get("n8n.node.name"): s for s in spans if s["name"] == "node.execute"}

    assert by["HTTP Call"]["health_status"] == "ERROR"
    assert by["HTTP Call"]["error_type"] == "AxiosError"
    assert by["HTTP Call"]["http_status"] == 503
    assert by["Empty Sheet"]["health_status"] == "EMPTY"
    assert by["Empty Sheet"]["output_items"] == 0
    assert by["Good Node"]["health_status"] == "OK"

    # The trace lied "success" at the top but is flagged silent in the list.
    traces = client.get("/api/otel/traces").json()["traces"]
    t = next(t for t in traces if t["trace_id"] == SILENT_TRACE_HEX)
    assert t["has_silent"] is True
    assert t["has_error"] is False  # no span carried ERROR status — that's the whole point
