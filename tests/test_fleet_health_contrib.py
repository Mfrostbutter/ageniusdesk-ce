"""Fleet Health contribution API: registry + aggregator.

The aggregator treats contributed rows as UNTRUSTED and must never 500 on a bad
source. These tests exercise validation, caps, detail_url security, cache TTL, and
degraded-not-fatal without standing up the FastAPI app.
"""

import asyncio

import pytest

from backend.modules.health import aggregator, registry


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


def _collect():
    return asyncio.run(aggregator.collect())


# ── registry ──────────────────────────────────────────────────────────────────

def test_register_and_unregister_provider():
    async def p():
        return []
    registry.register_source("core", p)
    assert [s.id for s in registry.all_sources()] == ["core"]
    registry.unregister("core")
    assert registry.all_sources() == []


def test_register_pull_source_and_unregister_module():
    registry.register_pull_source("proxmox", "fleet-health")
    ids = [s.id for s in registry.all_sources()]
    assert ids == ["module:proxmox"]
    registry.unregister_module("proxmox")
    assert registry.all_sources() == []


# ── happy path ────────────────────────────────────────────────────────────────

def test_provider_rows_flow_through():
    async def p():
        return [{
            "id": "pve", "kind": "cluster", "label": "Proxmox",
            "reachable": True, "status": "ok",
            "metrics": [{"label": "nodes", "value": 3}, {"label": "running", "value": 12}],
            "detail_url": "/modules/proxmox",
        }]
    registry.register_source("proxmox", p)
    out = _collect()
    assert out["summary"] == {"sources_total": 1, "sources_ok": 1, "sources_degraded": 0, "sources_down": 0}
    row = out["sources"][0]
    assert row["label"] == "Proxmox" and row["detail_url"] == "/modules/proxmox"
    assert len(row["metrics"]) == 2


def test_no_sources_is_empty_not_error():
    out = _collect()
    assert out["sources"] == []
    assert out["summary"]["sources_total"] == 0


# ── degraded-not-fatal ────────────────────────────────────────────────────────

def test_raising_source_becomes_down_row_not_exception():
    async def boom():
        raise RuntimeError("upstream on fire")
    registry.register_source("x", boom)
    out = _collect()   # must not raise
    assert len(out["sources"]) == 1
    assert out["sources"][0]["status"] == "down"
    assert out["sources"][0]["reachable"] is False
    assert "upstream on fire" in out["sources"][0]["error"]
    assert out["summary"]["sources_down"] == 1


def test_timeout_source_becomes_down(monkeypatch):
    monkeypatch.setattr(aggregator, "SOURCE_TIMEOUT", 0.05)

    async def slow():
        await asyncio.sleep(1.0)
        return []
    registry.register_source("slow", slow)
    out = _collect()
    assert out["sources"][0]["status"] == "down"


def test_one_bad_source_does_not_sink_a_good_one():
    async def good():
        return [{"id": "ok", "kind": "svc", "label": "Good", "reachable": True, "status": "ok"}]

    async def bad():
        raise ValueError("nope")
    registry.register_source("good", good)
    registry.register_source("bad", bad)
    out = _collect()
    by_status = sorted(r["status"] for r in out["sources"])
    assert by_status == ["down", "ok"]


# ── validation + caps ─────────────────────────────────────────────────────────

def test_malformed_rows_dropped():
    async def p():
        return [
            "not a dict",
            {"kind": "svc", "label": "no id"},                       # missing id
            {"id": "BAD ID", "kind": "svc", "label": "bad id"},      # id regex fail
            {"id": "ok", "kind": "svc"},                             # missing label
            {"id": "good", "kind": "svc", "label": "Keep", "reachable": True},
        ]
    registry.register_source("p", p)
    out = _collect()
    assert [r["id"] for r in out["sources"]] == ["good"]


def test_row_and_metric_caps_enforced():
    async def p():
        rows = []
        for i in range(50):   # > MAX_ROWS (32)
            rows.append({
                "id": f"r{i}", "kind": "svc", "label": f"R{i}", "reachable": True,
                "metrics": [{"label": f"m{j}", "value": j} for j in range(20)],  # > MAX_METRICS (8)
            })
        return rows
    registry.register_source("p", p)
    out = _collect()
    assert len(out["sources"]) == aggregator.MAX_ROWS
    assert all(len(r["metrics"]) <= aggregator.MAX_METRICS for r in out["sources"])


def test_detail_url_must_be_in_app_path():
    async def p():
        return [
            {"id": "a", "kind": "svc", "label": "ext", "reachable": True, "detail_url": "https://evil.example/x"},
            {"id": "b", "kind": "svc", "label": "js", "reachable": True, "detail_url": "javascript:alert(1)"},
            {"id": "c", "kind": "svc", "label": "proto-rel", "reachable": True, "detail_url": "//evil.example"},
            {"id": "d", "kind": "svc", "label": "ok", "reachable": True, "detail_url": "/modules/x"},
        ]
    registry.register_source("p", p)
    urls = {r["id"]: r["detail_url"] for r in _collect()["sources"]}
    assert urls == {"a": "", "b": "", "c": "", "d": "/modules/x"}


def test_non_numeric_and_bool_metric_values_filtered():
    async def p():
        return [{
            "id": "m", "kind": "svc", "label": "M", "reachable": True,
            "metrics": [
                {"label": "num", "value": 5},
                {"label": "str", "value": "12/34"},
                {"label": "bool", "value": True},        # dropped (bool is not a metric)
                {"label": "obj", "value": {"x": 1}},     # dropped
                {"nolabel": 1, "value": 3},              # dropped (no label)
            ],
        }]
    registry.register_source("p", p)
    metrics = _collect()["sources"][0]["metrics"]
    assert [m["label"] for m in metrics] == ["num", "str"]


def test_unreachable_forces_down_status():
    async def p():
        return [{"id": "z", "kind": "svc", "label": "Z", "reachable": False, "status": "ok"}]
    registry.register_source("p", p)
    assert _collect()["sources"][0]["status"] == "down"


# ── cache ─────────────────────────────────────────────────────────────────────

def test_ttl_cache_shares_one_fetch():
    calls = {"n": 0}

    async def p():
        calls["n"] += 1
        return [{"id": "c", "kind": "svc", "label": "C", "reachable": True}]
    registry.register_source("cached", p, ttl=60)
    _collect()
    _collect()
    assert calls["n"] == 1   # second collect served from cache


def test_expired_cache_refetches(monkeypatch):
    calls = {"n": 0}

    async def p():
        calls["n"] += 1
        return [{"id": "c", "kind": "svc", "label": "C", "reachable": True}]
    registry.register_source("cached", p, ttl=0)   # always expired
    _collect()
    _collect()
    assert calls["n"] == 2
