"""Credential-type resolution: live schemas fetched from the target n8n.

n8n exposes `GET /api/v1/credentials/schema/{type}` — returns the JSON schema
for a given credential type. We call that endpoint for each name in
`known_types.KNOWN_TYPES`, in parallel, and:

  1. Keep only types the instance actually supports (the rest 404).
  2. Use the schema to derive which field receives the secret value (apiKey,
     accessToken, token, …) and build the credential payload generically.

Some n8n credential types have schemas with `allOf`/conditional validation
(e.g. `anthropicApi` on 2.17.x has a `header: true/false` conditional that
requires headerName + headerValue). For those we keep hand-tuned overrides
in `CRED_TYPES` below — they win over the schema-derived shape.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .known_types import KNOWN_TYPES, detect_type_from_name, display_name_for

logger = logging.getLogger(__name__)

_VALUE = "value"  # sentinel: substitute the resolved (legacy single-value) secret


def _FIELD(field_name: str) -> dict:
    """Sentinel for compound-secret field lookups in CRED_TYPES.

    Emits `{"$field": "name"}` so it round-trips through JSON and is easy to
    test for in `build_credential_payload`.
    """
    return {"$field": field_name}


# Hand-tuned overrides for types whose schema has quirks (allOf conditionals,
# hidden required fields, etc.) that the generic schema-driven builder does
# not handle correctly. If a type is in here, this shape wins over whatever
# the n8n schema endpoint reports.
#
# Single-value types use the `_VALUE` sentinel.
# Compound types set `compound_template` (the secret_templates registry key)
# and use `_FIELD("name")` to pull sub-keys from the decrypted compound.
CRED_TYPES: dict[str, dict] = {
    "anthropicApi": {
        "fields": {
            "apiKey":      _VALUE,
            "header":      True,
            "headerName":  "x-api-key",
            "headerValue": _VALUE,
        },
    },
    "openAiApi": {
        "fields": {
            "apiKey":      _VALUE,
            "header":      True,
            "headerName":  "Authorization",
            "headerValue": _VALUE,
        },
    },
    # ── Compound types ──────────────────────────────────────────────────────
    "connectWiseManageApi": {
        "compound_template": "connectwise_manage",
        "fields": {
            "siteUrl":    _FIELD("siteUrl"),
            "companyId":  _FIELD("companyId"),
            "publicKey":  _FIELD("publicKey"),
            "privateKey": _FIELD("privateKey"),
            "clientId":   _FIELD("clientId"),
        },
    },
    "googleOAuth2Api": {
        "compound_template": "oauth2_client",
        "fields": {
            "clientId":     _FIELD("clientId"),
            "clientSecret": _FIELD("clientSecret"),
        },
    },
    "slackOAuth2Api": {
        "compound_template": "oauth2_client",
        "fields": {
            "clientId":     _FIELD("clientId"),
            "clientSecret": _FIELD("clientSecret"),
        },
    },
    "oAuth2Api": {
        "compound_template": "oauth2_client",
        "fields": {
            "clientId":     _FIELD("clientId"),
            "clientSecret": _FIELD("clientSecret"),
        },
    },
    "microsoftOAuth2Api": {
        "compound_template": "azure_ad",
        "fields": {
            "clientId":     _FIELD("clientId"),
            "clientSecret": _FIELD("clientSecret"),
            "tenantId":     _FIELD("tenantId"),
        },
    },
    "aws": {
        "compound_template": "aws",
        "fields": {
            "accessKeyId":     _FIELD("accessKeyId"),
            "secretAccessKey": _FIELD("secretAccessKey"),
            "region":          _FIELD("region"),
        },
    },
}


def _is_field_sentinel(v) -> bool:
    return isinstance(v, dict) and list(v.keys()) == ["$field"]


# Preferred secret-bearing field names, tried in order. First one present in
# the schema wins. After these, we fall back to any required string field.
_PREFERRED_SECRET_FIELDS = [
    "apiKey",
    "accessToken",
    "token",
    "apiToken",
    "personalAccessToken",
    "privateKey",
    "clientSecret",
    "secretKey",
    "authKey",
    "key",
]


def detect_secret_field(schema: dict) -> str:
    """Given an n8n credential schema, pick the field that should hold the secret."""
    properties = schema.get("properties", {}) or {}
    for name in _PREFERRED_SECRET_FIELDS:
        if name in properties:
            return name
    required = schema.get("required", []) or []
    for name in required:
        if properties.get(name, {}).get("type") == "string":
            return name
    # Last resort: first string property.
    for name, spec in properties.items():
        if spec.get("type") == "string":
            return name
    return ""


def _override_requires_compound(override: dict) -> bool:
    return bool(override.get("compound_template"))


def build_credential_payload(
    secret_name: str,
    secret_value: Any,
    credential_type: str,
    schema: dict | None = None,
) -> dict:
    """Build the body for POST /api/v1/credentials on an n8n instance.

    Priority: hand-tuned override → schema-derived → error.

    `secret_value` is either:
      - str (legacy single-value secret) — substituted wherever `_VALUE` appears
      - dict[str, str] (decrypted compound fields) — sub-keys are pulled via
        `_FIELD("name")` sentinels. Raises ValueError if the credential type
        requires a compound but a string was passed, or vice versa.
    """
    display_name = display_name_for(credential_type)

    override = CRED_TYPES.get(credential_type)
    if override:
        is_compound_type = _override_requires_compound(override)
        is_compound_value = isinstance(secret_value, dict)

        if is_compound_type and not is_compound_value:
            raise ValueError(
                f"{credential_type} requires a compound secret of type "
                f"'{override['compound_template']}'; got a single-value secret."
            )
        if not is_compound_type and is_compound_value:
            raise ValueError(
                f"{credential_type} is a single-value credential type; "
                f"cannot be filled from a compound secret."
            )

        data: dict[str, Any] = {}
        for field_name, source in override["fields"].items():
            if source == _VALUE:
                data[field_name] = secret_value
            elif _is_field_sentinel(source):
                key = source["$field"]
                if key not in secret_value:
                    raise ValueError(
                        f"Compound secret is missing required field '{key}' "
                        f"for {credential_type}."
                    )
                data[field_name] = secret_value[key]
            else:
                data[field_name] = source

        # Fill any OTHER schema-required fields with type-appropriate empty
        # defaults so n8n's validator accepts the payload. The override owns
        # the meaningful fields; this just keeps "scope", "serverUrl", etc.
        # from rejecting e.g. OAuth2-family credentials.
        if schema:
            properties = schema.get("properties", {}) or {}
            for fname in schema.get("required", []) or []:
                if fname in data:
                    continue
                ftype = properties.get(fname, {}).get("type")
                if ftype == "string":
                    data[fname] = ""
                elif ftype == "boolean":
                    data[fname] = False
                elif ftype in ("integer", "number"):
                    data[fname] = 0
                elif ftype == "array":
                    data[fname] = []
                elif ftype == "object":
                    data[fname] = {}

        return {
            "name": f"{display_name} (from AgeniusDesk: ${secret_name})",
            "type": credential_type,
            "data": data,
        }

    # Schema-derived path only handles single-value secrets.
    if isinstance(secret_value, dict):
        raise ValueError(
            f"{credential_type} has no compound template registered; "
            "cannot mirror a compound secret."
        )

    if not schema:
        raise ValueError(f"No schema or override available for {credential_type}")

    secret_field = detect_secret_field(schema)
    if not secret_field:
        raise ValueError(f"Could not find a secret-bearing field in schema for {credential_type}")

    properties = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []

    data = {secret_field: secret_value}
    # Fill any OTHER required fields with type-appropriate empty defaults so
    # n8n's schema validator doesn't reject the payload. User can fix up in
    # n8n's UI after. Strings → "", booleans → False, numbers → 0.
    for fname in required:
        if fname == secret_field or fname in data:
            continue
        ftype = properties.get(fname, {}).get("type")
        if ftype == "string":
            data[fname] = ""
        elif ftype == "boolean":
            data[fname] = False
        elif ftype in ("integer", "number"):
            data[fname] = 0

    return {
        "name": f"{display_name} (from AgeniusDesk: ${secret_name})",
        "type": credential_type,
        "data": data,
    }


async def fetch_live_schemas(url: str, api_key: str, timeout: float = 8.0) -> dict[str, dict]:
    """Fetch schemas for every name in KNOWN_TYPES from an n8n instance, in parallel.

    Types the instance doesn't ship (404) or that error are silently omitted.
    Returns `{type_name: schema_dict}`.
    """
    if not url or not api_key:
        return {}

    url = url.rstrip("/")
    headers = {"X-N8N-API-KEY": api_key}
    results: dict[str, dict] = {}

    async def _one(client: httpx.AsyncClient, type_name: str) -> None:
        try:
            r = await client.get(f"{url}/api/v1/credentials/schema/{type_name}", headers=headers)
            if r.status_code == 200:
                results[type_name] = r.json()
        except (httpx.HTTPError, ValueError):
            pass  # 404 / network / decode — drop quietly

    from backend.modules.n8n_proxy.client import _verify as _tls_verify

    async with httpx.AsyncClient(timeout=timeout, verify=_tls_verify()) as client:
        await asyncio.gather(*[_one(client, name) for name, _, _ in KNOWN_TYPES])

    return results


def build_types_list_for_ui(schemas: dict[str, dict]) -> list[dict]:
    """Build the dropdown payload for the frontend from live schemas.

    Keeps the ordering from KNOWN_TYPES (curated order) so related types
    cluster together visually.
    """
    out = []
    for type_name, display, patterns in KNOWN_TYPES:
        if type_name not in schemas:
            continue
        out.append({
            "type": type_name,
            "display_name": display,
            "name_patterns": patterns,
            "secret_field": detect_secret_field(schemas[type_name]),
            "required": schemas[type_name].get("required", []),
        })
    return out


# Re-export for backwards compat with callers that still import detect_type
detect_type = detect_type_from_name
