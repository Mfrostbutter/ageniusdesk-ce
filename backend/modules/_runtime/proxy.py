"""Reverse proxy: forward /api/{id}/* from the host to a module's worker.

The host authenticates and CSRF-checks the request (its normal middleware) BEFORE
this handler runs, then forwards to the worker with host identity stripped
(no Cookie, no Authorization) and the per-worker proxy secret added. Trusted
X-AGD-* identity headers are forwarded only when the identity middleware
(identity.py) stamped them; any other X-AGD-* header is dropped. The worker
hosts the module's router at its real /api/{id}/... path, so the full path is
forwarded unchanged. Responses are streamed (module job detail can be large).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.modules._runtime import supervisor

logger = logging.getLogger(__name__)

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1). Cookie + Authorization
# are dropped so the worker never sees the host session/credentials.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
_STRIP_REQUEST = _HOP_BY_HOP | {"cookie", "authorization", "host", "content-length"}
# Strip auth/origin-sensitive response headers so a community module can't
# influence host-origin browser state: Set-Cookie (session/CSRF poisoning, forced
# logout), Clear-Site-Data (wipe host storage/cookies), WWW-Authenticate (force a
# browser auth prompt). Also drop length/encoding that no longer match a streamed
# body. A denylist is sufficient here; module responses are otherwise data.
_STRIP_RESPONSE = _HOP_BY_HOP | {
    "content-length", "content-encoding",
    "set-cookie", "set-cookie2", "clear-site-data", "www-authenticate",
}


def _forward_request_headers(headers, trusted_identity: bool = False) -> dict[str, str]:
    from backend.modules._runtime.identity import IDENTITY_HEADERS

    out: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in _STRIP_REQUEST:
            continue
        # Only host-stamped identity crosses to the worker; every other X-AGD-*
        # (CSRF token, anything a browser invented) stays on the host side.
        if kl.startswith("x-agd-") and not (trusted_identity and kl in IDENTITY_HEADERS):
            continue
        out[k] = v
    return out


def _forward_response_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _STRIP_RESPONSE}


async def _proxy(module_id: str, request: Request) -> StreamingResponse | JSONResponse:
    worker = supervisor.get(module_id)
    if worker is None or not worker.is_alive():
        return JSONResponse({"detail": f"module '{module_id}' worker is not running"}, status_code=502)

    rel = request.url.path
    if request.url.query:
        rel = f"{rel}?{request.url.query}"
    headers = _forward_request_headers(request.headers, trusted_identity=bool(request.scope.get("agd_identity")))
    headers["x-agd-proxy-secret"] = worker.proxy_secret

    # Stream the request body to the worker (no full buffering in host memory).
    req = worker.client.build_request(request.method, rel, headers=headers, content=request.stream())
    try:
        resp = await worker.client.send(req, stream=True)
    except Exception as e:
        logger.warning("proxy to module '%s' failed: %s", module_id, e)
        return JSONResponse({"detail": f"module '{module_id}' is unreachable"}, status_code=502)

    async def _body():
        # Close the upstream stream on success, upstream failure, AND client
        # disconnect (Starlette cancels the generator; `finally` still runs).
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        _body(),
        status_code=resp.status_code,
        headers=_forward_response_headers(resp.headers),
    )


def register_proxy_route(app: FastAPI, module_id: str) -> None:
    """Mount a catch-all route that forwards /api/{module_id}/... to its worker."""

    async def endpoint(request: Request):
        return await _proxy(module_id, request)

    # One route per module id, matching the module prefix and any subpath.
    app.add_api_route(
        f"/api/{module_id}/{{rest:path}}",
        endpoint,
        methods=_ALL_METHODS,
        include_in_schema=False,
        name=f"proxy:{module_id}",
    )
    # Also match the bare prefix (no trailing subpath), e.g. /api/{id}.
    app.add_api_route(
        f"/api/{module_id}",
        endpoint,
        methods=_ALL_METHODS,
        include_in_schema=False,
        name=f"proxy-root:{module_id}",
    )
