"""Successful-execution data-save coverage detection, per n8n instance.

Trace backfill can only rebuild an execution n8n actually saved run data for.
An instance or workflow configured with saveDataSuccessExecution=none silently
discards that data on every successful run, which makes those runs permanently
unrecoverable. This module detects that condition ahead of a recovery attempt.
"""

import logging
from typing import Any, Optional

import httpx

from backend.config import decrypt_value
from backend.modules.n8n_proxy import client
from backend.net import tls_verify_for_instance

logger = logging.getLogger(__name__)

# How many recent executions to scan for one with status=success to probe.
_PROBE_EXEC_LIMIT = 10


def _resolve(inst: dict) -> Optional[tuple[str, str]]:
    """Decrypted (url, api_key) for one instance, or None if unresolvable."""
    try:
        url = client.dockerize_url(decrypt_value(inst.get("url", ""))).rstrip("/")
        api_key = decrypt_value(inst.get("api_key", ""))
    except Exception:
        return None
    return (url, api_key) if url else None


async def _fetch_active_workflows(inst: dict) -> Optional[list[dict]]:
    """Active workflows for one instance. None means unreachable."""
    resolved = _resolve(inst)
    if not resolved:
        return None
    url, api_key = resolved
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=client.TIMEOUT, verify=tls_verify_for_instance(inst)) as c:
            resp = await c.get(f"{url}/api/v1/workflows", headers=headers, params={"limit": 250, "active": "true"})
        if resp.status_code != 200:
            return None
        return (resp.json() or {}).get("data", []) or []
    except Exception:
        return None


async def _fetch_recent_executions(inst: dict, limit: int = _PROBE_EXEC_LIMIT) -> list[dict]:
    """Most recent executions for one instance. Best-effort; [] on any failure."""
    resolved = _resolve(inst)
    if not resolved:
        return []
    url, api_key = resolved
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=client.TIMEOUT, verify=tls_verify_for_instance(inst)) as c:
            resp = await c.get(f"{url}/api/v1/executions", headers=headers, params={"limit": limit})
        if resp.status_code != 200:
            return []
        return (resp.json() or {}).get("data", []) or []
    except Exception:
        return []


def _workflows_missing_success_data(workflows: list[dict]) -> list[dict]:
    """Active workflows with settings.saveDataSuccessExecution explicitly 'none'."""
    flagged = []
    for w in workflows:
        setting = str((w.get("settings") or {}).get("saveDataSuccessExecution", "")).lower()
        if setting == "none":
            flagged.append({"id": w.get("id", ""), "name": w.get("name", "Unknown")})
    return flagged


async def check_data_save_coverage(inst: dict) -> dict[str, Any]:
    """Whether successful-execution run data is being retained for one instance.

    Detection, cheapest signal first:
      1. Workflow-level: any ACTIVE workflow with settings.saveDataSuccessExecution
         == "none" is a direct hit, read straight off the workflow list already
         returned by GET /api/v1/workflows -- no extra request.
      2. Instance-level default: the public API never exposes
         EXECUTIONS_DATA_SAVE_ON_SUCCESS directly, so when no workflow overrides
         it, probe one recent successful execution with includeData=true. Empty
         runData on a completed run means the instance discards it by default.

    Returns {"status", "affected_workflows", "reason"}. status is one of
    "ok" / "degraded" / "unknown". Never raises -- an unreachable instance (or
    one with nothing to probe) comes back "unknown", mirroring
    client.get_execution_raw_for.
    """
    workflows = await _fetch_active_workflows(inst)
    if workflows is None:
        return {"status": "unknown", "affected_workflows": [], "reason": "instance unreachable"}

    flagged = _workflows_missing_success_data(workflows)
    if flagged:
        return {
            "status": "degraded",
            "affected_workflows": flagged,
            "reason": f"{len(flagged)} workflow(s) set saveDataSuccessExecution=none",
        }

    executions = await _fetch_recent_executions(inst)
    success_exec = next((e for e in executions if e.get("status") == "success"), None)
    if not success_exec:
        return {"status": "unknown", "affected_workflows": [], "reason": "no recent successful execution to probe"}

    raw = await client.get_execution_raw_for(inst, str(success_exec.get("id", "")))
    run_data = ((raw.get("data") or {}).get("resultData") or {}).get("runData") or {}
    if run_data:
        return {"status": "ok", "affected_workflows": [], "reason": ""}
    return {
        "status": "degraded",
        "affected_workflows": [],
        "reason": "instance default discards successful run data",
    }
