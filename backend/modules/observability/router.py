"""Observability module: OTLP/HTTP receiver + trace query API.

The ingest endpoint (`POST /api/otel/v1/traces`) is machine-ingest: it is
exempted from the session gate and token-checked in `main.py` (AGD_OTEL_TOKEN),
mirroring the legacy webhook pattern. The query endpoints are ordinary
session-authed `/api/*` routes consumed by the Observability view.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.config import get_active_instance, get_active_instance_id, get_instance_by_id, settings
from backend.modules.n8n_proxy.coverage import check_data_save_coverage
from backend.websocket import manager

from . import backfill, cost, health, ingest, pricing, storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/otel", tags=["observability"])

# One backfill at a time, process-wide.
_backfill_lock = asyncio.Lock()


@router.post("/v1/traces")
async def receive_traces(request: Request):
    """OTLP/HTTP traces receiver. Accepts protobuf (n8n default) or OTLP/JSON."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    body = await request.body()
    ctype = (request.headers.get("content-type") or "").lower()
    req = ExportTraceServiceRequest()
    try:
        if "json" in ctype:
            from google.protobuf.json_format import Parse
            Parse(body.decode("utf-8"), req, ignore_unknown_fields=True)
        else:
            req.ParseFromString(body)
    except Exception as e:
        return JSONResponse({"detail": f"Could not parse OTLP payload: {e}"}, status_code=400)

    try:
        await ingest.ingest_trace_request(req)
    except Exception as e:
        logger.exception("otel ingest failed: %s", e)
        return JSONResponse({"detail": "ingest failed"}, status_code=500)

    # OTLP success response (empty partialSuccess == fully accepted).
    return JSONResponse({"partialSuccess": {}}, status_code=200)


@router.get("/status")
async def otel_status():
    """Receiver state + current span volume, for the Observability view header.

    Reports the active instance's span count alongside the fleet-wide total. The
    traces list and metrics strip are scoped to the active instance, so a bare
    fleet-wide count reads as "data is arriving" on an instance that has never
    exported a span.
    """
    iid = get_active_instance_id()
    active = get_active_instance() or {}
    return {
        "enabled": settings.agd_otel_enabled,
        "token_set": bool(settings.agd_otel_token),
        "retention_hours": settings.agd_otel_retention_hours,
        "max_spans": settings.agd_otel_max_spans,
        "span_count": await storage.count_spans(),
        "instance_span_count": await storage.count_spans(iid),
        "instance_id": iid,
        "instance_name": active.get("name") or "",
    }


@router.get("/traces")
async def list_traces(limit: int = 50, instance_id: str = "", workflow_id: str = ""):
    """Recent traces (one per execution), scoped to an instance (default: active),
    optionally filtered to a single workflow."""
    iid = instance_id if instance_id else get_active_instance_id()
    limit = max(1, min(int(limit), 500))
    return {
        "traces": await storage.list_traces(iid, limit, workflow_id),
        "instance_id": iid,
        "workflow_id": workflow_id,
    }


@router.get("/metrics")
async def metrics(window_hours: int = 24, instance_id: str = "", workflow_id: str = ""):
    """Span-derived metrics strip for the active instance (optionally one workflow)."""
    iid = instance_id if instance_id else get_active_instance_id()
    window_hours = max(1, min(int(window_hours), 720))
    return await storage.metrics_summary(iid, window_hours, workflow_id)


async def _enrich(trace_id: str) -> None:
    """Best-effort lazy cost + health enrichment before returning a trace's spans."""
    try:
        await cost.enrich_trace(trace_id)
    except Exception as e:
        logger.debug("cost enrich skipped for %s: %s", trace_id, e)
    try:
        await health.enrich_trace_health(trace_id)
    except Exception as e:
        logger.debug("health enrich skipped for %s: %s", trace_id, e)


@router.get("/traces/{trace_id}")
async def trace_detail(trace_id: str):
    """All spans for one trace, ordered for the waterfall. Lazily priced."""
    await _enrich(trace_id)
    return {"trace_id": trace_id, "spans": await storage.get_trace(trace_id)}


@router.get("/by-execution/{execution_id}")
async def trace_by_execution(execution_id: str, instance_id: str = ""):
    """Resolve an n8n execution id to its trace + spans (for the per-execution popup).

    Execution ids collide across instances; scope to the given one (default active).
    """
    iid = instance_id if instance_id else get_active_instance_id()
    trace_id = await storage.trace_id_for_execution(execution_id, iid)
    if not trace_id:
        return {"execution_id": execution_id, "trace_id": "", "spans": []}
    await _enrich(trace_id)
    return {"execution_id": execution_id, "trace_id": trace_id, "spans": await storage.get_trace(trace_id)}


@router.get("/pricing")
async def pricing_status():
    """Price-book status: how many models from each layer and when last refreshed."""
    return pricing.status()


@router.post("/pricing/refresh", dependencies=[Depends(require_role("operator"))])
async def pricing_refresh():
    """Force a price-book refresh from OpenRouter's models API."""
    return await pricing.refresh(force=True)


@router.get("/backfill/preview")
async def backfill_preview(instance_id: str = "", since: str = "", until: str = ""):
    """Dry-run counts for a trace backfill over [since, until].

    One execution-list page walk, no run-data fetches. Repeats the data-save
    coverage warning (Decision 3) for the selected instance.
    """
    iid = instance_id if instance_id else get_active_instance_id()
    inst = get_instance_by_id(iid)
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found")

    cap = max(int(settings.agd_backfill_max_executions), 0) or 500
    since_dt = backfill._parse_iso(since)
    until_dt = backfill._parse_iso(until)
    retention_floor: Optional[datetime] = None
    if settings.agd_otel_retention_hours and settings.agd_otel_retention_hours > 0:
        retention_floor = datetime.now(timezone.utc) - timedelta(hours=settings.agd_otel_retention_hours)

    completed = already_traced = backfill_traced = outside_retention = 0
    cursor = ""
    max_pages = (cap // backfill._PAGE_SIZE) + 2
    for _ in range(max_pages):
        items, cursor = await backfill._list_executions_page(inst, cursor, backfill._PAGE_SIZE)
        if not items:
            break
        stop = False
        for e in items:
            started = backfill._parse_iso(str(e.get("startedAt") or ""))
            if since_dt and started and started < since_dt:
                stop = True  # newest-first: everything after this is older
                break
            if str(e.get("status") or "").lower() not in backfill._COMPLETED:
                continue
            if until_dt and started and started > until_dt:
                continue
            if since_dt and not started:
                continue
            if completed >= cap:
                stop = True
                break
            completed += 1
            if retention_floor and started and started < retention_floor:
                outside_retention += 1
                continue
            existing = await storage.trace_id_for_execution(str(e.get("id") or ""), iid)
            if existing:
                if await storage.trace_has_real_spans(existing):
                    already_traced += 1
                else:
                    backfill_traced += 1
        if stop or not cursor:
            break

    return {
        "instance_id": iid,
        "instance_name": inst.get("name") or "",
        "since": since,
        "until": until,
        "cap": cap,
        "retention_hours": settings.agd_otel_retention_hours,
        "completed": completed,
        "already_traced": already_traced,
        "backfill_traced": backfill_traced,
        "outside_retention": outside_retention,
        "rebuildable": max(0, completed - already_traced - outside_retention),
        "coverage": await check_data_save_coverage(inst),
    }


class _BackfillRunRequest(BaseModel):
    instance_id: str = ""
    since: str = ""
    until: str = ""
    limit: int = 0
    detect_health: bool = True


@router.post("/backfill/run", dependencies=[Depends(require_role("operator"))])
async def backfill_run(req: _BackfillRunRequest):
    """Rebuild traces from n8n execution history for one instance.

    Single-flight: a run already in progress answers 409. Progress is broadcast
    over the app WebSocket as backfill_progress (running summary per execution)
    and a final backfill_done.
    """
    iid = req.instance_id if req.instance_id else get_active_instance_id()
    if not get_instance_by_id(iid):
        raise HTTPException(status_code=404, detail="Instance not found")
    if _backfill_lock.locked():
        raise HTTPException(status_code=409, detail="A backfill is already running")

    async def _progress(running: dict) -> None:
        await manager.broadcast("backfill_progress", {"instance_id": iid, **running})

    async with _backfill_lock:
        summary = await backfill.backfill_instance(
            iid,
            since=req.since,
            until=req.until,
            limit=req.limit,
            detect_health=req.detect_health,
            progress_cb=_progress,
        )
    await manager.broadcast("backfill_done", {"instance_id": iid, "summary": summary})
    return {"instance_id": iid, "summary": summary}
