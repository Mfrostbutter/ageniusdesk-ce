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
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter(prefix="/api/trivialmod")


@router.get("/ping")
async def ping(request: Request):
    return {"pong": True, "headers": {k.lower(): v for k, v in request.headers.items()}}


@router.get("/big")
async def big():
    return PlainTextResponse("x" * 100000)


@router.post("/echo")
async def echo(request: Request):
    return {"len": len(await request.body())}


@router.get("/evilcookie")
async def evilcookie():
    r = JSONResponse({"ok": True})
    r.set_cookie("agd_session", "hijacked")
    r.headers["set-cookie2"] = "legacy=1"
    r.headers["clear-site-data"] = '"cookies", "storage"'
    r.headers["www-authenticate"] = "Basic realm=host"
    return r
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


def test_proxy_strips_auth_sensitive_response_headers(worker):
    # A community module must not influence host-origin browser state.
    with TestClient(_proxy_app()) as client:
        r = client.get("/api/trivialmod/evilcookie")
    assert r.status_code == 200
    lower = {k.lower() for k in r.headers}
    assert "set-cookie" not in lower
    assert "set-cookie2" not in lower
    assert "clear-site-data" not in lower
    assert "www-authenticate" not in lower
    assert "agd_session" not in r.cookies


def test_proxy_streams_request_body(worker):
    with TestClient(_proxy_app()) as client:
        r = client.post("/api/trivialmod/echo", content=b"a" * 5000)
    assert r.status_code == 200
    assert r.json()["len"] == 5000


def test_pid_identity_guard(worker):
    import os as _os

    # The live worker is provably ours; an unrelated pid (this test process) is not.
    assert supervisor._pid_is_our_worker(worker.pid(), "trivialmod") is True
    assert supervisor._pid_is_our_worker(worker.pid(), "someothermod") is False
    assert supervisor._pid_is_our_worker(_os.getpid(), "trivialmod") is False
    # Exact-token match, not substring: a prefix of the real id must NOT match.
    assert supervisor._pid_is_our_worker(worker.pid(), "trivial") is False
    assert supervisor._pid_is_our_worker(worker.pid(), "trivialmodx") is False
    # Cannot read argv (no such pid) -> skip (treated as not-ours).
    assert supervisor._pid_is_our_worker(2147483646, "trivialmod") is False


def test_parse_cmdline_string_skips_on_parse_error(monkeypatch):
    # Well-formed string tokenizes to exact tokens.
    toks = supervisor._parse_cmdline_string("python main.py --agd-module trivialmod")
    assert toks and "--agd-module" in toks and "trivialmod" in toks
    assert supervisor._parse_cmdline_string("") is None
    assert supervisor._parse_cmdline_string("   ") is None

    # A tokenizer failure (ambiguous/malformed cmdline) must yield None
    # ("cannot verify -> skip kill"), never a naive split that could forge tokens.
    def _raise(*a, **k):
        raise ValueError("No closing quotation")

    monkeypatch.setattr(supervisor.shlex, "split", _raise)
    assert supervisor._parse_cmdline_string('python main.py --agd-module trivialmod "') is None


def test_default_mode_has_no_side_effects(tmp_path, monkeypatch):
    # stop_all() with nothing started must not create data/run or the pidfile
    # (this is what the unconditional lifespan shutdown hits in default mode).
    monkeypatch.chdir(tmp_path)
    saved = dict(supervisor._workers)
    supervisor._workers.clear()
    try:
        supervisor.stop_all()
        assert not (tmp_path / "data" / "run").exists()
    finally:
        supervisor._workers.clear()
        supervisor._workers.update(saved)


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


async def test_stop_container_worker_untracked_cleans_up(monkeypatch):
    # After a restart or a mode switch a module's container is no longer tracked
    # in _workers; uninstall must still remove the container + volume and revoke
    # the grant (mode-independent cleanup), not early-return.
    from backend.modules._runtime import bridge, containers

    containers_calls = {}

    async def _fake_rm_container(name):
        containers_calls["container"] = name

    async def _fake_rm_volume(mid):
        containers_calls["volume"] = mid

    monkeypatch.setattr(containers, "_remove_container_by_name", _fake_rm_container)
    monkeypatch.setattr(containers, "_remove_volume", _fake_rm_volume)
    revoked = {}
    monkeypatch.setattr(bridge, "revoke_module", lambda mid: revoked.setdefault("mid", mid))
    supervisor._workers.pop("ghostmod-x", None)  # ensure untracked

    res = await containers.stop_container_worker("ghostmod-x", remove_volume=True)

    assert res is False  # nothing was tracked
    assert containers_calls["container"] == "agd-mod-ghostmod-x"
    assert containers_calls["volume"] == "ghostmod-x"
    assert revoked["mid"] == "ghostmod-x"


def test_uninstall_stops_worker_and_revokes_token(tmp_path, monkeypatch):
    # HIGH-2: uninstall must stop the subprocess and revoke its bridge token, not
    # leave the module fully operational until the host restarts. chdir so the
    # relative data/ roots (supervisor + installer) resolve under tmp_path.
    from backend.modules._runtime import bridge
    from backend.modules.modules import installer

    monkeypatch.chdir(tmp_path)
    parent = tmp_path / "mods"
    moddir = parent / "downmod"
    moddir.mkdir(parents=True)
    (moddir / "__init__.py").write_text(_FIXTURE.replace("trivialmod", "downmod"))

    worker = supervisor.start_worker("downmod", parent)
    token = worker.bridge_token
    assert worker.is_alive()
    assert bridge.grant_for(token) is not None  # token live while installed

    result = installer.uninstall("downmod")

    assert result["id"] == "downmod"
    assert not worker.is_alive()                    # subprocess stopped
    assert supervisor.get("downmod") is None        # deregistered
    assert bridge.grant_for(token) is None          # host-bridge token revoked
