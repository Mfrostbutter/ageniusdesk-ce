"""http.request: host-mediated outbound HTTP for community modules.

One policy implementation for every isolation mode. The bridge route
(`/api/_host/http/request`) calls `request()` with the worker's granted endpoint
ids; an in-process module calls `request()` directly with its own module id.
Either way the host owns the base URL, the credential, the TLS decision, and the
pinned address; the caller supplies an endpoint id and a relative path.

Enforcement order per call: endpoint grant + active revision, method, path,
same-origin URL, header sanitation, auth injection, pinned dial, capped read.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
from typing import Any

import httpcore
import httpx

from backend import audit
from backend.modules._runtime import endpoints as ep_store

logger = logging.getLogger(__name__)

TIMEOUT_S = 30.0
MAX_BODY_BYTES = 5_000_000
_RESOLVE_CACHE_S = 60.0

# Headers a worker may never set; the host's injected values always win.
_FORBIDDEN_REQ_HEADERS = frozenset({
    "authorization", "host", "cookie", "content-length", "proxy-authorization",
    "connection", "keep-alive", "proxy-authenticate", "te", "trailer", "trailers",
    "transfer-encoding", "upgrade", "expect",
})
_FORBIDDEN_REQ_PREFIXES = ("x-forwarded-", "x-agd-")

# Response headers echoed to the module. Allowlist, not denylist.
_RESP_HEADER_ALLOW = frozenset({
    "content-type", "content-length", "content-disposition", "etag",
    "last-modified", "retry-after", "cache-control", "location",
})


class HttpBridgeError(Exception):
    """Policy or transport failure with an HTTP status for the bridge route."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── path + URL ────────────────────────────────────────────────────────────────


def validate_rel_path(path: str) -> str:
    """Relative path only: no scheme, host, userinfo, traversal, query, or fragment."""
    p = (path or "").strip()
    if not p:
        return ""
    if "\x00" in p or "\\" in p:
        raise HttpBridgeError(400, "invalid path: control or backslash characters")
    if "://" in p or p.startswith("//"):
        raise HttpBridgeError(400, "invalid path: absolute URLs are not allowed")
    if "?" in p or "#" in p:
        raise HttpBridgeError(400, "invalid path: put query parameters in `query`")
    if "@" in p:
        raise HttpBridgeError(400, "invalid path: '@' is not allowed")
    segs = p.split("/")
    if any(seg == ".." for seg in segs):
        raise HttpBridgeError(400, "invalid path: traversal segment")
    if any(urllib.parse.unquote(seg) == ".." for seg in segs):
        raise HttpBridgeError(400, "invalid path: encoded traversal segment")
    return "/" + p.lstrip("/")


def build_url(base_url: str, rel: str, query: dict | None) -> str:
    url = base_url + (rel if rel else "")
    if query:
        qs = urllib.parse.urlencode({str(k): str(v) for k, v in query.items()})
        if qs:
            url += "?" + qs
    b, u = urllib.parse.urlsplit(base_url), urllib.parse.urlsplit(url)
    if (u.scheme, u.hostname, u.port, u.username, u.password) != (b.scheme, b.hostname, b.port, None, None):
        raise HttpBridgeError(400, "path escapes the consented endpoint origin")
    return url


def sanitize_headers(headers: dict | None, auth_header: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        kl = str(k).lower().strip()
        if not kl or kl in _FORBIDDEN_REQ_HEADERS or kl.startswith(_FORBIDDEN_REQ_PREFIXES):
            continue
        if auth_header and kl == auth_header.lower():
            continue
        if "\n" in str(v) or "\r" in str(v):
            raise HttpBridgeError(400, f"invalid header value for {k!r}")
        out[str(k)] = str(v)
    return out


# ── auth ──────────────────────────────────────────────────────────────────────


def _secret_present(name: str) -> bool:
    from backend.config import load_secrets

    base = name.split(".", 1)[0]
    return bool(os.environ.get(base)) or base in load_secrets()


def _resolve(name: str) -> str:
    from backend.config import decrypt_value

    if not name:
        return ""
    if not _secret_present(name):
        raise HttpBridgeError(503, f"secret {name.split('.', 1)[0]!r} is not configured; add it under Secrets")
    value = decrypt_value(f"${name}")
    if not value or value == name:
        raise HttpBridgeError(503, f"secret {name.split('.', 1)[0]!r} could not be resolved")
    return value


def inject_auth(auth: dict | None, headers: dict[str, str], url: str) -> str:
    """Apply the endpoint's auth to headers/url. Returns the (maybe changed) url."""
    if not auth:
        return url
    atype = (auth.get("type") or "").lower()
    value = _resolve(auth.get("secret_ref", ""))
    if atype == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    elif atype == "header":
        fmt = auth.get("format") or "{value}"
        headers[auth.get("header") or "Authorization"] = fmt.replace("{value}", value)
    elif atype == "basic":
        user = _resolve(auth["user_ref"]) if auth.get("user_ref") else (auth.get("user") or "")
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{value}".encode()).decode()
    elif atype == "query":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({auth.get('param') or 'token': value})}"
    else:
        raise HttpBridgeError(500, f"unsupported auth type {atype!r}")
    return url


# ── pinned transport ──────────────────────────────────────────────────────────


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Dial only the pinned IPs for the consented host. httpcore still starts TLS
    with the ORIGINAL hostname as SNI/verification target, so a certificate is
    checked against the consented name, not the address."""

    def __init__(self, host: str, ips: list[str], inner: httpcore.AsyncNetworkBackend | None = None):
        self._host = host.lower()
        self._ips = list(ips)
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host.lower() != self._host:
            raise httpcore.ConnectError(f"refusing to dial {host!r}: not the consented host")
        last: Exception | None = None
        for ip in self._ips:
            try:
                return await self._inner.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except Exception as e:  # noqa: BLE001 - try the next pinned address
                last = e
        raise last or httpcore.ConnectError("no pinned address reachable")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are not permitted")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, verify: bool, host: str, ips: list[str]):
        super().__init__(verify=verify)
        ssl_ctx = httpx.create_ssl_context(verify=verify)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_ctx, network_backend=PinnedBackend(host, ips), http1=True, http2=False
        )


_resolve_cache: dict[tuple[str, int], tuple[float, list[str]]] = {}


def dial_ips(rev: dict[str, Any]) -> list[str]:
    """Pinned IPs the call may dial. Fails closed when the host no longer
    resolves to any pinned address (DNS-rebinding guard)."""
    host, port, _ = ep_store.host_of(rev["base_url"])
    pinned = list(rev.get("pinned_ips") or [])
    if not pinned:
        raise HttpBridgeError(409, f"endpoint host {host!r} has no pinned address; re-pin it in the module manager")
    if ep_store.is_ip_literal(host):
        return [host]
    key = (host, port)
    now = time.monotonic()
    cached = _resolve_cache.get(key)
    if cached and now - cached[0] < _RESOLVE_CACHE_S:
        current = cached[1]
    else:
        current = ep_store.resolve_ips(host, port)
        _resolve_cache[key] = (now, current)
    usable = [ip for ip in pinned if ip in current]
    if not usable:
        raise HttpBridgeError(
            409, f"endpoint host {host!r} no longer resolves to a pinned address; re-pin it in the module manager"
        )
    return usable


# Test seam: replace to intercept the outbound client (keeps respx optional).
def _make_client(verify: bool, host: str, ips: list[str]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=PinnedTransport(verify, host, ips), timeout=TIMEOUT_S, follow_redirects=False
    )


# ── request ───────────────────────────────────────────────────────────────────


def _encode_body(payload: dict, headers: dict[str, str]) -> bytes | None:
    body = payload.get("body")
    if body is None:
        return None
    if isinstance(body, str):
        if (payload.get("body_encoding") or "").lower() == "base64":
            try:
                return base64.b64decode(body, validate=True)
            except Exception:
                raise HttpBridgeError(400, "body is not valid base64")
        return body.encode("utf-8")
    if not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"
    return json.dumps(body).encode("utf-8")


async def _read_capped(resp: httpx.Response) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            break
    raw = b"".join(chunks)
    truncated = len(raw) > MAX_BODY_BYTES
    return raw[:MAX_BODY_BYTES], truncated


def _shape_response(status: int, headers: httpx.Headers, raw: bytes, truncated: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "headers": {k.lower(): v for k, v in headers.items() if k.lower() in _RESP_HEADER_ALLOW},
        "truncated": truncated,
    }
    try:
        out["body"] = raw.decode("utf-8")
    except UnicodeDecodeError:
        out["body"] = base64.b64encode(raw).decode("ascii")
        out["content_encoding"] = "base64"
    return out


async def request(
    module_id: str,
    payload: dict[str, Any],
    allowed_endpoints: list[str] | None = None,
) -> dict[str, Any]:
    """Make one policy-checked upstream call. `allowed_endpoints` is the grant's
    declared id set (None = trust the store, in-process caller)."""
    endpoint_id = str(payload.get("endpoint") or "")
    method = (payload.get("method") or "GET").upper()
    rel_in = payload.get("path") or ""
    outcome = "error"
    status_out: int | None = None
    truncated = False
    try:
        if allowed_endpoints is not None and endpoint_id not in allowed_endpoints:
            raise HttpBridgeError(403, f"endpoint {endpoint_id!r} is not declared by this module")
        rev = ep_store.get(module_id, endpoint_id)
        if rev is None:
            raise HttpBridgeError(403, f"endpoint {endpoint_id!r} has no host configuration")
        if rev.get("status") != ep_store.STATUS_ACTIVE:
            raise HttpBridgeError(403, f"endpoint {endpoint_id!r} awaits operator confirmation in the module manager")
        if method not in set(rev.get("methods") or []):
            raise HttpBridgeError(403, f"method {method} is not granted on endpoint {endpoint_id!r}")

        rel = validate_rel_path(rel_in)
        url = build_url(rev["base_url"], rel, payload.get("query") or None)
        auth = rev.get("auth") or None
        headers = sanitize_headers(payload.get("headers"), (auth or {}).get("header", "") if auth else "")
        url = inject_auth(auth, headers, url)
        content = _encode_body(payload, headers)

        from backend.net import tls_verify

        verify = bool(rev.get("verify_tls", True)) and tls_verify()
        host, _port, _scheme = ep_store.host_of(rev["base_url"])
        ips = dial_ips(rev)
        try:
            async with _make_client(verify, host, ips) as client:
                async with client.stream(method, url, headers=headers, content=content) as resp:
                    raw, truncated = await _read_capped(resp)
                    status_out = resp.status_code
                    shaped = _shape_response(resp.status_code, resp.headers, raw, truncated)
        except httpx.HTTPError as e:
            raise HttpBridgeError(502, f"upstream request failed: {type(e).__name__}")
        outcome = "ok"
        return shaped
    finally:
        audit.record(
            "module.http_request", outcome,
            module=module_id, endpoint=endpoint_id, method=method, path=rel_in,
            status=status_out, truncated=truncated,
        )


def grant_summary(module_id: str, endpoint_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """What the module may currently call: id, status, methods, host. No secrets."""
    return ep_store.summary(module_id, endpoint_ids)
