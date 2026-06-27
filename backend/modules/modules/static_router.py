"""Static file serving for community module frontend assets.

Community modules ship their frontend (HTML + JS) in a `static/` subdir. When
the frontend loads a community module view, it fetches from
/modules/{id}/static/{path}, which maps to /data/modules/{id}/static/{path} on
disk, guarding against path traversal. Keeping assets under `static/` separates
them from the module's Python code (see CONTRIBUTING in the modules repo).

Built-in modules are NOT served via this route — their frontend code is
bundled into the main frontend/ directory and loaded via the existing
static serve.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.module_registry import COMMUNITY_MODULES_DIR

router = APIRouter(tags=["modules-static"])


def _safe_resolve(module_id: str, path: str) -> Path:
    """Resolve /data/modules/{module_id}/static/{path}, rejecting traversal."""
    base = (COMMUNITY_MODULES_DIR.resolve() / module_id / "static")
    target = (base / path).resolve()
    # Ensure the resolved path is still inside the module's static directory.
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="path_traversal_blocked")
    return target


@router.get("/modules/{module_id}/static/{file_path:path}")
async def serve_module_static(module_id: str, file_path: str):
    target = _safe_resolve(module_id, file_path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not_found")
    return FileResponse(target)
