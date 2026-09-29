"""Anthropic organization spend and token volume via the Admin API.

cost_report `amount` is a decimal STRING in cents: "123.78912" is $1.2378912.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from backend.modules.llm_cost.models import (
    SCOPE_MONTHLY,
    UNIT_USD,
    WINDOW_MTD,
    WINDOW_TODAY,
    Counter,
    Quota,
    Snapshot,
    Spend,
)
from backend.modules.llm_cost.providers.base import (
    Provider,
    ProviderError,
    add_tokens,
    build_groups,
    model_rows,
    month_start,
    next_month_epoch,
    num,
    paged,
    rfc3339,
    safe_groups,
    safe_names,
    today_start,
    zero_tokens,
)

API_ROOT = "https://api.anthropic.com/v1/organizations"
ANTHROPIC_VERSION = "2023-06-01"


def cents_to_usd(raw: Any) -> Decimal:
    """cost_report amounts are decimal strings in cents."""
    try:
        return Decimal(str(raw)) / Decimal(100)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def usage_tokens(result: dict) -> dict[str, float]:
    creation = result.get("cache_creation") or {}
    return {
        "input": num(result.get("uncached_input_tokens")),
        "output": num(result.get("output_tokens")),
        "cache_read": num(result.get("cache_read_input_tokens")),
        "cache_write_5m": num(creation.get("ephemeral_5m_input_tokens")),
        "cache_write_1h": num(creation.get("ephemeral_1h_input_tokens")),
    }


class AnthropicAdminProvider(Provider):
    TYPE = "anthropic_admin"
    DISPLAY_NAME = "Anthropic"
    KIND = "api"
    ACCENT = "#d97757"
    DEFAULT_INTERVAL = 900
    MIN_INTERVAL = 120
    DESCRIPTION = "Org spend, tokens, and per-model / per-workspace cost via the Admin API (sk-ant-admin key)."
    FIELDS = (
        {"key": "secret_ref", "label": "Admin key", "type": "secret", "default": "$ANTHROPIC_ADMIN_KEY"},
        {"key": "monthly_budget", "label": "Monthly budget (USD)", "type": "number", "default": 0},
        {"key": "workspace_ids", "label": "Workspace ids (comma separated, empty = all)", "type": "list"},
        {"key": "include_breakdown", "label": "Per-workspace / per-key breakdown", "type": "bool",
         "default": True},
    )

    def validate(self) -> None:
        self.context.require_secret(self.context.option("secret_ref", "$ANTHROPIC_ADMIN_KEY"))

    def _headers(self) -> dict[str, str]:
        key = self.context.require_secret(self.context.option("secret_ref", "$ANTHROPIC_ADMIN_KEY"))
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if key.startswith("sk-ant-"):
            headers["x-api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _filtered(self, params: dict) -> dict:
        ids = self.list_option("workspace_ids")
        if ids:
            params["workspace_ids[]"] = ids
        return params

    async def fetch(self) -> Snapshot:
        http = self.context.http
        headers = self._headers()
        start = month_start()
        today_key = today_start().strftime("%Y-%m-%d")

        buckets = await paged(http, f"{API_ROOT}/cost_report",
                              self._filtered({"starting_at": rfc3339(start), "bucket_width": "1d", "limit": 31}),
                              headers)
        month_total = Decimal(0)
        today_total = Decimal(0)
        currency = "USD"
        trend: dict[str, Decimal] = {}
        for bucket in buckets:
            day = str(bucket.get("starting_at", ""))[:10]
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                amount = cents_to_usd(result.get("amount"))
                currency = str(result.get("currency") or currency)
                month_total += amount
                trend[day] = trend.get(day, Decimal(0)) + amount
                if day == today_key:
                    today_total += amount

        spend = (Spend(WINDOW_MTD, float(month_total), currency), Spend(WINDOW_TODAY, float(today_total), currency))
        meta: dict[str, Any] = {"buckets": len(buckets)}
        counters: list[Counter] = []
        models: list[dict] = []
        try:
            counters, top_model, models = await self._usage(headers, start, today_key)
            if top_model:
                meta["topModel"] = top_model
        except ProviderError as exc:
            meta["usageError"] = str(exc)[:120]

        quotas: list[Quota] = []
        budget = self.float_option("monthly_budget", 0.0)
        if budget > 0:
            quotas.append(Quota(scope=SCOPE_MONTHLY, label="MONTH BUDGET", used=float(month_total), limit=budget,
                                unit=UNIT_USD, resets_at=next_month_epoch()))

        detail: dict[str, Any] = {
            "models": models,
            "trend": [{"date": d, "amount": round(float(v), 6)} for d, v in sorted(trend.items()) if d],
            "trendBasis": "actual",
        }
        if bool(self.context.option("include_breakdown", True)):
            dims, groups = await safe_groups(self._groups(headers, start, today_key))
            detail["groupDims"], detail["groups"] = dims, groups
        return self.snapshot(spend=spend, counters=tuple(counters), quotas=tuple(quotas), meta=meta, detail=detail)

    async def _usage(self, headers: dict, start, today_key: str) -> tuple[list[Counter], Optional[str], list[dict]]:
        buckets = await paged(self.context.http, f"{API_ROOT}/usage_report/messages",
                              self._filtered({"starting_at": rfc3339(start), "bucket_width": "1d", "limit": 31,
                                              "group_by[]": ["model"]}), headers)
        today = zero_tokens()
        per_model: dict[tuple[str, str], dict[str, float]] = {}
        today_by_model: dict[str, float] = {}
        for bucket in buckets:
            day = str(bucket.get("starting_at", ""))[:10]
            is_today = day == today_key
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                tok = usage_tokens(result)
                model = result.get("model")
                if is_today:
                    add_tokens(today, tok)
                if not model:
                    continue
                add_tokens(per_model.setdefault((model, "mtd"), zero_tokens()), tok)
                if is_today:
                    add_tokens(per_model.setdefault((model, "today"), zero_tokens()), tok)
                    today_by_model[model] = today_by_model.get(model, 0.0) + tok["input"] + tok["output"]
        written = today["cache_write_5m"] + today["cache_write_1h"]
        counters = [
            Counter("input_tokens", today["input"]),
            Counter("output_tokens", today["output"]),
            Counter("cache_read_tokens", today["cache_read"]),
            Counter("cache_write_tokens", written),
            Counter("total_tokens", today["input"] + today["output"] + today["cache_read"] + written),
        ]
        top = max(today_by_model, key=today_by_model.get) if today_by_model else None
        return counters, top, model_rows(per_model)

    async def _grouped_cost(self, headers: dict, start, today_key: str, field: str) -> dict:
        buckets = await paged(self.context.http, f"{API_ROOT}/cost_report",
                              self._filtered({"starting_at": rfc3339(start), "bucket_width": "1d", "limit": 31,
                                              "group_by[]": [field]}), headers)
        out: dict = {}
        for bucket in buckets:
            day = str(bucket.get("starting_at", ""))[:10]
            for r in bucket.get("results", []):
                if not isinstance(r, dict):
                    continue
                gid = r.get(field) or "untagged"
                amt = float(cents_to_usd(r.get("amount")))
                g = out.setdefault(gid, {"mtd": 0.0, "today": 0.0})
                g["mtd"] += amt
                if day == today_key:
                    g["today"] += amt
        return out

    async def _grouped_usage(self, headers: dict, start, field: str) -> dict:
        buckets = await paged(self.context.http, f"{API_ROOT}/usage_report/messages",
                              self._filtered({"starting_at": rfc3339(start), "bucket_width": "1d", "limit": 31,
                                              "group_by[]": [field, "model"]}), headers)
        per: dict = {}
        for bucket in buckets:
            for r in bucket.get("results", []):
                if not isinstance(r, dict) or not r.get("model"):
                    continue
                gid = r.get(field) or "untagged"
                add_tokens(per.setdefault(gid, {}).setdefault(r["model"], zero_tokens()), usage_tokens(r))
        return per

    async def _groups(self, headers: dict, start, today_key: str) -> tuple[list, dict]:
        dims: list = []
        groups: dict = {}
        http = self.context.http
        ws_names = await safe_names(http, f"{API_ROOT}/workspaces", headers)
        ws = build_groups("workspace", ws_names, await self._grouped_cost(headers, start, today_key, "workspace_id"),
                          await self._grouped_usage(headers, start, "workspace_id"))
        if ws:
            dims.append({"key": "workspace", "label": "Workspaces", "basis": "actual"})
            groups["workspace"] = ws
        key_names = await safe_names(http, f"{API_ROOT}/api_keys", headers)
        keys = build_groups("api_key", key_names, {}, await self._grouped_usage(headers, start, "api_key_id"))
        if keys:
            dims.append({"key": "api_key", "label": "API keys", "basis": "estimated"})
            groups["api_key"] = keys
        return dims, groups
