"""Push-mode source: never polled; workstation forwarders POST snapshots.

`parse_payload` bounds and re-validates untrusted ingest (a forwarder is trusted
to authenticate, never to be well-behaved). `aggregate` folds the latest push of
every device into one source snapshot.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence

from backend.modules.llm_cost.models import (
    HEALTH_OK,
    HEALTH_STATES,
    HEALTH_UNCONFIGURED,
    SCOPES,
    UNITS,
    WINDOW_MTD,
    WINDOW_TODAY,
    Counter,
    Quota,
    Snapshot,
    Spend,
    error_snapshot,
)
from backend.modules.llm_cost.providers.base import MODE_PUSH, Provider, ProviderError, model_rows

MAX_QUOTAS = 8
MAX_SPEND = 8
MAX_COUNTERS = 24
MAX_META_KEYS = 16
MAX_USAGE_ROWS = 64
MAX_STRING = 120
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


class PushProvider(Provider):
    TYPE = "push"
    DISPLAY_NAME = "Pushed"
    KIND = "push"
    ACCENT = "#a78bfa"
    MODE = MODE_PUSH
    DEFAULT_INTERVAL = 60
    DESCRIPTION = "Fed by workstation forwarders (Claude Code usage and plan limits). Create a device token to push."
    FIELDS = ()

    async def fetch(self) -> Snapshot:
        return error_snapshot(self.source_id, self.display_name, "waiting for a push from a forwarder",
                              kind=self.KIND, accent=self.accent, health=HEALTH_UNCONFIGURED, source_type=self.TYPE)


def clean_string(value: Any, limit: int = MAX_STRING) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = "".join(ch for ch in value if ch.isprintable()).strip()
    return stripped[:limit] or None


def clean_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def clean_accent(value: Any) -> str:
    text = clean_string(value, 9) or ""
    if len(text) in (4, 7) and text.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in text[1:]):
        return text
    return "#a78bfa"


def _usage_rows(raw: Any) -> list[dict]:
    rows = []
    for entry in (raw or [])[:MAX_USAGE_ROWS] if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        model = clean_string(entry.get("model"), 80)
        window = clean_string(entry.get("window"), 16) or WINDOW_TODAY
        if not model or window not in (WINDOW_TODAY, WINDOW_MTD):
            continue
        tokens = {k: max(0.0, clean_number(entry.get(k)) or 0.0) for k in TOKEN_KINDS}
        if sum(tokens.values()) <= 0:
            continue
        rows.append({"model": model, "window": window, **tokens})
    return rows


def _estimate(rows: Sequence[dict], window: str) -> tuple[Optional[float], list[str]]:
    """Price-book estimate for one window; None when nothing is priced."""
    from backend import pricing

    total, priced, unpriced = 0.0, False, []
    for row in rows:
        if row["window"] != window:
            continue
        cost = pricing.estimate_cost(row["model"], {k: row[k] for k in TOKEN_KINDS})
        if cost is None:
            unpriced.append(row["model"])
            continue
        total += cost
        priced = True
    return (round(total, 6) if priced else None), unpriced


def parse_payload(payload: Any, source_id: str, max_future_sec: int = 120, now: Optional[int] = None) -> Snapshot:
    """Turn an untrusted ingest body into a Snapshot for `source_id`, or raise ProviderError."""
    if not isinstance(payload, dict):
        raise ProviderError("body must be a JSON object")
    claimed = clean_string(payload.get("providerId") or payload.get("sourceId"), 64)
    if claimed and claimed != source_id:
        raise ProviderError(f"token is not allowed to push to {claimed!r}")

    now = int(time.time()) if now is None else now
    observed = payload.get("observedAt")
    observed_at = int(observed) if clean_number(observed) is not None else now
    observed_at = min(max(0, observed_at), now + max_future_sec)

    quotas: list[Quota] = []
    for entry in (payload.get("quotas") or [])[:MAX_QUOTAS]:
        if not isinstance(entry, dict):
            continue
        scope = clean_string(entry.get("scope"), 32)
        if scope not in SCOPES:
            continue
        unit = clean_string(entry.get("unit"), 16) or "pct"
        unit = unit if unit in UNITS else "pct"
        resets_at = clean_number(entry.get("resetsAt"))
        try:
            quotas.append(Quota(scope=scope, label=clean_string(entry.get("label"), 40) or scope.upper(),
                                pct=clean_number(entry.get("pct")), used=clean_number(entry.get("used")),
                                limit=clean_number(entry.get("limit")), unit=unit,
                                resets_at=int(resets_at) if resets_at is not None else None,
                                stale=bool(entry.get("stale", False))))
        except ValueError:
            continue

    spend: list[Spend] = []
    for entry in (payload.get("spend") or [])[:MAX_SPEND]:
        if not isinstance(entry, dict):
            continue
        amount = clean_number(entry.get("amount"))
        window = clean_string(entry.get("window"), 16)
        if amount is None or not window:
            continue
        try:
            spend.append(Spend(window, amount, clean_string(entry.get("currency"), 8) or "USD",
                               bool(entry.get("estimated", False))))
        except ValueError:
            continue

    counters: list[Counter] = []
    for entry in (payload.get("counters") or [])[:MAX_COUNTERS]:
        if not isinstance(entry, dict):
            continue
        key = clean_string(entry.get("key"), 40)
        value = clean_number(entry.get("value"))
        if not key or value is None:
            continue
        try:
            counters.append(Counter(key, value, clean_string(entry.get("unit"), 16) or "tokens",
                                    clean_string(entry.get("window"), 16) or WINDOW_TODAY))
        except ValueError:
            continue

    meta: dict[str, Any] = {}
    raw_meta = payload.get("meta")
    if isinstance(raw_meta, dict):
        for key, value in list(raw_meta.items())[:MAX_META_KEYS]:
            clean_key = clean_string(key, 40)
            if not clean_key:
                continue
            if isinstance(value, bool) or clean_number(value) is not None:
                meta[clean_key] = value
            else:
                clean_value = clean_string(value, MAX_STRING)
                if clean_value is not None:
                    meta[clean_key] = clean_value

    usage = _usage_rows(payload.get("usage"))
    have = {s.window for s in spend}
    unpriced: set[str] = set()
    for window in (WINDOW_TODAY, WINDOW_MTD):
        if window in have or not any(r["window"] == window for r in usage):
            continue
        amount, missing = _estimate(usage, window)
        unpriced.update(missing)
        if amount is not None:
            spend.append(Spend(window, amount, "USD", estimated=True))
    if unpriced:
        meta["unpricedModels"] = ",".join(sorted(unpriced))[:MAX_STRING]

    health = clean_string(payload.get("health"), 16) or HEALTH_OK
    if health not in HEALTH_STATES:
        health = HEALTH_OK

    return Snapshot(
        source_id=source_id, display_name=clean_string(payload.get("displayName"), 40) or source_id,
        observed_at=observed_at, health=health, error=clean_string(payload.get("error"), 200),
        quotas=tuple(quotas), spend=tuple(spend), counters=tuple(counters), meta=meta,
        detail={"usage": usage} if usage else {}, accent=clean_accent(payload.get("accent")),
        kind=clean_string(payload.get("kind"), 16) or "push", source_type="push",
    )


def _age_text(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"


def aggregate(source_id: str, display_name: str, pushes: Sequence[tuple[str, Snapshot]], stale_sec: int,
              now: Optional[float] = None, accent: str = "#a78bfa") -> Snapshot:
    """Fold each device's latest push into one snapshot.

    Only pushes observed in the current UTC day count (their `today` values are
    today's); a source whose every device has gone quiet reads stale.
    """
    moment = time.time() if now is None else now
    day_start = int(moment // 86400 * 86400)
    if not pushes:
        return error_snapshot(source_id, display_name, "waiting for a push from a forwarder", kind="push",
                              accent=accent, health=HEALTH_UNCONFIGURED, source_type="push")
    current = [(name, s) for name, s in pushes if s.observed_at >= day_start]
    newest = max((s for _, s in pushes), key=lambda s: s.observed_at)
    if not current:
        quotas = tuple(q for q in newest.quotas if not q.expired(moment))
        snap = Snapshot(source_id=source_id, display_name=display_name, observed_at=newest.observed_at,
                        quotas=quotas, meta={"devices": 0, "knownDevices": len(pushes)}, accent=newest.accent,
                        kind="push", source_type="push")
        return snap.marked_stale(f"no push today (last {_age_text(moment - newest.observed_at)} ago)")

    spend: dict[str, list] = {}
    for _, s in current:
        for e in s.spend:
            acc = spend.setdefault(e.window, [0.0, e.currency, False])
            acc[0] += e.amount
            acc[2] = acc[2] or e.estimated
    counters: dict[tuple, float] = {}
    for _, s in current:
        for c in s.counters:
            k = (c.key, c.unit, c.window)
            counters[k] = counters.get(k, 0.0) + c.value
    quotas: dict[tuple, Quota] = {}
    for _, s in current:
        for q in s.quotas:
            k = (q.scope, q.label)
            if k not in quotas or (q.pct or 0) > (quotas[k].pct or 0):
                quotas[k] = q
    usage: dict[tuple, dict] = {}
    for _, s in current:
        for row in s.detail.get("usage") or []:
            acc = usage.setdefault((row["model"], row["window"]), {k: 0.0 for k in TOKEN_KINDS})
            for k in TOKEN_KINDS:
                acc[k] += float(row.get(k) or 0.0)

    fresh = [(n, s) for n, s in current if moment - s.observed_at <= stale_sec]
    observed = max(s.observed_at for _, s in current)
    meta: dict[str, Any] = {
        "devices": len(current), "freshDevices": len(fresh),
        "hosts": ", ".join(sorted({n for n, _ in current}))[:MAX_STRING],
    }
    today_models = {m: sum(t.values()) for (m, w), t in usage.items() if w == WINDOW_TODAY}
    if today_models:
        meta["topModel"] = max(today_models, key=today_models.get)
    else:
        top = next((s.meta.get("topModel") for _, s in current if s.meta.get("topModel")), None)
        if top:
            meta["topModel"] = top
    unpriced = sorted({m for _, s in current for m in str(s.meta.get("unpricedModels") or "").split(",") if m})
    if unpriced:
        meta["unpricedModels"] = ",".join(unpriced)[:MAX_STRING]
    errors = [s.error for _, s in current if s.error]

    snap = Snapshot(
        source_id=source_id, display_name=display_name, observed_at=observed, health=HEALTH_OK,
        error=errors[0] if errors else None, quotas=tuple(quotas.values()),
        spend=tuple(Spend(w, v[0], v[1], v[2]) for w, v in spend.items()),
        counters=tuple(Counter(k, v, u, w) for (k, u, w), v in counters.items()), meta=meta,
        detail={"models": model_rows(usage)} if usage else {}, accent=current[0][1].accent or accent,
        kind="push", source_type="push",
    )
    if not fresh:
        return snap.marked_stale(f"no push received for {_age_text(moment - observed)}")
    return snap
