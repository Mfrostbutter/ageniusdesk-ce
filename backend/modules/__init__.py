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
    if COMMUNITY_MODULES_DIR.exists():
        for child in sorted(COMMUNITY_MODULES_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            _register_community(app, child)

    for entry in module_registry.get_registry().values():
        if entry.status in ("loaded", "missing_secrets"):
            loaded.append(entry.manifest.id)

    return loaded
