"""Generic JSON source: any endpoint, numbers located by dotted paths.

    options = {
      "url": "https://api.elevenlabs.io/v1/user/subscription",
      "secret_ref": "$ELEVENLABS_API_KEY", "auth": "header", "auth_header": "xi-api-key",
      "quotas": [{"scope": "monthly", "label": "CHARACTERS", "used_path": "character_count",
                  "limit_path": "character_limit", "unit": "requests",
                  "resets_at_path": "next_character_count_reset_unix"}]
    }
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Optional

from backend.modules.llm_cost.models import SCOPES, UNITS, Counter, Quota, Snapshot, Spend
from backend.modules.llm_cost.providers.base import ConfigError, Provider, ProviderError

AUTH_MODES = ("bearer", "header", "query", "basic", "none")


def dig(payload: Any, path: str) -> Any:
    """Resolve a dotted path; numeric segments index lists."""
    if not path:
        return None
    node = payload
    for segment in str(path).split("."):
        if node is None:
            return None
        if isinstance(node, dict):
            node = node.get(segment)
        elif isinstance(node, (list, tuple)):
            try:
                node = node[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "").lstrip("$"))
        except ValueError:
            return None
    return None


class HttpJsonProvider(Provider):
    TYPE = "http_json"
    DISPLAY_NAME = "HTTP JSON"
    KIND = "api"
    ACCENT = "#38bdf8"
    DEFAULT_INTERVAL = 300
    MIN_INTERVAL = 30
    DESCRIPTION = "Any JSON endpoint. Map quotas, spend, counters, and meta with dotted paths."
    FIELDS = (
        {"key": "url", "label": "URL", "type": "text"},
        {"key": "method", "label": "Method", "type": "text", "default": "GET"},
        {"key": "secret_ref", "label": "Credential (optional)", "type": "secret", "default": ""},
        {"key": "auth", "label": "Auth mode (bearer, header, query, basic, none)", "type": "text",
         "default": "bearer"},
        {"key": "auth_header", "label": "Auth header (auth=header)", "type": "text", "default": "Authorization"},
        {"key": "auth_query", "label": "Auth query param (auth=query)", "type": "text", "default": "api_key"},
        {"key": "root_path", "label": "Root path", "type": "text", "default": ""},
        {"key": "scale", "label": "Spend scale", "type": "number", "default": 1},
        {"key": "quotas", "label": "Quota mappings", "type": "json", "default": []},
        {"key": "spend", "label": "Spend mappings", "type": "json", "default": []},
        {"key": "counters", "label": "Counter mappings", "type": "json", "default": []},
        {"key": "meta", "label": "Meta mappings", "type": "json", "default": {}},
        {"key": "headers", "label": "Extra headers", "type": "json", "default": {}},
        {"key": "params", "label": "Query params", "type": "json", "default": {}},
    )

    def __init__(self, context) -> None:
        super().__init__(context)
        opt = context.option
        self.url = str(opt("url", "")).strip()
        self.method = str(opt("method", "GET")).upper()
        self.secret_ref = str(opt("secret_ref", "")).strip()
        self.auth = str(opt("auth", "bearer")).lower()
        self.auth_header = str(opt("auth_header", "Authorization"))
        self.auth_query = str(opt("auth_query", "api_key"))
        self.extra_headers = dict(opt("headers", {}) or {})
        self.params = dict(opt("params", {}) or {})
        self.root_path = str(opt("root_path", ""))
        self.scale = self.float_option("scale", 1.0)
        self.quota_specs = [s for s in (opt("quotas", []) or []) if isinstance(s, dict)]
        self.spend_specs = [s for s in (opt("spend", []) or []) if isinstance(s, dict)]
        self.counter_specs = [s for s in (opt("counters", []) or []) if isinstance(s, dict)]
        self.meta_specs = dict(opt("meta", {}) or {})

    def validate(self) -> None:
        if not self.url:
            raise ConfigError("options.url is required")
        if not self.url.lower().startswith(("http://", "https://")):
            raise ConfigError("options.url must be http(s)")
        if self.method not in ("GET", "POST"):
            raise ConfigError("options.method must be GET or POST")
        if self.auth not in AUTH_MODES:
            raise ConfigError(f"unknown auth mode: {self.auth}")
        if not (self.quota_specs or self.spend_specs or self.counter_specs):
            raise ConfigError("declare at least one of quotas, spend, or counters")
        for spec in self.quota_specs:
            if str(spec.get("scope", "")) not in SCOPES:
                raise ConfigError(f"quota scope {spec.get('scope')!r} must be one of: {', '.join(sorted(SCOPES))}")
        if self.secret_ref:
            self.context.require_secret(self.secret_ref)

    def auth_parts(self) -> tuple[dict, dict]:
        headers = {str(k): str(v) for k, v in self.extra_headers.items()}
        params = dict(self.params)
        if not self.secret_ref or self.auth == "none":
            return headers, params
        key = self.context.require_secret(self.secret_ref)
        if self.auth == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        elif self.auth == "header":
            headers[self.auth_header] = key
        elif self.auth == "query":
            params[self.auth_query] = key
        elif self.auth == "basic":
            headers["Authorization"] = "Basic " + base64.b64encode(key.encode("utf-8")).decode("ascii")
        return headers, params

    async def fetch(self) -> Snapshot:
        self.validate()
        from backend.net import UnsafeProbeURL, assert_safe_probe_url

        try:
            await asyncio.to_thread(assert_safe_probe_url, self.url)
        except UnsafeProbeURL as exc:
            raise ConfigError(f"url refused: {exc}") from exc
        headers, params = self.auth_parts()
        payload = await self.context.http.request_json(self.method, self.url, headers=headers, params=params)
        return self.parse(payload)

    def parse(self, payload: Any) -> Snapshot:
        root = dig(payload, self.root_path) if self.root_path else payload
        if root is None:
            raise ProviderError(f"root_path {self.root_path!r} not found in response")

        quotas: list[Quota] = []
        for spec in self.quota_specs:
            unit = str(spec.get("unit", "pct"))
            unit = unit if unit in UNITS else "pct"
            pct = as_number(dig(root, spec.get("pct_path", "")))
            used = as_number(dig(root, spec.get("used_path", "")))
            limit = as_number(dig(root, spec.get("limit_path", "")))
            remaining = as_number(dig(root, spec.get("remaining_path", "")))
            if used is None and limit is not None and remaining is not None:
                used = max(0.0, limit - remaining)
            if pct is None and used is None:
                continue
            try:
                quotas.append(Quota(scope=str(spec.get("scope")),
                                    label=str(spec.get("label", spec.get("scope", ""))).upper()[:40],
                                    pct=pct, used=used, limit=limit, unit=unit,
                                    resets_at=as_number(dig(root, spec.get("resets_at_path", "")))))
            except ValueError as exc:
                raise ProviderError(f"bad quota spec: {exc}") from exc

        spend: list[Spend] = []
        for spec in self.spend_specs:
            amount = as_number(dig(root, spec.get("path", "")))
            if amount is None:
                continue
            spend.append(Spend(window=str(spec.get("window", "today")),
                               amount=amount * float(spec.get("scale", self.scale)),
                               currency=str(spec.get("currency", "USD")),
                               estimated=bool(spec.get("estimated", False))))

        counters: list[Counter] = []
        for spec in self.counter_specs:
            value = as_number(dig(root, spec.get("path", "")))
            if value is None:
                continue
            counters.append(Counter(key=str(spec.get("key", "value"))[:40], value=value,
                                    unit=str(spec.get("unit", "tokens")), window=str(spec.get("window", "today"))))

        meta: dict[str, Any] = {}
        for name, path in self.meta_specs.items():
            value = dig(root, str(path))
            if isinstance(value, (str, int, float, bool)):
                meta[str(name)[:40]] = value if not isinstance(value, str) else value[:120]

        if not quotas and not spend and not counters:
            raise ProviderError("response matched no configured path")
        return self.snapshot(quotas=tuple(quotas), spend=tuple(spend), counters=tuple(counters), meta=meta)
