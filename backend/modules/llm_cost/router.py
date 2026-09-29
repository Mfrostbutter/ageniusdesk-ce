"""LLM Cost API: state, sources, devices + push ingest, history, attribution, pricing, settings."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from backend import audit
from backend.auth_gate import require_role, require_trusted_request
from backend.modules.llm_cost import attributed, store
from backend.modules.llm_cost.hub import hub, slugify
from backend.modules.llm_cost.models import HEALTH_OK
from backend.modules.llm_cost.providers import TYPES, available_types, demo_enabled
from backend.modules.llm_cost.providers.base import ProviderError, resolve_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/llm-cost", tags=["llm-cost"])

_READ = [Depends(require_trusted_request), Depends(require_role("viewer"))]
_WRITE = [Depends(require_trusted_request), Depends(require_role("operator"))]

INSTALL_DIR = Path(__file__).resolve().parents[3] / "install"
FORWARDER_FILES = {
    "llm-cost-forward.py": "text/x-python",
    "llm-cost-forwarder.ps1": "text/plain",
    "llm-cost-install-task.ps1": "text/plain",
    "llm-cost-install-launchd.sh": "text/x-shellscript",
    "llm-cost-forwarder.plist.example": "application/xml",
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_REF_RE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)?$")


# ── helpers ────────────────────────────────────────────────────────────────


def _tz(tz_offset_min: int) -> int:
    return max(-14 * 3600, min(14 * 3600, int(tz_offset_min) * 60))


def _clean_options(source_type: str, options: dict) -> dict:
    """Keep declared fields only; secret fields must be $REF names, never raw keys."""
    cls = TYPES[source_type]
    fields = {f["key"]: f for f in cls.FIELDS}
    out: dict[str, Any] = {}
    for key, value in (options or {}).items():
        if key == "accent" and isinstance(value, str):
            out[key] = value[:9]
            continue
        field = fields.get(key)
        if field is None:
            continue
        kind = field.get("type")
        if kind == "secret":
            ref = (value or "").strip() if isinstance(value, str) else ""
            if ref and not _REF_RE.match(ref):
                raise HTTPException(status_code=400, detail=(
                    f"{field['label']}: store the key under Secrets and reference it as $NAME; "
                    "raw keys are never stored"))
            out[key] = ref
        elif kind == "number":
            try:
                out[key] = float(value) if value not in (None, "") else field.get("default", 0)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{field['label']} must be a number")
        elif kind == "bool":
            out[key] = bool(value)
        elif kind == "list":
            items = value.split(",") if isinstance(value, str) else (value or [])
            out[key] = [str(x).strip() for x in items if str(x).strip()][:50]
        elif kind == "json":
            if not isinstance(value, (list, dict)):
                raise HTTPException(status_code=400, detail=f"{field['label']} must be JSON")
            out[key] = value
        else:
            out[key] = str(value or "")[:500]
    for key, field in fields.items():
        if key not in out and "default" in field:
            out[key] = field["default"]
    return out


def _secret_status(source: dict) -> dict:
    cls = TYPES.get(source["type"])
    out = {}
    for field in (cls.FIELDS if cls else ()):
        if field.get("type") != "secret":
            continue
        ref = (source.get("options") or {}).get(field["key"]) or ""
        out[field["key"]] = {"ref": ref, "resolved": bool(ref and resolve_secret(ref))}
    return out


def _public_source(source: dict) -> dict:
    return {**source, "secrets": _secret_status(source)}


def _base_url(request: Request) -> str:
    from backend.config import settings

    return (settings.agd_public_url or str(request.base_url)).rstrip("/")


def install_commands(base: str, token: str, name: str) -> dict:
    dl = f"{base}/api/llm-cost/forwarder"
    return {
        "macos": (f"curl -fsSL {dl}/llm-cost-install-launchd.sh -o /tmp/llm-cost-install-launchd.sh && "
                  f"AGD_URL='{base}' AGD_LLM_COST_TOKEN='{token}' bash /tmp/llm-cost-install-launchd.sh"),
        "windows": (f"$env:AGD_LLM_COST_TOKEN='{token}'; "
                    f"iwr {dl}/llm-cost-install-task.ps1 -OutFile $env:TEMP\\llm-cost-install-task.ps1; "
                    f"powershell -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\\llm-cost-install-task.ps1 "
                    f"-Url '{base}'"),
        "manual": (f"curl -fsSL {dl}/llm-cost-forward.py -o llm-cost-forward.py && "
                   f"AGD_LLM_COST_TOKEN='{token}' python3 llm-cost-forward.py --url '{base}' --device '{name}'"),
    }


# ── models ─────────────────────────────────────────────────────────────────


class SourceCreate(BaseModel):
    type: str
    display_name: str = ""
    id: str = ""
    enabled: bool = True
    interval_sec: int = Field(0, ge=0, le=86400)
    options: dict = Field(default_factory=dict)


class SourceUpdate(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    interval_sec: Optional[int] = Field(None, ge=0, le=86400)
    options: Optional[dict] = None


class RefreshRequest(BaseModel):
    source_id: Optional[str] = None


class DeviceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    source_id: str = "claude-code"
    source_name: str = "Claude Code"


class OverrideRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=120)
    price_in: float = Field(..., ge=0, alias="in")
    price_out: float = Field(..., ge=0, alias="out")

    model_config = {"populate_by_name": True}


class SettingsRequest(BaseModel):
    quota_warn_pct: float = Field(75.0, ge=1, le=100)
    quota_critical_pct: float = Field(90.0, ge=1, le=100)
    spend_daily_warn: float = Field(0.0, ge=0)
    stale_after_sec: int = Field(1800, ge=60, le=7 * 86400)
    push_stale_sec: int = Field(180, ge=30, le=86400)
    notify: bool = True
    mqtt: dict = Field(default_factory=dict)


# ── state ──────────────────────────────────────────────────────────────────


@router.get("/state", dependencies=_READ)
async def get_state():
    return await hub.state()


@router.get("/card", dependencies=_READ)
async def get_card():
    """Compact spend + worst quota for a dashboard card."""
    state = await hub.state()
    ov = state["overview"]
    return {"spendToday": ov["spend"]["today"], "spendMtd": ov["spend"]["mtd"],
            "estimated": ov["spendEstimated"], "worstQuota": ov["worstQuota"], "alerts": len(ov["alerts"]),
            "topAlert": ov["alerts"][0] if ov["alerts"] else None, "sources": ov["sourceCount"],
            "healthy": ov["healthyCount"], "burnPerHour": ov["burnPerHour"], "currency": ov["currency"]}


@router.get("/alerts", dependencies=_READ)
async def get_alerts():
    settings = await store.get_settings()
    from backend.modules.llm_cost import alerts as alert_rules

    return {"alerts": alert_rules.evaluate(await hub.current_snapshots(settings), settings)}


@router.get("/events", dependencies=_READ)
async def get_events(limit: int = 50):
    return {"events": await store.recent_events(max(1, min(500, limit)))}


# ── sources ────────────────────────────────────────────────────────────────


@router.get("/types", dependencies=_READ)
async def get_types():
    return {"types": available_types()}


@router.get("/sources", dependencies=_READ)
async def get_sources():
    sources = await store.list_sources()
    return {"sources": [_public_source(s) for s in sources], "store": await store.stats(), "leader": hub.leader}


@router.post("/sources", dependencies=_WRITE)
async def create_source(body: SourceCreate):
    if body.type not in TYPES or (TYPES[body.type].DEV_ONLY and not demo_enabled()):
        raise HTTPException(status_code=400, detail=f"Unknown source type '{body.type}'")
    name = (body.display_name or TYPES[body.type].DISPLAY_NAME).strip()[:60]
    source_id = (body.id or slugify(name)).strip().lower()
    if not _ID_RE.match(source_id):
        raise HTTPException(status_code=400, detail="id must be lowercase letters, digits, and dashes")
    if await store.get_source(source_id):
        raise HTTPException(status_code=409, detail=f"Source '{source_id}' already exists")
    options = _clean_options(body.type, body.options)
    created = await store.create_source(source_id, body.type, name, options, body.enabled, body.interval_sec)
    audit.record("llm_cost.source.create", source_id=source_id, source_type=body.type)
    await _request_refresh(source_id)
    return _public_source(created)


@router.put("/sources/{source_id}", dependencies=_WRITE)
async def update_source(source_id: str, body: SourceUpdate):
    current = await store.get_source(source_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Source not found")
    options = _clean_options(current["type"], body.options) if body.options is not None else None
    updated = await store.update_source(source_id, display_name=(body.display_name or "").strip()[:60] or None,
                                        enabled=body.enabled, interval_sec=body.interval_sec, options=options)
    audit.record("llm_cost.source.update", source_id=source_id)
    await _request_refresh(source_id)
    return _public_source(updated)


@router.delete("/sources/{source_id}", dependencies=_WRITE)
async def delete_source(source_id: str):
    if not await store.delete_source(source_id):
        raise HTTPException(status_code=404, detail="Source not found")
    audit.record("llm_cost.source.delete", source_id=source_id)
    return {"deleted": source_id}


@router.post("/sources/{source_id}/test", dependencies=_WRITE)
async def test_source(source_id: str):
    source = await store.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source["type"] == "push":
        return {"ok": True, "detail": "push sources are fed by forwarders; nothing to test"}
    return await hub.test_source(source)


@router.get("/sources/{source_id}/detail", dependencies=_READ)
async def source_detail(source_id: str):
    source = await store.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    snaps = await hub.current_snapshots(sources=[source])
    snap = snaps[0]
    out = snap.as_dict(include_detail=True)
    if not snap.detail:
        row = (await store.load_snapshots()).get(source_id) or {}
        good = row.get("last_good")
        if good is not None and good.detail:
            out["detail"] = dict(good.detail)
            out["detailStale"] = True
    out["source"] = _public_source(source)
    return out


async def _request_refresh(source_id: Optional[str]) -> None:
    hub.request_refresh(source_id)
    await store.set_flag("refresh", {"at": int(time.time()), "source": source_id})


@router.post("/refresh", dependencies=_WRITE)
async def refresh(body: RefreshRequest):
    await _request_refresh(body.source_id)
    return {"requested": body.source_id or "*", "leader": hub.leader}


# ── history ────────────────────────────────────────────────────────────────


@router.get("/history", dependencies=_READ)
async def history(source_id: str, scope: str = "", label: str = "", window: str = "", days: int = 7):
    if not scope and not window:
        raise HTTPException(status_code=400, detail="scope or window is required")
    since = int(time.time()) - max(1, min(400, days)) * 86400
    out: dict[str, Any] = {"sourceId": source_id}
    if scope:
        out["quota"] = await store.quota_series(source_id, scope, since, label)
    if window:
        out["spend"] = await store.spend_series(source_id, window, since)
    return out


@router.get("/heatmap", dependencies=_READ)
async def heatmap(source_id: str = "", scope: str = "", label: str = "", days: int = 140, tz_offset_min: int = 0):
    candidates = await store.heat_candidates()
    if not source_id and candidates:
        source_id, scope, label = candidates[0]["sourceId"], candidates[0]["scope"], candidates[0]["label"]
    peaks = await store.daily_peaks(source_id, scope, label, max(7, min(400, days)), _tz(tz_offset_min)) \
        if source_id and scope else []
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + _tz(tz_offset_min)))
    return {"sourceId": source_id, "scope": scope, "label": label, "days": peaks, "endDay": today,
            "candidates": candidates}


@router.get("/spend/daily", dependencies=_READ)
async def spend_daily(days: int = 30, window: str = "today", tz_offset_min: int = 0):
    sources = await store.list_sources()
    ids = [s["id"] for s in sources]
    tz = _tz(tz_offset_min)
    rows = await store.daily_spend(ids, window, max(1, min(400, days)), tz)
    totals: dict[str, dict] = {}
    for r in rows:
        t = totals.setdefault(r["day"], {"day": r["day"], "amount": 0.0, "estimated": False})
        t["amount"] = round(t["amount"] + float(r["amount"] or 0.0), 6)
        t["estimated"] = t["estimated"] or r["estimated"]
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + tz))
    return {"window": window, "days": days, "endDay": today, "series": list(totals.values()), "bySource": rows}


# ── attribution ────────────────────────────────────────────────────────────


@router.get("/attributed", dependencies=_READ)
async def get_attributed(days: int = 30, tz_offset_min: int = 0):
    from backend.config import get_instances

    workspace = "all"
    days = max(1, min(90, days))
    tz = _tz(tz_offset_min)
    otel = await attributed.otel_rows(days, None, tz)
    names = {i["id"]: i.get("name") or i["id"] for i in get_instances()}
    out: dict[str, Any] = {"workspace": workspace, "days": days, "otel": {
        "available": otel["available"], "reason": otel.get("reason"),
        **attributed.summarize_otel(otel["rows"], names)}}
    lg = await attributed.langgraph_rows(days)
    ag = await attributed.agent_session_rows(days)
    out["internal"] = {"langgraph": lg, "agentSessions": ag}
    if workspace == "all":
        sources = await store.list_sources()
        rows = await store.load_snapshots()
        snaps = {sid: (r.get("current") if r.get("current") and r["current"].health == HEALTH_OK
                       else r.get("last_good")) for sid, r in rows.items()}
        history = await store.daily_spend([s["id"] for s in sources], "today", days, tz)
        billed = attributed.billed_by_provider_day(sources, snaps, history, days, tz)
        out["reconciliation"] = attributed.reconcile(billed, otel["rows"] + lg["rows"])
        out["reconciliationNote"] = "Provider per model is inferred from the model id (vendor/model = OpenRouter)."
    else:
        out["reconciliation"] = []
        out["reconciliationNote"] = "Billed spend is org-wide; reconciliation shows only in the fleet-wide scope."
    return out


# ── devices + push ingest ──────────────────────────────────────────────────


@router.get("/devices", dependencies=_READ)
async def get_devices():
    return {"devices": await store.list_devices()}


@router.post("/devices", dependencies=_WRITE)
async def create_device(body: DeviceCreate, request: Request):
    source_id = slugify(body.source_id)
    source = await store.get_source(source_id)
    if source is None:
        source = await store.create_source(source_id, "push", (body.source_name or "Claude Code")[:60], {})
    elif source["type"] != "push":
        raise HTTPException(status_code=400, detail=f"Source '{source_id}' is not a push source")
    device, token = await store.create_device(body.name.strip(), source_id)
    audit.record("llm_cost.device.create", device_id=device["id"], source_id=source_id)
    return {"device": device, "token": token, "install": install_commands(_base_url(request), token, device["name"]),
            "note": "The token is shown once. Only its hash is stored."}


@router.delete("/devices/{device_id}", dependencies=_WRITE)
async def revoke_device(device_id: str):
    if not await store.revoke_device(device_id):
        raise HTTPException(status_code=404, detail="Device not found or already revoked")
    audit.record("llm_cost.device.revoke", device_id=device_id)
    return {"revoked": device_id}


@router.post("/ingest")
async def ingest(request: Request):
    """Forwarder push. Auth = device token only, independent of the dashboard auth gate."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    device = await store.device_for_token(token) if token else None
    if device is None:
        raise HTTPException(status_code=401, detail="Bad or revoked device token")
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        import json

        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    try:
        result = await hub.ingest(device, body)
    except ProviderError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not result["accepted"]:
        raise HTTPException(status_code=422, detail="; ".join(result["rejected"]) or "nothing accepted")
    return result


@router.get("/forwarder/{name}")
async def forwarder_file(name: str):
    """Forwarder scripts for workstation install (public repo files, no secrets)."""
    if name not in FORWARDER_FILES:
        raise HTTPException(status_code=404, detail="Unknown file")
    path = INSTALL_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing from this install")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type=FORWARDER_FILES[name])


# ── pricing ────────────────────────────────────────────────────────────────


@router.get("/pricing", dependencies=_READ)
async def pricing_status():
    from backend import pricing

    return pricing.status()


@router.get("/pricing/lookup", dependencies=_READ)
async def pricing_lookup(model: str):
    from backend import pricing

    return {"model": model, "price": pricing.price_for(model)}


@router.put("/pricing/overrides", dependencies=_WRITE)
async def pricing_set(body: OverrideRequest):
    from backend import pricing

    pricing.set_override(body.model.strip(), body.price_in, body.price_out)
    audit.record("llm_cost.pricing.override", model=body.model)
    return pricing.status()


@router.delete("/pricing/overrides/{model:path}", dependencies=_WRITE)
async def pricing_clear(model: str):
    from backend import pricing

    if not pricing.clear_override(model):
        raise HTTPException(status_code=404, detail="No override for that model")
    audit.record("llm_cost.pricing.clear", model=model)
    return pricing.status()


@router.post("/pricing/refresh", dependencies=_WRITE)
async def pricing_refresh():
    from backend import pricing

    return await pricing.refresh(force=True)


# ── settings ───────────────────────────────────────────────────────────────


@router.get("/settings", dependencies=_READ)
async def get_settings():
    s = await store.get_settings()
    mqtt = s["mqtt"]
    s["mqttSecrets"] = {k: bool(mqtt.get(k) and resolve_secret(mqtt.get(k))) for k in ("username_ref", "password_ref")}
    return s


@router.put("/settings", dependencies=_WRITE)
async def put_settings(body: SettingsRequest):
    if body.quota_critical_pct < body.quota_warn_pct:
        raise HTTPException(status_code=400, detail="critical threshold must be at or above warn")
    values = body.model_dump()
    for key in ("username_ref", "password_ref"):
        ref = str(values["mqtt"].get(key) or "").strip()
        if ref and not _REF_RE.match(ref):
            raise HTTPException(status_code=400, detail=f"mqtt.{key} must be a $SECRET reference")
    saved = await store.save_settings(values)
    audit.record("llm_cost.settings.update")
    return saved


@router.post("/mqtt/test", dependencies=_WRITE)
async def mqtt_test():
    from backend.modules.llm_cost import mqtt

    cfg = (await store.get_settings())["mqtt"]
    if not cfg.get("host"):
        raise HTTPException(status_code=400, detail="Set an MQTT host first")
    return await asyncio.to_thread(mqtt.check_connection, cfg)
