"""OpenRouter usage and remaining credit.

/key (inference key) reports spend FOR THAT KEY ONLY. /credits, /activity and
/keys need a management key and report the whole account. The scopes are never
merged: spend rows stay key-scoped; account figures arrive as `account_*`
counters. /activity covers the last 30 COMPLETED UTC days, so it excludes today;
meta.activityThrough names the last day it covers.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from backend.modules.llm_cost.models import (
    SCOPE_CREDIT,
    UNIT_CREDITS,
    UNIT_REQUESTS,
    UNIT_TOKENS,
    UNIT_USD,
    WINDOW_7D,
    WINDOW_30D,
    WINDOW_MTD,
    WINDOW_TODAY,
    WINDOW_TOTAL,
    Counter,
    Quota,
    Snapshot,
    Spend,
)
from backend.modules.llm_cost.providers.base import (
    ConfigError,
    Provider,
    ProviderError,
    number_or_none,
    short_id,
)

API_ROOT = "https://openrouter.ai/api/v1"
KEY_URL = f"{API_ROOT}/key"
CREDITS_URL = f"{API_ROOT}/credits"
ACTIVITY_URL = f"{API_ROOT}/activity"
KEYS_URL = f"{API_ROOT}/keys"
MAX_ACTIVITY_ROWS = 5000


def reset_epoch(limit_reset: Any, now: Optional[dt.datetime] = None) -> Optional[int]:
    """OpenRouter reports a reset cadence, not a timestamp. Project the next UTC boundary."""
    if not isinstance(limit_reset, str):
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    token = limit_reset.strip().lower()
    if token in ("daily", "day"):
        nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif token in ("weekly", "week"):
        nxt = (now + dt.timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif token in ("monthly", "month"):
        nxt = (now.replace(day=28) + dt.timedelta(days=7)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return None
    return int(nxt.timestamp())


class OpenRouterProvider(Provider):
    TYPE = "openrouter"
    DISPLAY_NAME = "OpenRouter"
    KIND = "api"
    ACCENT = "#6467f2"
    DEFAULT_INTERVAL = 300
    MIN_INTERVAL = 60
    DESCRIPTION = ("Key spend and limit (inference key); account credits, 30-day per-model spend, and per-key "
                   "usage (management key). Either key alone works.")
    FIELDS = (
        {"key": "secret_ref", "label": "Inference key", "type": "secret", "default": "$OPENROUTER_KEY"},
        {"key": "management_secret_ref", "label": "Management key (optional)", "type": "secret",
         "default": "$OPENROUTER_MANAGEMENT_KEY"},
        {"key": "include_byok", "label": "Include BYOK usage", "type": "bool", "default": False},
    )

    def _key_ref(self) -> str:
        return self.context.option("secret_ref", "")

    def _mgmt_ref(self) -> str:
        return self.context.option("management_secret_ref", "")

    def validate(self) -> None:
        if not (self.context.secret(self._key_ref()) or self.context.secret(self._mgmt_ref())):
            self.context.require_secret(self._key_ref() or "$OPENROUTER_KEY",
                                        self._mgmt_ref() or "$OPENROUTER_MANAGEMENT_KEY")

    def _auth(self, ref: str) -> Optional[dict]:
        key = self.context.secret(ref) if ref else None
        return {"Authorization": f"Bearer {key}"} if key else None

    async def fetch(self) -> Snapshot:
        inference = self._auth(self._key_ref())
        management = self._auth(self._mgmt_ref())
        if not inference and not management:
            raise ConfigError(f"set secret {self._key_ref() or '$OPENROUTER_KEY'} "
                              f"or {self._mgmt_ref() or '$OPENROUTER_MANAGEMENT_KEY'}")
        byok = bool(self.context.option("include_byok", False))
        spend: list[Spend] = []
        quotas: list[Quota] = []
        counters: list[Counter] = []
        meta: dict[str, Any] = {}
        detail: dict[str, Any] = {"models": [], "trend": [], "trendBasis": "actual"}

        if inference:
            payload = await self.context.http.get_json(KEY_URL, headers=inference)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise ProviderError("unexpected /key response")
            data = payload["data"]

            def value(base: str) -> float:
                total = number_or_none(data.get(base)) or 0.0
                if byok:
                    total += number_or_none(data.get("byok_" + base)) or 0.0
                return total

            spend = [Spend(WINDOW_TODAY, value("usage_daily")), Spend(WINDOW_7D, value("usage_weekly")),
                     Spend(WINDOW_MTD, value("usage_monthly")), Spend(WINDOW_TOTAL, value("usage"))]
            limit = number_or_none(data.get("limit"))
            remaining = number_or_none(data.get("limit_remaining"))
            if limit and limit > 0:
                used = limit - remaining if remaining is not None else value("usage")
                quotas.append(Quota(scope=SCOPE_CREDIT, label="KEY LIMIT", used=max(0.0, used), limit=limit,
                                    unit=UNIT_USD, resets_at=reset_epoch(data.get("limit_reset"))))
            meta["label"] = str(data.get("label") or "")[:40]
            meta["freeTier"] = bool(data.get("is_free_tier", False))
            if remaining is not None:
                meta["limitRemaining"] = round(remaining, 4)

        if management:
            activity = await self._activity(management, byok)
            if activity is not None:
                a_counters, a_meta, models, trend = activity
                counters.extend(a_counters)
                meta.update(a_meta)
                detail["models"], detail["trend"] = models, trend
            credits = await self._credits(management)
            if credits is not None:
                purchased, used = credits
                counters.append(Counter("credits_purchased", purchased, unit=UNIT_CREDITS, window=WINDOW_TOTAL))
                counters.append(Counter("credits_used", used, unit=UNIT_CREDITS, window=WINDOW_TOTAL))
                if purchased > 0:
                    quotas.append(Quota(scope=SCOPE_CREDIT, label="CREDITS", used=used, limit=purchased,
                                        unit=UNIT_CREDITS))
                meta["creditsRemaining"] = round(purchased - used, 4)
            groups = await self._keys(management)
            if groups:
                detail["groupDims"] = [{"key": "key", "label": "API keys", "basis": "actual"}]
                detail["groups"] = {"key": groups}

        return self.snapshot(spend=tuple(spend), quotas=tuple(quotas), counters=tuple(counters), meta=meta,
                             detail=detail)

    async def _activity(self, headers: dict, byok: bool):
        """Account-wide usage for the last 30 completed UTC days. Silent when unreadable."""
        try:
            payload = await self.context.http.get_json(ACTIVITY_URL, headers=headers)
        except ProviderError:
            return None
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not rows:
            return None
        spend = requests = prompt = completion = reasoning = 0.0
        per_model: dict[str, dict[str, float]] = {}
        per_day: dict[str, float] = {}
        latest = ""
        for row in rows[:MAX_ACTIVITY_ROWS]:
            if not isinstance(row, dict):
                continue
            used = number_or_none(row.get("usage")) or 0.0
            if byok:
                used += number_or_none(row.get("byok_usage_inference")) or 0.0
            spend += used
            requests += number_or_none(row.get("requests")) or 0.0
            p = number_or_none(row.get("prompt_tokens")) or 0.0
            c = number_or_none(row.get("completion_tokens")) or 0.0
            prompt += p
            completion += c
            reasoning += number_or_none(row.get("reasoning_tokens")) or 0.0
            model = row.get("model") or row.get("model_permaslug")
            if isinstance(model, str) and model:
                d = per_model.setdefault(model, {"cost": 0.0, "input": 0.0, "output": 0.0})
                d["cost"] += used
                d["input"] += p
                d["output"] += c
            date = row.get("date")
            if isinstance(date, str) and date:
                day = date[:10]
                per_day[day] = per_day.get(day, 0.0) + used
                if day > latest:
                    latest = day

        counters = [
            Counter("account_spend_30d", round(spend, 6), unit=UNIT_USD, window=WINDOW_30D),
            Counter("account_requests_30d", requests, unit=UNIT_REQUESTS, window=WINDOW_30D),
            Counter("account_prompt_tokens_30d", prompt, unit=UNIT_TOKENS, window=WINDOW_30D),
            Counter("account_completion_tokens_30d", completion, unit=UNIT_TOKENS, window=WINDOW_30D),
        ]
        if reasoning:
            counters.append(Counter("account_reasoning_tokens_30d", reasoning, unit=UNIT_TOKENS, window=WINDOW_30D))
        meta: dict[str, Any] = {"accountModels": len(per_model)}
        if latest:
            meta["activityThrough"] = latest
        if per_model:
            meta["accountTopModel"] = max(per_model.items(), key=lambda kv: kv[1]["cost"])[0][:48]
        models = sorted(
            ({"model": m, "window": "30d", "inputTokens": d["input"], "outputTokens": d["output"],
              "cacheTokens": 0.0, "tokens": d["input"] + d["output"], "cost": round(d["cost"], 6),
              "costBasis": "actual"} for m, d in per_model.items()),
            key=lambda r: -(r["cost"] or 0.0))
        trend = [{"date": d, "amount": round(v, 6)} for d, v in sorted(per_day.items())]
        return counters, meta, models, trend

    async def _credits(self, headers: dict) -> Optional[tuple[float, float]]:
        try:
            payload = await self.context.http.get_json(CREDITS_URL, headers=headers)
        except ProviderError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return None
        purchased = number_or_none(payload["data"].get("total_credits"))
        used = number_or_none(payload["data"].get("total_usage"))
        if purchased is None or used is None:
            return None
        return purchased, used

    async def _keys(self, headers: dict) -> list[dict]:
        """Per-key lifetime usage vs limit (actual)."""
        try:
            payload = await self.context.http.get_json(KEYS_URL, headers=headers, params={"limit": 100})
        except ProviderError:
            return []
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or r.get("label") or short_id(r.get("hash")) or "key")[:60]
            used = number_or_none(r.get("usage")) or 0.0
            limit = number_or_none(r.get("limit"))
            out.append({"dim": "key", "id": str(r.get("hash") or name), "name": name,
                        "spend": {"total": round(used, 6)}, "cost": round(used, 6), "costBasis": "actual",
                        "models": [], "limit": round(limit, 6) if limit and limit > 0 else None,
                        "disabled": bool(r.get("disabled"))})
        out.sort(key=lambda g: -(g["cost"] or 0.0))
        return out
