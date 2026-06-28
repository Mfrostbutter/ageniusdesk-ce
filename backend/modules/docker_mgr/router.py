"""Docker management API routes."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.config import get_instances, remove_instance, settings

from . import bundle as bundle_mod
from . import client as docker
from . import deployer, templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/containers", tags=["containers"], dependencies=[Depends(require_role("operator"))])


def _docker_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Docker daemon unreachable. Make sure /var/run/docker.sock is mounted.",
    )


async def _guard_not_self(container_id: str, action: str) -> None:
    """Refuse a destructive action on the container the dashboard runs in.

    Destroying / stopping / recreating our own container from inside the app
    takes the dashboard down (and a failed recreate could leave it gone). That
    is a Docker-Desktop / host operation, never an in-app one.
    """
    try:
        is_self = await docker.is_self_container(container_id)
    except Exception:
        is_self = False
    if is_self:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Refusing to {action} the AgeniusDesk dashboard's own container. "
                "Manage the dashboard container from Docker Desktop or the host instead."
            ),
        )


# ── Status ────────────────────────────────────────────────────────────────────


@router.get("/status")
async def docker_status():
    reachable = await docker.ping()
    if not reachable:
        return {"reachable": False}
    try:
        info = await docker.system_info()
        return {"reachable": True, **info}
    except Exception:
        return {"reachable": True}


# ── Container list ────────────────────────────────────────────────────────────


@router.get("")
async def list_containers(all: bool = True, project: str = ""):
    try:
        containers = await docker.list_containers(all=all)
    except RuntimeError as exc:
        raise _docker_unavailable() from exc

    if project:
        containers = [c for c in containers if c["compose_project"] == project]

    # Flag the dashboard's own container so the UI can mark it and hide its
    # destructive controls (it must be managed from Docker Desktop, not here).
    self_full, self_name = await docker.self_container()
    for c in containers:
        c["is_self"] = bool(
            (self_full and c.get("id_full") == self_full)
            or (self_name and c.get("name") == self_name)
        )

    # Sort: running first, then alphabetical by name.
    containers.sort(key=lambda c: (0 if c["state"] == "running" else 1, c["name"]))
    return {"count": len(containers), "containers": containers}


@router.get("/projects")
async def list_compose_projects():
    """Return distinct compose project names with container counts."""
    try:
        containers = await docker.list_containers(all=True)
    except RuntimeError as exc:
        raise _docker_unavailable() from exc

    projects: dict[str, dict] = {}
    for c in containers:
        proj = c["compose_project"]
        if not proj:
            continue
        if proj not in projects:
            projects[proj] = {"name": proj, "total": 0, "running": 0}
        projects[proj]["total"] += 1
        if c["state"] == "running":
            projects[proj]["running"] += 1

    return {"projects": sorted(projects.values(), key=lambda x: x["name"])}


@router.get("/images")
async def list_images():
    try:
        images = await docker.list_images()
    except RuntimeError as exc:
        raise _docker_unavailable() from exc
    images.sort(key=lambda i: -i["created"])
    return {"count": len(images), "images": images}


# ── Host aliases ──────────────────────────────────────────────────────────────


def _collect_host_aliases() -> list[str]:
    """Return all hostnames/IPs that are considered "local" to this Docker host.

    Includes:
    - Canonical loopback aliases
    - host.docker.internal (Docker Desktop / Linux 20.10+ bridge gateway)
    - All IPs bound to the container's own network interfaces (via socket)
    - The default-route gateway IP from /proc/net/route (= Docker host LAN IP
      when the container is on a bridge network)
    """
    aliases: set[str] = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

    # Container's own IPs via socket.
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            aliases.add(info[4][0])
    except Exception:
        pass

    # Default-gateway IP from /proc/net/route — on Linux/Docker this is the
    # Docker bridge gateway which routes to the host's LAN IP.  We treat it as
    # a local alias because port bindings on 0.0.0.0 are reachable from it.
    try:
        with open("/proc/net/route") as fh:
            for line in fh:
                parts = line.strip().split()
                if len(parts) < 3:
                    continue
                # Default route: Destination == 00000000
                if parts[1] == "00000000" and parts[2] != "00000000":
                    # Gateway field is little-endian hex IPv4
                    gateway_hex = parts[2]
                    gateway_bytes = bytes.fromhex(gateway_hex)[::-1]  # reverse for little-endian
                    gateway_ip = socket.inet_ntoa(gateway_bytes)
                    aliases.add(gateway_ip)
                    break
    except Exception:
        pass

    # Operator-supplied aliases via AGD_HOST_ALIASES env var.
    # These are needed when the dashboard runs inside Docker and /proc/net/route
    # only sees the container's bridge gateway — not the host's LAN IP.
    raw_env = settings.agd_host_aliases.strip()
    if raw_env:
        for part in raw_env.split(","):
            stripped = part.strip().lower()
            if stripped:
                aliases.add(stripped)

    # Normalise: lowercase everything so comparisons are case-insensitive.
    return sorted(a.lower() for a in aliases)


@router.get("/host-aliases")
async def host_aliases(request: Request):
    """Return the set of hostnames and IPs considered local to this Docker host.

    The frontend uses this to decide whether an n8n instance URL points to a
    container managed by this dashboard (and therefore eligible for auto-update),
    and to synthesize "Open container" URLs that work for remote dashboards.
    """
    return {
        "aliases": _collect_host_aliases(),
        "public_host": _public_host_from_request(request),
    }


@router.get("/public-host")
async def public_host(request: Request):
    """Return the hostname the operator's browser should use to reach
    container ports published on this Docker host."""
    return {"public_host": _public_host_from_request(request)}


def _public_host_from_request(request: Request) -> str:
    """Resolve the public host string for synthesized container URLs.

    Precedence:
      1. AGD_PUBLIC_HOST env var (operator override, right for prod)
      2. Request Host header, port stripped (works for any deployment where
         the browser already knows how to reach the dashboard host)
      3. "localhost" (single-machine dev fallback)
    """
    override = settings.agd_public_host.strip()
    if override:
        return override.split(":", 1)[0]
    host_header = (request.headers.get("host") or "").strip()
    if host_header:
        return host_header.split(":", 1)[0]
    return "localhost"


# ── Inspect ───────────────────────────────────────────────────────────────────


@router.get("/{container_id}/inspect")
async def inspect(container_id: str):
    try:
        return await docker.inspect_container(container_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── Destroy ───────────────────────────────────────────────────────────────────


def _is_local_host(host: str) -> bool:
    """True when `host` clearly refers to this machine: localhost, an *.local
    name, or a private / loopback IP. A public hostname or routable IP returns
    False, so we never port-match an instance that points at a remote n8n.
    """
    h = (host or "").strip().lower()
    if not h:
        return False
    if h == "localhost" or h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


async def _container_candidate_urls(container_id: str) -> tuple[list[str], set[str]]:
    """Return (candidate URLs, published host ports) for a container.

    Candidate URLs are http://localhost:<hostPort> / http://127.0.0.1:<hostPort>
    per TCP mapping plus http://<compose_service>:<containerPort>. The host-port
    set lets eviction also match instances registered by LAN IP (which the
    container can't enumerate as a hostname). Used to orphan-detect registered
    n8n instances after a destroy so stale entries clean up automatically.
    """
    try:
        c = await docker._get_client().containers.get(container_id)
        info = await c.show()
    except Exception:
        return [], set()

    candidates: list[str] = []
    host_ports: set[str] = set()
    port_bindings = (info.get("HostConfig") or {}).get("PortBindings") or {}
    service = (info.get("Config") or {}).get("Labels", {}).get("com.docker.compose.service", "")

    for port_proto, bindings in port_bindings.items():
        container_port = port_proto.split("/")[0]
        for b in bindings or []:
            host_port = b.get("HostPort", "")
            if host_port:
                host_ports.add(str(host_port))
                candidates.append(f"http://localhost:{host_port}")
                candidates.append(f"http://127.0.0.1:{host_port}")
        if service and container_port:
            candidates.append(f"http://{service}:{container_port}")

    return candidates, host_ports


def _evict_orphaned_instances(candidate_urls: list[str], host_ports: set[str] | None = None) -> list[str]:
    """Remove any registered instances that point at the destroyed container.

    Matches on an exact candidate URL, or — for an instance that points at a
    local host (private/loopback/localhost) — on the published host port. The
    port fallback covers instances registered by LAN IP (e.g.
    http://10.10.0.15:5678), which can't appear in the candidate-URL list.
    Returns the names of removed instances so callers can surface them.
    """
    host_ports = host_ports or set()
    if not candidate_urls and not host_ports:
        return []
    normalised = {u.rstrip("/").lower() for u in candidate_urls}
    evicted: list[str] = []
    for inst in get_instances():
        raw = inst.get("url", "")
        matched = raw.rstrip("/").lower() in normalised
        if not matched and host_ports:
            try:
                parts = urlsplit(raw)
                if parts.port and str(parts.port) in host_ports and _is_local_host(parts.hostname or ""):
                    matched = True
            except ValueError:
                pass
        if matched:
            remove_instance(inst["id"])
            evicted.append(inst.get("name", inst["id"]))
    return evicted


@router.delete("/{container_id}")
async def destroy_container(
    container_id: str,
    remove_volumes: bool = Query(default=False),
):
    """Stop and permanently remove a container.

    remove_volumes=true also deletes named volumes that were bind-mounted
    into the container (data volumes for managed deployments).

    Any registered n8n instances whose URL matches a port mapping of the
    destroyed container are automatically de-registered.
    """
    await _guard_not_self(container_id, "destroy")
    candidates, host_ports = await _container_candidate_urls(container_id)

    try:
        removed = await docker.destroy_container(container_id, remove_volumes=remove_volumes)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    evicted = _evict_orphaned_instances(candidates, host_ports)
    return {"ok": True, "volumes_removed": removed, "instances_removed": evicted}


# ── Bundle operations (multi-container templates) ────────────────────────────


async def _bundle_members(bundle_id: str) -> list[dict]:
    """Return raw container dicts for every member of a bundle.

    Selects on the `ageniusdesk.bundle` label, which the deployer stamps on
    every spec at create time. Returns an empty list if the bundle doesn't
    exist (already destroyed, never deployed, or wrong id).
    """
    try:
        containers = await docker.list_containers(all=True)
    except RuntimeError:
        return []
    return [c for c in containers if (c.get("labels") or {}).get("ageniusdesk.bundle") == bundle_id]


@router.get("/bundle/{bundle_id:path}")
async def get_bundle(bundle_id: str):
    """Return the member containers + metadata for a deployed bundle.

    bundle_id is `<template_id>:<instance_name>`. FastAPI's `:path` converter
    is required so the colon is preserved.
    """
    members = await _bundle_members(bundle_id)
    if not members:
        raise HTTPException(status_code=404, detail=f"Bundle not found: {bundle_id}")
    try:
        template_id, instance_name = bundle_mod.parse_bundle_id(bundle_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "bundle_id": bundle_id,
        "template_id": template_id,
        "instance_name": instance_name,
        "containers": members,
    }


@router.delete("/bundle/{bundle_id:path}")
async def destroy_bundle(
    bundle_id: str,
    remove_volumes: bool = Query(default=False),
):
    """Stop and remove every member of a bundle, then the bundle network.

    Cascade-orphan-evicts registered instances whose URL matches any member's
    port mappings.
    """
    members = await _bundle_members(bundle_id)
    if not members:
        raise HTTPException(status_code=404, detail=f"Bundle not found: {bundle_id}")

    removed_containers: list[str] = []
    volumes_removed_total = False
    evicted_total: list[str] = []

    # Destroy in reverse-topo order would be ideal, but we don't have the
    # topo here. Docker's container removal handles the dependency just fine
    # for the destroy path (containers can be removed in any order; the
    # network is removed after all members are gone).
    for c in members:
        try:
            candidates, host_ports = await _container_candidate_urls(c["id_full"])
            removed = await docker.destroy_container(c["id_full"], remove_volumes=remove_volumes)
            removed_containers.append(c["name"])
            if removed:
                volumes_removed_total = True
            evicted_total.extend(_evict_orphaned_instances(candidates, host_ports))
        except RuntimeError as exc:
            logger.warning("Bundle %s: failed to destroy member %s: %s", bundle_id, c["name"], exc)

    # Best-effort network teardown.
    try:
        template_id, instance_name = bundle_mod.parse_bundle_id(bundle_id)
        net_name = bundle_mod.bundle_network_name(template_id, instance_name)
        await docker._get_client()._query_json(f"networks/{net_name}", method="DELETE")
    except Exception as exc:  # noqa: BLE001
        logger.info("Bundle %s: network teardown skipped: %s", bundle_id, exc)

    return {
        "ok": True,
        "removed": removed_containers,
        "volumes_removed": volumes_removed_total,
        "instances_removed": evicted_total,
    }


@router.post("/bundle/{bundle_id:path}/recreate")
async def recreate_bundle_route(bundle_id: str, request: Request):
    """Pull latest images and recreate every member of a bundle in topo order.

    Returns a deploy_id; the SSE progress stream uses the same protocol as
    the initial bundle deploy.
    """
    if not await docker.ping():
        raise _docker_unavailable()

    deploy_id = deployer.new_deploy_id()
    deployer.register(deploy_id)
    asyncio.create_task(
        deployer.recreate_bundle(
            deploy_id,
            bundle_id,
            public_host=_public_host_from_request(request),
        )
    )
    return {"ok": True, "deploy_id": deploy_id, "bundle_id": bundle_id}


# ── Recreate (rolling update) ─────────────────────────────────────────────────


@router.post("/{container_id}/recreate")
async def recreate_container(container_id: str):
    """Pull the latest image and recreate the container with its current config.

    Returns a deploy_id — connect to GET /deploy/{id}/progress for SSE updates.
    """
    await _guard_not_self(container_id, "recreate")
    try:
        config, name = await docker.get_recreate_config(container_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not await docker.ping():
        raise _docker_unavailable()

    deploy_id = deployer.new_deploy_id()
    deployer.register(deploy_id)
    asyncio.create_task(deployer.recreate(deploy_id, config, name))
    return {"ok": True, "deploy_id": deploy_id, "container_name": name}


# ── Actions ───────────────────────────────────────────────────────────────────

_VALID_ACTIONS = {"start", "stop", "restart", "pause", "unpause"}
# Actions that would leave the dashboard DOWN if run against its own container.
# restart is allowed: it bounces but recovers (restart policy brings it back).
# start / unpause are harmless. stop / pause / destroy / recreate are blocked.
_SELF_PROTECTED_ACTIONS = {"stop", "pause"}


@router.post("/{container_id}/{action}")
async def container_action(container_id: str, action: str):
    if action not in _VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action. Choose from: {', '.join(_VALID_ACTIONS)}")
    if action in _SELF_PROTECTED_ACTIONS:
        await _guard_not_self(container_id, action)
    try:
        await docker.container_action(container_id, action)
        return {"ok": True, "action": action, "container_id": container_id}
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── Log streaming (Server-Sent Events) ───────────────────────────────────────


# ── Deployment templates ──────────────────────────────────────────────────────


@router.get("/templates")
async def list_templates():
    """Return available deployment templates (n8n, etc.)."""
    return {"templates": templates.as_json()}


# Host ports Chrome and most browsers refuse to open (ERR_UNSAFE_PORT). Deploying
# a browser-facing service on one of these means the "Open" link silently fails,
# so we reject them up front. Mirrors Chromium's net::kRestrictedPorts.
CHROME_UNSAFE_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79,
    87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137,
    139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532,
    540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723,
    2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669,
    6697, 10080,
})


class _DeployRequest(BaseModel):
    template_id: str
    fields: dict[str, Any]


@router.post("/deploy")
async def start_deploy(body: _DeployRequest, request: Request):
    """Kick off a deployment. Returns a deploy_id to poll for progress via SSE."""
    template = templates.get(body.template_id)
    if template is None:
        raise HTTPException(status_code=400, detail=f"Unknown template: {body.template_id}")

    # Reject browser-blocked / out-of-range host ports before standing anything up.
    raw_port = body.fields.get("port")
    if raw_port is not None and str(raw_port) != "":
        try:
            port_int = int(raw_port)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid host port {raw_port!r}. Use a number 1-65535.")
        if port_int < 1 or port_int > 65535:
            raise HTTPException(status_code=400, detail=f"Host port {port_int} is out of range. Use 1-65535.")
        if port_int in CHROME_UNSAFE_PORTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Host port {port_int} is blocked by Chrome and most browsers (ERR_UNSAFE_PORT), "
                    "so you would not be able to open it. Choose a different port, e.g. 5678 or 8080."
                ),
            )

    if not await docker.ping():
        raise _docker_unavailable()

    deploy_id = deployer.new_deploy_id()
    deployer.register(deploy_id)
    asyncio.create_task(
        deployer.deploy(
            deploy_id,
            body.template_id,
            body.fields,
            public_host=_public_host_from_request(request),
        )
    )

    return {"ok": True, "deploy_id": deploy_id}


@router.get("/deploy/{deploy_id}/progress")
async def deploy_progress(deploy_id: str):
    """SSE stream of deployment events.

    Connect with EventSource('/api/containers/deploy/{id}/progress').
    Each event.data is a JSON object:
      {"event": "step", "message": "...", "detail": "..."}
      {"event": "done", "container_id": "...", "url": "..."}
      {"event": "error", "message": "..."}
      null  — end of stream sentinel
    """
    q = deployer.get_queue(deploy_id)
    if q is None:
        raise HTTPException(status_code=404, detail="deploy_not_found")

    async def generate():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=300)
                except asyncio.TimeoutError:
                    yield "data: null\n\n"
                    break
                yield f"data: {json.dumps(item)}\n\n"
                if item is None:
                    break
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{container_id}/logs")
async def stream_logs(
    container_id: str,
    tail: int = Query(default=200, ge=1, le=5000),
    follow: bool = Query(default=False),
):
    """Stream container logs as Server-Sent Events.

    Connect with EventSource in the browser:
        const es = new EventSource('/api/containers/{id}/logs?tail=200&follow=true');
        es.onmessage = e => { if (e.data === '__END__') es.close(); else log(e.data); };
    """
    async def _generate():
        try:
            async for line in docker.stream_logs(container_id, tail=tail, follow=follow):
                cleaned = line.rstrip("\n").rstrip("\r")
                if cleaned:
                    yield f"data: {json.dumps(cleaned)}\n\n"
            yield "data: \"__END__\"\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
