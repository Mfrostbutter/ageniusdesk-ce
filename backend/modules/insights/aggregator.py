"""Insights aggregator — pulls n8n executions + local errors and shapes them
into summary, timeseries, and top-list payloads.

Keep this module pure-data: no FastAPI, no formatting. The router renders.

Caching strategy: 5-minute in-memory cache keyed by (instance_id, range_key).
For 30d range with chatty instances list_executions can paginate hundreds of
rows; without the cache every dashboard mount round-trips the entire window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Optional

from backend.modules.errors import collector as errors_collector
from backend.modules.n8n_proxy import client as n8n_client

logger = logging.getLogger(__name__)

RangeKey = Literal["24h", "7d", "30d"]
Bucket = Literal["hour", "day"]

# 'all' uses a far-past window so the page cap (not the date) bounds the scan.
_RANGE_HOURS: dict[str, int] = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "90d": 24 * 90, "all": 24 * 365 * 50}
# Per-range hard caps on n8n pagination so a single API call can't run away on
# a noisy instance. The slim v1 prefers truncated-but-fresh over complete-but-stale.
_RANGE_MAX_PAGES: dict[str, int] = {"24h": 4, "7d": 16, "30d": 40, "90d": 40, "all": 40}
_PAGE_SIZE = 250


@dataclass
class _CacheEntry:
    at: float
    payload: dict[str, Any]


_CACHE_TTL = 300.0
_cache: dict[tuple[str, str], _CacheEntry] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _range_window(range_key: str) -> tuple[datetime, datetime]:
    hours = _RANGE_HOURS.get(range_key, _RANGE_HOURS["24h"])
    end = _now()
    return end - timedelta(hours=hours), end


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # n8n returns ISO 8601 with trailing Z or offset
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


async def _fetch_executions(instance_id: str, since: datetime, max_pages: int) -> list[dict[str, Any]]:
    """Paginate n8n.list_executions until either we cross `since` or hit max_pages.

    n8n's API returns most-recent-first, so we can stop scanning as soon as a
    page's last execution is older than the window.
    """
    rows: list[dict[str, Any]] = []
    cursor = ""
    for page in range(max_pages):
        try:
            result = await n8n_client.list_executions(limit=_PAGE_SIZE, cursor=cursor)
        except Exception as exc:
            logger.warning("insights: list_executions failed page=%d: %s", page, exc)
            break
        items = result.get("executions", []) if isinstance(result, dict) else []
        if not items:
            break
        rows.extend(items)
        # Stop when oldest in this page is already older than the window.
        last = items[-1]
        last_started = _parse_iso(last.get("started_at", ""))
        if last_started is not None and last_started < since:
            break
        cursor = result.get("next_cursor") or ""
        if not cursor:
            break
    # Trim to window in-memory.
    trimmed: list[dict[str, Any]] = []
    for r in rows:
        ts = _parse_iso(r.get("started_at", ""))
        if ts and ts >= since:
            trimmed.append(r)
    return trimmed


def _bucket_key(ts: datetime, bucket: Bucket) -> str:
    if bucket == "hour":
        return ts.strftime("%Y-%m-%dT%H:00:00Z")
    return ts.strftime("%Y-%m-%dZ")


def _iter_buckets(start: datetime, end: datetime, bucket: Bucket) -> Iterable[str]:
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    if bucket == "day":
        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor <= end:
        yield _bucket_key(cursor, bucket)
        cursor += step


def _duration_ms(row: dict[str, Any]) -> Optional[int]:
    s = _parse_iso(row.get("started_at", ""))
    f = _parse_iso(row.get("finished_at", ""))
    if not s or not f:
        return None
    delta = (f - s).total_seconds() * 1000.0
    if delta < 0:
        return None
    return int(delta)


async def _build_payload(instance_id: str, range_key: str) -> dict[str, Any]:
    since, until = _range_window(range_key)
    max_pages = _RANGE_MAX_PAGES.get(range_key, 4)

    executions, errors = await asyncio.gather(
        _fetch_executions(instance_id, since, max_pages),
        errors_collector.get_errors(
            limit=1000,
            range_key=range_key,
            instance_id=instance_id or "",
        ),
        return_exceptions=False,
    )

    # ── Summary tiles ──────────────────────────────────────────────────────
    success = sum(1 for e in executions if e.get("status") == "success")
    error = sum(1 for e in executions if e.get("status") == "error")
    running = sum(1 for e in executions if e.get("status") == "running")
    total = len(executions)
    durations = [d for d in (_duration_ms(e) for e in executions) if d is not None]
    avg_ms = int(sum(durations) / len(durations)) if durations else 0
    success_rate = (success / total) if total else 0.0

    # ── Timeseries (bucketed counts) ───────────────────────────────────────
    bucket: Bucket = "hour" if range_key == "24h" else "day"
    # Bound the bucket span to the data actually scanned so a wide window
    # ('all' / '90d') doesn't allocate thousands of empty leading buckets.
    _ts_list = [t for t in (_parse_iso(e.get("started_at", "")) for e in executions) if t]
    ts_since = max(since, min(_ts_list)) if _ts_list else since
    series: dict[str, dict[str, int]] = {}
    for k in _iter_buckets(ts_since, until, bucket):
        series[k] = {"success": 0, "error": 0, "running": 0}
    for e in executions:
        ts = _parse_iso(e.get("started_at", ""))
        if not ts:
            continue
        k = _bucket_key(ts, bucket)
        if k not in series:
            continue
        st = e.get("status", "unknown")
        if st in series[k]:
            series[k][st] += 1
    points = [{"ts": k, **v, "total": v["success"] + v["error"] + v["running"]} for k, v in series.items()]

    # ── Top workflows by volume + by errors ───────────────────────────────
    by_wf: dict[str, dict[str, Any]] = {}
    for e in executions:
        wf_id = e.get("workflow_id", "") or "unknown"
        rec = by_wf.setdefault(wf_id, {
            "workflow_id": wf_id,
            "workflow_name": e.get("workflow_name", "Unknown"),
            "count": 0,
            "errors": 0,
        })
        rec["count"] += 1
        if e.get("status") == "error":
            rec["errors"] += 1
        # Keep the latest non-empty workflow_name we see.
        if e.get("workflow_name") and rec["workflow_name"] == "Unknown":
            rec["workflow_name"] = e["workflow_name"]
    for r in by_wf.values():
        r["success_rate"] = ((r["count"] - r["errors"]) / r["count"]) if r["count"] else 0.0

    by_volume = sorted(by_wf.values(), key=lambda r: r["count"], reverse=True)[:5]
    by_errors = sorted(
        (r for r in by_wf.values() if r["errors"] > 0),
        key=lambda r: r["errors"],
        reverse=True,
    )[:5]

    # ── Local error stream (counts by workflow from the errors table) ─────
    err_by_wf: dict[str, dict[str, Any]] = {}
    for row in errors:
        wf_id = row.get("workflow_id", "") or "unknown"
        rec = err_by_wf.setdefault(wf_id, {
            "workflow_id": wf_id,
            "workflow_name": row.get("workflow_name", "Unknown"),
            "errors": 0,
            "last_occurred": row.get("occurred_at", ""),
        })
        rec["errors"] += 1
    err_top = sorted(err_by_wf.values(), key=lambda r: r["errors"], reverse=True)[:5]

    return {
        "instance_id": instance_id,
        "range": range_key,
        "bucket": bucket,
        "window": {
            "since": since.isoformat().replace("+00:00", "Z"),
            "until": until.isoformat().replace("+00:00", "Z"),
            "executions_scanned": total,
            "max_pages": max_pages,
            "page_size": _PAGE_SIZE,
        },
        "summary": {
            "total_executions": total,
            "success": success,
            "error": error,
            "running": running,
            "success_rate": success_rate,
            "avg_duration_ms": avg_ms,
            "local_errors": len(errors),
        },
        "timeseries": {"points": points},
        "top_by_volume": by_volume,
        "top_by_errors": by_errors,
        "top_local_errors": err_top,
    }


async def get_insights(instance_id: str, range_key: str = "24h") -> dict[str, Any]:
    """Public entry — returns the cached payload or rebuilds it."""
    if range_key not in _RANGE_HOURS:
        range_key = "24h"
    key = (instance_id or "", range_key)
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached.at) < _CACHE_TTL:
        return {**cached.payload, "_cached": True, "_cache_age_s": int(now - cached.at)}

    payload = await _build_payload(instance_id or "", range_key)
    _cache[key] = _CacheEntry(at=now, payload=payload)
    return {**payload, "_cached": False, "_cache_age_s": 0}


def invalidate(instance_id: str = "", range_key: str = "") -> int:
    """Drop cache entries — used by the refresh button. Returns number dropped."""
    if not instance_id and not range_key:
        n = len(_cache)
        _cache.clear()
        return n
    dropped = 0
    for k in list(_cache.keys()):
        if (not instance_id or k[0] == instance_id) and (not range_key or k[1] == range_key):
            _cache.pop(k, None)
            dropped += 1
    return dropped
