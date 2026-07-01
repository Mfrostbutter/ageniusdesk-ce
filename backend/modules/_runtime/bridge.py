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

from backend.modules.notes import index, storage

logger = logging.getLogger(__name__)


# ── Per-module grants (token -> capabilities) ────────────────────────────────


@dataclass
class EndpointGrant:
    """One operator-consented outbound HTTP target, resolved host-side at mint.

    The worker names this endpoint's `id` + a relative path; the host owns the
    base URL, the credential (by ref, resolved per call), the allowed methods,
    and the pinned address set. The worker can change none of them.
    """
    id: str
    base_url: str            # scheme://host[:port][/base path], no trailing slash
    scheme: str
    host: str                # lowercased hostname or literal IP
    port: int
    methods: set[str]
    verify_tls: bool
    auth: dict               # {type, secret_ref, header, format, param, user, user_ref}
    pinned_ips: set[str]     # resolved once at mint; empty when host is unresolvable


@dataclass
class BridgeGrant:
    module_id: str
    write_paths: list[str] = field(default_factory=list)
    read_paths: list[str] = field(default_factory=list)  # effective: includes write_paths
    host_assistant: bool = False
    host_broadcast: bool = False
    http_endpoints: dict[str, EndpointGrant] = field(default_factory=dict)


_grants: dict[str, BridgeGrant] = {}


def _build_endpoint_grant(ep) -> EndpointGrant | None:
    """Resolve one declared HttpEndpoint into an EndpointGrant (host-side)."""
    import urllib.parse

    base_url = (getattr(ep, "base_url", "") or "").rstrip("/")
    ep_id = getattr(ep, "id", "") or ""
    if not base_url or not ep_id:
        return None
    parts = urllib.parse.urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    methods = {m.upper() for m in (getattr(ep, "methods", None) or ["GET", "HEAD"])}
    auth_obj = getattr(ep, "auth", None)
    auth = {
        "type": (getattr(auth_obj, "type", "") or "").lower(),
        "secret_ref": getattr(auth_obj, "secret_ref", "") or "",
        "header": getattr(auth_obj, "header", "") or "Authorization",
        "format": getattr(auth_obj, "format", "") or "{value}",
        "param": getattr(auth_obj, "param", "") or "",
        "user": getattr(auth_obj, "user", "") or "",
        "user_ref": getattr(auth_obj, "user_ref", "") or "",
    } if auth_obj else {"type": ""}
    return EndpointGrant(
        id=ep_id,
        base_url=base_url,
        scheme=parts.scheme,
        host=host,
        port=port,
        methods=methods,
        verify_tls=bool(getattr(ep, "verify_tls", True)),
        auth=auth,
        pinned_ips=_resolve_ips(host),
    )


def mint(module_id: str, capabilities) -> str:
    """Issue a bridge token for a module spawn, scoped to its declared caps."""
    fs = getattr(capabilities, "filesystem", None)
    host = getattr(capabilities, "host", None)
    write_paths = [p.strip("/").strip() for p in (getattr(fs, "write_paths", []) or []) if p.strip("/").strip()]
    read_only = [p.strip("/").strip() for p in (getattr(fs, "read_paths", []) or []) if p.strip("/").strip()]
    read_paths = list(dict.fromkeys(read_only + write_paths))  # write paths are readable
    http_cap = getattr(host, "http", None)
    http_endpoints: dict[str, EndpointGrant] = {}
    if http_cap and getattr(http_cap, "enabled", False):
        for ep in getattr(http_cap, "endpoints", None) or []:
            grant = _build_endpoint_grant(ep)
            if grant is not None:
                http_endpoints[grant.id] = grant
    token = secrets.token_urlsafe(32)
    _grants[token] = BridgeGrant(
        module_id=module_id,
        write_paths=write_paths,
        read_paths=read_paths,
        host_assistant=bool(getattr(host, "assistant", False)),
        host_broadcast=bool(getattr(host, "broadcast", False)),
        http_endpoints=http_endpoints,
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


def _path_in_scope(path: str, prefixes: list[str]) -> bool:
    """Non-raising scope predicate: True iff `path` is in scope by BOTH the
    requested string AND its resolved on-disk location. Used to filter search
    hits, where a symlink-aliased index entry (indexed under an in-scope-looking
    rel but resolving outside scope) must not leak."""
    try:
        vp = storage.resolve(path)
    except ValueError:
        return False
    return _under(vp.rel, prefixes) and _resolved_under(vp.abs, prefixes)


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


class _SearchPayload(BaseModel):
    query: str = ""
    tag: str = ""
    limit: int = 30


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


@bridge_app.post("/api/_host/notes/append")
async def notes_append(payload: _WritePayload, grant: BridgeGrant = Depends(_require_grant)):
    if len(payload.content.encode("utf-8")) > MAX_NOTE_BYTES:
        raise HTTPException(status_code=413, detail=f"note exceeds the {MAX_NOTE_BYTES}-byte write limit")
    rel = _note_rel_in_scope(payload.path, grant.write_paths)
    return await storage.append(rel, payload.content)


@bridge_app.post("/api/_host/notes/read")
async def notes_read(payload: _PathPayload, grant: BridgeGrant = Depends(_require_grant)):
    rel = _note_rel_in_scope(payload.path, grant.read_paths)
    try:
        return {"path": rel, "content": storage.read(rel)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="note not found")


@bridge_app.post("/api/_host/notes/search")
async def notes_search(payload: _SearchPayload, grant: BridgeGrant = Depends(_require_grant)):
    """Full-text search the vault, but return ONLY hits under the module's declared
    read paths (so a module cannot read snippets of notes outside its scope)."""
    limit = max(1, min(int(payload.limit or 30), 100))
    results = await index.search(payload.query or "", payload.tag or None, limit)
    # Resolved-scope filter (NOT a bare string prefix): a symlink-aliased index
    # entry must not leak a snippet of content that lives outside the read scope.
    scoped = [r for r in results if _path_in_scope(r.get("path", ""), grant.read_paths)]
    return {"results": scoped}


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


# ── http.request namespace (host-mediated outbound HTTP, host-injected creds) ──
#
# The worker names an operator-consented endpoint id + a relative path; the host
# owns the base URL, the credential (resolved per call), the allowed methods, the
# TLS policy, and the address pin. The credential never enters the worker. See the
# http.request bridge spec.

# Response body cap: a module cannot pull an unbounded body back through the bridge.
MAX_HTTP_BYTES = 5_000_000
HTTP_TIMEOUT = 30.0

# Worker-supplied request headers we always strip: the host owns auth + host
# identity, and hop-by-hop headers must not be forwarded. The injected auth is
# applied AFTER this, so it always wins.
_FORBIDDEN_REQ_HEADERS = {
    "authorization", "host", "cookie", "content-length",
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

# Response headers safe to echo back to the worker (allowlist, everything else
# dropped). set-cookie / www-authenticate / the injected auth are never echoed.
_RESP_HEADER_ALLOW = {
    "content-type", "content-length", "content-encoding",
    "etag", "last-modified", "retry-after",
}


def _is_ip(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _resolve_ips(host: str) -> set[str]:
    """Resolve a hostname to its IP set (empty on failure). A literal IP resolves
    to itself. Used to pin an endpoint's address at mint and detect rebinding."""
    if not host:
        return set()
    if _is_ip(host):
        return {host}
    try:
        infos = socket.getaddrinfo(host, None)
        return {info[4][0] for info in infos}
    except (socket.gaierror, OSError):
        return set()


def _validate_rel_path(path: str) -> str:
    """A worker-supplied path must be relative and cannot pivot the target. Reject
    an embedded scheme/authority, parent traversal, and control/escape chars."""
    p = (path or "").strip()
    if not p:
        return ""
    if "\x00" in p or "\\" in p:
        raise HTTPException(status_code=400, detail="invalid characters in path")
    if "://" in p or p.startswith("//"):
        raise HTTPException(status_code=400, detail="path may not contain a scheme or authority")
    if "@" in p.split("?", 1)[0]:
        raise HTTPException(status_code=400, detail="path may not contain '@'")
    # No parent traversal in the path portion (climbing above the base path).
    path_part = p.split("?", 1)[0].split("#", 1)[0]
    if any(seg == ".." for seg in path_part.split("/")):
        raise HTTPException(status_code=400, detail="path may not contain '..'")
    return p


def _build_url(ep: EndpointGrant, path: str, query: dict | None) -> str:
    """base_url + relative path (+ query), then assert the result still points at
    the consented scheme+host+port — the worker cannot pivot off the endpoint."""
    import urllib.parse

    rel = _validate_rel_path(path)
    joined = ep.base_url + ("/" + rel.lstrip("/") if rel else "")
    if query:
        qs = urllib.parse.urlencode({str(k): str(v) for k, v in query.items()})
        if qs:
            joined += ("&" if "?" in joined else "?") + qs
    parts = urllib.parse.urlsplit(joined)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme != ep.scheme or host != ep.host or port != ep.port:
        # Defense in depth: a validated relative path cannot get here, but never
        # issue a call whose target host differs from the consented one.
        raise HTTPException(status_code=400, detail="path resolves outside the consented endpoint host")
    return joined


def _inject_auth(ep: EndpointGrant, headers: dict, url: str) -> str:
    """Resolve the endpoint's secret host-side and inject it. Returns the URL
    (possibly with a query-auth param appended). The worker never sees the value."""
    from backend.config import decrypt_value

    auth = ep.auth or {}
    atype = auth.get("type", "")
    if not atype:
        return url
    ref = auth.get("secret_ref", "")
    value = decrypt_value(f"${ref}") if ref else ""
    if atype == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    elif atype == "header":
        fmt = auth.get("format") or "{value}"
        headers[auth.get("header") or "Authorization"] = fmt.replace("{value}", value)
    elif atype == "basic":
        import base64 as _b64
        user = decrypt_value(f"${auth['user_ref']}") if auth.get("user_ref") else auth.get("user", "")
        token = _b64.b64encode(f"{user}:{value}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    elif atype == "query":
        import urllib.parse
        param = auth.get("param") or "token"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({param: value})}"
    return url


class _HttpRequestPayload(BaseModel):
    endpoint: str
    method: str = "GET"
    path: str = ""
    query: dict | None = None
    headers: dict | None = None
    body: object = None  # str → sent as-is; dict/list → JSON-encoded


@bridge_app.post("/api/_host/http/request")
async def http_request(payload: _HttpRequestPayload, grant: BridgeGrant = Depends(_require_grant)):
    import httpx

    ep = grant.http_endpoints.get(payload.endpoint)
    if ep is None:
        raise HTTPException(status_code=403, detail=f"unknown or undeclared endpoint {payload.endpoint!r}")
    method = (payload.method or "GET").upper()
    if method not in ep.methods:
        raise HTTPException(status_code=403, detail=f"method {method} not permitted on endpoint {ep.id!r}")

    # DNS-rebind guard: an endpoint pinned to an address set at mint must still
    # resolve to one of those addresses. A literal-IP endpoint has no DNS to
    # rebind; an unresolvable-at-mint endpoint (empty pins) skips the check and
    # relies on the host-lock in _build_url. (Connection-level IP pinning via a
    # custom transport is a follow-up; this pre-flight check raises the bar now.)
    if ep.pinned_ips and not _is_ip(ep.host):
        current = _resolve_ips(ep.host)
        if current and not (current & ep.pinned_ips):
            raise HTTPException(
                status_code=502,
                detail=f"endpoint {ep.id!r} host resolves to an unpinned address; re-approve the endpoint",
            )

    # Sanitize worker headers, then inject host-owned auth (auth wins).
    headers: dict[str, str] = {}
    for k, v in (payload.headers or {}).items():
        if str(k).lower() not in _FORBIDDEN_REQ_HEADERS:
            headers[str(k)] = str(v)
    url = _build_url(ep, payload.path, payload.query)
    url = _inject_auth(ep, headers, url)

    # Body: a string is sent verbatim; a dict/list is JSON-encoded (with a default
    # content-type the worker can override via its own header).
    content = None
    if isinstance(payload.body, str):
        content = payload.body.encode("utf-8")
    elif payload.body is not None:
        import json as _json
        content = _json.dumps(payload.body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    try:
        async with httpx.AsyncClient(verify=ep.verify_tls, timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
            resp = await client.request(method, url, headers=headers, content=content)
            raw = resp.content
    except httpx.HTTPError as e:
        # A transport/timeout failure is returned as an error, not raised to a 500.
        raise HTTPException(status_code=502, detail=f"upstream request failed: {type(e).__name__}")

    truncated = len(raw) > MAX_HTTP_BYTES
    raw = raw[:MAX_HTTP_BYTES]
    # Text if it decodes as UTF-8; otherwise base64 with a flag.
    content_encoding = None
    try:
        body_out = raw.decode("utf-8")
    except UnicodeDecodeError:
        import base64 as _b64
        body_out = _b64.b64encode(raw).decode("ascii")
        content_encoding = "base64"

    out_headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in _RESP_HEADER_ALLOW}
    result = {"status": resp.status_code, "headers": out_headers, "body": body_out, "truncated": truncated}
    if content_encoding:
        result["content_encoding"] = content_encoding
    return result


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


async def start_bridge(host: str = "127.0.0.1") -> str:
    """Start the bridge listener (idempotent). Called from the app lifespan when
    isolation is enabled, BEFORE any worker is spawned.

    `host` is the bind address: loopback for the subprocess tier; `0.0.0.0` for
    the container tier, so module containers on the shared Docker network can
    reach it (the port is ephemeral and UNPUBLISHED, so it is reachable only by
    containers on that network, and every call is token + cookie gated).

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
        config = uvicorn.Config(bridge_app, host=host, port=_ensure_port(), log_level="warning")
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
