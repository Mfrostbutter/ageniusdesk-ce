"""Auto-discover and register API modules.

Each module subdirectory ships a `manifest.json` (retrofitted for built-ins,
required for community modules). Manifests drive:
  - routes_prefix declaration (audit trail)
  - min_app_version gating (incompatible modules are disabled, not crashed)
  - secrets_required (UI surfaces green/red per declared secret)
  - frontend contribution (nav entries loaded dynamically by app.js)

Load order:
  1. Built-in modules from backend/modules/{id}/
  2. Community modules from /data/modules/{id}/ (installed via /api/modules/install)

Community modules that fail to import are logged and registered with
status=failed so the module manager UI can show them as broken rather than
silently disappearing.
"""

import importlib
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI

from backend import module_registry
from backend.module_registry import (
    COMMUNITY_MODULES_DIR,
    RegistryEntry,
    check_secrets,
    is_compatible,
    load_manifest,
    synthesize_builtin_manifest,
)

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent

# Isolated-module spawns deferred from import-time registration to the lifespan
# (after the host bridge is listening). Each entry: (module_id, parent, caps).
_pending_isolated: list[tuple] = []


_ISOLATION_MODES = ("in_process", "subprocess", "container")


def _isolation_mode() -> str:
    """Community-module isolation mode: 'in_process' (default), 'subprocess', or
    'container'.

    Resolution: the AGD_MODULE_ISOLATION env var wins when set (an explicit ops
    override); otherwise the persisted operator setting (Settings > Modules
    toggle, `module_isolation` in data/config.json); otherwise the default
    'in_process', so existing installs are unaffected. Global for now (per-module
    opt-in is a later phase).
    """
    env = os.environ.get("AGD_MODULE_ISOLATION", "").strip().lower()
    if env in _ISOLATION_MODES:
        return env
    try:
        from backend.config import load_config
        v = (load_config().get("module_isolation") or "").strip().lower()
        if v in _ISOLATION_MODES:
            return v
    except Exception:
        pass
    return "in_process"


def _register_builtin(app: FastAPI, child: Path) -> None:
    """Load one built-in module from backend/modules/{child}."""
    manifest = load_manifest(child) or synthesize_builtin_manifest(child)
    entry_path = str(child)

    if not is_compatible(manifest.min_app_version):
        module_registry.register(RegistryEntry(
            manifest=manifest,
            status="incompatible",
            source="builtin",
            error=f"Requires app version >= {manifest.min_app_version}",
            path=entry_path,
        ))
        logger.warning("Module %s incompatible (needs %s)", manifest.id, manifest.min_app_version)
        return

    try:
        mod = importlib.import_module(f"backend.modules.{child.name}")
        if hasattr(mod, "router"):
            app.include_router(mod.router)
            missing = check_secrets(manifest)
            status = "missing_secrets" if missing else "loaded"
            module_registry.register(RegistryEntry(
                manifest=manifest,
                status=status,
                source="builtin",
                missing_secrets=missing,
                path=entry_path,
            ))
            logger.info("Registered built-in module: %s", manifest.id)
        else:
            logger.debug("Skipped module %s (no router)", child.name)
    except Exception as e:
        module_registry.register(RegistryEntry(
            manifest=manifest,
            status="failed",
            source="builtin",
            error=str(e),
            path=entry_path,
        ))
        logger.warning("Failed to load module %s: %s", child.name, e)


def _register_community(app: FastAPI, child: Path) -> None:
    """Load one community module from /data/modules/{child}.

    Community modules are added to sys.path under a synthetic package root so
    importlib can find them without them living inside backend/modules/.
    """
    manifest = load_manifest(child)
    if not manifest:
        logger.warning("Community module %s has no valid manifest.json, skipping", child.name)
        return

    entry_path = str(child)

    if not is_compatible(manifest.min_app_version):
        module_registry.register(RegistryEntry(
            manifest=manifest,
            status="incompatible",
            source="community",
            error=f"Requires app version >= {manifest.min_app_version}",
            path=entry_path,
        ))
        return

    if _isolation_mode() in ("subprocess", "container"):
        _register_community_isolated(app, child, manifest, entry_path)
        return

    try:
        # Add the community module's parent dir to sys.path so we can
        # `import {child.name}` as a top-level package.
        parent = str(child.parent.resolve())
        if parent not in sys.path:
            sys.path.insert(0, parent)

        mod = importlib.import_module(child.name)
        if hasattr(mod, "router"):
            app.include_router(mod.router)
            missing = check_secrets(manifest)
            status = "missing_secrets" if missing else "loaded"
            module_registry.register(RegistryEntry(
                manifest=manifest,
                status=status,
                source="community",
                missing_secrets=missing,
                path=entry_path,
            ))
            logger.info("Registered community module: %s", manifest.id)
        else:
            module_registry.register(RegistryEntry(
                manifest=manifest,
                status="failed",
                source="community",
                error="Module has no `router` attribute",
                path=entry_path,
            ))
    except Exception as e:
        module_registry.register(RegistryEntry(
            manifest=manifest,
            status="failed",
            source="community",
            error=str(e),
            path=entry_path,
        ))
        logger.warning("Failed to load community module %s: %s", child.name, e)


def _register_community_isolated(app: FastAPI, child: Path, manifest, entry_path: str) -> None:
    """Run a community module in a sandboxed subprocess and reverse-proxy to it.

    No host import of the module here: the worker imports it in a separate
    process with a scrubbed env and a blocked `backend` import. The host only
    spawns it and forwards /api/{id}/* to its loopback/UDS bind.

    The reverse-proxy route is registered now (routes must exist before the app
    serves), but the worker SPAWN is deferred to the lifespan via
    start_isolated_workers(): a worker that calls the host bridge during startup
    must find it already listening, and the bridge only starts in the lifespan.
    """
    from backend.modules._runtime import proxy

    try:
        proxy.register_proxy_route(app, manifest.id)
        missing = check_secrets(manifest)
        status = "missing_secrets" if missing else "loaded"
        module_registry.register(RegistryEntry(
            manifest=manifest,
            status=status,
            source="community",
            missing_secrets=missing,
            path=entry_path,
        ))
        # Do NOT source the worker env from the module's declared capabilities.env:
        # a module could name a host secret and have it forwarded. The worker gets
        # only the base allowlist; privileged actions go through the
        # capability-scoped host bridge (token minted from manifest.capabilities).
        _pending_isolated.append((manifest.id, child.parent, manifest.capabilities))
        logger.info("Registered community module (isolated, spawn deferred): %s", manifest.id)
    except Exception as e:
        module_registry.register(RegistryEntry(
            manifest=manifest,
            status="failed",
            source="community",
            error=str(e),
            path=entry_path,
        ))
        logger.warning("Failed to register isolated community module %s: %s", manifest.id, e)


def _mark_isolated_failed(module_id: str, err: Exception) -> None:
    entry = module_registry.get_registry().get(module_id)
    if entry is not None:
        module_registry.register(RegistryEntry(
            manifest=entry.manifest,
            status="failed",
            source="community",
            error=str(err),
            path=entry.path,
        ))
    logger.warning("Failed to start isolated community module %s: %s", module_id, err)


async def start_isolated_workers() -> None:
    """Spawn the deferred isolated-module workers (called from the app lifespan,
    AFTER the host bridge is listening).

    Subprocess tier: each spawn runs in a thread executor, because
    supervisor.start_worker blocks on a synchronous health wait and a worker may
    call the bridge during its own startup; running the wait off the event loop
    keeps the loop free to serve that bridge call instead of deadlocking.

    Container tier: spawn is natively async (aiodocker); a leftover-container
    sweep runs first.
    """
    if not _pending_isolated:
        return
    mode = _isolation_mode()
    pending = list(_pending_isolated)
    _pending_isolated.clear()

    # Sweep leftover module CONTAINERS from a prior run regardless of the current
    # tier, so switching container -> subprocess doesn't strand them. Best-effort
    # (no-op when Docker is unavailable).
    try:
        from backend.modules._runtime import containers as _sweep_containers
        await _sweep_containers.sweep_orphan_containers()
    except Exception as e:
        logger.warning("container orphan sweep failed: %s", e)

    if mode == "container":
        from backend.modules._runtime import containers
        for module_id, _parent, caps in pending:
            try:
                await containers.start_container_worker(module_id, capabilities=caps)
                logger.info("Started isolated community module (container): %s", module_id)
            except Exception as e:
                _mark_isolated_failed(module_id, e)
        return

    import asyncio
    import functools

    from backend.modules._runtime import supervisor

    loop = asyncio.get_running_loop()
    for module_id, parent, caps in pending:
        try:
            await loop.run_in_executor(
                None,
                functools.partial(
                    supervisor.start_worker, module_id, parent, capabilities=caps, forward_env=[]
                ),
            )
            logger.info("Started isolated community module: %s", module_id)
        except Exception as e:
            _mark_isolated_failed(module_id, e)


def register_modules(app: FastAPI) -> list[str]:
    """Scan built-in + community module dirs and mount routers.

    Returns list of successfully loaded module ids (for backward compat with
    the old function signature).
    """
    module_registry.clear_registry()
    loaded: list[str] = []

    # Built-ins
    for child in sorted(BUILTIN_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        _register_builtin(app, child)

    # Community modules (skip if dir doesn't exist — first-boot case)
    if _isolation_mode() == "subprocess":
        # Kill any workers left running by a previous host process before we
        # spawn fresh ones, so a crash/restart can't leak orphan subprocesses.
        try:
            from backend.modules._runtime import supervisor
            supervisor.sweep_orphans()
        except Exception as e:
            logger.warning("worker orphan sweep failed: %s", e)
    if COMMUNITY_MODULES_DIR.exists():
        for child in sorted(COMMUNITY_MODULES_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            _register_community(app, child)

    for entry in module_registry.get_registry().values():
        if entry.status in ("loaded", "missing_secrets"):
            loaded.append(entry.manifest.id)

    return loaded
