"""Phase 3 fleet-TLS regression tests (bug-hunt remediation 2026-08-16).

Covers: BUG-010 (per-instance TLS threaded through every per-instance client
construction), BUG-025 (observability probe had no verify= at all), BUG-033
(test_connection_with verify=None must fall back to the GLOBAL default, not
the active instance's override).

The must-pass direction: active instance tls_verify=False, target True — a
plaintext-credential POST to the target must go out with verify=True.
"""

import asyncio
import re
from pathlib import Path

import pytest

from backend.config import add_instance, get_instances, load_config, save_config
from backend.modules.n8n_promote import promote
from backend.modules.n8n_proxy import client as n8n_client
from backend.modules.observability import instance_map

BACKEND = Path(__file__).resolve().parent.parent / "backend"


# ── Instance seeding ─────────────────────────────────────────────────────────

_IDS = ("p3active", "p3target")
_prior_active = ""


def _seed_pair(active_tls: bool, target_tls: bool) -> tuple[dict, dict]:
    """Seed two instances; the first is made active. Returns (active, target)."""
    global _prior_active
    config = load_config()
    _prior_active = config.get("active_instance", "")
    config["instances"] = [i for i in config.get("instances", []) if i["id"] not in _IDS]
    save_config(config)
    add_instance({
        "id": "p3active", "name": "P3 Active", "url": "http://localhost:5678",
        "api_key": "active-key-1234567890", "color": "#111111", "tls_verify": active_tls,
    })
    add_instance({
        "id": "p3target", "name": "P3 Target", "url": "http://localhost:5679",
        "api_key": "target-key-1234567890", "color": "#222222", "tls_verify": target_tls,
    })
    config = load_config()
    config["active_instance"] = "p3active"
    save_config(config)
    insts = {i["id"]: i for i in get_instances()}
    return insts["p3active"], insts["p3target"]


def _cleanup():
    config = load_config()
    config["instances"] = [i for i in config.get("instances", []) if i["id"] not in _IDS]
    if config.get("active_instance") in _IDS:
        config["active_instance"] = _prior_active
    save_config(config)


@pytest.fixture
def pair():
    active, target = _seed_pair(active_tls=False, target_tls=True)
    yield active, target
    _cleanup()


# ── verify= capture stub ─────────────────────────────────────────────────────


class _Resp:
    status_code = 200
    text = "{}"

    def __init__(self, body=None):
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _ClientCapture:
    """Stand-in for httpx.AsyncClient that records the verify kwarg."""

    def __init__(self, captured: list, *args, **kwargs):
        captured.append(kwargs.get("verify", "<absent>"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        if "/executions" in url:
            return _Resp({"data": []})
        return _Resp({"data": [], "nextCursor": ""})

    async def post(self, url, **kwargs):
        return _Resp({"id": "cred-1", "name": "c"})

    async def delete(self, url, **kwargs):
        return _Resp({})


def _patch_httpx(monkeypatch, captured: list):
    def _factory(*args, **kwargs):
        return _ClientCapture(captured, *args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", _factory)


# ── BUG-010: per-instance sites resolve TLS against their target ─────────────


def test_instance_health_uses_target_tls(pair, monkeypatch):
    """Fleet health fans out to a NON-active instance; verify must follow it."""
    active, target = pair
    captured: list = []
    _patch_httpx(monkeypatch, captured)
    asyncio.run(n8n_client._instance_health(target))
    assert captured and all(v is True for v in captured)


def test_export_all_workflows_for_uses_target_tls(pair, monkeypatch):
    active, target = pair
    captured: list = []
    _patch_httpx(monkeypatch, captured)
    asyncio.run(n8n_client.export_all_workflows_for(target))
    assert captured and all(v is True for v in captured)


def test_promote_probe_uses_target_tls(pair, monkeypatch):
    active, target = pair
    captured: list = []
    _patch_httpx(monkeypatch, captured)
    asyncio.run(promote._probe_instance(target))
    assert captured and all(v is True for v in captured)


def test_observability_probe_uses_target_tls(pair, monkeypatch):
    """BUG-025: the probe previously constructed its client with no verify=."""
    active, target = pair
    captured: list = []
    _patch_httpx(monkeypatch, captured)
    asyncio.run(instance_map._probe_instance(target, "1", "1"))
    assert captured and all(v is True for v in captured)


def test_inverted_pair_honors_target_off(pair, monkeypatch):
    """Inverted: active verifies, target does not — target calls must not verify."""
    _cleanup()
    active, target = _seed_pair(active_tls=True, target_tls=False)
    try:
        captured: list = []
        _patch_httpx(monkeypatch, captured)
        asyncio.run(n8n_client._instance_health(target))
        asyncio.run(n8n_client.export_all_workflows_for(target))
        asyncio.run(promote._probe_instance(target))
        assert captured and all(v is False for v in captured)
    finally:
        _cleanup()


def test_provision_credential_posts_with_target_tls(pair, monkeypatch):
    """The must-pass case: plaintext secret POST to a verifying target while the
    active instance has verification off."""
    active, target = pair
    captured: list = []
    _patch_httpx(monkeypatch, captured)
    monkeypatch.setattr(promote, "_load_mirrors", lambda: {})
    monkeypatch.setattr(promote, "_assert_provision_allowed", lambda *a: None)
    monkeypatch.setattr(promote, "_schemas_for_instance", lambda tid: asyncio.sleep(0, result={}))
    monkeypatch.setattr(promote, "_resolve_secret", lambda name: "sekrit")
    monkeypatch.setattr(promote, "build_credential_payload", lambda *a, **k: {"name": "c", "type": "t", "data": {}})

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(promote, "_record_mirror", _noop)
    cred_id, _ = asyncio.run(promote._provision_credential(target, "$TEST", "httpBasicAuth"))
    assert cred_id == "cred-1"
    assert captured and all(v is True for v in captured)


# ── BUG-033: test_connection_with falls back to the GLOBAL default ───────────


def test_test_connection_with_none_uses_global_not_active(pair, monkeypatch):
    """Active instance has tls_verify=False; a verify=None probe of an unsaved
    URL must still use the global default (True), not borrow the active's."""
    captured: list = []
    _patch_httpx(monkeypatch, captured)
    asyncio.run(n8n_client.test_connection_with("http://localhost:5678", "k"))
    assert captured and captured[0] is True


def test_test_connection_with_explicit_false_honored(pair, monkeypatch):
    captured: list = []
    _patch_httpx(monkeypatch, captured)
    asyncio.run(n8n_client.test_connection_with("http://localhost:5678", "k", verify=False))
    assert captured and captured[0] is False


# ── Lint guard: the fixed sites must keep tls_verify_for_instance ────────────

# (relative path under backend/, function the fix landed in)
_FIXED_SITES = [
    ("modules/n8n_promote/promote.py", "_probe_instance"),
    ("modules/n8n_promote/promote.py", "_provision_credential"),
    ("modules/n8n_credentials/router.py", "mirror_to_instance"),
    ("modules/n8n_credentials/router.py", "unlink_mirror"),
    ("modules/n8n_credentials/mappings.py", "fetch_live_schemas"),
    ("modules/n8n_proxy/client.py", "_instance_health"),
    ("modules/n8n_proxy/client.py", "export_all_workflows_for"),
    ("modules/observability/instance_map.py", "_probe_instance"),
]


def _function_source(path: Path, func: str) -> str:
    """Extract one top-level function's source by indentation."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if re.match(rf"(async )?def {re.escape(func)}\(", ln))
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if ln and not ln[0].isspace() and not ln.startswith(("#", ")")):
            break
        out.append(ln)
    return "\n".join(out)


def test_fixed_sites_keep_per_instance_tls():
    """Regression guard: each fixed site must call tls_verify_for_instance."""
    for rel, func in _FIXED_SITES:
        src = _function_source(BACKEND / rel, func)
        assert "tls_verify_for_instance" in src, f"{rel}:{func} lost its per-instance TLS resolution"


def test_test_connection_with_uses_global_fallback():
    src = _function_source(BACKEND / "modules/n8n_proxy/client.py", "test_connection_with")
    assert "tls_verify() if verify is None" in src
    # Match the exact call form; a bare substring also matches inside tls_verify().
    assert not re.search(r"(?<![\w.])_verify\(\) if verify is None", src)
