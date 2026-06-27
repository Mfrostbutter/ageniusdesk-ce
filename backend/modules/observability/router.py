"""Observability module: OTLP/HTTP receiver + trace query API.

The ingest endpoint (`POST /api/otel/v1/traces`) is machine-ingest: it is
exempted from the session gate and token-checked in `main.py` (AGD_OTEL_TOKEN),
mirroring the legacy webhook pattern. The query endpoints are ordinary
session-authed `/api/*` routes consumed by the Observability view.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from backend.auth_gate import require_role
from backend.config import get_active_instance_id, settings

from . import cost, ingest, pricing, storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/otel", tags=["observability"])


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
    """Receiver state + current span volume, for the Observability view header."""
    return {
        "enabled": settings.agd_otel_enabled,
        "token_set": bool(settings.agd_otel_token),
        "retention_hours": settings.agd_otel_retention_hours,
        "max_spans": settings.agd_otel_max_spans,
        "span_count": await storage.count_spans(),
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
    """Best-effort lazy cost enrichment before returning a trace's spans."""
    try:
        await cost.enrich_trace(trace_id)
    except Exception as e:
        logger.debug("cost enrich skipped for %s: %s", trace_id, e)


@router.get("/traces/{trace_id}")
async def trace_detail(trace_id: str):
    """All spans for one trace, ordered for the waterfall. Lazily priced."""
    await _enrich(trace_id)
    return {"trace_id": trace_id, "spans": await storage.get_trace(trace_id)}


@router.get("/by-execution/{execution_id}")
async def trace_by_execution(execution_id: str):
    """Resolve an n8n execution id to its trace + spans (for the per-execution popup)."""
    trace_id = await storage.trace_id_for_execution(execution_id)
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
