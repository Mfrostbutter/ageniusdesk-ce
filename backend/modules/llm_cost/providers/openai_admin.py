"""OpenAI organization spend and token volume via the Admin API (sk-admin key).

Costs arrive in dollars under amount.value; completions input_tokens includes cached.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from backend.modules.llm_cost.models import (
    SCOPE_MONTHLY,
    UNIT_REQUESTS,
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
    safe_groups,
    safe_names,
    today_start,
    zero_tokens,
)

API_ROOT = "https://api.openai.com/v1/organization"
COSTS_URL = f"{API_ROOT}/costs"
COMPLETIONS_URL = f"{API_ROOT}/usage/completions"


def usage_tokens(result: dict) -> dict[str, float]:
    inp, cached = num(result.get("input_tokens")), num(result.get("input_cached_tokens"))
    return {"input": max(inp - cached, 0.0), "output": num(result.get("output_tokens")),
            "cache_read": cached, "cache_write_5m": 0.0, "cache_write_1h": 0.0}


def _day(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d") if epoch else ""


class OpenAIAdminProvider(Provider):
    TYPE = "openai_admin"
    DISPLAY_NAME = "OpenAI"
    KIND = "api"
    ACCENT = "#10a37f"
    DEFAULT_INTERVAL = 900
    MIN_INTERVAL = 120
    DESCRIPTION = "Org spend, tokens, and per-model / per-project cost via the Admin API (sk-admin key)."
    FIELDS = (
        {"key": "secret_ref", "label": "Admin key", "type": "secret", "default": "$OPENAI_ADMIN_KEY"},
        {"key": "monthly_budget", "label": "Monthly budget (USD)", "type": "number", "default": 0},
        {"key": "project_ids", "label": "Project ids (comma separated, empty = all)", "type": "list"},
        {"key": "include_breakdown", "label": "Per-project / per-key breakdown", "type": "bool", "default": True},
    )

    def validate(self) -> None:
        self.context.require_secret(self.context.option("secret_ref", "$OPENAI_ADMIN_KEY"))

    def _headers(self) -> dict[str, str]:
        key = self.context.require_secret(self.context.option("secret_ref", "$OPENAI_ADMIN_KEY"))
        return {"Authorization": f"Bearer {key}"}

    def _filtered(self, params: dict) -> dict:
        ids = self.list_option("project_ids")
        if ids:
            params["project_ids[]"] = ids
        return params

    async def fetch(self) -> Snapshot:
        http = self.context.http
        headers = self._headers()
        start = int(month_start().timestamp())
        today = int(today_start().timestamp())

        buckets = await paged(http, COSTS_URL, self._filtered({"start_time": start, "bucket_width": "1d",
                                                               "limit": 31}), headers)
        month_total = 0.0
        today_total = 0.0
        currency = "USD"
        trend: dict[str, float] = {}
        for bucket in buckets:
            bucket_start = int(num(bucket.get("start_time")))
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                amount = result.get("amount") or {}
                value = num(amount.get("value"))
                currency = str(amount.get("currency") or currency).upper()
                month_total += value
                trend[_day(bucket_start)] = trend.get(_day(bucket_start), 0.0) + value
                if bucket_start >= today:
                    today_total += value

        spend = (Spend(WINDOW_MTD, month_total, currency), Spend(WINDOW_TODAY, today_total, currency))
        meta: dict[str, Any] = {"buckets": len(buckets)}
        counters: list[Counter] = []
        models: list[dict] = []
        try:
            counters, top_model, models = await self._usage(headers, start, today)
            if top_model:
                meta["topModel"] = top_model
        except ProviderError as exc:
            meta["usageError"] = str(exc)[:120]

        quotas: list[Quota] = []
        budget = self.float_option("monthly_budget", 0.0)
        if budget > 0:
            quotas.append(Quota(scope=SCOPE_MONTHLY, label="MONTH BUDGET", used=month_total, limit=budget,
                                unit=UNIT_USD, resets_at=next_month_epoch()))
        detail: dict[str, Any] = {
            "models": models,
            "trend": [{"date": d, "amount": round(v, 6)} for d, v in sorted(trend.items()) if d],
            "trendBasis": "actual",
        }
        if bool(self.context.option("include_breakdown", True)):
            dims, groups = await safe_groups(self._groups(headers, start, today))
            detail["groupDims"], detail["groups"] = dims, groups
        return self.snapshot(spend=spend, counters=tuple(counters), quotas=tuple(quotas), meta=meta, detail=detail)

    async def _usage(self, headers: dict, start: int, today: int) -> tuple[list[Counter], Optional[str], list[dict]]:
        buckets = await paged(self.context.http, COMPLETIONS_URL,
                              self._filtered({"start_time": start, "bucket_width": "1d", "limit": 31,
                                              "group_by[]": ["model"]}), headers)
        totals = {"input": 0.0, "output": 0.0, "cached": 0.0, "requests": 0.0}
        per_model: dict[tuple[str, str], dict[str, float]] = {}
        today_by_model: dict[str, float] = {}
        for bucket in buckets:
            is_today = int(num(bucket.get("start_time"))) >= today
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                inp, out = num(result.get("input_tokens")), num(result.get("output_tokens"))
                if is_today:
                    totals["input"] += inp
                    totals["output"] += out
                    totals["cached"] += num(result.get("input_cached_tokens"))
                    totals["requests"] += num(result.get("num_model_requests"))
                model = result.get("model")
                if not model:
                    continue
                tok = usage_tokens(result)
                add_tokens(per_model.setdefault((model, "mtd"), zero_tokens()), tok)
                if is_today:
                    add_tokens(per_model.setdefault((model, "today"), zero_tokens()), tok)
                    today_by_model[model] = today_by_model.get(model, 0.0) + inp + out
        counters = [
            Counter("input_tokens", totals["input"]),
            Counter("output_tokens", totals["output"]),
            Counter("cached_tokens", totals["cached"]),
            Counter("requests", totals["requests"], unit=UNIT_REQUESTS),
            Counter("total_tokens", totals["input"] + totals["output"]),
        ]
        top = max(today_by_model, key=today_by_model.get) if today_by_model else None
        return counters, top, model_rows(per_model)

    async def _grouped_cost(self, headers: dict, start: int, today: int, field: str) -> dict:
        buckets = await paged(self.context.http, COSTS_URL,
                              self._filtered({"start_time": start, "bucket_width": "1d", "limit": 31,
                                              "group_by[]": [field]}), headers)
        out: dict = {}
        for bucket in buckets:
            bs = int(num(bucket.get("start_time")))
            for r in bucket.get("results", []):
                if not isinstance(r, dict):
                    continue
                gid = r.get(field) or "untagged"
                val = num((r.get("amount") or {}).get("value"))
                g = out.setdefault(gid, {"mtd": 0.0, "today": 0.0})
                g["mtd"] += val
                if bs >= today:
                    g["today"] += val
        return out

    async def _grouped_usage(self, headers: dict, start: int, field: str) -> dict:
        buckets = await paged(self.context.http, COMPLETIONS_URL,
                              self._filtered({"start_time": start, "bucket_width": "1d", "limit": 31,
                                              "group_by[]": [field, "model"]}), headers)
        per: dict = {}
        for bucket in buckets:
            for r in bucket.get("results", []):
                if not isinstance(r, dict) or not r.get("model"):
                    continue
                gid = r.get(field) or "untagged"
                add_tokens(per.setdefault(gid, {}).setdefault(r["model"], zero_tokens()), usage_tokens(r))
        return per

    async def _groups(self, headers: dict, start: int, today: int) -> tuple[list, dict]:
        dims: list = []
        groups: dict = {}
        names = await safe_names(self.context.http, f"{API_ROOT}/projects", headers)
        proj = build_groups("project", names, await self._grouped_cost(headers, start, today, "project_id"),
                            await self._grouped_usage(headers, start, "project_id"))
        if proj:
            dims.append({"key": "project", "label": "Projects", "basis": "actual"})
            groups["project"] = proj
        keys = build_groups("api_key", {}, {}, await self._grouped_usage(headers, start, "api_key_id"))
        if keys:
            dims.append({"key": "api_key", "label": "API keys", "basis": "estimated"})
            groups["api_key"] = keys
        return dims, groups
