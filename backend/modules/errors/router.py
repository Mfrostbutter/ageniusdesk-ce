"""Error collector API routes."""

import json
import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.auth_gate import require_trusted_request
from backend.config import get_active_instance_id, is_configured
from backend.modules.errors import collector
from backend.modules.n8n_proxy import client as n8n

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/errors", tags=["errors"])


class ErrorPayload(BaseModel):
    workflow_id: str = "unknown"
    workflow_name: str = "Unknown Workflow"
    execution_id: str = ""
    node_name: str = ""
    error_message: str = "Unknown error"
    error_type: str = "Error"


def _scope_instance(instance_id: str) -> str:
    """Resolve the instance filter used by list/count endpoints.

    `instance_id="active"` (default) scopes to the currently active n8n
    instance. `instance_id="all"` opts out for a cross-instance view.
    A concrete id is passed through verbatim.
    """
    if instance_id == "all":
        return ""
    if instance_id == "active" or not instance_id:
        return get_active_instance_id() or ""
    return instance_id


@router.post("/webhook")
async def receive_error(payload: ErrorPayload):
    """Receive an error from n8n's global error handler workflow.

    Tags the error with the currently active instance. A multi-instance beta
    tester should run one error handler per instance so switches produce the
    right attribution; a followup can extend the payload with instance_id.
    """
    data = payload.model_dump()
    data.setdefault("instance_id", get_active_instance_id() or "")
    error_id = await collector.store_error(data)

    return {"success": True, "error_id": error_id}


@router.get("")
async def list_errors(
    limit: int = 50,
    offset: int = 0,
    workflow_id: str = "",
    range: str = "",
    instance_id: str = "active",
):
    """List recent errors.

    `instance_id` defaults to "active" (scope to the active n8n instance).
    Pass "all" to see every stored error. Counts returned in the payload are
    scoped to the same instance filter so the UI badge matches the feed.

    Optional `range` param bounds the listing by occurrence time:
      - '24h' → last 24 hours
      - '7d'  → last 7 days
      - '30d' → last 30 days
      - ''    → no time filter
    """
    scope = _scope_instance(instance_id)
    errors = await collector.get_errors(limit, offset, workflow_id, range, scope)
    count_24h = await collector.get_error_count_24h(scope)
    payload: dict = {"errors": errors, "count_24h": count_24h, "instance_id": scope}
    if range:
        payload["range"] = range
        payload["count_range"] = await collector.get_error_count_range(range, scope)
    return payload


@router.get("/grouped")
async def list_errors_grouped(
    range: str = "",
    limit: int = 100,
    instance_id: str = "active",
):
    """Aggregate errors by workflow + node + error_type for the active instance.

    Each returned row represents a group of identical failures and carries a
    `count` plus the most recent occurrence as the sample. Same scope semantics
    as `GET /api/errors`.
    """
    scope = _scope_instance(instance_id)
    groups = await collector.get_errors_grouped(range, scope, limit)
    count_24h = await collector.get_error_count_24h(scope)
    payload: dict = {"groups": groups, "count_24h": count_24h, "instance_id": scope}
    if range:
        payload["range"] = range
        payload["count_range"] = await collector.get_error_count_range(range, scope)
    return payload


class _GroupClear(BaseModel):
    workflow_id: str
    node_name: str = ""
    error_type: str = "Error"
    purge_n8n: bool = False


@router.post("/clear-group")
async def clear_error_group(req: _GroupClear):
    """Delete every local row matching the group key on the active instance.

    Optional purge_n8n iterates n8n's delete-execution endpoint for each
    execution id in the group before removing the local rows.
    """
    active = get_active_instance_id() or ""

    if req.purge_n8n:
        rows = await collector.get_errors(
            limit=10000, workflow_id=req.workflow_id, instance_id=active,
        )
        purged = []
        for row in rows:
            if row.get("node_name", "") != req.node_name:
                continue
            if row.get("error_type", "") != req.error_type:
                continue
            exec_id = row.get("execution_id", "")
            if not exec_id:
                continue
            result = await n8n.delete_execution(exec_id)
            purged.append({"execution_id": exec_id, "success": result.get("success", False)})
    else:
        purged = []

    deleted = await collector.clear_errors(
        workflow_id=req.workflow_id,
        node_name=req.node_name,
        error_type=req.error_type,
        instance_id=active,
    )
    return {"deleted": deleted, "purged": purged, "instance_id": active}


@router.delete("/{execution_id}")
async def delete_single_error(execution_id: str, purge_n8n: bool = True):
    """Delete one error record by execution_id and optionally purge it from n8n.

    Scoped to the active instance because execution ids are not globally
    unique across instances.
    """
    active = get_active_instance_id() or ""
    deleted = await collector.clear_errors(
        workflow_id="", execution_id=execution_id, instance_id=active
    )
    result: dict = {"deleted": deleted}
    if purge_n8n and execution_id:
        result["n8n"] = await n8n.delete_execution(execution_id)
    return result


@router.post("/sync")
async def sync_errors_from_n8n(limit: int = 100):
    """Pull recent failed executions from n8n and backfill the error store.

    Tags every synced error with the active instance so future listings can
    filter cleanly.
    """
    active = get_active_instance_id() or ""
    exec_data = await n8n.list_executions(status="error", limit=limit)
    executions = exec_data.get("executions", [])

    synced = 0
    skipped = 0
    for ex in executions:
        exec_id = str(ex.get("id", ""))
        if await collector.execution_id_exists(exec_id, active):
            skipped += 1
            continue

        detail = await n8n.get_execution(exec_id)
        err = detail.get("error", {})

        node_name = err.get("source_node", "")
        error_message = err.get("message", "") or err.get("description", "") or "Unknown error"

        await collector.store_error({
            "instance_id": active,
            "workflow_id": detail.get("workflow_id", ex.get("workflow_id", "unknown")),
            "workflow_name": detail.get("workflow_name", ex.get("workflow_name", "Unknown Workflow")),
            "execution_id": exec_id,
            "node_name": node_name,
            "error_message": error_message,
            "error_type": "Error",
        })
        synced += 1
        logger.info("Synced execution %s from n8n (instance %s)", exec_id, active or "none")

    return {"synced": synced, "skipped": skipped, "instance_id": active}


@router.delete("")
async def clear_errors(
    before_date: str = "",
    workflow_id: str = "",
    purge_n8n: bool = False,
    instance_id: str = "active",
):
    """Clear errors from the local store. Optionally also purge executions from n8n.

    Scope behaves like list_errors: `instance_id="active"` (default), `"all"`,
    or a concrete id.
    """
    scope = _scope_instance(instance_id)
    deleted = await collector.clear_errors(before_date, workflow_id, instance_id=scope)
    result: dict = {"deleted": deleted, "instance_id": scope}
    if purge_n8n and workflow_id:
        n8n_result = await n8n.delete_executions_for_workflow(workflow_id)
        result["n8n"] = n8n_result
    return result


# ── Global error-handler workflow install ─────────────────────────────────────

_HANDLER_TEMPLATE = Path(__file__).resolve().parents[2] / "n8n_workflows" / "global-error-handler.json"
_HANDLER_NAME = "Global Error Handler → AgeniusDesk"


async def _find_handler() -> dict | None:
    """Return the existing global-error-handler workflow on the active instance,
    or None. Used to keep install idempotent (no duplicate workflows)."""
    try:
        res = await n8n.list_workflows(name_contains="Global Error Handler", limit=250)
    except Exception:
        return None
    for w in res.get("workflows", []):
        if "global error handler" in (w.get("name") or "").lower():
            return w
    return None


def _load_handler_template(dashboard_url: str = "") -> dict:
    """Load the global error-handler workflow JSON, pre-filling the dashboard
    webhook URL as the HTTP node's default. The `$env.FLOW_DASHBOARD_URL`
    fallback is preserved so an operator can still override it inside n8n (e.g.
    when n8n reaches the dashboard at a different address than the browser does).
    """
    wf = json.loads(_HANDLER_TEMPLATE.read_text(encoding="utf-8"))
    base = (dashboard_url or "").rstrip("/")
    if base:
        for node in wf.get("nodes", []):
            if node.get("type") == "n8n-nodes-base.httpRequest":
                params = node.setdefault("parameters", {})
                url = params.get("url", "")
                if "http://localhost:3000" in url:
                    params["url"] = url.replace("http://localhost:3000", base)
    return wf


class _InstallHandlerRequest(BaseModel):
    dashboard_url: str = ""
    activate: bool = True


@router.get("/handler-template")
async def handler_template(dashboard_url: str = ""):
    """Return the global error-handler workflow JSON (URL pre-filled). Backs the
    'Download workflow' affordance in the Error Handler settings tab."""
    return _load_handler_template(dashboard_url)


@router.get("/handler-status")
async def handler_status():
    """Whether the active instance already has the global error handler. Lets the
    UI skip the install prompt (and avoid a duplicate) when it's already there."""
    if not is_configured():
        return {"configured": False, "installed": False}
    existing = await _find_handler()
    if not existing:
        return {"configured": True, "installed": False}
    return {
        "configured": True,
        "installed": True,
        "active": bool(existing.get("active", False)),
        "workflow_id": existing.get("id", ""),
        "name": existing.get("name", _HANDLER_NAME),
    }


@router.post("/install-handler", dependencies=[Depends(require_trusted_request)])
async def install_handler(req: _InstallHandlerRequest):
    """One-click install: import the global error-handler workflow into the
    active n8n instance and (optionally) activate it. Idempotent — if a handler
    is already present it is reused (and activated) instead of importing a copy.

    n8n's public API cannot set a workflow as the instance-wide Error Workflow,
    so the caller still selects it once under n8n Settings -> Workflows ->
    Error Workflow. That single step is surfaced in the UI.
    """
    if not is_configured():
        raise HTTPException(status_code=503, detail="No n8n instance configured. Add one first.")

    # Idempotency: never create a second copy. Reuse an existing handler if found.
    existing = await _find_handler()
    if existing:
        wf_id = existing.get("id", "")
        activated = bool(existing.get("active", False))
        activation_error = ""
        if req.activate and not activated and wf_id:
            act = await n8n.set_workflow_active(wf_id, True)
            activated = bool(act.get("success"))
            if not activated:
                activation_error = act.get("error", "activation failed")
        return {
            "success": True,
            "workflow_id": wf_id,
            "name": existing.get("name", _HANDLER_NAME),
            "activated": activated,
            "activation_error": activation_error,
            "already_existed": True,
        }

    wf = _load_handler_template(req.dashboard_url)
    result = await n8n.import_workflow(wf)
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=f"Import failed: {result.get('error', 'unknown error')}")
    wf_id = result.get("workflow_id", "")
    activated = False
    activation_error = ""
    if req.activate and wf_id:
        act = await n8n.set_workflow_active(wf_id, True)
        if act.get("success"):
            activated = True
        else:
            activation_error = act.get("error", "activation failed")
    return {
        "success": True,
        "workflow_id": wf_id,
        "name": result.get("name", ""),
        "activated": activated,
        "activation_error": activation_error,
        "already_existed": False,
    }


# ── Auto-install on connect (targets a SPECIFIC instance, not the active one) ──

# n8n's Public API rejects these as read-only on POST /workflows (mirror of the
# n8n client's import shaping).
_HANDLER_READONLY = {
    "id", "active", "tags", "createdAt", "updatedAt", "versionId",
    "activeVersionId", "versionCounter", "triggerCount", "shared",
    "activeVersion", "staticData", "meta", "pinData",
}


def handler_dashboard_url(request: Request) -> str:
    """The address the error handler should POST errors to: one the n8n INSTANCE
    can reach the dashboard at (often a container, so not the browser URL).

    Precedence: AGD_PUBLIC_HOST (authoritative) -> the first AGD_HOST_ALIASES
    entry + the request's port (LAN address, the reason host aliases exist) ->
    the request origin (may be localhost; the workflow keeps an
    $env.FLOW_DASHBOARD_URL override either way).
    """
    from backend.config import settings

    scheme = "https" if request.url.scheme == "https" else "http"
    if settings.agd_public_host:
        return f"{scheme}://{settings.agd_public_host}"
    aliases = [a.strip() for a in (settings.agd_host_aliases or "").split(",") if a.strip()]
    if aliases:
        port = request.url.port
        return f"http://{aliases[0]}:{port}" if port else f"http://{aliases[0]}"
    return str(request.base_url).rstrip("/")


async def install_handler_into(inst: dict, dashboard_url: str = "", activate: bool = True) -> dict:
    """Best-effort, idempotent install of the global error handler into a SPECIFIC
    instance (used on connect; the active-instance client can't target it). Never
    raises. Returns {installed, already, activated, error}."""
    from backend.config import decrypt_value
    from backend.modules.n8n_proxy.client import TIMEOUT, _verify, dockerize_url

    out = {"installed": False, "already": False, "activated": False, "error": ""}
    base = dockerize_url(decrypt_value(inst.get("url", ""))).rstrip("/")
    key = decrypt_value(inst.get("api_key", ""))
    headers = {"X-N8N-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as c:
            lw = await c.get(f"{base}/api/v1/workflows", headers=headers, params={"limit": 250})
            if lw.status_code != 200:
                out["error"] = "auth" if lw.status_code in (401, 403) else f"HTTP {lw.status_code}"
                return out
            existing = next(
                (w for w in (lw.json() or {}).get("data", [])
                 if "global error handler" in (w.get("name") or "").lower()),
                None,
            )
            wf_id = ""
            if existing:
                out["already"] = True
                wf_id = existing.get("id", "")
                out["activated"] = bool(existing.get("active"))
            else:
                wf = _load_handler_template(dashboard_url)
                clean = {k: v for k, v in wf.items() if k not in _HANDLER_READONLY}
                clean.setdefault("settings", {})
                imp = await c.post(f"{base}/api/v1/workflows", headers=headers, json=clean)
                if imp.status_code not in (200, 201):
                    out["error"] = f"import HTTP {imp.status_code}: {imp.text[:120]}"
                    return out
                out["installed"] = True
                wf_id = (imp.json() or {}).get("id", "")
            if activate and wf_id and not out["activated"]:
                act = await c.post(f"{base}/api/v1/workflows/{wf_id}/activate", headers=headers)
                out["activated"] = act.status_code in (200, 201)
    except Exception as e:  # noqa: BLE001 - connect-time best effort, never fatal
        out["error"] = str(e)[:120]
    return out
