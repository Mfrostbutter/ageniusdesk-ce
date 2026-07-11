"""Async n8n REST API client with retry logic and webhook fallback."""

import asyncio
import logging
import os
import time
from typing import Any, Optional

import httpx

from backend.config import get_n8n_api_key, get_n8n_url

logger = logging.getLogger(__name__)

TIMEOUT = 30.0
MAX_RETRIES = 3

# Workflow id -> name lookup is fetched for every execution-list enrichment, which
# otherwise duplicates the 250-workflow pull the dashboard already makes on load.
# Cache it per base URL for a short window to cut that redundant round-trip.
_WF_NAME_TTL = 30.0  # seconds
_wf_name_cache: dict[str, tuple[float, dict[str, str]]] = {}


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def dockerize_url(url: str) -> str:
    """Rewrite a localhost n8n URL to host.docker.internal when this dashboard
    runs inside Docker.

    From inside a container `localhost` is the container itself, not the host
    where n8n is published, so a user who points the dashboard at
    `http://localhost:5678` gets a connection-refused. Docker Desktop (and Linux
    via the compose `extra_hosts: host.docker.internal:host-gateway`) routes that
    name back to the host, so this makes localhost "just work" for the backend
    while the browser keeps using the original URL (stored as login_url).
    Returns the URL unchanged when not containerized or not a localhost URL.
    """
    if not url or not _in_docker():
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        if (parts.hostname or "").lower() in _LOCAL_HOSTS:
            netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return url


def _verify() -> bool:
    """TLS cert verification flag. Default on; flip off only for self-signed
    LAN n8n via AGD_TLS_VERIFY=false. Pro-tier testers on public HTTPS n8n must
    keep this on.
    """
    val = os.environ.get("AGD_TLS_VERIFY", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def _headers() -> dict[str, str]:
    return {
        "X-N8N-API-KEY": get_n8n_api_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _base_url() -> str:
    return get_n8n_url().rstrip("/")


async def _get(path: str, params: Optional[dict] = None) -> dict | list:
    """GET with retry on 429 and graceful 404 handling."""
    url = _base_url() + path
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
                resp = await client.get(url, headers=_headers(), params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(2**attempt)
                    continue
                if resp.status_code == 404:
                    return {}
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("n8n GET %s failed: HTTP %s", path, e.response.status_code)
            return {}
        except httpx.RequestError as e:
            logger.error("n8n GET %s error: %s", path, e)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2**attempt)
            else:
                return {}
    return {}


async def _post(path: str, body: Optional[dict] = None) -> dict:
    """POST to n8n API."""
    url = _base_url() + path
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
        resp = await client.post(url, headers=_headers(), json=body or {})
        resp.raise_for_status()
        return resp.json()


async def _delete(path: str) -> dict:
    """DELETE to n8n API."""
    url = _base_url() + path
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
        resp = await client.delete(url, headers=_headers())
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}


async def delete_execution(execution_id: str) -> dict:
    """Delete a single execution from n8n by ID."""
    try:
        await _delete(f"/api/v1/executions/{execution_id}")
        return {"success": True}
    except Exception as e:
        logger.error("delete_execution %s failed: %s", execution_id, e)
        return {"success": False, "error": str(e)}


async def delete_executions_for_workflow(workflow_id: str) -> dict:
    """Delete all executions for a workflow by iterating the public API.

    n8n's public REST API (v1) has no bulk-delete endpoint — only the internal
    UI endpoint at /rest/executions/delete does, and it's not exposed via the
    API key auth path. Iterate DELETE /executions/{id} instead. Paginated via
    cursor so workflows with >250 executions also clear.
    """
    deleted = 0
    failed: list[str] = []
    cursor = ""
    while True:
        page = await list_executions(workflow_id=workflow_id, limit=250, cursor=cursor)
        items = page.get("executions", []) or []
        if not items:
            break
        for ex in items:
            exec_id = str(ex.get("id", ""))
            if not exec_id:
                continue
            result = await delete_execution(exec_id)
            if result.get("success"):
                deleted += 1
            else:
                failed.append(exec_id)
        cursor = page.get("next_cursor") or ""
        if not cursor:
            break
    return {"success": not failed, "deleted": deleted, "failed": failed}


def _extract_webhook_path(workflow: dict) -> Optional[str]:
    """Extract webhook path from a workflow's trigger node."""
    for node in workflow.get("nodes") or []:
        if "webhook" in node.get("type", "").lower():
            path = (node.get("parameters") or {}).get("path")
            if path:
                return path
    return None


def _detect_trigger_type(workflow: dict) -> str:
    """Detect trigger type from workflow nodes."""
    for node in workflow.get("nodes") or []:
        t = node.get("type", "")
        if "scheduleTrigger" in t or "cron" in t.lower():
            return "schedule"
        if "webhook" in t.lower():
            return "webhook"
        if "manualTrigger" in t:
            return "manual"
        if "errorTrigger" in t:
            return "error"
    return "unknown"


DASHBOARD_TRIGGER_NODE_NAME = "__dashboard_trigger"


def _find_dashboard_trigger(workflow: dict) -> Optional[dict]:
    """Return the dashboard-owned webhook node if it exists."""
    for node in workflow.get("nodes") or []:
        if node.get("name") == DASHBOARD_TRIGGER_NODE_NAME:
            return node
    return None


def _dashboard_trigger_url(workflow: dict) -> Optional[str]:
    node = _find_dashboard_trigger(workflow)
    if not node:
        return None
    path = (node.get("parameters") or {}).get("path")
    return f"{_base_url()}/webhook/{path}" if path else None


_NON_FUNCTIONAL_NODE_TYPES = ("stickynote",)


def _is_functional_node(node: dict) -> bool:
    """Filter out presentation-only nodes like sticky notes."""
    t = (node.get("type") or "").lower().replace("-", "")
    return not any(k in t for k in _NON_FUNCTIONAL_NODE_TYPES)


def _find_primary_downstream(workflow: dict) -> Optional[str]:
    """Find the node that the first existing trigger feeds into.

    Skips sticky notes. Falls back to first functional non-trigger node.
    """
    connections = workflow.get("connections") or {}
    nodes = workflow.get("nodes") or []
    trigger_names: list[str] = []
    for node in nodes:
        if not _is_functional_node(node):
            continue
        t = (node.get("type") or "").lower()
        if any(k in t for k in ("trigger", "cron")) and node.get("name") != DASHBOARD_TRIGGER_NODE_NAME:
            trigger_names.append(node.get("name", ""))
    for name in trigger_names:
        conn = connections.get(name) or {}
        main = conn.get("main") or []
        if main and main[0]:
            target = main[0][0].get("node")
            # Verify the target node is functional (not a sticky note)
            target_node = next((n for n in nodes if n.get("name") == target), None)
            if target_node and _is_functional_node(target_node):
                return target
    for node in nodes:
        if not _is_functional_node(node):
            continue
        t = (node.get("type") or "").lower()
        if not any(k in t for k in ("trigger", "cron")) and node.get("name") != DASHBOARD_TRIGGER_NODE_NAME:
            return node.get("name")
    return None


# ── Public API ───────────────────────────────────────────────────────────────


async def test_connection() -> dict[str, Any]:
    """Test the n8n connection using the active instance."""
    result = await _get("/api/v1/workflows", {"limit": 1})
    if isinstance(result, dict) and "data" in result:
        return {"connected": True, "message": "Connected to n8n"}
    return {"connected": False, "message": "Failed to connect to n8n"}


async def test_connection_with(url: str, api_key: str) -> dict[str, Any]:
    """Test a specific n8n connection (used before saving a new instance).

    Returns {"connected": bool, "error_class": str, "message": str}.
    error_class is one of: "ok", "dns", "auth", "notfound", "timeout", "generic".
    """
    from backend.config import decrypt_value
    from backend.net import UnsafeProbeURL, assert_safe_probe_url
    url = decrypt_value(url)
    api_key = decrypt_value(api_key)
    # SSRF floor for every connect path (create instance, setup wizard, test-creds):
    # block cloud-metadata / link-local / reserved so the error_class response can't
    # be used as a blind oracle. LAN / loopback stay allowed (n8n self-hosts there).
    try:
        assert_safe_probe_url(url)
    except UnsafeProbeURL as exc:
        return {"connected": False, "error_class": "blocked", "message": f"URL not allowed: {exc}"}
    headers = {"X-N8N-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.get(f"{url.rstrip('/')}/api/v1/workflows", headers=headers, params={"limit": 1})
            if resp.status_code == 200:
                return {"connected": True, "error_class": "ok", "message": "Connected to n8n"}
            if resp.status_code == 401:
                return {
                    "connected": False,
                    "error_class": "auth",
                    "message": "HTTP 401: n8n rejected the API key. Generate a fresh API key in n8n Settings -> API and try again.",
                }
            if resp.status_code == 403:
                return {
                    "connected": False,
                    "error_class": "generic",
                    "message": "HTTP 403: the URL is reachable but access is blocked. If this n8n URL is behind Cloudflare Access or another auth wall, use the direct internal n8n URL instead of the public browser URL.",
                }
            if resp.status_code == 404:
                return {
                    "connected": False,
                    "error_class": "notfound",
                    "message": "HTTP 404: this does not look like an n8n base URL. Use the root n8n URL, not /api/v1 or another subpath.",
                }
            return {"connected": False, "error_class": "generic", "message": f"HTTP {resp.status_code}"}
    except httpx.ConnectError:
        return {
            "connected": False,
            "error_class": "dns",
            "message": f"Cannot reach {url.rstrip('/')}: host not found or connection refused. "
                       "If running inside Docker, use the compose service name or LAN IP, not localhost.",
        }
    except httpx.TimeoutException:
        return {
            "connected": False,
            "error_class": "timeout",
            "message": f"Connection to {url.rstrip('/')} timed out after {TIMEOUT}s. "
                       "Check that n8n is running and the port is reachable.",
        }
    except Exception as e:
        return {"connected": False, "error_class": "generic", "message": str(e)}


# ── Fleet health: workflow health aggregated across ALL instances ─────────────


async def _instance_health(inst: dict, exec_limit: int = 50) -> dict[str, Any]:
    """Fetch one instance's workflow + recent-execution health directly from its
    own n8n API (not the active instance). Never raises: an unreachable or
    rejecting instance comes back with reachable=False and an error string."""
    from backend.config import decrypt_value

    out: dict[str, Any] = {
        "id": inst.get("id", ""),
        "name": inst.get("name", ""),
        "color": inst.get("color", ""),
        "login_url": inst.get("login_url", "") or inst.get("url", ""),
        "reachable": False,
        "error": "",
        "workflows_total": 0,
        "workflows_active": 0,
        "exec_total": 0,
        "exec_error": 0,
        "error_rate": 0,
        "unhealthy": [],
    }
    url = dockerize_url(decrypt_value(inst.get("url", ""))).rstrip("/")
    api_key = decrypt_value(inst.get("api_key", ""))
    headers = {"X-N8N-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            wf = await client.get(f"{url}/api/v1/workflows", headers=headers, params={"limit": 250})
            if wf.status_code != 200:
                out["error"] = "auth" if wf.status_code in (401, 403) else f"HTTP {wf.status_code}"
                return out
            wfs = (wf.json() or {}).get("data", []) or []
            out["reachable"] = True
            out["workflows_total"] = len(wfs)
            out["workflows_active"] = sum(1 for w in wfs if w.get("active"))
            names = {str(w.get("id", "")): w.get("name", "Unknown") for w in wfs}

            ex = await client.get(f"{url}/api/v1/executions", headers=headers, params={"limit": min(exec_limit, 250)})
            execs = (ex.json() or {}).get("data", []) or [] if ex.status_code == 200 else []
            out["exec_total"] = len(execs)
            err_by_wf: dict[str, int] = {}
            for e in execs:
                if e.get("status") == "error":
                    out["exec_error"] += 1
                    wid = str(e.get("workflowId", ""))
                    err_by_wf[wid] = err_by_wf.get(wid, 0) + 1
            out["error_rate"] = round(out["exec_error"] / out["exec_total"] * 100) if out["exec_total"] else 0
            out["unhealthy"] = sorted(
                ({"id": wid, "name": names.get(wid, wid), "errors": n} for wid, n in err_by_wf.items()),
                key=lambda x: -x["errors"],
            )[:10]
    except httpx.ConnectError:
        out["error"] = "unreachable"
    except httpx.TimeoutException:
        out["error"] = "timeout"
    except Exception as e:  # noqa: BLE001 - one bad instance must not sink the fleet view
        out["error"] = str(e)[:120]
    return out


async def fleet_health(exec_limit: int = 50) -> dict[str, Any]:
    """Fan out across every configured instance in parallel and roll up workflow
    health, so an operator sees the whole fleet (one client becomes ten) in one
    pane. Read-only aggregation; a degraded instance is shown, not fatal."""
    from backend.config import get_active_instance_id, get_instances

    instances = get_instances()
    active_id = get_active_instance_id()
    results = await asyncio.gather(*[_instance_health(i, exec_limit) for i in instances]) if instances else []
    for r in results:
        r["active"] = r["id"] == active_id

    totals = {
        "instances": len(results),
        "reachable": sum(1 for r in results if r["reachable"]),
        "workflows_total": sum(r["workflows_total"] for r in results),
        "workflows_active": sum(r["workflows_active"] for r in results),
        "exec_total": sum(r["exec_total"] for r in results),
        "exec_error": sum(r["exec_error"] for r in results),
    }
    totals["error_rate"] = round(totals["exec_error"] / totals["exec_total"] * 100) if totals["exec_total"] else 0
    return {"instances": results, "totals": totals}


async def list_workflows(
    active_only: bool = False,
    name_contains: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict[str, Any]:
    """List workflows with metadata."""
    params: dict = {"limit": min(limit, 250)}
    if active_only:
        params["active"] = "true"
    if cursor:
        params["cursor"] = cursor

    result = await _get("/api/v1/workflows", params)
    workflows = result.get("data", []) if isinstance(result, dict) else []
    next_cursor = (result.get("nextCursor") or "") if isinstance(result, dict) else ""

    if name_contains:
        needle = name_contains.lower()
        workflows = [w for w in workflows if needle in (w.get("name") or "").lower()]

    items = []
    for w in workflows:
        items.append({
            "id": w.get("id", ""),
            "name": w.get("name", "Unknown"),
            "active": w.get("active", False),
            "is_archived": bool(w.get("isArchived", False)),
            "trigger_type": _detect_trigger_type(w),
            "created_at": w.get("createdAt", ""),
            "updated_at": w.get("updatedAt", ""),
            "tags": [t.get("name", "") for t in (w.get("tags") or [])],
        })

    # Sort: active first, then alphabetical by name
    items.sort(key=lambda w: (not w["active"], w["name"].lower()))

    return {"workflows": items, "next_cursor": next_cursor}


async def get_workflow(workflow_id: str) -> dict[str, Any]:
    """Get full workflow details including nodes and webhook URL."""
    w = await _get(f"/api/v1/workflows/{workflow_id}")
    if not w:
        return {}

    nodes = w.get("nodes") or []
    webhook_path = _extract_webhook_path(w)

    dashboard_url = _dashboard_trigger_url(w)
    return {
        "id": w.get("id", ""),
        "name": w.get("name", "Unknown"),
        "active": w.get("active", False),
        "is_archived": bool(w.get("isArchived", False)),
        "trigger_type": _detect_trigger_type(w),
        "created_at": w.get("createdAt", ""),
        "updated_at": w.get("updatedAt", ""),
        "node_count": len(nodes),
        "node_types": list({n.get("type", "").split(".")[-1] for n in nodes}),
        "webhook_path": webhook_path,
        "webhook_url": f"{_base_url()}/webhook/{webhook_path}" if webhook_path else None,
        "dashboard_trigger_enabled": dashboard_url is not None,
        "dashboard_trigger_url": dashboard_url,
        "tags": [t.get("name", "") for t in (w.get("tags") or [])],
    }


async def _get_workflow_names() -> dict[str, str]:
    """Build a workflow_id -> name mapping for enriching execution data.

    Cached per base URL for _WF_NAME_TTL seconds so repeated execution-list
    calls do not each re-pull the full workflow set.
    """
    key = _base_url()
    hit = _wf_name_cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _WF_NAME_TTL:
        return hit[1]

    result = await _get("/api/v1/workflows", {"limit": 250})
    workflows = result.get("data", []) if isinstance(result, dict) else []
    names = {w.get("id", ""): w.get("name", "Unknown") for w in workflows}
    # Only cache a real result; an empty pull (transient error) should retry next time.
    if names:
        _wf_name_cache[key] = (time.monotonic(), names)
    return names


async def list_executions(
    workflow_id: str = "",
    status: str = "",
    limit: int = 20,
    cursor: str = "",
) -> dict[str, Any]:
    """List recent executions with status and timing."""
    params: dict = {"limit": min(limit, 250)}
    if workflow_id:
        params["workflowId"] = workflow_id
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor

    result = await _get("/api/v1/executions", params)
    executions = result.get("data", []) if isinstance(result, dict) else []
    next_cursor = (result.get("nextCursor") or "") if isinstance(result, dict) else ""

    # Enrich with workflow names
    wf_names = await _get_workflow_names()

    items = []
    for e in executions:
        wf_id = e.get("workflowId", "")
        items.append({
            "id": e.get("id", ""),
            "workflow_id": wf_id,
            "workflow_name": (e.get("workflowData") or {}).get("name") or wf_names.get(wf_id, "Unknown"),
            "status": e.get("status", "unknown"),
            "mode": e.get("mode", "unknown"),
            "started_at": e.get("startedAt", ""),
            "finished_at": e.get("stoppedAt") or e.get("finishedAt", ""),
        })

    return {"executions": items, "next_cursor": next_cursor}


async def get_execution(execution_id: str) -> dict[str, Any]:
    """Get execution details including error info and node results."""
    result = await _get(f"/api/v1/executions/{execution_id}?includeData=true")
    if not result:
        return {}

    # n8n v1 API returns the execution object directly (not wrapped in {"data": ...})
    e = result if isinstance(result, dict) else {}

    data = {
        "id": e.get("id", execution_id),
        "workflow_id": e.get("workflowId", ""),
        "workflow_name": (e.get("workflowData") or {}).get("name", "Unknown"),
        "status": e.get("status", "unknown"),
        "mode": e.get("mode", "unknown"),
        "started_at": e.get("startedAt", ""),
        "finished_at": e.get("stoppedAt") or e.get("finishedAt", ""),
    }

    # Extract error if present — execution data lives in e["data"]["resultData"]
    result_data = (e.get("data") or {}).get("resultData", {})
    if result_data.get("error"):
        err = result_data["error"]
        extra = err.get("extra") or {}
        data["error"] = {
            "message": str(err.get("message", err))[:500],
            "description": str(err.get("description", ""))[:500],
            "source_node": str(extra.get("sourceNodeName", ""))[:100],
            "destination_node": str(extra.get("destinationNodeName", ""))[:100],
        }

    # Node execution summary
    run_data = result_data.get("runData", {})
    if run_data:
        nodes = []
        for name, runs in run_data.items():
            node_info = {"name": name, "status": "unknown"}
            if runs:
                node_info["status"] = runs[-1].get("executionStatus", "unknown")
                if runs[-1].get("error"):
                    node_info["error"] = str(runs[-1]["error"].get("message", ""))[:200]
            nodes.append(node_info)
        data["nodes"] = nodes

    return data


async def get_execution_raw(execution_id: str) -> dict[str, Any]:
    """Raw execution payload including un-flattened run data (for cost enrichment).

    Unlike get_execution (which summarizes), this returns n8n's full object so the
    caller can read per-node token usage under data.resultData.runData. Targets
    the ACTIVE instance; use get_execution_raw_by_instance to reach another."""
    return await _get(f"/api/v1/executions/{execution_id}?includeData=true") or {}


async def get_execution_raw_for(inst: dict, execution_id: str) -> dict[str, Any]:
    """Raw execution payload from a SPECIFIC instance's API (not the active one).

    Mirrors _instance_health's direct per-instance call so cost/health enrichment
    can price/inspect a trace from whatever instance actually owns it, not only
    the one that happens to be active. Never raises: an unreachable / rejecting
    instance returns {}."""
    from backend.config import decrypt_value

    try:
        url = dockerize_url(decrypt_value(inst.get("url", ""))).rstrip("/")
        api_key = decrypt_value(inst.get("api_key", ""))
    except Exception:
        return {}
    if not url:
        return {}
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.get(
                f"{url}/api/v1/executions/{execution_id}?includeData=true", headers=headers
            )
        return (resp.json() or {}) if resp.status_code == 200 else {}
    except Exception:
        return {}


async def get_execution_raw_by_instance(execution_id: str, instance_id: str) -> dict[str, Any]:
    """Fetch raw run-data for a trace's OWNING instance.

    The active instance is the fast path (shared client). For any other configured
    instance, fetch directly with that instance's creds. An empty ``instance_id``
    (a degenerate/unattributed trace) defaults to the active instance; an
    unrecognized id returns {}."""
    from backend.config import get_active_instance_id, get_instances

    if not instance_id or instance_id == get_active_instance_id():
        return await get_execution_raw(execution_id)
    inst = next((i for i in get_instances() if i.get("id") == instance_id), None)
    if not inst:
        return {}
    return await get_execution_raw_for(inst, execution_id)


async def trigger_workflow(workflow_id: str, payload: Optional[dict] = None) -> dict[str, Any]:
    """Trigger a workflow via its dashboard-owned webhook node."""
    payload = payload or {}
    w = await _get(f"/api/v1/workflows/{workflow_id}")
    if not w:
        return {"success": False, "error": "Workflow not found"}

    node = _find_dashboard_trigger(w)
    if not node:
        return {
            "success": False,
            "error": (
                "This workflow doesn't have a Dashboard Trigger. "
                "Click 'Enable Dashboard Trigger' to add one."
            ),
        }

    webhook_path = (node.get("parameters") or {}).get("path")
    if not webhook_path:
        return {"success": False, "error": "Dashboard Trigger node is missing its path."}

    if not w.get("active"):
        return {
            "success": False,
            "error": "Workflow is inactive. Activate it before triggering.",
        }

    webhook_url = f"{_base_url()}/webhook/{webhook_path}"
    try:
        async with httpx.AsyncClient(timeout=60.0, verify=_verify()) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code >= 400:
                return {
                    "success": False,
                    "error": f"Webhook returned HTTP {resp.status_code}: {resp.text[:300]}",
                }
            return {
                "success": True,
                "method": "dashboard_webhook",
                "webhook_url": webhook_url,
                "status_code": resp.status_code,
                "response": resp.text[:500],
            }
    except httpx.TimeoutException:
        return {
            "success": True,
            "method": "dashboard_webhook (async)",
            "webhook_url": webhook_url,
            "note": "Webhook posted but n8n response timed out. The workflow is likely still running — check executions in a moment.",
        }
    except Exception as e:
        msg = str(e) or type(e).__name__
        return {"success": False, "error": f"Trigger failed: {msg}"}


async def _put(path: str, body: dict) -> dict:
    """PUT to n8n API."""
    url = _base_url() + path
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
        resp = await client.put(url, headers=_headers(), json=body or {})
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}


async def inject_dashboard_trigger(workflow_id: str) -> dict[str, Any]:
    """Add a dashboard-owned webhook trigger node to a workflow."""
    import uuid

    w = await _get(f"/api/v1/workflows/{workflow_id}")
    if not w:
        return {"success": False, "error": "Workflow not found"}

    if _find_dashboard_trigger(w):
        return {
            "success": True,
            "injected": False,
            "already_present": True,
            "webhook_url": _dashboard_trigger_url(w),
        }

    # Refuse injection on workflows that already have a (non-dashboard) webhook trigger.
    # n8n can't reliably register two webhook triggers for the same workflow, and such
    # workflows typically expect a specific payload that the dashboard can't provide.
    for node in w.get("nodes") or []:
        if node.get("name") == DASHBOARD_TRIGGER_NODE_NAME:
            continue
        if "webhook" in (node.get("type") or "").lower():
            return {
                "success": False,
                "error": (
                    "This workflow already has a webhook trigger. "
                    "n8n can't register a second webhook for the same workflow, "
                    "and webhook workflows typically require a specific payload. "
                    "Call the existing webhook URL directly to test."
                ),
            }

    nodes = list(w.get("nodes") or [])
    downstream = _find_primary_downstream(w)

    existing_paths = {
        (n.get("parameters") or {}).get("path")
        for n in nodes
        if "webhook" in (n.get("type") or "").lower()
    }
    webhook_path = f"dashboard/{workflow_id}"
    if webhook_path in existing_paths:
        webhook_path = f"dashboard/{workflow_id}/{uuid.uuid4().hex[:8]}"

    max_y = max((n.get("position", [0, 0])[1] for n in nodes), default=0) if nodes else 0
    new_node = {
        "id": str(uuid.uuid4()),
        "name": DASHBOARD_TRIGGER_NODE_NAME,
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [-200, max_y + 200],
        "webhookId": str(uuid.uuid4()),
        "parameters": {
            "httpMethod": "POST",
            "path": webhook_path,
            "responseMode": "onReceived",
            "options": {},
        },
    }

    connections = dict(w.get("connections") or {})
    if downstream:
        connections[DASHBOARD_TRIGGER_NODE_NAME] = {
            "main": [[{"node": downstream, "type": "main", "index": 0}]]
        }

    was_active = bool(w.get("active"))
    if was_active:
        try:
            await _post(f"/api/v1/workflows/{workflow_id}/deactivate")
        except Exception as e:
            logger.warning("deactivate before inject failed: %s", e)

    put_body = {
        "name": w.get("name") or "Workflow",
        "nodes": nodes + [new_node],
        "connections": connections,
        "settings": {k: v for k, v in (w.get("settings") or {}).items() if k in _ALLOWED_WF_SETTINGS},
    }
    try:
        await _put(f"/api/v1/workflows/{workflow_id}", put_body)
    except httpx.HTTPStatusError as e:
        if was_active:
            try:
                await _post(f"/api/v1/workflows/{workflow_id}/activate")
            except Exception:
                pass
        return {
            "success": False,
            "error": f"n8n rejected update: HTTP {e.response.status_code} {e.response.text[:300]}",
        }
    except Exception as e:
        if was_active:
            try:
                await _post(f"/api/v1/workflows/{workflow_id}/activate")
            except Exception:
                pass
        return {"success": False, "error": f"Failed to update workflow: {e}"}

    if was_active:
        try:
            await _post(f"/api/v1/workflows/{workflow_id}/activate")
        except Exception as e:
            logger.warning("reactivate after inject failed: %s", e)

    return {
        "success": True,
        "injected": True,
        "webhook_url": f"{_base_url()}/webhook/{webhook_path}",
        "downstream_node": downstream,
        "was_active": was_active,
    }


async def get_workflow_raw(workflow_id: str) -> dict[str, Any]:
    """Fetch the FULL raw workflow object (nodes with all parameters, e.g. a Code
    node's jsCode). Unlike get_workflow (a summary), this is what you edit + PUT
    back. Used by the Remediator agent. Returns {} if not found."""
    return await _get(f"/api/v1/workflows/{workflow_id}") or {}


# n8n's PUT /workflows schema rejects unknown keys in `settings`; a raw GET can
# carry extras (e.g. callerPolicy/timezone variants). Send only the allowed set.
_ALLOWED_WF_SETTINGS = {
    "saveExecutionProgress", "saveManualExecutions", "saveDataErrorExecution",
    "saveDataSuccessExecution", "executionTimeout", "errorWorkflow", "timezone",
    "executionOrder",
}


async def put_workflow_full(workflow_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """PUT a (possibly edited) raw workflow back to n8n. Sends only the writable
    fields n8n accepts (name/nodes/connections/settings); toggles active around
    the write and restores it. Returns {success, error?}."""
    was_active = bool(raw.get("active"))
    settings = {k: v for k, v in (raw.get("settings") or {}).items() if k in _ALLOWED_WF_SETTINGS}
    body = {
        "name": raw.get("name") or "Workflow",
        "nodes": raw.get("nodes") or [],
        "connections": raw.get("connections") or {},
        "settings": settings,
    }
    if was_active:
        try:
            await _post(f"/api/v1/workflows/{workflow_id}/deactivate")
        except Exception as e:
            logger.warning("deactivate before put failed: %s", e)
    try:
        await _put(f"/api/v1/workflows/{workflow_id}", body)
    except httpx.HTTPStatusError as e:
        if was_active:
            try:
                await _post(f"/api/v1/workflows/{workflow_id}/activate")
            except Exception:
                pass
        return {"success": False, "error": f"n8n rejected update: HTTP {e.response.status_code} {e.response.text[:300]}"}
    except Exception as e:
        if was_active:
            try:
                await _post(f"/api/v1/workflows/{workflow_id}/activate")
            except Exception:
                pass
        return {"success": False, "error": f"Failed to update workflow: {e}"}
    if was_active:
        try:
            await _post(f"/api/v1/workflows/{workflow_id}/activate")
        except Exception as e:
            logger.warning("reactivate after put failed: %s", e)
    return {"success": True, "was_active": was_active}


async def remove_dashboard_trigger(workflow_id: str) -> dict[str, Any]:
    """Remove the dashboard-owned webhook trigger node from a workflow."""
    w = await _get(f"/api/v1/workflows/{workflow_id}")
    if not w:
        return {"success": False, "error": "Workflow not found"}

    if not _find_dashboard_trigger(w):
        return {"success": True, "removed": False, "not_present": True}

    was_active = bool(w.get("active"))
    nodes = [
        n for n in (w.get("nodes") or [])
        if n.get("name") != DASHBOARD_TRIGGER_NODE_NAME
    ]
    connections = dict(w.get("connections") or {})
    connections.pop(DASHBOARD_TRIGGER_NODE_NAME, None)

    if was_active:
        try:
            await _post(f"/api/v1/workflows/{workflow_id}/deactivate")
        except Exception as e:
            logger.warning("deactivate before remove failed: %s", e)

    put_body = {
        "name": w.get("name") or "Workflow",
        "nodes": nodes,
        "connections": connections,
        "settings": {k: v for k, v in (w.get("settings") or {}).items() if k in _ALLOWED_WF_SETTINGS},
    }
    try:
        await _put(f"/api/v1/workflows/{workflow_id}", put_body)
    except httpx.HTTPStatusError as e:
        if was_active:
            try:
                await _post(f"/api/v1/workflows/{workflow_id}/activate")
            except Exception:
                pass
        return {
            "success": False,
            "error": f"n8n rejected update: HTTP {e.response.status_code} {e.response.text[:300]}",
        }
    except Exception as e:
        if was_active:
            try:
                await _post(f"/api/v1/workflows/{workflow_id}/activate")
            except Exception:
                pass
        return {"success": False, "error": f"Failed to update workflow: {e}"}

    if was_active:
        try:
            await _post(f"/api/v1/workflows/{workflow_id}/activate")
        except Exception as e:
            logger.warning("reactivate after remove failed: %s", e)

    return {"success": True, "removed": True, "was_active": was_active}


async def _ensure_tag(name: str) -> Optional[str]:
    """Look up or create a tag by name. Returns tag ID, or None on failure."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        listing = await _get("/api/v1/tags", {"limit": 250})
        existing = listing.get("data", []) if isinstance(listing, dict) else []
        for t in existing:
            if (t.get("name") or "").lower() == name.lower():
                return t.get("id")
        created = await _post("/api/v1/tags", {"name": name})
        return created.get("id")
    except httpx.HTTPStatusError as e:
        logger.warning("tag lookup/create failed for %r: HTTP %s", name, e.response.status_code)
        return None
    except Exception as e:
        logger.warning("tag lookup/create failed for %r: %s", name, e)
        return None


async def _apply_workflow_tags(workflow_id: str, tag_names: list[str]) -> list[str]:
    """Resolve tag names to IDs (creating as needed) and attach to a workflow.

    Returns the list of tag names that were successfully applied.
    """
    tag_ids: list[dict] = []
    applied: list[str] = []
    for name in tag_names:
        tid = await _ensure_tag(name)
        if tid:
            tag_ids.append({"id": tid})
            applied.append(name)
    if not tag_ids:
        return applied
    try:
        await _put(f"/api/v1/workflows/{workflow_id}/tags", tag_ids)
    except Exception as e:
        logger.warning("attach tags to workflow %s failed: %s", workflow_id, e)
        return []
    return applied


async def import_workflow(
    workflow_data: dict,
    name_override: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Import a workflow JSON into n8n via the REST API.

    Optionally overrides the workflow name and attaches tags (created if missing).
    """
    # n8n's Public API create schema is strict (additionalProperties: false): it
    # accepts ONLY name, nodes, connections, and settings, and 400s with
    # "request/body must NOT have additional properties" on anything else a full
    # export carries (id, active, tags, pinData, versionId, meta, staticData,
    # isArchived, triggerCount, ...). An allowlist is robust to any extra or future
    # field; a denylist silently breaks whenever n8n adds one. `active` and `tags`
    # are applied via separate endpoints after create.
    allowed = ("name", "nodes", "connections", "settings")
    clean = {k: workflow_data[k] for k in allowed if k in workflow_data}

    # `settings` is itself strict (additionalProperties:false): a full export carries
    # UI-only keys (e.g. timeSavedPerExecution) n8n's create API rejects with
    # "request/body/settings must NOT have additional properties". Keep only n8n's
    # allowed settings keys. n8n requires settings + connections present (empty ok).
    clean["settings"] = {k: v for k, v in (clean.get("settings") or {}).items() if k in _ALLOWED_WF_SETTINGS}
    clean.setdefault("connections", {})

    override = (name_override or "").strip()
    if override:
        clean["name"] = override
    clean.setdefault("name", "Imported workflow")

    try:
        result = await _post("/api/v1/workflows", clean)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        return {"success": False, "error": f"HTTP {e.response.status_code}: {body}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    wf_id = result.get("id", "")
    name = result.get("name", "Unknown")

    applied_tags: list[str] = []
    tag_warning: Optional[str] = None
    if wf_id and tags:
        applied_tags = await _apply_workflow_tags(wf_id, tags)
        missing = [t for t in tags if t not in applied_tags]
        if missing:
            tag_warning = f"Imported, but failed to attach tags: {', '.join(missing)}"

    response: dict[str, Any] = {
        "success": True,
        "workflow_id": wf_id,
        "name": name,
        "tags_applied": applied_tags,
    }
    if tag_warning:
        response["warning"] = tag_warning
    return response


async def export_workflow(workflow_id: str) -> dict[str, Any]:
    """Export a single workflow as its full JSON definition."""
    return await _get(f"/api/v1/workflows/{workflow_id}")


async def export_all_workflows(active_only: bool = False) -> list[dict]:
    """Export all workflows as full JSON definitions."""
    params: dict = {"limit": 250}
    if active_only:
        params["active"] = "true"

    result = await _get("/api/v1/workflows", params)
    workflows = result.get("data", []) if isinstance(result, dict) else []
    return workflows


async def export_all_workflows_for(inst: dict, active_only: bool = False) -> list[dict]:
    """Export all workflows from a SPECIFIC instance (not the active one).

    Mirrors ``_instance_health``: resolves the instance's own encrypted URL + key
    and talks to its n8n API directly, so scheduled backups can fan out across the
    whole fleet regardless of which instance is currently active. Paginates through
    n8n's cursor so instances with more than 250 workflows back up completely.
    """
    from backend.config import decrypt_value

    url = dockerize_url(decrypt_value(inst.get("url", ""))).rstrip("/")
    api_key = decrypt_value(inst.get("api_key", ""))
    headers = {"X-N8N-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    params: dict = {"limit": 250}
    if active_only:
        params["active"] = "true"

    workflows: list[dict] = []
    cursor = ""
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
        while True:
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            resp = await client.get(f"{url}/api/v1/workflows", headers=headers, params=q)
            resp.raise_for_status()
            body = resp.json() or {}
            workflows.extend(body.get("data", []) or [])
            cursor = body.get("nextCursor") or ""
            if not cursor:
                break
    return workflows


# ── n8n User Management ──────────────────────────────────────────────────────


async def list_n8n_users() -> list[dict]:
    """List all users on the active n8n instance."""
    result = await _get("/api/v1/users")
    users = result.get("data", []) if isinstance(result, dict) else []
    return [{
        "id": u.get("id", ""),
        "email": u.get("email", ""),
        "first_name": u.get("firstName", ""),
        "last_name": u.get("lastName", ""),
        "role": u.get("role", ""),
        "pending": u.get("isPending", False),
        "created_at": u.get("createdAt", ""),
    } for u in users]


async def invite_n8n_user(email: str, role: str = "global:member") -> dict[str, Any]:
    """Invite a user to the active n8n instance."""
    try:
        result = await _post("/api/v1/users", [{"email": email, "role": role}])
        # n8n returns array of created/invited users
        if isinstance(result, list) and result:
            user = result[0].get("user", result[0])
            return {"success": True, "user_id": user.get("id", ""), "email": email}
        if isinstance(result, dict) and result.get("data"):
            data = result["data"]
            if isinstance(data, list) and data:
                user = data[0].get("user", data[0])
                return {"success": True, "user_id": user.get("id", ""), "email": email}
        return {"success": True, "email": email}
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        return {"success": False, "error": f"HTTP {e.response.status_code}: {body}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def delete_n8n_user(user_id: str, transfer_to: str = "") -> dict[str, Any]:
    """Delete a user from the active n8n instance."""
    try:
        url = _base_url() + f"/api/v1/users/{user_id}"
        params = {}
        if transfer_to:
            params["transferId"] = transfer_to
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as c:
            resp = await c.delete(url, headers=_headers(), params=params)
            if resp.status_code in (200, 204):
                return {"success": True}
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def set_workflow_active(workflow_id: str, active: bool) -> dict[str, Any]:
    """Activate or deactivate a workflow."""
    action = "activate" if active else "deactivate"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.post(
                f"{_base_url()}/api/v1/workflows/{workflow_id}/{action}",
                headers=_headers(),
            )
            if resp.status_code in (200, 204):
                return {"success": True, "active": active}
            return {"success": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def delete_workflow(workflow_id: str) -> dict[str, Any]:
    """Hard-delete a workflow from n8n. Irreversible on the n8n side."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as c:
            resp = await c.delete(
                f"{_base_url()}/api/v1/workflows/{workflow_id}",
                headers=_headers(),
            )
            if resp.status_code in (200, 204):
                return {"success": True, "deleted": True}
            if resp.status_code == 404:
                return {"success": False, "error": "Workflow not found"}
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_archived_workflows() -> list[dict[str, Any]]:
    """Page through every workflow on the active instance, return only archived ones."""
    archived: list[dict[str, Any]] = []
    cursor = ""
    # n8n's Public API caps page size at 250; loop until nextCursor is empty.
    # Hard cap on iterations as a paranoia stop in case n8n ever returns a self-referencing cursor.
    for _ in range(200):
        params: dict = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        result = await _get("/api/v1/workflows", params)
        if not isinstance(result, dict):
            break
        for w in result.get("data") or []:
            if w.get("isArchived"):
                archived.append({"id": w.get("id", ""), "name": w.get("name", "Unknown")})
        cursor = result.get("nextCursor") or ""
        if not cursor:
            break
    return archived


async def delete_archived_workflows() -> dict[str, Any]:
    """Find every archived workflow on the active n8n instance and hard-delete it.

    Each delete is its own Public API call; partial failures don't abort the run.
    """
    archived = await list_archived_workflows()
    if not archived:
        return {"success": True, "deleted": 0, "failed": 0, "errors": [], "items": []}

    deleted: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for w in archived:
        wid = w["id"]
        if not wid:
            continue
        result = await delete_workflow(wid)
        if result.get("success"):
            deleted.append(w)
        else:
            errors.append({"id": wid, "name": w["name"], "error": str(result.get("error", "unknown"))})

    return {
        "success": True,
        "deleted": len(deleted),
        "failed": len(errors),
        "errors": errors,
        "items": deleted,
    }
