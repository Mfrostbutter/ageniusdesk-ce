"""Seed the harness skill library (``data/workspace/skills/``) on first run.

Vendors the curated n8n skill set (czlonkowski/n8n-skills, MIT) from
``backend/skills_seed/`` into the operator's vault once, so the in-app assistant
and Code Lab have focused n8n guidance out of the box.

Idempotent and non-destructive: it seeds only when the ``skills/`` folder is
absent, so operator edits and deletions are never clobbered (same first-run-only
contract as the constitution seed). Opt out with ``AGD_SEED_SKILLS=false``.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from backend.modules.notes import index as _index
from backend.modules.notes import storage as _ws

logger = logging.getLogger(__name__)

SKILLS_DIRNAME = "skills"
# backend/modules/notes/skills_seed.py -> parents[2] == backend/
SEED_SRC: Path = Path(__file__).resolve().parents[2] / "skills_seed"


def _enabled() -> bool:
    return os.environ.get("AGD_SEED_SKILLS", "true").strip().lower() not in {"false", "0", "no", "off"}


async def ensure_skills() -> None:
    """Copy the bundled skill library into the vault the first time only.

    Seeds when ``<vault>/skills/`` does not exist; once present (even if the
    operator emptied or edited it) it is left untouched. Best-effort: never
    raises, so a seeding failure cannot block boot.
    """
    if not _enabled():
        return
    _ws.ensure_vault()  # guarantees VAULT_DIR exists + the index DB path is set
    target = _ws.VAULT_DIR / SKILLS_DIRNAME
    if target.exists():
        return
    if not SEED_SRC.is_dir():
        logger.warning("skills: seed source missing at %s; skipping", SEED_SRC)
        return
    try:
        shutil.copytree(SEED_SRC, target)
    except Exception as e:  # noqa: BLE001 - seeding must never block boot
        logger.warning("skills: seed copy failed: %s", e)
        return
    count = sum(1 for _ in target.rglob("*.md"))
    logger.info("skills: seeded %d skill docs into %s", count, target)
    # Make the seeded skills searchable out of the box. One-time (only on the
    # first seed); non-fatal if it fails — the files are on disk regardless and
    # the operator can Reindex from the Harness.
    try:
        await _index.rebuild_index(_ws.VAULT_DIR)
    except Exception as e:  # noqa: BLE001
        logger.debug("skills: post-seed reindex failed (non-fatal): %s", e)
