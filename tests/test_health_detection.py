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
                           "data": {"main": [[{"json": {"error": {
                               "name": "AxiosError", "status": 503, "message": "boom"}}}]]}}],
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

    # The metrics strip surfaces the silent-failure count for the UI card.
    m = client.get("/api/otel/metrics?window_hours=24").json()
    assert m["silent_failures"] >= 1
    assert m["silent_rate"] > 0


LOUD_TRACE_HEX = "77" * 16


def _loud_request() -> ExportTraceServiceRequest:
    """An execution n8n itself reported as error (loud, not silent)."""
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(_kv("service.name", "n8n"))
    ss = rs.scope_spans.add()
    wf = ss.spans.add()
    wf.trace_id = b"\x77" * 16
    wf.span_id = b"\x01" * 8
    wf.name = "workflow.execute"
    wf.start_time_unix_nano = 1_000_000_000
    wf.end_time_unix_nano = 1_500_000_000
    wf.status.code = 1
    wf.attributes.append(_kv("n8n.workflow.name", "Halting WF"))
    wf.attributes.append(_kv("n8n.execution.id", "7007"))
    wf.attributes.append(_kv("n8n.execution.status", "error"))  # loud: n8n reported it
    node = ss.spans.add()
    node.trace_id = b"\x77" * 16
    node.span_id = b"\x02" * 8
    node.parent_span_id = b"\x01" * 8
    node.name = "node.execute"
    node.start_time_unix_nano = 1_100_000_000
    node.end_time_unix_nano = 1_200_000_000
    node.status.code = 1
    node.attributes.append(_kv("n8n.node.name", "Boom"))
    return req


def test_loud_error_is_not_flagged_silent(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)
    from backend.modules.observability import ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "get_active_instance_id", lambda: "test-inst")
    monkeypatch.setattr(health, "get_active_instance_id", lambda: "test-inst")

    async def fake_raw(execution_id):
        return {"data": {"resultData": {"runData": {
            "Boom": [{"executionStatus": "error", "error": {"name": "NodeApiError", "message": "down"},
                      "data": {"main": [[]]}}],
        }}}}
    monkeypatch.setattr(health.n8n_client, "get_execution_raw", fake_raw)

    r = client.post("/api/otel/v1/traces",
                    content=_loud_request().SerializeToString(),
                    headers={"Content-Type": "application/x-protobuf"})
    assert r.status_code == 200

    spans = client.get(f"/api/otel/traces/{LOUD_TRACE_HEX}").json()["spans"]
    boom = next(s for s in spans if (s.get("attributes") or {}).get("n8n.node.name") == "Boom")
    assert boom["health_status"] == "ERROR"  # the node error is still detected...
    t = next(t for t in client.get("/api/otel/traces").json()["traces"] if t["trace_id"] == LOUD_TRACE_HEX)
    assert t["has_silent"] is False  # ...but the run was loud, so not a silent failure


# ── Phase 2: low-output anomaly classifier ────────────────────────────────────


def test_classify_cold_start_never_fires():
    # Fewer than min_samples: unknown normal -> EMPTY for a zero, never LOW.
    assert health._classify_low_output([5] * 5, 0, 3) == ("EMPTY", "cold_start")


def test_classify_steady_producer_zero_with_input_is_low():
    assert health._classify_low_output([200] * 25, 0, 5) == ("LOW", "empty")


def test_classify_steady_producer_zero_without_input_is_inherited():
    # Origin rule: no input means it inherited emptiness upstream, not the origin.
    assert health._classify_low_output([200] * 25, 0, 0) == ("EMPTY", "inherited")


def test_classify_intermittent_zero_is_expected():
    # ~40% zeros -> not a steady producer -> a zero is within normal (Email Assistant).
    hist = ([0, 5] * 10) + [5] * 5
    assert health._classify_low_output(hist, 0, 3) == ("EMPTY", "expected")


def test_classify_magnitude_drop_fires_low():
    # 200*0.1 = 20 band; output 3 is far below -> drop anomaly.
    assert health._classify_low_output([200] * 25, 3, 5) == ("LOW", "drop")


def test_classify_normal_output_is_ok():
    assert health._classify_low_output([200] * 25, 190, 5)[0] == "OK"


def test_classify_drop_origin_flags_when_input_normal():
    # Origin of a drop: a data source (input is the steady trigger count) whose
    # output collapsed vs its own baseline -> fire.
    out_hist = [500] * 25
    in_hist = [1] * 25
    assert health._classify_low_output(out_hist, 5, 1, in_hist) == ("LOW", "drop")


def test_classify_drop_victim_suppressed_when_input_also_dropped():
    # Downstream victim: normally receives 500, now receives 5 because upstream
    # dropped. It only passed the reduced volume through -> suppressed so only the
    # origin fires (root-cause dedup, locked 2026-07-09).
    out_hist = [500] * 25
    in_hist = [500] * 25
    assert health._classify_low_output(out_hist, 5, 5, in_hist) == ("OK", "inherited_drop")


def test_classify_drop_not_suppressed_without_input_history():
    # Too little input history to prove a victim -> keep the flag (recall on origin).
    assert health._classify_low_output([500] * 25, 5, 5, [500] * 2) == ("LOW", "drop")


LOW_TRACE_HEX = "66" * 16


def _low_request() -> ExportTraceServiceRequest:
    """Green run: a 'Reliable' node and a 'New' node both output 0 with input 1."""
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(_kv("service.name", "n8n"))
    ss = rs.scope_spans.add()
    wf = ss.spans.add()
    wf.trace_id = b"\x66" * 16
    wf.span_id = b"\x01" * 8
    wf.name = "workflow.execute"
    wf.start_time_unix_nano = 1_000_000_000
    wf.end_time_unix_nano = 1_500_000_000
    wf.status.code = 1
    wf.attributes.append(_kv("n8n.workflow.name", "Daily Report"))
    wf.attributes.append(_kv("n8n.execution.id", "6006"))
    wf.attributes.append(_kv("n8n.execution.status", "success"))
    for i, (node, nid) in enumerate([("Reliable", "rel"), ("New", "new")], start=2):
        n = ss.spans.add()
        n.trace_id = b"\x66" * 16
        n.span_id = bytes([i]) * 8
        n.parent_span_id = b"\x01" * 8
        n.name = "node.execute"
        n.start_time_unix_nano = 1_000_000_000 + i
        n.end_time_unix_nano = 1_000_000_100 + i
        n.status.code = 1  # OK — the lie
        n.attributes.append(_kv("n8n.node.name", node))
        n.attributes.append(_kv("n8n.node.id", nid))
        n.attributes.append(_kv("n8n.node.items.input", 1))
        n.attributes.append(_kv("n8n.node.items.output", 0))
    return req


def test_low_output_flags_silent_by_history(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)

    from backend.modules.observability import ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "get_active_instance_id", lambda: "test-inst")
    monkeypatch.setattr(health, "get_active_instance_id", lambda: "test-inst")

    async def no_rundata(execution_id):
        return {}  # pure mode-3 path, no errors
    monkeypatch.setattr(health.n8n_client, "get_execution_raw", no_rundata)

    async def fake_history(node_id, window, exclude_trace_id=""):
        # "rel" is a steady producer; "new" has no history (cold start).
        return [200] * 25 if node_id == "rel" else []
    monkeypatch.setattr(health.storage, "node_output_history", fake_history)

    async def fake_input_history(node_id, window, exclude_trace_id=""):
        return []  # no input baseline -> drop-origin suppression stays off
    monkeypatch.setattr(health.storage, "node_input_history", fake_input_history)

    r = client.post("/api/otel/v1/traces",
                    content=_low_request().SerializeToString(),
                    headers={"Content-Type": "application/x-protobuf"})
    assert r.status_code == 200

    spans = client.get(f"/api/otel/traces/{LOW_TRACE_HEX}").json()["spans"]
    by = {(s.get("attributes") or {}).get("n8n.node.name"): s for s in spans if s["name"] == "node.execute"}
    # Reliable producer dropping to zero with input is a silent failure...
    assert by["Reliable"]["health_status"] == "LOW"
    assert by["Reliable"]["error_type"] == "empty_output"
    # ...a brand-new node with no history stays informational (precision over recall).
    assert by["New"]["health_status"] == "EMPTY"

    t = next(t for t in client.get("/api/otel/traces").json()["traces"] if t["trace_id"] == LOW_TRACE_HEX)
    assert t["has_silent"] is True

    # It must also surface in the errors pipeline (Overview / Insights / Errors
    # views), as a distinct "Silent failure" class, so operators who never open
    # the trace waterfall still see it.
    errs = client.get("/api/errors?instance_id=all&limit=50").json()["errors"]
    silent_rows = [e for e in errs if e.get("error_type") == "Silent failure"]
    assert any(e.get("node_name") == "Reliable" for e in silent_rows)
    # The informational EMPTY node ("New") must NOT emit an error row.
    assert all(e.get("node_name") != "New" for e in silent_rows)
