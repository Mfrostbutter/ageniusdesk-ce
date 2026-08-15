"""Synthesize otel_spans rows from n8n execution history.

Rebuilds a trace from ``GET /api/v1/executions/{id}?includeData=true`` when the
real exporter trace was lost (rejected export, retention, unwired instance).
Output rows match ``ingest.parse_request``'s contract so every downstream
consumer (storage, waterfall, enrichers) works unchanged.
Spec: docs/specs/2026-08-14-trace-backfill-from-execution-history.md.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import httpx

from backend.config import decrypt_value, get_instance_by_id, settings
from backend.modules.n8n_proxy import client as n8n_client
from backend.net import tls_verify_for_instance

from . import storage
from .ingest import _encode_attrs

logger = logging.getLogger(__name__)

_NS_PER_MS = 1_000_000

# Execution statuses eligible for backfill (finished runs with run-data).
_COMPLETED = {"success", "error", "crashed", "failed", "canceled"}

# Executions listed per page on the range walk.
_PAGE_SIZE = 100

# n8n executionStatus -> span status column value.
_STATUS = {"success": "OK", "error": "ERROR", "crashed": "ERROR", "failed": "ERROR"}


def _trace_id(instance_id: str, execution_id: str) -> str:
    """Deterministic backfill trace id, disjoint from real 128-bit exporter ids."""
    return hashlib.sha256(f"agd-backfill:{instance_id}:{execution_id}".encode()).hexdigest()[:32]


def _span_id(trace_id: str, node_name: str, run_index: int) -> str:
    """Deterministic span id within a backfill trace."""
    return hashlib.sha256(f"{trace_id}:{node_name}:{run_index}".encode()).hexdigest()[:16]


def _iso_to_ns(iso: str) -> int:
    """ISO-8601 timestamp to unix nanoseconds."""
    if not iso:
        return 0
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


def _iso_to_received(iso: str) -> str:
    """ISO-8601 timestamp to the SQLite datetime('now') format ingest writes."""
    if not iso:
        return ""
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _status(execution_status: str, error: object = None) -> str:
    """Map an n8n executionStatus (plus a per-node error) to the span status value."""
    if error:
        return "ERROR"
    return _STATUS.get(str(execution_status or "").lower(), "UNSET")


def _output_items(run: dict) -> int:
    """Total items across all main output branches of a node run."""
    branches = ((run.get("data") or {}).get("main")) or []
    return sum(len(b) for b in branches if b)


def _input_items(run: dict, run_data: dict) -> int:
    """Total items fed into a node run, resolved from its source (upstream) entries."""
    total = 0
    for src in run.get("source") or []:
        if not isinstance(src, dict):
            continue
        prev_runs = run_data.get(src.get("previousNode")) or []
        prev_run = int(src.get("previousNodeRun") or 0)
        out_idx = int(src.get("previousNodeOutput") or 0)
        if prev_run < len(prev_runs):
            branches = ((prev_runs[prev_run].get("data") or {}).get("main")) or []
            if out_idx < len(branches) and branches[out_idx]:
                total += len(branches[out_idx])
    return total


async def synthesize(execution_raw: dict, instance_id: str) -> list[dict]:
    """Convert a raw n8n execution payload into otel_spans row dicts.

    Pure and deterministic: no DB, no network. One workflow.execute root plus
    one node.execute child per runData node run. Ids are derived (the payload's
    tracingContext is ignored); every row is labeled origin='backfill' with
    received_at set to the execution's true start time.
    """
    execution_id = str(execution_raw.get("id") or "")
    trace_id = _trace_id(instance_id, execution_id)
    root_span_id = _span_id(trace_id, "workflow.execute", 0)

    wf = execution_raw.get("workflowData") or {}
    nodes_by_name = {n.get("name"): n for n in wf.get("nodes") or [] if isinstance(n, dict)}
    result_data = ((execution_raw.get("data") or {}).get("resultData")) or {}
    run_data = result_data.get("runData") or {}

    started_at = str(execution_raw.get("startedAt") or "")
    received_at = _iso_to_received(started_at)
    workflow_id = str(wf.get("id") or execution_raw.get("workflowId") or "")
    workflow_name = str(wf.get("name") or "")

    root_attrs: dict = {
        "n8n.workflow.id": workflow_id,
        "n8n.workflow.name": workflow_name,
        "n8n.workflow.version_id": str(execution_raw.get("workflowVersionId") or ""),
        "n8n.workflow.node_count": len(wf.get("nodes") or []),
        "n8n.execution.id": execution_id,
        "n8n.execution.mode": str(execution_raw.get("mode") or ""),
        "n8n.execution.status": str(execution_raw.get("status") or ""),
        "n8n.execution.is_retry": bool(execution_raw.get("retryOf")),
    }
    project_id = execution_raw.get("projectId") or wf.get("projectId")
    if project_id:
        root_attrs["n8n.project.id"] = str(project_id)

    rows: list[dict] = [{
        "trace_id": trace_id,
        "span_id": root_span_id,
        "parent_id": "",
        "instance_id": instance_id,
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "execution_id": execution_id,
        "name": "workflow.execute",
        "kind": 1,
        "start_ns": _iso_to_ns(started_at),
        "end_ns": _iso_to_ns(str(execution_raw.get("stoppedAt") or "")),
        "status": _status(str(execution_raw.get("status") or ""), result_data.get("error")),
        "attributes_json": _encode_attrs(root_attrs),
        "origin": "backfill",
        "received_at": received_at,
    }]

    for node_name, runs in run_data.items():
        node = nodes_by_name.get(node_name) or {}
        for run_index, run in enumerate(runs or []):
            if not isinstance(run, dict):
                continue
            start_ns = int(run.get("startTime") or 0) * _NS_PER_MS
            end_ns = start_ns + int(round(float(run.get("executionTime") or 0) * _NS_PER_MS))
            attrs = {
                "n8n.node.id": str(node.get("id") or ""),
                "n8n.node.name": node_name,
                "n8n.node.type": str(node.get("type") or ""),
                "n8n.node.type_version": node.get("typeVersion") or 0,
                "n8n.node.items.input": _input_items(run, run_data),
                "n8n.node.items.output": _output_items(run),
            }
            rows.append({
                "trace_id": trace_id,
                "span_id": _span_id(trace_id, node_name, run_index),
                "parent_id": root_span_id,
                "instance_id": instance_id,
                "workflow_id": "",
                "workflow_name": "",
                "execution_id": "",
                "name": "node.execute",
                "kind": 1,
                "start_ns": start_ns,
                "end_ns": end_ns,
                "status": _status(str(run.get("executionStatus") or ""), run.get("error")),
                "attributes_json": _encode_attrs(attrs),
                "origin": "backfill",
                "received_at": received_at,
            })
    return rows


def _parse_iso(iso: str) -> Optional[datetime]:
    """ISO-8601 string to an aware UTC datetime, None on empty/unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _enrich(trace_id: str, detect_health: bool) -> None:
    """Run the post-insert enrichers, mirroring ingest: cost always, health opt-out."""
    from . import cost, health

    try:
        await cost.enrich_trace(trace_id)
    except Exception as e:  # noqa: BLE001 - enrichment is best-effort
        logger.debug("backfill: cost enrich failed for %s: %s", trace_id, e)
    if detect_health:
        try:
            await health.enrich_trace_health(trace_id)
        except Exception as e:  # noqa: BLE001 - enrichment is best-effort
            logger.debug("backfill: health enrich failed for %s: %s", trace_id, e)


async def _backfill_one(execution_id: str, instance_id: str, detect_health: bool) -> tuple[str, int]:
    """Backfill a single execution. Returns (outcome, spans_inserted).

    Outcomes: 'backfilled', 'skipped_traced' (a real trace exists and always
    outranks a reconstruction), 'no_data' (unfetchable or no saved run-data).
    An existing backfill trace is re-synthesized; deterministic ids make that
    idempotent.
    """
    existing = await storage.trace_id_for_execution(execution_id)
    if existing and await storage.trace_has_real_spans(existing):
        return ("skipped_traced", 0)
    raw = await n8n_client.get_execution_raw_by_instance(execution_id, instance_id)
    if not raw:
        return ("no_data", 0)
    run_data = (((raw.get("data") or {}).get("resultData")) or {}).get("runData") or {}
    if not run_data:
        return ("no_data", 0)
    rows = await synthesize(raw, instance_id)
    inserted = await storage.insert_spans(rows)
    await _enrich(rows[0]["trace_id"], detect_health)
    return ("backfilled", inserted)


async def backfill_execution(execution_id: str, instance_id: str, detect_health: bool = True) -> int:
    """Rebuild the trace for one execution. Returns spans inserted (0 if skipped)."""
    _, inserted = await _backfill_one(str(execution_id), instance_id, detect_health)
    return inserted


async def _list_executions_page(inst: dict, cursor: str = "", limit: int = _PAGE_SIZE) -> tuple[list[dict], str]:
    """One page of executions from a SPECIFIC instance's API, newest first.

    Direct per-instance call (own credentials + TLS), mirroring
    client.get_execution_raw_for. Lives here rather than in n8n_proxy.client
    because client.list_executions targets only the active instance.
    Returns (items, next_cursor); ([], '') on any failure.
    """
    try:
        url = n8n_client.dockerize_url(decrypt_value(inst.get("url", ""))).rstrip("/")
        api_key = decrypt_value(inst.get("api_key", ""))
    except Exception:
        return ([], "")
    if not url:
        return ([], "")
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    params: dict = {"limit": min(int(limit), 250)}
    if cursor:
        params["cursor"] = cursor
    try:
        async with httpx.AsyncClient(timeout=n8n_client.TIMEOUT, verify=tls_verify_for_instance(inst)) as c:
            resp = await c.get(f"{url}/api/v1/executions", headers=headers, params=params)
        if resp.status_code != 200:
            return ([], "")
        body = resp.json() or {}
        return (body.get("data") or [], str(body.get("nextCursor") or ""))
    except Exception:
        return ([], "")


async def backfill_instance(
    instance_id: str,
    since: str = "",
    until: str = "",
    limit: int = 0,
    detect_health: bool = True,
    progress_cb: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> dict:
    """Rebuild traces for an instance's completed executions in [since, until].

    Pages the instance's execution list newest-first, refuses executions outside
    the retention window (reported, never silently skipped), skips executions
    already covered by a real trace, and fans out fetches under bounded
    concurrency. ``limit`` lowers the per-run cap but never raises it.
    ``progress_cb`` is awaited with a copy of the running summary after each
    execution completes.
    """
    summary = {
        "scanned": 0, "backfilled": 0, "spans": 0, "skipped_traced": 0,
        "outside_retention": 0, "no_data": 0, "errors": 0,
    }
    inst = get_instance_by_id(instance_id)
    if not inst:
        summary["errors"] = 1
        return summary

    cap = max(int(settings.agd_backfill_max_executions), 0) or 500
    if limit and limit > 0:
        cap = min(cap, int(limit))
    since_dt = _parse_iso(since)
    until_dt = _parse_iso(until)
    retention_floor: Optional[datetime] = None
    if settings.agd_otel_retention_hours and settings.agd_otel_retention_hours > 0:
        retention_floor = datetime.now(timezone.utc) - timedelta(hours=settings.agd_otel_retention_hours)

    # Walk pages newest-first, classifying up to the per-run cap.
    to_backfill: list[str] = []
    cursor = ""
    max_pages = (cap // _PAGE_SIZE) + 2
    for _ in range(max_pages):
        items, cursor = await _list_executions_page(inst, cursor, _PAGE_SIZE)
        if not items:
            break
        stop = False
        for e in items:
            started = _parse_iso(str(e.get("startedAt") or ""))
            if since_dt and started and started < since_dt:
                stop = True  # newest-first: everything after this is older
                break
            if str(e.get("status") or "").lower() not in _COMPLETED:
                continue
            if until_dt and started and started > until_dt:
                continue
            if since_dt and not started:
                continue
            if summary["scanned"] >= cap:
                stop = True
                break
            summary["scanned"] += 1
            if retention_floor and started and started < retention_floor:
                summary["outside_retention"] += 1
                continue
            exec_id = str(e.get("id") or "")
            existing = await storage.trace_id_for_execution(exec_id)
            if existing and await storage.trace_has_real_spans(existing):
                summary["skipped_traced"] += 1
                continue
            to_backfill.append(exec_id)
        if stop or not cursor:
            break

    # Bounded fan-out against the instance.
    sem = asyncio.Semaphore(max(int(settings.agd_backfill_concurrency), 1))

    async def _run(exec_id: str) -> None:
        async with sem:
            try:
                outcome, n = await _backfill_one(exec_id, instance_id, detect_health)
            except Exception as e:  # noqa: BLE001 - one failure never aborts the run
                logger.warning("backfill: execution %s failed: %s", exec_id, e)
                outcome, n = ("error", 0)
        if outcome == "backfilled":
            summary["backfilled"] += 1
            summary["spans"] += n
        elif outcome == "skipped_traced":
            summary["skipped_traced"] += 1
        elif outcome == "no_data":
            summary["no_data"] += 1
        else:
            summary["errors"] += 1
        if progress_cb:
            try:
                await progress_cb(dict(summary))
            except Exception as e:  # noqa: BLE001 - progress is best-effort
                logger.debug("backfill: progress callback failed: %s", e)

    if to_backfill:
        await asyncio.gather(*(_run(x) for x in to_backfill))
    return summary
