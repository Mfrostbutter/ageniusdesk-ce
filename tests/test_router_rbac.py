"""RBAC regression coverage for the full-application review findings (#1-#4).

Several routers previously relied only on the fail-closed internal-API middleware
("some identity present") and attached no per-router role gate, so a viewer (the
lowest role) could reach write/secret surfaces:

  #1 n8n_credentials — mirror resolves $secrets and POSTs them to a chosen n8n
  #2 knowledge        — source/connector/instructions config writes + probe
  #3 messages         — delete / clear-all messages
  #4 themes           — save custom theme / set active theme (config write)

Roles are simulated by patching backend.auth_gate.current_user (same approach as
tests/test_assistant_authz_ssrf.py): require_role and the middleware both resolve
the name at call time. No real sessions/cookies, so CSRF is skipped (no cookie).
"""

import backend.auth_gate as auth_gate


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


# ── #1 n8n_credentials (router-level operator) ───────────────────────────────


def test_viewer_blocked_n8n_credentials(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    # The mirror endpoint is the secret-exfil surface; the router-level gate
    # blocks a viewer on every route, including this recon read.
    assert anon.get("/api/n8n-credentials/some-instance/mappings").status_code == 403
    assert anon.post("/api/n8n-credentials/some-instance/mirror", json={"items": []}).status_code == 403


def test_operator_allowed_n8n_credentials(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    # Unknown instance -> not 403 (the gate let the operator through).
    assert anon.get("/api/n8n-credentials/some-instance/mappings").status_code != 403


# ── #2 knowledge (router-level operator) ─────────────────────────────────────


def test_viewer_blocked_knowledge(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.get("/api/knowledge/sources").status_code == 403


def test_operator_allowed_knowledge(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    assert anon.get("/api/knowledge/sources").status_code != 403


# ── #3 messages (deletes gated; reads open) ──────────────────────────────────


def test_viewer_blocked_messages_delete(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.delete("/api/messages/1").status_code == 403
    assert anon.delete("/api/messages").status_code == 403


def test_viewer_can_read_messages(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.get("/api/messages").status_code != 403


def test_operator_allowed_messages_delete(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    assert anon.delete("/api/messages/999999").status_code != 403


# ── #4 themes (writes gated; reads open) ─────────────────────────────────────


def test_viewer_blocked_themes_write(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.post("/api/themes/active/dark").status_code == 403
    assert anon.post("/api/themes", json={"name": "x", "colors": {}}).status_code == 403


def test_viewer_can_read_themes(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.get("/api/themes").status_code != 403


def test_operator_allowed_themes_write(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    # 404 (no such theme) is fine; it proves the role gate let the operator past.
    assert anon.post("/api/themes/active/nonexistent-xyz").status_code != 403


# ── #5 convention sweep: writes gated on remaining routers; reads stay open ───


def test_viewer_blocked_insights_refresh_reads_open(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.post("/api/insights/refresh").status_code == 403
    assert anon.get("/api/insights").status_code != 403  # analytics read stays open


def test_viewer_blocked_observability_pricing_refresh(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.post("/api/otel/pricing/refresh").status_code == 403


def test_viewer_blocked_notes_writes_reads_open(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.post("/api/notes/reindex").status_code == 403
    assert anon.put("/api/notes/foo", json={"content": "x"}).status_code == 403
    assert anon.delete("/api/notes/foo").status_code == 403
    assert anon.get("/api/notes/tree").status_code != 403  # vault read stays open


def test_operator_allowed_notes_write(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    assert anon.put("/api/notes/rbac-test-note", json={"content": "hi"}).status_code != 403


def test_viewer_blocked_player(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    # Router-level operator on the Spotify integration.
    assert anon.post("/api/spotify/play").status_code == 403


# ── modules management: isolation tier flip + install/uninstall are operator ──


def test_viewer_blocked_modules_writes_reads_open(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    # A viewer must not weaken the isolation boundary or change the install set.
    assert anon.post("/api/modules/isolation", json={"mode": "in_process"}).status_code == 403
    assert anon.post("/api/modules/install", json={"repo": "x/y"}).status_code == 403
    assert anon.post("/api/modules/discover", json={"repo": "x/y"}).status_code == 403
    assert anon.delete("/api/modules/some-module").status_code == 403
    # Reads stay open (list + isolation status).
    assert anon.get("/api/modules").status_code != 403
    assert anon.get("/api/modules/isolation").status_code != 403


def test_operator_allowed_modules_isolation(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    # in_process is the default, so this write is harmless; not-403 proves the gate.
    assert anon.post("/api/modules/isolation", json={"mode": "in_process"}).status_code != 403
