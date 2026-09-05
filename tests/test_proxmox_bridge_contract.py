"""Real bridge contract test with the Proxmox community module as the fixture.

Spawns the actual Proxmox module in a SUBPROCESS worker against the live host
bridge listener, mocks Proxmox in the host process (where the bridge makes the
call), and drives the module through the reverse proxy: a cluster read and one
guarded power operation. The worker process never holds the token; the host
resolves it per call. In-process semantics are covered by test_http_bridge.py
against the same implementation.

Needs the community-modules checkout (sibling repo or AGD_PROXMOX_MODULE_DIR);
skipped when absent so CI without it stays green.
"""

import asyncio
import functools
import os
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI

from backend.module_registry import load_manifest
from backend.modules._runtime import bridge, endpoints, http_bridge, proxy, supervisor

_DEFAULT = Path(__file__).resolve().parents[2] / "ageniusdesk-community-modules" / "modules"
_OVERRIDE = os.environ.get("AGD_PROXMOX_MODULE_DIR", "")
MODULES_DIR = Path(_OVERRIDE) if _OVERRIDE else _DEFAULT
PVE = "https://10.10.0.20:8006/api2/json"

pytestmark = pytest.mark.skipif(
    not (MODULES_DIR / "proxmox" / "manifest.json").exists(),
    reason="Proxmox community module checkout not available",
)


@pytest.fixture
def pve_store(monkeypatch, tmp_path):
    monkeypatch.setattr(endpoints, "STORE_FILE", tmp_path / "module-endpoints.json")
    monkeypatch.setenv("PROXMOX_TOKEN", "root@pam!agd=deadbeef-secret")
    http_bridge._resolve_cache.clear()
    manifest = load_manifest(MODULES_DIR / "proxmox")
    assert manifest is not None
    return manifest


def _mock_pve(rx):
    # Only Proxmox is mocked; worker health/proxy traffic (127.0.0.1 on Windows,
    # the UDS "worker" host on POSIX) passes through to the real transport.
    rx.route(host="127.0.0.1").pass_through()
    rx.route(host="worker").pass_through()
    rx.get(PVE + "/cluster/resources").mock(return_value=httpx.Response(200, json={"data": [
        {"type": "qemu", "vmid": 100, "name": "web", "node": "pve1", "status": "running",
         "cpu": 0.1, "maxcpu": 4, "mem": 1024, "maxmem": 4096, "uptime": 100},
    ]}))
    rx.get(PVE + "/nodes").mock(return_value=httpx.Response(200, json={"data": [
        {"node": "pve1", "status": "online", "cpu": 0.2, "maxcpu": 8, "mem": 2048, "maxmem": 8192, "uptime": 10},
    ]}))
    rx.get(PVE + "/cluster/status").mock(return_value=httpx.Response(200, json={"data": []}))
    return rx.post(PVE + "/nodes/pve1/qemu/100/status/start").mock(
        return_value=httpx.Response(200, json={"data": "UPID:pve1:1"}))


async def _run(manifest, methods, scenario):
    endpoints.seed(manifest, {"proxmox": {"base_url": PVE, "methods": methods}}, consented_by="test")
    await bridge.start_bridge()
    loop = asyncio.get_running_loop()
    worker = await loop.run_in_executor(
        None, functools.partial(supervisor.start_worker, "proxmox", MODULES_DIR,
                                capabilities=manifest.capabilities, forward_env=[]))
    try:
        app = FastAPI()
        proxy.register_proxy_route(app, "proxmox")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as c:
            await scenario(c, worker)
    finally:
        await worker.aclose()
        supervisor.stop_worker("proxmox")
        await bridge.stop_bridge()


@pytest.mark.asyncio
async def test_isolated_proxmox_reads_and_guarded_power_via_bridge(pve_store):
    rx = respx.mock(assert_all_called=False)
    start = _mock_pve(rx)

    async def scenario(c, worker):
        # Token never entered the worker process env.
        env_tail = worker.proc.args
        assert "deadbeef" not in " ".join(map(str, env_tail))
        r = await c.get("/api/proxmox/cluster")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cluster"]["reachable"] is True
        assert body["cluster"]["totals"]["running"] == 1
        assert body["settings"]["grant"]["mutating"] is True
        assert "deadbeef" not in r.text
        # Guarded power op through the same bridge (host injected the token).
        r = await c.post("/api/proxmox/guests/pve1/qemu/100/start")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert start.calls.last.request.headers["authorization"] == "PVEAPIToken=root@pam!agd=deadbeef-secret"

    with rx:
        await _run(pve_store, ["GET", "POST", "DELETE"], scenario)


@pytest.mark.asyncio
async def test_isolated_proxmox_read_only_grant_blocks_power_at_host(pve_store):
    """Acceptance criterion 4, cross-process: the module's own guard allows the
    action, but the host grant is GET-only, so the bridge refuses the POST."""
    rx = respx.mock(assert_all_called=False)
    start = _mock_pve(rx)

    async def scenario(c, worker):
        r = await c.get("/api/proxmox/settings")
        assert r.status_code == 200 and r.json()["grant"]["mutating"] is False
        r = await c.post("/api/proxmox/guests/pve1/qemu/100/start")
        assert r.status_code == 502, r.text  # module maps the bridge refusal to an upstream failure
        assert start.call_count == 0
        audit = (await c.get("/api/proxmox/audit")).json()["audit"]
        assert audit and audit[0]["result"] == "failed" and "not granted" in audit[0]["reason"]

    with rx:
        await _run(pve_store, ["GET"], scenario)
