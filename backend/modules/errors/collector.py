"""Error storage and WebSocket broadcast."""

import logging
from datetime import datetime, timezone
from typing import Any

from backend.config import get_active_instance_id
from backend.database import get_db
from backend.websocket import manager

logger = logging.getLogger(__name__)


async def execution_id_exists(execution_id: str, instance_id: str = "") -> bool:
    """Return True if an error with this execution_id is already stored.

    Scoped by instance_id because different n8n instances use disjoint
    execution id spaces, so the same id string can legitimately appear on
    each. Pass instance_id="" to search across all instances (legacy behavior).
    """
    if not execution_id:
        return False
    db = await get_db()
    if instance_id:
        cursor = await db.execute(
            "SELECT 1 FROM errors WHERE execution_id = ? AND instance_id = ? LIMIT 1",
            (execution_id, instance_id),
        )
    else:
        cursor = await db.execute(
            "SELECT 1 FROM errors WHERE execution_id = ? LIMIT 1", (execution_id,)
        )
    return await cursor.fetchone() is not None


async def store_error(error: dict[str, Any]) -> int:
    """Store an error in SQLite and broadcast to WebSocket clients. Returns the error ID.

    `error["instance_id"]` wins if the caller supplied it (e.g. webhook ingest
    that maps a known source instance). Otherwise we tag with the currently
    active instance so the UI can filter reliably.
    """
    occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    instance_id = error.get("instance_id") or get_active_instance_id() or ""
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO errors
           (instance_id, workflow_id, workflow_name, execution_id,
            node_name, error_message, error_type, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            error.get("workflow_id", "unknown"),
            error.get("workflow_name", "Unknown Workflow"),
            error.get("execution_id", ""),
            error.get("node_name", ""),
            error.get("error_message", "Unknown error"),
            error.get("error_type", "Error"),
            occurred_at,
        ),
    )
    await db.commit()
    error_id = cursor.lastrowid

    # Broadcast to connected clients. Include instance_id so the frontend can
    # decide whether the live event belongs to the current view.
    broadcast_data = {
        "id": error_id,
        "occurred_at": occurred_at,
        "instance_id": instance_id,
        **error,
    }
    await manager.broadcast("error", broadcast_data)
    logger.info("Error stored and broadcast: [%s/%s] %s", instance_id or "no-instance", error.get("workflow_name"), error.get("error_message", "")[:80])

    return error_id


_RANGE_SQL = {
    "24h": "-1 day",
    "7d": "-7 days",
    "30d": "-30 days",
    "90d": "-90 days",
}


def _range_modifier(range_key: str) -> str | None:
    """Map a range key to the SQLite datetime modifier. None = no time filter.

    ``"all"`` (and an empty key) resolve to None so callers count/list every
    stored error regardless of age.
    """
    if not range_key:
        return None
    return _RANGE_SQL.get(range_key)


async def get_errors(
    limit: int = 50,
    offset: int = 0,
    workflow_id: str = "",
    range_key: str = "",
    instance_id: str = "",
) -> list[dict]:
    """Fetch recent errors from SQLite, optionally bounded by a time range.

    Pass instance_id="" to list across all instances (legacy cross-instance
    view); pass a real id to scope to one instance.
    """
    db = await get_db()
    modifier = _range_modifier(range_key)

    clauses = []
    params: list = []
    if instance_id:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if workflow_id:
        clauses.append("workflow_id = ?")
        params.append(workflow_id)
    if modifier:
        clauses.append(f"occurred_at >= datetime('now', '{modifier}')")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM errors {where} ORDER BY occurred_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await db.execute(sql, tuple(params))
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_error_count_24h(instance_id: str = "") -> int:
    """Count errors in the last 24 hours, optionally scoped to one instance."""
    return await get_error_count_range("24h", instance_id)


async def get_error_count_range(range_key: str, instance_id: str = "") -> int:
    """Count errors within a named range, optionally scoped to one instance.

    A range with no time modifier ("all", or an empty key) counts every stored
    error rather than silently narrowing to 24h.
    """
    modifier = _range_modifier(range_key)
    db = await get_db()
    clauses: list[str] = []
    params: list = []
    if instance_id:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if modifier:
        clauses.append(f"occurred_at >= datetime('now', '{modifier}')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor = await db.execute(f"SELECT COUNT(*) as cnt FROM errors {where}", tuple(params))
    row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def get_errors_grouped(
    range_key: str = "",
    instance_id: str = "",
    limit: int = 100,
) -> list[dict]:
    """Aggregate errors by (workflow_id, node_name, error_type), optionally
    scoped to one instance and bounded by a time range.

    Returns one row per group with count, first/last occurrence, and the most
    recent execution_id + error_message as the representative sample. Ordered
    by last_occurred desc so the most recently failing groups float to the top.

    Uses SQLite window functions (3.25+) so we do the aggregation in a single
    query rather than N+1 correlated subqueries.
    """
    db = await get_db()
    modifier = _range_modifier(range_key)

    clauses: list[str] = []
    params: list = []
    if instance_id:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if modifier:
        clauses.append(f"occurred_at >= datetime('now', '{modifier}')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    sql = f"""
        SELECT * FROM (
            SELECT
                instance_id,
                workflow_id,
                workflow_name,
                node_name,
                error_type,
                execution_id AS last_execution_id,
                error_message AS last_error_message,
                occurred_at AS last_occurred,
                COUNT(*)  OVER w AS count,
                MIN(occurred_at) OVER w AS first_occurred,
                ROW_NUMBER() OVER (
                    PARTITION BY instance_id, workflow_id, node_name, error_type
                    ORDER BY occurred_at DESC
                ) AS rn
            FROM errors
            {where}
            WINDOW w AS (PARTITION BY instance_id, workflow_id, node_name, error_type)
        )
        WHERE rn = 1
        ORDER BY last_occurred DESC
        LIMIT ?
    """
    params.append(limit)
    cursor = await db.execute(sql, tuple(params))
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def clear_errors(before_date: str = "", workflow_id: str = "", execution_id: str = "", instance_id: str = "", node_name: str = "", error_type: str = "") -> int:
    """Delete errors with any combination of filters: date, workflow_id,
    execution_id, instance_id, node_name, error_type.

    node_name + error_type + workflow_id is the grouping key the UI uses
    when collapsing duplicates, so "Clear This Group" translates to passing
    those three plus instance_id.
    """
    db = await get_db()
    clauses, params = [], []
    if before_date:
        clauses.append("occurred_at < ?")
        params.append(before_date)
    if workflow_id:
        clauses.append("workflow_id = ?")
        params.append(workflow_id)
    if execution_id:
        clauses.append("execution_id = ?")
        params.append(execution_id)
    if instance_id:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if node_name:
        clauses.append("node_name = ?")
        params.append(node_name)
    if error_type:
        clauses.append("error_type = ?")
        params.append(error_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor = await db.execute(f"DELETE FROM errors {where}", tuple(params))
    await db.commit()
    return cursor.rowcount
