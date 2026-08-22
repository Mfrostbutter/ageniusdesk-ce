"""Trace backfill, Phase 1 step 6 (preview + run endpoints).

Pager, coverage, and backfill_instance are monkeypatched stubs; no network.
Spec: docs/specs/2026-08-14-trace-backfill-from-execution-history.md.
"""

import asyncio
import importlib
import threading
from datetime import datetime, timedelta, timezone

import backend.auth_gate as auth_gate
from backend import websocket as ws_module
from backend.modules.observability import backfill, storage

# The package re-exports `router` (the APIRouter), so import the module directly.
obs_router = importlib.import_module("backend.modules.observability.router")

INSTANCE = "eb570e498824919b"
INST_DICT = {"id": INSTANCE, "name": "Test", "url": "http://n8n.test:5678", "api_key": "k"}

SUMMARY = {
    "scanned": 2, "backfilled": 2, "spans": 6, "skipped_traced": 0,
    "outside_retention": 0, "no_data": 0, "errors": 0,
}

OWNER = {"email": "owner@example.com", "password": "Fro5tbutt3r!"}


def _auth(client):
    """Establish (or recover) the owner session so gated endpoints are reachable."""
    client.cookies.clear()
    r = client.post("/api/auth/setup", json=OWNER)
    if r.status_code == 409:
        r = client.post(
            "/api/auth/login",
            json={"username": OWNER["email"], "password": OWNER["password"]},
        )
    assert r.status_code in (200, 201), r.text
    return client


def _csrf(client) -> dict:
    """Double-submit CSRF header echoing the agd_csrf cookie set at login."""
    return {"x-agd-csrf": client.cookies.get("agd_csrf", "")}


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _exec_row(exec_id: str, started: datetime, status: str = "success") -> dict:
    return {"id": exec_id, "status": status, "startedAt": _iso(started)}


def _stub_instance(monkeypatch):
    monkeypatch.setattr(obs_router, "get_instance_by_id", lambda iid: INST_DICT if iid == INSTANCE else None)
    monkeypatch.setattr(obs_router, "get_active_instance_id", lambda: INSTANCE)


COVERAGE = {
    "status": "degraded",
    "affected_workflows": [{"id": "wf-1", "name": "Order Sync"}],
    "reason": "1 workflow(s) set saveDataSuccessExecution=none",
}


def _stub_coverage(monkeypatch, result=None):
    async def fake(inst):
        assert inst is INST_DICT
        return dict(result or COVERAGE)

    monkeypatch.setattr(obs_router, "check_data_save_coverage", fake)


# ── GET /api/otel/backfill/preview ──────────────────────────────────────────


def test_preview_requires_auth(client):
    _auth(client)  # ensure a user exists so login is enforced
    client.cookies.clear()
    r = client.get("/api/otel/backfill/preview")
    assert r.status_code == 401


def test_preview_unknown_instance_404(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    _stub_instance(monkeypatch)
    r = anon.get("/api/otel/backfill/preview?instance_id=no-such-instance")
    assert r.status_code == 404


def test_preview_happy_path(anon, monkeypatch):
    """Counts classify one page walk correctly; coverage passes through verbatim."""
    _as_role(monkeypatch, "viewer")
    _stub_instance(monkeypatch)
    _stub_coverage(monkeypatch)

    now = datetime.now(timezone.utc)
    from backend.config import settings
    old = now - timedelta(hours=settings.agd_otel_retention_hours + 10)
    listing = [
        _exec_row("81001", now - timedelta(hours=1)),                       # rebuildable
        _exec_row("81002", now - timedelta(hours=1)),                       # real trace
        _exec_row("81003", now - timedelta(hours=2)),                       # backfill trace
        _exec_row("81004", now - timedelta(minutes=30), status="running"),  # not completed
        _exec_row("81005", old),                                            # outside retention
    ]
    pages = []

    async def fake_page(inst, cursor="", limit=100):
        pages.append(cursor)
        assert inst is INST_DICT
        return (listing, "")

    monkeypatch.setattr(backfill, "_list_executions_page", fake_page)

    traces = {"81002": "real-trace", "81003": "bf-trace"}
    real = {"real-trace": True, "bf-trace": False}

    async def fake_trace_for(execution_id, instance_id):
        assert instance_id == INSTANCE  # preview must scope the lookup
        return traces.get(execution_id, "")

    async def fake_has_real(trace_id):
        return real[trace_id]

    monkeypatch.setattr(storage, "trace_id_for_execution", fake_trace_for)
    monkeypatch.setattr(storage, "trace_has_real_spans", fake_has_real)

    r = anon.get("/api/otel/backfill/preview")  # no instance_id: falls to active
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["instance_id"] == INSTANCE
    assert body["instance_name"] == "Test"
    assert body["completed"] == 4
    assert body["already_traced"] == 1
    assert body["backfill_traced"] == 1
    assert body["outside_retention"] == 1
    assert body["rebuildable"] == 2  # a stale backfill trace stays rebuildable
    assert body["cap"] == settings.agd_backfill_max_executions
    assert body["retention_hours"] == settings.agd_otel_retention_hours
    assert body["coverage"] == COVERAGE
    assert pages == [""]  # one page walk, empty next-cursor ends it


# ── POST /api/otel/backfill/run ─────────────────────────────────────────────


def _stub_run(monkeypatch, calls=None):
    async def fake(instance_id, since="", until="", limit=0, detect_health=True, progress_cb=None):
        if calls is not None:
            calls.append({
                "instance_id": instance_id, "since": since, "until": until,
                "limit": limit, "detect_health": detect_health,
            })
        return dict(SUMMARY)

    monkeypatch.setattr(backfill, "backfill_instance", fake)


def test_run_viewer_blocked_operator_allowed(anon, monkeypatch):
    _stub_instance(monkeypatch)
    _stub_run(monkeypatch)
    _as_role(monkeypatch, "viewer")
    assert anon.post("/api/otel/backfill/run", json={}).status_code == 403
    _as_role(monkeypatch, "operator")
    assert anon.post("/api/otel/backfill/run", json={}).status_code == 200


def test_run_csrf_enforced_on_cookie_session(client, monkeypatch):
    """A cookie-authed mutation needs the double-submit header; with it, 200."""
    _stub_instance(monkeypatch)
    _stub_run(monkeypatch)
    _auth(client)
    r = client.post("/api/otel/backfill/run", json={})
    assert r.status_code == 403
    assert r.json()["detail"] == "CSRF check failed"
    r = client.post("/api/otel/backfill/run", json={}, headers=_csrf(client))
    assert r.status_code == 200, r.text


def test_run_returns_summary_verbatim(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    _stub_instance(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    r = anon.post("/api/otel/backfill/run", json={
        "since": "2026-08-13T18:00:00Z", "until": "2026-08-14T23:00:00Z",
        "limit": 40, "detect_health": False,
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"instance_id": INSTANCE, "summary": SUMMARY}
    assert calls == [{
        "instance_id": INSTANCE, "since": "2026-08-13T18:00:00Z",
        "until": "2026-08-14T23:00:00Z", "limit": 40, "detect_health": False,
    }]


def test_run_unknown_instance_404(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    _stub_instance(monkeypatch)
    r = anon.post("/api/otel/backfill/run", json={"instance_id": "no-such-instance"})
    assert r.status_code == 404


def test_run_concurrent_conflicts_409(anon, monkeypatch):
    """A second POST while one run is in flight answers 409, never queues."""
    _as_role(monkeypatch, "operator")
    _stub_instance(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    async def slow(instance_id, since="", until="", limit=0, detect_health=True, progress_cb=None):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return dict(SUMMARY)

    monkeypatch.setattr(backfill, "backfill_instance", slow)
    results = []
    t = threading.Thread(
        target=lambda: results.append(anon.post("/api/otel/backfill/run", json={}))
    )
    t.start()
    try:
        assert started.wait(5), "first run never started"
        r2 = anon.post("/api/otel/backfill/run", json={})
        assert r2.status_code == 409
    finally:
        release.set()
        t.join(timeout=10)
    assert results and results[0].status_code == 200


def test_run_broadcasts_progress_and_done(anon, monkeypatch):
    """progress_cb wiring: one backfill_progress per execution, one final backfill_done."""
    _as_role(monkeypatch, "operator")
    _stub_instance(monkeypatch)
    events = []

    async def fake_broadcast(event, data):
        events.append((event, data))

    monkeypatch.setattr(ws_module.manager, "broadcast", fake_broadcast)

    partial = {
        "scanned": 2, "backfilled": 1, "spans": 3, "skipped_traced": 0,
        "outside_retention": 0, "no_data": 0, "errors": 0,
    }

    async def fake(instance_id, since="", until="", limit=0, detect_health=True, progress_cb=None):
        assert progress_cb is not None
        await progress_cb(dict(partial))
        await progress_cb(dict(SUMMARY))
        return dict(SUMMARY)

    monkeypatch.setattr(backfill, "backfill_instance", fake)

    r = anon.post("/api/otel/backfill/run", json={})
    assert r.status_code == 200, r.text
    assert [e for e, _ in events] == ["backfill_progress", "backfill_progress", "backfill_done"]
    assert events[0][1] == {"instance_id": INSTANCE, **partial}
    assert events[1][1] == {"instance_id": INSTANCE, **SUMMARY}
    assert events[2][1] == {"instance_id": INSTANCE, "summary": SUMMARY}


async def test_backfill_instance_invokes_progress_cb(client, monkeypatch):
    """The range path awaits progress_cb with a running summary after each
    execution; the last call equals the returned summary."""
    now = datetime.now(timezone.utc)
    listing = [
        _exec_row("82001", now - timedelta(hours=1)),
        _exec_row("82002", now - timedelta(hours=2)),
    ]

    async def fake_page(inst, cursor="", limit=100):
        return (listing, "")

    monkeypatch.setattr(backfill, "_list_executions_page", fake_page)
    monkeypatch.setattr(backfill, "get_instance_by_id", lambda iid: INST_DICT)

    from backend.modules.n8n_proxy import client as n8n_client

    async def empty_fetch(execution_id, instance_id):
        return {}  # unfetchable: counts as no_data, no spans inserted

    monkeypatch.setattr(n8n_client, "get_execution_raw_by_instance", empty_fetch)

    calls = []

    async def cb(running):
        calls.append(running)

    summary = await backfill.backfill_instance(INSTANCE, progress_cb=cb)
    assert summary["scanned"] == 2
    assert summary["no_data"] == 2
    assert len(calls) == 2
    assert calls[-1] == summary
    assert calls[0] is not summary  # copies, not the live dict
