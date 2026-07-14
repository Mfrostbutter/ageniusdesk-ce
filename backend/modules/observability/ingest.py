"""Decode OTLP trace requests into span rows and persist them.

Works on a parsed ``ExportTraceServiceRequest`` (protobuf or OTLP/JSON, decoded
in the router) so this module has no transport concerns. n8n emits two span
kinds: ``workflow.execute`` (root, carries workflow id/name/execution) and
``node.execute`` (children). Attribute keys are matched best-effort so the
mapping survives minor n8n naming changes; the full attribute set is stored
verbatim so nothing is lost while the schema settles.
"""

import asyncio
import json
import logging

from backend.config import get_instances, settings
from backend.websocket import manager

from . import storage

logger = logging.getLogger(__name__)

_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}

# Markers of UTF-8-decoded-as-cp1252 mojibake (e.g. an em-dash "—" arriving as
# "â€""). Some emitters double-encode text before exporting; we repair it on
# ingest so names render correctly.
_MOJIBAKE_MARKERS = ("Ã", "â€", "Â", "ð\x9f")


def _fix_mojibake(s: str) -> str:
    """Best-effort repair of double-encoded UTF-8 (cp1252 round-trip).

    Only attempts a repair when the string carries a known mojibake marker and
    the round-trip succeeds, so clean text is never touched.
    """
    if not s or not any(m in s for m in _MOJIBAKE_MARKERS):
        return s
    try:
        repaired = s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return repaired if repaired != s else s


def _anyvalue(v):
    """Convert an OTLP AnyValue to a plain Python value."""
    if v.HasField("string_value"):
        return _fix_mojibake(v.string_value)
    if v.HasField("bool_value"):
        return v.bool_value
    if v.HasField("int_value"):
        return v.int_value
    if v.HasField("double_value"):
        return v.double_value
    if v.HasField("array_value"):
        return [_anyvalue(x) for x in v.array_value.values]
    if v.HasField("kvlist_value"):
        return {kv.key: _anyvalue(kv.value) for kv in v.kvlist_value.values}
    if v.HasField("bytes_value"):
        return v.bytes_value.hex()
    return None


def _attrs(kvs) -> dict:
    return {kv.key: _anyvalue(kv.value) for kv in kvs}


def _pick(attrs: dict, *keys: str) -> str:
    for k in keys:
        val = attrs.get(k)
        if val not in (None, ""):
            return str(val)
    return ""


def _name_to_id() -> dict[str, str]:
    """Lowercased instance name -> id, for matching the deterministic stamp."""
    out: dict[str, str] = {}
    for inst in get_instances():
        name = (inst.get("name") or "").strip().lower()
        if name:
            out[name] = inst["id"]
    return out


def _map_instance(resource_attrs: dict, pins: dict[str, str], name_to_id: dict[str, str]) -> str:
    """Attribute a resource to a configured instance (deterministic stamp, then a
    learned hash pin, then a stable ``unknown-<hash>`` bucket).

    Delegates to ``instance_map.map_from_attrs``. It deliberately never falls back
    to the active instance: an observed trace belongs to its source instance, and
    stamping it with whoever is active corrupts cross-instance cost/health/counts.
    Unknown hashes are resolved and re-attributed after insert by
    ``instance_map.learn_unknowns``.
    """
    from . import instance_map

    return instance_map.map_from_attrs(resource_attrs, pins, name_to_id)


def parse_request(req, pins: dict[str, str] | None = None, name_to_id: dict[str, str] | None = None) -> list[dict]:
    """Flatten an ExportTraceServiceRequest into otel_spans row dicts.

    ``pins`` (resource_hash -> instance_id) and ``name_to_id`` are loaded once by
    the caller so the per-resource mapping stays synchronous and network-free.
    """
    pins = pins if pins is not None else {}
    name_to_id = name_to_id if name_to_id is not None else _name_to_id()
    rows: list[dict] = []
    for rs in req.resource_spans:
        resource_attrs = _attrs(rs.resource.attributes) if rs.resource else {}
        instance_id = _map_instance(resource_attrs, pins, name_to_id)
        res_prefixed = {f"resource.{k}": v for k, v in resource_attrs.items()}
        for ss in rs.scope_spans:
            for sp in ss.spans:
                sattrs = _attrs(sp.attributes)
                merged = {**sattrs, **res_prefixed}
                rows.append({
                    "trace_id": sp.trace_id.hex(),
                    "span_id": sp.span_id.hex(),
                    "parent_id": sp.parent_span_id.hex(),
                    "instance_id": instance_id,
                    "workflow_id": _pick(sattrs, "n8n.workflow.id", "workflow.id", "workflowId"),
                    "workflow_name": _pick(sattrs, "n8n.workflow.name", "workflow.name", "workflowName"),
                    "execution_id": _pick(sattrs, "n8n.execution.id", "execution.id", "executionId"),
                    "name": sp.name or "",
                    "kind": int(sp.kind),
                    "start_ns": int(sp.start_time_unix_nano),
                    "end_ns": int(sp.end_time_unix_nano),
                    "status": _STATUS.get(int(sp.status.code), "UNSET"),
                    "attributes_json": json.dumps(merged, default=str),
                })
    return rows


async def ingest_trace_request(req) -> int:
    """Persist a decoded OTLP trace request, prune, and broadcast. Returns inserted span count."""
    from . import instance_map

    pins = await instance_map.load_pins()
    name_to_id = _name_to_id()
    rows = parse_request(req, pins, name_to_id)
    if not rows:
        return 0
    inserted = await storage.insert_spans(rows)
    # Learn any still-unknown exporter hashes: probe the fleet for the execution,
    # pin hash->instance, and re-attribute this batch's unknown rows. One probe
    # per new hash ever; fire-and-forget so it never delays the ingest response.
    unknowns: dict[str, tuple[str, str]] = {}
    for r in rows:
        inst = r["instance_id"]
        if inst.startswith(instance_map.UNKNOWN_PREFIX) and r.get("execution_id"):
            rhash = inst[len(instance_map.UNKNOWN_PREFIX):]
            if rhash and rhash not in unknowns:
                unknowns[rhash] = (r["execution_id"], r.get("workflow_id", ""))
    if unknowns:
        asyncio.create_task(instance_map.learn_unknowns(unknowns))
    try:
        await storage.prune(settings.agd_otel_retention_hours, settings.agd_otel_max_spans)
    except Exception as e:
        logger.warning("otel prune failed: %s", e)
    try:
        trace_ids = sorted({r["trace_id"] for r in rows})
        await manager.broadcast("otel:trace", {"trace_ids": trace_ids, "spans": len(rows)})
    except Exception:
        pass
    # Eager silent-failure check: a batch carrying a workflow.execute root means
    # the execution has finished, so run health enrichment now (loud without
    # anyone opening the trace). Fire-and-forget: the run-data fetch is bounded
    # inside enrich_trace_health and must never block the ingest response.
    try:
        from . import health
        from . import cost as cost_mod
        completed = {r["trace_id"] for r in rows if r["name"] == "workflow.execute"}
        for tid in completed:
            asyncio.create_task(health.enrich_trace_health(tid))
            # price the run now too, so SPEND aggregates without anyone opening the trace
            asyncio.create_task(cost_mod.enrich_trace(tid))
    except Exception as e:  # noqa: BLE001 - scheduling is best-effort
        logger.debug("enrich schedule failed: %s", e)
    return inserted
