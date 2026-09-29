"""The hub: polls sources, applies the honesty rules, and builds the read views.

Single process: this hub polls and serves the read views from the shared SQLite
database, so snapshots and history survive restarts.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any, Optional

from backend.modules.llm_cost import alerts, store
from backend.modules.llm_cost.models import (
    HEALTH_DISABLED,
    HEALTH_ERROR,
    HEALTH_OK,
    HEALTH_STALE,
    HEALTH_UNCONFIGURED,
    Snapshot,
    any_estimated,
    error_snapshot,
    total_spend,
)
from backend.modules.llm_cost.providers import TYPES
from backend.modules.llm_cost.providers.base import (
    MODE_PUSH,
    ConfigError,
    Http,
    Provider,
    ProviderContext,
    ProviderError,
    resolve_secret,
)
from backend.modules.llm_cost.providers.push import aggregate, parse_payload

logger = logging.getLogger(__name__)

BACKOFF_STEPS = (1, 2, 4, 8, 15, 30)
TICK_SEC = 2.0
ALERT_EVERY_SEC = 30
REFRESH_CHECK_SEC = 10
PRUNE_EVERY_SEC = 3600


def _feature_on() -> bool:
    """Honor the feature switch when the host ships one; on otherwise."""
    try:
        from backend import features
    except ImportError:
        return True
    try:
        return bool(features.enabled("llm_cost"))
    except Exception:  # noqa: BLE001
        return True

# Env secrets that seed a default source once when no sources exist.
SEEDS = (
    ("anthropic", "anthropic_admin", "Anthropic", {"secret_ref": "$ANTHROPIC_ADMIN_KEY"}, ("ANTHROPIC_ADMIN_KEY",)),
    ("openai", "openai_admin", "OpenAI", {"secret_ref": "$OPENAI_ADMIN_KEY"}, ("OPENAI_ADMIN_KEY",)),
    ("openrouter", "openrouter", "OpenRouter",
     {"secret_ref": "$OPENROUTER_KEY", "management_secret_ref": "$OPENROUTER_MANAGEMENT_KEY"},
     ("OPENROUTER_KEY", "OPENROUTER_MANAGEMENT_KEY")),
)


def backoff_delay(interval: int, failures: int, rng: Optional[random.Random] = None) -> float:
    """Interval times the backoff step, with 0.85-1.15 jitter."""
    step = BACKOFF_STEPS[min(failures - 1, len(BACKOFF_STEPS) - 1)] if failures > 0 else 1
    return interval * step * (0.85 + (rng or random).random() * 0.3)


def stale_ceiling(settings: dict, interval: int) -> int:
    return max(int(settings.get("stale_after_sec") or 0), 2 * int(interval))


def expired(snap: Snapshot, ceiling: int, now: Optional[float] = None) -> bool:
    """A cached reading dies at its own reset, or at the ceiling."""
    moment = time.time() if now is None else now
    if ceiling and moment - snap.observed_at > ceiling:
        return True
    return any(q.expired(moment) for q in snap.quotas if q.resets_at is not None)


def _age_text(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (value or "").lower()).strip("-")[:48] or "source"


class Hub:
    def __init__(self, http: Optional[Http] = None) -> None:
        self.http = http or Http()
        self.leader = True
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._providers: dict[str, tuple[str, Provider]] = {}
        self._runners: dict[str, dict] = {}
        self._inflight: set[asyncio.Task] = set()
        self._sources: list[dict] = []
        self._settings: dict = dict(store.DEFAULT_SETTINGS)
        self._last = {"sources": 0.0, "alerts": 0.0, "refresh": 0.0, "prune": 0.0, "mqtt": 0.0}
        self._refresh_seen = 0
        self._mqtt = None

    # ── construction ───────────────────────────────────────────────────────

    def build(self, source: dict) -> Provider:
        cls = TYPES.get(source["type"])
        if cls is None:
            raise ConfigError(f"unknown source type {source['type']!r}")
        ctx = ProviderContext(source_id=source["id"], source_type=source["type"],
                              display_name=source.get("display_name") or "", options=source.get("options") or {},
                              interval_sec=int(source.get("interval_sec") or 0), secret=resolve_secret,
                              http=self.http)
        return cls(ctx)

    def provider_for(self, source: dict) -> Provider:
        """Cached per source; rebuilt when the config changes (keeps tailer state)."""
        stamp = f"{source.get('updated_at')}|{source['type']}"
        cached = self._providers.get(source["id"])
        if cached and cached[0] == stamp:
            return cached[1]
        provider = self.build(source)
        self._providers[source["id"]] = (stamp, provider)
        return provider

    # ── polling ────────────────────────────────────────────────────────────

    async def poll_source(self, source: dict, settings: Optional[dict] = None) -> Snapshot:
        """One poll with the honesty rules applied, persisted, and broadcast."""
        settings = settings or await store.get_settings()
        prev = (await store.load_snapshots()).get(source["id"]) or {}
        last_good: Optional[Snapshot] = prev.get("last_good")
        failures = int(prev.get("failures") or 0)
        started = time.monotonic()
        try:
            provider = self.provider_for(source)
        except ConfigError as exc:
            provider = None
            snap = error_snapshot(source["id"], source.get("display_name") or source["id"], str(exc),
                                  health=HEALTH_UNCONFIGURED, source_type=source["type"])
            failures += 1
        if provider is not None:
            try:
                provider.validate()
                snap = await provider.fetch()
                if not isinstance(snap, Snapshot):
                    raise ProviderError(f"fetch() returned {type(snap).__name__}")
                failures = 0
                if snap.health == HEALTH_OK:
                    last_good = snap
            except ConfigError as exc:
                failures += 1
                snap = provider.unconfigured(str(exc))
            except Exception as exc:  # a source must never take the hub down
                failures += 1
                detail = str(exc) or exc.__class__.__name__
                logger.warning("llm-cost source %s failed (%d in a row): %s", source["id"], failures, detail)
                ceiling = stale_ceiling(settings, provider.interval_sec)
                if last_good is not None and not expired(last_good, ceiling):
                    snap = last_good.marked_stale(detail)
                else:
                    snap = error_snapshot(source["id"], provider.display_name, detail, kind=provider.KIND,
                                          accent=provider.accent, health=HEALTH_ERROR, source_type=source["type"])
        duration = int((time.monotonic() - started) * 1000)
        await self._publish(snap, last_good, failures, duration)
        runner = self._runners.setdefault(source["id"], {})
        runner.update({"failures": failures, "lastDurationMs": duration})
        return snap

    async def _publish(self, snap: Snapshot, last_good: Optional[Snapshot], failures: int,
                       duration: Optional[int]) -> None:
        from backend.config import settings as app_settings

        await store.save_snapshot(snap, last_good, failures, duration)
        try:
            await store.record_history(snap, app_settings.agd_llm_cost_bucket_sec)
        except Exception:
            logger.exception("llm-cost history write failed for %s", snap.source_id)
        await store.record_event(snap.source_id, snap.health, snap.error if snap.health != HEALTH_OK else None)
        try:
            from backend.websocket import manager

            await manager.broadcast("llm_cost:update", {"sourceId": snap.source_id, "health": snap.health,
                                                        "observedAt": snap.observed_at})
        except Exception:
            logger.debug("llm-cost broadcast failed", exc_info=True)

    async def test_source(self, source: dict) -> dict:
        """Fetch once without persisting; for the UI's Test button."""
        try:
            provider = self.build(source)
            provider.validate()
            snap = await provider.fetch()
            return {"ok": snap.health == HEALTH_OK, "snapshot": snap.as_dict()}
        except ConfigError as exc:
            return {"ok": False, "error": str(exc), "health": HEALTH_UNCONFIGURED}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc) or exc.__class__.__name__, "health": HEALTH_ERROR}

    # ── push ingest ────────────────────────────────────────────────────────

    async def ingest(self, device: dict, body: Any, host: str = "") -> dict:
        source = await store.get_source(device["source_id"])
        if source is None or source["type"] != "push":
            raise ProviderError(f"push source {device['source_id']!r} does not exist")
        if not source["enabled"]:
            raise ProviderError(f"push source {device['source_id']!r} is disabled")
        entries = body.get("snapshots") if isinstance(body, dict) and "snapshots" in body else body
        entries = entries if isinstance(entries, list) else [entries]
        accepted, rejected = [], []
        for entry in entries[:4]:
            try:
                snap = parse_payload(entry, source["id"])
            except ProviderError as exc:
                rejected.append(str(exc))
                continue
            await store.upsert_push(device["id"], snap)
            accepted.append(source["id"])
            host = host or str(snap.meta.get("host") or "")
        await store.touch_device(device["id"], host)
        if accepted:
            settings = await store.get_settings()
            pushes = (await store.list_push(source["id"])).get(source["id"], [])
            agg = aggregate(source["id"], source["display_name"], pushes, int(settings["push_stale_sec"]))
            await self._publish(agg, agg if agg.health == HEALTH_OK else None, 0, None)
        return {"accepted": accepted, "rejected": rejected}

    # ── read views ─────────────────────────────────────────────────────────

    async def current_snapshots(self, settings: Optional[dict] = None,
                                sources: Optional[list[dict]] = None) -> list[Snapshot]:
        settings = settings or await store.get_settings()
        sources = sources if sources is not None else await store.list_sources()
        rows = await store.load_snapshots()
        pushes = await store.list_push() if any(s["type"] == "push" for s in sources) else {}
        now = time.time()
        out = []
        for src in sources:
            name = src.get("display_name") or src["id"]
            cls = TYPES.get(src["type"])
            if not src["enabled"]:
                out.append(error_snapshot(src["id"], name, "disabled", health=HEALTH_DISABLED,
                                          source_type=src["type"], accent=getattr(cls, "ACCENT", "#38bdf8")))
                continue
            if src["type"] == "push":
                out.append(aggregate(src["id"], name, pushes.get(src["id"], []), int(settings["push_stale_sec"]),
                                     now=now))
                continue
            row = rows.get(src["id"])
            cur = row.get("current") if row else None
            if cur is None:
                out.append(error_snapshot(src["id"], name, "waiting for first poll", health=HEALTH_UNCONFIGURED,
                                          source_type=src["type"], kind=getattr(cls, "KIND", "api"),
                                          accent=getattr(cls, "ACCENT", "#38bdf8")))
                continue
            interval = max(getattr(cls, "MIN_INTERVAL", 30), int(src.get("interval_sec") or 0)
                           or getattr(cls, "DEFAULT_INTERVAL", 300))
            ceiling = stale_ceiling(settings, interval)
            age = now - cur.observed_at
            if cur.health == HEALTH_OK and age > max(3 * interval + 60, ceiling):
                cur = cur.marked_stale(f"no fresh poll for {_age_text(age)}")
            if cur.health == HEALTH_STALE and expired(cur, max(ceiling, 3 * interval + 60), now):
                cur = error_snapshot(src["id"], name, cur.error or "last good reading expired",
                                     source_type=src["type"], kind=cur.kind, accent=cur.accent)
            if cur.display_name != name:
                from dataclasses import replace

                cur = replace(cur, display_name=name)
            out.append(cur)
        return out

    async def overview(self, snapshots: list[Snapshot], settings: dict) -> dict:
        now = time.time()
        worst = None
        for snap in snapshots:
            for q in snap.quotas:
                if q.pct is None:
                    continue
                if worst is None or q.pct > worst["pct"]:
                    worst = {"pct": q.pct, "source": snap.display_name, "sourceId": snap.source_id,
                             "label": q.label, "resetsInSec": q.resets_in_seconds(now),
                             "remainingUsd": q.money_remaining(), "stale": q.stale}
        burn = [c.value for s in snapshots if s.health in (HEALTH_OK, HEALTH_STALE)
                for c in s.counters if c.key == "tokens_per_hour"]
        return {
            "now": int(now),
            "sourceCount": len(snapshots),
            "healthyCount": sum(1 for s in snapshots if s.health == HEALTH_OK),
            "spend": {"today": total_spend(snapshots, "today"), "mtd": total_spend(snapshots, "mtd"),
                      "30d": total_spend(snapshots, "30d")},
            "spendEstimated": {"today": any_estimated(snapshots, "today"), "mtd": any_estimated(snapshots, "mtd")},
            "burnPerHour": sum(burn) if burn else None,
            "currency": "USD",
            "worstQuota": worst,
            "alerts": alerts.evaluate(snapshots, settings),
        }

    async def state(self) -> dict:
        settings = await store.get_settings()
        sources = await store.list_sources()
        snaps = await self.current_snapshots(settings, sources)
        rows = await store.load_snapshots()
        by_id = {s["id"]: s for s in sources}
        out = []
        for snap in snaps:
            d = snap.as_dict()
            src = by_id.get(snap.source_id) or {}
            runner = self._runners.get(snap.source_id, {})
            row = rows.get(snap.source_id) or {}
            d["runner"] = {"failures": row.get("failures", 0), "lastDurationMs": row.get("last_duration_ms"),
                           "nextDueAt": int(runner["next_due"]) if runner.get("next_due") else None,
                           "mode": MODE_PUSH if src.get("type") == "push" else "poll"}
            out.append(d)
        return {"v": 1, "leader": self.leader, "overview": await self.overview(snaps, settings), "sources": out}

    # ── seeding ────────────────────────────────────────────────────────────

    async def seed_defaults(self) -> list[str]:
        """Create default sources from resolvable env secrets, once, when none exist."""
        if await store.get_flag("seeded"):
            return []
        if await store.list_sources():
            await store.set_flag("seeded", True)
            return []
        created = []
        for sid, stype, name, options, names in SEEDS:
            if any(resolve_secret("$" + n) for n in names):
                await store.create_source(sid, stype, name, dict(options))
                created.append(sid)
        if created:
            await store.set_flag("seeded", True)
            logger.info("llm-cost seeded sources: %s", ", ".join(created))
        return created

    # ── scheduler ──────────────────────────────────────────────────────────

    def request_refresh(self, source_id: Optional[str] = None) -> int:
        """Wake pollers now in this process. Returns how many were nudged."""
        n = 0
        self._last["sources"] = 0.0
        for sid, runner in self._runners.items():
            if source_id in (None, sid):
                runner["next_due"] = 0.0
                n += 1
        self._wake.set()
        return n

    async def _reload(self) -> None:
        self._sources = await store.list_sources()
        self._settings = await store.get_settings()
        rows = await store.load_snapshots()
        now = time.time()
        live = set()
        for src in self._sources:
            if not src["enabled"] or TYPES.get(src["type"]) is None or TYPES[src["type"]].MODE == MODE_PUSH:
                continue
            live.add(src["id"])
            runner = self._runners.setdefault(src["id"], {})
            if "next_due" not in runner:
                row = rows.get(src["id"]) or {}
                interval = self.provider_for(src).interval_sec if src["type"] in TYPES else 300
                resume = row.get("updated_at", 0) + interval if not row.get("failures") else 0
                runner["next_due"] = max(now + random.random() * 2.0, resume)
        for sid in [s for s in self._runners if s not in live]:
            self._runners.pop(sid, None)
            self._providers.pop(sid, None)
        self._last["sources"] = now

    async def _poll_task(self, src: dict) -> None:
        runner = self._runners.setdefault(src["id"], {})
        try:
            await self.poll_source(src, self._settings)
        except Exception:
            logger.exception("llm-cost poll crashed for %s", src["id"])
        finally:
            runner["inflight"] = False
            try:
                interval = self.provider_for(src).interval_sec
            except Exception:
                interval = 300
            runner["next_due"] = time.time() + backoff_delay(interval, int(runner.get("failures") or 0))

    async def _check_refresh(self) -> None:
        flag = await store.get_flag("refresh") or {}
        at = int(flag.get("at") or 0)
        if at > self._refresh_seen:
            self._refresh_seen = at
            self.request_refresh(flag.get("source") or None)

    async def tick(self) -> None:
        if not _feature_on():
            return
        now = time.time()
        if now - self._last["sources"] > 30:
            await self._reload()
        for src in self._sources:
            runner = self._runners.get(src["id"])
            if runner is None or runner.get("inflight") or now < runner.get("next_due", 0):
                continue
            runner["inflight"] = True
            task = asyncio.create_task(self._poll_task(src), name=f"llm_cost_poll_{src['id']}")
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        if now - self._last["refresh"] > REFRESH_CHECK_SEC:
            self._last["refresh"] = now
            await self._check_refresh()
        if now - self._last["alerts"] > ALERT_EVERY_SEC:
            self._last["alerts"] = now
            snaps = await self.current_snapshots(self._settings)
            await alerts.process(snaps, self._settings)
            await self._maybe_mqtt(snaps, now)
        if now - self._last["prune"] > PRUNE_EVERY_SEC:
            self._last["prune"] = now
            from backend.config import settings as app_settings

            await store.prune(app_settings.agd_llm_cost_retention_days)

    async def _maybe_mqtt(self, snaps: list[Snapshot], now: float) -> None:
        cfg = self._settings.get("mqtt") or {}
        if not cfg.get("enabled") or not cfg.get("host"):
            if self._mqtt is not None:
                await asyncio.to_thread(self._mqtt.close)
                self._mqtt = None
            return
        if now - self._last["mqtt"] < max(15, int(cfg.get("publish_interval_sec") or 60)):
            return
        self._last["mqtt"] = now
        from backend.modules.llm_cost import mqtt

        try:
            if self._mqtt is None or not self._mqtt.same_config(cfg):
                if self._mqtt is not None:
                    await asyncio.to_thread(self._mqtt.close)
                self._mqtt = mqtt.HomeAssistantBridge.from_settings(cfg)
            overview = await self.overview(snaps, self._settings)
            await asyncio.to_thread(self._mqtt.publish_all, snaps, overview)
        except Exception:
            logger.exception("llm-cost mqtt publish failed")

    async def run(self) -> None:
        started = False
        while not self._stopping:
            try:
                if not started:
                    started = True
                    await self.seed_defaults()
                    await self._reload()
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("llm-cost scheduler tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SEC)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self.run(), name="llm_cost_scheduler")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._inflight):
            task.cancel()
        if self._mqtt is not None:
            try:
                await asyncio.to_thread(self._mqtt.close)
            except Exception:
                pass
            self._mqtt = None
        self._task = None


hub = Hub()


async def start() -> None:
    from backend.config import settings

    if settings.agd_llm_cost_enabled:
        hub.start()


async def stop() -> None:
    await hub.stop()
