"""Community module installer.

Two-phase install: `inspect` downloads + scans a module and returns a report
WITHOUT registering it; `install` re-downloads, verifies the resolved commit
still matches what was inspected, enforces consent proportional to the scan
findings, then extracts to /data/modules/{id}/ and records the install in both
/data/modules-lock.json and the `module_installs` audit table.

Security posture (per spec 2026-06-26):
  - No sandboxing; modules run in-process with full Python access. The
    scan/consent flow is defense-in-depth and an informed-consent record, NOT
    containment. Out-of-process isolation is the deferred real boundary.
  - Tarball pinned to a specific tag or SHA; we record the resolved commit and
    reject an install whose resolved sha drifted since inspection (swapped tag).
  - A static AST scan (scanner.py) reconciles declared capabilities against
    detected ones; the operator consents before registration, with friction
    proportional to severity (CRITICAL -> type the id; HIGH -> acknowledge).
  - manifest.secrets_required drives which keys the user is prompted for; we do
    NOT auto-inject the full .env into community module env.
  - Uninstall removes the directory; secrets remain in the store (user decides
    whether to delete them separately).
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
    is_valid_module_id,
    load_manifest,
)

from .scanner import ScanReport, scan_module, scan_summary

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


def _safe_community_dir(module_id: str) -> Path:
    """Resolve data/modules/{module_id}, rejecting an unsafe id or any resolved
    path that escapes COMMUNITY_MODULES_DIR.

    Call before every destructive op (rmtree/move) so a crafted id (e.g. '..',
    which would resolve to data/) can never delete or move a path outside the
    community modules tree. The manifest validator already blocks bad ids on the
    install path; this is the choke point for uninstall, which takes the id
    straight from the request URL.
    """
    if not is_valid_module_id(module_id):
        raise RuntimeError(f"Invalid module id: {module_id!r}")
    base = COMMUNITY_MODULES_DIR.resolve()
    target = (COMMUNITY_MODULES_DIR / module_id).resolve()
    if base not in target.parents:
        raise RuntimeError(f"Module path escapes the modules directory: {module_id!r}")
    return target


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


async def _download_and_extract(repo: str, ref: str) -> tuple[Path, str, str, str]:
    """Download a repo tarball and extract it to a staging dir.

    Returns (staging_dir, resolved_sha, owner, repo_name). The caller owns the
    staging dir lifecycle (discard on inspect, promote on install).
    """
    owner, repo_name = _parse_repo(repo)
    data, resolved_sha = await _download_tarball(owner, repo_name, ref)
    tmp_dir = COMMUNITY_MODULES_DIR / f".stage-{resolved_sha[:12]}-{int(time.time())}"
    _extract_tarball(data, tmp_dir)
    return tmp_dir, resolved_sha, owner, repo_name


def _module_root(staging: Path, path: str) -> Path:
    """Resolve the module directory inside a staged repo, traversal-safe.

    `path` is the relative subdir of the module within the repo (blank = repo
    root, the single-module case). Must stay inside the staging dir and contain
    a manifest.json.
    """
    path = (path or "").strip().strip("/")
    if not path:
        return staging
    if ".." in Path(path).parts or Path(path).is_absolute():
        raise RuntimeError(f"Unsafe module path: {path!r}")
    base = staging.resolve()
    target = (staging / path).resolve()
    if target != base and base not in target.parents:
        raise RuntimeError(f"Module path escapes the repo: {path!r}")
    if not target.is_dir():
        raise RuntimeError(f"Module path not found in repo: {path!r}")
    return target


def _find_modules(staging: Path) -> list[tuple[str, ModuleManifest]]:
    """Find every installable module in a staged repo.

    A root manifest.json means a single-module repo (path ""). Otherwise scan
    one and two levels deep (`<id>/manifest.json` and `modules/<id>/manifest.json`,
    the monorepo convention), returning (relative_path, manifest) for each valid
    manifest, sorted by path.
    """
    root = load_manifest(staging)
    if root:
        return [("", root)]
    dirs: set[Path] = set()
    for pattern in ("*/manifest.json", "*/*/manifest.json"):
        for mf_path in staging.glob(pattern):
            dirs.add(mf_path.parent)
    found: list[tuple[str, ModuleManifest]] = []
    for d in sorted(dirs, key=lambda p: p.relative_to(staging).as_posix()):
        manifest = load_manifest(d)
        if manifest:
            found.append((d.relative_to(staging).as_posix(), manifest))
    return found


async def discover(repo: str, ref: str = "main") -> dict[str, Any]:
    """List every installable module in a repo without registering anything.

    Returns the modules found (id/name/version/description/path/compatibility) so
    the operator can pick one to inspect. Supports both a single-module repo (a
    root manifest, path "") and a monorepo (`modules/<id>/manifest.json`).
    """
    tmp_dir, resolved_sha, owner, repo_name = await _download_and_extract(repo, ref)
    try:
        modules = [
            {
                "path": path,
                "id": mf.id,
                "name": mf.name,
                "version": mf.version,
                "description": mf.description,
                "min_app_version": mf.min_app_version,
                "compatible": is_compatible(mf.min_app_version),
                "has_capabilities": mf.capabilities is not None,
            }
            for path, mf in _find_modules(tmp_dir)
        ]
        return {
            "repo": f"{owner}/{repo_name}",
            "ref": ref,
            "resolved_sha": resolved_sha,
            "modules": modules,
        }
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _consent_satisfied(report: ScanReport, manifest: ModuleManifest, consent: dict[str, Any]) -> tuple[bool, str]:
    """Enforce consent friction proportional to scan severity, server-side.

    We never trust client-submitted findings: the scan is re-run on the freshly
    downloaded code (same sha => same code) and the gate is checked here.
    CRITICAL requires typing the module id; HIGH requires an acknowledgement.
    """
    if report.has("CRITICAL") and (consent.get("typed_id") or "") != manifest.id:
        return False, (
            f"This module has CRITICAL findings. Type the module id "
            f"('{manifest.id}') to confirm you understand the risk."
        )
    if report.has("HIGH") and not consent.get("acknowledged"):
        return False, "This module has HIGH findings. Acknowledge the elevated/undeclared capabilities to proceed."
    return True, ""


async def _record_install(
    manifest: ModuleManifest, repo: str, ref: str, resolved_sha: str, report: ScanReport, approved_by: str
) -> None:
    """Append an audit row. Best-effort: an already-completed install must not
    fail because the audit write failed, so we log instead of raising."""
    try:
        from backend.database import get_db

        caps = manifest.capabilities.model_dump() if manifest.capabilities else None
        db = await get_db()
        await db.execute(
            """INSERT INTO module_installs
               (module_id, repo, ref, resolved_sha, capabilities_json, scan_summary, scan_max_severity, approved_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                manifest.id,
                repo,
                ref,
                resolved_sha,
                json.dumps(caps),
                scan_summary(report),
                report.max_severity or "none",
                approved_by,
            ),
        )
        await db.commit()
    except Exception as e:  # pragma: no cover - audit must never break install
        logger.warning("Failed to record install audit for %s: %s", manifest.id, e)


async def inspect(repo: str, ref: str = "main", path: str = "") -> dict[str, Any]:
    """Dry-run: download, scan, and report WITHOUT registering the module.

    `path` selects a module subdir for a monorepo (blank = repo root). The
    staging dir is discarded before returning, so nothing persists until the
    operator confirms install with the returned resolved_sha.
    """
    tmp_dir, resolved_sha, owner, repo_name = await _download_and_extract(repo, ref)
    try:
        module_root = _module_root(tmp_dir, path)
        manifest = load_manifest(module_root)
        if not manifest:
            where = f"at '{path}'" if path else "at its root"
            raise RuntimeError(f"No valid manifest.json {where}")
        report = scan_module(module_root, manifest)
        return {
            "manifest": manifest.model_dump(),
            "capabilities": manifest.capabilities.model_dump() if manifest.capabilities else None,
            "scan_report": report.model_dump(),
            "resolved_sha": resolved_sha,
            "repo": f"{owner}/{repo_name}",
            "ref": ref,
            "path": path,
            "compatible": is_compatible(manifest.min_app_version),
            "min_app_version": manifest.min_app_version,
        }
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def install(
    repo: str,
    ref: str = "main",
    expected_sha: str | None = None,
    consent: dict[str, Any] | None = None,
    approved_by: str = "",
    expected_id: str | None = None,
    path: str = "",
) -> dict[str, Any]:
    """Install a community module from GitHub after inspection + consent.

    Args:
      repo: 'owner/repo' or 'https://github.com/owner/repo'
      ref: tag, branch, or commit SHA (default: 'main')
      expected_sha: the resolved sha returned by inspect; install is rejected if
        the ref now resolves to a different commit (swapped-tag guard)
      consent: {'acknowledged': bool, 'typed_id': str|None} from the operator
      approved_by: resolved identity of the operator (for the audit record)
      expected_id: if set, validates the downloaded manifest.id matches
      path: module subdir for a monorepo (blank = repo root)

    Returns a dict describing the installed module. Raises on failure.
    """
    consent = consent or {}
    tmp_dir, resolved_sha, owner, repo_name = await _download_and_extract(repo, ref)

    try:
        if expected_sha and resolved_sha != expected_sha:
            raise RuntimeError(
                f"The ref resolved to a different commit since inspection "
                f"({expected_sha[:12]} -> {resolved_sha[:12]}). Re-inspect before installing."
            )

        module_root = _module_root(tmp_dir, path)
        manifest = load_manifest(module_root)
        if not manifest:
            where = f"at '{path}'" if path else "at its root"
            raise RuntimeError(f"No valid manifest.json {where}")

        if expected_id and manifest.id != expected_id:
            raise RuntimeError(f"Manifest id {manifest.id!r} does not match expected {expected_id!r}")

        if not is_compatible(manifest.min_app_version):
            raise RuntimeError(f"Module requires app version >= {manifest.min_app_version}")

        report = scan_module(module_root, manifest)
        ok, reason = _consent_satisfied(report, manifest, consent)
        if not ok:
            raise RuntimeError(reason)

        final_dir = _safe_community_dir(manifest.id)
        if final_dir.exists():
            shutil.rmtree(final_dir)
        # Promote the module dir out of staging. For path="" this moves the whole
        # repo (staging itself); for a monorepo subdir it lifts just that subtree
        # and the now-stale staging dir is removed in the finally below.
        shutil.move(str(module_root), str(final_dir))

        lock = _load_lock()
        lock[manifest.id] = {
            "repo": f"{owner}/{repo_name}",
            "pinned_ref": ref,
            "path": path,
            "installed_sha": resolved_sha,
            "installed_at": int(time.time()),
            "version": manifest.version,
            "approved_by": approved_by,
            "scan_max_severity": report.max_severity or "none",
        }
        _save_lock(lock)

        await _record_install(manifest, f"{owner}/{repo_name}", ref, resolved_sha, report, approved_by)

        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "installed_sha": resolved_sha,
            "path": str(final_dir),
            "secrets_required": [s.model_dump() for s in manifest.secrets_required],
            "scan_max_severity": report.max_severity or "none",
            "scan_summary": scan_summary(report),
            "restart_required": True,
        }
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def uninstall(module_id: str) -> dict[str, Any]:
    """Remove a community module directory and lock entry."""
    target = _safe_community_dir(module_id)
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
