"""Host capability bridge for out-of-process community modules.

A sandboxed worker holds no host credentials and cannot import `backend`, so any
privileged action goes through this bridge: a loopback-only HTTP surface
(`/api/_host/*`) the host serves, separate from the public bind. Each worker is
issued a per-spawn bearer token that maps to its module's declared capabilities;
every call is gated by that grant and path-scoped server-side.

Phase 3 ships the `notes.*` namespace (vault read/write within the module's
declared paths). `assistant.complete` is phase 4; `broadcast` is later.

Security posture:
  - Bound to loopback only; never mounted on the public app.
  - Per-module token (random per spawn), revoked on worker stop.
  - Cookie-bearing requests are rejected: this is not a browser surface.
  - Paths are validated AND scoped to the module's declared prefixes here, on the
    host, never trusted from the worker.
"""

from __future__ import annotations

import logging
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from backend.modules.notes import storage

logger = logging.getLogger(__name__)


# ── Per-module grants (token -> capabilities) ────────────────────────────────


@dataclass
class BridgeGrant:
    module_id: str
    write_paths: list[str] = field(default_factory=list)
    read_paths: list[str] = field(default_factory=list)  # effective: includes write_paths
    host_assistant: bool = False
    host_broadcast: bool = False


_grants: dict[str, BridgeGrant] = {}


def mint(module_id: str, capabilities) -> str:
    """Issue a bridge token for a module spawn, scoped to its declared caps."""
    fs = getattr(capabilities, "filesystem", None)
    host = getattr(capabilities, "host", None)
    write_paths = [p.strip("/").strip() for p in (getattr(fs, "write_paths", []) or []) if p.strip("/").strip()]
    read_only = [p.strip("/").strip() for p in (getattr(fs, "read_paths", []) or []) if p.strip("/").strip()]
    read_paths = list(dict.fromkeys(read_only + write_paths))  # write paths are readable
    token = secrets.token_urlsafe(32)
    _grants[token] = BridgeGrant(
        module_id=module_id,
        write_paths=write_paths,
        read_paths=read_paths,
        host_assistant=bool(getattr(host, "assistant", False)),
        host_broadcast=bool(getattr(host, "broadcast", False)),
    )
    return token


def revoke(token: str) -> None:
    _grants.pop(token, None)


def revoke_module(module_id: str) -> None:
    for tok in [t for t, g in _grants.items() if g.module_id == module_id]:
        _grants.pop(tok, None)


def grant_for(token: str) -> BridgeGrant | None:
    return _grants.get(token)


# ── Path scoping (validated + scoped on the host, never trusted from worker) ──


def _under(rel: str, prefixes: list[str]) -> bool:
    """Segment-aware containment: 'research/x' is under 'research' but
    'research-evil/x' is not."""
    for p in prefixes:
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


def _resolve_dir(rel: str) -> tuple[str, Path]:
    """Validate a vault-relative DIRECTORY path (no `.md` suffix forced). Mirrors
    storage.resolve's escape checks. Returns (normalized_rel, abs_path)."""
    if not rel or rel in (".", "/"):
        raise ValueError("empty path")
    rel = rel.lstrip("/")
    if "\x00" in rel or "\\" in rel:
        raise ValueError("invalid characters in path")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError(f"invalid path segment: {part!r}")
    abs_path = (storage.VAULT_DIR / rel).resolve()
    try:
        abs_path.relative_to(storage.VAULT_DIR.resolve())
    except ValueError as e:
        raise ValueError("path escapes vault") from e
    return "/".join(parts), abs_path


def _note_rel_in_scope(path: str, prefixes: list[str]) -> str:
    """Note-safe resolve (storage.resolve) + scope check. Returns vault-rel path."""
    try:
        vp = storage.resolve(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid path: {e}")
    if not _under(vp.rel, prefixes):
        raise HTTPException(status_code=403, detail=f"path '{vp.rel}' is outside the module's declared scope")
    return vp.rel


def _dir_rel_in_scope(rel: str, prefixes: list[str]) -> tuple[str, Path]:
    try:
        nrel, abspath = _resolve_dir(rel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid path: {e}")
    if not _under(nrel, prefixes):
        raise HTTPException(status_code=403, detail=f"path '{nrel}' is outside the module's declared scope")
    return nrel, abspath


# ── Auth dependency ───────────────────────────────────────────────────────────


async def _require_grant(request: Request) -> BridgeGrant:
    # Not a browser surface: a real worker never sends cookies; reject any that do.
    if request.headers.get("cookie"):
        raise HTTPException(status_code=403, detail="bridge is not a browser surface")
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    grant = grant_for(token) if token else None
    if grant is None:
        raise HTTPException(status_code=401, detail="invalid or missing bridge token")
    return grant


# ── Bridge app: notes.* namespace ─────────────────────────────────────────────

bridge_app = FastAPI(title="agd-host-bridge", docs_url=None, redoc_url=None)


class _WritePayload(BaseModel):
    path: str
    content: str


class _PathPayload(BaseModel):
    path: str


class _RelPayload(BaseModel):
    rel: str


class _MovePayload(BaseModel):
    src: str
    dst: str


@bridge_app.get("/api/_host/health")
async def _health():
    return {"status": "ok"}


@bridge_app.post("/api/_host/notes/write")
async def notes_write(payload: _WritePayload, grant: BridgeGrant = Depends(_require_grant)):
    rel = _note_rel_in_scope(payload.path, grant.write_paths)
    return await storage.write(rel, payload.content)


@bridge_app.post("/api/_host/notes/read")
async def notes_read(payload: _PathPayload, grant: BridgeGrant = Depends(_require_grant)):
    rel = _note_rel_in_scope(payload.path, grant.read_paths)
    try:
        return {"path": rel, "content": storage.read(rel)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="note not found")


@bridge_app.post("/api/_host/notes/delete")
async def notes_delete(payload: _PathPayload, grant: BridgeGrant = Depends(_require_grant)):
    rel = _note_rel_in_scope(payload.path, grant.write_paths)
    try:
        return await storage.archive(rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="note not found")


@bridge_app.post("/api/_host/notes/move")
async def notes_move(payload: _MovePayload, grant: BridgeGrant = Depends(_require_grant)):
    src = _note_rel_in_scope(payload.src, grant.write_paths)
    dst = _note_rel_in_scope(payload.dst, grant.write_paths)
    try:
        content = storage.read(src)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="source note not found")
    result = await storage.write(dst, content)
    await storage.archive(src)
    return result


@bridge_app.post("/api/_host/notes/make-folder")
async def notes_make_folder(payload: _RelPayload, grant: BridgeGrant = Depends(_require_grant)):
    nrel, abspath = _dir_rel_in_scope(payload.rel, grant.write_paths)
    abspath.mkdir(parents=True, exist_ok=True)
    return {"rel": nrel}


@bridge_app.post("/api/_host/notes/list-folders")
async def notes_list_folders(payload: _RelPayload, grant: BridgeGrant = Depends(_require_grant)):
    nrel, abspath = _dir_rel_in_scope(payload.rel, grant.read_paths)
    if not abspath.is_dir():
        return {"folders": []}
    folders = sorted(c.name for c in abspath.iterdir() if c.is_dir() and not c.name.startswith("."))
    return {"folders": folders}


@bridge_app.post("/api/_host/notes/list-files")
async def notes_list_files(payload: _RelPayload, grant: BridgeGrant = Depends(_require_grant)):
    nrel, abspath = _dir_rel_in_scope(payload.rel, grant.read_paths)
    if not abspath.is_dir():
        return {"files": []}
    files = sorted(c.name for c in abspath.iterdir() if c.is_file() and not c.name.startswith("."))
    return {"files": files}


# ── Loopback listener ─────────────────────────────────────────────────────────

_bridge_port: int | None = None
_server = None


def _ensure_port() -> int:
    """Reserve (once) a loopback port for the bridge. Reserved synchronously so a
    worker spawned at import time gets a stable AGD_BRIDGE_URL before the async
    listener starts in the app lifespan."""
    global _bridge_port
    if _bridge_port is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            _bridge_port = s.getsockname()[1]
        finally:
            s.close()
    return _bridge_port


def bridge_url() -> str:
    return f"http://127.0.0.1:{_ensure_port()}"


async def start_bridge() -> str:
    """Start the loopback bridge listener (idempotent). Called from the app
    lifespan when isolation is enabled."""
    global _server
    if _server is not None:
        return bridge_url()
    import asyncio

    import uvicorn

    config = uvicorn.Config(bridge_app, host="127.0.0.1", port=_ensure_port(), log_level="warning")
    _server = uvicorn.Server(config)
    asyncio.create_task(_server.serve())
    for _ in range(100):
        if getattr(_server, "started", False):
            break
        await asyncio.sleep(0.05)
    logger.info("host bridge listening on %s", bridge_url())
    return bridge_url()


async def stop_bridge() -> None:
    global _server
    if _server is not None:
        _server.should_exit = True
        _server = None
