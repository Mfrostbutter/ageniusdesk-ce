"""Async Docker client — thin wrapper around aiodocker.

A single aiodocker.Docker instance is reused across requests. It connects
to /var/run/docker.sock by default (DOCKER_HOST env overrides for TCP).
All functions raise RuntimeError if Docker is unreachable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

import aiodocker

logger = logging.getLogger(__name__)

_docker: aiodocker.Docker | None = None


def _get_client() -> aiodocker.Docker:
    global _docker
    if _docker is None:
        _docker = aiodocker.Docker()
    return _docker


async def close() -> None:
    global _docker
    if _docker is not None:
        await _docker.close()
        _docker = None


async def ping() -> bool:
    """Return True if the Docker daemon is reachable."""
    try:
        await _get_client().system.info()
        return True
    except Exception:
        return False


def _normalize(raw: dict) -> dict:
    labels = raw.get("Labels") or {}
    seen_ports: set[str] = set()
    ports: list[str] = []
    for p in raw.get("Ports") or []:
        if p.get("PublicPort"):
            key = f"{p['PublicPort']}→{p['PrivatePort']}/{p['Type']}"
        else:
            key = f"{p['PrivatePort']}/{p['Type']}"
        if key not in seen_ports:
            seen_ports.add(key)
            ports.append(key)

    names = raw.get("Names") or []
    name = names[0].lstrip("/") if names else raw.get("Id", "")[:12]

    return {
        "id": raw["Id"][:12],
        "id_full": raw["Id"],
        "name": name,
        "image": raw.get("Image", ""),
        "state": raw.get("State", ""),
        "status": raw.get("Status", ""),
        "created": raw.get("Created", 0),
        "ports": ports,
        "compose_project": labels.get("com.docker.compose.project", ""),
        "compose_service": labels.get("com.docker.compose.service", ""),
        "labels": labels,
    }


async def list_containers(all: bool = True) -> list[dict]:
    try:
        containers = await _get_client().containers.list(all=all)
        return [_normalize(c._container) for c in containers]
    except Exception as exc:
        raise RuntimeError(f"Docker list failed: {exc}") from exc


async def inspect_container(container_id: str) -> dict:
    try:
        c = await _get_client().containers.get(container_id)
        return await c.show()
    except Exception as exc:
        raise RuntimeError(f"Inspect failed: {exc}") from exc


async def container_action(container_id: str, action: str) -> None:
    """Perform start | stop | restart | pause | unpause on a container."""
    try:
        c = await _get_client().containers.get(container_id)
        if action == "start":
            await c.start()
        elif action == "stop":
            await c.stop()
        elif action == "restart":
            await c.restart()
        elif action == "pause":
            await c.pause()
        elif action == "unpause":
            await c.unpause()
        else:
            raise ValueError(f"Unknown action: {action}")
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        raise RuntimeError(f"Action '{action}' failed: {exc}") from exc


async def stream_logs(
    container_id: str,
    tail: int = 200,
    follow: bool = False,
) -> AsyncGenerator[str, None]:
    """Yield log lines from a container. follow=True streams indefinitely."""
    try:
        c = await _get_client().containers.get(container_id)
        if follow:
            # c.log(follow=True) returns an async generator directly -- no await.
            async for line in c.log(stdout=True, stderr=True, follow=True, tail=tail):
                yield line if isinstance(line, str) else line.decode("utf-8", errors="replace")
        else:
            # c.log(follow=False) returns a coroutine that resolves to List[str] -- must await.
            lines = await c.log(stdout=True, stderr=True, follow=False, tail=tail)
            for line in lines:
                yield line if isinstance(line, str) else line.decode("utf-8", errors="replace")
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        yield f"[ERROR] Log stream failed: {exc}\n"


async def list_images() -> list[dict]:
    try:
        images = await _get_client().images.list()
        result = []
        for img in images:
            raw = img._image if hasattr(img, "_image") else img
            tags = raw.get("RepoTags") or []
            result.append({
                "id": raw.get("Id", "")[:19],
                "tags": [t for t in tags if t != "<none>:<none>"],
                "size_mb": round(raw.get("Size", 0) / 1_048_576, 1),
                "created": raw.get("Created", 0),
            })
        return result
    except Exception as exc:
        raise RuntimeError(f"Image list failed: {exc}") from exc


async def destroy_container(container_id: str, remove_volumes: bool = False) -> list[str]:
    """Stop (if running) then remove a container. Returns names of removed volumes."""
    try:
        c = await _get_client().containers.get(container_id)
        info = await c.show()
    except Exception as exc:
        raise RuntimeError(f"Container not found: {exc}") from exc

    vol_names: list[str] = []
    if remove_volumes:
        binds = (info.get("HostConfig") or {}).get("Binds") or []
        vol_names = [b.split(":")[0] for b in binds if not b.startswith("/")]

    state = (info.get("State") or {}).get("Status", "")
    if state in ("running", "restarting", "paused"):
        try:
            await c.stop()
        except Exception:
            pass  # Best effort; force delete below will catch it.
    try:
        await c.delete()
    except Exception:
        # Docker refuses to remove a "restarting" container without force.
        # Crash-loops land here. Force removal so the bundle destroy path
        # doesn't get stuck on a misconfigured member.
        await c.delete(force=True)

    removed: list[str] = []
    for vol in vol_names:
        try:
            await _get_client()._query_json(f"volumes/{vol}", method="DELETE")
            removed.append(vol)
        except Exception:
            pass
    return removed


async def get_recreate_config(container_id: str) -> tuple[dict, str]:
    """Inspect a container and return the config needed to recreate it."""
    try:
        c = await _get_client().containers.get(container_id)
        info = await c.show()
    except Exception as exc:
        raise RuntimeError(f"Container not found: {exc}") from exc

    name = info["Name"].lstrip("/")
    config = {
        "Image": info["Config"]["Image"],
        "Env": info["Config"].get("Env") or [],
        "Labels": info["Config"].get("Labels") or {},
        "HostConfig": info["HostConfig"],
    }
    return config, name


async def system_info() -> dict:
    try:
        info = await _get_client().system.info()
        return {
            "containers": info.get("Containers", 0),
            "running": info.get("ContainersRunning", 0),
            "paused": info.get("ContainersPaused", 0),
            "stopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "docker_version": info.get("ServerVersion", ""),
            "os": info.get("OperatingSystem", ""),
            "arch": info.get("Architecture", ""),
            "memory_gb": round(info.get("MemTotal", 0) / 1_073_741_824, 1),
        }
    except Exception as exc:
        raise RuntimeError(f"System info failed: {exc}") from exc
