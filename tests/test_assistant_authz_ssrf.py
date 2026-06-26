"""Regression coverage for the final pre-release review findings.

#1 (MED) — /api/assistant/* had no role gate: a viewer could mutate agent
config, read masked keys, spend tokens, and reach the probe SSRF. The router now
has an operator floor with an admin bar on the config-write routes.

#2 (MED) — /test-creds (and the provider probes) fetched an operator-supplied
Ollama URL and reflected the response body, a viewer-reachable SSRF read into the
internal network / cloud metadata. The URL is now validated and the body is no
longer echoed.

Role checks are exercised by patching backend.auth_gate.current_user (the name
require_role and the internal-API middleware both resolve at call time), so we
get a viewer / operator / admin identity without standing up real sessions.
"""

import pytest

import backend.auth_gate as auth_gate
import backend.modules.assistant.providers as providers


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


# ── #1 RBAC: operator floor + admin bar ──────────────────────────────────────


def test_viewer_cannot_read_assistant_config(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.get("/api/assistant/config").status_code == 403


def test_operator_can_read_assistant_config(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    assert anon.get("/api/assistant/config").status_code == 200


def test_operator_cannot_write_jobs(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    # Config mutation requires admin even for an operator.
    assert anon.post("/api/assistant/jobs", json={"jobs": {}}).status_code == 403


def test_admin_can_write_jobs(anon, monkeypatch):
    _as_role(monkeypatch, "admin")
    assert anon.post("/api/assistant/jobs", json={"jobs": {}}).status_code != 403


# ── #2 SSRF: probe-URL guard ─────────────────────────────────────────────────


@pytest.mark.parametrize("blocked", [
    "http://169.254.169.254/",          # AWS/GCP/Azure metadata (link-local)
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0:11434",             # unspecified
    "ftp://example.com",                # non-http scheme
    "",                                 # empty
])
def test_probe_url_blocks_unsafe_targets(blocked):
    with pytest.raises(providers.UnsafeProbeURL):
        providers.assert_safe_probe_url(blocked)


@pytest.mark.parametrize("allowed", [
    "http://localhost:11434",           # loopback
    "http://127.0.0.1:11434",
    "http://10.0.0.5:11434",            # private LAN (legit remote Ollama)
    "http://192.168.1.20:11434/",       # trailing slash trimmed
])
def test_probe_url_allows_normal_ollama(allowed):
    out = providers.assert_safe_probe_url(allowed)
    assert not out.endswith("/")


def test_test_creds_blocks_metadata_and_does_not_reflect_body(anon, monkeypatch):
    # Even an admin (above the operator floor) cannot turn the probe into an
    # SSRF read of cloud metadata.
    _as_role(monkeypatch, "admin")
    r = anon.post(
        "/api/assistant/test-creds",
        json={"provider": "ollama", "ollama_url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"].startswith("Ollama URL not allowed")
    # No fetched content reflected back.
    assert "meta-data" not in body["error"].replace("169.254.169.254", "")
