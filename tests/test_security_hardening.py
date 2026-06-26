"""Regression coverage for the security-hardening pass (docs/code-review).

These lock in the behaviors that have no other safety net:

  * the blanket internal-API auth middleware (fail-closed allowlist),
  * edge-auth headers honored only when AGD_TRUST_EDGE_AUTH=true,
  * legacy webhook token enforcement,
  * theme- and JS-path traversal guards,
  * the no-account-enumeration property of password recovery.

The middleware is the highest-blast-radius change in the patch: a new pre-login
route added later that forgets the allowlist will 401 in prod and trip here
first.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend.config import settings

# ── Public allowlist ─────────────────────────────────────────────────────────


def test_status_is_public(anon):
    assert anon.get("/api/status").status_code == 200


def test_auth_status_is_public(anon):
    # The frontend hits this before any session exists to choose setup vs login.
    assert anon.get("/api/auth/status").status_code == 200


# ── Fail-closed default for internal API ─────────────────────────────────────


def test_private_route_blocked_without_identity(anon):
    r = anon.get("/api/themes")
    assert r.status_code == 401
    assert r.json()["detail"] == "Authentication required"


def test_admin_token_satisfies_gate(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_admin_token", "s3cret-admin-token")
    r = anon.get("/api/themes", headers={"Authorization": "Bearer s3cret-admin-token"})
    assert r.status_code != 401


def test_wrong_admin_token_still_blocked(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_admin_token", "s3cret-admin-token")
    r = anon.get("/api/themes", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


# ── Edge auth is opt-in ──────────────────────────────────────────────────────


def test_edge_headers_ignored_when_untrusted(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_trust_edge_auth", False)
    r = anon.get("/api/themes", headers={"Cf-Access-Authenticated-User-Email": "evil@example.com"})
    assert r.status_code == 401


def test_edge_headers_trusted_when_enabled(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_trust_edge_auth", True)
    r = anon.get("/api/themes", headers={"Cf-Access-Authenticated-User-Email": "ops@example.com"})
    assert r.status_code == 200


# ── Legacy webhook token enforcement ─────────────────────────────────────────


def test_webhook_open_when_no_token_configured(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_webhook_token", "")
    # Empty body may 422 on validation; the point is the gate did not 401 it.
    assert anon.post("/api/errors/webhook", json={}).status_code != 401


def test_webhook_blocked_without_token(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_webhook_token", "hook-token")
    r = anon.post("/api/errors/webhook", json={})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid or missing webhook token"


def test_webhook_accepts_bearer_token(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_webhook_token", "hook-token")
    r = anon.post("/api/errors/webhook", json={}, headers={"Authorization": "Bearer hook-token"})
    assert r.status_code != 401


def test_webhook_accepts_header_token(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_webhook_token", "hook-token")
    r = anon.post("/api/errors/webhook", json={}, headers={"X-AGD-Webhook-Token": "hook-token"})
    assert r.status_code != 401


# ── Theme path traversal ─────────────────────────────────────────────────────


def test_theme_id_rejects_traversal():
    import importlib

    # import_module, not `import x.y.router`: the themes package __init__ does
    # `from .router import router`, which shadows the submodule attribute with
    # the APIRouter object. sys.modules still holds the real module.
    themes = importlib.import_module("backend.modules.themes.router")

    with pytest.raises(HTTPException) as exc:
        themes._safe_theme_path(themes.CUSTOM_THEMES_DIR, "../../etc/passwd")
    assert exc.value.status_code == 400


def test_theme_name_is_slugified():
    import importlib

    # import_module, not `import x.y.router`: the themes package __init__ does
    # `from .router import router`, which shadows the submodule attribute with
    # the APIRouter object. sys.modules still holds the real module.
    themes = importlib.import_module("backend.modules.themes.router")

    theme_id = themes._theme_id_from_name("../../Evil Theme")
    assert "/" not in theme_id and ".." not in theme_id
    assert themes.THEME_ID_RE.fullmatch(theme_id)


def test_set_active_unknown_theme_404(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_admin_token", "s3cret-admin-token")
    r = anon.post(
        "/api/themes/active/does-not-exist",
        headers={"Authorization": "Bearer s3cret-admin-token"},
    )
    assert r.status_code == 404


# ── JS static path traversal ─────────────────────────────────────────────────


def test_serve_js_blocks_traversal():
    from backend.main import serve_js

    for evil in ("../config.py", "../../backend/config.py", "..%2f..%2fconfig.py"):
        assert asyncio.run(serve_js(evil)).status_code == 404


def test_serve_js_serves_real_module():
    from backend.main import serve_js

    resp = asyncio.run(serve_js("app.js"))
    assert resp.status_code == 200


def test_serve_js_rejects_non_js_suffix():
    from backend.main import serve_js

    # A real file under the frontend tree but not a .js must not be served here.
    assert asyncio.run(serve_js("../../README.md")).status_code == 404


# ── Password recovery: no account enumeration ────────────────────────────────


def test_forgot_password_does_not_enumerate(anon):
    # Establish a known account through the real setup endpoint.
    anon.post(
        "/api/auth/setup",
        json={"email": "owner@example.com", "password": "Fro5tbutt3r!"},
    )
    anon.cookies.clear()

    known = anon.post("/api/auth/forgot", json={"email": "owner@example.com"})
    unknown = anon.post("/api/auth/forgot", json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json() == {"ok": True}


def test_forgot_throttle_is_per_ip_and_isolated(monkeypatch):
    # The /forgot limiter must not lock a victim's login or every IP at once.
    from backend.modules.auth import service

    monkeypatch.setattr(settings, "agd_login_max_attempts", 3)
    ip = "203.0.113.42"  # unique so module-global throttle state doesn't collide

    for _ in range(3):
        assert service.forgot_blocked(ip) is False
        service.forgot_record(ip)
    assert service.forgot_blocked(ip) is True

    # Login throttle for a user from the same IP is untouched (separate namespace).
    assert service.throttle_blocked("victim@example.com", ip) is False
