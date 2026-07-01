"""The http.request host-bridge namespace.

Drives bridge_app with a minted per-module token whose grant carries declared
http endpoints, and asserts the security contract: endpoint + method gating,
path/host-lock validation, host-side credential injection (the worker never sends
the secret), worker-header sanitation, the response-header allowlist, the response
size cap, and the DNS-rebind guard. The upstream is mocked with respx, so no real
network or DNS is touched (endpoints use literal-IP base URLs).
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from backend.module_registry import (
    Capabilities,
    HostBridgeCapability,
    HttpAuth,
    HttpBridgeCapability,
    HttpEndpoint,
)
from backend.modules._runtime import bridge

BASE = "https://10.9.9.9:8006/api2/json"


@pytest.fixture
def http_client():
    client = TestClient(bridge.bridge_app)
    issued: list[str] = []

    def _mint(endpoints):
        caps = Capabilities(
            host=HostBridgeCapability(http=HttpBridgeCapability(enabled=True, endpoints=endpoints)),
        )
        token = bridge.mint("testmod", caps)
        issued.append(token)
        return token

    yield client, _mint
    for t in issued:
        bridge.revoke(t)


def _h(token):
    return {"authorization": f"Bearer {token}"}


def _ep(**kw):
    kw.setdefault("id", "pve")
    kw.setdefault("base_url", BASE)
    return HttpEndpoint(**kw)


# ── Endpoint + method gating ──────────────────────────────────────────────────


def test_unknown_endpoint_403(http_client):
    client, mint = http_client
    token = mint([_ep(methods=["GET"])])
    r = client.post("/api/_host/http/request", json={"endpoint": "nope", "path": "/nodes"}, headers=_h(token))
    assert r.status_code == 403


def test_method_not_permitted_403(http_client):
    client, mint = http_client
    token = mint([_ep(methods=["GET"])])
    r = client.post(
        "/api/_host/http/request",
        json={"endpoint": "pve", "method": "POST", "path": "/nodes/x/status/stop"},
        headers=_h(token),
    )
    assert r.status_code == 403


@respx.mock
def test_get_allowed(http_client):
    client, mint = http_client
    token = mint([_ep(methods=["GET"])])
    respx.route(url__startswith=BASE).mock(return_value=httpx.Response(200, json={"data": []}))
    r = client.post("/api/_host/http/request", json={"endpoint": "pve", "path": "/nodes"}, headers=_h(token))
    assert r.status_code == 200
    assert r.json()["status"] == 200


# ── Path / host-lock validation ───────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["/../../etc/passwd", "//evil.example/x", "http://evil.example/x", "a\\b", "x@y"])
def test_path_traversal_and_pivot_rejected(http_client, bad):
    client, mint = http_client
    token = mint([_ep(methods=["GET"])])
    r = client.post("/api/_host/http/request", json={"endpoint": "pve", "path": bad}, headers=_h(token))
    assert r.status_code == 400


# ── Auth injection (host-side; worker never sends the secret) ─────────────────


@respx.mock
def test_bearer_auth_injected(http_client, monkeypatch):
    client, mint = http_client
    monkeypatch.setenv("CF_TOKEN", "sekret-value")
    token = mint([_ep(auth=HttpAuth(type="bearer", secret_ref="CF_TOKEN"), methods=["GET"])])
    route = respx.route(url__startswith=BASE).mock(return_value=httpx.Response(200, json={}))
    r = client.post("/api/_host/http/request", json={"endpoint": "pve", "path": "/zones"}, headers=_h(token))
    assert r.status_code == 200
    assert route.calls.last.request.headers["authorization"] == "Bearer sekret-value"


@respx.mock
def test_header_auth_format(http_client, monkeypatch):
    client, mint = http_client
    monkeypatch.setenv("PVE_TOKEN", "user@pam!t=uuid")
    token = mint([
        _ep(
            auth=HttpAuth(type="header", header="Authorization", secret_ref="PVE_TOKEN", format="PVEAPIToken={value}"),
            methods=["GET"],
        )
    ])
    route = respx.route(url__startswith=BASE).mock(return_value=httpx.Response(200, json={}))
    client.post("/api/_host/http/request", json={"endpoint": "pve", "path": "/nodes"}, headers=_h(token))
    assert route.calls.last.request.headers["authorization"] == "PVEAPIToken=user@pam!t=uuid"


@respx.mock
def test_query_auth_appended(http_client, monkeypatch):
    client, mint = http_client
    monkeypatch.setenv("PH_TOKEN", "abc123")
    token = mint([_ep(auth=HttpAuth(type="query", param="auth", secret_ref="PH_TOKEN"), methods=["GET"])])
    route = respx.route(url__startswith=BASE).mock(return_value=httpx.Response(200, json={}))
    client.post("/api/_host/http/request", json={"endpoint": "pve", "path": "/admin"}, headers=_h(token))
    assert "auth=abc123" in str(route.calls.last.request.url)


# ── Worker header sanitation ──────────────────────────────────────────────────


@respx.mock
def test_worker_auth_and_cookie_stripped(http_client):
    client, mint = http_client
    token = mint([_ep(methods=["GET"])])  # no endpoint auth
    route = respx.route(url__startswith=BASE).mock(return_value=httpx.Response(200, json={}))
    client.post(
        "/api/_host/http/request",
        json={
            "endpoint": "pve", "path": "/nodes",
            "headers": {"Authorization": "Bearer WORKER-SET", "Cookie": "x=1", "Accept": "application/json"},
        },
        headers=_h(token),
    )
    req = route.calls.last.request
    assert "authorization" not in req.headers  # worker's stripped, endpoint declared none
    assert "cookie" not in req.headers
    assert req.headers["accept"] == "application/json"  # innocuous header preserved


# ── Response header allowlist + size cap ──────────────────────────────────────


@respx.mock
def test_response_header_allowlist(http_client):
    client, mint = http_client
    token = mint([_ep(methods=["GET"])])
    respx.route(url__startswith=BASE).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json", "set-cookie": "s=1", "x-secret": "leak"},
            json={},
        )
    )
    r = client.post("/api/_host/http/request", json={"endpoint": "pve", "path": "/nodes"}, headers=_h(token))
    out = r.json()["headers"]
    assert out.get("content-type") == "application/json"
    assert "set-cookie" not in out
    assert "x-secret" not in out


@respx.mock
def test_response_size_cap(http_client, monkeypatch):
    client, mint = http_client
    monkeypatch.setattr(bridge, "MAX_HTTP_BYTES", 10)
    token = mint([_ep(methods=["GET"])])
    respx.route(url__startswith=BASE).mock(return_value=httpx.Response(200, text="abcdefghijklmnopqrstuvwxyz"))
    r = client.post("/api/_host/http/request", json={"endpoint": "pve", "path": "/nodes"}, headers=_h(token))
    body = r.json()
    assert body["truncated"] is True
    assert len(body["body"]) == 10


# ── Non-2xx returned, not raised ──────────────────────────────────────────────


@respx.mock
def test_non_2xx_returned_as_is(http_client):
    client, mint = http_client
    token = mint([_ep(methods=["GET"])])
    respx.route(url__startswith=BASE).mock(return_value=httpx.Response(404, json={"error": "not found"}))
    r = client.post("/api/_host/http/request", json={"endpoint": "pve", "path": "/nodes/x"}, headers=_h(token))
    assert r.status_code == 200  # the bridge call succeeded
    assert r.json()["status"] == 404  # the upstream status is passed through


# ── DNS-rebind guard ──────────────────────────────────────────────────────────


@respx.mock
def test_dns_rebind_fails_closed(http_client, monkeypatch):
    client, mint = http_client
    # Resolve to 1.2.3.4 at mint (pin), then rebind to a disjoint address.
    monkeypatch.setattr(bridge, "_resolve_ips", lambda h: {"1.2.3.4"})
    token = mint([_ep(id="pve", base_url="https://proxmox.lan:8006/api2/json", methods=["GET"])])
    monkeypatch.setattr(bridge, "_resolve_ips", lambda h: {"9.9.9.9"})
    respx.route(url__startswith="https://proxmox.lan:8006").mock(return_value=httpx.Response(200, json={}))
    r = client.post("/api/_host/http/request", json={"endpoint": "pve", "path": "/nodes"}, headers=_h(token))
    assert r.status_code == 502
