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


async def list_traces(instance_id: str, limit: int = 50, workflow_id: str = "") -> list[dict]:
    """One row per trace (execution), newest first, scoped to an instance.

    instance_id == '' means all instances. workflow_id != '' filters to traces of
    that workflow (only the root span carries workflow_id, so match via subquery).
    Workflow name/id and execution id are pulled with MAX() because only the root
    workflow span carries them; the aggregate surfaces the non-empty value.
    """
    db = await get_db()
    cur = await db.execute(
        """
        SELECT trace_id,
               MIN(start_ns)                                   AS start_ns,
               MAX(end_ns)                                     AS end_ns,
               COUNT(*)                                        AS span_count,
               MAX(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS has_error,
               MAX(CASE WHEN silent=1 THEN 1 ELSE 0 END) AS has_silent,
               MAX(workflow_name)                              AS workflow_name,
               MAX(workflow_id)                                AS workflow_id,
               MAX(execution_id)                               AS execution_id,
               MAX(instance_id)                                AS instance_id,
               COALESCE(SUM(cost_usd), 0)                      AS cost_total,
               MAX(CASE WHEN cost_source IS NOT NULL THEN 1 ELSE 0 END) AS has_cost
        FROM otel_spans
        WHERE (? = '' OR instance_id = ?)
          AND (? = '' OR trace_id IN (SELECT trace_id FROM otel_spans WHERE workflow_id = ?))
        GROUP BY trace_id
        ORDER BY start_ns DESC
        LIMIT ?
        """,
        (instance_id, instance_id, workflow_id, workflow_id, int(limit)),
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
            "has_silent": bool(r["has_silent"]),
            "start_ns": start_ns,
            "duration_ms": round((end_ns - start_ns) / 1e6, 2) if end_ns > start_ns else 0.0,
            "cost_usd": round(float(r["cost_total"]), 6) if r["cost_total"] else 0.0,
            "has_cost": bool(r["has_cost"]),
        })
    return out


async def metrics_summary(instance_id: str, window_hours: int = 24, workflow_id: str = "") -> dict:
    """Span-derived metrics strip: executions, error rate, p50/p95 latency, throughput.

    Computed from the captured spans (n8n exports traces, not OTLP metrics), per
    trace = one execution. A trace counts as errored if any of its spans is ERROR.
    Windowed by ingest time (received_at) so it matches the retention model.
    """
    db = await get_db()
    cur = await db.execute(
        """
        SELECT trace_id,
               MIN(start_ns) AS s,
               MAX(end_ns)   AS e,
               MAX(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS err,
               MAX(CASE WHEN silent=1 THEN 1 ELSE 0 END) AS silent,
               COALESCE(SUM(cost_usd), 0) AS cost
        FROM otel_spans
        WHERE (? = '' OR instance_id = ?)
          AND (? = '' OR trace_id IN (SELECT trace_id FROM otel_spans WHERE workflow_id = ?))
          AND received_at >= datetime('now', ?)
        GROUP BY trace_id
        """,
        (instance_id, instance_id, workflow_id, workflow_id, f"-{int(window_hours)} hours"),
    )
    rows = await cur.fetchall()
    durs = sorted((int(r["e"]) - int(r["s"])) / 1e6 for r in rows if int(r["e"]) > int(r["s"]))
    n = len(rows)
    errs = sum(int(r["err"]) for r in rows)
    silent = sum(int(r["silent"]) for r in rows)
    spend = sum(float(r["cost"] or 0) for r in rows)

    def pct(p: int) -> float:
        if not durs:
            return 0.0
        i = min(len(durs) - 1, int(round((p / 100) * (len(durs) - 1))))
        return round(durs[i], 1)

    return {
        "window_hours": int(window_hours),
        "executions": n,
        "errors": errs,
        "error_rate": round(errs / n, 4) if n else 0.0,
        "silent_failures": silent,
        "silent_rate": round(silent / n, 4) if n else 0.0,
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "throughput_per_hr": round(n / window_hours, 2) if window_hours else 0.0,
        "spend_usd": round(spend, 4),
    }


async def silent_execution_ids(execution_ids: list[str], instance_id: str = "") -> set[str]:
    """Of the given execution ids, which ones had a silent failure (a node span
    flagged ``silent=1`` under a green run). Scoped to ``instance_id`` when given
    so per-instance execution-id reuse (n8n ids are small integers) can't leak a
    false positive across instances. Empty input returns an empty set."""
    ids = [str(e) for e in execution_ids if e]
    if not ids:
        return set()
    # silent=1 rides the node.execute spans, which carry no execution_id (only the
    # workflow.execute span does). Bridge the two via trace_id: keep an execution
    # id whose trace contains at least one silent node span.
    placeholders = ",".join("?" * len(ids))
    q = (
        f"SELECT DISTINCT execution_id FROM otel_spans "
        f"WHERE execution_id IN ({placeholders}) "
        f"AND trace_id IN (SELECT trace_id FROM otel_spans WHERE silent = 1)"
    )
    params: list = list(ids)
    if instance_id:
        q += " AND instance_id = ?"
        params.append(instance_id)
    db = await get_db()
    cur = await db.execute(q, tuple(params))
    rows = await cur.fetchall()
    return {r["execution_id"] for r in rows if r["execution_id"]}


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
            "model": r["model"] or "",
            "tokens_in": int(r["tokens_in"]) if r["tokens_in"] is not None else None,
            "tokens_out": int(r["tokens_out"]) if r["tokens_out"] is not None else None,
            "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
            "cost_source": r["cost_source"] or "",
            "price_source": r["price_source"] or "",
            "cost_is_estimate": bool(r["cost_is_estimate"]) if r["cost_is_estimate"] is not None else None,
            "health_status": r["health_status"] or "",
            "error_type": r["error_type"] or "",
            "error_summary": r["error_summary"] or "",
            "http_status": int(r["http_status"]) if r["http_status"] is not None else None,
            "output_items": int(r["output_items"]) if r["output_items"] is not None else None,
            "attributes": attrs,
        })
    return spans


async def has_cost(trace_id: str) -> bool:
    """True if any span in the trace already has a cost source (enrichment ran)."""
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM otel_spans WHERE trace_id = ? AND cost_source IS NOT NULL LIMIT 1", (trace_id,)
    )
    return (await cur.fetchone()) is not None


async def set_costs(updates: list[dict]) -> int:
    """Write per-span cost rows. Each update: span_id + cost fields. Idempotent."""
    if not updates:
        return 0
    db = await get_db()
    await db.executemany(
        """
        UPDATE otel_spans SET
            model = :model, tokens_in = :tokens_in, tokens_out = :tokens_out,
            cost_usd = :cost_usd, cost_source = :cost_source,
            price_in_per_mtok = :price_in_per_mtok, price_out_per_mtok = :price_out_per_mtok,
            price_source = :price_source, cost_is_estimate = :cost_is_estimate,
            priced_at = :priced_at
        WHERE span_id = :span_id
        """,
        updates,
    )
    await db.commit()
    return len(updates)


async def has_health(trace_id: str) -> bool:
    """True if the trace's spans have been health-checked already (idempotency)."""
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM otel_spans WHERE trace_id = ? AND checked_at IS NOT NULL LIMIT 1", (trace_id,)
    )
    return (await cur.fetchone()) is not None


async def set_health(updates: list[dict]) -> int:
    """Write per-span silent-failure health rows. Update by span_id. Idempotent."""
    if not updates:
        return 0
    db = await get_db()
    await db.executemany(
        """
        UPDATE otel_spans SET
            health_status = :health_status, error_type = :error_type,
            error_summary = :error_summary, http_status = :http_status,
            output_items = :output_items, input_items = :input_items,
            node_id = :node_id, silent = :silent, checked_at = :checked_at
        WHERE span_id = :span_id
        """,
        updates,
    )
    await db.commit()
    return len(updates)


async def node_output_history(node_id: str, window: int, exclude_trace_id: str = "") -> list[int]:
    """Recent output-item counts for a node id (newest first), for the anomaly
    classifier. Reads enriched spans only (output_items populated on ingest);
    excludes the current trace so a run is never compared against itself.
    """
    if not node_id:
        return []
    db = await get_db()
    cur = await db.execute(
        """
        SELECT output_items FROM otel_spans
        WHERE node_id = ? AND output_items IS NOT NULL AND trace_id != ?
        ORDER BY start_ns DESC
        LIMIT ?
        """,
        (node_id, exclude_trace_id, int(window)),
    )
    return [int(r["output_items"]) for r in await cur.fetchall()]


async def node_input_history(node_id: str, window: int, exclude_trace_id: str = "") -> list[int]:
    """Recent input-item counts for a node id (newest first). Mirrors
    ``node_output_history``; used by the drop-origin rule to tell a node whose
    input itself collapsed (a downstream victim) from the node where the volume
    actually dropped (the origin, whose input stayed normal).
    """
    if not node_id:
        return []
    db = await get_db()
    cur = await db.execute(
        """
        SELECT input_items FROM otel_spans
        WHERE node_id = ? AND input_items IS NOT NULL AND trace_id != ?
        ORDER BY start_ns DESC
        LIMIT ?
        """,
        (node_id, exclude_trace_id, int(window)),
    )
    return [int(r["input_items"]) for r in await cur.fetchall()]


async def node_run_rate(
    workflow_id: str, node_name: str, window: int, exclude_trace_id: str = ""
) -> tuple[int, int]:
    """How reliably a node runs, for the dead-man's switch.

    Over the most recent ``window`` executions of ``workflow_id``, returns
    ``(ran, total)`` where ``ran`` is how many had a ``node.execute`` span for
    ``node_name``. A node that ran in nearly every recent execution but produced
    no span this run is a dead-man candidate. Matched by node NAME (unique within
    a workflow) since a missing node has no span/id to join on. Excludes the
    current trace so a run is never judged against itself.
    """
    if not workflow_id or not node_name:
        return (0, 0)
    db = await get_db()
    cur = await db.execute(
        """
        SELECT execution_id FROM otel_spans
        WHERE workflow_id = ? AND execution_id != '' AND trace_id != ?
        GROUP BY execution_id
        ORDER BY MAX(start_ns) DESC
        LIMIT ?
        """,
        (workflow_id, exclude_trace_id, int(window)),
    )
    exec_ids = [r["execution_id"] for r in await cur.fetchall()]
    total = len(exec_ids)
    if not total:
        return (0, 0)
    placeholders = ",".join("?" * total)
    cur = await db.execute(
        f"""
        SELECT COUNT(DISTINCT execution_id) AS c FROM otel_spans
        WHERE name = 'node.execute'
          AND execution_id IN ({placeholders})
          AND json_extract(attributes_json, '$."n8n.node.name"') = ?
        """,
        (*exec_ids, node_name),
    )
    row = await cur.fetchone()
    ran = int(row["c"]) if row and row["c"] is not None else 0
    return (ran, total)
