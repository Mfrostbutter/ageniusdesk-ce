"""Filesystem-facing helpers for the notes vault.

All note paths are relative (forward-slash) paths inside VAULT_DIR. This
module validates + resolves them, preventing escapes via `..` or absolute
paths. I/O is sync — these files are small and local; wrapping in a thread
pool would add more complexity than it saves.

VAULT_DIR is a module-level variable; never hardcode it elsewhere. Phase 2
will extract these functions into a `MemoryBackend` Protocol — keep the
signatures stable.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from backend.modules.notes import index as _index

logger = logging.getLogger(__name__)

VAULT_DIR: Path = Path("data/workspace")
LEGACY_VAULT_DIR: Path = Path("data/notes")
ARCHIVE_DIRNAME = ".archive"

# sha256 of every PRIOR pristine README seed we have shipped. A README whose
# content still matches one of these has never been touched by the operator, so
# it is safe to refresh to the current seed on boot (keeps existing installs
# current without clobbering edits). The current seed is intentionally NOT in
# this set, so a refreshed file never re-triggers.
_PRIOR_README_SEED_HASHES = frozenset({
    "1f1d4754c5c6f1d6694ac35cbb6fc937fcb4cf46de0eea421d529437e706b67e",  # original
    "0093127aef854f5bfe9a3245759b32b0ee3bda005fa8aee6944e76754c6420f9",  # + skills/ folder row
})


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _refresh_pristine_readme(readme: Path) -> None:
    """Upgrade a pristine (unedited) harness README to the current seed.

    No-op if the file was edited (hash not a known prior seed) or already current.
    """
    try:
        current = readme.read_text(encoding="utf-8")
    except OSError:
        return
    if _sha256(current) in _PRIOR_README_SEED_HASHES and current != _SEED_README:
        readme.write_text(_SEED_README, encoding="utf-8")
        logger.info("workspace: refreshed pristine README to the current seed")


@dataclass
class VaultPath:
    rel: str          # relative path with forward slashes, e.g. "user/foo.md"
    abs: Path         # absolute resolved path under VAULT_DIR
    is_archive: bool  # true if the rel path is inside ARCHIVE_DIRNAME


def _migrate_legacy_vault() -> None:
    """One-time rename of the pre-harness `data/notes` vault to `data/workspace`.

    Idempotent: only fires when the new root is absent and the legacy root
    exists. The on-disk index stores paths relative to the vault root, so the
    rename is transparent to search/backlinks; the index DB lives outside the
    vault dir (see INDEX_DB) and is unaffected.
    """
    if VAULT_DIR.exists() or not LEGACY_VAULT_DIR.exists():
        return
    try:
        VAULT_DIR.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_VAULT_DIR.rename(VAULT_DIR)
        logger.info("workspace: migrated legacy vault %s -> %s", LEGACY_VAULT_DIR, VAULT_DIR)
    except OSError as e:
        logger.warning("workspace: legacy vault migration failed (%s); using fresh root", e)


def ensure_vault() -> None:
    """Create the workspace (harness) scaffolding on first run. Idempotent."""
    _migrate_legacy_vault()
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    (VAULT_DIR / ARCHIVE_DIRNAME).mkdir(exist_ok=True)

    # Point the index DB at a fixed location next to the vault. Does not
    # import at module-load in index.py to keep that module pure.
    _index.INDEX_DB = VAULT_DIR.parent / "notes.db"

    # Seed a minimal README and one example note the first time around so
    # the UI isn't eerily empty. Everything else is operator-authored.
    # AGENTS.md (the harness-wide agent instructions) is seeded separately by
    # the baseline loader so it carries the constitution frontmatter.
    readme = VAULT_DIR / "README.md"
    if not readme.exists():
        readme.write_text(_SEED_README, encoding="utf-8")
    else:
        _refresh_pristine_readme(readme)
    # Harness folder layout: human notes (user/), agent scratch (agent/),
    # canonical facts (shared/), session logs (sessions/), plus the harness
    # working areas docs/ workflows/ research/ that agents and add-ins write to.
    for sub in ("user", "agent", "shared", "sessions", "docs", "workflows", "research"):
        (VAULT_DIR / sub).mkdir(exist_ok=True)
        gitkeep = VAULT_DIR / sub / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()


def resolve(rel: str) -> VaultPath:
    """Validate and normalize a note path. Raises ValueError on escape."""
    if not rel or rel in (".", "/"):
        raise ValueError("empty path")
    rel = rel.lstrip("/")
    # Forbid backslashes (Windows-style), null bytes, and `..` traversal.
    if "\x00" in rel or "\\" in rel:
        raise ValueError("invalid characters in path")
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise ValueError(f"invalid path segment: {part!r}")
    if not rel.endswith(".md"):
        rel = rel + ".md"
    abs_path = (VAULT_DIR / rel).resolve()
    try:
        abs_path.relative_to(VAULT_DIR.resolve())
    except ValueError as e:
        raise ValueError("path escapes vault") from e
    return VaultPath(
        rel=rel,
        abs=abs_path,
        is_archive=rel.split("/", 1)[0] == ARCHIVE_DIRNAME,
    )


def list_tree() -> dict:
    """Return the vault's folder tree as a nested dict suitable for the UI."""
    root: dict = {"name": "", "type": "dir", "children": []}

    def attach(parts: list[str], node: dict, is_file: bool) -> None:
        if not parts:
            return
        head, *tail = parts
        for child in node["children"]:
            if child["name"] == head:
                if tail:
                    attach(tail, child, is_file)
                return
        if tail:
            new = {"name": head, "type": "dir", "children": []}
            node["children"].append(new)
            attach(tail, new, is_file)
        elif is_file:
            node["children"].append({"name": head, "type": "file"})
        else:
            # Terminal directory — always include an empty children list so
            # later paths that add entries underneath don't KeyError.
            node["children"].append({"name": head, "type": "dir", "children": []})

    for path in sorted(VAULT_DIR.rglob("*")):
        rel = path.relative_to(VAULT_DIR).as_posix()
        if any(p.startswith(".") for p in rel.split("/")):
            continue  # skip hidden (incl. .archive)
        parts = rel.split("/")
        attach(parts, root, path.is_file())
    return root


def read(rel: str) -> str:
    vp = resolve(rel)
    if vp.is_archive:
        raise FileNotFoundError("note is archived")
    if not vp.abs.exists():
        raise FileNotFoundError(vp.rel)
    return vp.abs.read_text()


async def write(rel: str, content: str) -> dict:
    """Create-or-update. Creates parent dirs. Reindexes. Returns metadata."""
    vp = resolve(rel)
    vp.abs.parent.mkdir(parents=True, exist_ok=True)
    vp.abs.write_text(content)
    stat = vp.abs.stat()
    note = await _index.upsert_note(vp.rel, content, stat.st_mtime, stat.st_size)
    return {
        "path": vp.rel,
        "title": note.title,
        "tags": note.tags,
        "links": list(dict.fromkeys(note.links)),
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }


async def append(rel: str, content: str) -> dict:
    """Append content to an existing note (or create if missing). Adds a
    newline separator if the existing content doesn't already end in one.
    Primary use case: agent scratchpads that accumulate over time."""
    vp = resolve(rel)
    existing = vp.abs.read_text() if vp.abs.exists() else ""
    joiner = "" if existing.endswith("\n") or not existing else "\n"
    return await write(rel, existing + joiner + content)


async def archive(rel: str) -> dict:
    """Move a note to .archive/, preserving subfolder structure. Never
    deletes. Reindex removes it from search."""
    vp = resolve(rel)
    if vp.is_archive:
        return {"path": vp.rel, "status": "already-archived"}
    if not vp.abs.exists():
        raise FileNotFoundError(vp.rel)
    ts = time.strftime("%Y%m%d-%H%M%S")
    archive_rel = f"{ARCHIVE_DIRNAME}/{ts}/{vp.rel}"
    archive_abs = VAULT_DIR / archive_rel
    archive_abs.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(vp.abs), str(archive_abs))
    await _index.remove_note(vp.rel)
    return {"path": vp.rel, "archived_to": archive_rel}


_SEED_README = """---
title: Your workspace (the harness)
tags: [meta]
---

# This is the harness

Most AI tools start cold every session. This one does not.

Everything in this folder is plain markdown and JSON on your disk,
inside AgeniusDesk's container volume. It is the workspace every agent
here works within. The in-app AI assistant, and any MCP client you
point at this dashboard, reads and writes these files. When an agent
saves a workflow, a runbook, or a note, it lands here and the next
session can read it back. The context compounds.

You own the files. Nothing is hosted. Sync the folder to Obsidian
(iCloud, Syncthing, whatever) and edit on your phone if you want.
Both the dashboard and Obsidian write to the same files. The
dashboard reindexes on save; if you edit externally, hit **Reindex**
in the Harness.

## The folders

| Folder | What goes in it | Who writes it |
|---|---|---|
| `user/` | Your own notes: clients, runbooks, ideas | You |
| `agent/` | Scratchpads the AI writes for itself | The agents |
| `docs/` | Documentation agents and you write | Both |
| `workflows/` | Saved n8n workflow JSON | Both |
| `research/` | Output from add-ins (research modules, etc.) | Add-ins |
| `shared/` | Canonical facts (company info, conventions) | Both |
| `sessions/` | Per-session logs | The agents |
| `skills/` | Curated n8n skills the assistant loads on demand | Seeded (yours to edit) |

`AGENTS.md` at the root holds the instructions that steer every agent.
Edit it here, or from the Harness Instructions panel.

## n8n skills + tools

`skills/` holds a curated library of focused n8n skills (one folder each:
a `SKILL.md` plus reference docs). The in-app assistant and Code Lab read
them on demand to build and debug workflows correctly. Start at
`skills/README.md` (the router). They are seeded once and yours to edit or
extend. Paired with the built-in n8n-mcp server (Settings -> MCP Servers ->
n8n Intelligence), the assistant gets live node knowledge and workflow
validation. Skills + n8n-mcp by czlonkowski (MIT).

## Syntax

Obsidian-compatible. The cheat sheet:

- `[[Other Note]]`: wikilink to any note by basename
- `[[folder/Note|display text]]`: link with alias
- `[[Note#heading]]`: link to a specific heading
- `#tag-name` inline, or `tags: [ops, on-call]` in frontmatter
- `---` fences at the top for YAML frontmatter

The **Backlinks** panel shows which notes link to the current note.
Search is full-text over title, body, and tags with BM25 ranking.

## The move

Write one note about something you actually do: a client runbook, an
incident playbook, a workflow you keep tweaking. Then ask the AI
assistant about it. It can read and update these files directly. That
is the loop.
"""
