"""Compound-secret templates — typed field layouts for multi-field credentials.

Templates drive the Secrets UI (type picker + labeled form) and the Sync-to-n8n
mapper (which n8n credential types a template can fill). Each template lists
ordered fields; secret fields render as password inputs with a reveal toggle.

Add new templates as users hit gaps. Keep field names in the shape the target
n8n credential type expects — the mapper references them directly.
"""
from __future__ import annotations

from typing import Any


# Field metadata:
#   name   — programmatic key, matches the n8n credential schema field name
#   label  — human label shown in the Secrets form
#   secret — True → password input + masked storage view
#   default — optional prefilled value
#
# n8n_types: list of n8n credential type ids this template can fill. The
# Sync-to-n8n endpoint checks this list before allowing a mirror.
TEMPLATES: dict[str, dict[str, Any]] = {
    "api_key": {
        "label": "API Key / Token",
        "description": "Single-value secret (API key, bearer token, PAT).",
        "fields": [
            {"name": "value", "label": "Value", "secret": True},
        ],
        # Single-value types are handled by the legacy _VALUE sentinel; listing
        # them here is informational — any type that wants just `value`.
        "n8n_types": ["*"],
    },
    "oauth2_client": {
        "label": "OAuth2 Client",
        "description": "Client ID + client secret. Finish the OAuth handshake in n8n.",
        "fields": [
            {"name": "clientId",     "label": "Client ID",     "secret": False},
            {"name": "clientSecret", "label": "Client Secret", "secret": True},
        ],
        "n8n_types": [
            "oAuth2Api",
            "googleOAuth2Api",
            "googleSheetsOAuth2Api",
            "googleDriveOAuth2Api",
            "googleCalendarOAuth2Api",
            "gmailOAuth2",
            "slackOAuth2Api",
            "microsoftOAuth2Api",
        ],
    },
    "connectwise_manage": {
        "label": "ConnectWise Manage",
        "description": "Public key, private key, client ID, company ID, and site URL.",
        "fields": [
            {"name": "siteUrl",    "label": "Site URL",    "secret": False,
             "default": "https://api-na.myconnectwise.net"},
            {"name": "companyId",  "label": "Company ID",  "secret": False},
            {"name": "publicKey",  "label": "Public Key",  "secret": True},
            {"name": "privateKey", "label": "Private Key", "secret": True},
            {"name": "clientId",   "label": "Client ID",   "secret": False},
        ],
        "n8n_types": ["connectWiseManageApi"],
    },
    "aws": {
        "label": "AWS",
        "description": "Access key ID, secret access key, and region.",
        "fields": [
            {"name": "accessKeyId",     "label": "Access Key ID",     "secret": False},
            {"name": "secretAccessKey", "label": "Secret Access Key", "secret": True},
            {"name": "region",          "label": "Region",            "secret": False,
             "default": "us-east-1"},
        ],
        "n8n_types": ["aws", "s3"],
    },
    "azure_ad": {
        "label": "Azure AD / Entra",
        "description": "Tenant ID, client ID, client secret.",
        "fields": [
            {"name": "tenantId",     "label": "Tenant ID",     "secret": False},
            {"name": "clientId",     "label": "Client ID",     "secret": False},
            {"name": "clientSecret", "label": "Client Secret", "secret": True},
        ],
        "n8n_types": ["microsoftOAuth2Api"],
    },
    "custom": {
        "label": "Custom (key/value)",
        "description": "Freeform multi-field secret. Add rows as needed.",
        "fields": [],  # UI renders a dynamic row editor
        "n8n_types": [],
    },
}


def get_template(name: str) -> dict[str, Any] | None:
    return TEMPLATES.get(name)


def template_supports_n8n_type(template_name: str, n8n_type: str) -> bool:
    tpl = TEMPLATES.get(template_name)
    if not tpl:
        return False
    allowed = tpl.get("n8n_types", [])
    return n8n_type in allowed or "*" in allowed


def field_names(template_name: str) -> list[str]:
    tpl = TEMPLATES.get(template_name)
    if not tpl:
        return []
    return [f["name"] for f in tpl.get("fields", [])]


def sanitized_registry() -> dict[str, Any]:
    """Registry shape safe to return over HTTP — labels + field metadata, no secrets."""
    return {
        name: {
            "label": tpl["label"],
            "description": tpl.get("description", ""),
            "fields": [
                {k: v for k, v in field.items() if k != "default" or v is not None}
                for field in tpl.get("fields", [])
            ],
            "n8n_types": tpl.get("n8n_types", []),
        }
        for name, tpl in TEMPLATES.items()
    }
