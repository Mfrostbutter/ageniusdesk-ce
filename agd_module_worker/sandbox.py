"""Sandbox primitives for the community-module worker.

PURE, host-independent helpers (no `backend` import, import-light) used by the
bootstrap to drop host credentials, host source, and host imports before any
module code runs. Each is independently testable.

Three layers, from softest to hardest guarantee:
  - build_worker_env: an ALLOWLIST environment (never a blocklist) so host
    secrets cannot leak into the worker, even ones added later.
  - curate_sys_path: rebuild sys.path to keep stdlib + site-packages and the
    module's parent dir, but drop the host source root.
  - install_import_blocker: a sys.meta_path finder that refuses `import backend`
    even if the host package is reachable (site-packages or an uncurated path).
    This is the reliable guarantee; the other two are defense in depth.
"""

from __future__ import annotations

import os
import sys

# Env names a worker may inherit verbatim. Everything else (every secret, token,
# DB path, host config) is dropped. Allowlist, not blocklist: a newly added host
# secret can never silently appear in a worker.
_ENV_ALLOW = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD",
        "LANG", "LANGUAGE", "TZ",
        "TMPDIR", "TEMP", "TMP",
        "PYTHONHASHSEED", "PYTHONUNBUFFERED", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE",
        # Windows runtime essentials (sockets/asyncio fail without these; many
        # libraries call Path.home(), which needs USERPROFILE/HOMEDRIVE/HOMEPATH).
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
        "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME",
    }
)
_ENV_ALLOW_PREFIXES = ("LC_",)

# A name that looks like a credential. A module's declared capabilities.env may
# request extra inherited vars, but never one that looks secret.
_SECRET_SUBSTRINGS = (
    "secret", "token", "password", "passwd", "api_key", "apikey",
    "private", "credential", "passphrase",
)
_SECRET_SUFFIXES = ("_key",)


def is_secret_like(name: str) -> bool:
    """True if an env var name looks like a credential and must never be forwarded."""
    n = name.lower()
    if any(s in n for s in _SECRET_SUBSTRINGS):
        return True
    return any(n.endswith(suf) for suf in _SECRET_SUFFIXES)


def build_worker_env(
    parent_env: dict[str, str],
    injected: dict[str, str],
    declared_env: list[str] | None = None,
) -> dict[str, str]:
    """Build the allowlisted environment for a worker subprocess.

    Starts empty; copies only allowlisted names from `parent_env`, then any
    declared (non-secret) env the module asked for, then the `injected` worker
    vars (authoritative, applied last). Host secrets in `parent_env` never
    appear in the result.
    """
    out: dict[str, str] = {}
    for k, v in parent_env.items():
        ku = k.upper()
        if ku in _ENV_ALLOW or any(ku.startswith(p) for p in _ENV_ALLOW_PREFIXES):
            out[k] = v
    for k in declared_env or []:
        if k in out or is_secret_like(k):
            continue
        if k in parent_env:
            out[k] = parent_env[k]
    out.update(injected)
    return out


def curate_sys_path(current: list[str], module_parent: str, host_root: str) -> list[str]:
    """Return a sys.path with the host SOURCE ROOT removed (so `import backend`
    from source fails) and the module's parent dir present.

    Deliberately a denylist, not an allowlist: everything else (stdlib, the
    Windows DLLs dir, lib-dynload, site-packages) is kept, because aggressively
    reconstructing the runtime path drops C-extension dirs like `_socket`. The
    import blocker is the guarantee against `backend` in site-packages; this only
    needs to drop the one directory where the host source lives.
    """
    host_real = os.path.realpath(host_root) if host_root else None
    keep: list[str] = []
    for p in current:
        rp = os.path.realpath(p) if p else os.path.realpath(os.getcwd())
        if host_real and rp == host_real:
            continue
        keep.append(p)
    if module_parent and module_parent not in keep:
        keep.insert(0, module_parent)
    return keep


class BlockedHostImportError(ModuleNotFoundError):
    """Raised when a worker tries to import the host package."""


class HostImportBlocker:
    """A sys.meta_path finder that refuses to import the host package.

    Guarantees `import backend` / `import backend.x` fail even if the host
    package is present in site-packages or on an uncurated path.
    """

    blocked: tuple[str, ...] = ("backend",)

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001 (stdlib finder signature)
        root = fullname.split(".", 1)[0]
        if root in self.blocked:
            raise BlockedHostImportError(
                f"import of host package '{fullname}' is blocked in a community-module worker"
            )
        return None


def install_import_blocker() -> None:
    """Install the host-import blocker at the front of sys.meta_path (idempotent)."""
    if not any(isinstance(f, HostImportBlocker) for f in sys.meta_path):
        sys.meta_path.insert(0, HostImportBlocker())
