"""CRUD over the `knowledge_sources` table.

Schema is owned by backend/database.py. This module only reads/writes rows.
config_json is stored as a string and only parsed at the boundaries — callers
always see plain dicts.
"""

from __future__ import annotations

import json
from typing import Any

from backend.database import get_db


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["config"] = json.loads(d.pop("config_json", "") or "{}")
    except Exception:
        d["config"] = {}
    d["enabled"] = bool(d.get("enabled", 0))
    return d


async def list_sources(enabled_only: bool = False) -> list[dict[str, Any]]:
    db = await get_db()
    sql = "SELECT * FROM knowledge_sources"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    cur = await db.execute(sql)
    return [_row_to_dict(r) for r in await cur.fetchall()]


async def get_source(source_id: int) -> dict[str, Any] | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM knowledge_sources WHERE id = ?", (source_id,))
    row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def get_source_by_name(name: str) -> dict[str, Any] | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM knowledge_sources WHERE name = ?", (name,))
    row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def create_source(
    name: str, kind: str, description: str, config: dict[str, Any], enabled: bool = True
) -> dict[str, Any]:
    db = await get_db()
    await db.execute(
        "INSERT INTO knowledge_sources (name, kind, description, config_json, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, kind, description, json.dumps(config or {}), 1 if enabled else 0),
    )
    await db.commit()
    got = await get_source_by_name(name)
    assert got is not None
    return got


async def update_source(source_id: int, **fields) -> dict[str, Any] | None:
    allowed = {"name", "kind", "description", "config", "enabled"}
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not patch:
        return await get_source(source_id)
    sets, vals = [], []
    for k, v in patch.items():
        if k == "config":
            sets.append("config_json = ?")
            vals.append(json.dumps(v or {}))
        elif k == "enabled":
            sets.append("enabled = ?")
            vals.append(1 if v else 0)
        else:
            sets.append(f"{k} = ?")
            vals.append(v)
    sets.append("updated_at = datetime('now')")
    vals.append(source_id)
    db = await get_db()
    await db.execute(f"UPDATE knowledge_sources SET {', '.join(sets)} WHERE id = ?", vals)
    await db.commit()
    return await get_source(source_id)


async def delete_source(source_id: int) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM knowledge_sources WHERE id = ?", (source_id,))
    await db.commit()
    return (cur.rowcount or 0) > 0
