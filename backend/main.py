"""AgeniusDesk — FastAPI application."""

import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import (
    get_active_instance,
    get_active_theme,
    harden_file_permissions,
    is_setup_complete,
    load_config,
    migrate_inline_to_secrets,
    settings,
)
from backend.database import close_db, get_db
from backend.module_registry import APP_VERSION
from backend.modules import register_modules
from backend.websocket import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await get_db()
    try:
        harden_file_permissions()
    except Exception as e:
        logger.exception("harden_file_permissions failed: %s", e)
    try:
        migrate_inline_to_secrets()
    except Exception as e:
        logger.exception("migrate_inline_to_secrets failed: %s", e)
    try:
        from backend.config import migrate_legacy_enc_to_fernet
        migrate_legacy_enc_to_fernet()
    except Exception as e:
        logger.exception("migrate_legacy_enc_to_fernet failed: %s", e)
    try:
        from backend.config import settings as _settings
        from backend.config_overlay import apply_overlay_to_settings, load_config_overlay
        apply_overlay_to_settings(_settings, load_config_overlay())
    except Exception as e:
        logger.exception("config_overlay apply failed: %s", e)

    # FastMCP's streamable-HTTP manager requires an async task group that
    # only starts when the MCP Starlette app's lifespan runs. Mounted
    # sub-apps' lifespans are NOT invoked by FastAPI, so we drive it here.
    try:
        from backend.modules.dashboard_mcp.server import mcp as _dashboard_mcp
        _mcp_sm = _dashboard_mcp.session_manager
    except Exception:
        _mcp_sm = None

    # C3: seed the constitution file if it doesn't exist yet.
    try:
        from backend.modules.assistant.baseline import loader as _baseline_loader
        await _baseline_loader.ensure_baseline()
    except Exception as e:
        logger.exception("baseline ensure_baseline failed: %s", e)

    # Harness skill library: seed the curated n8n skills into the vault on first
    # run so the assistant + Code Lab have focused n8n guidance out of the box.
    try:
        from backend.modules.notes.skills_seed import ensure_skills
        await ensure_skills()
    except Exception as e:
        logger.exception("skills ensure_skills failed: %s", e)

    # Built-in n8n-mcp: best-effort auto-install (docs mode) when Docker is
    # available, so Code Lab has node intelligence out of the box. Runs in the
    # background — the image pull must not block serving.
    try:
        import asyncio as _aio

        from backend.modules.assistant.n8n_mcp_provision import ensure_n8n_mcp
        _aio.create_task(ensure_n8n_mcp())
    except Exception as e:
        logger.debug("n8n-mcp auto-provision kickoff failed: %s", e)

    # Cost observability: refresh the LLM price book from OpenRouter in the
    # background (best-effort; bundled defaults + last-good cache cover failures).
    try:
        import asyncio as _asyncio

        from backend.modules.observability import pricing as _pricing
        _asyncio.create_task(_pricing.refresh())
    except Exception as e:
        logger.debug("price book refresh kickoff failed: %s", e)

    # Out-of-process module isolation: start the loopback capability bridge before
    # serving, THEN spawn the isolated-module workers (deferred from import-time
    # registration) so a worker that calls the bridge during startup finds it
    # already listening. Only when isolation is on (default in_process is dormant).
    try:
        from backend.modules import _isolation_mode as _iso
        from backend.modules import start_isolated_workers as _start_isolated_workers
        _mode = _iso()
        if _mode in ("subprocess", "container"):
            from backend.modules._runtime import bridge as _bridge
            # Container workers reach the bridge over the shared Docker network, so
            # it must bind 0.0.0.0 there (unpublished port, token + cookie gated).
            await _bridge.start_bridge(host="0.0.0.0" if _mode == "container" else "127.0.0.1")
            await _start_isolated_workers()
    except Exception as e:
        logger.exception("host bridge / isolated worker start failed: %s", e)

    logger.info("AgeniusDesk started, database ready")
    # F2: warn when the in-app auth gate is off, so an operator on a naked-port
    # self-host knows privileged routes rely on an edge proxy for auth.
    try:
        from backend.config import settings as _settings
        if _settings.agd_disable_login:
            logger.warning(
                "AGD_DISABLE_LOGIN is set: browser login is OFF. Anyone who can reach "
                "this port has full access. Use this only on a trusted localhost bind."
            )
        elif not _settings.agd_require_auth:
            logger.info(
                "Local account login is enforced. First browser visit will prompt to "
                "create an owner account. Edge identity (Cloudflare Access) still "
                "satisfies the gate without a local account."
            )
    except Exception:
        pass
    if _mcp_sm is not None:
        async with _mcp_sm.run():
            yield
    else:
        yield
    # Shutdown
    try:
        from backend.modules._runtime import supervisor as _supervisor
        _supervisor.stop_all()  # subprocess workers (skips container workers)
    except Exception as e:
        logger.warning("module worker stop_all failed: %s", e)
    try:
        from backend.modules._runtime import containers as _containers
        await _containers.stop_all_containers()
        await _containers.close_docker()
    except Exception as e:
        logger.warning("module container teardown failed: %s", e)
    try:
        from backend.modules._runtime import bridge as _bridge
        await _bridge.stop_bridge()
    except Exception as e:
        logger.warning("host bridge stop failed: %s", e)
    await close_db()


app = FastAPI(title="AgeniusDesk", version=APP_VERSION, lifespan=lifespan)


def _cors_origins() -> list[str]:
    raw = (settings.agd_cors_origins or "*").strip()
    if raw == "*" or not raw:
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


_PUBLIC_API_EXACT = frozenset({
    "/api/status",
    "/api/health/docker-env",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/login/totp",
    "/api/auth/forgot",
    "/api/auth/reset",
})
_PUBLIC_API_PREFIXES = ("/api/v1/",)
_LEGACY_WEBHOOK_EXACT = frozenset({
    "/api/errors/webhook",
    "/api/messages/webhook",
})
# OTLP ingest is machine-ingest (n8n's OTel exporter, not a browser): exempt
# from the session gate, token-checked by AGD_OTEL_TOKEN, and only live when
# the receiver is enabled. Only /v1/traces has a handler; a metrics receiver is
# not implemented, so its path is intentionally NOT allowlisted (allowlisting it
# would advertise a machine-ingest exemption for a route that only 404s).
_OTEL_INGEST_EXACT = frozenset({
    "/api/otel/v1/traces",
})
_SELF_AUTHENTICATING_EXACT = frozenset({
    "/api/music/triggers/fire",
})
_DASHBOARD_MCP_PREFIX = "/api/mcp-dashboard"


def _bearer_token(request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _legacy_webhook_ok(request) -> bool:
    token = settings.agd_webhook_token
    if not token:
        return True
    supplied = request.headers.get("x-agd-webhook-token", "").strip() or _bearer_token(request)
    return bool(supplied) and hmac.compare_digest(supplied, token)


def _dashboard_mcp_token_ok(request) -> bool:
    token = os.environ.get("DASHBOARD_MCP_TOKEN", "")
    supplied = _bearer_token(request)
    return bool(token and supplied) and hmac.compare_digest(supplied, token)


def _otel_token_ok(request) -> bool:
    """Validate the OTLP ingest token. Unset token = open (trusted-LAN only),
    same posture as the legacy webhooks. n8n sends it as an Authorization bearer
    via N8N_OTEL_EXPORTER_OTLP_HEADERS; an x-agd-otel-token header is also accepted."""
    token = settings.agd_otel_token
    if not token:
        return True
    supplied = request.headers.get("x-agd-otel-token", "").strip() or _bearer_token(request)
    return bool(supplied) and hmac.compare_digest(supplied, token)


@app.middleware("http")
async def require_internal_api_auth(request, call_next):
    """Require identity for internal API routes.

    Individual routers still enforce role checks, but this gives the app one
    easy-to-audit default: `/api/*` is private unless explicitly listed here or
    protected by its own machine token/API-key scheme.
    """
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in _PUBLIC_API_EXACT or any(path.startswith(p) for p in _PUBLIC_API_PREFIXES):
        return await call_next(request)
    if path in _LEGACY_WEBHOOK_EXACT:
        if _legacy_webhook_ok(request):
            return await call_next(request)
        return JSONResponse({"detail": "Invalid or missing webhook token"}, status_code=401)
    if path in _OTEL_INGEST_EXACT:
        if not settings.agd_otel_enabled:
            return JSONResponse({"detail": "OTel receiver disabled (set AGD_OTEL_ENABLED=true)"}, status_code=404)
        if _otel_token_ok(request):
            return await call_next(request)
        return JSONResponse({"detail": "Invalid or missing OTel token"}, status_code=401)
    if path in _SELF_AUTHENTICATING_EXACT:
        return await call_next(request)
    if path.startswith(_DASHBOARD_MCP_PREFIX) and _dashboard_mcp_token_ok(request):
        return await call_next(request)

    from backend.auth_gate import current_user, login_enforced

    if not login_enforced() and not settings.agd_require_auth:
        return await call_next(request)
    if await current_user(request) is not None:
        return await call_next(request)
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


@app.middleware("http")
async def limit_request_size(request, call_next):
    """Reject oversized bodies by Content-Length before reading them."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > settings.agd_max_request_bytes:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        except ValueError:
            pass
    return await call_next(request)


@app.middleware("http")
async def security_headers(request, call_next):
    """Baseline security response headers. CSP is opt-in via AGD_CSP."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Only assert HSTS over HTTPS (honoring an upstream TLS-terminating proxy).
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if settings.agd_csp:
        response.headers.setdefault("Content-Security-Policy", settings.agd_csp)
    return response


@app.middleware("http")
async def csrf_protect(request, call_next):
    """Double-submit CSRF check for cookie-authenticated mutations.

    Enforces only when the request is a cookie-authenticated browser mutation:
    a non-safe method, an internal `/api/` path (not the X-API-Key public API
    at `/api/v1/`), and an `agd_session` cookie present. Bearer-token / API-key
    callers and unauthenticated/edge-only requests are not cookie-CSRF exposed,
    so they are skipped. On mismatch: 403.
    """
    from backend.modules.auth.service import CSRF_COOKIE, CSRF_HEADER, SAFE_METHODS, SESSION_COOKIE

    path = request.url.path
    # Auth bootstrap endpoints establish (or recover) the session, so they cannot
    # be double-submit-CSRF gated: there is no valid session token yet, and a
    # stale/foreign agd_session cookie (e.g. left over after a data-volume wipe,
    # or bleeding across ports on localhost) must not block first-run setup.
    csrf_exempt = path in (
        "/api/auth/setup",
        "/api/auth/login",
        "/api/auth/login/totp",
        "/api/auth/forgot",
        "/api/auth/reset",
    ) or path in _OTEL_INGEST_EXACT  # machine-ingest, token-authed, not a browser surface
    if (
        request.method not in SAFE_METHODS
        and path.startswith("/api/")
        and not path.startswith("/api/v1/")
        and not csrf_exempt
        and request.cookies.get(SESSION_COOKIE)
    ):
        auth = request.headers.get("authorization", "")
        has_api_key = bool(request.headers.get("x-api-key"))
        if not auth.lower().startswith("bearer ") and not has_api_key:
            cookie_tok = request.cookies.get(CSRF_COOKIE, "")
            header_tok = request.headers.get(CSRF_HEADER, "")
            if not cookie_tok or cookie_tok != header_tok:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "CSRF check failed"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Force revalidation on frontend assets so browsers pick up new deploys immediately."""
    response = await call_next(request)
    path = request.url.path
    if path.endswith((".js", ".css", ".html")) or path == "/" or path.startswith("/js/") or path.startswith("/css/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ── API routes ───────────────────────────────────────────────────────────────


@app.get("/api/status")
async def status():
    """Dashboard status — used by frontend to check if setup is complete."""
    configured = is_setup_complete()
    config = load_config()
    active = get_active_instance()
    # Status endpoint is consumed by the browser. When an instance has a
    # browser-reachable `login_url` (used when `url` is a compose-internal
    # hostname the browser cannot resolve), surface that instead so
    # window.__n8nUrl and "Open in n8n" links use a real browser URL.
    public_url = ""
    if active:
        public_url = active.get("login_url") or active["url"]
    return {
        "configured": configured,
        "n8n_url": public_url,
        "active_instance": {
            "id": active["id"],
            "name": active["name"],
            "url": public_url,
            "color": active.get("color", ""),
        } if active else None,
        "theme": get_active_theme(),
        "version": APP_VERSION,
        "websocket_clients": manager.count,
        "health_endpoints": config.get("health_endpoints", []),
    }


@app.get("/api/health/docker-env")
async def docker_env():
    """Return whether the dashboard is running inside a Docker container.

    Detected by the presence of /.dockerenv, which Docker creates in every
    container's root filesystem. Used by the frontend to show a localhost hint
    on the Add Instance dialog.
    """
    import os
    in_docker = os.path.exists("/.dockerenv")
    return {"in_docker": in_docker}


# ── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Gate the WS upgrade the same way as the HTTP boundary: a valid local
    # session cookie OR an edge-authenticated identity. Browsers can't set an
    # Authorization header on a WS handshake, so token-only mode does not gate
    # /ws. When login is disabled (open install) the gate is skipped.
    from backend.auth_gate import edge_identity, login_enforced
    if login_enforced() or settings.agd_require_auth:
        from backend.modules.auth.service import SESSION_COOKIE, session_user
        raw = ws.cookies.get(SESSION_COOKIE)
        authed = bool(edge_identity(ws)) or bool(await session_user(raw))
        if not authed:
            await ws.close(code=1008)
            return
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # Keep alive; client can send pings
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── Register modules BEFORE static mount ─────────────────────────────────────

modules = register_modules(app)
logger.info("Registered %d modules: %s", len(modules), ", ".join(modules))

# ── Public API v1 sub-app — clean docs at /api/v1/docs ───────────────────────
# Mounted as a separate ASGI sub-app so /api/v1/docs is isolated from internal
# routes. Starlette strips the mount prefix before routing to the sub-app, so
# handlers in the v1 router use bare paths (/workflows, not /api/v1/workflows).
try:
    from backend.modules.public_api.router import router as _v1_router
    _v1_app = FastAPI(
        title="AgeniusDesk Public API",
        version="1.0.0",
        description=(
            "Versioned, X-API-Key authenticated REST surface for external integrations. "
            "Pass your key via the **X-API-Key** header. "
            "Create keys at POST /api/admin/api-keys."
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    _v1_app.include_router(_v1_router)
    app.mount("/api/v1", _v1_app)
    logger.info("Public API v1 sub-app mounted at /api/v1 (docs: /api/v1/docs)")
except Exception as _e:
    logger.warning("Public API v1 sub-app mount failed: %s", _e)

# Mount the dashboard's own MCP server. It's a streamable-HTTP Starlette app
# (FastMCP) that can't be registered as a regular APIRouter. The module's
# router (the /_meta health probe) is already mounted by register_modules.
try:
    from backend.modules.dashboard_mcp.server import mount_on as _mount_mcp
    _mount_mcp(app)
except Exception as e:
    logger.warning("Dashboard MCP mount failed: %s", e)

# ── Static files (frontend) — must be LAST ───────────────────────────────────

BUILD_ID = str(int(time.time()))
_IMPORT_RE = re.compile(r"""(from|import)\s+(['"])(\.\.?/[^'"?]+\.js)(['"])""")


def _bust_imports(js: str) -> str:
    """Append ?v=BUILD_ID to every relative .js import so module graph reloads."""
    return _IMPORT_RE.sub(rf"\1 \2\3?v={BUILD_ID}\4", js)


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index():
    """Serve index.html with cache-busting query on entry module."""
    path = FRONTEND_DIR / "index.html"
    if not path.exists():
        return HTMLResponse("<h1>frontend missing</h1>", status_code=500)
    html = path.read_text()
    html = html.replace('src="/js/app.js"', f'src="/js/app.js?v={BUILD_ID}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/js/{full_path:path}")
async def serve_js(full_path: str):
    """Serve JS modules with versioned imports so Safari can't reuse stale modules."""
    from fastapi.responses import Response
    js_root = (FRONTEND_DIR / "js").resolve()
    try:
        js_path = (js_root / full_path).resolve()
        js_path.relative_to(js_root)
    except ValueError:
        return Response(status_code=404)
    if not js_path.is_file() or js_path.suffix.lower() != ".js":
        return Response(status_code=404)
    content = _bust_imports(js_path.read_text(encoding="utf-8"))
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=False), name="frontend")
