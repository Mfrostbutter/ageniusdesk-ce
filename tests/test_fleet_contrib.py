"""Fleet Health contribution API.

A community module declares `contributes.fleet_health` (a subpath) and the host
pulls its rows through one authenticated in-process call, merging them into the
Fleet Health pane. These tests register fake community modules with live routes
on the real app so the collector runs exactly as in production: through the
identity middleware, the internal-api gate, and the internal-actor self-call.
"""

import contextlib

import pytest
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

import backend.auth_gate as auth_gate
from backend import module_registry
from backend.module_registry import ModuleManifest, RegistryEntry


@contextlib.contextmanager
def _fake_module(mod_id, handler, *, contributes="fleet-health", name="Fake"):
    """Register a community module and mount `handler` at /api/{id}/fleet-health.

    `contributes=None` registers a module that declares no contribution.
    """
    from backend.main import app

    kwargs = {}
    if contributes is not None:
        kwargs["contributes"] = {"fleet_health": contributes}
    manifest = ModuleManifest(id=mod_id, name=name, routes_prefix=f"/api/{mod_id}", **kwargs)
    module_registry.register(RegistryEntry(manifest=manifest, status="loaded", source="community", path="x"))

    before = list(app.router.routes)
    app.router.routes.insert(0, APIRoute(f"/api/{mod_id}/fleet-health", handler, methods=["GET"]))
    try:
        yield
    finally:
        app.router.routes[:] = before
        module_registry.unregister(mod_id)


@pytest.fixture
def viewer(monkeypatch):
    """Authenticate every request as a viewer (enough for read routes)."""
    async def _fake(_request):
        return {"username": "viewer-user", "user_id": "viewer-user", "source": "session", "role": "viewer", "email": None}
    monkeypatch.setattr(auth_gate, "current_user", _fake)


def _rows_handler(rows):
    async def _h(_request: Request):
        return {"rows": rows}
    return _h


_ROW = {
    "id": "demo:cluster",
    "label": "Demo Cluster",
    "reachable": True,
    "status": "ok",
    "metrics": [{"label": "nodes", "value": 4}],
    "detail_url": "/modules/demo",
}


def test_contribution_rows_reach_the_pane(client, viewer):
    """A declared contribution is fetched and its rows are stamped and merged."""
    with _fake_module("demo", _rows_handler([_ROW]), name="Demo"):
        r = client.get("/api/modules/fleet-health")
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["label"] == "Demo Cluster"
    assert row["module"] == "demo" and row["module_name"] == "Demo"
    assert body["modules"][0]["reachable"] is True


def test_module_without_contribution_is_skipped(client, viewer):
    with _fake_module("plain", _rows_handler([_ROW]), contributes=None):
        r = client.get("/api/modules/fleet-health")
    assert r.status_code == 200
    assert r.json()["rows"] == []
    assert r.json()["modules"] == []


def test_broken_contributor_degrades_not_fatal(client, viewer):
    """A contributor that 500s is reported unreachable; the pane still responds."""
    async def _boom(_request: Request):
        raise HTTPException(status_code=500, detail="upstream on fire")

    with _fake_module("broken", _boom):
        r = client.get("/api/modules/fleet-health")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert body["modules"][0]["reachable"] is False
    assert "500" in body["modules"][0]["error"]


def test_malformed_contribution_is_rejected(client, viewer):
    """A response without a rows list is not merged."""
    async def _bad(_request: Request):
        return {"not_rows": 1}

    with _fake_module("weird", _bad):
        r = client.get("/api/modules/fleet-health")
    body = r.json()
    assert body["rows"] == []
    assert body["modules"][0]["reachable"] is False
    assert body["modules"][0]["error"] == "malformed response"


def test_only_dict_rows_survive(client, viewer):
    with _fake_module("mixed", _rows_handler([_ROW, "junk", 42, {"label": "ok", "reachable": True}])):
        r = client.get("/api/modules/fleet-health")
    rows = r.json()["rows"]
    assert len(rows) == 2
    assert all(isinstance(x, dict) for x in rows)


def test_no_contributors_returns_empty(client, viewer):
    r = client.get("/api/modules/fleet-health")
    assert r.status_code == 200
    assert r.json() == {"modules": [], "rows": []}


def test_internal_actor_authorizes_the_nested_read(client, monkeypatch):
    """The collector's self-call succeeds even when the module route has no
    browser session of its own: the viewer session only reaches the outer
    /api/modules endpoint; the nested /api/{mod}/fleet-health read is authorized
    by the internal system actor, not a cookie."""
    async def _path_aware(request):
        if request.url.path.startswith("/api/modules"):
            return {"username": "viewer-user", "user_id": "viewer-user", "source": "session", "role": "viewer", "email": None}
        return None  # the module route sees no browser identity

    monkeypatch.setattr(auth_gate, "current_user", _path_aware)
    with _fake_module("internalonly", _rows_handler([_ROW])):
        r = client.get("/api/modules/fleet-health")
    assert r.status_code == 200
    body = r.json()
    assert body["modules"][0]["reachable"] is True
    assert len(body["rows"]) == 1


def test_internal_actor_is_not_settable_by_a_header(client, viewer):
    """A browser cannot claim the internal system actor: the X-AGD-* headers are
    stripped and replaced with the real caller's identity, never 'internal'."""
    async def _echo(request: Request):
        return {"rows": [], "seen": request.headers.get("x-agd-auth-source", "")}

    with _fake_module("spoof", _echo):
        r = client.get(
            "/api/spoof/fleet-health",
            headers={"X-AGD-Auth-Source": "internal", "X-AGD-Role": "admin"},
        )
    assert r.status_code == 200
    assert r.json()["seen"] == "session"
