"""Community module installer.

Downloads a module tarball from GitHub, validates the manifest, extracts to
/data/modules/{id}/, and records the install in /data/modules-lock.json.

Security posture (per research report):
  - No sandboxing; modules run in-process with full Python access
  - Tarball pinned to a specific tag or SHA; we record the resolved commit
  - manifest.secrets_required drives which keys the user is prompted for;
    we do NOT auto-inject the full .env into community module env
  - Uninstall removes the directory; secrets remain in the store (user
    decides whether to delete them separately)
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx

from backend.module_registry import (
    COMMUNITY_MODULES_DIR,
    ModuleManifest,
    is_compatible,
    load_manifest,
)

logger = logging.getLogger(__name__)

LOCK_FILE = Path("data/modules-lock.json")
GITHUB_TARBALL = "https://api.github.com/repos/{owner}/{repo}/tarball/{ref}"


def _load_lock() -> dict[str, Any]:
    if LOCK_FILE.exists():
        try:
            return json.loads(LOCK_FILE.read_text())
        except Exception:
            logger.warning("modules-lock.json unreadable; treating as empty")
    return {}


def _save_lock(data: dict[str, Any]) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps(data, indent=2))


def _parse_repo(repo: str) -> tuple[str, str]:
    """Parse 'owner/repo' or 'https://github.com/owner/repo' → (owner, repo)."""
    s = repo.strip().rstrip("/")
    if s.startswith("https://github.com/"):
        s = s[len("https://github.com/"):]
    if s.endswith(".git"):
        s = s[:-4]
    parts = s.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid repo spec: {repo!r}. Expected 'owner/repo' or GitHub URL.")
    return parts[0], parts[1]


async def _download_tarball(owner: str, repo: str, ref: str) -> tuple[bytes, str]:
    """Download a tarball from GitHub. Returns (bytes, resolved_sha).

    GitHub's tarball endpoint follows a redirect to codeload.github.com.
    The Content-Disposition header includes the resolved commit SHA.
    """
    url = GITHUB_TARBALL.format(owner=owner, repo=repo, ref=ref)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub tarball download failed: HTTP {r.status_code} — {r.text[:200]}")
        # The download URL includes the full SHA in its path. Example:
        # codeload.github.com/owner/repo/tar.gz/abc123def...
        final_url = str(r.url)
        sha = final_url.rsplit("/", 1)[-1]
        if len(sha) < 7:  # didn't get a SHA — fall back to content hash
            sha = hashlib.sha256(r.content).hexdigest()[:16]
        return r.content, sha


def _extract_tarball(data: bytes, dest: Path) -> Path:
    """Extract tarball to dest. GitHub tarballs have a single top-level dir
    named `{owner}-{repo}-{sha}` — we extract and then rename that to `dest`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)

    staging = dest.parent / f".{dest.name}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    staging_resolved = staging.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        # Validate every member before extracting anything.
        for member in tf.getmembers():
            # Reject symlinks/hardlinks: their link target can point outside the
            # staging dir even when the member name itself looks safe, so
            # extractall would write/escape through the link. v1 posture — a
            # GitHub `git archive` tarball of a repo that COMMITS a symlink
            # becomes uninstallable; acceptable (most modules have none). The
            # richer "allow links whose resolved target stays inside staging" is
            # deferred.
            if member.issym() or member.islnk():
                raise RuntimeError(f"Unsafe link in tarball: {member.name}")
            # Only regular files and directories; reject devices, fifos, etc.
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"Unsupported tar member type: {member.name}")
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise RuntimeError(f"Unsafe path in tarball: {member.name}")
            # Belt-and-suspenders: the resolved destination must stay under staging.
            target = (staging / member.name).resolve()
            if not target.is_relative_to(staging_resolved):
                raise RuntimeError(f"Tar member escapes staging dir: {member.name}")
        # All members validated (no links, no specials, no traversal). On 3.12+
        # add the stdlib 'data' filter as a second layer; do not rely on it alone.
        extract_kwargs: dict[str, Any] = {}
        if sys.version_info >= (3, 12):
            extract_kwargs["filter"] = "data"
        tf.extractall(staging, **extract_kwargs)

    # Find the single top-level extracted dir and promote its contents.
    top_dirs = [p for p in staging.iterdir() if p.is_dir()]
    if len(top_dirs) != 1:
        shutil.rmtree(staging)
        raise RuntimeError(f"Tarball did not contain a single top-level dir (found {len(top_dirs)})")

    shutil.move(str(top_dirs[0]), str(dest))
    shutil.rmtree(staging)
    return dest


async def install(
    repo: str,
    ref: str = "main",
    expected_id: str | None = None,
) -> dict[str, Any]:
    """Install a community module from GitHub.

    Args:
      repo: 'owner/repo' or 'https://github.com/owner/repo'
      ref: tag, branch, or commit SHA (default: 'main')
      expected_id: if set, validates the downloaded manifest.id matches

    Returns a dict describing the installed module. Raises on failure.
    """
    owner, repo_name = _parse_repo(repo)
    data, resolved_sha = await _download_tarball(owner, repo_name, ref)

    # Extract to a temp location first so we can read the manifest before
    # committing to a final install path.
    tmp_dir = COMMUNITY_MODULES_DIR / f".install-{int(time.time())}"
    _extract_tarball(data, tmp_dir)

    try:
        manifest = load_manifest(tmp_dir)
        if not manifest:
            raise RuntimeError("Tarball has no valid manifest.json at its root")

        if expected_id and manifest.id != expected_id:
            raise RuntimeError(f"Manifest id {manifest.id!r} does not match expected {expected_id!r}")

        if not is_compatible(manifest.min_app_version):
            raise RuntimeError(
                f"Module requires app version >= {manifest.min_app_version}"
            )

        final_dir = COMMUNITY_MODULES_DIR / manifest.id
        if final_dir.exists():
            shutil.rmtree(final_dir)
        shutil.move(str(tmp_dir), str(final_dir))

        lock = _load_lock()
        lock[manifest.id] = {
            "repo": f"{owner}/{repo_name}",
            "pinned_ref": ref,
            "installed_sha": resolved_sha,
            "installed_at": int(time.time()),
            "version": manifest.version,
        }
        _save_lock(lock)

        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "installed_sha": resolved_sha,
            "path": str(final_dir),
            "secrets_required": [s.model_dump() for s in manifest.secrets_required],
            "restart_required": True,
        }
    except Exception:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def uninstall(module_id: str) -> dict[str, Any]:
    """Remove a community module directory and lock entry."""
    target = COMMUNITY_MODULES_DIR / module_id
    if not target.exists():
        raise RuntimeError(f"Module {module_id!r} not installed")

    shutil.rmtree(target)

    lock = _load_lock()
    removed = lock.pop(module_id, None)
    _save_lock(lock)

    return {
        "id": module_id,
        "removed": removed is not None,
        "restart_required": True,
    }


def list_installed() -> dict[str, ModuleManifest]:
    """Return {id: manifest} for every community module currently installed."""
    result: dict[str, ModuleManifest] = {}
    if not COMMUNITY_MODULES_DIR.exists():
        return result
    for child in COMMUNITY_MODULES_DIR.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest = load_manifest(child)
        if manifest:
            result[manifest.id] = manifest
    return result


def get_lock() -> dict[str, Any]:
    """Expose lock file contents for the UI."""
    return _load_lock()
