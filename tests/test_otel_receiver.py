"""OTLP trace receiver: gating, decode, storage, and the trace query API.

Builds real OTLP protobuf (and JSON) payloads with opentelemetry-proto and
drives them through the HTTP receiver, mirroring how n8n's native exporter posts
to /api/otel/v1/traces.
"""

import pytest

from backend.config import settings

# opentelemetry-proto is a hard dep of the observability module.
otlp = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
from google.protobuf.json_format import MessageToJson  # noqa: E402
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest  # noqa: E402
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue  # noqa: E402

OWNER = {"email": "owner@example.com", "password": "Fro5tbutt3r!"}


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _sample_request() -> ExportTraceServiceRequest:
    """One execution: a workflow.execute root span + one failing node.execute child."""
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(_kv("service.name", "n8n"))
    ss = rs.scope_spans.add()

    wf = ss.spans.add()
    wf.trace_id = b"\x11" * 16
    wf.span_id = b"\x01" * 8
    wf.name = "workflow.execute"
    wf.kind = 1
    wf.start_time_unix_nano = 1_000_000_000
    wf.end_time_unix_nano = 1_500_000_000
    wf.status.code = 1  # OK
    wf.attributes.append(_kv("n8n.workflow.id", "wf123"))
    wf.attributes.append(_kv("n8n.workflow.name", "Engagement Tracker"))
    wf.attributes.append(_kv("n8n.execution.id", "7989"))

    node = ss.spans.add()
    node.trace_id = b"\x11" * 16
    node.span_id = b"\x02" * 8
    node.parent_span_id = b"\x01" * 8
    node.name = "node.execute"
    node.kind = 1
    node.start_time_unix_nano = 1_100_000_000
    node.end_time_unix_nano = 1_400_000_000
    node.status.code = 2  # ERROR
    node.attributes.append(_kv("n8n.node.name", "HTTP Request"))
    return req


def _auth(client):
    """Establish (or recover) the owner session so the query endpoints are reachable."""
    client.cookies.clear()
    r = client.post("/api/auth/setup", json=OWNER)
    if r.status_code == 409:
        r = client.post("/api/auth/login", json={"username": OWNER["email"], "password": OWNER["password"]})
    assert r.status_code in (200, 201), r.text
    return client


TRACE_HEX = ("11" * 16)


# ── Gating ────────────────────────────────────────────────────────────────────


def test_ingest_404_when_disabled(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", False)
    r = anon.post(
        "/api/otel/v1/traces",
        content=_sample_request().SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 404


def test_ingest_rejects_bad_token(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "otel-secret")
    r = anon.post(
        "/api/otel/v1/traces",
        content=_sample_request().SerializeToString(),
        headers={"Content-Type": "application/x-protobuf", "Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_ingest_accepts_bearer_token(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "otel-secret")
    r = anon.post(
        "/api/otel/v1/traces",
        content=_sample_request().SerializeToString(),
        headers={"Content-Type": "application/x-protobuf", "Authorization": "Bearer otel-secret"},
    )
    assert r.status_code == 200


# ── Decode + storage + query ──────────────────────────────────────────────────


def test_protobuf_ingest_stores_and_queries(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")  # open path
    _auth(client)

    r = client.post(
        "/api/otel/v1/traces",
        content=_sample_request().SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 200

    traces = client.get("/api/otel/traces").json()["traces"]
    ours = [t for t in traces if t["trace_id"] == TRACE_HEX]
    assert len(ours) == 1
    t = ours[0]
    assert t["span_count"] == 2
    assert t["has_error"] is True
    assert t["workflow_name"] == "Engagement Tracker"
    assert t["execution_id"] == "7989"

    detail = client.get(f"/api/otel/traces/{TRACE_HEX}").json()["spans"]
    assert len(detail) == 2
    root = next(s for s in detail if s["parent_id"] == "")
    child = next(s for s in detail if s["parent_id"] != "")
    assert root["name"] == "workflow.execute"
    assert child["status"] == "ERROR"
    assert child["parent_id"] == root["span_id"]
    # Resource attributes are preserved on every span for later attribution work.
    assert root["attributes"].get("resource.service.name") == "n8n"


def test_ingest_dedupes_on_replay(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)
    body = _sample_request().SerializeToString()
    headers = {"Content-Type": "application/x-protobuf"}
    assert client.post("/api/otel/v1/traces", content=body, headers=headers).status_code == 200
    assert client.post("/api/otel/v1/traces", content=body, headers=headers).status_code == 200
    # Same (trace_id, span_id) pairs must not double-insert.
    detail = client.get(f"/api/otel/traces/{TRACE_HEX}").json()["spans"]
    assert len(detail) == 2


def test_by_execution_lookup(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)
    client.post(
        "/api/otel/v1/traces",
        content=_sample_request().SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    r = client.get("/api/otel/by-execution/7989").json()
    assert r["trace_id"] == TRACE_HEX
    assert len(r["spans"]) == 2
    # Unknown execution resolves to an empty result, not an error.
    empty = client.get("/api/otel/by-execution/nope-404").json()
    assert empty["trace_id"] == "" and empty["spans"] == []


def test_metrics_summary(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)
    client.post(
        "/api/otel/v1/traces",
        content=_sample_request().SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    m = client.get("/api/otel/metrics?window_hours=24").json()
    assert m["executions"] >= 1
    assert m["errors"] >= 1  # the sample's node span is ERROR -> trace is errored
    assert m["error_rate"] > 0
    # Root trace spans 1.0s -> 1.5s == 500ms; min-start/max-end per trace.
    assert m["p50_ms"] == 500.0


def test_traces_workflow_filter(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)
    client.post(
        "/api/otel/v1/traces",
        content=_sample_request().SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    hit = client.get("/api/otel/traces?workflow_id=wf123").json()["traces"]
    assert any(t["trace_id"] == TRACE_HEX for t in hit)
    miss = client.get("/api/otel/traces?workflow_id=does-not-exist").json()["traces"]
    assert all(t["trace_id"] != TRACE_HEX for t in miss)


def test_pricing_bundled_and_normalize():
    from backend.modules.observability import pricing
    p = pricing.price_for("claude-sonnet-4-6")
    assert p and p["in"] == 3.0 and p["out"] == 15.0 and p["source"] == "bundled" and p["estimate"]
    # Vendor prefix + dotted variant should normalize to the same entry.
    p2 = pricing.price_for("anthropic/claude-sonnet-4.6")
    assert p2 and p2["in"] == 3.0
    assert pricing.price_for("totally-unknown-model-xyz") is None


def test_fix_mojibake_repairs_double_encoded_names():
    from backend.modules.observability.ingest import _fix_mojibake

    # An em-dash that arrived double-encoded (UTF-8 bytes read as cp1252).
    assert _fix_mojibake("Job Hunt â€” Build Packages") == "Job Hunt — Build Packages"
    # A correctly-encoded em-dash carries no marker and is left untouched.
    assert _fix_mojibake("A — B") == "A — B"
    # Plain ASCII and empty strings are untouched.
    assert _fix_mojibake("Email Assistant 4 - get many") == "Email Assistant 4 - get many"
    assert _fix_mojibake("") == ""


def _ai_request():
    """A trace with an AI language-model node (workflow root + one node span)."""
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(_kv("service.name", "n8n"))
    ss = rs.scope_spans.add()
    wf = ss.spans.add()
    wf.trace_id = b"\x33" * 16
    wf.span_id = b"\x01" * 8
    wf.name = "workflow.execute"
    wf.start_time_unix_nano = 1_000_000_000
    wf.end_time_unix_nano = 2_000_000_000
    wf.status.code = 1
    wf.attributes.append(_kv("n8n.workflow.name", "Cost WF"))
    wf.attributes.append(_kv("n8n.execution.id", "9001"))
    node = ss.spans.add()
    node.trace_id = b"\x33" * 16
    node.span_id = b"\x02" * 8
    node.parent_span_id = b"\x01" * 8
    node.name = "node.execute"
    node.start_time_unix_nano = 1_100_000_000
    node.end_time_unix_nano = 1_900_000_000
    node.status.code = 1
    node.attributes.append(_kv("n8n.node.name", "Sonnet 4.6"))
    node.attributes.append(_kv("n8n.node.type", "@n8n/n8n-nodes-langchain.lmChatAnthropic"))
    return req


COST_TRACE_HEX = "33" * 16


def test_cost_enrichment_from_rundata(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)

    from backend.modules.observability import cost

    async def fake_raw(execution_id):
        assert execution_id == "9001"
        return {"data": {"resultData": {"runData": {
            "Sonnet 4.6": [{
                "data": {"ai_languageModel": [[{"json": {
                    "tokenUsage": {"promptTokens": 1000, "completionTokens": 200, "totalTokens": 1200},
                    "options": {"model": "claude-sonnet-4-6"},
                }}]]},
            }],
        }}}}

    monkeypatch.setattr(cost.n8n_client, "get_execution_raw", fake_raw)

    client.post(
        "/api/otel/v1/traces",
        content=_ai_request().SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    # GET the trace triggers lazy enrichment.
    spans = client.get(f"/api/otel/traces/{COST_TRACE_HEX}").json()["spans"]
    ai = next(s for s in spans if s["name"] == "node.execute")
    assert ai["model"] == "claude-sonnet-4-6"
    assert ai["tokens_in"] == 1000 and ai["tokens_out"] == 200
    # 1000/1e6*3 + 200/1e6*15 = 0.003 + 0.003 = 0.006
    assert abs(ai["cost_usd"] - 0.006) < 1e-9
    assert ai["cost_source"] == "n8n-rundata"
    # Trace list surfaces the rolled-up cost.
    traces = client.get("/api/otel/traces").json()["traces"]
    t = next(t for t in traces if t["trace_id"] == COST_TRACE_HEX)
    assert abs(t["cost_usd"] - 0.006) < 1e-9 and t["has_cost"]


def test_json_ingest_path(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_otel_enabled", True)
    monkeypatch.setattr(settings, "agd_otel_token", "")
    _auth(client)
    payload = MessageToJson(_sample_request())
    r = client.post(
        "/api/otel/v1/traces",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    detail = client.get(f"/api/otel/traces/{TRACE_HEX}").json()["spans"]
    assert len(detail) == 2
