"""Normalized usage model. Every source maps into these shapes.

Honesty contract: an unknown value is None (rendered `--`), never 0; stale
readings are flagged, never passed off as fresh.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Sequence

SCOPE_SESSION = "session"
SCOPE_WEEKLY = "weekly"
SCOPE_MODEL_WEEKLY = "model_weekly"
SCOPE_MONTHLY = "monthly"
SCOPE_DAILY = "daily"
SCOPE_CREDIT = "credit"
SCOPES = frozenset({SCOPE_SESSION, SCOPE_WEEKLY, SCOPE_MODEL_WEEKLY, SCOPE_MONTHLY, SCOPE_DAILY, SCOPE_CREDIT})

UNIT_PCT = "pct"
UNIT_TOKENS = "tokens"
UNIT_REQUESTS = "requests"
UNIT_USD = "usd"
UNIT_CREDITS = "credits"
UNITS = frozenset({UNIT_PCT, UNIT_TOKENS, UNIT_REQUESTS, UNIT_USD, UNIT_CREDITS})

WINDOW_TODAY = "today"
WINDOW_MTD = "mtd"
WINDOW_7D = "7d"
WINDOW_30D = "30d"
WINDOW_TOTAL = "total"

HEALTH_OK = "ok"
HEALTH_STALE = "stale"
HEALTH_ERROR = "error"
HEALTH_DISABLED = "disabled"
HEALTH_UNCONFIGURED = "unconfigured"
HEALTH_STATES = frozenset({HEALTH_OK, HEALTH_STALE, HEALTH_ERROR, HEALTH_DISABLED, HEALTH_UNCONFIGURED})


def finite(value: Any) -> bool:
    """True for a real, non-bool, finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def clamp_pct(value: Any) -> Optional[float]:
    if not finite(value):
        return None
    return round(min(100.0, max(0.0, float(value))), 2)


def _epoch(value: Any) -> Optional[int]:
    if not finite(value):
        return None
    number = int(value)
    return number if number >= 0 else None


@dataclass(frozen=True)
class Quota:
    """How much of an allowance is gone, and when it resets."""

    scope: str
    label: str
    pct: Optional[float] = None
    used: Optional[float] = None
    limit: Optional[float] = None
    unit: str = UNIT_PCT
    resets_at: Optional[int] = None
    stale: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "pct", clamp_pct(self.pct))
        object.__setattr__(self, "used", float(self.used) if finite(self.used) else None)
        object.__setattr__(self, "limit", float(self.limit) if finite(self.limit) else None)
        object.__setattr__(self, "resets_at", _epoch(self.resets_at))
        object.__setattr__(self, "stale", bool(self.stale))
        if self.scope not in SCOPES:
            raise ValueError(f"unknown quota scope: {self.scope!r}")
        if self.unit not in UNITS:
            raise ValueError(f"unknown quota unit: {self.unit!r}")
        if self.pct is None and self.used is not None and self.limit:
            object.__setattr__(self, "pct", clamp_pct(self.used / self.limit * 100.0))

    def resets_in_seconds(self, now: Optional[float] = None) -> Optional[int]:
        if self.resets_at is None:
            return None
        return max(0, int(self.resets_at - (time.time() if now is None else now)))

    def expired(self, now: Optional[float] = None) -> bool:
        remaining = self.resets_in_seconds(now)
        return remaining is not None and remaining <= 0

    def money_remaining(self) -> Optional[float]:
        """Dollars left on a money-denominated quota (OpenRouter credits are USD)."""
        if self.unit not in (UNIT_USD, UNIT_CREDITS) or self.used is None or self.limit is None:
            return None
        return round(max(0.0, self.limit - self.used), 2)

    def as_dict(self, now: Optional[float] = None) -> dict[str, Any]:
        return {
            "scope": self.scope, "label": self.label, "pct": self.pct, "used": self.used,
            "limit": self.limit, "unit": self.unit, "resetsAt": self.resets_at,
            "resetsInSec": self.resets_in_seconds(now), "stale": self.stale,
            "remainingUsd": self.money_remaining(),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Quota":
        return cls(scope=d["scope"], label=d.get("label") or d["scope"].upper(), pct=d.get("pct"),
                   used=d.get("used"), limit=d.get("limit"), unit=d.get("unit") or UNIT_PCT,
                   resets_at=d.get("resetsAt"), stale=bool(d.get("stale")))


@dataclass(frozen=True)
class Spend:
    """Money burned in a window."""

    window: str
    amount: float
    currency: str = "USD"
    estimated: bool = False

    def __post_init__(self) -> None:
        if not finite(self.amount):
            raise ValueError("spend amount must be a finite number")
        object.__setattr__(self, "amount", round(float(self.amount), 6))
        object.__setattr__(self, "currency", str(self.currency).upper()[:8])
        object.__setattr__(self, "estimated", bool(self.estimated))

    def as_dict(self) -> dict[str, Any]:
        return {"window": self.window, "amount": self.amount, "currency": self.currency, "estimated": self.estimated}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Spend":
        return cls(d["window"], d["amount"], d.get("currency") or "USD", bool(d.get("estimated")))


@dataclass(frozen=True)
class Counter:
    """A raw tally: tokens, requests, whatever the provider meters."""

    key: str
    value: float
    unit: str = UNIT_TOKENS
    window: str = WINDOW_TODAY

    def __post_init__(self) -> None:
        if not finite(self.value):
            raise ValueError("counter value must be a finite number")
        object.__setattr__(self, "value", float(self.value))

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "unit": self.unit, "window": self.window}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Counter":
        return cls(d["key"], d["value"], d.get("unit") or UNIT_TOKENS, d.get("window") or WINDOW_TODAY)


@dataclass(frozen=True)
class Snapshot:
    """One source at one instant. `detail` carries drill-down (models, trend, groups)."""

    source_id: str
    display_name: str
    observed_at: int
    health: str = HEALTH_OK
    error: Optional[str] = None
    quotas: tuple[Quota, ...] = ()
    spend: tuple[Spend, ...] = ()
    counters: tuple[Counter, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)
    detail: Mapping[str, Any] = field(default_factory=dict)
    accent: str = "#38bdf8"
    kind: str = "api"
    source_type: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "quotas", tuple(self.quotas))
        object.__setattr__(self, "spend", tuple(self.spend))
        object.__setattr__(self, "counters", tuple(self.counters))
        object.__setattr__(self, "meta", dict(self.meta))
        object.__setattr__(self, "detail", dict(self.detail))
        if self.health not in HEALTH_STATES:
            raise ValueError(f"unknown health: {self.health!r}")

    def spend_for(self, window: str) -> Optional[Spend]:
        return next((s for s in self.spend if s.window == window), None)

    def counter_for(self, key: str) -> Optional[Counter]:
        return next((c for c in self.counters if c.key == key), None)

    def marked_stale(self, error: Optional[str] = None) -> "Snapshot":
        """Re-serve prior values honestly flagged, never as a fresh reading."""
        return replace(self, health=HEALTH_STALE, error=error or self.error,
                       quotas=tuple(replace(q, stale=True) for q in self.quotas))

    def as_dict(self, now: Optional[float] = None, include_detail: bool = False) -> dict[str, Any]:
        moment = time.time() if now is None else now
        out = {
            "sourceId": self.source_id, "displayName": self.display_name, "type": self.source_type,
            "kind": self.kind, "accent": self.accent, "observedAt": self.observed_at,
            "ageSec": max(0, int(moment - self.observed_at)), "health": self.health, "error": self.error,
            "quotas": [q.as_dict(moment) for q in self.quotas], "spend": [s.as_dict() for s in self.spend],
            "counters": [c.as_dict() for c in self.counters], "meta": dict(self.meta),
        }
        if include_detail:
            out["detail"] = dict(self.detail)
        return out

    def to_json_dict(self) -> dict[str, Any]:
        """Lossless persistence form."""
        d = self.as_dict(now=self.observed_at, include_detail=True)
        d.pop("ageSec", None)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Snapshot":
        def _many(builder, rows):
            out = []
            for row in rows or []:
                try:
                    out.append(builder(row))
                except (KeyError, TypeError, ValueError):
                    continue
            return tuple(out)

        return cls(
            source_id=d["sourceId"], display_name=d.get("displayName") or d["sourceId"],
            observed_at=int(d.get("observedAt") or 0), health=d.get("health") or HEALTH_OK,
            error=d.get("error"), quotas=_many(Quota.from_dict, d.get("quotas")),
            spend=_many(Spend.from_dict, d.get("spend")), counters=_many(Counter.from_dict, d.get("counters")),
            meta=d.get("meta") or {}, detail=d.get("detail") or {}, accent=d.get("accent") or "#38bdf8",
            kind=d.get("kind") or "api", source_type=d.get("type") or "",
        )


def error_snapshot(source_id: str, display_name: str, message: str, kind: str = "api",
                   accent: str = "#38bdf8", health: str = HEALTH_ERROR, source_type: str = "") -> Snapshot:
    """A failed poll is a first-class result."""
    return Snapshot(source_id=source_id, display_name=display_name, observed_at=int(time.time()), health=health,
                    error=(message or "")[:400] or None, kind=kind, accent=accent, source_type=source_type)


def total_spend(snapshots: Sequence[Snapshot], window: str) -> Optional[float]:
    """Sum one window across sources; None when no source reports it."""
    found = [s.spend_for(window) for s in snapshots]
    values = [e.amount for e in found if e is not None]
    return round(sum(values), 6) if values else None


def any_estimated(snapshots: Sequence[Snapshot], window: str) -> bool:
    return any((e := s.spend_for(window)) is not None and e.estimated for s in snapshots)
