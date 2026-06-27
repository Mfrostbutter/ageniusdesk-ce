"""Module manager API — list modules, surface nav, inspect/install/uninstall."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend import module_registry
from backend.auth_gate import current_user, require_trusted_request
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


class InspectPayload(BaseModel):
    repo: str  # 'owner/repo' or GitHub URL
    ref: str = "main"  # tag, branch, or commit SHA


class Consent(BaseModel):
    acknowledged: bool = False  # operator checked the HIGH-findings acknowledgement
    typed_id: str | None = None  # operator typed the module id (CRITICAL findings)


class InstallPayload(BaseModel):
    repo: str  # 'owner/repo' or GitHub URL
    ref: str = "main"  # tag, branch, or commit SHA
    resolved_sha: str | None = None  # the sha returned by /inspect (swapped-tag guard)
    consent: Consent = Consent()
    expected_id: str | None = None


@router.post("/inspect")
async def inspect_module(payload: InspectPayload):
    """Dry-run a community module: download, statically scan, and return the
    manifest + declared capabilities + scan report + resolved sha WITHOUT
    registering anything. The operator reviews this before consenting to install.

    Heuristic review, not a sandbox: a static scan of code that runs in-process
    cannot contain a determined author. The report documents its own limits.
    """
    try:
        return await installer.inspect(repo=payload.repo, ref=payload.ref)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/install")
async def install_module(payload: InstallPayload, request: Request):
    """Install a community module after inspection + consent.

    Requires the `resolved_sha` from /inspect (rejected if the ref drifted) and
    a consent block sufficient for the scan severity (CRITICAL -> typed id,
    HIGH -> acknowledgement); the gate is enforced server-side. The module is
    NOT mounted until the app restarts (`restart_required: true`): hot-importing
    a new router onto a live app is fragile, and restart is the production path.
    """
    user = await current_user(request)
    approved_by = (user or {}).get("username", "") if user else "anonymous"
    try:
        return await installer.install(
            repo=payload.repo,
            ref=payload.ref,
            expected_sha=payload.resolved_sha,
            consent=payload.consent.model_dump(),
            approved_by=approved_by,
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
