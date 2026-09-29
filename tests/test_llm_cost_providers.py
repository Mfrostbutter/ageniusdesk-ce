"""LLM Cost: provider parsing against mocked APIs (respx)."""

import json

import httpx
import pytest
import respx

from backend.modules.llm_cost.providers import TYPES
from backend.modules.llm_cost.providers.anthropic_admin import API_ROOT as ANTHROPIC
from backend.modules.llm_cost.providers.anthropic_admin import cents_to_usd
from backend.modules.llm_cost.providers.base import ConfigError, Http, ProviderContext, ProviderError
from backend.modules.llm_cost.providers.claude_code_local import ClaudeLogTailer
from backend.modules.llm_cost.providers.http_json import HttpJsonProvider, dig
from backend.modules.llm_cost.providers.openai_admin import COMPLETIONS_URL, COSTS_URL
from backend.modules.llm_cost.providers.openrouter import ACTIVITY_URL, CREDITS_URL, KEY_URL, KEYS_URL, reset_epoch
from tests._llm_cost_fixtures import *  # noqa: F401,F403

SECRETS = {"$ADMIN": "sk-ant-admin01-x", "$OAI": "sk-admin-x", "$ORK": "sk-or-key", "$ORM": "sk-or-mgmt",
           "$JSONKEY": "k123"}


@pytest.fixture(autouse=True)
def _isolated_prices(tmp_path, monkeypatch):
    from backend import pricing

    monkeypatch.setattr(pricing, "PRICE_BOOK_FILE", tmp_path / "price_book.json")
    monkeypatch.setattr(pricing, "_cache", None)


def make(type_, options, secrets=SECRETS, interval=0):
    ctx = ProviderContext(source_id="s1", source_type=type_, display_name="S", options=options,
                          interval_sec=interval, secret=lambda ref: secrets.get(ref), http=Http())
    return TYPES[type_](ctx)


def _today():
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# ── base ───────────────────────────────────────────────────────────────────


def test_interval_floor_and_garbage_fallback():
    assert make("anthropic_admin", {"secret_ref": "$ADMIN"}, interval=5).interval_sec == 120
    assert make("anthropic_admin", {"secret_ref": "$ADMIN"}, interval=3600).interval_sec == 3600
    assert make("anthropic_admin", {"secret_ref": "$ADMIN"}).interval_sec == 900


def test_require_secret_names_what_to_set():
    p = make("anthropic_admin", {"secret_ref": "$MISSING"})
    with pytest.raises(ConfigError, match=r"\$MISSING"):
        p.validate()


def test_resolve_secret_uses_env_and_never_returns_unresolved_ref(monkeypatch, tmp_data_dir):
    from backend.modules.llm_cost.providers.base import resolve_secret

    monkeypatch.setenv("LLM_COST_TEST_KEY", "value-1")
    assert resolve_secret("$LLM_COST_TEST_KEY") == "value-1"
    assert resolve_secret("LLM_COST_TEST_KEY") == "value-1"
    assert resolve_secret("$LLM_COST_NOT_SET_ANYWHERE") is None
    assert resolve_secret("") is None


# ── anthropic ──────────────────────────────────────────────────────────────


def test_cost_amount_is_cents_as_a_string():
    assert float(cents_to_usd("123.78912")) == pytest.approx(1.2378912)
    assert float(cents_to_usd("garbage")) == 0.0


@respx.mock
async def test_anthropic_spend_tokens_models_and_budget():
    today = _today()
    cost = respx.get(f"{ANTHROPIC}/cost_report").mock(side_effect=lambda req: httpx.Response(200, json={
        "data": [{"starting_at": f"{today}T00:00:00Z", "results": [{"amount": "250.0", "currency": "USD"}]},
                 {"starting_at": "2000-01-01T00:00:00Z", "results": [{"amount": "100", "currency": "USD"}]}]
        if "group_by[]" not in str(req.url) else [], "has_more": False}))
    respx.get(f"{ANTHROPIC}/usage_report/messages").mock(side_effect=lambda req: httpx.Response(200, json={
        "data": [{"starting_at": f"{today}T00:00:00Z", "results": [
            {"model": "claude-sonnet-4-5", "uncached_input_tokens": 1_000_000, "output_tokens": 100_000,
             "cache_read_input_tokens": 10, "cache_creation": {"ephemeral_5m_input_tokens": 5,
                                                               "ephemeral_1h_input_tokens": 7}}]}]
        if req.url.params.get_list("group_by[]") == ["model"] else [], "has_more": False}))
    respx.get(f"{ANTHROPIC}/workspaces").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{ANTHROPIC}/api_keys").mock(return_value=httpx.Response(200, json={"data": []}))

    snap = await make("anthropic_admin", {"secret_ref": "$ADMIN", "monthly_budget": 10}).fetch()
    assert snap.spend_for("today").amount == pytest.approx(2.5)
    assert snap.spend_for("mtd").amount == pytest.approx(3.5)
    assert snap.counter_for("cache_write_tokens").value == 12
    assert snap.meta["topModel"] == "claude-sonnet-4-5"
    assert snap.quotas[0].pct == pytest.approx(35.0)
    rows = {(m["model"], m["window"]): m for m in snap.detail["models"]}
    assert rows[("claude-sonnet-4-5", "today")]["costBasis"] == "estimated"
    assert rows[("claude-sonnet-4-5", "mtd")]["cost"] == pytest.approx(3.0 + 1.5, abs=0.01)
    sent = cost.calls[0].request
    assert sent.headers["x-api-key"] == "sk-ant-admin01-x"
    assert sent.headers["anthropic-version"]


@respx.mock
async def test_anthropic_workspace_breakdown_actual_and_key_estimated():
    today = _today()

    def cost(req):
        if req.url.params.get("group_by[]") == "workspace_id":
            return httpx.Response(200, json={"data": [{"starting_at": f"{today}T00:00:00Z", "results": [
                {"workspace_id": "ws_1", "amount": "500"}]}]})
        return httpx.Response(200, json={"data": []})

    def usage(req):
        groups = req.url.params.get_list("group_by[]")
        if groups == ["api_key_id", "model"]:
            return httpx.Response(200, json={"data": [{"starting_at": today, "results": [
                {"api_key_id": "key_1", "model": "claude-haiku-4-5", "uncached_input_tokens": 1_000_000}]}]})
        return httpx.Response(200, json={"data": []})

    respx.get(f"{ANTHROPIC}/cost_report").mock(side_effect=cost)
    respx.get(f"{ANTHROPIC}/usage_report/messages").mock(side_effect=usage)
    respx.get(f"{ANTHROPIC}/workspaces").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "ws_1", "name": "Prod"}]}))
    respx.get(f"{ANTHROPIC}/api_keys").mock(return_value=httpx.Response(403, json={"error": {"message": "no"}}))
    snap = await make("anthropic_admin", {"secret_ref": "$ADMIN"}).fetch()
    ws = snap.detail["groups"]["workspace"][0]
    assert ws["name"] == "Prod" and ws["cost"] == 5.0 and ws["costBasis"] == "actual"
    key = snap.detail["groups"]["api_key"][0]
    assert key["costBasis"] == "estimated" and key["cost"] == pytest.approx(1.0)


@respx.mock
async def test_anthropic_oauth_token_uses_bearer():
    route = respx.get(f"{ANTHROPIC}/cost_report").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{ANTHROPIC}/usage_report/messages").mock(return_value=httpx.Response(200, json={"data": []}))
    await make("anthropic_admin", {"secret_ref": "$T", "include_breakdown": False},
               secrets={"$T": "oauth-token"}).fetch()
    assert route.calls[0].request.headers["authorization"] == "Bearer oauth-token"


@respx.mock
async def test_anthropic_http_error_surfaces_without_the_key():
    respx.get(f"{ANTHROPIC}/cost_report").mock(return_value=httpx.Response(401, json={
        "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    with pytest.raises(ProviderError) as exc:
        await make("anthropic_admin", {"secret_ref": "$ADMIN"}).fetch()
    assert "401" in str(exc.value) and "sk-ant" not in str(exc.value)


# ── openai ─────────────────────────────────────────────────────────────────


@respx.mock
async def test_openai_costs_in_dollars_and_cached_input_netted():
    import time as _t

    now = int(_t.time())
    respx.get(COSTS_URL).mock(side_effect=lambda req: httpx.Response(200, json={
        "data": [{"start_time": now - 60, "results": [{"amount": {"value": 1.25, "currency": "usd"}}]}]
        if "group_by[]" not in str(req.url) else []}))
    respx.get(COMPLETIONS_URL).mock(side_effect=lambda req: httpx.Response(200, json={
        "data": [{"start_time": now - 60, "results": [
            {"model": "gpt-4o", "input_tokens": 1_000_000, "input_cached_tokens": 400_000,
             "output_tokens": 0, "num_model_requests": 3}]}]
        if req.url.params.get_list("group_by[]") == ["model"] else []}))
    respx.get("https://api.openai.com/v1/organization/projects").mock(
        return_value=httpx.Response(200, json={"data": []}))
    snap = await make("openai_admin", {"secret_ref": "$OAI"}).fetch()
    assert snap.spend_for("today").amount == 1.25
    assert snap.spend_for("today").currency == "USD"
    assert snap.counter_for("requests").value == 3
    row = next(m for m in snap.detail["models"] if m["window"] == "mtd")
    assert row["inputTokens"] == 600_000
    assert row["cost"] == pytest.approx(0.6 * 2.5 + 0.4 * 1.25)


# ── openrouter ─────────────────────────────────────────────────────────────


def _key_payload(**extra):
    data = {"usage_daily": 1.0, "usage_weekly": 2.0, "usage_monthly": 3.0, "usage": 9.0, "label": "main",
            "byok_usage_daily": 50.0, **extra}
    return httpx.Response(200, json={"data": data})


@respx.mock
async def test_openrouter_key_scope_untouched_by_account_data():
    respx.get(KEY_URL).mock(return_value=_key_payload())
    respx.get(ACTIVITY_URL).mock(return_value=httpx.Response(200, json={"data": [
        {"date": "2026-09-20 00:00:00", "model": "a/x", "usage": 5.0, "requests": 2, "prompt_tokens": 10},
        {"date": "2026-09-22 00:00:00", "model": "b/y", "usage": 7.0, "requests": 1},
        "junk"]}))
    respx.get(CREDITS_URL).mock(return_value=httpx.Response(200, json={
        "data": {"total_credits": 100.0, "total_usage": 40.0}}))
    respx.get(KEYS_URL).mock(return_value=httpx.Response(200, json={"data": [
        {"name": "ci", "usage": 4.0, "limit": 10}, {"name": "dev", "usage": 8.0}]}))
    snap = await make("openrouter", {"secret_ref": "$ORK", "management_secret_ref": "$ORM"}).fetch()
    assert snap.spend_for("today").amount == 1.0
    assert snap.counter_for("account_spend_30d").value == 12.0
    assert snap.counter_for("account_spend_30d").window == "30d"
    assert snap.meta["activityThrough"] == "2026-09-22"
    assert snap.meta["accountTopModel"] == "b/y"
    credits = next(q for q in snap.quotas if q.label == "CREDITS")
    assert credits.pct == 40.0 and credits.money_remaining() == 60.0
    assert snap.detail["models"][0]["costBasis"] == "actual"
    assert [g["name"] for g in snap.detail["groups"]["key"]] == ["dev", "ci"]


@respx.mock
async def test_openrouter_byok_excluded_unless_asked():
    respx.get(KEY_URL).mock(return_value=_key_payload())
    snap = await make("openrouter", {"secret_ref": "$ORK"}).fetch()
    assert snap.spend_for("today").amount == 1.0
    snap = await make("openrouter", {"secret_ref": "$ORK", "include_byok": True}).fetch()
    assert snap.spend_for("today").amount == 51.0


@respx.mock
async def test_openrouter_without_management_key_requests_nothing_account_wide():
    respx.get(KEY_URL).mock(return_value=_key_payload(limit=20.0, limit_remaining=5.0, limit_reset="monthly"))
    activity = respx.get(ACTIVITY_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    snap = await make("openrouter", {"secret_ref": "$ORK", "management_secret_ref": "$UNSET"}).fetch()
    assert not activity.called
    limit = snap.quotas[0]
    assert limit.label == "KEY LIMIT" and limit.pct == 75.0 and limit.resets_at


@respx.mock
async def test_openrouter_management_key_never_used_for_key_endpoint():
    key = respx.get(KEY_URL).mock(return_value=_key_payload())
    respx.get(ACTIVITY_URL).mock(return_value=httpx.Response(500))
    respx.get(CREDITS_URL).mock(return_value=httpx.Response(500))
    respx.get(KEYS_URL).mock(return_value=httpx.Response(500))
    snap = await make("openrouter", {"secret_ref": "$ORK", "management_secret_ref": "$ORM"}).fetch()
    assert key.calls[0].request.headers["authorization"] == "Bearer sk-or-key"
    assert snap.health == "ok" and snap.spend_for("today").amount == 1.0


@respx.mock
async def test_openrouter_management_key_alone_works():
    respx.get(ACTIVITY_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(CREDITS_URL).mock(return_value=httpx.Response(200, json={
        "data": {"total_credits": 10.0, "total_usage": 1.0}}))
    respx.get(KEYS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    snap = await make("openrouter", {"secret_ref": "$UNSET", "management_secret_ref": "$ORM"}).fetch()
    assert snap.spend == () and snap.quotas[0].label == "CREDITS"


def test_openrouter_reset_projection():
    import datetime as dt

    now = dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc)
    assert reset_epoch("daily", now) == int(dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc).timestamp())
    assert reset_epoch("monthly", now) == int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp())
    assert reset_epoch("never", now) is None


# ── http_json ──────────────────────────────────────────────────────────────


def test_dig_walks_dicts_and_lists():
    assert dig({"a": [{"b": 3}]}, "a.0.b") == 3
    assert dig({"a": []}, "a.4") is None and dig({}, "") is None


def test_http_json_quota_from_limit_and_remaining_root_path_and_scale():
    p = make("http_json", {"url": "https://x.test/", "root_path": "data",
                           "quotas": [{"scope": "monthly", "label": "chars", "limit_path": "lim",
                                       "remaining_path": "rem", "unit": "requests"}],
                           "spend": [{"path": "cents", "window": "mtd", "scale": 0.01}],
                           "counters": [{"key": "calls", "path": "n", "unit": "requests"}]})
    snap = p.parse({"data": {"lim": 100, "rem": "25", "cents": "$1,250", "n": 4}})
    assert snap.quotas[0].pct == 75.0 and snap.quotas[0].label == "CHARS"
    assert snap.spend_for("mtd").amount == 12.5
    assert snap.counter_for("calls").value == 4


def test_http_json_auth_modes_place_the_key():
    base = {"url": "https://x.test/", "secret_ref": "$JSONKEY", "spend": [{"path": "a"}]}
    assert make("http_json", {**base, "auth": "bearer"}).auth_parts()[0]["Authorization"] == "Bearer k123"
    assert make("http_json", {**base, "auth": "header", "auth_header": "xi"}).auth_parts()[0]["xi"] == "k123"
    assert make("http_json", {**base, "auth": "query", "auth_query": "key"}).auth_parts()[1]["key"] == "k123"
    assert make("http_json", {**base, "auth": "basic"}).auth_parts()[0]["Authorization"].startswith("Basic ")


def test_http_json_validation():
    with pytest.raises(ConfigError):
        make("http_json", {"spend": [{"path": "a"}]}).validate()
    with pytest.raises(ConfigError):
        make("http_json", {"url": "https://x.test/"}).validate()
    with pytest.raises(ConfigError):
        make("http_json", {"url": "https://x.test/", "quotas": [{"scope": "hourly"}]}).validate()
    with pytest.raises(ProviderError):
        make("http_json", {"url": "https://x.test/", "spend": [{"path": "nope"}]}).parse({"a": 1})


def test_http_json_fetch_refuses_metadata_address():
    p = make("http_json", {"url": "http://169.254.169.254/latest", "spend": [{"path": "a"}]})
    assert isinstance(p, HttpJsonProvider)
    import asyncio

    with pytest.raises(ConfigError, match="refused"):
        asyncio.run(p.fetch())


# ── claude code tailer ─────────────────────────────────────────────────────


def _line(mid, rid, ts, model="claude-sonnet-4-5", **usage):
    return json.dumps({"timestamp": ts, "requestId": rid, "sessionId": "s1",
                       "message": {"id": mid, "model": model, "usage": usage}}) + "\n"


def test_tailer_dedupes_reads_incrementally_and_skips_junk(tmp_path):
    import datetime as dt
    import time as _t

    now = _t.time()
    ts = dt.datetime.fromtimestamp(now - 30, dt.timezone.utc).isoformat()
    log = tmp_path / "proj" / "a.jsonl"
    log.parent.mkdir()
    log.write_text(_line("m1", "r1", ts, input_tokens=100, output_tokens=10)
                   + _line("m1", "r1", ts, input_tokens=100, output_tokens=10)
                   + "{not json\n" + json.dumps({"message": "x"}) + "\n")
    tailer = ClaudeLogTailer(str(tmp_path))
    first = tailer.scan(now)
    counters = {c["key"]: c["value"] for c in first["counters"]}
    assert counters["total_tokens"] == 110 and counters["sessions"] == 1
    assert first["meta"]["backfill"] is True
    with log.open("a") as fh:
        fh.write(_line("m2", "r2", ts, input_tokens=5, cache_creation_input_tokens=7))
    second = tailer.scan(now)
    counters = {c["key"]: c["value"] for c in second["counters"]}
    assert counters["total_tokens"] == 122 and counters["cache_write_tokens"] == 7
    assert counters["tokens_per_hour"] == 122
    assert {r["window"] for r in second["usage"]} == {"today", "mtd"}


def test_tailer_excludes_usage_older_than_an_hour_from_burn(tmp_path):
    import datetime as dt
    import time as _t

    now = _t.time()
    old = dt.datetime.fromtimestamp(now - 7200, dt.timezone.utc).isoformat()
    (tmp_path / "a.jsonl").write_text(_line("m1", "r1", old, input_tokens=100))
    counters = {c["key"]: c["value"] for c in ClaudeLogTailer(str(tmp_path)).scan(now)["counters"]}
    assert counters["tokens_per_hour"] == 0


async def test_claude_code_local_missing_dir_is_config_error(tmp_path):
    p = make("claude_code_local", {"projects_dir": str(tmp_path / "missing")})
    with pytest.raises(ConfigError):
        p.validate()


async def test_claude_code_local_prices_through_the_price_book(tmp_path):
    import datetime as dt
    import time as _t

    ts = dt.datetime.fromtimestamp(_t.time() - 10, dt.timezone.utc).isoformat()
    (tmp_path / "a.jsonl").write_text(_line("m1", "r1", ts, input_tokens=1_000_000))
    snap = await make("claude_code_local", {"projects_dir": str(tmp_path)}).fetch()
    assert snap.spend_for("today").estimated
    assert snap.spend_for("today").amount == pytest.approx(3.0)
    assert snap.detail["models"]


def test_demo_hidden_unless_enabled(monkeypatch):
    from backend.modules.llm_cost.providers import available_types

    monkeypatch.delenv("AGD_LLM_COST_DEMO", raising=False)
    assert "demo" not in {t["type"] for t in available_types()}
    monkeypatch.setenv("AGD_LLM_COST_DEMO", "true")
    assert "demo" in {t["type"] for t in available_types()}
