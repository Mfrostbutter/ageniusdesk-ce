"""Phase 2: host-side supervisor + reverse proxy for out-of-process modules.

Spawns a REAL trivial module worker (a subprocess running the phase-1 bootstrap)
and drives it through the reverse proxy: round-trip, header hygiene (host
Cookie/Authorization stripped, proxy secret added), proxy-secret enforcement on a
direct hit, and response streaming. Also asserts the default isolation mode is
in_process so existing behavior is unchanged.
"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.modules._runtime import proxy, supervisor

# A self-contained community module: its router lives at the real /api/{id} prefix
# and echoes the headers it received so the test can assert what the proxy passed.
_FIXTURE = '''
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/trivialmod")


@router.get("/ping")
async def ping(request: Request):
    return {"pong": True, "headers": {k.lower(): v for k, v in request.headers.items()}}


@router.get("/big")
async def big():
    return PlainTextResponse("x" * 100000)
'''


@pytest.fixture(scope="module")
def worker(tmp_path_factory):
    parent = tmp_path_factory.mktemp("mods")
    moddir = parent / "trivialmod"
    moddir.mkdir()
    (moddir / "__init__.py").write_text(_FIXTURE)
    w = supervisor.start_worker("trivialmod", parent)
    yield w
    w.stop()


def _proxy_app() -> FastAPI:
    app = FastAPI()
    proxy.register_proxy_route(app, "trivialmod")
    return app


def test_worker_spawns_healthy(worker):
    assert worker.is_alive()
    assert supervisor.get("trivialmod") is worker


def test_proxy_roundtrip_and_header_hygiene(worker):
    with TestClient(_proxy_app()) as client:
        r = client.get(
            "/api/trivialmod/ping",
            headers={"cookie": "agd_session=secret", "authorization": "Bearer tok", "x-custom": "keep"},
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["pong"] is True
    got = data["headers"]
    # Host identity must NOT reach the worker.
    assert "cookie" not in got
    assert "authorization" not in got
    # Innocuous headers pass through; the proxy secret is injected.
    assert got.get("x-custom") == "keep"
    assert got.get("x-agd-proxy-secret") == worker.proxy_secret


def test_proxy_streams_large_body(worker):
    with TestClient(_proxy_app()) as client:
        r = client.get("/api/trivialmod/big")
    assert r.status_code == 200
    assert len(r.text) == 100000


def test_direct_hit_requires_proxy_secret(worker):
    # A local process that reaches the worker bind directly, without the secret,
    # is rejected before any module routing.
    transport = httpx.HTTPTransport(uds=worker.uds_path) if supervisor.USE_UDS else httpx.HTTPTransport()
    with httpx.Client(transport=transport, base_url=worker.base_url, timeout=5.0) as c:
        assert c.get("/api/trivialmod/ping").status_code == 403
        ok = c.get("/api/trivialmod/ping", headers={"x-agd-proxy-secret": worker.proxy_secret})
        assert ok.status_code == 200


def test_proxy_502_when_worker_absent():
    # No worker registered for this id -> proxy returns 502, not a crash.
    app = FastAPI()
    proxy.register_proxy_route(app, "ghostmod")
    with TestClient(app) as client:
        assert client.get("/api/ghostmod/anything").status_code == 502


def test_default_isolation_mode_is_in_process(monkeypatch):
    monkeypatch.delenv("AGD_MODULE_ISOLATION", raising=False)
    from backend.modules import _isolation_mode

    assert _isolation_mode() == "in_process"
