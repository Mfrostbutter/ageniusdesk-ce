"""Routes that mirror AgeniusDesk secrets into n8n credentials.

Endpoints:
  GET  /api/n8n-credentials/mappings
      -> list of credential-type definitions (used by the wizard UI to build
         the dropdown + auto-detect).

  POST /api/n8n-credentials/{instance_id}/mirror
      -> body: {items: [{secret_name, credential_type, skip?}]}
         For each non-skipped item, resolves the $secret_name to its value,
         POSTs to the n8n instance's /api/v1/credentials, and records the
         mapping in data/credential_mirrors.json. Returns a per-item result
         list — one bad item does not abort the batch.

  GET  /api/n8n-credentials/{instance_id}/mapped
      -> current mirror state for that instance (what's already synced).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.config import (
    DATA_DIR,
    decrypt_value,
    get_instances,
    is_secret_allowed_on_instance,
    load_secrets,
)

from .mappings import build_credential_payload, build_types_list_for_ui, fetch_live_schemas

logger = logging.getLogger(__name__)

# Operator floor: mirroring resolves stored secrets to their decrypted values and
# POSTs them to a caller-chosen n8n instance, and the schema/mapping reads are
# recon into the n8n API. A viewer must not reach any of it.
router = APIRouter(
    prefix="/api/n8n-credentials",
    tags=["n8n-credentials"],
    dependencies=[Depends(require_role("operator"))],
)

MIRRORS_FILE = DATA_DIR / "credential_mirrors.json"

# Per-instance schema cache. Each entry: {"fetched_at": epoch, "schemas": {type: schema}}.
# TTL keeps us from hammering n8n on every dropdown open.
_SCHEMA_CACHE: dict[str, dict] = {}
_SCHEMA_CACHE_TTL_SECS = 300  # 5 minutes


# ── Mirror-state storage ─────────────────────────────────────────────────────


def _load_mirrors() -> dict:
    if MIRRORS_FILE.exists():
        try:
            return json.loads(MIRRORS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_mirrors(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MIRRORS_FILE.write_text(json.dumps(data, indent=2))


# ── Instance lookup ──────────────────────────────────────────────────────────


def _instance_by_id(instance_id: str) -> dict:
    for inst in get_instances():
        if inst["id"] == instance_id:
            return inst
    raise HTTPException(status_code=404, detail=f"n8n instance '{instance_id}' not found")


def _resolve_instance_creds(inst: dict) -> tuple[str, str]:
    """Return (url, api_key) with both $VAR refs resolved and trailing slashes trimmed."""
    url = decrypt_value(inst.get("url", "")) or inst.get("url", "")
    api_key = decrypt_value(inst.get("api_key", ""))
    if url.endswith("/"):
        url = url[:-1]
    return url, api_key


# ── Secret lookup ────────────────────────────────────────────────────────────


def _resolve_secret(secret_name: str):
    """Look up `$SECRET_NAME` and return either the decrypted string (legacy)
    or the decrypted compound fields dict.

    Accepts both forms ("ANTHROPIC_KEY" and "$ANTHROPIC_KEY"). Raises if missing.
    """
    name = secret_name.lstrip("$").strip()
    if not name:
        raise HTTPException(status_code=400, detail="secret_name is required")
    stored = load_secrets()
    if name not in stored:
        raise HTTPException(status_code=404, detail=f"Secret '{name}' not found in AgeniusDesk store")
    entry = stored[name]
    # Compound: return decrypted fields map.
    if isinstance(entry, dict) and "type" in entry and isinstance(entry.get("fields"), dict):
        return {k: decrypt_value(v) for k, v in entry["fields"].items()}
    # Legacy string.
    value = decrypt_value(entry)
    if not value or value == name:
        raise HTTPException(status_code=500, detail=f"Could not decrypt secret '{name}'")
    return value


# ── Routes ───────────────────────────────────────────────────────────────────


async def _schemas_for_instance(instance_id: str) -> dict[str, dict]:
    """Return the live-schema map for an instance, using a 5-min cache."""
    import time

    now = time.time()
    cached = _SCHEMA_CACHE.get(instance_id)
    if cached and now - cached["fetched_at"] < _SCHEMA_CACHE_TTL_SECS:
        return cached["schemas"]

    inst = _instance_by_id(instance_id)
    url, api_key = _resolve_instance_creds(inst)
    schemas = await fetch_live_schemas(url, api_key)
    _SCHEMA_CACHE[instance_id] = {"fetched_at": now, "schemas": schemas}
    return schemas


@router.get("/{instance_id}/mappings")
async def get_mappings_for_instance(instance_id: str):
    """List credential types the given n8n instance supports, with schemas.

    Hits n8n's `/api/v1/credentials/schema/{type}` for every name in
    known_types.KNOWN_TYPES in parallel. Types the instance doesn't ship are
    dropped. Result shape: `{types: [{type, display_name, name_patterns,
    secret_field, required}]}`.
    """
    schemas = await _schemas_for_instance(instance_id)
    return {"types": build_types_list_for_ui(schemas)}


@router.post("/{instance_id}/refresh-schemas")
async def refresh_schemas(instance_id: str):
    """Bust the schema cache for an instance (force a re-fetch on the next call)."""
    _SCHEMA_CACHE.pop(instance_id, None)
    return {"success": True}


class MirrorItem(BaseModel):
    secret_name: str
    credential_type: str
    skip: bool = False


class MirrorBatch(BaseModel):
    items: list[MirrorItem]


@router.post("/{instance_id}/mirror")
async def mirror_to_instance(instance_id: str, req: MirrorBatch):
    """Push a batch of AgeniusDesk secrets to the given n8n instance as credentials."""
    inst = _instance_by_id(instance_id)
    url, api_key = _resolve_instance_creds(inst)
    if not url or not api_key:
        raise HTTPException(status_code=400, detail="Instance is missing URL or API key")

    mirrors = _load_mirrors()
    instance_state = mirrors.setdefault(instance_id, {})

    # Grab live schemas for this instance up-front — one batch fetch instead of
    # one per item. Results per type are used to build payloads for non-override
    # types; hand-tuned overrides in mappings.CRED_TYPES are used when present.
    schemas = await _schemas_for_instance(instance_id)

    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for item in req.items:
            if item.skip:
                results.append({"secret_name": item.secret_name, "status": "skipped"})
                continue

            if not item.credential_type:
                results.append({
                    "secret_name": item.secret_name,
                    "status": "error",
                    "error": "No credential type selected",
                })
                continue

            # Per-secret instance scope. Unscoped secrets are allowed everywhere.
            if not is_secret_allowed_on_instance(item.secret_name.lstrip("$"), instance_id):
                results.append({
                    "secret_name": item.secret_name,
                    "status": "error",
                    "error": "Secret is not scoped to this instance. Update its Applies To list.",
                })
                continue

            try:
                secret_value = _resolve_secret(item.secret_name)
                payload = build_credential_payload(
                    item.secret_name,
                    secret_value,
                    item.credential_type,
                    schema=schemas.get(item.credential_type),
                )

                # Re-mirror idempotency: if this secret was previously mirrored,
                # delete the prior credential in n8n before POSTing a new one so
                # the user doesn't accumulate duplicates on every wizard run.
                prior = instance_state.get(item.secret_name)
                if prior and prior.get("credential_id"):
                    try:
                        await client.delete(
                            f"{url}/api/v1/credentials/{prior['credential_id']}",
                            headers={"X-N8N-API-KEY": api_key},
                        )
                    except httpx.HTTPError:
                        # If the old credential was already deleted in n8n,
                        # that's fine — we just move on and create the new one.
                        pass

                r = await client.post(
                    f"{url}/api/v1/credentials",
                    headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json"},
                    json=payload,
                )
                if r.status_code >= 400:
                    # Surface n8n's error verbatim — most informative.
                    detail = r.text[:500]
                    try:
                        body = r.json()
                        detail = body.get("message") or body.get("detail") or detail
                    except (ValueError, json.JSONDecodeError):
                        pass
                    results.append({
                        "secret_name": item.secret_name,
                        "status": "error",
                        "error": f"n8n {r.status_code}: {detail}",
                    })
                    continue

                body = r.json()
                cred_id = body.get("id") or ""
                cred_name = body.get("name") or payload["name"]
                instance_state[item.secret_name] = {
                    "credential_id": cred_id,
                    "credential_name": cred_name,
                    "credential_type": item.credential_type,
                    "mirrored_at": datetime.utcnow().isoformat() + "Z",
                }
                results.append({
                    "secret_name": item.secret_name,
                    "status": "ok",
                    "credential_id": cred_id,
                    "credential_name": cred_name,
                    "credential_type": item.credential_type,
                })
            except HTTPException as e:
                results.append({"secret_name": item.secret_name, "status": "error", "error": e.detail})
            except httpx.HTTPError as e:
                results.append({"secret_name": item.secret_name, "status": "error", "error": f"Network: {e}"})
            except Exception as e:
                # Catch-all so one bad item doesn't abort the whole batch.
                logger.exception("Credential mirror failed for %s", item.secret_name)
                results.append({"secret_name": item.secret_name, "status": "error", "error": str(e)})

    # Persist whatever succeeded.
    mirrors[instance_id] = instance_state
    _save_mirrors(mirrors)
    return {"results": results}


@router.get("/{instance_id}/mapped")
async def list_mapped(instance_id: str):
    """Return the current mirror state for an instance."""
    # Instance must exist (surface 404 if the id is stale).
    _instance_by_id(instance_id)
    mirrors = _load_mirrors()
    return {"mirrors": mirrors.get(instance_id, {})}


@router.delete("/{instance_id}/{secret_name}")
async def unlink_mirror(instance_id: str, secret_name: str):
    """Delete the n8n credential mirrored from a secret and forget the mapping.

    Best-effort on the n8n side — if the credential was already deleted in
    n8n's UI, we still clear our mapping so the Sync panel is accurate.
    """
    inst = _instance_by_id(instance_id)
    mirrors = _load_mirrors()
    instance_state = mirrors.get(instance_id, {})
    entry = instance_state.get(secret_name)
    if not entry:
        # Nothing to unlink — idempotent success.
        return {"success": True, "message": "No mapping to unlink"}

    url, api_key = _resolve_instance_creds(inst)
    cred_id = entry.get("credential_id", "")
    n8n_deleted = False
    n8n_error = ""
    if cred_id and url and api_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.delete(
                    f"{url}/api/v1/credentials/{cred_id}",
                    headers={"X-N8N-API-KEY": api_key},
                )
                n8n_deleted = r.status_code < 400 or r.status_code == 404
                if not n8n_deleted:
                    n8n_error = f"n8n {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as e:
            n8n_error = f"Network: {e}"

    # Always clear the local mapping so the UI stays consistent with reality.
    instance_state.pop(secret_name, None)
    mirrors[instance_id] = instance_state
    _save_mirrors(mirrors)

    return {
        "success": True,
        "n8n_deleted": n8n_deleted,
        "n8n_error": n8n_error,
        "credential_id": cred_id,
    }
