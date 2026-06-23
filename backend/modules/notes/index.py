"""SQLite FTS5 index for the notes vault.

Lives at data/notes.db alongside the main dashboard.db so the notes data is
cleanly separable. The vault itself (markdown files) is the source of truth;
the index is derived and can be rebuilt from scratch with `rebuild_index()`.

Phase 2 will swap this for alternate backends (Qdrant, sqlite-vec); keep
the public function signatures stable and don't leak SQLite-specific shapes
into the return values.

Schema:
  notes_fts (FTS5)     — full-text over title + body + tags; path is the key
  notes_meta           — per-note metadata (mtime, size, tags JSON, links JSON)
  note_links           — (src_path, target_basename) one row per wikilink
                         occurrence; target is lower-cased basename (no .md)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from backend.modules.notes.parser import Note, parse_note

logger = logging.getLogger(__name__)

# Re-assigned by storage.py at import — avoids a circular import.
INDEX_DB: Path = Path("data/notes.db")


async def _connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(INDEX_DB))
    db.row_factory = aiosqlite.Row
    await _init_schema(db)
    return db


async def _init_schema(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            path UNINDEXED,
            title,
            body,
            tags,
            tokenize = 'porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS notes_meta (
            path TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            mtime REAL NOT NULL DEFAULT 0,
            size INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '[]',
            links TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS note_links (
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            PRIMARY KEY (src, dst)
        );
        CREATE INDEX IF NOT EXISTS idx_note_links_dst ON note_links(dst);
    """)
    await db.commit()


async def upsert_note(path: str, content: str, mtime: float, size: int) -> Note:
    """Parse and upsert a single note. Returns the parsed Note for callers
    that want to inspect extracted metadata (e.g., echoing tags on response).
    """
    note = parse_note(content, filename_hint=_basename(path))
    db = await _connect()
    try:
        # Replace FTS row (FTS5 has no UPSERT, delete-then-insert).
        await db.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
        await db.execute(
            "INSERT INTO notes_fts (path, title, body, tags) VALUES (?, ?, ?, ?)",
            (path, note.title, note.body, " ".join(note.tags)),
        )
        await db.execute(
            "INSERT OR REPLACE INTO notes_meta (path, title, mtime, size, tags, links) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, note.title, mtime, size, json.dumps(note.tags), json.dumps(list(dict.fromkeys(note.links)))),
        )
        await db.execute("DELETE FROM note_links WHERE src = ?", (path,))
        for target in set(note.links):
            await db.execute(
                "INSERT OR IGNORE INTO note_links (src, dst) VALUES (?, ?)",
                (path, _norm_target(target)),
            )
        await db.commit()
    finally:
        await db.close()
    return note


async def remove_note(path: str) -> None:
    """Drop a note from all index tables. Idempotent."""
    db = await _connect()
    try:
        await db.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
        await db.execute("DELETE FROM notes_meta WHERE path = ?", (path,))
        await db.execute("DELETE FROM note_links WHERE src = ?", (path,))
        await db.commit()
    finally:
        await db.close()


async def rename_note(old_path: str, new_path: str) -> None:
    """Move a note's index rows from old_path to new_path. Links that
    referenced old_path are NOT rewritten — callers who care about keeping
    wikilinks live should do a rewrite pass themselves."""
    db = await _connect()
    try:
        await db.execute("UPDATE notes_meta SET path = ? WHERE path = ?", (new_path, old_path))
        await db.execute("UPDATE note_links SET src = ? WHERE src = ?", (new_path, old_path))
        # FTS5 rows are rebuilt rather than renamed (simpler, rows are small).
        row = await (await db.execute(
            "SELECT title, mtime, size, tags FROM notes_meta WHERE path = ?", (new_path,)
        )).fetchone()
        if row:
            await db.execute("DELETE FROM notes_fts WHERE path = ?", (old_path,))
        await db.commit()
    finally:
        await db.close()


async def search(query: str, tag: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    """FTS5 search across title/body/tags. Returns list of {path, title,
    snippet, tags} ordered by bm25 rank. Empty query returns recent notes
    (by mtime) so the UI can present sensible defaults."""
    db = await _connect()
    try:
        rows: list[dict[str, Any]]
        if query.strip():
            # FTS5 match. snippet() returns a highlighted excerpt.
            sql = (
                "SELECT fts.path AS path, fts.title AS title, "
                "snippet(notes_fts, 2, '<mark>', '</mark>', '…', 12) AS snippet, "
                "m.tags AS tags "
                "FROM notes_fts fts JOIN notes_meta m ON m.path = fts.path "
                "WHERE notes_fts MATCH ? "
            )
            params: list[Any] = [_sanitize_query(query)]
            if tag:
                sql += "AND m.tags LIKE ? "
                params.append(f"%{json.dumps(tag.lower())[1:-1]}%")
            sql += "ORDER BY bm25(notes_fts) LIMIT ?"
            params.append(limit)
            cur = await db.execute(sql, params)
        else:
            sql = "SELECT path, title, '' AS snippet, tags FROM notes_meta "
            params = []
            if tag:
                sql += "WHERE tags LIKE ? "
                params.append(f"%{json.dumps(tag.lower())[1:-1]}%")
            sql += "ORDER BY mtime DESC LIMIT ?"
            params.append(limit)
            cur = await db.execute(sql, params)
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            try:
                r["tags"] = json.loads(r["tags"])
            except Exception:
                r["tags"] = []
        return rows
    finally:
        await db.close()


async def backlinks(path: str) -> list[dict[str, Any]]:
    """All notes that wikilink to this note's basename."""
    target = _norm_target(_basename(path))
    db = await _connect()
    try:
        cur = await db.execute(
            "SELECT l.src AS path, m.title AS title "
            "FROM note_links l LEFT JOIN notes_meta m ON m.path = l.src "
            "WHERE l.dst = ? AND l.src != ? "
            "ORDER BY m.mtime DESC",
            (target, path),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def list_tags() -> list[dict[str, Any]]:
    """All unique tags with occurrence counts, sorted by count desc."""
    db = await _connect()
    try:
        cur = await db.execute("SELECT tags FROM notes_meta")
        counts: dict[str, int] = {}
        for row in await cur.fetchall():
            try:
                for t in json.loads(row["tags"]):
                    counts[t] = counts.get(t, 0) + 1
            except Exception:
                continue
        return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    finally:
        await db.close()


async def rebuild_index(vault: Path) -> dict[str, int]:
    """Drop and re-index every .md file under vault. Skips hidden files
    (leading dot) and the archive folder.
    """
    count = 0
    db = await _connect()
    try:
        await db.executescript(
            "DELETE FROM notes_fts; DELETE FROM notes_meta; DELETE FROM note_links;"
        )
        await db.commit()
    finally:
        await db.close()

    for path in vault.rglob("*.md"):
        rel = path.relative_to(vault).as_posix()
        if _is_hidden(rel):
            continue
        try:
            text = path.read_text()
        except Exception as e:
            logger.warning("notes: skip %s (%s)", rel, e)
            continue
        stat = path.stat()
        await upsert_note(rel, text, stat.st_mtime, stat.st_size)
        count += 1
    return {"indexed": count}


# ── helpers ─────────────────────────────────────────────────────────────────


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _norm_target(s: str) -> str:
    return s.strip().lower()


def _is_hidden(rel: str) -> bool:
    return any(part.startswith(".") for part in rel.split("/"))


def _sanitize_query(q: str) -> str:
    """FTS5 MATCH takes a query grammar — operators like AND/OR/NOT/"" are
    meaningful. To avoid crashes on operator-like input from naive users,
    fall back to a prefix-style tokenized query: each whitespace-split term
    becomes `term*`."""
    q = q.strip()
    # If it already looks like valid FTS syntax (has quoted phrase or explicit
    # operator), pass through. Otherwise tokenize.
    if any(ch in q for ch in '"()') or " OR " in q or " AND " in q:
        return q
    tokens = [t for t in q.split() if t]
    return " ".join(f'"{t}"*' for t in tokens) if tokens else q
