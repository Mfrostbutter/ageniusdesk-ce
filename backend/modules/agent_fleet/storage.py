"""SQLite persistence for Agent Fleet runs.

One row per run. `events` holds the full event log as JSON so a past run replays
the exact tool-call timeline (and so the polling frontend always reads the current
log). `usage_detail` holds the per-call token/cost breakdown.

This module owns its OWN database (`data/agentfleet.db`) rather than the shared
`dashboard.db`. CE's central migration deliberately DROPs `langgraph_runs` from the
shared DB ("not in CE"), so a separate file sidesteps that and keeps the module
self-contained. Pattern mirrors the notes module's index DB.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

DB_PATH = Path("data/agentfleet.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS langgraph_runs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    triage_md TEXT NOT NULL DEFAULT '',
    trace_url TEXT NOT NULL DEFAULT '',
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0,
    usage_detail TEXT NOT NULL DEFAULT '',
    events TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lgruns_created ON langgraph_runs(created_at);
"""

# Columns returned in list views (omit the big text blobs: events, triage_md, usage_detail).
_LIST_COLS = (
    "id, agent_id, target, prompt, model, status, trace_url, "
    "total_tokens, total_cost, error, created_at, updated_at"
)

_db: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


async def _get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db
    async with _lock:
        if _db is not None:
            return _db
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(DB_PATH))
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await db.commit()
        _db = db
    return _db


def new_id() -> str:
    # Full UUID4: doubles as the LangSmith run id (config["run_id"]), which must be a
    # parseable UUID, not a short hex slug.
    return str(uuid.uuid4())


async def create_run(agent_id: str, target: str, prompt: str, model: str) -> dict[str, Any]:
    db = await _get_db()
    run_id = new_id()
    await db.execute(
        "INSERT INTO langgraph_runs (id, agent_id, target, prompt, model, status) "
        "VALUES (?, ?, ?, ?, ?, 'running')",
        (run_id, agent_id, target, prompt, model),
    )
    await db.commit()
    return await get_run(run_id)


async def update_run(run_id: str, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return await get_run(run_id)
    sets = []
    vals: list[Any] = []
    for k, v in fields.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = ?")
        vals.append(v)
    sets.append("updated_at = datetime('now')")
    vals.append(run_id)
    db = await _get_db()
    await db.execute(f"UPDATE langgraph_runs SET {', '.join(sets)} WHERE id = ?", vals)
    await db.commit()
    return await get_run(run_id)


async def get_run(run_id: str) -> dict[str, Any] | None:
    db = await _get_db()
    cur = await db.execute("SELECT * FROM langgraph_runs WHERE id = ?", (run_id,))
    row = await cur.fetchone()
    await cur.close()
    return _row_to_dict(row) if row else None


async def list_runs(limit: int = 100, agent_id: str = "") -> list[dict[str, Any]]:
    db = await _get_db()
    capped = (min(max(limit, 1), 500),)
    if agent_id:
        cur = await db.execute(
            f"SELECT {_LIST_COLS} FROM langgraph_runs WHERE agent_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (agent_id, *capped),
        )
    else:
        cur = await db.execute(
            f"SELECT {_LIST_COLS} FROM langgraph_runs ORDER BY created_at DESC LIMIT ?",
            capped,
        )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def delete_run(run_id: str) -> bool:
    db = await _get_db()
    cur = await db.execute("DELETE FROM langgraph_runs WHERE id = ?", (run_id,))
    await db.commit()
    return cur.rowcount > 0


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    raw = d.get("events")
    try:
        d["events"] = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        d["events"] = []
    detail = d.get("usage_detail")
    try:
        d["usage_detail"] = json.loads(detail) if detail else {}
    except (json.JSONDecodeError, TypeError):
        d["usage_detail"] = {}
    return d
