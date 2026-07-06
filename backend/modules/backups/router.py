"""Scheduled-backup API routes.

Operator-gated: backups read workflow definitions (which can embed configuration)
and write to disk, so both the settings and the snapshot listing/download sit
behind the operator role, matching the export/backup endpoints in n8n_proxy.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.modules.backups import remote as remote_sink
from backend.modules.backups import service
from backend.scheduler import scheduler

router = APIRouter(prefix="/api/backups", tags=["backups"])

_operator = Depends(require_role("operator"))

JOB_ID = "workflow-backup"


class RemoteSettings(BaseModel):
    enabled: bool | None = None
    bucket: str | None = None
    prefix: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    access_key_id_ref: str | None = None
    secret_access_key_ref: str | None = None
    mirror_retention: bool | None = None
    encrypt: bool | None = None


class BackupSettings(BaseModel):
    enabled: bool | None = None
    interval_hours: int | None = None
    retention: int | None = None
    active_only: bool | None = None
    remote: RemoteSettings | None = None


def _with_redacted_remote(st: dict) -> dict:
    """Never return resolved credential values; the $VAR ref names are safe."""
    out = dict(st)
    out["remote"] = remote_sink.redacted(st.get("remote"))
    return out


@router.get("/settings", dependencies=[_operator])
async def get_settings():
    st = service.get_settings()
    job = next((j for j in scheduler.status() if j["id"] == JOB_ID), None)
    return {"settings": _with_redacted_remote(st), "job": job}


@router.put("/settings", dependencies=[_operator])
async def update_settings(patch: BackupSettings):
    effective = service.save_settings(patch.model_dump(exclude_none=True))
    return {"settings": _with_redacted_remote(effective)}


@router.post("/test-remote", dependencies=[_operator])
async def test_remote(patch: RemoteSettings | None = None):
    """Validate the offsite destination with a put+delete probe. Tests the given
    overrides merged onto the saved config (without persisting), so an operator
    can check settings before saving. Credentials still resolve from the secret
    store via their $VAR refs."""
    cfg = dict(service.get_settings()["remote"])
    if patch is not None:
        cfg.update(patch.model_dump(exclude_none=True))
    return await remote_sink.test_remote(cfg)


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
