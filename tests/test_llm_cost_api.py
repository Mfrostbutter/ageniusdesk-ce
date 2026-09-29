"""LLM Cost: HTTP API (sources, devices, ingest, attribution, pricing, settings)."""

import time

import pytest

from backend.modules.llm_cost import store
from tests._llm_cost_fixtures import *  # noqa: F401,F403


@pytest.fixture(autouse=True)
def _isolated_prices(tmp_path, monkeypatch):
    from backend import pricing

    monkeypatch.setattr(pricing, "PRICE_BOOK_FILE", tmp_path / "price_book.json")
    monkeypatch.setattr(pricing, "_cache", None)
    monkeypatch.setenv("AGD_PRICEBOOK_DISABLE_REFRESH", "1")


async def _device(client, name="macbook"):
    resp = await client.post("/api/llm-cost/devices", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _payload(**kw):
    return {"observedAt": int(time.time()), "displayName": "Claude Code",
            "quotas": [{"scope": "session", "label": "SESSION", "pct": kw.get("pct", 20)}],
            "counters": [{"key": "total_tokens", "value": kw.get("tokens", 1000)},
                         {"key": "tokens_per_hour", "value": 500}],
            "usage": [{"model": "claude-sonnet-4-5", "window": "today", "input": kw.get("input", 1_000_000)}],
            "meta": {"host": kw.get("host", "mac")}}


async def test_create_source_rejects_raw_keys(client):
    resp = await client.post("/api/llm-cost/sources", json={
        "type": "anthropic_admin", "display_name": "Org A", "options": {"secret_ref": "sk-ant-admin-RAW"}})
    assert resp.status_code == 400 and "raw keys" in resp.json()["detail"]
    assert await store.list_sources() == []


async def test_source_crud_and_secret_status(client, monkeypatch):
    monkeypatch.setenv("LLMC_ORG_A", "sk-ant-admin-x")
    resp = await client.post("/api/llm-cost/sources", json={
        "type": "anthropic_admin", "display_name": "Org A", "options": {"secret_ref": "$LLMC_ORG_A",
                                                                       "monthly_budget": "50", "bogus": 1}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "org-a" and "bogus" not in body["options"]
    assert body["options"]["monthly_budget"] == 50.0
    assert body["secrets"]["secret_ref"] == {"ref": "$LLMC_ORG_A", "resolved": True}
    assert "sk-ant" not in resp.text

    dup = await client.post("/api/llm-cost/sources", json={"type": "anthropic_admin", "display_name": "Org A"})
    assert dup.status_code == 409
    upd = await client.put("/api/llm-cost/sources/org-a", json={"enabled": False, "interval_sec": 1800})
    assert upd.json()["enabled"] is False and upd.json()["interval_sec"] == 1800
    listing = (await client.get("/api/llm-cost/sources")).json()
    assert [s["id"] for s in listing["sources"]] == ["org-a"]
    assert (await client.delete("/api/llm-cost/sources/org-a")).status_code == 200
    assert (await client.delete("/api/llm-cost/sources/org-a")).status_code == 404


async def test_unknown_and_demo_types(client, monkeypatch):
    monkeypatch.delenv("AGD_LLM_COST_DEMO", raising=False)
    assert (await client.post("/api/llm-cost/sources", json={"type": "nope"})).status_code == 400
    assert (await client.post("/api/llm-cost/sources", json={"type": "demo"})).status_code == 400
    monkeypatch.setenv("AGD_LLM_COST_DEMO", "1")
    assert (await client.post("/api/llm-cost/sources", json={"type": "demo"})).status_code == 200


async def test_device_token_shown_once_and_only_hash_stored(client, test_db):
    body = await _device(client)
    token = body["token"]
    assert token.startswith("agdlc_")
    assert token in body["install"]["macos"] and token in body["install"]["windows"]
    rows = await test_db.fetch("SELECT token_hash, token_prefix FROM llm_cost_devices")
    assert rows[0]["token_hash"] != token and token not in rows[0]["token_prefix"] + "x"
    listing = await client.get("/api/llm-cost/devices")
    assert token not in listing.text
    src = await store.get_source("claude-code")
    assert src["type"] == "push"


async def test_ingest_requires_a_valid_token(client):
    assert (await client.post("/api/llm-cost/ingest", json=_payload())).status_code == 401
    resp = await client.post("/api/llm-cost/ingest", json=_payload(),
                             headers={"Authorization": "Bearer agdlc_wrong"})
    assert resp.status_code == 401


async def test_ingest_ignores_the_dashboard_auth_gate(client, monkeypatch):
    body = await _device(client)
    from backend import auth_gate

    async def _nobody(_request):
        return None

    monkeypatch.setattr(auth_gate, "current_user", _nobody)
    assert (await client.get("/api/llm-cost/state")).status_code == 401
    resp = await client.post("/api/llm-cost/ingest", json=_payload(),
                             headers={"Authorization": f"Bearer {body['token']}"})
    assert resp.status_code == 200, resp.text


async def test_ingest_aggregates_devices_and_state_reflects_it(client):
    mac = await _device(client, "macbook")
    pc = await _device(client, "desktop")
    r1 = await client.post("/api/llm-cost/ingest", json=_payload(pct=20, host="mac"),
                           headers={"Authorization": f"Bearer {mac['token']}"})
    r2 = await client.post("/api/llm-cost/ingest", json=_payload(pct=60, tokens=500, host="pc"),
                           headers={"Authorization": f"Bearer {pc['token']}"})
    assert r1.json()["accepted"] == ["claude-code"] and r2.status_code == 200
    # a second push from the same device replaces, not adds
    await client.post("/api/llm-cost/ingest", json=_payload(pct=20, host="mac"),
                      headers={"Authorization": f"Bearer {mac['token']}"})
    state = (await client.get("/api/llm-cost/state")).json()
    src = next(s for s in state["sources"] if s["sourceId"] == "claude-code")
    assert src["health"] == "ok" and src["meta"]["devices"] == 2
    assert next(c for c in src["counters"] if c["key"] == "total_tokens")["value"] == 1500
    today = next(s for s in src["spend"] if s["window"] == "today")
    assert today["estimated"] and today["amount"] == pytest.approx(6.0)
    assert state["overview"]["worstQuota"]["pct"] == 60
    assert state["overview"]["burnPerHour"] == 1000
    card = (await client.get("/api/llm-cost/card")).json()
    assert card["spendToday"] == pytest.approx(6.0) and card["estimated"]["today"] is True
    detail = (await client.get("/api/llm-cost/sources/claude-code/detail")).json()
    assert detail["detail"]["models"][0]["model"] == "claude-sonnet-4-5"


async def test_ingest_rejects_foreign_source_and_revoked_token(client):
    body = await _device(client)
    auth = {"Authorization": f"Bearer {body['token']}"}
    bad = await client.post("/api/llm-cost/ingest", json={**_payload(), "providerId": "someone-else"}, headers=auth)
    assert bad.status_code == 422
    assert (await client.delete(f"/api/llm-cost/devices/{body['device']['id']}")).status_code == 200
    assert (await client.post("/api/llm-cost/ingest", json=_payload(), headers=auth)).status_code == 401


async def test_ingest_payload_bounded(client):
    body = await _device(client)
    resp = await client.post("/api/llm-cost/ingest", content=b"x" * (300 * 1024),
                             headers={"Authorization": f"Bearer {body['token']}"})
    assert resp.status_code == 413


async def test_history_heatmap_spend_daily_and_events(client):
    body = await _device(client)
    await client.post("/api/llm-cost/ingest", json=_payload(), headers={"Authorization": f"Bearer {body['token']}"})
    hist = (await client.get("/api/llm-cost/history", params={"source_id": "claude-code", "scope": "session",
                                                                "window": "today"})).json()
    assert hist["quota"][0]["pct"] == 20 and hist["spend"][0]["amount"] > 0
    heat = (await client.get("/api/llm-cost/heatmap", params={"tz_offset_min": -240})).json()
    assert heat["sourceId"] == "claude-code" and heat["days"]
    daily = (await client.get("/api/llm-cost/spend/daily")).json()
    assert daily["series"] and daily["series"][-1]["estimated"] is True
    assert (await client.get("/api/llm-cost/history", params={"source_id": "x"})).status_code == 400
    assert (await client.get("/api/llm-cost/events")).status_code == 200


async def _otel_table(db):
    """Observe's table (migration 009) when present; a compatible stand-in otherwise."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS otel_spans (id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL,"
        " span_id TEXT NOT NULL, parent_id TEXT NOT NULL DEFAULT '', instance_id TEXT NOT NULL DEFAULT '',"
        " workflow_id TEXT NOT NULL DEFAULT '', workflow_name TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',"
        " start_ns INTEGER NOT NULL DEFAULT 0, model TEXT, tokens_in INTEGER, tokens_out INTEGER,"
        " cost_usd REAL, cost_is_estimate INTEGER)")
    await db.execute("DELETE FROM otel_spans")


async def _fleet_run(run_id: str, agent_id: str, model: str, cost: float, tokens: int) -> None:
    """Agent Fleet runs live in their own database (data/agentfleet.db)."""
    import aiosqlite

    from backend.modules.agent_fleet import storage as fleet_storage

    fleet_storage.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(fleet_storage.DB_PATH)) as db:
        await db.executescript(fleet_storage._SCHEMA)
        await db.execute("DELETE FROM langgraph_runs")
        await db.execute(
            "INSERT INTO langgraph_runs (id, agent_id, model, total_cost, total_tokens, created_at)"
            " VALUES (?, ?, ?, ?, ?, datetime('now'))", (run_id, agent_id, model, cost, tokens))
        await db.commit()


async def test_attributed_without_observe_tables_is_honest(client, monkeypatch):
    from backend.modules.llm_cost import attributed

    real = attributed._exists

    async def _no_otel(db, table):
        return False if table == "otel_spans" else await real(db, table)

    monkeypatch.setattr(attributed, "_exists", _no_otel)
    await _fleet_run("r0", "ops-triage", "claude-haiku-4-5", 0.01, 100)
    resp = (await client.get("/api/llm-cost/attributed")).json()
    assert resp["otel"]["available"] is False and resp["otel"]["byWorkflow"] == []
    assert resp["internal"]["langgraph"]["available"] is True


async def test_attributed_rolls_up_spans_and_reconciles(client, test_db):
    await _otel_table(test_db)
    now_ns = int(time.time() * 1e9)
    rows = [
        ("t1", "root", "", "inst1", "wf1", "Daily digest", "workflow.execute", now_ns, None, None, None, None),
        ("t1", "c1", "root", "inst1", "", "", "llm", now_ns, "claude-sonnet-4-5", 1000, 100, 0.40),
        ("t1", "c2", "root", "inst1", "", "", "llm", now_ns, "gpt-4o", 10, 10, 0.10),
        ("t2", "root", "", "inst2", "wf2", "Other", "workflow.execute", now_ns, None, None, None, None),
        ("t2", "c3", "root", "inst2", "", "", "llm", now_ns, "claude-sonnet-4-5", 5, 5, None),
    ]
    for tr, sp, par, inst, wf, wfn, name, ns, model, tin, tout, cost in rows:
        await test_db.execute(
            "INSERT INTO otel_spans (trace_id, span_id, parent_id, instance_id, workflow_id, workflow_name, name,"
            " start_ns, model, tokens_in, tokens_out, cost_usd, cost_is_estimate) VALUES"
            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)", (tr, sp, par, inst, wf, wfn, name, ns, model, tin, tout, cost))
    await _fleet_run("r1", "error-triage", "claude-sonnet-4-5", 0.25, 900)
    from backend.modules.llm_cost.models import Snapshot, Spend

    await store.create_source("anthropic", "anthropic_admin", "Anthropic", {"secret_ref": "$X"})
    import datetime as dt

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    snap = Snapshot("anthropic", "Anthropic", int(time.time()), spend=(Spend("today", 2.0),),
                    detail={"trend": [{"date": today, "amount": 2.0}]}, source_type="anthropic_admin")
    await store.save_snapshot(snap, snap)

    resp = (await client.get("/api/llm-cost/attributed")).json()
    otel = resp["otel"]
    assert otel["available"] and otel["total"] == pytest.approx(0.5)
    wf = {w["workflowName"]: w for w in otel["byWorkflow"]}
    assert wf["Daily digest"]["cost"] == pytest.approx(0.5) and wf["Other"]["unpricedCalls"] == 1
    assert {m["key"] for m in otel["byModel"]} == {"claude-sonnet-4-5", "gpt-4o"}
    assert resp["internal"]["langgraph"]["rows"][0]["cost"] == 0.25
    rec = resp["reconciliation"]
    anth = next(r for r in rec if r["provider"] == "anthropic")
    assert anth["billed"] == 2.0 and anth["attributed"] == pytest.approx(0.65)
    assert anth["unattributed"] == pytest.approx(1.35)


async def test_attributed_is_fleet_wide(client, test_db, tmp_data_dir):
    """CE has no workspaces: attribution always covers every instance."""
    from backend.config import save_config

    save_config({"instances": [{"id": "inst1", "name": "one", "url": "http://a"},
                               {"id": "inst2", "name": "two", "url": "http://b"}]})
    await _otel_table(test_db)
    now_ns = int(time.time() * 1e9)
    for tr, inst in (("t1", "inst1"), ("t2", "inst2")):
        await test_db.execute(
            "INSERT INTO otel_spans (trace_id, instance_id, name, start_ns, model, cost_usd, span_id,"
            " cost_is_estimate) VALUES (?, ?, 'llm', ?, 'gpt-4o', 1.0, 's', 1)", (tr, inst, now_ns))
    fleet = (await client.get("/api/llm-cost/attributed", headers={"X-AGD-Workspace": "client-a"})).json()
    assert fleet["workspace"] == "all"
    assert {i["key"] for i in fleet["otel"]["byInstance"]} == {"inst1", "inst2"}
    assert {i["name"] for i in fleet["otel"]["byInstance"]} == {"one", "two"}


async def test_pricing_override_crud(client):
    status = (await client.get("/api/llm-cost/pricing")).json()
    assert status["bundled_models"] > 0
    resp = await client.put("/api/llm-cost/pricing/overrides", json={"model": "my-model", "in": 1.5, "out": 3})
    assert resp.json()["overrides"]["my-model"] == {"in": 1.5, "out": 3.0}
    look = (await client.get("/api/llm-cost/pricing/lookup", params={"model": "my-model"})).json()
    assert look["price"]["source"] == "override"
    assert (await client.delete("/api/llm-cost/pricing/overrides/my-model")).status_code == 200
    assert (await client.delete("/api/llm-cost/pricing/overrides/my-model")).status_code == 404


async def test_settings_validation_and_round_trip(client):
    base = (await client.get("/api/llm-cost/settings")).json()
    assert base["quota_warn_pct"] == 75.0 and base["mqtt"]["enabled"] is False
    bad = await client.put("/api/llm-cost/settings", json={**base, "quota_warn_pct": 95, "quota_critical_pct": 90})
    assert bad.status_code == 400
    raw = await client.put("/api/llm-cost/settings", json={**base, "mqtt": {"password_ref": "hunter2"}})
    assert raw.status_code == 400
    ok = await client.put("/api/llm-cost/settings", json={**base, "spend_daily_warn": 25,
                                                           "mqtt": {"host": "broker", "password_ref": "$MQTT_PW"}})
    assert ok.status_code == 200 and ok.json()["mqtt"]["host"] == "broker"
    assert (await client.get("/api/llm-cost/settings")).json()["spend_daily_warn"] == 25


async def test_forwarder_files_served_from_an_allowlist(client):
    resp = await client.get("/api/llm-cost/forwarder/llm-cost-forward.py")
    assert resp.status_code == 200 and "ClaudeLogTailer" in resp.text
    assert (await client.get("/api/llm-cost/forwarder/..%2Fpyproject.toml")).status_code == 404
    assert (await client.get("/api/llm-cost/forwarder/secrets.json")).status_code == 404


async def test_refresh_and_types(client):
    assert (await client.post("/api/llm-cost/refresh", json={})).json()["requested"] == "*"
    types = {t["type"] for t in (await client.get("/api/llm-cost/types")).json()["types"]}
    assert {"anthropic_admin", "openai_admin", "openrouter", "http_json", "push"} <= types
