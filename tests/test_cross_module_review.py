"""Regression coverage for the 2026-07-01 CROSS-MODULE review remediation.

Findings addressed here (see docs/code-review/2026-07-01-cross-module-review.md):

  C1/S1 — knowledge qdrant fetch now runs the shared SSRF guard and no longer
          reflects the target's body on error.
  C1/S2 — every n8n connect path (create / setup / test-creds) is guarded at the
          shared chokepoint client.test_connection_with.
  S3    — the TOTP second factor is rate-limited on the same counter as the
          password step; the password step no longer resets the counter when 2FA
          is enabled, so a wrong-code loop trips the lockout.
  C2    — providers / rag / knowledge outbound clients honor AGD_TLS_VERIFY.
  C3    — the modules router uses require_role("viewer") (reads open to viewer,
          mutations still operator).
  C4    — the music trigger token compare is constant-time.

Roles are simulated by patching backend.auth_gate.current_user (same approach as
tests/test_router_rbac.py).
"""

import asyncio
from pathlib import Path

import pytest

import backend.auth_gate as auth_gate

_SRC = Path(__file__).resolve().parent.parent / "backend"


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


# ── C1/S1 knowledge qdrant SSRF guard ────────────────────────────────────────


@pytest.mark.parametrize("blocked", [
    "http://169.254.169.254/",          # cloud metadata (link-local)
    "http://[fe80::1]:6333",            # IPv6 link-local
    "http://0.0.0.0:6333",              # unspecified
])
def test_knowledge_qdrant_blocks_unsafe_url(blocked):
    from backend.modules.knowledge import backends

    src = {"kind": "qdrant", "config": {"url": blocked, "collection": "c"}}
    res = asyncio.run(backends._search_qdrant(src, "q", 1))
    assert res["results"] == []
    assert "blocked" in res["error"]


def test_knowledge_qdrant_error_does_not_reflect_body():
    # The status-only error format must not carry the target response body, so a
    # guarded-but-reachable host can't be turned into a read primitive.
    src = (_SRC / "modules" / "knowledge" / "backends.py").read_text(encoding="utf-8")
    assert 'error": f"qdrant HTTP {r.status_code}"' in src
    assert "r.text[:200]" not in src.split("_search_qdrant")[1].split("DISPATCH")[0]


# ── C1/S2 n8n connect chokepoint SSRF guard ──────────────────────────────────


def test_n8n_connect_blocks_metadata():
    from backend.modules.n8n_proxy import client

    res = asyncio.run(client.test_connection_with("http://169.254.169.254/latest/meta-data/", "k"))
    assert res["connected"] is False
    assert res["error_class"] == "blocked"


def test_n8n_connect_allows_lan():
    from backend.modules.n8n_proxy import client

    # Loopback is allowed past the SSRF floor (n8n self-hosts on LAN); the connect
    # itself fails, but not with the "blocked" class.
    res = asyncio.run(client.test_connection_with("http://127.0.0.1:5678", "k"))
    assert res.get("error_class") != "blocked"


# ── C2 outbound clients honor AGD_TLS_VERIFY ─────────────────────────────────


def test_tls_verify_reads_env(monkeypatch):
    from backend import net

    monkeypatch.setenv("AGD_TLS_VERIFY", "false")
    assert net.tls_verify() is False
    monkeypatch.setenv("AGD_TLS_VERIFY", "true")
    assert net.tls_verify() is True
    monkeypatch.delenv("AGD_TLS_VERIFY", raising=False)
    assert net.tls_verify() is True  # default on


@pytest.mark.parametrize("rel", [
    "modules/assistant/providers.py",
    "modules/assistant/rag.py",
    "modules/knowledge/backends.py",
])
def test_outbound_modules_thread_tls_verify(rel):
    src = (_SRC / rel).read_text(encoding="utf-8")
    assert "tls_verify" in src
    # No bare client left that ignores the flag.
    assert "httpx.AsyncClient(timeout=" not in src


# ── C3 modules router primitive (viewer read, operator mutate) ───────────────


def test_viewer_can_read_modules(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.get("/api/modules").status_code != 403


def test_viewer_blocked_module_mutations(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.post("/api/modules/discover", json={"repo": "a/b", "ref": "main"}).status_code == 403
    assert anon.post("/api/modules/install", json={
        "repo": "a/b", "ref": "main", "resolved_sha": "", "consent": {},
    }).status_code == 403


# ── C4 music trigger token constant-time compare ─────────────────────────────


def test_music_trigger_uses_constant_time_compare():
    src = (_SRC / "modules" / "player" / "music_router.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest(supplied, expected)" in src
    assert "supplied != expected" not in src


# ── S3 TOTP second factor is rate-limited ────────────────────────────────────


def test_totp_second_factor_locks_out_after_repeated_failures(anon):
    from backend import config, totp
    from backend.config import settings
    from backend.modules.auth import service

    original = service.load_users()
    uname = "s3-totp-throttle"
    ip = "testclient"
    try:
        service.throttle_reset(uname, ip)
        secret = totp.generate_secret()
        users = [u for u in original if u.get("username") != uname]
        users.append({
            "username": uname,
            "role": "admin",
            "display_name": "",
            **service.hash_password("Correct-Horse-1!"),
            "totp": {"enabled": True, "secret_enc": config.encrypt_value(secret), "recovery_codes": []},
        })
        service.save_users(users)

        saw_lockout = False
        # Each round: correct password -> pending (must NOT reset throttle when 2FA
        # is on), then a wrong code -> recorded failure. After max_attempts the
        # counter trips and further attempts are 429.
        for _ in range(settings.agd_login_max_attempts + 2):
            r = anon.post("/api/auth/login", json={"username": uname, "password": "Correct-Horse-1!"})
            if r.status_code == 429:
                saw_lockout = True
                break
            assert r.json().get("totp_required") is True
            token = r.json()["pending_token"]
            rr = anon.post("/api/auth/login/totp", json={"pending_token": token, "code": "000000"})
            if rr.status_code == 429:
                saw_lockout = True
                break
            assert rr.status_code == 401  # wrong code rejected
        assert saw_lockout, "second-factor failures never tripped the lockout"
    finally:
        service.throttle_reset(uname, ip)
        service.save_users(original)


def test_password_step_defers_throttle_reset_when_2fa_enabled():
    # Source guard: the reset must not sit on the password-success path above the
    # totp branch (that was the S3 bug). It now lives after the non-2FA return.
    src = (_SRC / "modules" / "auth" / "router.py").read_text(encoding="utf-8")
    login = src.split("async def login(")[1].split("async def login_totp(")[0]
    reset_idx = login.index("throttle_reset(username, ip)")
    totp_idx = login.index("totp_required")
    assert reset_idx > totp_idx, "throttle_reset must come after the totp_required branch"
