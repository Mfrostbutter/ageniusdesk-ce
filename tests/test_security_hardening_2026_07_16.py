"""A2/A3/A4 — public API key scoping, egress control, ingest bounds.

Companion to test_assistant_tool_gate.py (A1). Each group states the exposure it
closes, because the value of these is entirely in the negative case: the fix is
correct only if the restricted thing is actually refused.
"""

import ipaddress
import json
from datetime import datetime, timedelta, timezone

import pytest

import backend.main as main
import backend.modules.public_api.auth as pub_auth
import backend.net as net
from backend.config import settings
from backend.modules.public_api import api_keys
from backend.ratelimit import TokenBucket


@pytest.fixture(autouse=True)
def _isolate_keys(monkeypatch, tmp_path):
    """Point the key store at a temp file and reset the limiter per test."""
    monkeypatch.setattr(api_keys, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(api_keys, "_API_KEYS_FILE", tmp_path / "api_keys.json")
    pub_auth._reset_limiter()
    yield
    pub_auth._reset_limiter()


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


# ── A2: public API keys ──────────────────────────────────────────────────────


def test_expired_key_is_rejected(anon):
    raw, _ = api_keys.create_api_key("ci", "read", expires_at=_iso(timedelta(seconds=-1)))
    r = anon.get("/api/v1/status", headers={"X-API-Key": raw})
    assert r.status_code == 401


def test_unexpired_key_works(anon):
    raw, _ = api_keys.create_api_key("ci", "read", expires_at=_iso(timedelta(days=1)))
    assert anon.get("/api/v1/status", headers={"X-API-Key": raw}).status_code == 200


def test_key_without_expiry_never_expires(anon):
    raw, _ = api_keys.create_api_key("ci", "read")
    assert anon.get("/api/v1/status", headers={"X-API-Key": raw}).status_code == 200


def test_unparseable_expiry_fails_closed():
    """A key whose expiry cannot be read must not become immortal."""
    assert api_keys.is_expired({"expires_at": "not-a-date"}) is True


def test_ip_allowlist_blocks_other_sources(anon):
    raw, _ = api_keys.create_api_key("ci", "read", allowed_ips=["10.9.9.0/24"])
    # TestClient presents as testclient/127.0.0.1, which is outside the range.
    assert anon.get("/api/v1/status", headers={"X-API-Key": raw}).status_code == 403


def test_ip_allowlist_admits_a_listed_source():
    rec = {"allowed_ips": [str(ipaddress.ip_network("10.9.9.0/24"))]}
    assert api_keys.ip_allowed(rec, "10.9.9.5") is True
    assert api_keys.ip_allowed(rec, "10.9.10.5") is False
    # No usable client address must not silently satisfy a scoped key.
    assert api_keys.ip_allowed(rec, "unknown") is False


def test_empty_ip_allowlist_admits_anything():
    assert api_keys.ip_allowed({}, "203.0.113.9") is True


def test_invalid_cidr_is_rejected_at_creation():
    with pytest.raises(ValueError):
        api_keys.create_api_key("ci", "read", allowed_ips=["not-a-cidr"])


def test_invalid_expiry_is_rejected_at_creation():
    with pytest.raises(ValueError):
        api_keys.create_api_key("ci", "read", expires_at="whenever")


def test_rate_limit_returns_429(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_public_api_rate", 2)
    pub_auth._reset_limiter()
    raw, _ = api_keys.create_api_key("ci", "read")
    h = {"X-API-Key": raw}
    assert anon.get("/api/v1/status", headers=h).status_code == 200
    assert anon.get("/api/v1/status", headers=h).status_code == 200
    r = anon.get("/api/v1/status", headers=h)
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"


def test_rate_limit_is_per_key(anon, monkeypatch):
    """One noisy key must not spend another key's budget."""
    monkeypatch.setattr(settings, "agd_public_api_rate", 1)
    pub_auth._reset_limiter()
    a, _ = api_keys.create_api_key("a", "read")
    b, _ = api_keys.create_api_key("b", "read")
    assert anon.get("/api/v1/status", headers={"X-API-Key": a}).status_code == 200
    assert anon.get("/api/v1/status", headers={"X-API-Key": a}).status_code == 429
    assert anon.get("/api/v1/status", headers={"X-API-Key": b}).status_code == 200


def test_rate_zero_disables_the_limiter(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_public_api_rate", 0)
    pub_auth._reset_limiter()
    raw, _ = api_keys.create_api_key("ci", "read")
    for _ in range(5):
        assert anon.get("/api/v1/status", headers={"X-API-Key": raw}).status_code == 200


def test_workflow_scoping_refuses_other_workflows():
    from fastapi import HTTPException

    key = {"id": "k1", "allowed_workflows": ["42"]}
    pub_auth.assert_resource_allowed(key, "allowed_workflows", "42")  # allowed
    with pytest.raises(HTTPException) as e:
        pub_auth.assert_resource_allowed(key, "allowed_workflows", "99")
    assert e.value.status_code == 403


def test_unscoped_key_reaches_any_workflow():
    pub_auth.assert_resource_allowed({"id": "k1"}, "allowed_workflows", "anything")


# ── A3: egress ───────────────────────────────────────────────────────────────


def test_egress_allowlist_blocks_outside_cidr(monkeypatch):
    monkeypatch.setattr(settings, "agd_egress_allow_cidrs", "10.9.9.0/24")
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("192.168.1.50", 5678)),
    ])
    with pytest.raises(net.UnsafeProbeURL):
        net.assert_safe_probe_url("http://n8n.internal:5678")


def test_egress_allowlist_admits_inside_cidr(monkeypatch):
    monkeypatch.setattr(settings, "agd_egress_allow_cidrs", "10.9.9.0/24")
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("10.9.9.5", 5678)),
    ])
    assert net.assert_safe_probe_url("http://n8n.internal:5678") == "http://n8n.internal:5678"


def test_no_allowlist_preserves_private_range_access(monkeypatch):
    """The default must keep working: self-hosted n8n lives on RFC1918."""
    monkeypatch.setattr(settings, "agd_egress_allow_cidrs", "")
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("192.168.1.50", 5678)),
    ])
    assert net.assert_safe_probe_url("http://n8n.lan:5678")


def test_allowlist_does_not_override_the_metadata_block(monkeypatch):
    """An operator cannot allowlist their way to cloud metadata."""
    monkeypatch.setattr(settings, "agd_egress_allow_cidrs", "169.254.0.0/16")
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("169.254.169.254", 80)),
    ])
    with pytest.raises(net.UnsafeProbeURL):
        net.assert_safe_probe_url("http://169.254.169.254/")


def test_invalid_cidr_entry_does_not_widen_egress(monkeypatch):
    """A typo must not be read as 'allow everything'."""
    monkeypatch.setattr(settings, "agd_egress_allow_cidrs", "garbage,10.9.9.0/24")
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("192.168.1.50", 5678)),
    ])
    with pytest.raises(net.UnsafeProbeURL):
        net.assert_safe_probe_url("http://n8n.lan:5678")


def test_per_instance_tls_verify_overrides_the_global(monkeypatch):
    monkeypatch.setenv("AGD_TLS_VERIFY", "true")
    assert net.tls_verify_for_instance({"tls_verify": False}) is False
    assert net.tls_verify_for_instance({"tls_verify": True}) is True
    # Absent key = follow the global, which is how every existing instance behaves.
    assert net.tls_verify_for_instance({}) is True
    assert net.tls_verify_for_instance(None) is True
    monkeypatch.setenv("AGD_TLS_VERIFY", "false")
    assert net.tls_verify_for_instance({}) is False
    # One self-signed box no longer costs the rest of the fleet its cert checks.
    assert net.tls_verify_for_instance({"tls_verify": True}) is True


def test_n8n_client_resolves_tls_per_active_instance(monkeypatch):
    import backend.modules.n8n_proxy.client as client

    monkeypatch.setenv("AGD_TLS_VERIFY", "true")
    monkeypatch.setattr(
        "backend.config.get_active_instance", lambda: {"id": "i1", "tls_verify": False}
    )
    assert client._verify() is False
    monkeypatch.setattr("backend.config.get_active_instance", lambda: {"id": "i2"})
    assert client._verify() is True


# ── A4: ingest bounds ────────────────────────────────────────────────────────


def test_webhook_ingest_is_rate_limited(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_ingest_rate", 3)
    main._ingest_limiter = None
    main._ingest_limiter_rate = None
    body = {"title": "t", "body": "b"}
    codes = [anon.post("/api/messages/webhook", json=body).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[-1] == 429


def test_ingest_rate_zero_disables_the_limiter(anon, monkeypatch):
    monkeypatch.setattr(settings, "agd_ingest_rate", 0)
    main._ingest_limiter = None
    main._ingest_limiter_rate = None
    for _ in range(5):
        assert anon.post("/api/messages/webhook", json={"title": "t"}).status_code == 200


def test_oversized_span_attributes_are_truncated_but_still_parse():
    from backend.modules.observability.ingest import _MAX_ATTRS_CHARS, _encode_attrs

    merged = {"n8n.node.name": "Webhook", "huge": "x" * 200_000, "count": 7}
    blob = _encode_attrs(merged)
    assert len(blob) <= _MAX_ATTRS_CHARS
    parsed = json.loads(blob)  # must still be valid JSON, not a chopped string
    assert parsed["_agd_truncated"] is True
    # The small scalars the UI and enrichers read by key survive.
    assert parsed["n8n.node.name"] == "Webhook"
    assert parsed["count"] == 7
    assert "huge" not in parsed


def test_normal_span_attributes_are_untouched():
    from backend.modules.observability.ingest import _encode_attrs

    merged = {"n8n.node.name": "Webhook", "n8n.workflow.id": "42"}
    assert json.loads(_encode_attrs(merged)) == merged


@pytest.mark.asyncio
async def test_prune_reserves_headroom_for_the_incoming_batch():
    """The row cap must be a ceiling, not a number the table oscillates above."""
    from backend.database import get_db
    from backend.modules.observability import storage

    db = await get_db()
    await db.execute("DELETE FROM otel_spans")
    rows = [{
        "trace_id": "t", "span_id": f"s{i}", "parent_id": "", "instance_id": "i",
        "workflow_id": "w", "workflow_name": "W", "execution_id": "e",
        "name": "node.execute", "kind": 1, "start_ns": i, "end_ns": i + 1,
        "status": "OK", "attributes_json": "{}",
    } for i in range(20)]
    await storage.insert_spans(rows)
    assert await storage.count_spans() == 20

    # Cap 10, batch of 4 incoming: prune to 6 so the post-insert total is 10.
    await storage.prune(retention_hours=0, max_spans=10, incoming=4)
    assert await storage.count_spans() == 6
    await db.execute("DELETE FROM otel_spans")
    await db.commit()


# ── Rate limiter itself ──────────────────────────────────────────────────────


def test_token_bucket_refills_over_time(monkeypatch):
    import backend.ratelimit as rl

    clock = {"t": 1000.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: clock["t"])
    b = TokenBucket(rate_per_min=60)  # 1/sec
    for _ in range(60):
        assert b.allow("k") is True
    assert b.allow("k") is False
    clock["t"] += 2.0  # two seconds -> two tokens back
    assert b.allow("k") is True
    assert b.allow("k") is True
    assert b.allow("k") is False


def test_token_bucket_keys_are_independent():
    b = TokenBucket(rate_per_min=1)
    assert b.allow("a") is True
    assert b.allow("a") is False
    assert b.allow("b") is True
