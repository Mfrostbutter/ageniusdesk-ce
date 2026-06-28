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


def _resolved_under(abs_path: Path, prefixes: list[str]) -> bool:
    """Scope check against the RESOLVED on-disk location.

    `storage.resolve`/`_resolve_dir` validate the requested STRING and confirm it
    stays under the vault, but `Path.resolve()` follows symlinks: a link inside
    the vault (e.g. `research/evil -> ../user`, dropped by an Obsidian sync or a
    prior in-process module) would let an in-scope-looking request land outside
    the module's prefixes. We re-check where I/O actually lands, not just the
    requested path, so a symlink cannot redirect a write/read/move out of scope.
    """
    try:
        resolved_rel = abs_path.relative_to(storage.VAULT_DIR.resolve()).as_posix()
    except ValueError:
        return False
    return _under(resolved_rel, prefixes)


def _note_rel_in_scope(path: str, prefixes: list[str]) -> str:
    """Note-safe resolve (storage.resolve) + scope check. Returns vault-rel path.

    Both the requested string AND the resolved on-disk location must be in scope
    (see _resolved_under) so a symlink inside the vault cannot defeat scoping.
    """
    try:
        vp = storage.resolve(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid path: {e}")
    if not (_under(vp.rel, prefixes) and _resolved_under(vp.abs, prefixes)):
        raise HTTPException(status_code=403, detail=f"path '{vp.rel}' is outside the module's declared scope")
    return vp.rel


def _dir_rel_in_scope(rel: str, prefixes: list[str]) -> tuple[str, Path]:
    try:
        nrel, abspath = _resolve_dir(rel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid path: {e}")
    if not (_under(nrel, prefixes) and _resolved_under(abspath, prefixes)):
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

# Per-write byte cap. Disk exhaustion is not fully contained in v1 (see the spec
# enforcement matrix), but a coarse per-call limit cheaply raises the bar against
# a module filling the disk through the notes bridge.
MAX_NOTE_BYTES = 1_000_000


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
    if len(payload.content.encode("utf-8")) > MAX_NOTE_BYTES:
        raise HTTPException(status_code=413, detail=f"note exceeds the {MAX_NOTE_BYTES}-byte write limit")
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
    # Skip symlinks: c.is_dir() follows them, so a link could surface a name that
    # points outside the listed scope (see _resolved_under for the I/O guard).
    folders = sorted(
        c.name for c in abspath.iterdir() if c.is_dir() and not c.is_symlink() and not c.name.startswith(".")
    )
    return {"folders": folders}


@bridge_app.post("/api/_host/notes/list-files")
async def notes_list_files(payload: _RelPayload, grant: BridgeGrant = Depends(_require_grant)):
    nrel, abspath = _dir_rel_in_scope(payload.rel, grant.read_paths)
    if not abspath.is_dir():
        return {"files": []}
    files = sorted(
        c.name for c in abspath.iterdir() if c.is_file() and not c.is_symlink() and not c.name.startswith(".")
    )
    return {"files": files}


# ── assistant.complete namespace (tool-free LLM, host-resolved key) ───────────


class _CompletePayload(BaseModel):
    user: str
    system: str = ""
    model: str = ""
    max_tokens: int = 8000


@bridge_app.post("/api/_host/assistant/complete")
async def assistant_complete(payload: _CompletePayload, grant: BridgeGrant = Depends(_require_grant)):
    if not grant.host_assistant:
        raise HTTPException(status_code=403, detail="module did not declare host.assistant")
    # Lazy import: keep the assistant provider stack out of the bridge import path.
    from backend.modules.assistant.completion import HARD_MAX_TOKENS, CompletionError, complete
    mt = max(1, min(int(payload.max_tokens or 8000), HARD_MAX_TOKENS))
    try:
        text = await complete(payload.system, payload.user, model=payload.model, max_tokens=mt)
    except CompletionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"text": text}


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
    lifespan when isolation is enabled, BEFORE any worker is spawned.

    The pre-reserved port can in theory be grabbed by another process between
    reservation and bind (a TOCTOU window). If the bind fails we pick a fresh
    port and retry; because workers are spawned only after this returns, they
    always read the final, actually-bound port from bridge_url().
    """
    global _server, _bridge_port
    if _server is not None:
        return bridge_url()
    import asyncio

    import uvicorn

    last_err: object = "did not start in time"
    for _attempt in range(5):
        config = uvicorn.Config(bridge_app, host="127.0.0.1", port=_ensure_port(), log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        for _ in range(200):
            if getattr(server, "started", False):
                _server = server
                _publish_bound_port(server)
                logger.info("host bridge listening on %s", bridge_url())
                return bridge_url()
            if task.done():
                break
            await asyncio.sleep(0.025)
        # Bind likely failed (port taken) or the server never came up: capture the
        # cause, drop the reserved port, and retry with a fresh one.
        last_err = task.exception() if task.done() else last_err
        if not task.done():
            server.should_exit = True
        _bridge_port = None
    raise RuntimeError(f"host bridge failed to start after retries: {last_err}")


def _publish_bound_port(server) -> None:
    """Adopt the port uvicorn actually bound as authoritative (closes the gap
    between the pre-reserved port and the real listener)."""
    global _bridge_port
    try:
        actual = server.servers[0].sockets[0].getsockname()[1]
        if actual:
            _bridge_port = actual
    except (AttributeError, IndexError, OSError):  # pragma: no cover - version-dependent
        pass


async def stop_bridge() -> None:
    global _server
    if _server is not None:
        _server.should_exit = True
        _server = None
