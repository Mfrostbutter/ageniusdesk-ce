"""Scheduled-backup API routes.

Operator-gated: backups read workflow definitions (which can embed configuration)
and write to disk, so both the settings and the snapshot listing/download sit
behind the operator role, matching the export/backup endpoints in n8n_proxy.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.modules.backups import service
from backend.scheduler import scheduler

router = APIRouter(prefix="/api/backups", tags=["backups"])

_operator = Depends(require_role("operator"))

JOB_ID = "workflow-backup"


class BackupSettings(BaseModel):
    enabled: bool | None = None
    interval_hours: int | None = None
    retention: int | None = None
    active_only: bool | None = None


@router.get("/settings", dependencies=[_operator])
async def get_settings():
    st = service.get_settings()
    job = next((j for j in scheduler.status() if j["id"] == JOB_ID), None)
    return {"settings": st, "job": job}


@router.put("/settings", dependencies=[_operator])
async def update_settings(patch: BackupSettings):
    effective = service.save_settings(patch.model_dump(exclude_none=True))
    return {"settings": effective}


@router.get("", dependencies=[_operator])
async def list_backups():
    return {"instances": service.list_backups()}


@router.post("/run", dependencies=[_operator])
async def run_now():
    """Trigger a backup immediately and return its summary."""
    result = await scheduler.run_now(JOB_ID)
    return result


@router.get("/{instance_id}/{filename}", dependencies=[_operator])
async def download_backup(instance_id: str, filename: str):
    path = service.resolve_backup_path(instance_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, media_type="application/json", filename=filename)


@router.delete("/{instance_id}/{filename}", dependencies=[_operator])
async def delete_backup(instance_id: str, filename: str):
    if not service.delete_backup(instance_id, filename):
        raise HTTPException(status_code=404, detail="Backup not found")
    return {"deleted": True}
