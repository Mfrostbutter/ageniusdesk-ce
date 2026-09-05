"""http.request host bridge: manifest model, effective endpoint store, policy,
auth injection, pinning, response shaping, and the bridge route.

The Proxmox community module is the acceptance fixture shape: a `header` auth
endpoint with a declared mutating method that the operator may reduce.
"""

import base64
import json
import logging

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from backend.module_registry import Capabilities, ModuleManifest
from backend.modules._runtime import bridge, endpoints, http_bridge

BASE = "https://pve.lab.example:8006/api2/json"


def _manifest(methods=("GET", "POST", "DELETE"), base_url=BASE, verify_tls=False, auth=True, **extra):
    caps = {
        "host": {"http": {"enabled": True, "endpoints": [{
            "id": "proxmox", "base_url": base_url, "methods": list(methods), "verify_tls": verify_tls,
            **({"auth": {"type": "header", "header": "Authorization", "secret_ref": "PROXMOX_TOKEN",
                         "format": "PVEAPIToken={value}"}} if auth else {}),
        }]}},
        **extra,
    }
    return ModuleManifest(id="proxmox", name="Proxmox", capabilities=Capabilities(**caps))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoints, "STORE_FILE", tmp_path / "module-endpoints.json")
    monkeypatch.setenv("PROXMOX_TOKEN", "root@pam!agd=deadbeef-secret")
    # Deterministic DNS: hostnames pin to a fixed lab address, literals to themselves.
    real = endpoints.resolve_ips
    monkeypatch.setattr(endpoints, "resolve_ips",
                        lambda h, p: real(h, p) if endpoints.is_ip_literal(h) else ["10.0.0.9"])
    http_bridge._resolve_cache.clear()
    yield endpoints


@pytest.fixture
def active(store):
    """An operator-confirmed revision with the full declared method set."""
    return store.seed(_manifest(), {"proxmox": {"methods": ["GET", "POST", "DELETE"]}}, consented_by="op")


@pytest.fixture
def bridge_client():
    client = TestClient(bridge.bridge_app)
    issued = []

    def _mint(manifest=None):
        caps = (manifest or _manifest()).capabilities
        tok = bridge.mint("proxmox", caps)
        issued.append(tok)
        return tok

    yield client, _mint
    for t in issued:
        bridge.revoke(t)


def _h(token):
    return {"authorization": f"Bearer {token}"}


# ── manifest model ────────────────────────────────────────────────────────────


def test_manifest_rejects_unknown_host_field():
    with pytest.raises(Exception):
        ModuleManifest(id="m", name="m", capabilities=Capabilities(host={"http": {"enabled": True}, "bogus": 1}))
    with pytest.raises(Exception):
        ModuleManifest(id="m", name="m", capabilities=Capabilities(
            host={"http": {"enabled": True, "endpoints": [{"id": "a", "base_url": BASE, "extra": 1}]}}))


@pytest.mark.parametrize("bad", [
    "ftp://x/y", "pve.lab:8006", "https://user:pw@pve.lab:8006", "https://pve.lab:8006/?x=1", "https://pve#frag",
])
def test_manifest_rejects_bad_base_url(bad):
    with pytest.raises(Exception):
        _manifest(base_url=bad)


def test_manifest_methods_default_read_only_and_uppercase():
    m = ModuleManifest(id="m", name="m", capabilities=Capabilities(
        host={"http": {"enabled": True, "endpoints": [{"id": "a", "base_url": BASE, "methods": ["get", "post"]}]}}))
    assert m.capabilities.host.http.endpoints[0].methods == ["GET", "POST"]
    m2 = ModuleManifest(id="m", name="m", capabilities=Capabilities(
        host={"http": {"enabled": True, "endpoints": [{"id": "a", "base_url": BASE}]}}))
    assert m2.capabilities.host.http.endpoints[0].methods == ["GET", "HEAD"]
    with pytest.raises(Exception):
        _manifest(methods=["TRACE"])


def test_manifest_rejects_duplicate_endpoint_ids():
    with pytest.raises(Exception):
        ModuleManifest(id="m", name="m", capabilities=Capabilities(host={"http": {"enabled": True, "endpoints": [
            {"id": "a", "base_url": BASE}, {"id": "a", "base_url": BASE}]}}))


# ── effective store ───────────────────────────────────────────────────────────


def test_seed_pending_then_operator_activates(store):
    revs = store.seed(_manifest(), activate=False)
    assert revs["proxmox"]["status"] == "pending"
    assert set(revs["proxmox"]["methods"]) == {"DELETE", "GET", "HEAD", "POST"}
    # Re-seeding pending never overwrites an existing revision.
    store.seed(_manifest(base_url="https://other:1/x"), activate=False)
    assert store.get("proxmox", "proxmox")["base_url"] == BASE
    # Operator confirms with a real host, read-only.
    rev = store.update(_manifest(), "proxmox", base_url="https://10.10.0.20:8006/api2/json",
                       methods=["GET"], consent=True, consented_by="michael")
    assert rev["status"] == "active"
    assert rev["methods"] == ["GET", "HEAD"]
    assert rev["pinned_ips"] == ["10.10.0.20"]
    assert rev["consented_by"] == "michael"


def test_methods_cannot_exceed_declared(store):
    with pytest.raises(store.EndpointConfigError):
        store.seed(_manifest(methods=["GET"]), {"proxmox": {"methods": ["POST"]}})


def test_widening_requires_consent_reduction_does_not(store, active):
    m = _manifest()
    # Reduce to read-only: no consent flag needed.
    rev = store.update(m, "proxmox", methods=["GET"])
    assert rev["methods"] == ["GET", "HEAD"]
    # Widen back: consent required.
    with pytest.raises(store.EndpointConfigError, match="consent required"):
        store.update(m, "proxmox", methods=["GET", "POST"])
    # Host change: consent required.
    with pytest.raises(store.EndpointConfigError, match="host"):
        store.update(m, "proxmox", base_url="https://10.10.0.21:8006/api2/json")
    rev = store.update(m, "proxmox", base_url="https://10.10.0.21:8006/api2/json", consent=True)
    assert rev["pinned_ips"] == ["10.10.0.21"]
    assert rev["revision"] > 1


def test_summary_never_exposes_secret_ref_or_ips(store, active):
    s = store.summary("proxmox")[0]
    assert set(s) == {"id", "status", "methods", "declared_methods", "verify_tls", "host", "revision", "pinned"}
    assert "PROXMOX_TOKEN" not in json.dumps(s)


def test_uninstall_removes_config(store, active):
    store.remove_module("proxmox")
    assert store.get("proxmox", "proxmox") is None


# ── policy: path, url, headers ────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../x", "a/../b", "//evil", "http://evil/x", "a\\b", "x@y", "a?b=1", "a#f",
                                 "%2e%2e/x"])
def test_path_rejections(bad):
    with pytest.raises(http_bridge.HttpBridgeError) as ei:
        http_bridge.validate_rel_path(bad)
    assert ei.value.status == 400


def test_build_url_pins_origin():
    assert http_bridge.build_url(BASE, "/nodes", {"type": "vm"}) == BASE + "/nodes?type=vm"
    assert http_bridge.build_url(BASE, "", None) == BASE


def test_header_sanitation_drops_auth_host_cookie_and_hop_by_hop():
    out = http_bridge.sanitize_headers({
        "Authorization": "Bearer mine", "Host": "evil", "Cookie": "a=b", "Content-Length": "3",
        "X-Forwarded-For": "1.1.1.1", "X-AGD-User": "spoof", "Accept": "application/json", "X-Custom-Auth": "z",
    }, auth_header="X-Custom-Auth")
    assert out == {"Accept": "application/json"}
    with pytest.raises(http_bridge.HttpBridgeError):
        http_bridge.sanitize_headers({"Accept": "a\r\nInjected: 1"})


# ── request: end to end against a mocked upstream ─────────────────────────────


def _payload(**kw):
    p = {"endpoint": "proxmox", "method": "GET", "path": "/nodes"}
    p.update(kw)
    return p


@respx.mock
def test_request_injects_auth_and_returns_upstream_status(active, bridge_client):
    client, mint = bridge_client
    route = respx.get(BASE + "/nodes").mock(return_value=httpx.Response(200, json={"data": [{"node": "pve1"}]}))
    r = client.post("/api/_host/http/request", json=_payload(headers={"Authorization": "Bearer worker-supplied"}),
                    headers=_h(mint()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == 200 and body["truncated"] is False
    assert json.loads(body["body"])["data"][0]["node"] == "pve1"
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "PVEAPIToken=root@pam!agd=deadbeef-secret"
    # The secret never appears in what the worker gets back.
    assert "deadbeef" not in r.text


@respx.mock
def test_non_2xx_and_redirect_returned_not_raised_or_followed(active, bridge_client):
    client, mint = bridge_client
    respx.get(BASE + "/missing").mock(return_value=httpx.Response(404, json={"errors": "nope"}))
    respx.get(BASE + "/moved").mock(return_value=httpx.Response(302, headers={"location": "https://evil/x"}))
    tok = mint()
    r = client.post("/api/_host/http/request", json=_payload(path="/missing"), headers=_h(tok))
    assert r.status_code == 200 and r.json()["status"] == 404
    r = client.post("/api/_host/http/request", json=_payload(path="/moved"), headers=_h(tok))
    assert r.status_code == 200 and r.json()["status"] == 302
    assert r.json()["headers"]["location"] == "https://evil/x"
    assert len(respx.calls) == 2  # the redirect target was never fetched


@respx.mock
def test_read_only_reduction_blocks_post_at_bridge(store, bridge_client):
    """Acceptance criterion 4: the manifest declares POST, the operator granted
    GET only, the worker calls POST directly on the bridge -> 403, no upstream."""
    client, mint = bridge_client
    store.seed(_manifest(), {"proxmox": {"methods": ["GET"]}}, consented_by="op")
    route = respx.post(BASE + "/nodes/pve1/qemu/100/status/start").mock(return_value=httpx.Response(200))
    r = client.post("/api/_host/http/request",
                    json=_payload(method="POST", path="/nodes/pve1/qemu/100/status/start"), headers=_h(mint()))
    assert r.status_code == 403
    assert "not granted" in r.json()["detail"]
    assert route.call_count == 0


@respx.mock
def test_store_not_manifest_is_source_of_truth(store, bridge_client):
    """A worker (or a tampered manifest) cannot move the endpoint: the call goes
    to the operator-confirmed base URL."""
    client, mint = bridge_client
    store.seed(_manifest(), {"proxmox": {"base_url": "https://10.10.0.20:8006/api2/json", "methods": ["GET"]}})
    real = respx.get("https://10.10.0.20:8006/api2/json/nodes").mock(return_value=httpx.Response(200, text="real"))
    manifest_host = respx.get(BASE + "/nodes").mock(return_value=httpx.Response(200, text="manifest"))
    r = client.post("/api/_host/http/request", json=_payload(),
                    headers=_h(mint(_manifest(base_url="https://attacker.example/x"))))
    assert r.status_code == 200 and r.json()["body"] == "real"
    assert real.call_count == 1 and manifest_host.call_count == 0


def test_bridge_gates(store, bridge_client):
    client, mint = bridge_client
    # No host.http declared at all.
    no_http = ModuleManifest(id="proxmox", name="p", capabilities=Capabilities())
    r = client.post("/api/_host/http/request", json=_payload(), headers=_h(mint(no_http)))
    assert r.status_code == 403 and "host.http" in r.json()["detail"]
    tok = mint()
    # Declared but undeclared endpoint id.
    r = client.post("/api/_host/http/request", json=_payload(endpoint="other"), headers=_h(tok))
    assert r.status_code == 403
    # Declared, but no host configuration yet.
    r = client.post("/api/_host/http/request", json=_payload(), headers=_h(tok))
    assert r.status_code == 403 and "no host configuration" in r.json()["detail"]
    # Pending (seeded on registration, not yet confirmed).
    store.seed(_manifest(), activate=False)
    r = client.post("/api/_host/http/request", json=_payload(), headers=_h(tok))
    assert r.status_code == 403 and "awaits operator confirmation" in r.json()["detail"]
    # Bad path -> 400.
    store.seed(_manifest(), {"proxmox": {"methods": ["GET"]}})
    r = client.post("/api/_host/http/request", json=_payload(path="../x"), headers=_h(tok))
    assert r.status_code == 400
    # Cookie-bearing request rejected (not a browser surface).
    r = client.post("/api/_host/http/request", json=_payload(), headers={**_h(tok), "cookie": "s=1"})
    assert r.status_code == 403


@respx.mock
def test_missing_secret_fails_closed(store, bridge_client, monkeypatch):
    client, mint = bridge_client
    store.seed(_manifest(), {"proxmox": {"methods": ["GET"]}})
    monkeypatch.delenv("PROXMOX_TOKEN")
    route = respx.get(BASE + "/nodes").mock(return_value=httpx.Response(200))
    r = client.post("/api/_host/http/request", json=_payload(), headers=_h(mint()))
    assert r.status_code == 503
    assert route.call_count == 0  # never sends the ref name as a credential


@respx.mock
def test_truncation_and_binary_shaping(active, bridge_client, monkeypatch):
    client, mint = bridge_client
    monkeypatch.setattr(http_bridge, "MAX_BODY_BYTES", 10)
    respx.get(BASE + "/big").mock(return_value=httpx.Response(200, text="x" * 50))
    respx.get(BASE + "/bin").mock(return_value=httpx.Response(
        200, content=b"\x89PNG\x00\xff", headers={"content-type": "application/octet-stream", "set-cookie": "a=b"}))
    tok = mint()
    r = client.post("/api/_host/http/request", json=_payload(path="/big"), headers=_h(tok)).json()
    assert r["truncated"] is True and len(r["body"]) == 10
    r = client.post("/api/_host/http/request", json=_payload(path="/bin"), headers=_h(tok)).json()
    assert r["content_encoding"] == "base64"
    assert base64.b64decode(r["body"]) == b"\x89PNG\x00\xff"
    assert "set-cookie" not in r["headers"] and r["headers"]["content-type"] == "application/octet-stream"


@respx.mock
def test_json_body_and_base64_body(active, bridge_client):
    client, mint = bridge_client
    route = respx.post(BASE + "/nodes/pve1/lxc").mock(return_value=httpx.Response(200, json={"data": "UPID"}))
    tok = mint()
    r = client.post("/api/_host/http/request",
                    json=_payload(method="POST", path="/nodes/pve1/lxc", body={"vmid": 200}), headers=_h(tok))
    assert r.status_code == 200
    sent = route.calls.last.request
    assert sent.headers["content-type"] == "application/json" and json.loads(sent.content) == {"vmid": 200}
    raw = base64.b64encode(b"\x00\x01").decode()
    client.post("/api/_host/http/request",
                json=_payload(method="POST", path="/nodes/pve1/lxc", body=raw, body_encoding="base64"),
                headers=_h(tok))
    assert route.calls.last.request.content == b"\x00\x01"


@respx.mock
def test_in_process_and_bridge_share_semantics(active, bridge_client):
    """Acceptance criterion 6: the in-process facade and the bridge route are the
    same implementation, so status/body/truncation match."""
    import asyncio

    client, mint = bridge_client
    respx.get(BASE + "/version").mock(return_value=httpx.Response(418, json={"data": {"version": "8.2"}}))
    via_bridge = client.post("/api/_host/http/request", json=_payload(path="/version"), headers=_h(mint())).json()
    in_proc = asyncio.run(http_bridge.request("proxmox", _payload(path="/version")))
    assert via_bridge == in_proc
    assert in_proc["status"] == 418


@respx.mock
def test_audit_records_call_without_secret(active, caplog):
    import asyncio

    respx.get(BASE + "/nodes").mock(return_value=httpx.Response(200, text="ok"))
    with caplog.at_level(logging.WARNING, logger="agd.audit"):
        asyncio.run(http_bridge.request("proxmox", _payload()))
    lines = [r.getMessage() for r in caplog.records if "module.http_request" in r.getMessage()]
    assert lines and "deadbeef" not in "".join(lines)
    assert '"endpoint": "proxmox"' in lines[-1] and '"status": 200' in lines[-1]


def test_grant_summary_route(active, bridge_client):
    client, mint = bridge_client
    r = client.get("/api/_host/http/endpoints", headers=_h(mint()))
    assert r.status_code == 200
    eps = r.json()["endpoints"]
    assert eps[0]["id"] == "proxmox" and "POST" in eps[0]["methods"] and eps[0]["status"] == "active"
    assert "PROXMOX_TOKEN" not in r.text and "pinned_ips" not in r.text


# ── pinning ───────────────────────────────────────────────────────────────────


def test_dial_ips_fails_closed_without_pins_and_on_rebind(store, monkeypatch):
    store.seed(_manifest(), {"proxmox": {"methods": ["GET"]}})
    rev = store.get("proxmox", "proxmox")
    rev["pinned_ips"] = []
    with pytest.raises(http_bridge.HttpBridgeError) as ei:
        http_bridge.dial_ips(rev)
    assert ei.value.status == 409
    rev["pinned_ips"] = ["10.0.0.5"]
    monkeypatch.setattr(endpoints, "resolve_ips", lambda h, p: ["10.0.0.6"])  # rebound elsewhere
    with pytest.raises(http_bridge.HttpBridgeError, match="re-pin"):
        http_bridge.dial_ips(rev)
    http_bridge._resolve_cache.clear()
    monkeypatch.setattr(endpoints, "resolve_ips", lambda h, p: ["10.0.0.6", "10.0.0.5"])
    assert http_bridge.dial_ips(rev) == ["10.0.0.5"]


def test_pinned_backend_dials_pinned_ip_with_original_host_kept_for_tls():
    import asyncio

    dialed = []

    class Inner:
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            dialed.append((host, port))
            if host == "10.0.0.1":
                raise OSError("down")
            return "stream"

        async def sleep(self, s):
            pass

    be = http_bridge.PinnedBackend("pve.lab.example", ["10.0.0.1", "10.0.0.2"], inner=Inner())
    assert asyncio.run(be.connect_tcp("pve.lab.example", 8006)) == "stream"
    assert dialed == [("10.0.0.1", 8006), ("10.0.0.2", 8006)]  # never the hostname itself
    with pytest.raises(Exception):
        asyncio.run(be.connect_tcp("other.example", 8006))


# ── scanner ───────────────────────────────────────────────────────────────────


def _scan(tmp_path, code, manifest):
    from backend.modules.modules.scanner import scan_module

    (tmp_path / "mod.py").write_text(code + "\n", encoding="utf-8")
    return scan_module(tmp_path, manifest)


def test_scanner_flags_undeclared_http_bridge_use(tmp_path):
    code = 'URL = "/api/_host/http/request"'
    r = _scan(tmp_path, code, ModuleManifest(id="m", name="m", capabilities=Capabilities()))
    assert any(f.severity == "HIGH" and f.category == "undeclared-host" for f in r.findings)
    r = _scan(tmp_path, code, _manifest())
    assert not any(f.severity == "HIGH" for f in r.findings)
    assert any(f.category == "host-bridge" and "http.request" in f.detail for f in r.findings)


def test_scanner_allows_in_process_facade_import_only_when_declared(tmp_path):
    code = "from backend.modules._runtime import http_bridge"
    r = _scan(tmp_path, code, _manifest())
    assert not any(f.category == "host-import" for f in r.findings)
    assert any(f.category == "host-bridge" and "in-process" in f.detail for f in r.findings)
    r = _scan(tmp_path, code, ModuleManifest(id="m", name="m", capabilities=Capabilities()))
    assert any(f.severity == "HIGH" and f.category == "undeclared-host" for f in r.findings)
    # Any other host import is still the HIGH host-import finding.
    r = _scan(tmp_path, "from backend.config import load_secrets", _manifest())
    assert any(f.severity == "HIGH" and f.category == "host-import" for f in r.findings)


def test_scanner_surfaces_verify_tls_false_and_mutating_endpoints(tmp_path):
    r = _scan(tmp_path, "x = 1", _manifest())
    details = [f.detail for f in r.findings if f.file == "manifest.json"]
    assert any("verify_tls=false" in d for d in details)
    assert any("can CHANGE data" in d for d in details)
