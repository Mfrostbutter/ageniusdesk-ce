"""Container tier: run each community-module worker in its own hardened Docker
container instead of a host subprocess.

Reuses the bridge + reverse proxy UNCHANGED; only the spawn mechanism and the
transport differ from the subprocess tier (supervisor.py), so module code is
identical across tiers.

Posture (see spec 2026-06-27 section 5.8):
  - Image: the dashboard's own image (self-discovered), worker bootstrap launched
    by absolute path. Host source is on the image but `import backend` is blocked
    and /app is not on the worker's sys.path; no host secrets ride along (the
    container env is ONLY the injected handshake vars, never the dashboard env).
  - Mounts: host data volume READ-ONLY for the module source; a per-module named
    volume READ-WRITE for its _data (jobs.db), chowned to a non-root uid.
  - Hardening: read-only rootfs + tmpfs /tmp, cap-drop ALL, no-new-privileges,
    non-root uid, pids/memory/cpu limits, NO docker socket.
  - Network: no-network modules join an internal network (bridge reachable, zero
    internet); network-declaring modules join an egress network (internet, any
    host in v1 — per-host allowlist is the deferred egress-proxy step).

Async throughout (aiodocker). Workers are registered in supervisor._workers so the
reverse proxy (supervisor.get) finds them the same way as subprocess workers.
"""

from __future__ import annotations

import logging
import socket

import httpx

logger = logging.getLogger(__name__)

CONTAINER_PREFIX = "agd-mod-"
NET_INTERNAL = "agd-mod-internal"
NET_EGRESS = "agd-mod-egress"
WORKER_PORT = 8000
LABEL_ROLE = "agd.role"
LABEL_MODULE = "agd.module"
ROLE_WORKER = "module-worker"

# v1 runs the worker as the image's default user (root) INSIDE a maximally
# hardened container (cap-drop ALL, read-only rootfs, no-new-privileges, no Docker
# socket, isolated network, resource limits). cap-drop ALL removes the root powers
# that matter; this is deterministic (no data-volume chown race). Dropping to a
# non-root uid is a documented hardening follow-up: set MOD_USER and re-enable the
# data-volume chown init below.
MOD_USER = ""  # "" = image default (root); e.g. "1000:1000" for non-root
MOD_UID = 1000
MEM_BYTES = 512 * 1024 * 1024
NANO_CPUS = 1_000_000_000  # 1.0 CPU
PIDS_LIMIT = 256

HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_S = 0.5

# Host paths inside the module container.
SRC_MOUNT = "/agd-host"      # dashboard data volume, read-only (module source under modules/)
DATA_MOUNT = "/data"         # per-module writable volume (the module's _data)


class ContainerError(RuntimeError):
    """A module container failed to spawn or never became healthy."""


# ── aiodocker client + dashboard self-discovery (cached) ──────────────────────

_docker = None
_self_cache: dict | None = None


def _client():
    global _docker
    if _docker is None:
        import aiodocker
        _docker = aiodocker.Docker()
    return _docker


async def close_docker() -> None:
    global _docker
    if _docker is not None:
        try:
            await _docker.close()
        finally:
            _docker = None


async def _self() -> dict:
    """Inspect the dashboard's own container to learn its image, name, and the
    named volume backing /app/data. Cached for the process lifetime."""
    global _self_cache
    if _self_cache is not None:
        return _self_cache
    import os
    hostname = socket.gethostname()  # Docker sets this to the container short id
    c = await _client().containers.get(hostname)
    info = await c.show()
    image = os.environ.get("AGD_MODULE_IMAGE") or info["Config"]["Image"]
    name = info["Name"].lstrip("/")
    data_volume = ""
    for m in info.get("Mounts", []):
        if m.get("Destination") == "/app/data" and m.get("Name"):
            data_volume = m["Name"]
            break
    if not data_volume:
        raise ContainerError("could not find the dashboard data volume (mount at /app/data)")
    _self_cache = {"image": image, "name": name, "data_volume": data_volume}
    logger.info("container tier: image=%s dashboard=%s data_volume=%s", image, name, data_volume)
    return _self_cache


# ── network + volume provisioning ─────────────────────────────────────────────


async def _ensure_network(name: str, internal: bool) -> None:
    import aiodocker
    try:
        await _client().networks.create({"Name": name, "Internal": internal, "CheckDuplicate": True})
        logger.info("container tier: created network %s (internal=%s)", name, internal)
    except aiodocker.exceptions.DockerError as e:
        if e.status not in (409, 500):  # already exists
            raise


async def _connect_self(network: str, dashboard: str) -> None:
    import aiodocker
    try:
        net = await _client().networks.get(network)
        await net.connect({"Container": dashboard})
        logger.info("container tier: connected %s to %s", dashboard, network)
    except aiodocker.exceptions.DockerError as e:
        if e.status not in (403, 409):  # already connected
            raise


async def ensure_infra() -> dict:
    """Create the two module networks and connect the dashboard to both. Returns
    the cached self info. Idempotent."""
    me = await _self()
    await _ensure_network(NET_INTERNAL, internal=True)
    await _ensure_network(NET_EGRESS, internal=False)
    await _connect_self(NET_INTERNAL, me["name"])
    await _connect_self(NET_EGRESS, me["name"])
    return me


async def _ensure_data_volume(module_id: str, image: str) -> str:
    """Create the per-module RW data volume and chown it to the worker uid (a
    fresh named volume is root-owned; the worker runs non-root). Idempotent."""
    import aiodocker
    vol = f"{CONTAINER_PREFIX}{module_id}-data"
    try:
        await _client().volumes.create({"Name": vol})
    except aiodocker.exceptions.DockerError as e:
        if e.status not in (409, 500):
            raise
    # Non-root only: chown the (root-owned) volume so the worker uid can write it.
    # Skipped for the v1 root worker, which writes the root-owned volume directly.
    if MOD_USER:
        init = await _client().containers.create(config={
            "Image": image,
            "Entrypoint": [],
            "Cmd": ["sh", "-c", f"chown -R {MOD_USER} {DATA_MOUNT} && chmod 0775 {DATA_MOUNT}"],
            "User": "0:0",
            "HostConfig": {"Binds": [f"{vol}:{DATA_MOUNT}"], "AutoRemove": False},
            "Labels": {LABEL_ROLE: "module-init", LABEL_MODULE: module_id},
        })
        try:
            await init.start()
            res = await init.wait()
            code = (res or {}).get("StatusCode", -1)
            if code != 0:
                logs = await init.log(stdout=True, stderr=True)
                logger.warning("data-volume chown init for %s exited %s: %s", module_id, code, "".join(logs)[:300])
        finally:
            try:
                await init.delete(force=True)
            except Exception:
                pass
    return vol


# ── the worker ────────────────────────────────────────────────────────────────


class ContainerWorker:
    """One community module running in its own hardened container, behind the
    same proxy-secret + capability-bridge contract as a subprocess worker."""

    def __init__(self, module_id: str, capabilities=None):
        import secrets as _secrets

        from backend.modules._runtime import bridge

        self.module_id = module_id
        self.capabilities = capabilities
        self.proxy_secret = _secrets.token_urlsafe(32)
        self.bridge_token = bridge.mint(module_id, capabilities)
        self.container_name = f"{CONTAINER_PREFIX}{module_id}"
        self.base_url = f"http://{self.container_name}:{WORKER_PORT}"
        self.container_id: str | None = None
        self._running = False
        self._client: httpx.AsyncClient | None = None
        self._client_loop = None
        self.started_at: float = 0.0
        self.restarts: int = 0

    # proxy interface ----------------------------------------------------------

    def is_alive(self) -> bool:
        return self._running

    @property
    def client(self) -> httpx.AsyncClient:
        import asyncio
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=None)
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    # lifecycle ----------------------------------------------------------------

    def _injected_env(self, me: dict) -> list[str]:
        """The container env: ONLY the handshake vars (no dashboard env copied, so
        no host secret can ride along). The bridge is reached by the dashboard
        container name on the shared network."""
        from backend.modules._runtime import bridge

        env = {
            "AGD_MODULE_ID": self.module_id,
            "AGD_MODULE_PARENT": f"{SRC_MOUNT}/modules",
            "AGD_MODULE_DATA_DIR": DATA_MOUNT,
            "AGD_HOST_ROOT": "/app",
            "AGD_PROXY_SECRET": self.proxy_secret,
            "AGD_WORKER_BIND": f"0.0.0.0:{WORKER_PORT}",
            "AGD_BRIDGE_URL": f"http://{me['name']}:{bridge._ensure_port()}",
            "AGD_BRIDGE_TOKEN": self.bridge_token,
            "HOME": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        return [f"{k}={v}" for k, v in env.items()]

    async def spawn(self) -> None:
        import time

        me = await ensure_infra()
        vol = await _ensure_data_volume(self.module_id, me["image"])
        network = NET_EGRESS if getattr(getattr(self.capabilities, "network", None), "enabled", False) else NET_INTERNAL

        # Remove any stale container with our name from a prior run.
        await self._remove_existing()

        config = {
            "Image": me["image"],
            "Cmd": ["python", "/app/agd_module_worker/main.py", "--agd-module", self.module_id],
            "Env": self._injected_env(me),
            "WorkingDir": DATA_MOUNT,
            "Labels": {LABEL_ROLE: ROLE_WORKER, LABEL_MODULE: self.module_id},
            "HostConfig": {
                "Binds": [
                    f"{me['data_volume']}:{SRC_MOUNT}:ro",
                    f"{vol}:{DATA_MOUNT}",
                ],
                "NetworkMode": network,
                "ReadonlyRootfs": True,
                "Tmpfs": {"/tmp": "rw,size=64m"},
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": PIDS_LIMIT,
                "Memory": MEM_BYTES,
                "NanoCpus": NANO_CPUS,
                "AutoRemove": False,
                "RestartPolicy": {"Name": "no"},
            },
        }
        if MOD_USER:
            config["User"] = MOD_USER
        c = await _client().containers.create(config=config, name=self.container_name)
        self.container_id = c.id
        await c.start()
        self.started_at = time.time()
        try:
            await self._wait_healthy()
            self._running = True
        except Exception:
            await self.stop_async()
            raise

    async def _wait_healthy(self) -> None:
        import asyncio

        deadline = asyncio.get_running_loop().time() + HEALTH_TIMEOUT_S
        headers = {"x-agd-proxy-secret": self.proxy_secret}
        last = ""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=3.0) as c:
            while asyncio.get_running_loop().time() < deadline:
                if not await self._container_running():
                    raise ContainerError(
                        f"container '{self.container_name}' exited before becoming healthy.\n{await self._logs()}"
                    )
                try:
                    r = await c.get("/_worker/health", headers=headers)
                    if r.status_code == 200:
                        logger.info("module container '%s' healthy", self.module_id)
                        return
                    last = f"health HTTP {r.status_code}"
                except httpx.HTTPError as e:
                    last = str(e)
                await asyncio.sleep(HEALTH_POLL_S)
        raise ContainerError(f"container '{self.container_name}' not healthy in {HEALTH_TIMEOUT_S}s ({last})")

    async def _container_running(self) -> bool:
        try:
            c = await _client().containers.get(self.container_name)
            info = await c.show()
            return (info.get("State") or {}).get("Running", False)
        except Exception:
            return False

    async def _logs(self, tail: int = 60) -> str:
        try:
            c = await _client().containers.get(self.container_name)
            lines = await c.log(stdout=True, stderr=True, follow=False, tail=tail)
            return "".join(line if isinstance(line, str) else line.decode("utf-8", "replace") for line in lines)
        except Exception:
            return "(no container log)"

    async def _remove_existing(self) -> None:
        try:
            c = await _client().containers.get(self.container_name)
            await c.delete(force=True)
        except Exception:
            pass

    async def stop_async(self) -> None:
        from backend.modules._runtime import bridge

        self._running = False
        try:
            bridge.revoke(self.bridge_token)
        except Exception:
            pass
        await self.aclose()
        await self._remove_existing()


# ── registry-integrated lifecycle (workers live in supervisor._workers) ───────


async def start_container_worker(module_id: str, capabilities=None) -> ContainerWorker:
    """Spawn (or replace) a module's container worker and block until healthy.
    Registered in supervisor._workers so the reverse proxy finds it."""
    from backend.modules._runtime import supervisor

    existing = supervisor._workers.pop(module_id, None)
    if existing is not None:
        await _stop_any(existing)
    worker = ContainerWorker(module_id, capabilities=capabilities)
    await worker.spawn()
    supervisor._workers[module_id] = worker
    return worker


async def _stop_any(worker) -> None:
    """Stop a worker of either tier (container = async, subprocess = sync)."""
    if isinstance(worker, ContainerWorker):
        await worker.stop_async()
    else:
        try:
            worker.stop()
        except Exception as e:  # pragma: no cover
            logger.warning("error stopping subprocess worker: %s", e)


async def stop_container_worker(module_id: str, *, remove_volume: bool = False) -> bool:
    from backend.modules._runtime import supervisor

    worker = supervisor._workers.pop(module_id, None)
    if worker is None:
        return False
    await _stop_any(worker)
    if remove_volume:
        await _remove_volume(module_id)
    return True


async def _remove_volume(module_id: str) -> None:
    try:
        vol = await _client().volumes.get(f"{CONTAINER_PREFIX}{module_id}-data")
        await vol.delete()
    except Exception:
        pass


async def stop_all_containers() -> None:
    from backend.modules._runtime import supervisor

    for module_id in [mid for mid, w in supervisor._workers.items() if isinstance(w, ContainerWorker)]:
        worker = supervisor._workers.pop(module_id, None)
        if worker is not None:
            try:
                await worker.stop_async()
            except Exception as e:  # pragma: no cover
                logger.warning("error stopping container worker %s: %s", module_id, e)


async def sweep_orphan_containers() -> None:
    """Remove leftover module-worker containers from a previous dashboard run."""
    try:
        containers = await _client().containers.list(all=True)
    except Exception as e:
        logger.warning("container orphan sweep: list failed: %s", e)
        return
    for c in containers:
        labels = (c._container.get("Labels") or {})
        if labels.get(LABEL_ROLE) == ROLE_WORKER:
            try:
                await c.delete(force=True)
                logger.info("swept orphan module container %s", labels.get(LABEL_MODULE, c.id[:12]))
            except Exception:
                pass
