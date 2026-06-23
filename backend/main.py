"""AgeniusDesk — FastAPI application."""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
        from backend.config_overlay import apply_overlay_to_settings, load_config_overlay
        from backend.config import settings as _settings
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

    logger.info("AgeniusDesk started, database ready")
    # F2: warn when the in-app auth gate is off, so an operator on a naked-port
    # self-host knows privileged routes rely on an edge proxy for auth.
    try:
        from backend.config import settings as _settings
        if not _settings.agd_require_auth:
            logger.warning(
                "AGD_REQUIRE_AUTH is off: privileged API routes (admin, modules, n8n) "
                "rely on edge auth (e.g. Cloudflare Access). Set AGD_REQUIRE_AUTH=true "
                "to enforce in-app auth on a naked-port bind."
            )
    except Exception:
        pass
    if _mcp_sm is not None:
        async with _mcp_sm.run():
            yield
    else:
        yield
    # Shutdown
    await close_db()


app = FastAPI(title="AgeniusDesk", version="0.1.0", lifespan=lifespan)


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


@app.middleware("http")
async def limit_request_size(request, call_next):
    """Reject oversized bodies by Content-Length before reading them."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > settings.agd_max_request_bytes:
                from fastapi.responses import JSONResponse
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
        "version": "0.1.0",
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
    # When in-app auth is enforced, require an edge-authenticated identity on the
    # WS upgrade (Cloudflare Access / trusted proxy header). No-op by default.
    # Browsers can't set Authorization on a WS handshake, so token-only mode
    # does not gate /ws; edge auth does.
    if settings.agd_require_auth:
        from backend.auth_gate import edge_identity
        if not edge_identity(ws):
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

import re

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
    js_path = FRONTEND_DIR / "js" / full_path
    if not js_path.is_file():
        return Response(status_code=404)
    content = _bust_imports(js_path.read_text())
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=False), name="frontend")
