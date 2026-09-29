"""LLM Cost: hub honesty rules, backoff, history, events, alerts, seeding (SQLite)."""

import random
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from backend.modules.llm_cost import alerts, store
from backend.modules.llm_cost.hub import Hub, backoff_delay, expired
from backend.modules.llm_cost.models import Quota, Snapshot, Spend
from backend.modules.llm_cost.providers.base import ConfigError, ProviderError
from tests._llm_cost_fixtures import *  # noqa: F401,F403


@pytest.fixture(autouse=True)
def _isolated_prices(tmp_path, monkeypatch):
    from backend import pricing

    monkeypatch.setattr(pricing, "PRICE_BOOK_FILE", tmp_path / "price_book.json")
    monkeypatch.setattr(pricing, "_cache", None)


class _Fake:
    """Provider double whose next fetch result the test sets."""

    TYPE = "demo"
    KIND = "agent"
    MODE = "poll"
    interval_sec = 300

    def __init__(self, source_id="src"):
        self.source_id = source_id
        self.display_name = "Fake"
        self.accent = "#123456"
        self.next = None

    def validate(self):
        pass

    async def fetch(self):
        if isinstance(self.next, Exception):
            raise self.next
        return self.next

    def unconfigured(self, message):
        from backend.modules.llm_cost.models import error_snapshot

        return error_snapshot(self.source_id, self.display_name, message, health="unconfigured")


def _snap(pct=50.0, resets_at=None, observed=None, today=1.0):
    return Snapshot("src", "Fake", int(observed or time.time()),
                    quotas=(Quota("weekly", "WEEK", pct=pct, resets_at=resets_at),),
                    spend=(Spend("today", today),), source_type="demo")


async def _hub_with(fake):
    await store.create_source("src", "demo", "Fake", {})
    hub = Hub()
    source = await store.get_source("src")
    hub.provider_for = lambda s: fake
    return hub, source


async def test_success_then_failure_reserves_stale_with_flagged_quotas(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = _snap()
    assert (await hub.poll_source(source)).health == "ok"
    fake.next = ProviderError("upstream 502")
    snap = await hub.poll_source(source)
    assert snap.health == "stale" and snap.error == "upstream 502"
    assert all(q.stale for q in snap.quotas)
    assert snap.spend_for("today").amount == 1.0


async def test_failure_with_no_prior_success_is_an_error(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = RuntimeError("boom")
    snap = await hub.poll_source(source)
    assert snap.health == "error" and snap.quotas == ()


async def test_config_error_reads_unconfigured_not_broken(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = ConfigError("set secret $X")
    snap = await hub.poll_source(source)
    assert snap.health == "unconfigured" and "$X" in snap.error


async def test_cache_past_ceiling_or_reset_is_not_reserved(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = _snap(observed=time.time() - 99999)
    await hub.poll_source(source)
    fake.next = ProviderError("down")
    assert (await hub.poll_source(source)).health == "error"

    fake.next = _snap(resets_at=int(time.time()) - 1)
    await hub.poll_source(source)
    fake.next = ProviderError("down")
    assert (await hub.poll_source(source)).health == "error"


def test_backoff_multiplies_and_caps():
    rng = random.Random(1)
    assert 85 <= backoff_delay(100, 0, rng) <= 115
    assert 170 <= backoff_delay(100, 2, rng) <= 230
    assert 2550 <= backoff_delay(100, 99, rng) <= 3450


def test_expired_by_ceiling_and_reset():
    now = 10_000
    assert expired(_snap(observed=now - 500), ceiling=100, now=now)
    assert not expired(_snap(observed=now - 50), ceiling=100, now=now)
    assert expired(_snap(observed=now, resets_at=now - 1), ceiling=100, now=now)


async def test_recovery_resets_failure_count_and_history_skips_stale(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = _snap(pct=20)
    await hub.poll_source(source)
    fake.next = ProviderError("down")
    await hub.poll_source(source)
    rows = await store.load_snapshots()
    assert rows["src"]["failures"] == 1
    since = int(time.time()) - 3600
    series = await store.quota_series("src", "weekly", since)
    assert len(series) == 1 and series[0]["pct"] == 20
    fake.next = _snap(pct=30)
    await hub.poll_source(source)
    assert (await store.load_snapshots())["src"]["failures"] == 0


async def test_events_log_transitions_not_states(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = RuntimeError("boom")
    await hub.poll_source(source)
    await hub.poll_source(source)
    fake.next = _snap()
    await hub.poll_source(source)
    events = await store.recent_events()
    assert [e["health"] for e in events] == ["ok", "error"]


async def test_snapshots_survive_a_restart(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = _snap(pct=42)
    await hub.poll_source(source)
    fresh = Hub()
    snaps = await fresh.current_snapshots()
    assert snaps[0].health == "ok" and snaps[0].quotas[0].pct == 42


async def test_read_time_staleness_when_scheduler_stops(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = _snap()
    await hub.poll_source(source)
    rows = await store.load_snapshots()
    old = replace(rows["src"]["current"], observed_at=int(time.time()) - 3 * 3600)
    await store.save_snapshot(old, old, 0, 1)
    snap = (await Hub().current_snapshots())[0]
    assert snap.health in ("stale", "error")


async def test_disabled_and_never_polled_sources(test_db):
    await store.create_source("off", "anthropic_admin", "Off", {}, enabled=False)
    await store.create_source("new", "openai_admin", "New", {})
    snaps = {s.source_id: s for s in await Hub().current_snapshots()}
    assert snaps["off"].health == "disabled"
    assert snaps["new"].health == "unconfigured" and snaps["new"].error == "waiting for first poll"


async def test_state_overview_worst_quota_spend_and_burn(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = replace(_snap(pct=12, today=4.0), quotas=(
        Quota("credit", "CREDITS", used=80, limit=100, unit="credits"),))
    await hub.poll_source(source)
    state = await hub.state()
    ov = state["overview"]
    assert ov["spend"]["today"] == 4.0 and ov["spend"]["mtd"] is None
    assert ov["worstQuota"]["pct"] == 80 and ov["worstQuota"]["remainingUsd"] == 20.0
    assert ov["burnPerHour"] is None
    assert state["sources"][0]["runner"]["mode"] == "poll"


# ── store queries ──────────────────────────────────────────────────────────


async def test_daily_spend_takes_last_value_per_day(test_db):
    await store.create_source("src", "demo", "Fake", {})
    day0 = (int(time.time()) // 86400) * 86400
    for offset, amount in ((3600, 1.0), (7200, 2.5), (86400 + 3600, 0.5)):
        await store.record_history(Snapshot("src", "F", day0 - 86400 + offset, spend=(Spend("today", amount),)),
                                   900)
    rows = await store.daily_spend(["src"], "today", 5)
    by_day = {r["day"]: r["amount"] for r in rows}
    assert sorted(by_day.values()) == [0.5, 2.5]


async def test_daily_peaks_and_tz_shift(test_db):
    day0 = (int(time.time()) // 86400) * 86400
    await store.record_history(Snapshot("src", "F", day0 + 3600, quotas=(Quota("weekly", "W", pct=30),)), 900)
    await store.record_history(Snapshot("src", "F", day0 + 7200, quotas=(Quota("weekly", "W", pct=60),)), 900)
    peaks = await store.daily_peaks("src", "weekly")
    assert peaks[-1]["peak"] == 60 and peaks[-1]["samples"] == 2
    shifted = await store.daily_peaks("src", "weekly", tz_offset_sec=-5 * 3600)
    assert shifted[-1]["day"] < peaks[-1]["day"]
    assert (await store.heat_candidates())[0] == {"sourceId": "src", "scope": "weekly", "label": "W"}


async def test_prune_removes_old_samples(test_db):
    await store.record_history(Snapshot("src", "F", 1000, spend=(Spend("today", 1.0),)), 900)
    await store.record_history(Snapshot("src", "F", int(time.time()), spend=(Spend("today", 1.0),)), 900)
    assert await store.prune(30) >= 1
    assert (await store.stats())["spendSamples"] == 1


# ── alerts ─────────────────────────────────────────────────────────────────


SETTINGS = dict(store.DEFAULT_SETTINGS)


def test_alert_levels_and_money_suffix():
    snaps = [Snapshot("a", "OpenRouter", 1, quotas=(Quota("credit", "CREDITS", used=95, limit=100, unit="usd"),
                                                     Quota("session", "SESSION", pct=80)))]
    out = alerts.evaluate(snaps, SETTINGS)
    assert out[0]["level"] == "critical" and "($5.00 left)" in out[0]["title"]
    assert out[1]["level"] == "warn" and "left" not in out[1]["title"]


def test_daily_spend_alert_threshold_and_zero_disables():
    snaps = [Snapshot("a", "A", 1, spend=(Spend("today", 12.0),))]
    assert any(a["key"] == "spend:today" for a in alerts.evaluate(snaps, {**SETTINGS, "spend_daily_warn": 10}))
    assert not alerts.evaluate(snaps, {**SETTINGS, "spend_daily_warn": 20})
    assert not alerts.evaluate(snaps, {**SETTINGS, "spend_daily_warn": 0})


def test_health_alerts():
    snaps = [Snapshot("a", "A", 1, health="error", error="x"), Snapshot("b", "B", 1, health="stale")]
    levels = {a["sourceId"]: a["level"] for a in alerts.evaluate(snaps, SETTINGS)}
    assert levels == {"a": "error", "b": "warn"}


async def test_notifications_fire_on_transitions_only(test_db):
    hot = [Snapshot("a", "A", 1, quotas=(Quota("session", "SESSION", pct=80),))]
    hotter = [Snapshot("a", "A", 1, quotas=(Quota("session", "SESSION", pct=95),))]
    with patch("backend.modules.llm_cost.alerts.notify", new=AsyncMock()) as notify:
        await alerts.process(hot, SETTINGS)
        await alerts.process(hot, SETTINGS)
        assert notify.await_count == 1
        await alerts.process(hotter, SETTINGS)
        assert notify.await_count == 2
        await alerts.process(hot, SETTINGS)
        assert notify.await_count == 2
        await alerts.process([], SETTINGS)
        assert notify.await_count == 3
        assert "resolved" in notify.await_args.args[0]
    assert await store.load_alert_state() == {}


async def test_notify_writes_a_persisted_message(test_db):
    await alerts.notify("LLM Cost: test", "body", "warning")
    rows = await test_db.fetch("SELECT title, level, source FROM messages")
    assert rows[0]["title"] == "LLM Cost: test" and rows[0]["source"] == "llm-cost"


# ── seeding ────────────────────────────────────────────────────────────────


async def test_seed_defaults_once_from_resolvable_env(test_db, monkeypatch):
    for name in ("ANTHROPIC_ADMIN_KEY", "OPENAI_ADMIN_KEY", "OPENROUTER_KEY", "OPENROUTER_MANAGEMENT_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin-test")
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "mgmt")
    hub = Hub()
    assert await hub.seed_defaults() == ["anthropic", "openrouter"]
    source = await store.get_source("anthropic")
    assert source["options"] == {"secret_ref": "$ANTHROPIC_ADMIN_KEY"}
    await store.delete_source("anthropic")
    assert await hub.seed_defaults() == []


async def test_seed_skipped_when_sources_exist(test_db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin-test")
    await store.create_source("mine", "openai_admin", "Mine", {"secret_ref": "$X"})
    assert await Hub().seed_defaults() == []


async def test_scheduler_tick_polls_due_sources(test_db):
    fake = _Fake()
    hub, source = await _hub_with(fake)
    fake.next = _snap()
    with patch("backend.modules.llm_cost.hub.TYPES", {"demo": _Fake}):
        await hub._reload()
    hub._runners["src"]["next_due"] = 0
    hub._last["alerts"] = time.time()
    hub._last["prune"] = time.time()
    await hub.tick()
    import asyncio

    await asyncio.gather(*list(hub._inflight))
    assert (await store.load_snapshots())["src"]["current"].health == "ok"
    assert hub._runners["src"]["next_due"] > time.time()
