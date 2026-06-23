"""Module manager API — list modules, surface nav, install/uninstall community."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import module_registry
from backend.auth_gate import require_trusted_request
from backend.module_registry import APP_VERSION

from . import installer

router = APIRouter(prefix="/api/modules", tags=["modules"], dependencies=[Depends(require_trusted_request)])


# ── Read endpoints ───────────────────────────────────────────────────────────


@router.get("")
async def list_modules():
    """Return all registered modules with manifest + live status."""
    registry = module_registry.get_registry()
    return {
        "app_version": APP_VERSION,
        "count": len(registry),
        "modules": [entry.model_dump() for entry in registry.values()],
        "lock": installer.get_lock(),
    }


@router.get("/nav")
async def nav_entries():
    """Return nav entries from modules that declare one.

    Frontend appends these to the hardcoded built-in nav — primarily used
    for community modules, but built-ins also declare nav so the module
    manager UI can show what each module contributes.
    """
    entries = []
    for entry in module_registry.get_registry().values():
        if entry.status not in ("loaded", "missing_secrets"):
            continue
        fe = entry.manifest.frontend
        if not fe or not fe.nav:
            continue
        entries.append({
            "module_id": entry.manifest.id,
            "source": entry.source,
            "label": fe.nav.label,
            "icon": fe.nav.icon,
            "view": fe.nav.view,
            "static_base": f"/modules/{entry.manifest.id}/static/" if entry.source == "community" else None,
        })
    return {"entries": entries}


@router.get("/{module_id}")
async def get_module(module_id: str):
    entry = module_registry.get_registry().get(module_id)
    if not entry:
        raise HTTPException(status_code=404, detail="module_not_found")
    return entry.model_dump()


# ── Write endpoints (community modules only) ─────────────────────────────────


class InstallPayload(BaseModel):
    repo: str  # 'owner/repo' or GitHub URL
    ref: str = "main"  # tag, branch, or commit SHA
    expected_id: str | None = None


@router.post("/install")
async def install_module(payload: InstallPayload):
    """Install a community module from GitHub.

    The installed module is NOT mounted until the app restarts; the response
    includes `restart_required: true` so the UI can prompt for a reload.
    We deliberately do not hot-import at install time: mounting a new router
    on a live FastAPI app without a full restart is fragile, and the
    restart path is the one we exercise in production anyway.
    """
    try:
        return await installer.install(
            repo=payload.repo,
            ref=payload.ref,
            expected_id=payload.expected_id,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{module_id}")
async def uninstall_module(module_id: str):
    """Remove a community module. Built-in modules cannot be uninstalled."""
    entry = module_registry.get_registry().get(module_id)
    if entry and entry.source == "builtin":
        raise HTTPException(status_code=400, detail="cannot_uninstall_builtin")
    try:
        return installer.uninstall(module_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
