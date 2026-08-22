"""Trace backfill, Phase 1 steps 1-2 (schema + synthesizer).

Golden-fixture tests: synthesize() over the recorded raw payload for execution
25173 is compared against the real exporter trace captured for that same run
(tests/fixtures/trace_25173_real_spans.json). Also covers the origin column
migration and the insert_spans round-trip.
Spec: docs/specs/2026-08-14-trace-backfill-from-execution-history.md.
"""

import json
from hashlib import sha256
from pathlib import Path

import pytest

from backend.modules.observability.backfill import synthesize

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW = json.loads((FIXTURES / "execution_25173_raw.json").read_text(encoding="utf-8"))
REAL = json.loads((FIXTURES / "trace_25173_real_spans.json").read_text(encoding="utf-8"))
INSTANCE = "eb570e498824919b"

NS_PER_MS = 1_000_000


def _real_root() -> dict:
    return next(r for r in REAL if r["name"] == "workflow.execute")


def _real_nodes_by_name() -> dict:
    out = {}
    for r in REAL:
        if r["name"] == "node.execute":
            out[json.loads(r["attributes_json"])["n8n.node.name"]] = r
    return out


async def _synth() -> list[dict]:
    return await synthesize(RAW, INSTANCE)


# ── Golden comparison against the real exporter trace ───────────────────────


async def test_span_count_and_names_match_real_trace():
    rows = await _synth()
    assert len(rows) == len(REAL) == 4
    assert sum(1 for r in rows if r["name"] == "workflow.execute") == 1
    assert sum(1 for r in rows if r["name"] == "node.execute") == 3
    assert all(r["kind"] == 1 for r in rows)


async def test_parent_child_edges_match_real_trace():
    rows = await _synth()
    root = next(r for r in rows if r["name"] == "workflow.execute")
    assert root["parent_id"] == ""
    for r in rows:
        if r["name"] == "node.execute":
            assert r["parent_id"] == root["span_id"]
    # Same topology as the real trace: every child hangs off the single root.
    real_root = _real_root()
    assert all(r["parent_id"] == real_root["span_id"] for r in REAL if r["name"] == "node.execute")


async def test_node_identity_matches_real_trace():
    rows = await _synth()
    real = _real_nodes_by_name()
    synth = {json.loads(r["attributes_json"])["n8n.node.name"]: r for r in rows if r["name"] == "node.execute"}
    assert set(synth) == set(real)
    for name, row in synth.items():
        got = json.loads(row["attributes_json"])
        want = json.loads(real[name]["attributes_json"])
        for key in ("n8n.node.id", "n8n.node.name", "n8n.node.type", "n8n.node.type_version"):
            assert got[key] == want[key], f"{name}: {key}"


async def test_item_counts_match_real_trace():
    rows = await _synth()
    real = _real_nodes_by_name()
    for row in rows:
        if row["name"] != "node.execute":
            continue
        got = json.loads(row["attributes_json"])
        want = json.loads(real[got["n8n.node.name"]]["attributes_json"])
        assert got["n8n.node.items.input"] == want["n8n.node.items.input"], got["n8n.node.name"]
        assert got["n8n.node.items.output"] == want["n8n.node.items.output"], got["n8n.node.name"]


async def test_node_timings_close_to_real_trace():
    """runData timing vs exporter timing: starts within 2ms, durations within
    15ms (the exporter span includes per-node engine overhead runData omits)."""
    rows = await _synth()
    real = _real_nodes_by_name()
    for row in rows:
        if row["name"] != "node.execute":
            continue
        name = json.loads(row["attributes_json"])["n8n.node.name"]
        want = real[name]
        assert abs(row["start_ns"] - want["start_ns"]) <= 2 * NS_PER_MS, name
        dur = row["end_ns"] - row["start_ns"]
        real_dur = want["end_ns"] - want["start_ns"]
        assert abs(dur - real_dur) <= 15 * NS_PER_MS, name
        # And exactly what runData recorded.
        run = RAW["data"]["resultData"]["runData"][name][0]
        assert row["start_ns"] == run["startTime"] * NS_PER_MS
        assert dur == run["executionTime"] * NS_PER_MS


async def test_root_bounds_status_and_attributes():
    rows = await _synth()
    root = next(r for r in rows if r["name"] == "workflow.execute")
    real_root = _real_root()
    # Bounds come from startedAt/stoppedAt; the exporter measured slightly differently.
    assert abs(root["start_ns"] - real_root["start_ns"]) <= 50 * NS_PER_MS
    assert abs(root["end_ns"] - real_root["end_ns"]) <= 50 * NS_PER_MS
    assert root["status"] == real_root["status"] == "OK"
    assert root["workflow_id"] == real_root["workflow_id"]
    assert root["workflow_name"] == real_root["workflow_name"]
    assert root["execution_id"] == real_root["execution_id"]
    got = json.loads(root["attributes_json"])
    want = json.loads(real_root["attributes_json"])
    for key in (
        "n8n.workflow.id", "n8n.workflow.name", "n8n.workflow.version_id",
        "n8n.workflow.node_count", "n8n.execution.id", "n8n.execution.mode",
        "n8n.execution.status", "n8n.execution.is_retry",
    ):
        assert got[key] == want[key], key
    # Child rows mirror the real trace: workflow/execution identity only on the root.
    for r in rows:
        if r["name"] == "node.execute":
            assert r["workflow_id"] == "" and r["execution_id"] == ""


# ── Determinism and id derivation ───────────────────────────────────────────


async def test_synthesize_is_deterministic():
    assert await _synth() == await _synth()


async def test_id_derivation_scheme():
    rows = await _synth()
    trace_id = sha256(f"agd-backfill:{INSTANCE}:25173".encode()).hexdigest()[:32]
    assert all(r["trace_id"] == trace_id for r in rows)
    root = next(r for r in rows if r["name"] == "workflow.execute")
    # Root key is namespaced (BUG-027): a node NAMED "workflow.execute" must not
    # collide with the root span id.
    assert root["span_id"] == sha256(f"{trace_id}:\x00agd-root:0".encode()).hexdigest()[:16]
    for r in rows:
        if r["name"] == "node.execute":
            name = json.loads(r["attributes_json"])["n8n.node.name"]
            assert r["span_id"] == sha256(f"{trace_id}:{name}:0".encode()).hexdigest()[:16]
    # The payload's tracingContext ids are ignored entirely.
    assert trace_id != RAW["tracingContext"]["traceparent"].split("-")[1]


async def test_origin_and_received_at_on_every_row():
    rows = await _synth()
    for r in rows:
        assert r["origin"] == "backfill"
        # Execution start time (spec section 5), in SQLite datetime('now') format.
        assert r["received_at"] == "2026-08-14 22:31:36"


async def test_multiple_run_indexes_each_get_a_span():
    raw = json.loads(json.dumps(RAW))
    run_data = raw["data"]["resultData"]["runData"]
    second = json.loads(json.dumps(run_data["Get Orders"][0]))
    second["startTime"] += 500
    run_data["Get Orders"].append(second)
    rows = await synthesize(raw, INSTANCE)
    assert len(rows) == 5
    get_orders = [r for r in rows if r["name"] == "node.execute"
                  and json.loads(r["attributes_json"])["n8n.node.name"] == "Get Orders"]
    assert len(get_orders) == 2
    assert get_orders[0]["span_id"] != get_orders[1]["span_id"]


# ── Schema migration and storage round-trip ─────────────────────────────────

# Base otel_spans shape before the origin column (and before cost/health ALTERs).
_OLD_OTEL_SPANS = """
    CREATE TABLE otel_spans (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id        TEXT NOT NULL,
        span_id         TEXT NOT NULL,
        parent_id       TEXT NOT NULL DEFAULT '',
        instance_id     TEXT NOT NULL DEFAULT '',
        workflow_id     TEXT NOT NULL DEFAULT '',
        workflow_name   TEXT NOT NULL DEFAULT '',
        execution_id    TEXT NOT NULL DEFAULT '',
        name            TEXT NOT NULL DEFAULT '',
        kind            INTEGER NOT NULL DEFAULT 0,
        start_ns        INTEGER NOT NULL DEFAULT 0,
        end_ns          INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT '',
        attributes_json TEXT NOT NULL DEFAULT '{}',
        received_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""


async def test_migration_adds_origin_and_old_rows_read_null(tmp_path):
    import aiosqlite

    from backend import database

    db = await aiosqlite.connect(str(tmp_path / "premigration.db"))
    db.row_factory = aiosqlite.Row
    try:
        await database._init_tables(db)
        await db.execute(_OLD_OTEL_SPANS)
        await db.execute("INSERT INTO otel_spans (trace_id, span_id) VALUES ('t-old', 's-old')")
        await db.commit()
        await database._migrate(db)
        cur = await db.execute("PRAGMA table_info(otel_spans)")
        cols = {r["name"] for r in await cur.fetchall()}
        assert "origin" in cols
        cur = await db.execute("SELECT origin FROM otel_spans WHERE trace_id = 't-old'")
        row = await cur.fetchone()
        assert row is not None and row["origin"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_insert_spans_round_trips_synthesized_rows(client):
    from backend.database import get_db
    from backend.modules.observability import storage

    rows = await _synth()
    trace_id = rows[0]["trace_id"]
    db = await get_db()
    await db.execute("DELETE FROM otel_spans WHERE trace_id = ?", (trace_id,))
    await db.commit()
    try:
        assert await storage.insert_spans(rows) >= 1
        # Deterministic ids make a re-run a no-op, not a duplicate.
        await storage.insert_spans(rows)
        cur = await db.execute("SELECT COUNT(*) AS n FROM otel_spans WHERE trace_id = ?", (trace_id,))
        assert (await cur.fetchone())["n"] == 4
        cur = await db.execute("SELECT origin, received_at FROM otel_spans WHERE trace_id = ?", (trace_id,))
        for r in await cur.fetchall():
            assert r["origin"] == "backfill"
            assert r["received_at"] == "2026-08-14 22:31:36"
        spans = await storage.get_trace(trace_id)
        assert len(spans) == 4
        assert {s["name"] for s in spans} == {"workflow.execute", "node.execute"}
        assert await storage.trace_id_for_execution("25173", INSTANCE) == trace_id
    finally:
        await db.execute("DELETE FROM otel_spans WHERE trace_id = ?", (trace_id,))
        await db.commit()


@pytest.mark.asyncio
async def test_insert_spans_without_origin_defaults_to_null(client):
    from backend.database import get_db
    from backend.modules.observability import storage

    row = {
        "trace_id": "t-no-origin", "span_id": "s-no-origin", "parent_id": "",
        "instance_id": "inst-x", "workflow_id": "", "workflow_name": "",
        "execution_id": "", "name": "workflow.execute", "kind": 1,
        "start_ns": 1, "end_ns": 2, "status": "OK", "attributes_json": "{}",
    }
    db = await get_db()
    await db.execute("DELETE FROM otel_spans WHERE trace_id = 't-no-origin'")
    await db.commit()
    try:
        assert await storage.insert_spans([row]) >= 1
        cur = await db.execute("SELECT origin, received_at FROM otel_spans WHERE trace_id = 't-no-origin'")
        r = await cur.fetchone()
        assert r["origin"] is None
        assert r["received_at"], "received_at must default to datetime('now')"
    finally:
        await db.execute("DELETE FROM otel_spans WHERE trace_id = 't-no-origin'")
        await db.commit()
