"""Community-module worker bootstrap (run by absolute path, never imported by the host).

    python /app/agd_module_worker/main.py            # serve the module
    python /app/agd_module_worker/main.py --selfcheck # prove the host is unreachable

Launched by path so sys.path[0] is THIS directory, which lets it import its
sibling `sandbox` module without the host source root on the path. It then scrubs
sys.path, blocks host imports, and serves the target module's FastAPI router
behind a proxy-secret check.

Environment (all injected by the host spawner in phase 2):
  AGD_MODULE_ID      module package name to import (e.g. "youtube-research")
  AGD_MODULE_PARENT  dir containing that package (e.g. ".../data/modules")
  AGD_HOST_ROOT      the host source root to exclude from sys.path
  AGD_PROXY_SECRET   shared secret the host sends as X-AGD-Proxy-Secret
  AGD_WORKER_BIND    "host:port" or "unix:/path/to.sock"

Phase 1: bootstrap + sandbox + health + proxy-secret. The host spawner and
reverse proxy land in phase 2.
"""

from __future__ import annotations

import importlib
import os
import sys

# Make the sibling `sandbox` module importable when launched by absolute path,
# without putting the host source root on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402  (must follow the sys.path setup above)


def apply_sandbox(module_parent: str, host_root: str) -> None:
    """Block host imports, then curate sys.path. Order matters: install the
    blocker first so it covers any import triggered while rebuilding the path."""
    sandbox.install_import_blocker()
    sys.path[:] = sandbox.curate_sys_path(list(sys.path), module_parent, host_root)


def build_app(module_id: str, module_parent: str, proxy_secret: str):
    """Build the worker's FastAPI app: the module router behind a proxy-secret
    gate, plus a health probe. Assumes apply_sandbox() already ran."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    if module_parent and module_parent not in sys.path:
        sys.path.insert(0, module_parent)
    mod = importlib.import_module(module_id)

    app = FastAPI(title=f"agd-module-worker:{module_id}", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def _require_proxy_secret(request: Request, call_next):
        # The host always sends the secret; a direct hit (another local process)
        # has no way to know it. Rejects before any module routing runs.
        if proxy_secret and request.headers.get("x-agd-proxy-secret") != proxy_secret:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/_worker/health")
    async def _health():
        return {"status": "ok", "module": module_id}

    if hasattr(mod, "router"):
        app.include_router(mod.router)
    return app


def run() -> None:
    module_id = os.environ["AGD_MODULE_ID"]
    module_parent = os.environ.get("AGD_MODULE_PARENT", "")
    host_root = os.environ.get("AGD_HOST_ROOT", "")
    proxy_secret = os.environ.get("AGD_PROXY_SECRET", "")
    bind = os.environ.get("AGD_WORKER_BIND", "127.0.0.1:0")

    apply_sandbox(module_parent, host_root)
    app = build_app(module_id, module_parent, proxy_secret)

    import uvicorn

    if bind.startswith("unix:"):
        uvicorn.run(app, uds=bind[len("unix:"):], log_level="warning")
    else:
        host, _, port = bind.rpartition(":")
        uvicorn.run(app, host=host or "127.0.0.1", port=int(port or "0"), log_level="warning")


def selfcheck() -> int:
    """Prove the host package is unreachable from a worker, the hard way.

    Deliberately puts the host source root ON sys.path (the worst case where
    `import backend` would otherwise succeed), installs only the blocker, and
    asserts the import is refused. Exit 0 = blocked (good), 1 = leaked.
    """
    host_root = os.environ.get("AGD_HOST_ROOT", "")
    if host_root and host_root not in sys.path:
        sys.path.insert(0, host_root)
    sandbox.install_import_blocker()
    try:
        importlib.import_module("backend")
    except ImportError:
        return 0
    sys.stderr.write("FAIL: host package 'backend' was importable inside the worker\n")
    return 1


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        raise SystemExit(selfcheck())
    run()
