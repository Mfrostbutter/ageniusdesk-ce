"""Built-in n8n-mcp provisioning: run czlonkowski/n8n-mcp as a container and
register it as an MCP server so Code Lab / the assistant get n8n node
intelligence out of the box.

Design (see docs/specs): the dashboard's MCP client is HTTP-only, so n8n-mcp runs
in its own container in HTTP mode and the dashboard reaches it over a published
host port (via host.docker.internal when the dashboard itself is containerized,
else localhost) — the same host-gateway path the n8n proxy already relies on. A
post-start probe (MCP initialize + tools/list) is the correctness gate: we only
register a server that actually answers, otherwise we surface the manual path.

Modes:
  - docs (default): node knowledge, search, validation. Needs no n8n credentials,
    so it works immediately, before any instance is connected.
  - full: docs + workflow create/update/manage, wired to the active instance's
    N8N_API_URL/KEY (the one-click "upgrade").

Auto-install on boot is best-effort and gated on Docker availability; opt out
with AGD_N8N_MCP_AUTO=false. Everything here is non-fatal.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets

from backend.config import decrypt_value, encrypt_value
from backend.modules.assistant import mcp_client

logger = logging.getLogger(__name__)

SERVER_ID = "n8n-mcp-builtin"
SERVER_NAME = "n8n-mcp (node intelligence)"
SERVER_DESC = "Built-in n8n node knowledge, search, and workflow validation."
CONTAINER_NAME = "agd-n8n-mcp"
IMAGE = os.environ.get("AGD_N8N_MCP_IMAGE", "ghcr.io/czlonkowski/n8n-mcp:latest")
INTERNAL_PORT = 3000


def _enabled() -> bool:
    return os.environ.get("AGD_N8N_MCP_AUTO", "true").strip().lower() not in {"false", "0", "no", "off"}


def _host_port() -> int:
    try:
        return int(os.environ.get("AGD_N8N_MCP_PORT", "3456"))
    except ValueError:
        return 3456


def _in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _candidate_urls(port: int) -> list[str]:
    """URLs the dashboard can use to reach the n8n-mcp container, best first.

    An explicit AGD_N8N_MCP_URL override wins (operator escape hatch). Otherwise:
    inside Docker the published port is reachable via the host gateway
    (host.docker.internal); on bare metal it's localhost.
    """
    override = os.environ.get("AGD_N8N_MCP_URL", "").strip()
    if override:
        return [override.rstrip("/")]
    hosts = ["host.docker.internal", "127.0.0.1"] if _in_docker() else ["localhost", "127.0.0.1"]
    return [f"http://{h}:{port}/mcp" for h in hosts]


def get_registered() -> dict | None:
    """The built-in n8n-mcp server entry in config.mcp_servers, or None."""
    for s in mcp_client.get_mcp_servers():
        if s.get("id") == SERVER_ID:
            return s
    return None


def _register(url: str, token: str, mode: str) -> None:
    """Upsert the built-in n8n-mcp server (encrypted token) into config."""
    servers = [s for s in mcp_client.get_mcp_servers() if s.get("id") != SERVER_ID]
    servers.append({
        "id": SERVER_ID,
        "name": SERVER_NAME,
        "url": url.rstrip("/"),
        "token": encrypt_value(token),
        "description": SERVER_DESC,
        "enabled": True,
        "instances": [],
        "managed": "n8n-mcp",  # marks this as dashboard-managed (not operator-added)
        "mode": mode,
    })
    mcp_client.save_mcp_servers(servers)


def _unregister() -> bool:
    servers = mcp_client.get_mcp_servers()
    kept = [s for s in servers if s.get("id") != SERVER_ID]
    if len(kept) == len(servers):
        return False
    mcp_client.save_mcp_servers(kept)
    return True


async def _docker_available() -> bool:
    try:
        from backend.modules.docker_mgr import client as dc
        return await dc.ping()
    except Exception:
        return False


async def _container_state() -> str | None:
    """State string of the n8n-mcp container ('running', 'exited', ...) or None."""
    try:
        from backend.modules.docker_mgr import client as dc
        info = await dc.inspect_container(CONTAINER_NAME)
        return (info.get("State") or {}).get("Status")
    except Exception:
        return None


async def _run_container(token: str, *, n8n_url: str = "", n8n_key: str = "") -> None:
    """Pull n8n-mcp and (re)create + start it in HTTP mode on the host port.

    Raises on Docker errors so callers can surface them; the auto path catches.
    """
    import aiodocker

    from backend.modules.docker_mgr import client as dc

    docker = dc._get_client()
    await docker.images.pull(IMAGE)

    # Replace any existing managed container so re-provisioning is idempotent.
    try:
        stale = await docker.containers.get(CONTAINER_NAME)
        try:
            await stale.stop()
        except Exception:
            pass
        await stale.delete(force=True)
    except aiodocker.DockerError:
        pass  # not present — fine

    env = [
        "MCP_MODE=http",
        f"AUTH_TOKEN={token}",
        f"PORT={INTERNAL_PORT}",
        "LOG_LEVEL=warn",
    ]
    if n8n_url and n8n_key:
        env += [f"N8N_API_URL={n8n_url}", f"N8N_API_KEY={n8n_key}"]

    host_port = _host_port()
    config = {
        "Image": IMAGE,
        "Env": env,
        "ExposedPorts": {f"{INTERNAL_PORT}/tcp": {}},
        "HostConfig": {
            # Empty HostIp = 0.0.0.0 so a containerized dashboard can reach the
            # published port via host.docker.internal. The AUTH_TOKEN gates it.
            "PortBindings": {f"{INTERNAL_PORT}/tcp": [{"HostPort": str(host_port)}]},
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "Labels": {"agd.role": "n8n-mcp", "agd.managed": "true"},
    }
    container = await docker.containers.create(config, name=CONTAINER_NAME)
    await container.start()


async def _probe_and_register(token: str, mode: str) -> dict:
    """Wait for n8n-mcp to answer, then register the first reachable URL.

    Returns {ok, url?, tools_count?, mode?, error?}. Retries while the container
    boots (~30s budget total).
    """
    port = _host_port()
    candidates = _candidate_urls(port)
    enc = encrypt_value(token)
    last_err = ""
    # ~30s: 8 rounds with growing backoff across all candidate URLs.
    for attempt in range(8):
        for url in candidates:
            probe = {"id": SERVER_ID, "name": SERVER_NAME, "url": url, "token": enc}
            try:
                res = await mcp_client.test_server(probe)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                continue
            if res.get("connected") and res.get("tools_count", 0) > 0:
                _register(url, token, mode)
                logger.info("n8n-mcp: registered %s (%s, %d tools)", url, mode, res["tools_count"])
                return {"ok": True, "url": url, "tools_count": res["tools_count"], "mode": mode}
            last_err = res.get("error", "") or last_err
        await asyncio.sleep(min(2 + attempt, 6))
    logger.warning("n8n-mcp: container started but no URL answered (%s)", last_err)
    return {"ok": False, "error": last_err or "n8n-mcp did not answer on any candidate URL"}


async def status() -> dict:
    """Current state of the built-in n8n-mcp integration."""
    existing = get_registered()
    docker_ok = await _docker_available()
    running = (await _container_state()) == "running" if docker_ok else False
    return {
        "auto": _enabled(),
        "docker_available": docker_ok,
        "registered": bool(existing),
        "mode": (existing or {}).get("mode", "") if existing else "",
        "container_running": running,
        "url": (existing or {}).get("url", "") if existing else "",
        "image": IMAGE,
        "host_port": _host_port(),
    }


async def ensure_n8n_mcp() -> None:
    """Best-effort auto-install on boot. Non-fatal; no-op unless Docker is up and
    the server isn't already registered. Run as a background task so the image
    pull never blocks startup."""
    if not _enabled():
        return
    if get_registered():
        return
    if not await _docker_available():
        logger.info("n8n-mcp: Docker unavailable; skipping auto-install (use the one-click Enable)")
        return
    token = secrets.token_hex(24)
    try:
        await _run_container(token)
    except Exception as e:  # noqa: BLE001
        logger.warning("n8n-mcp: auto-install container start failed: %s", e)
        return
    await _probe_and_register(token, "docs")


async def enable() -> dict:
    """One-click provision (docs mode). Returns a result for the UI."""
    if not await _docker_available():
        return {
            "ok": False,
            "error": "Docker is not reachable from the dashboard.",
            "manual": True,
        }
    token = secrets.token_hex(24)
    try:
        await _run_container(token)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Could not start n8n-mcp: {e}"}
    return await _probe_and_register(token, "docs")


async def upgrade() -> dict:
    """Recreate n8n-mcp wired to the active instance (full mode) so the workflow
    create/update/manage tools light up."""
    from backend.config import get_active_instance
    from backend.modules.n8n_proxy.client import dockerize_url

    if not await _docker_available():
        return {"ok": False, "error": "Docker is not reachable from the dashboard.", "manual": True}
    inst = get_active_instance()
    if not inst:
        return {"ok": False, "error": "No active n8n instance. Connect one first."}
    # n8n-mcp reaches n8n from its OWN container; dockerize_url maps a localhost
    # URL to the host gateway so a sibling container can reach it.
    n8n_url = dockerize_url(decrypt_value(inst.get("url", ""))).rstrip("/")
    n8n_key = decrypt_value(inst.get("api_key", ""))
    if not n8n_url or not n8n_key:
        return {"ok": False, "error": "Active instance is missing a URL or API key."}
    token = secrets.token_hex(24)
    try:
        await _run_container(token, n8n_url=n8n_url, n8n_key=n8n_key)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Could not start n8n-mcp: {e}"}
    return await _probe_and_register(token, "full")


async def disable() -> dict:
    """Stop + remove the managed container and unregister the server."""
    unregistered = _unregister()
    removed = False
    if await _docker_available():
        try:
            from backend.modules.docker_mgr import client as dc
            await dc.destroy_container(CONTAINER_NAME)
            removed = True
        except Exception as e:  # noqa: BLE001
            logger.debug("n8n-mcp: container teardown failed (may not exist): %s", e)
    return {"ok": True, "unregistered": unregistered, "container_removed": removed}
