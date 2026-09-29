"""SQLite persistence for LLM Cost: sources, snapshots, pushes, devices, history, settings.

Lives in the shared dashboard.db; the module owns its schema (idempotent, applied once per process).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Optional, Sequence

from backend.database import get_db as _host_db
from backend.modules.llm_cost.models import HEALTH_OK, Snapshot

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cost_sources (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    interval_sec INTEGER NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS llm_cost_snapshots (
    source_id TEXT PRIMARY KEY,
    current_json TEXT NOT NULL,
    last_good_json TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    last_duration_ms INTEGER,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cost_push (
    device_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (device_id, source_id)
);
CREATE TABLE IF NOT EXISTS llm_cost_devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at INTEGER,
    last_host TEXT NOT NULL DEFAULT '',
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_cost_quota_samples (
    source_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    bucket_ts INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    pct REAL,
    used REAL,
    lim REAL,
    unit TEXT NOT NULL,
    resets_at INTEGER,
    PRIMARY KEY (source_id, scope, label, bucket_ts)
);
CREATE INDEX IF NOT EXISTS idx_llm_cost_quota_ts ON llm_cost_quota_samples (bucket_ts);
CREATE TABLE IF NOT EXISTS llm_cost_spend_samples (
    source_id TEXT NOT NULL,
    spend_window TEXT NOT NULL,
    bucket_ts INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    estimated BOOLEAN NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, spend_window, bucket_ts)
);
CREATE INDEX IF NOT EXISTS idx_llm_cost_spend_ts ON llm_cost_spend_samples (bucket_ts);
CREATE TABLE IF NOT EXISTS llm_cost_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    at INTEGER NOT NULL,
    health TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_cost_events_at ON llm_cost_events (at DESC);
CREATE TABLE IF NOT EXISTS llm_cost_alert_state (
    alert_key TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    since INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cost_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_schema_ready_for: Any = None


async def ensure_schema(db) -> None:
    """Create the llm_cost_* tables once per connection."""
    global _schema_ready_for
    if _schema_ready_for is db:
        return
    await db.executescript(_SCHEMA)
    await db.commit()
    _schema_ready_for = db


async def get_db():
    db = await _host_db()
    await ensure_schema(db)
    return db


async def _fetch(db, sql: str, params: Sequence[Any] = ()) -> list:
    async with db.execute(sql, tuple(params)) as cur:
        return await cur.fetchall()


async def _write(db, sql: str, params: Sequence[Any] = ()) -> int:
    cur = await db.execute(sql, tuple(params))
    n = cur.rowcount
    await cur.close()
    await db.commit()
    return n

TOKEN_PREFIX = "agdlc_"

DEFAULT_SETTINGS: dict[str, Any] = {
    "quota_warn_pct": 75.0,
    "quota_critical_pct": 90.0,
    "spend_daily_warn": 0.0,
    "stale_after_sec": 1800,
    "push_stale_sec": 180,
    "notify": True,
    "mqtt": {
        "enabled": False, "host": "", "port": 1883, "username_ref": "", "password_ref": "",
        "base_topic": "agd_llm_cost", "discovery_prefix": "homeassistant", "publish_interval_sec": 60,
    },
}


def _now() -> int:
    return int(time.time())


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ── sources ────────────────────────────────────────────────────────────────


def _source_row(r) -> dict:
    return {
        "id": r["id"], "type": r["type"], "display_name": r["display_name"], "enabled": bool(r["enabled"]),
        "interval_sec": int(r["interval_sec"] or 0), "options": _loads(r["options_json"], {}),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


async def list_sources() -> list[dict]:
    db = await get_db()
    rows = await _fetch(db, "SELECT * FROM llm_cost_sources ORDER BY created_at, id")
    return [_source_row(r) for r in rows]


async def get_source(source_id: str) -> Optional[dict]:
    db = await get_db()
    rows = await _fetch(db, "SELECT * FROM llm_cost_sources WHERE id = ?", (source_id,))
    return _source_row(rows[0]) if rows else None


async def create_source(source_id: str, source_type: str, display_name: str, options: dict,
                        enabled: bool = True, interval_sec: int = 0) -> dict:
    db = await get_db()
    await _write(db,
        "INSERT INTO llm_cost_sources (id, type, display_name, enabled, interval_sec, options_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, source_type, display_name, bool(enabled), int(interval_sec or 0), json.dumps(options or {})),
    )
    return await get_source(source_id)


async def update_source(source_id: str, *, display_name: Optional[str] = None, enabled: Optional[bool] = None,
                        interval_sec: Optional[int] = None, options: Optional[dict] = None) -> Optional[dict]:
    current = await get_source(source_id)
    if current is None:
        return None
    db = await get_db()
    await _write(db,
        "UPDATE llm_cost_sources SET display_name = ?, enabled = ?, interval_sec = ?, options_json = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (display_name if display_name is not None else current["display_name"],
         bool(enabled) if enabled is not None else current["enabled"],
         int(interval_sec) if interval_sec is not None else current["interval_sec"],
         json.dumps(options if options is not None else current["options"]), source_id),
    )
    return await get_source(source_id)


async def delete_source(source_id: str) -> bool:
    db = await get_db()
    n = await _write(db, "DELETE FROM llm_cost_sources WHERE id = ?", (source_id,))
    await _write(db, "DELETE FROM llm_cost_snapshots WHERE source_id = ?", (source_id,))
    await _write(db, "DELETE FROM llm_cost_push WHERE source_id = ?", (source_id,))
    return n > 0


# ── snapshots ──────────────────────────────────────────────────────────────


async def save_snapshot(current: Snapshot, last_good: Optional[Snapshot], failures: int = 0,
                        duration_ms: Optional[int] = None) -> None:
    db = await get_db()
    await _write(db,
        "INSERT INTO llm_cost_snapshots (source_id, current_json, last_good_json, failures, last_duration_ms, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source_id) DO UPDATE SET "
        "current_json = EXCLUDED.current_json, last_good_json = EXCLUDED.last_good_json, "
        "failures = EXCLUDED.failures, last_duration_ms = EXCLUDED.last_duration_ms, "
        "updated_at = EXCLUDED.updated_at",
        (current.source_id, json.dumps(current.to_json_dict()),
         json.dumps(last_good.to_json_dict()) if last_good is not None else None,
         int(failures), duration_ms, _now()),
    )


def _snapshot(raw: Any) -> Optional[Snapshot]:
    data = _loads(raw, None)
    if not isinstance(data, dict):
        return None
    try:
        return Snapshot.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


async def load_snapshots() -> dict[str, dict]:
    db = await get_db()
    rows = await _fetch(db, "SELECT * FROM llm_cost_snapshots")
    out = {}
    for r in rows:
        out[r["source_id"]] = {
            "current": _snapshot(r["current_json"]), "last_good": _snapshot(r["last_good_json"]),
            "failures": int(r["failures"] or 0), "last_duration_ms": r["last_duration_ms"],
            "updated_at": int(r["updated_at"] or 0),
        }
    return out


# ── push ───────────────────────────────────────────────────────────────────


async def upsert_push(device_id: str, snap: Snapshot) -> None:
    db = await get_db()
    await _write(db,
        "INSERT INTO llm_cost_push (device_id, source_id, snapshot_json, observed_at, received_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (device_id, source_id) DO UPDATE SET "
        "snapshot_json = EXCLUDED.snapshot_json, observed_at = EXCLUDED.observed_at, "
        "received_at = EXCLUDED.received_at",
        (device_id, snap.source_id, json.dumps(snap.to_json_dict()), int(snap.observed_at), _now()),
    )


async def list_push(source_id: Optional[str] = None) -> dict[str, list[tuple[str, Snapshot]]]:
    """source_id -> [(device name, snapshot)] for every device's latest push."""
    db = await get_db()
    sql = ("SELECT p.source_id, p.snapshot_json, p.device_id, COALESCE(d.name, p.device_id) AS name "
           "FROM llm_cost_push p LEFT JOIN llm_cost_devices d ON d.id = p.device_id "
           "WHERE (d.revoked_at IS NULL)")
    params: tuple = ()
    if source_id:
        sql += " AND p.source_id = ?"
        params = (source_id,)
    out: dict[str, list] = {}
    for r in await _fetch(db, sql, params):
        snap = _snapshot(r["snapshot_json"])
        if snap is not None:
            out.setdefault(r["source_id"], []).append((r["name"], snap))
    return out


# ── devices ────────────────────────────────────────────────────────────────


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _device_row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "source_id": r["source_id"], "token_prefix": r["token_prefix"],
            "created_at": r["created_at"], "last_seen_at": r["last_seen_at"], "last_host": r["last_host"],
            "revoked_at": r["revoked_at"]}


async def create_device(name: str, source_id: str) -> tuple[dict, str]:
    """Mint a device token. Only its hash is stored; the plaintext is returned once."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    device_id = "dev_" + secrets.token_hex(6)
    db = await get_db()
    await _write(db,
        "INSERT INTO llm_cost_devices (id, name, source_id, token_hash, token_prefix) VALUES (?, ?, ?, ?, ?)",
        (device_id, name, source_id, hash_token(token), token[: len(TOKEN_PREFIX) + 4]),
    )
    rows = await _fetch(db, "SELECT * FROM llm_cost_devices WHERE id = ?", (device_id,))
    return _device_row(rows[0]), token


async def list_devices() -> list[dict]:
    db = await get_db()
    return [_device_row(r) for r in await _fetch(db, "SELECT * FROM llm_cost_devices ORDER BY created_at, id")]


async def revoke_device(device_id: str) -> bool:
    db = await get_db()
    n = await _write(db,
        "UPDATE llm_cost_devices SET revoked_at = datetime('now') WHERE id = ? AND revoked_at IS NULL", (device_id,))
    await _write(db, "DELETE FROM llm_cost_push WHERE device_id = ?", (device_id,))
    return n > 0


async def device_for_token(token: str) -> Optional[dict]:
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    db = await get_db()
    rows = await _fetch(db,
        "SELECT * FROM llm_cost_devices WHERE token_hash = ? AND revoked_at IS NULL", (hash_token(token),))
    return _device_row(rows[0]) if rows else None


async def touch_device(device_id: str, host: str) -> None:
    db = await get_db()
    await _write(db, "UPDATE llm_cost_devices SET last_seen_at = ?, last_host = ? WHERE id = ?",
                     (_now(), (host or "")[:80], device_id))


# ── history ────────────────────────────────────────────────────────────────


def bucket_of(ts: int, bucket_sec: int) -> int:
    size = max(60, int(bucket_sec))
    return int(ts) // size * size


async def record_history(snap: Snapshot, bucket_sec: int) -> None:
    """Persist one healthy snapshot. Stale or failed readings never enter history."""
    if snap.health != HEALTH_OK:
        return
    bucket = bucket_of(snap.observed_at, bucket_sec)
    quota_rows = [(snap.source_id, q.scope, q.label, bucket, snap.observed_at, q.pct, q.used, q.limit, q.unit,
                   q.resets_at) for q in snap.quotas if not q.stale]
    spend_rows = [(snap.source_id, s.window, bucket, snap.observed_at, s.amount, s.currency, s.estimated)
                  for s in snap.spend]
    db = await get_db()
    if quota_rows:
        await db.executemany(
            "INSERT INTO llm_cost_quota_samples (source_id, scope, label, bucket_ts, observed_at, pct, used, lim, "
            "unit, resets_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source_id, scope, label, bucket_ts) DO UPDATE SET observed_at = EXCLUDED.observed_at, "
            "pct = EXCLUDED.pct, used = EXCLUDED.used, lim = EXCLUDED.lim, unit = EXCLUDED.unit, "
            "resets_at = EXCLUDED.resets_at", quota_rows)
        await db.commit()
    if spend_rows:
        await db.executemany(
            "INSERT INTO llm_cost_spend_samples (source_id, spend_window, bucket_ts, observed_at, amount, currency, "
            "estimated) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source_id, spend_window, bucket_ts) DO UPDATE SET observed_at = EXCLUDED.observed_at, "
            "amount = EXCLUDED.amount, currency = EXCLUDED.currency, estimated = EXCLUDED.estimated", spend_rows)
        await db.commit()


async def record_event(source_id: str, health: str, detail: Optional[str]) -> bool:
    """Log a health transition. Returns True when a row was written."""
    db = await get_db()
    rows = await _fetch(db,
        "SELECT health, detail FROM llm_cost_events WHERE source_id = ? ORDER BY id DESC LIMIT 1", (source_id,))
    clipped = (detail or "")[:400] or None
    if rows and rows[0]["health"] == health and rows[0]["detail"] == clipped:
        return False
    if not rows and health == HEALTH_OK:
        return False
    await _write(db, "INSERT INTO llm_cost_events (source_id, at, health, detail) VALUES (?, ?, ?, ?)",
                     (source_id, _now(), health, clipped))
    return True


async def last_event_health(source_id: str) -> Optional[str]:
    db = await get_db()
    rows = await _fetch(db, "SELECT health FROM llm_cost_events WHERE source_id = ? ORDER BY id DESC LIMIT 1",
                          (source_id,))
    return rows[0]["health"] if rows else None


async def quota_series(source_id: str, scope: str, since: int, label: str = "", limit: int = 2000) -> list[dict]:
    db = await get_db()
    rows = await _fetch(db,
        "SELECT bucket_ts, label, pct, used, lim, unit FROM llm_cost_quota_samples WHERE source_id = ? "
        "AND scope = ? AND (? = '' OR label = ?) AND bucket_ts >= ? ORDER BY bucket_ts ASC LIMIT ?",
        (source_id, scope, label, label, int(since), int(limit)))
    return [{"t": r["bucket_ts"], "label": r["label"], "pct": r["pct"], "used": r["used"], "limit": r["lim"],
             "unit": r["unit"]} for r in rows]


async def spend_series(source_id: str, window: str, since: int, limit: int = 2000) -> list[dict]:
    db = await get_db()
    rows = await _fetch(db,
        "SELECT bucket_ts, amount, currency, estimated FROM llm_cost_spend_samples WHERE source_id = ? "
        "AND spend_window = ? AND bucket_ts >= ? ORDER BY bucket_ts ASC LIMIT ?",
        (source_id, window, int(since), int(limit)))
    return [{"t": r["bucket_ts"], "amount": r["amount"], "currency": r["currency"],
             "estimated": bool(r["estimated"])} for r in rows]


_DAY = "strftime('%Y-%m-%d', bucket_ts + ?, 'unixepoch')"


async def daily_peaks(source_id: str, scope: str, label: str = "", days: int = 140,
                      tz_offset_sec: int = 0) -> list[dict]:
    """Highest observed percent per day (heatmap). tz_offset_sec shifts the day boundary."""
    since = _now() - int(days) * 86400
    db = await get_db()
    rows = await _fetch(db,
        f"SELECT {_DAY} AS day, MAX(pct) AS peak, COUNT(*) AS samples FROM llm_cost_quota_samples "
        "WHERE source_id = ? AND scope = ? AND (? = '' OR label = ?) AND bucket_ts >= ? AND pct IS NOT NULL "
        "GROUP BY day ORDER BY day ASC",
        (int(tz_offset_sec), source_id, scope, label, label, since))
    return [{"day": r["day"], "peak": r["peak"], "samples": r["samples"]} for r in rows]


async def heat_candidates() -> list[dict]:
    """(source, scope, label) combos that have percent history."""
    db = await get_db()
    rows = await _fetch(db,
        "SELECT source_id, scope, label, MAX(bucket_ts) AS last FROM llm_cost_quota_samples "
        "WHERE pct IS NOT NULL GROUP BY source_id, scope, label ORDER BY last DESC")
    return [{"sourceId": r["source_id"], "scope": r["scope"], "label": r["label"]} for r in rows]


async def daily_spend(source_ids: Sequence[str], window: str = "today", days: int = 30,
                      tz_offset_sec: int = 0) -> list[dict]:
    """Last value seen per day per source: the right read for a cumulative window."""
    if not source_ids:
        return []
    since = _now() - int(days) * 86400
    marks = ",".join("?" for _ in source_ids)
    db = await get_db()
    rows = await _fetch(db,
        "SELECT day, source_id, amount, estimated FROM ("
        f"  SELECT {_DAY} AS day, source_id, amount, estimated, "
        f"         ROW_NUMBER() OVER (PARTITION BY source_id, {_DAY} ORDER BY bucket_ts DESC) AS rnk "
        "  FROM llm_cost_spend_samples WHERE spend_window = ? AND bucket_ts >= ? "
        f"  AND source_id IN ({marks})"
        ") ranked WHERE rnk = 1 ORDER BY day ASC, source_id",
        (int(tz_offset_sec), int(tz_offset_sec), window, since, *source_ids))
    return [{"day": r["day"], "sourceId": r["source_id"], "amount": r["amount"],
             "estimated": bool(r["estimated"])} for r in rows]


async def recent_events(limit: int = 50) -> list[dict]:
    db = await get_db()
    rows = await _fetch(db, "SELECT source_id, at, health, detail FROM llm_cost_events ORDER BY id DESC LIMIT ?",
                          (int(limit),))
    return [{"sourceId": r["source_id"], "at": r["at"], "health": r["health"], "detail": r["detail"]} for r in rows]


async def prune(retention_days: int) -> int:
    cutoff = _now() - max(1, int(retention_days)) * 86400
    db = await get_db()
    removed = 0
    for sql in ("DELETE FROM llm_cost_quota_samples WHERE bucket_ts < ?",
                "DELETE FROM llm_cost_spend_samples WHERE bucket_ts < ?",
                "DELETE FROM llm_cost_events WHERE at < ?",
                "DELETE FROM llm_cost_push WHERE received_at < ?"):
        n = await _write(db, sql, (cutoff,))
        removed += n
    return removed


async def stats() -> dict:
    db = await get_db()
    q = await _fetch(db, "SELECT COUNT(*) AS n FROM llm_cost_quota_samples")
    s = await _fetch(db, "SELECT COUNT(*) AS n FROM llm_cost_spend_samples")
    e = await _fetch(db, "SELECT COUNT(*) AS n FROM llm_cost_events")
    return {"quotaSamples": q[0]["n"], "spendSamples": s[0]["n"], "events": e[0]["n"]}


# ── settings + flags ───────────────────────────────────────────────────────


async def _get_doc(key: str) -> Any:
    db = await get_db()
    rows = await _fetch(db, "SELECT value_json FROM llm_cost_settings WHERE key = ?", (key,))
    return _loads(rows[0]["value_json"], None) if rows else None


async def _set_doc(key: str, value: Any) -> None:
    db = await get_db()
    await _write(db,
        "INSERT INTO llm_cost_settings (key, value_json) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
        "value_json = EXCLUDED.value_json, updated_at = datetime('now')", (key, json.dumps(value)))


def merge_settings(saved: Any) -> dict:
    out = copy.deepcopy(DEFAULT_SETTINGS)
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k == "mqtt" and isinstance(v, dict):
                out["mqtt"].update({mk: mv for mk, mv in v.items() if mk in out["mqtt"]})
            elif k in out:
                out[k] = v
    return out


async def get_settings() -> dict:
    return merge_settings(await _get_doc("settings"))


async def save_settings(values: dict) -> dict:
    merged = merge_settings(values)
    await _set_doc("settings", merged)
    return merged


async def get_flag(key: str) -> Any:
    return await _get_doc("flag:" + key)


async def set_flag(key: str, value: Any) -> None:
    await _set_doc("flag:" + key, value)


# ── alert state ────────────────────────────────────────────────────────────


async def load_alert_state() -> dict[str, dict]:
    db = await get_db()
    rows = await _fetch(db, "SELECT * FROM llm_cost_alert_state")
    return {r["alert_key"]: {"level": r["level"], "title": r["title"], "since": r["since"]} for r in rows}


async def upsert_alert(key: str, level: str, title: str, since: int) -> None:
    db = await get_db()
    await _write(db,
        "INSERT INTO llm_cost_alert_state (alert_key, level, title, since) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (alert_key) DO UPDATE SET level = EXCLUDED.level, title = EXCLUDED.title",
        (key, level, title[:200], int(since)))


async def delete_alert(key: str) -> None:
    db = await get_db()
    await _write(db, "DELETE FROM llm_cost_alert_state WHERE alert_key = ?", (key,))
