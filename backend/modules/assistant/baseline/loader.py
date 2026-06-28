"""C3 constitution loader.

Single read/write path for the operator-authored constitution document.
All functions are fail-soft: errors are logged and callers receive safe
fallbacks rather than exceptions (except write(), which raises HTTP errors
for concurrency conflicts and size violations — those are meaningful to the
caller and must not be swallowed).

File layout (harness):
    data/workspace/AGENTS.md           <- default / single-tenant

The constitution is now a real file at the workspace (harness) root, editable
like any other workspace file, instead of a hidden data/baseline/baseline.md.
On boot it is migrated from the legacy location if present.

C1 hook: the ``tenant_id`` parameter exists from day one.  When C1 lands,
    _path_for("acme") -> a per-tenant AGENTS file, falling back to the default.
    Until then every call with any tenant_id resolves to AGENTS.md.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
from pathlib import Path

from fastapi import HTTPException

from backend.modules.assistant.baseline.merge import apply_overrides
from backend.modules.assistant.baseline.seed import render_seed
from backend.modules.notes import storage as _ws
from backend.modules.notes.parser import parse_frontmatter

logger = logging.getLogger(__name__)

AGENTS_FILENAME = "AGENTS.md"
LEGACY_BASELINE = Path("data/baseline/baseline.md")
_MAX_BYTES = 64 * 1024  # 64 KiB

# sha256 of the BODY (frontmatter stripped) of every PRIOR pristine constitution
# seed. An AGENTS.md still at version 1 (never saved via the editor, which bumps
# the version) AND whose body matches one of these has not been customized, so we
# refresh it to the current seed on boot. Both conditions must hold, so an
# operator edit (higher version, or a changed body) is never overwritten.
_PRIOR_BASELINE_BODY_HASHES = frozenset({
    "560ac8deb1d10b4ce694193717f2177938530c1d88de36cbf117cdbd8b36da6f",  # original
    "98fcf34e76d0fce404b699912d8ce14ecda9edfcc7a1c3a610313dd4d51fd8aa",  # + skills pointer
})


def _constitution_enabled() -> bool:
    return os.environ.get("AGD_CONSTITUTION_ENABLED", "true").lower() not in {"false", "0", "no"}


def _path_for(tenant_id: str) -> Path:
    """Return the filesystem path for the given tenant.

    v1: always returns <workspace>/AGENTS.md regardless of tenant_id.
    C1 will add per-tenant resolution here.
    """
    return _ws.VAULT_DIR / AGENTS_FILENAME


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refresh_pristine_baseline(path: Path) -> None:
    """Upgrade a pristine (unedited) AGENTS.md to the current seed.

    Pristine = still version 1 (the editor bumps version on save) AND the body
    matches a known prior seed. No-op otherwise, so operator customizations —
    whether saved via the editor or edited directly — are never overwritten.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
    except Exception as e:  # noqa: BLE001 - best effort; never block boot
        logger.debug("constitution: refresh parse skipped: %s", e)
        return
    try:
        version = int(fm.get("version", 1))
    except (TypeError, ValueError):
        version = 1
    if version != 1:
        return
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if body_hash not in _PRIOR_BASELINE_BODY_HASHES:
        return
    seed = render_seed()
    _, seed_body = parse_frontmatter(seed)
    if hashlib.sha256(seed_body.encode("utf-8")).hexdigest() == body_hash:
        return  # already current
    try:
        path.write_text(seed, encoding="utf-8")
        logger.info("constitution: refreshed pristine AGENTS.md to the current seed")
    except OSError as e:
        logger.warning("constitution: refresh write failed: %s", e)


async def ensure_baseline() -> None:
    """Ensure the workspace AGENTS.md exists. Idempotent; safe per boot.

    Migrates the legacy data/baseline/baseline.md into the workspace root the
    first time, otherwise writes the seed.
    """
    _ws.ensure_vault()
    path = _path_for("default")
    if path.exists():
        _refresh_pristine_baseline(path)
        return
    if LEGACY_BASELINE.exists():
        try:
            path.write_text(LEGACY_BASELINE.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info("constitution: migrated %s -> %s", LEGACY_BASELINE, path)
            return
        except OSError as e:
            logger.warning("constitution: legacy migration failed (%s); writing seed", e)
    logger.info("constitution: AGENTS.md not found; writing seed")
    path.write_text(render_seed(), encoding="utf-8")


async def read(tenant_id: str = "default") -> dict:
    """Return a BaselineResponse-shaped dict for the given tenant.

    Calls ensure_baseline() first if the file is missing (e.g. on first boot
    before the lifespan hook ran, or if a file was deleted).
    """
    path = _path_for(tenant_id)
    if not path.exists():
        await ensure_baseline()

    raw = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(raw)

    version = int(fm.get("version", 1))
    updated = str(fm.get("updated", ""))
    overrideable_sections: list[str] = fm.get("overrideable_sections", [])
    if not isinstance(overrideable_sections, list):
        overrideable_sections = []

    return {
        "version": version,
        "updated": updated,
        "overrideable_sections": overrideable_sections,
        "content": body,
        "size": len(raw.encode("utf-8")),
    }


async def write(
    expected_version: int,
    content: str,
    overrideable_sections: list[str],
    tenant_id: str = "default",
) -> dict:
    """Persist a new constitution body.

    Raises:
        HTTPException 409 if the on-disk version does not match expected_version.
        HTTPException 413 if the encoded body exceeds 64 KiB.
    """
    # Size check on the body the client submitted (the frontmatter we add is
    # small; checking the body alone is a sufficient proxy for total size).
    if len(content.encode("utf-8")) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="constitution body too large (max 64 KiB)")

    path = _path_for(tenant_id)

    # Read current version for optimistic concurrency check.
    current_version = 1
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(raw)
        current_version = int(fm.get("version", 1))

    if current_version != expected_version:
        raise HTTPException(
            status_code=409,
            detail="version_conflict",
            headers={"X-Server-Version": str(current_version)},
        )

    new_version = current_version + 1
    updated = _now_utc()

    # Build the sections list for the YAML block.
    if overrideable_sections:
        sections_yaml = "\n".join(f"  - {s}" for s in overrideable_sections)
        sections_block = f"overrideable_sections:\n{sections_yaml}"
    else:
        sections_block = "overrideable_sections: []"

    new_raw = (
        f"---\n"
        f"version: {new_version}\n"
        f"updated: {updated}\n"
        f"{sections_block}\n"
        f"---\n"
        f"{content}"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_raw, encoding="utf-8")

    return {
        "version": new_version,
        "updated": updated,
        "overrideable_sections": overrideable_sections,
        "content": content,
        "size": len(new_raw.encode("utf-8")),
    }


async def render(
    tenant_id: str = "default",
    per_agent_overrides: dict | None = None,
) -> str:
    """Return the constitution body (no frontmatter) ready for injection.

    The body has any per-agent overrides merged in via merge.apply_overrides().
    Returns "" on any error or when AGD_CONSTITUTION_ENABLED=false.
    Never raises -- callers (chat()) depend on fail-soft behaviour.
    """
    try:
        if not _constitution_enabled():
            return ""

        data = await read(tenant_id)
        body: str = data["content"]
        overrideable: list[str] = data["overrideable_sections"]

        per_agent_text: str | None = None
        if per_agent_overrides:
            per_agent_text = per_agent_overrides.get("text") or per_agent_overrides.get("content")

        return apply_overrides(body, overrideable, per_agent_text)
    except Exception as exc:
        logger.debug("constitution render failed (returning empty): %s", exc)
        return ""
