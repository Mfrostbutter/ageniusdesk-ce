"""Workflow promotion API — move workflows between registered n8n instances.

Community Edition: ungated. (The internal build gates this on a Pro license
feature; CE has no licensing, so promotion is always available.)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth_gate import require_role
from backend.modules.n8n_promote import promote as promote_svc

router = APIRouter(prefix="/api/promote", tags=["promote"], dependencies=[Depends(require_role("operator"))])


class PreflightRequest(BaseModel):
    source_instance_id: str
    target_instance_id: str
    workflow_ids: list[str] = Field(default_factory=list)


class AutoProvisionRequest(BaseModel):
    target_instance_id: str
    # each: {cred_type, source_id, name}
    credentials: list[dict] = Field(default_factory=list)
    # optional source_id -> secret_name overrides (resolves ambiguous rows)
    secret_choices: dict[str, str] = Field(default_factory=dict)


class PromoteRequest(BaseModel):
    source_instance_id: str
    target_instance_id: str
    workflow_ids: list[str] = Field(default_factory=list)
    # source-cred-id -> target-cred-id, and optional target display names.
    cred_map: dict[str, str] = Field(default_factory=dict)
    cred_names: dict[str, str] = Field(default_factory=dict)
    activate: bool = False
    name_suffix: str = ""
    dry_run: bool = False


@router.get("/workflows/{instance_id}")
async def list_workflows(instance_id: str):
    result = await promote_svc.list_instance_workflows(instance_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "instance not found"))
    return result


@router.post("/preflight")
async def preflight(req: PreflightRequest):
    if not req.workflow_ids:
        raise HTTPException(status_code=400, detail="No workflows selected.")
    result = await promote_svc.preflight(
        req.source_instance_id, req.target_instance_id, req.workflow_ids
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "preflight failed"))
    return result


@router.post("/auto-provision")
async def auto_provision(req: AutoProvisionRequest):
    if not req.credentials:
        raise HTTPException(status_code=400, detail="No credentials to provision.")
    result = await promote_svc.auto_provision_credentials(
        req.target_instance_id, req.credentials, req.secret_choices
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "auto-provision failed"))
    return result


@router.post("/run")
async def run(req: PromoteRequest):
    if not req.workflow_ids:
        raise HTTPException(status_code=400, detail="No workflows selected.")
    result = await promote_svc.promote(
        req.source_instance_id,
        req.target_instance_id,
        req.workflow_ids,
        cred_map=req.cred_map,
        cred_names=req.cred_names,
        activate=req.activate,
        name_suffix=req.name_suffix,
        dry_run=req.dry_run,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "promote failed"))
    return result
