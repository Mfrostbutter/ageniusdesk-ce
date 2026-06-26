"""Admin API routes — user management, secrets store, config reset."""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.config import (
    DATA_DIR,
    decrypt_value,
    encrypt_value,
    load_config,
    load_secret_scopes,
    load_secrets,
    promote_to_secret,
    save_config,
    save_secret_scopes,
    save_secrets,
    settings,
)
from backend.modules.auth import service as auth_service
from backend.modules.admin.secret_templates import (
    TEMPLATES as SECRET_TEMPLATES,
    sanitized_registry as secret_templates_public,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])

USERS_FILE = DATA_DIR / "users.json"


class CreateUser(BaseModel):
    username: str
    display_name: str = ""
    role: str = "viewer"
    password: str


class CreateSecret(BaseModel):
    """Payload for POST /secrets. Accepts legacy single-value or compound form.

    Legacy:   {name, value}
    Compound: {name, type, fields:{fieldName: rawValue, ...}}

    `type` (when present) must be a registered template from secret_templates.
    """

    name: str
    # Legacy single-value secret.
    value: str | None = None
    # Compound secret fields.
    type: str | None = None
    fields: dict[str, str] | None = None


# ── Dashboard Users ──────────────────────────────────────────────────────────


def _load_users() -> list[dict]:
    return auth_service.load_users()


def _save_users(users: list[dict]) -> None:
    auth_service.save_users(users)


@router.get("/users")
async def list_users():
    users = _load_users()
    safe = [
        {
            "username": u["username"],
            "display_name": u.get("display_name", ""),
            "role": u.get("role", "viewer"),
        }
        for u in users
    ]
    return {"users": safe}


@router.post("/users")
async def create_user(req: CreateUser):
    users = _load_users()
    if req.role not in ("viewer", "operator", "admin"):
        raise HTTPException(status_code=400, detail="Role must be viewer, operator, or admin")
    if any(u["username"] == req.username for u in users):
        raise HTTPException(status_code=409, detail=f"User '{req.username}' already exists")
    if len(req.password) < settings.agd_password_min_length:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {settings.agd_password_min_length} characters",
        )
    now = auth_service._iso(auth_service._now())
    users.append({
        "username": req.username,
        "display_name": req.display_name,
        "role": req.role,
        "created_at": now,
        "password_changed_at": now,
        **auth_service.hash_password(req.password),
        "totp": {"enabled": False, "secret_enc": "", "recovery_codes": []},
    })
    _save_users(users)
    return {"success": True, "username": req.username}


@router.delete("/users/{username}")
async def delete_user(username: str):
    users = _load_users()
    before = len(users)
    users = [u for u in users if u["username"] != username]
    if len(users) == before:
        raise HTTPException(status_code=404, detail="User not found")
    _save_users(users)
    return {"success": True}


# ── Secrets Store ────────────────────────────────────────────────────────────
# Secrets are stored encrypted in data/secrets.json.
# Users reference them as $SECRET_NAME in instance API key fields.
# At runtime, $SECRET_NAME is resolved by decrypt_value() in config.py.


def _hint_for(decrypted: str) -> str:
    """Build a masked hint for display (never reveals full value)."""
    if decrypted and not decrypted.startswith(("enc:", "fernet:")):
        return decrypted[:4] + "..." + decrypted[-3:] if len(decrypted) > 8 else "****"
    return "****"


def _is_compound(entry) -> bool:
    return isinstance(entry, dict) and "type" in entry and isinstance(entry.get("fields"), dict)


def _describe_entry(name: str, entry) -> dict:
    """Public-safe description of a stored secret — no raw values."""
    if _is_compound(entry):
        tpl_name = entry.get("type", "custom")
        tpl = SECRET_TEMPLATES.get(tpl_name) or SECRET_TEMPLATES["custom"]
        fields = entry.get("fields", {})
        field_meta = []
        # Prefer template order; fall back to whatever is stored (custom).
        tpl_fields = tpl.get("fields") or [{"name": k, "label": k, "secret": True} for k in fields]
        seen = set()
        for f in tpl_fields:
            fname = f["name"]
            if fname not in fields:
                continue
            seen.add(fname)
            field_meta.append({
                "name":   fname,
                "label":  f.get("label", fname),
                "secret": f.get("secret", True),
                "hint":   _hint_for(decrypt_value(fields[fname])),
            })
        # Stragglers stored but not in template (schema drift / custom template).
        for fname, enc in fields.items():
            if fname in seen:
                continue
            field_meta.append({
                "name": fname, "label": fname, "secret": True,
                "hint": _hint_for(decrypt_value(enc)),
            })
        return {
            "name": name,
            "kind": "compound",
            "type": tpl_name,
            "type_label": tpl["label"],
            "fields": field_meta,
        }
    return {
        "name": name,
        "kind": "string",
        "type": "api_key",
        "type_label": SECRET_TEMPLATES["api_key"]["label"],
        "hint": _hint_for(decrypt_value(entry)),
    }


@router.get("/secret-templates")
async def list_secret_templates():
    """Public template registry for the Secrets UI type picker."""
    return {"templates": secret_templates_public()}


@router.get("/secrets")
async def list_secrets():
    """List stored secrets (names + hints only, never raw values)."""
    stored = load_secrets()
    scopes = load_secret_scopes()
    items = []
    for name, entry in sorted(stored.items()):
        info = _describe_entry(name, entry)
        info["allowed_instances"] = scopes.get(name, [])
        items.append(info)
    return {"secrets": items}


class ScopeUpdate(BaseModel):
    """Payload for PUT /secrets/{name}/scope."""

    allowed_instances: list[str] = []


@router.get("/secrets/{name}/scope")
async def get_secret_scope(name: str):
    """Return the instance scope for one secret. Empty list = all instances."""
    stored = load_secrets()
    if name not in stored:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"name": name, "allowed_instances": load_secret_scopes().get(name, [])}


@router.put("/secrets/{name}/scope")
async def set_secret_scope(name: str, req: ScopeUpdate):
    """Set the instance scope for one secret.

    `allowed_instances=[]` means allowed on all instances (also the default
    for secrets that have never been scoped).
    """
    stored = load_secrets()
    if name not in stored:
        raise HTTPException(status_code=404, detail="Secret not found")
    scopes = load_secret_scopes()
    if req.allowed_instances:
        scopes[name] = list(dict.fromkeys(req.allowed_instances))  # dedupe, preserve order
    else:
        scopes.pop(name, None)
    save_secret_scopes(scopes)
    return {"success": True, "name": name, "allowed_instances": scopes.get(name, [])}


@router.get("/secrets/refs")
async def list_secret_refs():
    """Compact shape for dropdowns. Compound secrets expand into one ref per field."""
    stored = load_secrets()
    refs = []
    for name, entry in sorted(stored.items()):
        if _is_compound(entry):
            tpl_name = entry.get("type", "custom")
            tpl = SECRET_TEMPLATES.get(tpl_name) or SECRET_TEMPLATES["custom"]
            tpl_label = tpl["label"]
            for fname, enc in entry.get("fields", {}).items():
                refs.append({
                    "name": f"{name}.{fname}",
                    "ref":  f"${name}.{fname}",
                    "hint": _hint_for(decrypt_value(enc)),
                    "compound_type": tpl_name,
                    "compound_label": tpl_label,
                })
        else:
            refs.append({
                "name": name,
                "ref":  f"${name}",
                "hint": _hint_for(decrypt_value(entry)),
            })
    return {"refs": refs}


@router.post("/secrets")
async def create_secret(req: CreateSecret):
    """Store a new secret (encrypted at rest)."""
    name = req.name.upper().strip().replace(" ", "_")
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    stored = load_secrets()

    # Compound secret: type + fields provided (fields may also be present with
    # a legacy `value`; compound wins if `type` is set to anything non-default).
    if req.type and req.type != "api_key":
        if req.type not in SECRET_TEMPLATES:
            raise HTTPException(status_code=400, detail=f"Unknown secret type: {req.type}")
        if not req.fields:
            raise HTTPException(status_code=400, detail="Fields are required for compound secrets")
        tpl = SECRET_TEMPLATES[req.type]
        template_field_names = {f["name"] for f in tpl.get("fields", [])}

        encrypted_fields: dict[str, str] = {}
        for fname, fval in req.fields.items():
            # For non-custom templates, reject unknown fields; custom accepts anything.
            if req.type != "custom" and template_field_names and fname not in template_field_names:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown field '{fname}' for template '{req.type}'",
                )
            if fval is None or fval == "":
                continue
            encrypted_fields[fname] = encrypt_value(fval)

        if not encrypted_fields:
            raise HTTPException(status_code=400, detail="At least one field value is required")

        stored[name] = {"type": req.type, "fields": encrypted_fields}
        save_secrets(stored)
        return {"success": True, "name": name, "ref": f"${name}", "kind": "compound"}

    # Legacy single-value secret.
    value = req.value if req.value is not None else ""
    # api_key template may also arrive as {type:"api_key", fields:{value:"..."}}
    if not value and req.fields and "value" in req.fields:
        value = req.fields["value"] or ""
    if not value:
        raise HTTPException(status_code=400, detail="Value is required")

    stored[name] = encrypt_value(value)
    save_secrets(stored)
    # Also inject into the current process environment so $NAME resolves immediately.
    os.environ[name] = value

    return {"success": True, "name": name, "ref": f"${name}", "kind": "string"}


class PromoteSecret(BaseModel):
    value: str
    prefix: str = "SECRET"
    context: str = ""


@router.post("/secrets/promote")
async def promote_secret_endpoint(req: PromoteSecret):
    """Promote a raw value to the secrets store and return its $VAR ref.

    Idempotent: if `value` is already "$VAR", returns it unchanged. If the same
    raw value already lives under an existing name with the same prefix/context
    base, that existing name is reused.
    """
    if not req.value:
        raise HTTPException(status_code=400, detail="Value is required")
    if req.value.startswith("$"):
        # Already a reference — pass it through.
        name = req.value[1:]
        return {"name": name, "ref": req.value}

    ref = promote_to_secret(req.value, prefix=req.prefix, context=req.context)
    if not ref or not ref.startswith("$"):
        raise HTTPException(status_code=500, detail="Failed to promote value")
    return {"name": ref[1:], "ref": ref}


@router.delete("/secrets/{name}")
async def delete_secret(name: str):
    """Delete a stored secret (also clears any instance scope)."""
    stored = load_secrets()
    if name not in stored:
        raise HTTPException(status_code=404, detail="Secret not found")
    del stored[name]
    save_secrets(stored)
    scopes = load_secret_scopes()
    if scopes.pop(name, None) is not None:
        save_secret_scopes(scopes)
    os.environ.pop(name, None)
    return {"success": True}


# ── Assistant Test (resolves $VAR before pinging) ─────────────────────────────


class AssistantTestRequest(BaseModel):
    provider: str
    api_key: str = ""
    model: str = ""
    ollama_url: str = ""


@router.post("/assistant/test")
async def test_assistant_creds(req: AssistantTestRequest):
    """Test LLM creds without saving config. Resolves $VAR refs first.

    Accepts a raw key, a $VAR reference, or an Ollama URL. Sends a minimal
    request to the provider and returns {ok, error?, model?}.
    """
    from backend.modules.assistant.providers import ping_provider

    # Resolve $VAR refs against secrets store / env
    api_key = decrypt_value(req.api_key) if req.api_key else ""
    ollama_url = decrypt_value(req.ollama_url) if req.ollama_url else ""
    # decrypt_value returns the name unchanged if $VAR isn't found — treat that as empty
    if api_key.startswith("$"):
        api_key = ""
    if ollama_url.startswith("$"):
        ollama_url = ""

    result = await ping_provider(
        provider=req.provider,
        api_key=api_key,
        model=req.model,
        ollama_url=ollama_url,
    )
    return result


# ── Environment Variables ────────────────────────────────────────────────────


@router.get("/env")
async def list_env_vars():
    """Detect relevant environment variables (names only)."""
    prefixes = ("N8N_", "FLOW_DASHBOARD_", "QDRANT_", "LLM_", "OLLAMA_")
    variables = []
    for key, value in sorted(os.environ.items()):
        if any(key.startswith(p) for p in prefixes):
            variables.append({"name": key, "set": bool(value), "length": len(value) if value else 0})
    return {"variables": variables}


# ── Config Reset ─────────────────────────────────────────────────────────────


@router.post("/reset")
async def reset_config():
    save_config({})
    return {"success": True}


# ── Public API Key Management ─────────────────────────────────────────────────
# Keys are stored as sha256 hashes in data/api_keys.json — never raw values.
# The raw key is returned exactly once on creation and never persisted.


class CreateApiKey(BaseModel):
    name: str
    scope: str = "read"  # "read" | "trigger"


@router.post("/api-keys")
async def create_api_key_endpoint(req: CreateApiKey):
    """Create a new public API key. Returns the raw key once — store it safely."""
    from backend.modules.public_api.api_keys import VALID_SCOPES, create_api_key

    if req.scope not in VALID_SCOPES:
        raise HTTPException(status_code=400, detail=f"scope must be one of {sorted(VALID_SCOPES)}")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    raw_key, record = create_api_key(name, req.scope)
    return {
        "key": raw_key,  # shown once — caller must copy it now
        "id": record["id"],
        "name": record["name"],
        "scope": record["scope"],
        "created_at": record["created_at"],
    }


@router.get("/api-keys")
async def list_api_keys():
    """List all public API keys (metadata only — hashes never returned)."""
    from backend.modules.public_api.api_keys import load_api_keys

    keys = load_api_keys()
    safe = [
        {
            "id": k["id"],
            "name": k["name"],
            "scope": k["scope"],
            "created_at": k["created_at"],
        }
        for k in keys
    ]
    return {"api_keys": safe}


@router.delete("/api-keys/{key_id}")
async def delete_api_key_endpoint(key_id: str):
    """Revoke a public API key by ID."""
    from backend.modules.public_api.api_keys import delete_api_key

    if not delete_api_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"success": True}


@router.post("/setup-complete")
async def mark_setup_complete():
    """Persist wizard completion even when no n8n instance is added yet."""
    config = load_config()
    config["setup_complete"] = True
    save_config(config)
    return {"success": True, "configured": True}
