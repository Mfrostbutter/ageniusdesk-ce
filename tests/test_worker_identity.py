"""Trusted worker identity + host-side route policy for community routes.

A fake community module is registered at runtime and a route mounted on the
real app so the identity middleware (main.py -> identity.apply) runs exactly as
in production. Roles are simulated by patching auth_gate.current_user.
"""

import pytest
from fastapi import Request

import backend.auth_gate as auth_gate
from backend import module_registry
from backend.module_registry import ModuleManifest, RegistryEntry
from backend.modules._runtime import identity


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


@pytest.fixture
def fakemod(client):
    """Register `fakemod` as a community module with an echo route on the live app."""
    from backend.main import app

    manifest = ModuleManifest(
        id="fakemod", name="Fake", routes_prefix="/api/fakemod",
        routes=[{"pattern": "/api/fakemod/secret*", "methods": ["GET"], "min_role": "operator"}],
    )
    module_registry.register(RegistryEntry(manifest=manifest, status="loaded", source="community", path="x"))

    async def echo(request: Request):
        return {"headers": {k: v for k, v in request.headers.items() if k.startswith("x-agd-")}}

    from fastapi.routing import APIRoute

    before = list(app.router.routes)
    # Insert ahead of the SPA catch-all so the routes actually match.
    for path in ("/api/fakemod/echo", "/api/fakemod/secret"):
        app.router.routes.insert(0, APIRoute(path, echo, methods=["GET", "POST"]))
    yield client
    app.router.routes[:] = before
    module_registry.unregister("fakemod")


def test_anonymous_is_401_before_forwarding(fakemod):
    fakemod.cookies.clear()
    assert fakemod.get("/api/fakemod/echo").status_code == 401


def test_viewer_reads_but_cannot_mutate(fakemod, monkeypatch):
    _as_role(monkeypatch, "viewer")
    r = fakemod.get("/api/fakemod/echo")
    assert r.status_code == 200
    h = r.json()["headers"]
    assert h["x-agd-user"] == "viewer-user" and h["x-agd-role"] == "viewer"
    assert h["x-agd-user-id"] == "viewer-user" and h["x-agd-auth-source"] == "session"
    assert fakemod.post("/api/fakemod/echo").status_code == 403


def test_operator_mutates(fakemod, monkeypatch):
    _as_role(monkeypatch, "operator")
    r = fakemod.post("/api/fakemod/echo")
    assert r.status_code == 200 and r.json()["headers"]["x-agd-role"] == "operator"


def test_browser_cannot_spoof_actor_headers(fakemod, monkeypatch):
    """Acceptance criterion 5."""
    _as_role(monkeypatch, "viewer")
    r = fakemod.get("/api/fakemod/echo", headers={
        "X-AGD-User": "root", "X-AGD-User-Id": "0", "X-AGD-Role": "admin", "X-AGD-Anything": "x",
    })
    assert r.status_code == 200
    h = r.json()["headers"]
    assert h["x-agd-user"] == "viewer-user" and h["x-agd-role"] == "viewer"
    assert "x-agd-anything" not in h


def test_manifest_route_class_raises_floor(fakemod, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert fakemod.get("/api/fakemod/secret").status_code == 403
    _as_role(monkeypatch, "operator")
    assert fakemod.get("/api/fakemod/secret").status_code == 200


def test_open_install_gets_synthetic_admin(fakemod, monkeypatch):
    from backend.config import settings

    fakemod.cookies.clear()
    monkeypatch.setattr(settings, "agd_disable_login", True)
    monkeypatch.setattr(settings, "agd_require_auth", False)
    r = fakemod.post("/api/fakemod/echo")
    assert r.status_code == 200
    h = r.json()["headers"]
    assert h["x-agd-user"] == "anonymous" and h["x-agd-role"] == "admin" and h["x-agd-auth-source"] == "open"


def test_builtin_routes_untouched(client, monkeypatch):
    # The middleware only acts on registered COMMUNITY module prefixes.
    assert identity.community_module_id("/api/modules") is None
    assert identity.community_module_id("/api/nope/x") is None
    assert identity.community_module_id("/static/x") is None


def test_min_role_never_lowered():
    m = ModuleManifest(id="m", name="m", routes=[{"pattern": "/api/m/*", "methods": ["POST"], "min_role": "viewer"}])
    assert identity.min_role_for(m, "POST", "/api/m/x") == "operator"
    assert identity.min_role_for(m, "GET", "/api/m/x") == "viewer"
    assert identity.min_role_for(None, "DELETE", "/api/m/x") == "operator"
    m2 = ModuleManifest(id="m", name="m", routes=[{"pattern": "/api/m/admin/*", "min_role": "admin"}])
    assert identity.min_role_for(m2, "GET", "/api/m/admin/keys") == "admin"
    assert identity.min_role_for(m2, "GET", "/api/m/other") == "viewer"
