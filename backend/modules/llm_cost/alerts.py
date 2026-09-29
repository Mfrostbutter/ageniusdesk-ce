"""Alert rules over current snapshots, plus transition-only notifications."""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from backend.modules.llm_cost import store
from backend.modules.llm_cost.models import HEALTH_ERROR, HEALTH_STALE, Snapshot, total_spend

logger = logging.getLogger(__name__)

_ORDER = {"critical": 0, "error": 1, "warn": 2}
_MESSAGE_LEVEL = {"critical": "error", "error": "error", "warn": "warning"}


def evaluate(snapshots: Sequence[Snapshot], settings: dict) -> list[dict[str, Any]]:
    """Threshold and health conditions worth surfacing. Each carries a stable `key`."""
    warn = float(settings.get("quota_warn_pct", 75.0))
    critical = float(settings.get("quota_critical_pct", 90.0))
    out: list[dict[str, Any]] = []
    for snap in snapshots:
        if snap.health == HEALTH_ERROR:
            out.append({"key": f"health:{snap.source_id}", "level": "error", "sourceId": snap.source_id,
                        "title": f"{snap.display_name} unreachable", "detail": snap.error})
        elif snap.health == HEALTH_STALE:
            out.append({"key": f"health:{snap.source_id}", "level": "warn", "sourceId": snap.source_id,
                        "title": f"{snap.display_name} data is stale", "detail": snap.error})
        for quota in snap.quotas:
            if quota.pct is None:
                continue
            level = "critical" if quota.pct >= critical else "warn" if quota.pct >= warn else None
            if not level:
                continue
            title = f"{snap.display_name} {quota.label} at {quota.pct:.0f}%"
            remaining = quota.money_remaining()
            if remaining is not None:
                title += f" (${remaining:,.2f} left)"
            out.append({"key": f"quota:{snap.source_id}:{quota.scope}:{quota.label}", "level": level,
                        "sourceId": snap.source_id, "title": title, "detail": None,
                        "resetsInSec": quota.resets_in_seconds()})
    daily_warn = float(settings.get("spend_daily_warn") or 0.0)
    if daily_warn > 0:
        today = total_spend(snapshots, "today")
        if today is not None and today >= daily_warn:
            out.append({"key": "spend:today", "level": "warn", "sourceId": None,
                        "title": f"Spend today ${today:,.2f} over ${daily_warn:,.2f}", "detail": None})
    out.sort(key=lambda a: _ORDER.get(a["level"], 9))
    return out


def diff(previous: dict[str, dict], current: Sequence[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """(raised or escalated, level changes to persist silently, cleared keys)."""
    now_keys = {a["key"]: a for a in current}
    raised, changed = [], []
    for key, alert in now_keys.items():
        before = previous.get(key)
        if before is None:
            raised.append(alert)
        elif before["level"] != alert["level"]:
            if _ORDER.get(alert["level"], 9) < _ORDER.get(before["level"], 9):
                raised.append(alert)
            else:
                changed.append(alert)
    cleared = [k for k in previous if k not in now_keys]
    return raised, changed, cleared


async def notify(title: str, body: str, level: str) -> None:
    """Persisted toast through the messages module (broadcast to connected browsers)."""
    try:
        from backend.modules.messages import collector

        await collector.store_message({"title": title, "body": body, "level": level, "source": "llm-cost"})
    except Exception:
        logger.exception("llm-cost message store failed")


async def process(snapshots: Sequence[Snapshot], settings: dict) -> dict[str, list]:
    """Compare against persisted state, notify on transitions only, persist the new state."""
    current = evaluate(snapshots, settings)
    previous = await store.load_alert_state()
    raised, changed, cleared = diff(previous, current)
    now = int(time.time())
    for alert in raised:
        await store.upsert_alert(alert["key"], alert["level"], alert["title"], now)
    for alert in changed:
        await store.upsert_alert(alert["key"], alert["level"], alert["title"], previous[alert["key"]]["since"])
    for key in cleared:
        await store.delete_alert(key)
    if settings.get("notify", True):
        for alert in raised:
            await notify(f"LLM Cost: {alert['title']}", alert.get("detail") or "",
                         _MESSAGE_LEVEL.get(alert["level"], "warning"))
        for key in cleared:
            await notify(f"LLM Cost: resolved: {previous[key]['title']}", "", "success")
    if raised or cleared:
        try:
            from backend.websocket import manager

            await manager.broadcast("llm_cost:alerts", {"alerts": current})
        except Exception:
            logger.debug("llm-cost alert broadcast failed", exc_info=True)
    return {"raised": raised, "cleared": cleared, "current": current}
