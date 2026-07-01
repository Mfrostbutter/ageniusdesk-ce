"""Regression coverage for the medium/low findings in the 2026-07-01 review.

Wave 1 (this file, extended per wave):
  #8  Dashboard MCP endpoint had no role floor — a viewer (or the static token)
      could reach vault writes + secret-name enumeration. The internal-API
      middleware now requires operator+ on the MCP prefix when no static token
      is presented.
  #11 Notes search snippet was rendered unescaped (stored XSS). Guarded by a
      source assertion that the snippet is escaped and only <mark> is restored.
  #13 Public API key hash lookup switched from `==` to hmac.compare_digest.
  #14 Music routes had no role granularity — a viewer could mutate config and
      read triggers.token (which self-authenticates /triggers/fire). Mutations
      and the token-exposing read are now operator+; other reads stay open.

Roles are simulated by patching backend.auth_gate.current_user (same approach as
tests/test_router_rbac.py): the middleware and require_role both resolve the name
at call time. No real sessions/cookies, so CSRF is skipped (no cookie).
"""

from pathlib import Path

import backend.auth_gate as auth_gate

_NOTES_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "views" / "notes.js"


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


# ── #8 Dashboard MCP role floor ──────────────────────────────────────────────


def test_viewer_blocked_dashboard_mcp(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    # The MCP tools include write_note/append_note and list_secrets_metadata.
    r = anon.post("/api/mcp-dashboard", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 403
    # The debug ping under the same prefix is gated too.
    assert anon.get("/api/mcp-dashboard/_meta/ping").status_code == 403


def test_operator_allowed_dashboard_mcp(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    # Not 403 proves the middleware let the operator through to the MCP app
    # (the transport itself may answer 400/406 for a bare request; that's fine).
    assert anon.get("/api/mcp-dashboard/_meta/ping").status_code != 403


def test_static_token_bypasses_role_floor(anon, monkeypatch):
    # A configured machine token is a deliberate grant: it reaches MCP with no
    # identity at all. Patch the env the middleware reads and send the bearer.
    monkeypatch.setenv("DASHBOARD_MCP_TOKEN", "secret-token")

    async def _no_user(_request):
        return None

    monkeypatch.setattr(auth_gate, "current_user", _no_user)
    r = anon.get("/api/mcp-dashboard/_meta/ping", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code not in (401, 403)
    # A wrong token falls through to the (absent) identity -> blocked.
    bad = anon.get("/api/mcp-dashboard/_meta/ping", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code in (401, 403)


# ── #13 API key hash compare (constant-time) ─────────────────────────────────


def test_api_key_lookup_by_hash_roundtrip():
    from backend.modules.public_api import api_keys

    original = api_keys.load_api_keys()
    try:
        raw, record = api_keys.create_api_key("wave1-test", "read")
        import hashlib

        h = hashlib.sha256(raw.encode()).hexdigest()
        found = api_keys.lookup_by_hash(h)
        assert found is not None and found["id"] == record["id"]
        assert api_keys.lookup_by_hash("0" * 64) is None
    finally:
        api_keys.save_api_keys(original)


# ── #14 Music route role granularity ─────────────────────────────────────────


def test_viewer_blocked_music_mutations_and_token(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    # Mutations
    assert anon.put("/api/music/config", json={"history_cap": 50}).status_code == 403
    assert anon.post("/api/music/vibes", json={"name": "x", "urls": []}).status_code == 403
    assert anon.delete("/api/music/history").status_code == 403
    assert anon.put("/api/music/triggers", json={"enabled": True}).status_code == 403
    assert anon.post("/api/music/triggers/token/rotate").status_code == 403
    # The token-exposing read (drives the self-authenticating /fire endpoint).
    assert anon.get("/api/music/triggers").status_code == 403


def test_viewer_can_read_music(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.get("/api/music/config").status_code != 403
    assert anon.get("/api/music/vibes").status_code != 403
    assert anon.get("/api/music/history").status_code != 403


def test_operator_allowed_music(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    assert anon.put("/api/music/config", json={"history_cap": 50}).status_code != 403
    assert anon.get("/api/music/triggers").status_code != 403


# ── #11 Notes snippet escaping (source guard) ────────────────────────────────


def test_notes_snippet_is_escaped():
    src = _NOTES_JS.read_text(encoding="utf-8")
    # The raw interpolation `${item.snippet}` must be gone; it now runs through
    # escapeSnippet, which escapes everything then restores only <mark>.
    assert "${item.snippet}" not in src
    assert "escapeSnippet(item.snippet)" in src
    assert "function escapeSnippet" in src
    # The restore must target the FTS5 highlight tags specifically.
    assert "&lt;mark&gt;" in src and "&lt;/mark&gt;" in src


# ── #5 inspect redacts secret env ────────────────────────────────────────────


def test_inspect_redacts_secret_env():
    from backend.modules.docker_mgr.router import _redact_inspect

    info = {
        "Config": {
            "Env": [
                "NODE_ENV=production",
                "TZ=UTC",
                "N8N_ENCRYPTION_KEY=super-secret",
                "POSTGRES_PASSWORD=hunter2",
                "SOME_TOKEN=abc123",
                "DB_DSN=postgres://user:pass@db:5432/app",
                "PLAIN_URL=http://example.com/health",
            ]
        }
    }
    env = dict(e.split("=", 1) for e in _redact_inspect(info)["Config"]["Env"])
    assert env["NODE_ENV"] == "production"
    assert env["TZ"] == "UTC"
    assert env["PLAIN_URL"] == "http://example.com/health"  # no inline creds
    assert env["N8N_ENCRYPTION_KEY"] == "<redacted>"
    assert env["POSTGRES_PASSWORD"] == "<redacted>"
    assert env["SOME_TOKEN"] == "<redacted>"
    assert env["DB_DSN"] == "<redacted>"  # scheme://user:pass@ connection string


# ── #9 community-template HostConfig allowlist ───────────────────────────────


def _write_template(dirpath, name, data):
    import json

    (dirpath / name).write_text(json.dumps(data), encoding="utf-8")


def test_unsafe_community_template_rejected(tmp_path, monkeypatch):
    from backend.modules.docker_mgr import templates

    monkeypatch.setattr(templates, "COMMUNITY_TEMPLATE_DIR", tmp_path)

    safe = {
        "id": "safe-tpl", "name": "Safe", "image": "nginx",
        "fields": [{"id": "port", "type": "number", "default": 8080}],
        "container_config": {"Image": "nginx", "HostConfig": {"PortBindings": {}}},
    }
    privileged = {
        "id": "priv-tpl", "name": "Privileged", "image": "nginx",
        "container_config": {"Image": "nginx", "HostConfig": {"Privileged": True}},
    }
    host_bind = {
        "id": "bind-tpl", "name": "HostBind", "image": "nginx",
        "container_config": {"Image": "nginx", "HostConfig": {"Binds": ["/:/host:rw"]}},
    }
    pid_host = {
        "id": "pid-tpl", "name": "PidHost", "image": "nginx",
        "container_config": {"Image": "nginx", "HostConfig": {"PidMode": "host"}},
    }
    _write_template(tmp_path, "safe.json", safe)
    _write_template(tmp_path, "priv.json", privileged)
    _write_template(tmp_path, "bind.json", host_bind)
    _write_template(tmp_path, "pid.json", pid_host)

    loaded = {t.id for t in templates.load_community_templates()}
    assert "safe-tpl" in loaded
    assert "priv-tpl" not in loaded
    assert "bind-tpl" not in loaded
    assert "pid-tpl" not in loaded


def test_unsafe_bundle_entry_rejected(tmp_path, monkeypatch):
    from backend.modules.docker_mgr import templates

    monkeypatch.setattr(templates, "COMMUNITY_TEMPLATE_DIR", tmp_path)
    bundle = {
        "id": "bundle-tpl", "name": "Bundle", "image": "",
        "containers": [
            {"name": "web", "config": {"Image": "nginx"}, "role": "service"},
            {"name": "bad", "config": {"Image": "x", "HostConfig": {"Privileged": True}}},
        ],
    }
    _write_template(tmp_path, "bundle.json", bundle)
    loaded = {t.id for t in templates.load_community_templates()}
    assert "bundle-tpl" not in loaded
