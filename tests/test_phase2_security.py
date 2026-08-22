"""Phase 2 backend-security regression tests (bug-hunt remediation 2026-08-16).

Covers: BUG-009 (edit_instance SSRF + probe + blank-field), BUG-008 (TOTP
pending-secret state machine), BUG-021 (last-admin delete refusal), BUG-015
(MCP naming-profile fail-closed), BUG-032 (login dummy-hash timing parity),
BUG-018 (MCP secrets tool admin gate), BUG-031 (chunked-body 413).
"""

import importlib

import pytest

from backend import auth_gate
from backend.config import add_instance, get_instances, load_config, save_config
from backend.modules.assistant import mcp_client

n8n_router = importlib.import_module("backend.modules.n8n_proxy.router")
admin_router = importlib.import_module("backend.modules.admin.router")


# ── Shared helpers ───────────────────────────────────────────────────────────


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


_prior_active = ""


def _seed_instance(inst_id: str = "p2edit01") -> dict:
    config = load_config()
    config["instances"] = [i for i in config.get("instances", []) if i["id"] != inst_id]
    save_config(config)
    global _prior_active
    _prior_active = config.get("active_instance", "")
    add_instance({
        "id": inst_id,
        "name": "Phase2 Edit",
        "url": "http://localhost:5678",
        "api_key": "old-key-1234567890",
        "color": "#123456",
    })
    return next(i for i in get_instances() if i["id"] == inst_id)


def _cleanup(inst_id: str):
    config = load_config()
    config["instances"] = [i for i in config.get("instances", []) if i["id"] != inst_id]
    if config.get("active_instance") == inst_id:
        config["active_instance"] = _prior_active
    save_config(config)


def _put(inst_id, **fields):
    body = {
        "name": "Phase2 Edit",
        "url": "http://localhost:5678",
        "api_key": "old-key-1234567890",
        "color": "#123456",
    }
    body.update(fields)
    return body


# ── BUG-009: edit_instance guard ─────────────────────────────────────────────


def test_edit_rejects_blank_url_and_name(anon, monkeypatch):
    """BUG-009/034: a blank url or name in the PUT is a 400, not a save."""
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()
    try:
        body = _put(inst["id"], url="   ")
        assert anon.put(f"/api/n8n/instances/{inst['id']}", json=body).status_code == 400
        body = _put(inst["id"], name="   ")
        assert anon.put(f"/api/n8n/instances/{inst['id']}", json=body).status_code == 400
        body = _put(inst["id"], api_key="   ")
        assert anon.put(f"/api/n8n/instances/{inst['id']}", json=body).status_code == 400
    finally:
        _cleanup(inst["id"])


def test_edit_rejects_ssrf_url(anon, monkeypatch):
    """BUG-009: the edit URL runs through the outbound-URL guard."""
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()
    try:
        body = _put(inst["id"], url="http://169.254.169.254/latest/meta-data")
        r = anon.put(f"/api/n8n/instances/{inst['id']}", json=body)
        assert r.status_code == 400
        # Stored instance is untouched.
        after = next(i for i in get_instances() if i["id"] == inst["id"])
        assert after["url"] == inst["url"]
    finally:
        _cleanup(inst["id"])


def test_edit_failed_probe_leaves_instance_untouched(anon, monkeypatch):
    """BUG-009: changing URL or key re-probes; a failed probe is a 400 and the
    stored instance keeps its old values."""
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()
    probes = []

    async def fake_probe(url, api_key, verify=None):
        probes.append(url)
        return {"connected": False, "error_class": "http", "message": "refused"}

    monkeypatch.setattr(n8n_router.client, "test_connection_with", fake_probe)
    try:
        body = _put(inst["id"], url="http://localhost:9999", api_key="new-key-abcdef")
        r = anon.put(f"/api/n8n/instances/{inst['id']}", json=body)
        assert r.status_code == 400
        assert probes, "a changed URL/key must trigger a connectivity probe"
        after = next(i for i in get_instances() if i["id"] == inst["id"])
        assert after["url"] == inst["url"]
        assert after["api_key"] == inst["api_key"]
    finally:
        _cleanup(inst["id"])


def test_edit_unchanged_connection_skips_probe(anon, monkeypatch):
    """BUG-009: a metadata-only edit (name/color) does not re-probe."""
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()
    calls = []

    async def fake_probe(url, api_key, verify=None):
        calls.append(url)
        return {"connected": True, "error_class": "", "message": ""}

    monkeypatch.setattr(n8n_router.client, "test_connection_with", fake_probe)
    try:
        body = _put(inst["id"], name="Renamed Only")
        r = anon.put(f"/api/n8n/instances/{inst['id']}", json=body)
        assert r.status_code == 200
        assert calls == [], "no URL/key change must not re-probe"
    finally:
        _cleanup(inst["id"])


# ── BUG-021: last-admin delete refusal ───────────────────────────────────────


def test_delete_last_admin_refused(anon, monkeypatch):
    _as_role(monkeypatch, "admin")
    users = [
        {"username": "only-admin", "role": "admin", "display_name": "A"},
        {"username": "a-viewer", "role": "viewer", "display_name": "V"},
    ]
    monkeypatch.setattr(admin_router, "_load_users", lambda: [dict(u) for u in users])
    saved = []
    monkeypatch.setattr(admin_router, "_save_users", lambda u: saved.append(u))

    r = anon.delete("/api/admin/users/only-admin")
    assert r.status_code == 400
    assert saved == [], "the last admin must not be deleted"

    # A non-admin deletes fine.
    r2 = anon.delete("/api/admin/users/a-viewer")
    assert r2.status_code == 200
    assert saved and all(u["username"] != "a-viewer" for u in saved[-1])


def test_delete_admin_allowed_when_another_admin_remains(anon, monkeypatch):
    _as_role(monkeypatch, "admin")
    users = [
        {"username": "admin-one", "role": "admin"},
        {"username": "admin-two", "role": "admin"},
    ]
    monkeypatch.setattr(admin_router, "_load_users", lambda: [dict(u) for u in users])
    saved = []
    monkeypatch.setattr(admin_router, "_save_users", lambda u: saved.append(u))
    r = anon.delete("/api/admin/users/admin-one")
    assert r.status_code == 200
    assert saved[-1] == [{"username": "admin-two", "role": "admin"}]


# ── BUG-015: MCP naming profile fails closed ─────────────────────────────────


def _tool(name, annotations=None):
    return {"_mcp_tool_name": name, "_mcp_annotations": annotations or {}}


def test_n8n_profile_known_reads_and_writes():
    assert mcp_client.classify_read_only(_tool("search_nodes"), "n8n-mcp") is True
    assert mcp_client.classify_read_only(_tool("n8n_get_workflow"), "n8n-mcp") is True
    assert mcp_client.classify_read_only(_tool("n8n_create_workflow"), "n8n-mcp") is False
    assert mcp_client.classify_read_only(_tool("n8n_delete_workflow"), "n8n-mcp") is False


def test_n8n_profile_decoy_name_fails_closed():
    """BUG-015: a server-invented name that is not in the known read set must
    NOT be waved through as a read. It returns None so the caller gates it."""
    for decoy in ("n8n_run_sql", "exfiltrate_data", "harmless_lookup", "n8n_secret_thing"):
        assert mcp_client.classify_read_only(_tool(decoy), "n8n-mcp") is None, decoy


# ── BUG-008: TOTP re-enroll does not clobber a live 2FA secret ───────────────


def test_totp_reenroll_stages_without_clobbering(monkeypatch):
    """BUG-008: enrolling while 2FA is enabled stages a pending secret and leaves
    the live secret + enabled flag untouched, so an abandoned enrollment cannot
    silently turn 2FA off. Activation then swaps the pending secret in."""
    from backend.modules.auth import service

    store = [{
        "username": "u1",
        "totp": {"enabled": True, "secret_enc": service.encrypt_value("LIVE-SECRET"), "recovery_codes": ["x"]},
    }]
    monkeypatch.setattr(service, "load_users", lambda: [dict(u) for u in store])
    monkeypatch.setattr(service, "save_users", lambda users: store.__setitem__(slice(None), users))

    live_before = store[0]["totp"]["secret_enc"]
    new_secret, _uri = service.totp_enroll("u1")

    # Live secret and enabled flag survive; the new secret is only staged.
    block = store[0]["totp"]
    assert block["enabled"] is True
    assert block["secret_enc"] == live_before
    assert service.decrypt_value(block["pending_secret_enc"]) == new_secret

    # Activation swaps the staged secret into the live slot and drops staging.
    monkeypatch.setattr(service.totp, "verify_step", lambda secret, code: 42 if secret == new_secret else None)
    monkeypatch.setattr(service.totp, "generate_recovery_codes", lambda: ["r1", "r2"])
    monkeypatch.setattr(service.totp, "hash_recovery_code", lambda c: f"h:{c}")
    codes = service.totp_activate("u1", "000000")
    assert codes == ["r1", "r2"]
    block = store[0]["totp"]
    assert block["enabled"] is True
    assert service.decrypt_value(block["secret_enc"]) == new_secret
    assert "pending_secret_enc" not in block or not block["pending_secret_enc"]


def test_totp_abandoned_reenroll_leaves_2fa_on(monkeypatch):
    """BUG-008: staging then never activating must leave the original secret live."""
    from backend.modules.auth import service

    store = [{
        "username": "u1",
        "totp": {"enabled": True, "secret_enc": service.encrypt_value("LIVE-SECRET"), "recovery_codes": []},
    }]
    monkeypatch.setattr(service, "load_users", lambda: [dict(u) for u in store])
    monkeypatch.setattr(service, "save_users", lambda users: store.__setitem__(slice(None), users))

    service.totp_enroll("u1")  # stage, then walk away
    block = store[0]["totp"]
    assert block["enabled"] is True
    assert service.decrypt_value(block["secret_enc"]) == "LIVE-SECRET"


# ── BUG-018: MCP secrets-metadata tool requires admin ────────────────────────


async def test_mcp_secrets_tool_denies_operator(monkeypatch):
    """BUG-018: the transport gates operator+, but list_secrets_metadata mirrors
    the admin-only /api/admin/secrets, so it must additionally require admin."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from backend.modules.dashboard_mcp import server

    async def as_operator(_req):
        return {"username": "op", "role": "operator", "source": "session"}

    monkeypatch.setattr(server, "current_user", as_operator)
    monkeypatch.setattr(server, "mcp", SimpleNamespace(request_context=SimpleNamespace(request=object())))

    with pytest.raises(HTTPException) as exc:
        await server._require_admin_for_tool()
    assert exc.value.status_code == 403


async def test_mcp_secrets_tool_fails_closed_without_context(monkeypatch):
    """BUG-018: no resolvable request context is denied, not waved through."""

    from fastapi import HTTPException

    from backend.modules.dashboard_mcp import server

    # request_context access raises -> caught -> request None -> 403.
    class _Boom:
        @property
        def request_context(self):
            raise RuntimeError("no active MCP request")

    monkeypatch.setattr(server, "mcp", _Boom())
    with pytest.raises(HTTPException) as exc:
        await server._require_admin_for_tool()
    assert exc.value.status_code == 403


# ── BUG-031: chunked over-cap body is aborted with 413 ───────────────────────


async def test_chunked_body_over_cap_is_413(monkeypatch):
    """BUG-031: a Transfer-Encoding: chunked body (no Content-Length) is counted
    as it streams and aborted with 413 the moment the cap is crossed, instead of
    being buffered whole."""
    from starlette.requests import Request
    from starlette.responses import Response

    import backend.main as main

    monkeypatch.setattr(main.settings, "agd_max_request_bytes", 150)

    chunks = [b"x" * 100, b"x" * 100]  # 200 bytes total, over the 150 cap

    async def receive():
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/x",
             "headers": [(b"transfer-encoding", b"chunked")]}
    request = Request(scope, receive)

    async def call_next(req):
        await req.body()  # reads the guarded stream, which raises past the cap
        return Response("ok")

    resp = await main.limit_request_size(request, call_next)
    assert resp.status_code == 413


# ── BUG-032: login timing parity (work happens on the miss path) ─────────────


def test_login_unknown_user_still_hashes(anon, monkeypatch):
    """BUG-032: a login for a nonexistent user must run the PBKDF2 work so the
    response time does not reveal the miss. Asserted via the dummy-hash path,
    not wall clock."""
    from backend.modules.auth import service

    calls = []
    real = service.hashlib.pbkdf2_hmac

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(service.hashlib, "pbkdf2_hmac", spy)
    r = anon.post("/api/auth/login", json={"username": "ghost@nowhere.test", "password": "pw-123456"})
    assert r.status_code == 401
    assert calls, "the unknown-username path must still run the PBKDF2 verify"


def test_forgot_unknown_email_still_hashes(anon, monkeypatch):
    """BUG-032: /forgot with an unregistered email runs the same hash work as a
    registered one, keeping the timing shape constant."""
    from backend.modules.auth import service

    calls = []
    real = service.hashlib.pbkdf2_hmac

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(service.hashlib, "pbkdf2_hmac", spy)
    r = anon.post("/api/auth/login".replace("login", "forgot"), json={"email": "ghost@nowhere.test"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls, "the unregistered-email path must still run the PBKDF2 work"
