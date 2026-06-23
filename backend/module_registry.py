"""Module registry — manifest loading, validation, and state tracking.

This is the foundation of AgeniusDesk's plugin system. Every module (built-in
or community) ships a manifest.json describing its id, routes prefix, declared
secrets, and optional frontend contribution. The registry holds the live state
(loaded/failed/incompatible/missing_secrets) so the UI and installer can query
it without poking at the filesystem.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BUILTIN_MODULES_DIR = Path(__file__).parent / "modules"
COMMUNITY_MODULES_DIR = Path("data/modules")


def _read_app_version() -> str:
    """Pull version from pyproject.toml; fall back to 0.0.0 so we never crash.

    We regex-parse instead of using tomllib so this works on Python 3.10
    (tomllib is stdlib only in 3.11+).
    """
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    try:
        text = pyproject.read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        return match.group(1) if match else "0.0.0"
    except Exception:
        return "0.0.0"


APP_VERSION = _read_app_version()


# ── Manifest schema ──────────────────────────────────────────────────────────


class SecretRequirement(BaseModel):
    key: str
    description: str = ""
    required: bool = True


class NavEntry(BaseModel):
    label: str
    icon: str = ""
    # Built-in modules reference a view id registered in app.js `views`.
    # Community modules reference a relative HTML path served at
    # /modules/{id}/static/{view}.
    view: str = ""


class FrontendDecl(BaseModel):
    nav: NavEntry | None = None
    views: list[str] = Field(default_factory=list)
    scripts: list[str] = Field(default_factory=list)


class ModuleManifest(BaseModel):
    id: str
    name: str
    version: str = "1.0.0"
    min_app_version: str = "0.0.0"
    description: str = ""
    author: str = ""
    author_url: str = ""
    repo: str = ""
    license: str = ""
    routes_prefix: str = ""
    python_entry: str = "__init__.py"
    secrets_required: list[SecretRequirement] = Field(default_factory=list)
    frontend: FrontendDecl | None = None
    builtin: bool = False
    homepage: str = ""


# ── Live registry ────────────────────────────────────────────────────────────


Status = Literal["loaded", "failed", "incompatible", "missing_secrets", "disabled"]


class RegistryEntry(BaseModel):
    manifest: ModuleManifest
    status: Status
    source: Literal["builtin", "community"]
    error: str | None = None
    missing_secrets: list[str] = Field(default_factory=list)
    path: str = ""
    installed_sha: str | None = None


_registry: dict[str, RegistryEntry] = {}


def get_registry() -> dict[str, RegistryEntry]:
    return _registry


def register(entry: RegistryEntry) -> None:
    _registry[entry.manifest.id] = entry


def unregister(module_id: str) -> None:
    _registry.pop(module_id, None)


def clear_registry() -> None:
    _registry.clear()


# ── Loading helpers ──────────────────────────────────────────────────────────


def load_manifest(module_dir: Path) -> ModuleManifest | None:
    """Read manifest.json from a module directory. Returns None if absent/invalid."""
    manifest_path = module_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text())
        return ModuleManifest(**data)
    except Exception as e:
        logger.warning("Invalid manifest in %s: %s", module_dir.name, e)
        return None


def version_tuple(v: str) -> tuple[int, ...]:
    """Parse 'X.Y.Z' into an int tuple. Non-numeric segments become 0."""
    parts = []
    for seg in v.split("."):
        try:
            parts.append(int(seg.split("-")[0]))  # strip pre-release suffix
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_compatible(min_app_version: str) -> bool:
    """True when current app version satisfies the module's min_app_version."""
    try:
        return version_tuple(min_app_version) <= version_tuple(APP_VERSION)
    except Exception:
        return True  # permissive on malformed versions


def check_secrets(manifest: ModuleManifest) -> list[str]:
    """Return list of required secret keys that aren't present in the store."""
    from backend.config import load_secrets

    stored = load_secrets()
    missing = []
    for req in manifest.secrets_required:
        if req.required and req.key not in stored:
            missing.append(req.key)
    return missing


# ── Fallback manifest for built-ins without one (transition period) ──────────


def synthesize_builtin_manifest(module_dir: Path) -> ModuleManifest:
    """Build a minimal manifest for a built-in module that hasn't been
    retrofitted yet. Used so register_modules() keeps working during the
    rollout of manifest.json across all built-ins.
    """
    mod_id = module_dir.name
    return ModuleManifest(
        id=mod_id,
        name=mod_id.replace("_", " ").title(),
        version="0.0.0",
        min_app_version="0.0.0",
        builtin=True,
    )
