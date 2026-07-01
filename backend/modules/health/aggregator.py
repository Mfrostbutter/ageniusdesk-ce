"""Fan out across registered fleet sources, validate their rows, and roll them up.

Contributed rows are UNTRUSTED (a community module supplies them), so every row is
schema-validated and capped here; a malformed row is dropped, a source that is
slow / unreachable / throwing becomes a single `down` row (degraded-not-fatal,
mirroring the n8n `_instance_health` contract). The endpoint never 500s on a bad
source. Per-source results are cached for a short TTL so a rapid poll shares one
fetch. See docs/specs/2026-07-01-fleet-health-contribution-api.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from backend.modules.health import registry

logger = logging.getLogger(__name__)

# Per-source fetch timeout; a hung module cannot stall the whole roll-up.
SOURCE_TIMEOUT = 5.0
# A failed source is negatively cached this long (shorter than a healthy TTL) so it
# is retried soon without hammering.
NEG_TTL = 10.0

MAX_ROWS = 32       # per source
MAX_METRICS = 8     # per row

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,63}$")
_KIND_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_URL_RE = re.compile(r"^/[A-Za-z0-9/_-]*$")   # in-app absolute path only
_STATUS = {"ok", "degraded", "down"}


def _validate_row(raw: Any) -> dict | None:
    """Coerce one contributed row to the strict schema, or None to drop it."""
    if not isinstance(raw, dict):
        return None
    rid, kind, label = raw.get("id"), raw.get("kind"), raw.get("label")
    if not (isinstance(rid, str) and _ID_RE.match(rid)):
        return None
    if not (isinstance(kind, str) and _KIND_RE.match(kind)):
        return None
    if not isinstance(label, str) or not label:
        return None

    reachable = bool(raw.get("reachable"))
    status = raw.get("status")
    if status not in _STATUS:
        status = "ok" if reachable else "down"
    if not reachable:
        status = "down"

    metrics = []
    for m in (raw.get("metrics") or [])[:MAX_METRICS]:
        if not isinstance(m, dict) or not isinstance(m.get("label"), str):
            continue
        mv = m.get("value")
        if isinstance(mv, bool):
            continue
        if isinstance(mv, (int, float)):
            value: Any = mv
        elif isinstance(mv, str):
            value = mv[:40]
        else:
            continue
        metrics.append({"label": m["label"][:24], "value": value})

    detail_url = raw.get("detail_url") or ""
    if not (isinstance(detail_url, str) and _URL_RE.match(detail_url)):
        detail_url = ""   # reject external / javascript: / //host

    return {
        "id": rid,
        "kind": kind,
        "label": label[:80],
        "reachable": reachable,
        "status": status,
        "error": (str(raw.get("error") or ""))[:200],
        "metrics": metrics,
        "detail_url": detail_url,
    }


async def _pull_rows(source: registry.FleetSource) -> list[dict]:
    """Pull a community module's health route over the worker proxy transport.

    Isolated tier only in this increment: the host reaches the worker directly via
    its per-spawn client + proxy secret (no app-auth middleware in the path). An
    in_process community module is not pulled yet (it renders in its own view until
    run isolated) — a documented limitation.
    """
    from backend.modules._runtime import supervisor

    worker = supervisor.get(source.module_id)
    if worker is None or not worker.is_alive():
        raise RuntimeError("module worker is not running (or runs in_process, not yet pulled)")
    path = f"/api/{source.module_id}/{source.route}"
    resp = await worker.client.get(path, headers={"x-agd-proxy-secret": worker.proxy_secret})
    if resp.status_code != 200:
        raise RuntimeError(f"health route returned HTTP {resp.status_code}")
    data = resp.json()
    return data.get("rows", []) if isinstance(data, dict) else []


def _down_row(source: registry.FleetSource, error: str) -> dict:
    return {
        "id": source.id, "kind": "unknown", "label": source.module_id or source.id,
        "reachable": False, "status": "down", "error": error[:200], "metrics": [], "detail_url": "",
    }


async def _fetch_one(source: registry.FleetSource) -> list[dict]:
    now = time.monotonic()
    c = source.cache
    if c and (now - c["t"]) < source.ttl:
        return c["rows"]
    try:
        if source.kind == "provider" and source.provider is not None:
            raw_rows = await asyncio.wait_for(source.provider(), timeout=SOURCE_TIMEOUT)
        else:
            raw_rows = await asyncio.wait_for(_pull_rows(source), timeout=SOURCE_TIMEOUT)
        raw_rows = raw_rows or []
        if len(raw_rows) > MAX_ROWS:
            logger.warning("fleet-health: source %s returned %d rows, capped at %d", source.id, len(raw_rows), MAX_ROWS)
        rows = [v for v in (_validate_row(r) for r in raw_rows[:MAX_ROWS]) if v]
        source.cache = {"t": now, "rows": rows}
        return rows
    except Exception as e:
        rows = [_down_row(source, f"{type(e).__name__}: {e}")]
        # Negative cache: expire after NEG_TTL rather than the full ttl.
        source.cache = {"t": now - max(0.0, source.ttl - NEG_TTL), "rows": rows}
        return rows


async def collect() -> dict:
    """Fetch every registered source (bounded, degraded-not-fatal) and roll up."""
    sources = registry.all_sources()
    if not sources:
        empty = {"sources_total": 0, "sources_ok": 0, "sources_degraded": 0, "sources_down": 0}
        return {"sources": [], "summary": empty}
    results = await asyncio.gather(*[_fetch_one(s) for s in sources], return_exceptions=True)
    rows: list[dict] = []
    for res in results:
        if isinstance(res, Exception):   # _fetch_one already catches; belt-and-suspenders
            continue
        rows.extend(res)
    summary = {
        "sources_total": len(rows),
        "sources_ok": sum(1 for r in rows if r["status"] == "ok"),
        "sources_degraded": sum(1 for r in rows if r["status"] == "degraded"),
        "sources_down": sum(1 for r in rows if r["status"] == "down"),
    }
    return {"sources": rows, "summary": summary}
