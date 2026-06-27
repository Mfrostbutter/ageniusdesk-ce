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

from pydantic import BaseModel, Field, field_validator

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


# ── Module id validation ─────────────────────────────────────────────────────
#
# A module id is used as a filesystem path component (the install dir under
# data/modules/, and under out-of-process isolation also a run-socket path and a
# per-module data dir) and as a registry/token key. It must be a strict,
# cross-platform-safe slug. We forbid dots entirely: that removes '..' traversal
# AND the Windows trailing-dot alias (`a.` resolves to `a` on Windows, so two
# distinct ids could target one directory). We also reject Windows reserved
# device names. Enforced at every entry point: the manifest validator below (the
# install path) and a containment-checked resolver in the installer (the
# uninstall path, which takes the id straight from the URL).

MODULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Windows reserved device names. A directory named for one of these aliases the
# device on Windows, so reject them on every platform for portable installs. The
# slug regex already forces lowercase, so a lowercase set suffices.
_WIN_RESERVED = (
    frozenset({"con", "prn", "aux", "nul"})
    | frozenset(f"com{i}" for i in range(10))
    | frozenset(f"lpt{i}" for i in range(10))
)


def is_valid_module_id(module_id: str) -> bool:
    """True for a safe, cross-platform module id: a 1-64 char lowercase slug
    ([a-z0-9_-]) starting alphanumeric, with no dots (so no '..' and no Windows
    trailing-dot alias), no path separators, and not a Windows reserved device
    name."""
    if not isinstance(module_id, str) or not MODULE_ID_RE.match(module_id):
        return False
    return module_id not in _WIN_RESERVED


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


# ── Capability declaration ───────────────────────────────────────────────────
#
# A module author asserts intent here; the AST scanner (modules/scanner.py)
# checks the code against the declaration and surfaces any gap. A manifest with
# no `capabilities` block declares NOTHING, so any capability the scanner detects
# becomes an undeclared finding. This is declaration + heuristic review, not a
# sandbox; community modules still run in-process with full Python access.


class NetworkCapability(BaseModel):
    enabled: bool = False
    # Allowlist of hostnames/domains the module may reach (glob allowed, e.g.
    # "*.youtube.com"). Empty list with enabled=true means "any host" and is
    # itself a HIGH finding.
    hosts: list[str] = Field(default_factory=list)


class FilesystemCapability(BaseModel):
    # Paths under data/ the module writes. Anything outside is a finding.
    write_paths: list[str] = Field(default_factory=list)


class Capabilities(BaseModel):
    network: NetworkCapability = Field(default_factory=NetworkCapability)
    filesystem: FilesystemCapability = Field(default_factory=FilesystemCapability)
    subprocess: bool = False
    # Environment variable keys the module reads (beyond secrets_required).
    env: list[str] = Field(default_factory=list)


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
    # Declared capability surface. None means "declares nothing" (see above) —
    # distinct from an explicit all-false Capabilities() which still declares the
    # author looked at it. The scanner treats both as the empty declaration.
    capabilities: Capabilities | None = None
    # Optional detached signature over the manifest (base64). Key distribution is
    # out of scope for now; verification is best-effort/additive, and the field
    # shape is fixed here so authors can start signing. Absent = "unsigned".
    signature: str = ""

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        # The id becomes a filesystem path component and a registry key; reject
        # anything that isn't a safe slug so a manifest can never drive a path
        # outside data/modules/ (see is_valid_module_id). A bad id makes
        # load_manifest() return None, so the module is skipped, not loaded.
        if not is_valid_module_id(v):
            raise ValueError(
                f"invalid module id {v!r}: must be a lowercase slug [a-z0-9_-], "
                "1-64 chars, starting alphanumeric, no dots, not a reserved device name"
            )
        return v


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
