"""Per-instance n8n capability record: edition, key scopes, reachable licensed endpoints.

Read-only against n8n. The record lets each module pick the Community path
(AgeniusDesk supplies the feature) or the licensed path (drive n8n's native
feature through the public API).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config import get_instance_by_id, update_instance

logger = logging.getLogger(__name__)

# endpoint key -> (path, query params). One GET each; every status is tolerated.
ENDPOINTS: dict[str, tuple[str, Optional[dict]]] = {
    "projects": ("/api/v1/projects", {"limit": 1}),
    "variables": ("/api/v1/variables", {"limit": 1}),
    "insights_summary": ("/api/v1/insights/summary", None),
    "roles": ("/api/v1/roles", None),
    "log_streaming": ("/api/v1/settings/log-streaming/destinations", None),
    "settings_otel": ("/api/v1/settings/otel", None),
    "security_policy": ("/api/v1/settings/security-policy", None),
    "source_control": ("/api/v1/source-control/pull", None),
    "data_tables": ("/api/v1/data-tables", {"limit": 1}),
}

# endpoint key -> feature key
FEATURE_OF: dict[str, str] = {
    "projects": "projects",
    "variables": "variables",
    "insights_summary": "insights",
    "roles": "roles",
    "log_streaming": "log_streaming",
    "settings_otel": "otel_settings",
    "security_policy": "security_policy",
    "source_control": "source_control",
    "data_tables": "data_tables",
    "workflow_history": "workflow_history",
}

# Endpoints a licence unlocks; a 403 here on a licensed instance means a frozen key.
LICENSED_ENDPOINTS = frozenset({
    "projects", "variables", "insights_summary", "roles", "log_streaming",
    "settings_otel", "security_policy", "source_control", "workflow_history",
})

# Response bodies never kept: otel settings carry exporterHeaders (a bearer).
BODYLESS_ENDPOINTS = frozenset({"settings_otel"})

FROZEN_KEY_NOTE = "key minted before licensing: scopes frozen; re-mint to reach enterprise endpoints"


def _present(key: str, status: Optional[int]) -> bool:
    if status == 200:
        return True
    return key == "source_control" and status == 405


def _unwrap(body: Any) -> dict:
    """/rest/settings and /discover both wrap the payload in `data`."""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


def _empty_record() -> dict[str, Any]:
    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "version": None,
        "licensed": False,
        "sso": {"saml": False, "ldap": False, "oidc": False},
        "key_scopes": [],
        "endpoints": {k: None for k in FEATURE_OF},
        "features": {v: False for v in FEATURE_OF.values()},
        "notes": [],
    }


async def probe(inst: dict, client=None) -> dict[str, Any]:
    """Build the capability record for one stored instance. Never raises.

    `client` must expose `probe_get(inst, path, params, authed=, want_body=)`;
    defaults to the n8n proxy client.
    """
    if client is None:
        from backend.modules.n8n_proxy import client as client_mod
        client = client_mod
    rec = _empty_record()
    notes: list[str] = rec["notes"]

    # Edition signal: unauthenticated /rest/settings, no key needed.
    status, body = await client.probe_get(inst, "/rest/settings", authed=False)
    settings = _unwrap(body) if status == 200 else {}
    if status == 200 and settings:
        ent = settings.get("enterprise") or {}
        rec["sso"] = {k: bool(ent.get(k)) for k in ("saml", "ldap", "oidc")}
        rec["licensed"] = any(rec["sso"].values())
        ver = settings.get("versionCli")
        rec["version"] = str(ver) if ver else None
    else:
        notes.append(
            f"GET /rest/settings unreachable ({'HTTP ' + str(status) if status else 'no response'}); "
            "edition unknown, treated as Community"
        )

    # Key scopes.
    status, body = await client.probe_get(inst, "/api/v1/discover")
    scopes = _unwrap(body).get("scopes") if status == 200 else None
    rec["key_scopes"] = [str(s) for s in scopes] if isinstance(scopes, list) else []

    # Endpoint fan-out, one GET each.
    keys = list(ENDPOINTS)
    results = await asyncio.gather(*(
        client.probe_get(inst, ENDPOINTS[k][0], ENDPOINTS[k][1], want_body=k not in BODYLESS_ENDPOINTS)
        for k in keys
    ))
    for k, (status, _body) in zip(keys, results):
        rec["endpoints"][k] = status

    # Workflow history needs a workflow id.
    status, body = await client.probe_get(inst, "/api/v1/workflows", {"limit": 1})
    wfs = (body or {}).get("data") if status == 200 and isinstance(body, dict) else None
    first_id = (wfs[0] or {}).get("id") if isinstance(wfs, list) and wfs else None
    if first_id:
        status, _ = await client.probe_get(
            inst, f"/api/v1/workflows/{first_id}/history", {"limit": 1}, want_body=False,
        )
        rec["endpoints"]["workflow_history"] = status
    else:
        notes.append("workflow history not probed: instance has no workflows")

    for k, feat in FEATURE_OF.items():
        rec["features"][feat] = _present(k, rec["endpoints"].get(k))

    if rec["licensed"]:
        frozen = sorted(k for k in LICENSED_ENDPOINTS if rec["endpoints"].get(k) == 403)
        if frozen:
            notes.append(f"{FROZEN_KEY_NOTE} ({', '.join(frozen)})")
    return rec


async def refresh_capabilities(instance_id: str) -> Optional[dict[str, Any]]:
    """Probe one instance and persist the record on it. None when the id is unknown."""
    inst = get_instance_by_id(instance_id)
    if not inst:
        return None
    rec = await probe(inst)
    update_instance(instance_id, {"capabilities": rec})
    return rec
