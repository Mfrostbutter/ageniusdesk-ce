"""FastAPI routes for the notes vault (Obsidian-compatible markdown + FTS5)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.modules.notes import index as _index
from backend.modules.notes import storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notes", tags=["notes"])


class WriteRequest(BaseModel):
    content: str


class AppendRequest(BaseModel):
    content: str


@router.get("/tree")
async def get_tree():
    storage.ensure_vault()
    return storage.list_tree()


@router.get("/search")
async def search_notes(q: str = "", tag: str = "", limit: int = 30):
    storage.ensure_vault()
    results = await _index.search(q, tag=tag or None, limit=min(max(limit, 1), 200))
    return {"results": results}


@router.get("/tags")
async def list_tags():
    storage.ensure_vault()
    return {"tags": await _index.list_tags()}


@router.post("/reindex")
async def reindex():
    """Drop and rebuild the full-text index from disk. Use after editing
    files directly (e.g. via Obsidian sync) to pick up external changes."""
    storage.ensure_vault()
    return await _index.rebuild_index(storage.VAULT_DIR)


# Catch-all must come AFTER specific sub-paths so /tree etc. don't match first.
@router.get("/{path:path}/backlinks")
async def get_backlinks(path: str):
    storage.ensure_vault()
    vp = _resolve_or_400(path)
    return {"backlinks": await _index.backlinks(vp.rel)}


@router.get("/{path:path}")
async def read_note(path: str):
    storage.ensure_vault()
    try:
        vp = _resolve_or_400(path)
        content = storage.read(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"path": vp.rel, "content": content}


@router.put("/{path:path}")
async def write_note(path: str, req: WriteRequest):
    storage.ensure_vault()
    _resolve_or_400(path)
    result = await storage.write(path, req.content)
    return result


@router.post("/{path:path}/append")
async def append_note(path: str, req: AppendRequest):
    storage.ensure_vault()
    _resolve_or_400(path)
    result = await storage.append(path, req.content)
    return result


@router.delete("/{path:path}")
async def delete_note(path: str):
    storage.ensure_vault()
    try:
        _resolve_or_400(path)
        return await storage.archive(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")


def _resolve_or_400(path: str):
    try:
        return storage.resolve(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
