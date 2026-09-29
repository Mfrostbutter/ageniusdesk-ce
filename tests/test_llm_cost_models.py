"""LLM Cost: normalized model, honesty rules, push validation, and aggregation."""

import time

import pytest

from backend.modules.llm_cost.models import (
    Counter,
    Quota,
    Snapshot,
    Spend,
    error_snapshot,
    total_spend,
)
from backend.modules.llm_cost.providers.base import ProviderError
from backend.modules.llm_cost.providers.push import MAX_QUOTAS, aggregate, parse_payload


@pytest.fixture(autouse=True)
def _isolated_prices(tmp_path, monkeypatch):
    from backend import pricing

    monkeypatch.setattr(pricing, "PRICE_BOOK_FILE", tmp_path / "price_book.json")
    monkeypatch.setattr(pricing, "_cache", None)


# ── model ──────────────────────────────────────────────────────────────────


def test_pct_is_clamped_and_non_numbers_are_none():
    assert Quota("session", "S", pct=140).pct == 100.0
    assert Quota("session", "S", pct=-3).pct == 0.0
    assert Quota("session", "S", pct="12").pct is None
    assert Quota("session", "S", pct=float("nan")).pct is None


def test_pct_derived_from_used_and_limit_but_explicit_wins():
    assert Quota("credit", "C", used=25, limit=100, unit="usd").pct == 25.0
    assert Quota("credit", "C", pct=10, used=25, limit=100, unit="usd").pct == 10.0
    assert Quota("credit", "C", used=5, limit=0, unit="usd").pct is None


def test_unknown_scope_unit_and_health_rejected():
    with pytest.raises(ValueError):
        Quota("hourly", "X")
    with pytest.raises(ValueError):
        Quota("session", "X", unit="bananas")
    with pytest.raises(ValueError):
        Snapshot("a", "A", 0, health="great")


def test_reset_countdown_and_expiry():
    q = Quota("session", "S", pct=5, resets_at=1000)
    assert q.resets_in_seconds(now=900) == 100
    assert not q.expired(now=900)
    assert q.expired(now=1000)


def test_money_remaining_only_for_money_units():
    assert Quota("credit", "C", used=30, limit=100, unit="usd").money_remaining() == 70.0
    assert Quota("session", "S", pct=50).money_remaining() is None


def test_spend_currency_normalized_and_non_finite_rejected():
    assert Spend("today", 1.2345678, "usd").currency == "USD"
    with pytest.raises(ValueError):
        Spend("today", float("inf"))


def test_marked_stale_flags_every_quota():
    snap = Snapshot("a", "A", 1, quotas=(Quota("session", "S", pct=5), Quota("weekly", "W", pct=6)))
    stale = snap.marked_stale("boom")
    assert stale.health == "stale" and stale.error == "boom"
    assert all(q.stale for q in stale.quotas)


def test_total_spend_is_none_when_nobody_reports():
    a = Snapshot("a", "A", 1, spend=(Spend("today", 2.0),))
    b = Snapshot("b", "B", 1)
    assert total_spend([a, b], "today") == 2.0
    assert total_spend([b], "today") is None


def test_snapshot_json_round_trip_keeps_detail():
    snap = Snapshot("a", "A", 100, quotas=(Quota("credit", "C", used=1, limit=4, unit="usd", resets_at=500),),
                    spend=(Spend("mtd", 3.5, estimated=True),), counters=(Counter("total_tokens", 9),),
                    meta={"topModel": "x"}, detail={"models": [{"model": "x"}]}, source_type="demo")
    back = Snapshot.from_dict(snap.to_json_dict())
    assert back == snap


def test_error_snapshot_truncates_and_blanks():
    assert len(error_snapshot("a", "A", "x" * 1000).error) == 400
    assert error_snapshot("a", "A", "").error is None


# ── push validation ────────────────────────────────────────────────────────


def test_non_object_and_foreign_source_rejected():
    with pytest.raises(ProviderError):
        parse_payload([], "claude-code")
    with pytest.raises(ProviderError):
        parse_payload({"providerId": "other"}, "claude-code")


def test_timestamps_are_clamped():
    now = 1_000_000
    assert parse_payload({"observedAt": now + 99999}, "cc", now=now).observed_at == now + 120
    assert parse_payload({"observedAt": -5}, "cc", now=now).observed_at == 0
    assert parse_payload({}, "cc", now=now).observed_at == now


def test_bad_quota_entries_dropped_and_list_bounded():
    body = {"quotas": [{"scope": "session", "pct": 10, "unit": "furlongs"}, {"scope": "bogus", "pct": 1}, "x"]
            + [{"scope": "weekly", "pct": i} for i in range(20)]}
    snap = parse_payload(body, "cc")
    assert snap.quotas[0].unit == "pct"
    assert all(q.scope in ("session", "weekly") for q in snap.quotas)
    assert len(snap.quotas) <= MAX_QUOTAS


def test_spend_and_counters_validated():
    body = {"spend": [{"window": "today", "amount": 1.5}, {"window": "today"}, {"amount": 2},
                      {"window": "mtd", "amount": float("nan")}],
            "counters": [{"key": "t", "value": 5}, {"key": "", "value": 1}, {"key": "x", "value": "7"}] * 20}
    snap = parse_payload(body, "cc")
    assert [s.window for s in snap.spend] == ["today"]
    assert len(snap.counters) <= 24 and all(c.key == "t" for c in snap.counters)


def test_strings_cleaned_meta_scalar_only_accent_hex_health_normalized():
    body = {"displayName": "Claude\x00\x07 Code" + "y" * 100, "meta": {"a": {"nested": 1}, "b": "ok", "c": 3},
            "accent": "red", "health": "fantastic"}
    snap = parse_payload(body, "cc")
    assert "\x00" not in snap.display_name and len(snap.display_name) <= 40
    assert snap.meta == {"b": "ok", "c": 3}
    assert snap.accent == "#a78bfa"
    assert snap.health == "ok"


def test_usage_rows_become_estimated_spend_from_price_book():
    body = {"usage": [{"model": "claude-sonnet-4-5", "window": "today", "input": 1_000_000, "output": 1_000_000},
                      {"model": "mystery-model", "window": "today", "input": 5}]}
    snap = parse_payload(body, "cc")
    today = snap.spend_for("today")
    assert today is not None and today.estimated
    assert today.amount == pytest.approx(3.0 + 15.0)
    assert snap.meta["unpricedModels"] == "mystery-model"


def test_unpriced_only_usage_yields_no_spend_not_zero():
    snap = parse_payload({"usage": [{"model": "mystery", "window": "today", "input": 10}]}, "cc")
    assert snap.spend_for("today") is None


def test_explicit_spend_wins_over_usage_estimate():
    body = {"spend": [{"window": "today", "amount": 1.0}],
            "usage": [{"model": "claude-sonnet-4-5", "window": "today", "input": 1_000_000}]}
    assert parse_payload(body, "cc").spend_for("today").amount == 1.0


# ── aggregation ────────────────────────────────────────────────────────────


def _push(now, **kw):
    base = {"observedAt": now, "spend": [{"window": "today", "amount": kw.get("today", 1.0), "estimated": True}],
            "counters": [{"key": "total_tokens", "value": kw.get("tokens", 100)}],
            "quotas": [{"scope": "session", "label": "SESSION", "pct": kw.get("pct", 10)}]}
    return parse_payload(base, "cc", now=int(now))


def test_devices_sum_spend_and_counters_and_take_worst_quota():
    now = time.time()
    agg = aggregate("cc", "Claude Code", [("mac", _push(now, today=1.0, pct=10)),
                                          ("pc", _push(now, today=2.5, pct=40, tokens=50))], 180, now=now)
    assert agg.health == "ok"
    assert agg.spend_for("today").amount == 3.5 and agg.spend_for("today").estimated
    assert agg.counter_for("total_tokens").value == 150
    assert agg.quotas[0].pct == 40
    assert agg.meta["devices"] == 2


def test_dead_forwarder_reads_stale_not_ok():
    now = time.time()
    agg = aggregate("cc", "Claude Code", [("mac", _push(now - 600))], 180, now=now)
    if int(now - 600) // 86400 == int(now) // 86400:
        assert agg.health == "stale" and "no push received" in agg.error
        assert all(q.stale for q in agg.quotas)


def test_yesterdays_push_is_not_todays_spend():
    now = time.time()
    agg = aggregate("cc", "Claude Code", [("mac", _push(now - 2 * 86400))], 180, now=now)
    assert agg.health == "stale"
    assert agg.spend_for("today") is None


def test_no_devices_is_unconfigured():
    assert aggregate("cc", "Claude Code", [], 180).health == "unconfigured"
