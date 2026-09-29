"""Synthetic source for UI work and smoke tests. Hidden unless AGD_LLM_COST_DEMO=true."""

from __future__ import annotations

import math
import random
import time
from typing import Any

from backend.modules.llm_cost.models import (
    SCOPE_MODEL_WEEKLY,
    SCOPE_SESSION,
    SCOPE_WEEKLY,
    UNIT_REQUESTS,
    WINDOW_MTD,
    WINDOW_TODAY,
    Counter,
    Quota,
    Snapshot,
    Spend,
)
from backend.modules.llm_cost.providers.base import Provider, model_rows


class DemoProvider(Provider):
    TYPE = "demo"
    DISPLAY_NAME = "Demo"
    KIND = "agent"
    ACCENT = "#22d3ee"
    DEFAULT_INTERVAL = 30
    MIN_INTERVAL = 5
    DEV_ONLY = True
    DESCRIPTION = "Deterministic synthetic data for UI work."
    FIELDS = (
        {"key": "seed", "label": "Seed", "type": "number", "default": 7},
        {"key": "base_pct", "label": "Base quota %", "type": "number", "default": 42},
    )

    def __init__(self, context) -> None:
        super().__init__(context)
        self.base_pct = self.float_option("base_pct", 42.0)
        self.rng = random.Random(int(self.float_option("seed", 7)))

    async def fetch(self) -> Snapshot:
        now = time.time()
        wave = (math.sin(now / 900.0) + 1.0) / 2.0
        session = min(100.0, self.base_pct * wave + self.rng.uniform(0, 8))
        weekly = min(100.0, self.base_pct + wave * 30 + self.rng.uniform(0, 4))
        hour = 3600
        quotas = (
            Quota(SCOPE_SESSION, "SESSION", pct=session, resets_at=int(now + 5 * hour - (now % (5 * hour)))),
            Quota(SCOPE_WEEKLY, "WEEK", pct=weekly, resets_at=int(now + 3 * 86400)),
            Quota(SCOPE_MODEL_WEEKLY, "OPUS WEEK", pct=min(100.0, weekly * 1.15), resets_at=int(now + 3 * 86400)),
        )
        tokens = 12_000_000 + wave * 40_000_000
        spend = (Spend(WINDOW_TODAY, round(4.2 + wave * 18, 2), estimated=True),
                 Spend(WINDOW_MTD, round(180 + wave * 90, 2), estimated=True))
        counters = (Counter("total_tokens", round(tokens)), Counter("tokens_per_hour", round(tokens / 9)),
                    Counter("sessions", float(2 + int(wave * 4)), unit=UNIT_REQUESTS))
        per_model = {
            ("claude-opus-5", "today"): {"input": 400_000.0, "output": 90_000.0, "cache_read": 9_000_000.0},
            ("claude-sonnet-5", "today"): {"input": 900_000.0, "output": 120_000.0, "cache_read": 3_000_000.0},
        }
        meta: dict[str, Any] = {"topModel": "claude-opus-5", "synthetic": True}
        return self.snapshot(quotas=quotas, spend=spend, counters=counters, meta=meta,
                             detail={"models": model_rows(per_model)})
