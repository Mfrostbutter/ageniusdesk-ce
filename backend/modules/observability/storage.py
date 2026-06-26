"""SQLite persistence for OTLP spans.

High-volume table: every n8n execution produces a workflow span plus one span
per node. Writes dedupe on (trace_id, span_id) and prune by age + row cap so the
embedded store stays bounded. Real retention is the external-stack path.
"""

import json
import logging

from backend.database import get_db

logger = logging.getLogger(__name__)

_INSERT = """
    INSERT OR IGNORE INTO otel_spans
        (trace_id, span_id, parent_id, instance_id, workflow_id, workflow_name,
         execution_id, name, kind, start_ns, end_ns, status, attributes_json)
    VALUES (:trace_id, :span_id, :parent_id, :instance_id, :workflow_id, :workflow_name,
            :execution_id, :name, :kind, :start_ns, :end_ns, :status, :attributes_json)
"""


async def insert_spans(rows: list[dict]) -> int:
    """Insert span rows, ignoring duplicates. Returns rows actually inserted."""
    if not rows:
        return 0
    db = await get_db()
    cur = await db.executemany(_INSERT, rows)
    await db.commit()
    # rowcount on executemany is driver-dependent; report best-effort.
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)


async def prune(retention_hours: int, max_spans: int) -> None:
    """Enforce both retention bounds: age first, then the hard row cap."""
    db = await get_db()
    if retention_hours and retention_hours > 0:
        await db.execute(
            "DELETE FROM otel_spans WHERE received_at < datetime('now', ?)",
            (f"-{int(retention_hours)} hours",),
        )
    if max_spans and max_spans > 0:
        await db.execute(
            "DELETE FROM otel_spans WHERE id NOT IN "
            "(SELECT id FROM otel_spans ORDER BY id DESC LIMIT ?)",
            (int(max_spans),),
        )
    await db.commit()


async def count_spans() -> int:
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) AS n FROM otel_spans")
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def list_traces(instance_id: str, limit: int = 50) -> list[dict]:
    """One row per trace (execution), newest first, scoped to an instance.

    instance_id == '' means all instances. Workflow name/id and execution id are
    pulled with MAX() because only the root workflow span carries them; the
    aggregate surfaces the non-empty value across the trace's spans.
    """
    db = await get_db()
    cur = await db.execute(
        """
        SELECT trace_id,
               MIN(start_ns)                                   AS start_ns,
               MAX(end_ns)                                     AS end_ns,
               COUNT(*)                                        AS span_count,
               MAX(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS has_error,
               MAX(workflow_name)                              AS workflow_name,
               MAX(workflow_id)                                AS workflow_id,
               MAX(execution_id)                               AS execution_id,
               MAX(instance_id)                                AS instance_id
        FROM otel_spans
        WHERE (? = '' OR instance_id = ?)
        GROUP BY trace_id
        ORDER BY start_ns DESC
        LIMIT ?
        """,
        (instance_id, instance_id, int(limit)),
    )
    out = []
    for r in await cur.fetchall():
        start_ns = int(r["start_ns"] or 0)
        end_ns = int(r["end_ns"] or 0)
        out.append({
            "trace_id": r["trace_id"],
            "workflow_name": r["workflow_name"] or "",
            "workflow_id": r["workflow_id"] or "",
            "execution_id": r["execution_id"] or "",
            "instance_id": r["instance_id"] or "",
            "span_count": int(r["span_count"] or 0),
            "has_error": bool(r["has_error"]),
            "start_ns": start_ns,
            "duration_ms": round((end_ns - start_ns) / 1e6, 2) if end_ns > start_ns else 0.0,
        })
    return out


async def trace_id_for_execution(execution_id: str) -> str:
    """Most recent trace id for an n8n execution id, or '' if none captured."""
    db = await get_db()
    cur = await db.execute(
        "SELECT trace_id FROM otel_spans WHERE execution_id = ? ORDER BY start_ns DESC LIMIT 1",
        (execution_id,),
    )
    row = await cur.fetchone()
    return row["trace_id"] if row else ""


async def get_trace(trace_id: str) -> list[dict]:
    """All spans for one trace, ordered for waterfall rendering."""
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM otel_spans WHERE trace_id = ? ORDER BY start_ns ASC, id ASC",
        (trace_id,),
    )
    spans = []
    for r in await cur.fetchall():
        try:
            attrs = json.loads(r["attributes_json"] or "{}")
        except Exception:
            attrs = {}
        spans.append({
            "trace_id": r["trace_id"],
            "span_id": r["span_id"],
            "parent_id": r["parent_id"] or "",
            "instance_id": r["instance_id"] or "",
            "workflow_id": r["workflow_id"] or "",
            "workflow_name": r["workflow_name"] or "",
            "execution_id": r["execution_id"] or "",
            "name": r["name"] or "",
            "kind": int(r["kind"] or 0),
            "start_ns": int(r["start_ns"] or 0),
            "end_ns": int(r["end_ns"] or 0),
            "duration_ms": round((int(r["end_ns"] or 0) - int(r["start_ns"] or 0)) / 1e6, 3),
            "status": r["status"] or "",
            "attributes": attrs,
        })
    return spans
