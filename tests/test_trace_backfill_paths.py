"""Trace backfill, Phase 1 steps 3-5 (single path, range path, ingest precedence).

Client calls are monkeypatched stubs; the golden fixture for execution 25173
serves as the canonical raw payload. Spec:
docs/specs/2026-08-14-trace-backfill-from-execution-history.md.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.config import settings
from backend.modules.n8n_proxy import client as n8n_client
from backend.modules.observability import backfill, cost, health, ingest, storage

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW = json.loads((FIXTURES / "execution_25173_raw.json").read_text(encoding="utf-8"))
INSTANCE = "eb570e498824919b"
INST_DICT = {"id": INSTANCE, "name": "Test", "url": "http://n8n.test:5678", "api_key": "k"}

_UTC_FMT = "%Y-%m-%d %H:%M:%S"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _mk_raw(exec_id: str, started: datetime, out_items: int = 1, status: str = "success") -> dict:
    """Minimal raw execution payload: trigger feeding one producer node."""
    stopped = started + timedelta(seconds=1)
    start_ms = int(started.timestamp() * 1000)
    trig_items = [{"json": {"i": n}} for n in range(3)]
    return {
        "id": exec_id,
        "status": status,
        "mode": "trigger",
        "startedAt": _iso(started),
        "stoppedAt": _iso(stopped),
        "workflowId": "wf-bf",
        "workflowVersionId": "v1",
        "workflowData": {
            "id": "wf-bf",
            "name": "Backfill WF",
            "nodes": [
                {"id": "bf-node-trigger", "name": "Trigger", "type": "n8n-nodes-base.webhook", "typeVersion": 2},
                {"id": "bf-node-producer", "name": "Producer", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4},
            ],
        },
        "data": {"resultData": {"runData": {
            "Trigger": [{
                "startTime": start_ms, "executionTime": 5, "executionStatus": "success",
                "source": [], "data": {"main": [trig_items]},
            }],
            "Producer": [{
                "startTime": start_ms + 10, "executionTime": 200, "executionStatus": "success",
                "source": [{"previousNode": "Trigger", "previousNodeOutput": 0, "previousNodeRun": 0}],
                "data": {"main": [[{"json": {"n": i}} for i in range(out_items)]]},
            }],
        }}},
    }


async def _purge(*trace_ids: str) -> None:
    from backend.database import get_db

    db = await get_db()
    for tid in trace_ids:
        await db.execute("DELETE FROM otel_spans WHERE trace_id = ?", (tid,))
    await db.commit()


async def _trace_count(trace_id: str) -> int:
    from backend.database import get_db

    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) AS n FROM otel_spans WHERE trace_id = ?", (trace_id,))
    return (await cur.fetchone())["n"]


@pytest.fixture
def no_enrich(monkeypatch):
    """Neutralize the enrichers where a test only exercises the span path."""
    async def _noop(trace_id):
        return 0

    monkeypatch.setattr(cost, "enrich_trace", _noop)
    monkeypatch.setattr(health, "enrich_trace_health", _noop)


def _stub_fetch(monkeypatch, payloads: dict, calls: list | None = None):
    """Route get_execution_raw_by_instance to canned payloads."""
    async def fake(execution_id, instance_id):
        if calls is not None:
            calls.append((execution_id, instance_id))
        return payloads.get(str(execution_id), {})

    monkeypatch.setattr(n8n_client, "get_execution_raw_by_instance", fake)


# ── Step 3: single-execution path ───────────────────────────────────────────


async def test_backfill_execution_idempotent(client, monkeypatch, no_enrich):
    calls: list = []
    _stub_fetch(monkeypatch, {"25173": RAW}, calls)
    trace_id = backfill._trace_id(INSTANCE, "25173")
    await _purge(trace_id)
    try:
        n1 = await backfill.backfill_execution("25173", INSTANCE)
        assert n1 == 4
        assert await _trace_count(trace_id) == 4
        # Second run over an existing backfill re-synthesizes; deterministic ids
        # make it a no-op, never a duplicate.
        n2 = await backfill.backfill_execution("25173", INSTANCE)
        assert n2 == 0
        assert await _trace_count(trace_id) == 4
        assert len(calls) == 2  # fetched both times: a backfill never blocks a re-backfill
    finally:
        await _purge(trace_id)


async def test_backfill_skipped_when_real_trace_exists(client, monkeypatch, no_enrich):
    async def explode(execution_id, instance_id):
        raise AssertionError("must not fetch when a real trace exists")

    monkeypatch.setattr(n8n_client, "get_execution_raw_by_instance", explode)
    real_trace = "aa" * 16
    await _purge(real_trace)
    await storage.insert_spans([{
        "trace_id": real_trace, "span_id": "bb" * 8, "parent_id": "",
        "instance_id": INSTANCE, "workflow_id": "wf-real", "workflow_name": "Real",
        "execution_id": "91001", "name": "workflow.execute", "kind": 1,
        "start_ns": 1, "end_ns": 2, "status": "OK", "attributes_json": "{}",
    }])
    try:
        assert await backfill.backfill_execution("91001", INSTANCE) == 0
        assert await _trace_count(backfill._trace_id(INSTANCE, "91001")) == 0
    finally:
        await _purge(real_trace)


async def test_backfill_empty_payload_returns_zero(client, monkeypatch, no_enrich):
    _stub_fetch(monkeypatch, {})
    assert await backfill.backfill_execution("91002", INSTANCE) == 0
    # Saved-run-data absent (saveDataSuccessExecution=none) is equally unrecoverable.
    _stub_fetch(monkeypatch, {"91003": {"id": "91003", "startedAt": _iso(datetime.now(timezone.utc))}})
    assert await backfill.backfill_execution("91003", INSTANCE) == 0


async def test_detect_health_false_skips_health_but_not_cost(client, monkeypatch):
    cost_calls, health_calls = [], []

    async def fake_cost(trace_id):
        cost_calls.append(trace_id)
        return 0

    async def fake_health(trace_id):
        health_calls.append(trace_id)
        return 0

    monkeypatch.setattr(cost, "enrich_trace", fake_cost)
    monkeypatch.setattr(health, "enrich_trace_health", fake_health)
    raw = _mk_raw("91004", datetime.now(timezone.utc) - timedelta(hours=1))
    _stub_fetch(monkeypatch, {"91004": raw})
    trace_id = backfill._trace_id(INSTANCE, "91004")
    await _purge(trace_id)
    try:
        assert await backfill.backfill_execution("91004", INSTANCE, detect_health=False) == 3
        assert cost_calls == [trace_id]
        assert health_calls == []
        assert await backfill.backfill_execution("91004", INSTANCE, detect_health=True) == 0
        assert health_calls == [trace_id]
    finally:
        await _purge(trace_id)


# ── Decision 1 + detector compatibility ─────────────────────────────────────


async def test_synthesized_trace_trips_silent_failure_like_a_received_one(client, monkeypatch):
    """A zero-output steady producer fires identically whether the trace was
    received over OTLP or synthesized, and the backfilled incident carries the
    EXECUTION's time, not ingest time (Decision 1 mitigation)."""
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
    raw = _mk_raw("91005", started, out_items=0)
    _stub_fetch(monkeypatch, {"91005": raw})

    steady = [5] * (settings.agd_health_min_samples + 10)

    async def fake_out_history(node_id, window, exclude_trace_id=""):
        return steady if node_id == "bf-node-producer" else []

    async def fake_in_history(node_id, window, exclude_trace_id=""):
        return steady if node_id == "bf-node-producer" else []

    monkeypatch.setattr(health.storage, "node_output_history", fake_out_history)
    monkeypatch.setattr(health.storage, "node_input_history", fake_in_history)
    monkeypatch.setattr(settings, "agd_health_deadman_enabled", False)

    bf_trace = backfill._trace_id(INSTANCE, "91005")
    rx_trace = "cd" * 16
    await _purge(bf_trace, rx_trace)
    from backend.modules.errors import collector as errors_collector
    try:
        assert await backfill.backfill_execution("91005", INSTANCE) == 3

        # Received twin: identical rows minus origin/received_at, real-style ids.
        rows = await backfill.synthesize(raw, INSTANCE)
        received = []
        for i, r in enumerate(rows):
            rr = {k: v for k, v in r.items() if k not in ("origin", "received_at")}
            rr["trace_id"] = rx_trace
            rr["span_id"] = f"{i:016x}"
            rr["parent_id"] = "0" * 16 if r["parent_id"] else ""
            received.append(rr)
        # Route the twin's run-data fetch at its own execution id.
        _stub_fetch(monkeypatch, {"91005": raw, "91005-rx": raw})
        await storage.insert_spans(received)
        await health.enrich_trace_health(rx_trace)

        def _health_by_node(spans):
            return {
                (s.get("attributes") or {}).get("n8n.node.name"): (s["health_status"], s.get("error_type"))
                for s in spans if s["name"] == "node.execute"
            }

        bf_spans = await storage.get_trace(bf_trace)
        rx_spans = await storage.get_trace(rx_trace)
        assert _health_by_node(bf_spans) == _health_by_node(rx_spans)
        producer = next(
            s for s in bf_spans
            if (s.get("attributes") or {}).get("n8n.node.name") == "Producer"
        )
        assert producer["health_status"] == "LOW"
        assert producer["error_type"] == "empty_output"

        # Incident row carries execution time, so it sorts into history.
        errs = [e for e in await errors_collector.get_errors(limit=50) if e["execution_id"] == "91005"]
        assert errs, "silent failure must land in the errors table"
        assert all(e["error_type"] == health.SILENT_ERROR_TYPE for e in errs)
        expected = started.strftime(_UTC_FMT)
        assert all(e["occurred_at"] == expected for e in errs)
    finally:
        await _purge(bf_trace, rx_trace)
        await errors_collector.clear_errors(execution_id="91005")


# ── Step 4: range path ──────────────────────────────────────────────────────


def _exec_row(exec_id: str, started: datetime, status: str = "success") -> dict:
    return {"id": exec_id, "status": status, "startedAt": _iso(started), "stoppedAt": _iso(started)}


async def test_backfill_instance_summary_counts(client, monkeypatch, no_enrich):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=settings.agd_otel_retention_hours + 10)
    listing = [
        _exec_row("92001", now - timedelta(hours=1)),                    # backfilled
        _exec_row("92002", now - timedelta(hours=1)),                    # skipped: real trace
        _exec_row("92003", now - timedelta(minutes=30), status="running"),  # not completed: ignored
        _exec_row("92004", old),                                         # outside retention: reported
        _exec_row("92005", now - timedelta(hours=2)),                    # no saved data
    ]

    async def fake_page(inst, cursor="", limit=100):
        return (listing, "")

    monkeypatch.setattr(backfill, "_list_executions_page", fake_page)
    monkeypatch.setattr(backfill, "get_instance_by_id", lambda iid: INST_DICT if iid == INSTANCE else None)
    _stub_fetch(monkeypatch, {"92001": _mk_raw("92001", now - timedelta(hours=1))})

    real_trace = "ee" * 16
    bf_trace = backfill._trace_id(INSTANCE, "92001")
    await _purge(real_trace, bf_trace)
    await storage.insert_spans([{
        "trace_id": real_trace, "span_id": "ff" * 8, "parent_id": "",
        "instance_id": INSTANCE, "workflow_id": "wf-real", "workflow_name": "Real",
        "execution_id": "92002", "name": "workflow.execute", "kind": 1,
        "start_ns": 1, "end_ns": 2, "status": "OK", "attributes_json": "{}",
    }])
    try:
        summary = await backfill.backfill_instance(INSTANCE)
        assert summary == {
            "scanned": 4, "backfilled": 1, "spans": 3, "skipped_traced": 1,
            "outside_retention": 1, "no_data": 1, "errors": 0,
        }
        assert await _trace_count(bf_trace) == 3
    finally:
        await _purge(real_trace, bf_trace)


async def test_backfill_instance_since_until_window(client, monkeypatch, no_enrich):
    now = datetime.now(timezone.utc)
    listing = [
        _exec_row("92101", now - timedelta(hours=1)),   # after until: out of range
        _exec_row("92102", now - timedelta(hours=3)),   # inside window
        _exec_row("92103", now - timedelta(hours=9)),   # before since: stops the walk
    ]

    async def fake_page(inst, cursor="", limit=100):
        return (listing, "next")  # cursor present; the since stop must still end the walk

    monkeypatch.setattr(backfill, "_list_executions_page", fake_page)
    monkeypatch.setattr(backfill, "get_instance_by_id", lambda iid: INST_DICT)
    _stub_fetch(monkeypatch, {"92102": _mk_raw("92102", now - timedelta(hours=3))})

    bf_trace = backfill._trace_id(INSTANCE, "92102")
    await _purge(bf_trace)
    try:
        summary = await backfill.backfill_instance(
            INSTANCE, since=_iso(now - timedelta(hours=6)), until=_iso(now - timedelta(hours=2))
        )
        assert summary["scanned"] == 1
        assert summary["backfilled"] == 1
        assert summary["spans"] == 3
    finally:
        await _purge(bf_trace)


async def test_backfill_instance_caps_and_semaphore(client, monkeypatch, no_enrich):
    now = datetime.now(timezone.utc)
    listing = [_exec_row(f"93{i:03d}", now - timedelta(minutes=i + 1)) for i in range(20)]

    async def fake_page(inst, cursor="", limit=100):
        return (listing, "")

    monkeypatch.setattr(backfill, "_list_executions_page", fake_page)
    monkeypatch.setattr(backfill, "get_instance_by_id", lambda iid: INST_DICT)
    monkeypatch.setattr(settings, "agd_backfill_concurrency", 2)
    monkeypatch.setattr(settings, "agd_backfill_max_executions", 500)

    active = 0
    max_active = 0
    fetched: list[str] = []

    async def slow_fetch(execution_id, instance_id):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)  # stubbed slow instance
        active -= 1
        fetched.append(str(execution_id))
        return _mk_raw(str(execution_id), now - timedelta(minutes=5))

    monkeypatch.setattr(n8n_client, "get_execution_raw_by_instance", slow_fetch)

    traces = [backfill._trace_id(INSTANCE, e["id"]) for e in listing]
    await _purge(*traces)
    try:
        summary = await backfill.backfill_instance(INSTANCE, limit=5)
        assert summary["scanned"] == 5  # the call's limit lowers the cap
        assert summary["backfilled"] == 5
        assert len(fetched) == 5
        assert max_active <= 2  # semaphore bound held against the slow instance

        # The env cap is a ceiling the limit arg can never raise.
        monkeypatch.setattr(settings, "agd_backfill_max_executions", 3)
        await _purge(*traces)
        fetched.clear()
        summary = await backfill.backfill_instance(INSTANCE, limit=50)
        assert summary["scanned"] == 3
        assert len(fetched) == 3
    finally:
        await _purge(*traces)


async def test_backfill_instance_unknown_instance(client):
    summary = await backfill.backfill_instance("no-such-instance")
    assert summary["errors"] == 1
    assert summary["scanned"] == 0


# ── Step 5: ingest precedence ───────────────────────────────────────────────

otlp = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest  # noqa: E402
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue  # noqa: E402


def _kv(key: str, value) -> KeyValue:
    if isinstance(value, bool):
        return KeyValue(key=key, value=AnyValue(bool_value=value))
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=str(value)))


def _real_request(exec_id: str, trace_bytes: bytes) -> ExportTraceServiceRequest:
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(_kv("service.name", "n8n"))
    ss = rs.scope_spans.add()
    wf = ss.spans.add()
    wf.trace_id = trace_bytes
    wf.span_id = b"\x01" * 8
    wf.name = "workflow.execute"
    wf.start_time_unix_nano = 2_000_000_000
    wf.end_time_unix_nano = 2_500_000_000
    wf.status.code = 1
    wf.attributes.append(_kv("n8n.workflow.id", "wf-bf"))
    wf.attributes.append(_kv("n8n.execution.id", exec_id))
    wf.attributes.append(_kv("n8n.execution.status", "success"))
    node = ss.spans.add()
    node.trace_id = trace_bytes
    node.span_id = b"\x02" * 8
    node.parent_span_id = b"\x01" * 8
    node.name = "node.execute"
    node.start_time_unix_nano = 2_100_000_000
    node.end_time_unix_nano = 2_200_000_000
    node.status.code = 1
    node.attributes.append(_kv("n8n.node.name", "Producer"))
    return req


async def test_late_real_trace_replaces_backfill(client, monkeypatch, no_enrich):
    exec_id = "94001"
    raw = _mk_raw(exec_id, datetime.now(timezone.utc) - timedelta(hours=1))
    _stub_fetch(monkeypatch, {exec_id: raw})
    monkeypatch.setattr(ingest, "_map_instance", lambda attrs, pins, n2i: INSTANCE)

    bf_trace = backfill._trace_id(INSTANCE, exec_id)
    real_trace = (b"\x9a" * 16).hex()
    await _purge(bf_trace, real_trace)
    try:
        assert await backfill.backfill_execution(exec_id, INSTANCE) == 3
        assert await _trace_count(bf_trace) == 3

        inserted = await ingest.ingest_trace_request(_real_request(exec_id, b"\x9a" * 16))
        await asyncio.sleep(0)  # let fire-and-forget enrich stubs settle
        assert inserted == 2
        # Backfilled spans gone, root and children both (children carry no execution_id).
        assert await _trace_count(bf_trace) == 0
        assert await _trace_count(real_trace) == 2
        assert await storage.trace_id_for_execution(exec_id) == real_trace
        assert await storage.trace_has_real_spans(real_trace) is True
    finally:
        await _purge(bf_trace, real_trace)


async def test_delete_backfill_spans_is_noop_without_backfill(client):
    real_trace = "77" * 16
    await _purge(real_trace)
    await storage.insert_spans([{
        "trace_id": real_trace, "span_id": "88" * 8, "parent_id": "",
        "instance_id": INSTANCE, "workflow_id": "wf-x", "workflow_name": "X",
        "execution_id": "94002", "name": "workflow.execute", "kind": 1,
        "start_ns": 1, "end_ns": 2, "status": "OK", "attributes_json": "{}",
    }])
    try:
        assert await storage.delete_backfill_spans(INSTANCE, "94002") == 0
        assert await _trace_count(real_trace) == 1
    finally:
        await _purge(real_trace)
