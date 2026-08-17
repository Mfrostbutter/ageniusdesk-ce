"""Phase 4 P3 observability regressions (bug-hunt remediation 2026-08-16).

BUG-022 learn-side backfill-precedence delete, BUG-023 TOCTOU re-check,
BUG-027 root-span-id collision, BUG-028 missing startedAt, BUG-024 origin
surfaced by get_trace, BUG-030 retention default.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.modules.observability import backfill, instance_map, storage


def _mk_raw(exec_id: str, node_name: str = "Producer") -> dict:
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "id": exec_id,
        "status": "success",
        "startedAt": now.isoformat(),
        "stoppedAt": (now + timedelta(seconds=2)).isoformat(),
        "workflowData": {"id": "wf-x", "name": "WF X", "nodes": [{"name": node_name, "id": "n1", "type": "t"}]},
        "data": {"resultData": {"runData": {node_name: [{
            "startTime": int(now.timestamp() * 1000), "executionTime": 50,
            "executionStatus": "success", "data": {"main": [[{"json": {}}]]}, "source": [],
        }]}}},
    }


def _real_row(trace_id: str, span_id: str, instance_id: str, exec_id: str) -> dict:
    return {
        "trace_id": trace_id, "span_id": span_id, "parent_id": "",
        "instance_id": instance_id, "workflow_id": "wf-x", "workflow_name": "WF X",
        "execution_id": exec_id, "name": "workflow.execute", "kind": 1,
        "start_ns": 10, "end_ns": 20, "status": "OK", "attributes_json": "{}",
    }


async def _purge(*trace_ids: str) -> None:
    from backend.database import get_db

    db = await get_db()
    for tid in trace_ids:
        await db.execute("DELETE FROM otel_spans WHERE trace_id = ?", (tid,))
    await db.commit()


# ── BUG-027 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_named_workflow_execute_gets_distinct_span_id():
    rows = await backfill.synthesize(_mk_raw("95001", node_name="workflow.execute"), "inst-a")
    ids = [r["span_id"] for r in rows]
    assert len(ids) == len(set(ids)), "root span id collided with a node named workflow.execute"


# ── BUG-028 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_startedat_is_an_error_not_garbage_rows(client, monkeypatch):
    from backend.modules.n8n_proxy import client as n8n_client

    raw = _mk_raw("95002")
    raw["startedAt"] = ""

    async def fake_fetch(execution_id, instance_id):
        return raw

    monkeypatch.setattr(n8n_client, "get_execution_raw_by_instance", fake_fetch)
    outcome, n = await backfill._backfill_one("95002", "inst-a", detect_health=False)
    assert (outcome, n) == ("error", 0)
    assert await storage.trace_id_for_execution("95002", "inst-a") == ""


# ── BUG-023 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_trace_landing_mid_fetch_wins(client, monkeypatch):
    """A real trace inserted between the precedence check and the insert must
    still win: the re-check discards the synthesized batch."""
    from backend.modules.n8n_proxy import client as n8n_client

    exec_id = "95003"
    real_trace = "95" * 16
    bf_trace = backfill._trace_id("inst-a", exec_id)
    await _purge(real_trace, bf_trace)

    async def fetch_and_race(execution_id, instance_id):
        # The real exporter batch lands while backfill is mid-fetch.
        await storage.insert_spans([_real_row(real_trace, "aa" * 8, "inst-a", exec_id)])
        return _mk_raw(exec_id)

    monkeypatch.setattr(n8n_client, "get_execution_raw_by_instance", fetch_and_race)
    try:
        outcome, n = await backfill._backfill_one(exec_id, "inst-a", detect_health=False)
        assert (outcome, n) == ("skipped_traced", 0)
        assert not await storage.get_trace(bf_trace)
    finally:
        await _purge(real_trace, bf_trace)


# ── BUG-022 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_learn_unknowns_deletes_backfill_twin(client, monkeypatch):
    """When an unknown-<hash> batch is re-attributed, the backfill trace stored
    under the REAL instance id (which the ingest-time delete missed) goes too."""
    rhash = "p4hash22"
    exec_id = "95004"
    real_trace, span = "94" * 16, "cc" * 8
    bf_rows = await backfill.synthesize(_mk_raw(exec_id), "inst-a")
    bf_trace = bf_rows[0]["trace_id"]
    await _purge(real_trace, bf_trace)
    await storage.insert_spans(bf_rows)
    await storage.insert_spans([_real_row(real_trace, span, f"unknown-{rhash}", exec_id)])

    async def fake_load_pins():
        return {}

    async def fake_resolve(rh, eid, wid):
        return "inst-a"

    async def fake_pin(rh, iid, source):
        return None

    monkeypatch.setattr(instance_map, "load_pins", fake_load_pins)
    monkeypatch.setattr(instance_map, "resolve_hash", fake_resolve)
    monkeypatch.setattr(instance_map, "pin", fake_pin)
    try:
        assert await instance_map.learn_unknowns({rhash: (exec_id, "wf-x")}) == 1
        spans = await storage.get_trace(real_trace)
        assert spans and spans[0]["instance_id"] == "inst-a"  # re-attributed
        assert not await storage.get_trace(bf_trace), "backfill twin must be deleted on learn"
    finally:
        await _purge(real_trace, bf_trace)


# ── BUG-024 / BUG-030 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_trace_exposes_origin(client):
    rows = await backfill.synthesize(_mk_raw("95005"), "inst-a")
    tid = rows[0]["trace_id"]
    await _purge(tid)
    try:
        await storage.insert_spans(rows)
        spans = await storage.get_trace(tid)
        assert spans and all(s["origin"] == "backfill" for s in spans)
    finally:
        await _purge(tid)


def test_retention_default_is_spec_168():
    from backend.config import Settings

    assert Settings.model_fields["agd_otel_retention_hours"].default == 168
