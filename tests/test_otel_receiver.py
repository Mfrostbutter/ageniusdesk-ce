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
