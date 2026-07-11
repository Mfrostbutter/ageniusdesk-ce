"""Dead-man's switch: a node that should have run but produced no span.

On a completed green run the detector diffs the workflow's declared nodes (from
workflowData on the run-data fetch) against the spans that landed. A declared node
that had input available (an upstream node produced output) but never ran, and
that historically runs, is flagged. Graph-aware, so a legitimate cascade skip
downstream of an empty node is not flagged.
"""

import pytest

from backend.config import settings
from backend.modules.observability import health, ingest, storage

otlp = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest  # noqa: E402
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue  # noqa: E402

# Trigger -> Fetch -> Process. Only Trigger and Fetch produce spans; Process is missing.
CONNECTIONS = {
    "Trigger": {"main": [[{"node": "Fetch", "type": "main", "index": 0}]]},
    "Fetch": {"main": [[{"node": "Process", "type": "main", "index": 0}]]},
}
NODES = [
    {"name": "Trigger", "type": "n8n-nodes-base.webhook"},
    {"name": "Fetch", "type": "n8n-nodes-base.httpRequest"},
    {"name": "Process", "type": "n8n-nodes-base.code"},
]


# ── Unit: predecessor graph ────────────────────────────────────────────────────


def test_predecessors_from_connections():
    preds = health._predecessors(CONNECTIONS)
    assert preds == {"Fetch": {"Trigger"}, "Process": {"Fetch"}}


def test_predecessors_empty_is_safe():
    assert health._predecessors({}) == {}
    assert health._predecessors(None) == {}


# ── Unit: missing-node candidates ──────────────────────────────────────────────


def _wf():
    return {"nodes": NODES, "connections": CONNECTIONS}


def test_missing_node_with_producing_feeder_is_a_candidate():
    # Process never ran; its feeder Fetch produced 5, so input was available.
    got = health._missing_candidates(_wf(), {"Trigger", "Fetch"}, {"Trigger": 1, "Fetch": 5})
    assert got == ["Process"]


def test_missing_node_with_empty_feeder_is_suppressed():
    # Fetch produced 0, so Process legitimately never ran (cascade, not a dead node).
    got = health._missing_candidates(_wf(), {"Trigger", "Fetch"}, {"Trigger": 1, "Fetch": 0})
    assert got == []


def test_only_the_origin_fires_when_two_are_missing():
    # Both Fetch and Process missing: Fetch's feeder produced output (origin), but
    # Process's feeder Fetch is itself missing (=0), so only Fetch is flagged.
    got = health._missing_candidates(_wf(), {"Trigger"}, {"Trigger": 1})
    assert got == ["Fetch"]


def test_disabled_missing_node_is_ignored():
    wf = {"nodes": [{"name": "Trigger", "type": "webhook"},
                    {"name": "Fetch", "type": "httpRequest"},
                    {"name": "Process", "type": "code", "disabled": True}],
          "connections": CONNECTIONS}
    assert health._missing_candidates(wf, {"Trigger", "Fetch"}, {"Fetch": 5}) == []


def test_missing_trigger_node_is_not_a_deadman():
    # A missing trigger/webhook is "the workflow never fired", not this diff's job.
    wf = {"nodes": [{"name": "Cron", "type": "n8n-nodes-base.scheduleTrigger"},
                    {"name": "Fetch", "type": "httpRequest"}],
          "connections": {"Cron": {"main": [[{"node": "Fetch"}]]}}}
    assert health._missing_candidates(wf, set(), {}) == []


def test_missing_source_without_predecessors_is_skipped():
    # A node with no incoming edges has no "input available" signal to judge it.
    wf = {"nodes": [{"name": "Lonely", "type": "code"}], "connections": {}}
    assert health._missing_candidates(wf, set(), {}) == []


# ── End-to-end: missing node surfaces through the errors pipeline ──────────────


def _kv(key, value):
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=str(value)))


DEADMAN_TRACE_HEX = "da" * 16      # distinct from other test files' trace ids
DEADMAN_OFF_TRACE_HEX = "db" * 16


def _deadman_request(tb: bytes = b"\xda", exec_id: str = "4004") -> ExportTraceServiceRequest:
    """Green run: Trigger + Fetch produce spans, Process (declared) produces none.

    Parameterized by trace byte + execution id so each test owns a distinct trace
    (enrichment is idempotent per trace, and error rows persist across the run).
    """
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(_kv("service.name", "n8n"))
    ss = rs.scope_spans.add()

    wf = ss.spans.add()
    wf.trace_id = tb * 16
    wf.span_id = b"\x01" * 8
    wf.name = "workflow.execute"
    wf.start_time_unix_nano = 1_000_000_000
    wf.end_time_unix_nano = 1_500_000_000
    wf.status.code = 1
    wf.attributes.append(_kv("n8n.workflow.name", "Order Pipeline"))
    wf.attributes.append(_kv("n8n.workflow.id", "wf-dead"))
    wf.attributes.append(_kv("n8n.execution.id", exec_id))
    wf.attributes.append(_kv("n8n.execution.status", "success"))

    for i, (node, nid, out) in enumerate([("Trigger", "trg", 1), ("Fetch", "fch", 5)], start=2):
        n = ss.spans.add()
        n.trace_id = tb * 16
        n.span_id = bytes([i]) * 8
        n.parent_span_id = b"\x01" * 8
        n.name = "node.execute"
        n.start_time_unix_nano = 1_000_000_000 + i
        n.end_time_unix_nano = 1_000_000_100 + i
        n.status.code = 1
        n.attributes.append(_kv("n8n.node.name", node))
        n.attributes.append(_kv("n8n.node.id", nid))
        n.attributes.append(_kv("n8n.node.items.output", out))
    return req


def _run_data():
    return {
        "data": {"resultData": {"runData": {
            "Trigger": [{"executionStatus": "success", "data": {"main": [[{"json": {"ok": 1}}]]}}],
            "Fetch": [{"executionStatus": "success", "data": {"main": [[{"json": {"id": 1}}]]}}],
            # Process is absent from runData too: it never ran.
        }}},
        "workflowData": {"nodes": NODES, "connections": CONNECTIONS},
    }


async def _insert_and_enrich(req, trace_hex):
    """Persist the trace's spans and run enrichment directly. Bypasses the OTLP
    receiver's fire-and-forget enrichment task, so the assertion is deterministic
    and no background task leaks onto the shared (session-scoped) event loop to
    perturb other tests."""
    rows = ingest.parse_request(req, {}, {})
    await storage.insert_spans(rows)
    await health.enrich_trace_health(trace_hex)


async def _silent_errors_for(execution_id: str) -> list[dict]:
    db = await storage.get_db()
    cur = await db.execute(
        "SELECT node_name, error_message FROM errors "
        "WHERE execution_id = ? AND error_type = 'Silent failure'",
        (execution_id,),
    )
    return [{"node_name": r["node_name"], "error_message": r["error_message"]} for r in await cur.fetchall()]


async def test_deadman_missing_node_surfaces_in_errors(client, monkeypatch):
    async def fake_raw(execution_id):
        return _run_data() if execution_id == "4004" else {}
    monkeypatch.setattr(health.n8n_client, "get_execution_raw", fake_raw)

    async def fake_run_rate(workflow_id, node_name, window, exclude_trace_id=""):
        # Process is a steady runner historically (ran in 30/30 recent executions).
        return (30, 30) if node_name == "Process" else (0, 0)
    monkeypatch.setattr(health.storage, "node_run_rate", fake_run_rate)

    await _insert_and_enrich(_deadman_request(b"\xda", "4004"), DEADMAN_TRACE_HEX)

    rows = await _silent_errors_for("4004")
    dead = next((e for e in rows if e["node_name"] == "Process"), None)
    assert dead is not None, "missing node should surface as a silent failure"
    assert "30/30" in (dead["error_message"] or "")
    # Fetch ran and produced output; it must not be flagged.
    assert all(e["node_name"] != "Fetch" for e in rows)


async def test_deadman_off_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "agd_health_deadman_enabled", False)

    async def fake_raw(execution_id):
        return _run_data() if execution_id == "4003" else {}
    monkeypatch.setattr(health.n8n_client, "get_execution_raw", fake_raw)

    called = {"n": 0}

    async def fake_run_rate(workflow_id, node_name, window, exclude_trace_id=""):
        called["n"] += 1
        return (30, 30)
    monkeypatch.setattr(health.storage, "node_run_rate", fake_run_rate)

    await _insert_and_enrich(_deadman_request(b"\xdb", "4003"), DEADMAN_OFF_TRACE_HEX)

    rows = await _silent_errors_for("4003")
    assert all(e["node_name"] != "Process" for e in rows)
    assert called["n"] == 0  # detector short-circuits before the history query
