"""LLM price book, shared by Observe (n8n trace cost) and LLM Cost (usage estimates).

Layered, highest wins: operator override > local ($0 exact) > bundled current-gen
table > OpenRouter-fetched. Prices are USD per 1M tokens. Everything except a local
model is flagged as an estimate. Adapted from AgeniusDesk CE
`backend/modules/observability/pricing.py` plus the TokenPulse cache-aware table.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from backend.config import DATA_DIR, settings

logger = logging.getLogger(__name__)

PRICE_BOOK_FILE = DATA_DIR / "price_book.json"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Anthropic: cache read 0.1x input, 5m write 1.25x, 1h write 2x. Verified 2026-09-09.
_A_OPUS = {"in": 5.0, "out": 25.0, "cache_read": 0.5, "cache_write_5m": 6.25, "cache_write_1h": 10.0}
_A_OPUS_LEGACY = {"in": 15.0, "out": 75.0, "cache_read": 1.5, "cache_write_5m": 18.75, "cache_write_1h": 30.0}
_A_SONNET5 = {"in": 2.0, "out": 10.0, "cache_read": 0.2, "cache_write_5m": 2.5, "cache_write_1h": 4.0}
_A_SONNET4 = {"in": 3.0, "out": 15.0, "cache_read": 0.3, "cache_write_5m": 3.75, "cache_write_1h": 6.0}
_A_HAIKU45 = {"in": 1.0, "out": 5.0, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0}
_A_HAIKU35 = {"in": 0.8, "out": 4.0, "cache_read": 0.08, "cache_write_5m": 1.0, "cache_write_1h": 1.6}
_A_FABLE51 = {"in": 10.0, "out": 50.0, "cache_read": 0.25, "cache_write_5m": 12.5, "cache_write_1h": 20.0}
_A_FABLE5 = {"in": 10.0, "out": 50.0, "cache_read": 1.0, "cache_write_5m": 12.5, "cache_write_1h": 20.0}

# OpenAI: cached input has its own rate, no cache-write charge.
_O = lambda i, o, c=None: {"in": i, "out": o, **({"cache_read": c} if c is not None else {})}  # noqa: E731

# Keyed by model-name substring; longest matching key wins.
BUNDLED: dict[str, dict[str, float]] = {
    "claude-opus-5": _A_OPUS,
    "claude-opus-4-8": _A_OPUS, "claude-opus-4-7": _A_OPUS,
    "claude-opus-4-6": _A_OPUS, "claude-opus-4-5": _A_OPUS,
    "claude-opus-4-1": _A_OPUS_LEGACY, "claude-opus-4": _A_OPUS_LEGACY,
    "claude-sonnet-5": _A_SONNET5,
    "claude-sonnet-4-6": _A_SONNET4, "claude-sonnet-4-5": _A_SONNET4, "claude-sonnet-4": _A_SONNET4,
    "claude-3-5-sonnet": _A_SONNET4, "claude-3-7-sonnet": _A_SONNET4,
    "claude-haiku-4-5": _A_HAIKU45, "claude-haiku-3-5": _A_HAIKU35, "claude-3-5-haiku": _A_HAIKU35,
    "claude-fable-5-1": _A_FABLE51, "claude-fable-5": _A_FABLE5,
    "opus": _A_OPUS, "sonnet": _A_SONNET4, "haiku": _A_HAIKU45, "fable": _A_FABLE51,
    "gpt-5-1": _O(1.25, 10.0, 0.125), "gpt-5-mini": _O(0.25, 2.0, 0.025),
    "gpt-5-nano": _O(0.05, 0.4, 0.005), "gpt-5": _O(1.25, 10.0, 0.125),
    "gpt-4-1-mini": _O(0.4, 1.6, 0.1), "gpt-4-1-nano": _O(0.1, 0.4, 0.025), "gpt-4-1": _O(2.0, 8.0, 0.5),
    "gpt-4o-mini": _O(0.15, 0.6, 0.075), "gpt-4o": _O(2.5, 10.0, 1.25),
    "o4-mini": _O(1.1, 4.4, 0.275), "o3": _O(2.0, 8.0, 0.5), "o1-mini": _O(1.1, 4.4), "o1": _O(15.0, 60.0, 7.5),
    "gpt-4-turbo": _O(10.0, 30.0), "gpt-3-5-turbo": _O(0.5, 1.5),
}
_BUNDLED_KEYS = sorted(BUNDLED, key=len, reverse=True)

_cache: Optional[dict[str, Any]] = None


def normalize(model: str) -> str:
    """Loose key: drop vendor prefix, -YYYYMMDD suffix, :tag, and dots."""
    m = (model or "").lower().strip()
    if "/" in m:
        m = m.split("/", 1)[1]
    m = re.sub(r"-\d{8}$", "", m)
    m = re.sub(r":\w+$", "", m)
    return m.replace(".", "-")


def _load() -> dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(PRICE_BOOK_FILE.read_text()) if PRICE_BOOK_FILE.exists() else {}
        except Exception:
            logger.warning("price_book.json unreadable; treating as empty")
            _cache = {}
    return _cache


def _save(cache: dict[str, Any]) -> None:
    global _cache
    _cache = cache
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PRICE_BOOK_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        logger.warning("could not persist price_book.json: %s", e)


def _exact(table: dict[str, Any], model: str, norm: str) -> Optional[dict[str, float]]:
    if not table:
        return None
    if model in table:
        return table[model]
    if norm in table:
        return table[norm]
    for k, v in table.items():
        if normalize(k) == norm:
            return v
    return None


def _bundled(norm: str) -> Optional[dict[str, float]]:
    for key in _BUNDLED_KEYS:
        if key in norm:
            return BUNDLED[key]
    return None


def _entry(hit: dict[str, Any], source: str, estimate: bool) -> dict[str, Any]:
    out = {k: float(v) for k, v in hit.items() if isinstance(v, (int, float))}
    out.setdefault("in", 0.0)
    out.setdefault("out", 0.0)
    return {**out, "source": source, "estimate": estimate}


def price_for(model: str, is_local: bool = False) -> Optional[dict[str, Any]]:
    """Resolve a model to {in, out, [cache_*], source, estimate} per 1M tokens, or None."""
    norm = normalize(model) if model else ""
    cache = _load()
    if model:
        override = _exact(cache.get("overrides", {}), model, norm)
        if override:
            return _entry(override, "override", True)
    if is_local:
        return {"in": 0.0, "out": 0.0, "source": "local", "estimate": False}
    if not model:
        return None
    hit = _bundled(norm)
    if hit:
        return _entry(hit, "bundled", True)
    hit = _exact(cache.get("fetched", {}), model, norm)
    if hit:
        return _entry(hit, "openrouter", True)
    return None


def estimate_cost(model: str, tokens: dict[str, float], is_local: bool = False) -> Optional[float]:
    """USD for a token breakdown {input, output, cache_read, cache_write_5m, cache_write_1h, cache_write}.

    None when the model is unpriced, so callers flag it instead of reporting a false zero.
    """
    p = price_for(model, is_local=is_local)
    if p is None:
        return None

    def per_m(kind: str) -> float:
        return float(tokens.get(kind, 0.0) or 0.0) / 1_000_000.0

    cw5 = p.get("cache_write_5m", p["in"])
    cw1 = p.get("cache_write_1h", cw5)
    cost = (per_m("input") * p["in"]
            + per_m("output") * p["out"]
            + per_m("cache_read") * p.get("cache_read", p["in"])
            + per_m("cache_write_5m") * cw5
            + per_m("cache_write_1h") * cw1
            + per_m("cache_write") * cw5)
    return round(cost, 6)


def set_override(model: str, price_in: float, price_out: float) -> None:
    cache = _load()
    cache.setdefault("overrides", {})[model] = {"in": float(price_in), "out": float(price_out)}
    _save(cache)


def clear_override(model: str) -> bool:
    cache = _load()
    hit = cache.get("overrides", {}).pop(model, None) is not None
    if hit:
        _save(cache)
    return hit


def status() -> dict[str, Any]:
    cache = _load()
    return {
        "fetched_models": len(cache.get("fetched", {})),
        "override_models": len(cache.get("overrides", {})),
        "overrides": cache.get("overrides", {}),
        "bundled_models": len(BUNDLED),
        "fetched_at": cache.get("fetched_at"),
    }


def _is_stale() -> bool:
    ts = _load().get("fetched_at")
    if not ts:
        return True
    try:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600
        return age_h >= max(1, int(settings.agd_pricebook_refresh_hours))
    except Exception:
        return True


def _tls_verify() -> bool:
    return os.environ.get("AGD_TLS_VERIFY", "true").lower() != "false"


async def refresh(force: bool = False) -> dict[str, Any]:
    """Refresh the fetched table from OpenRouter's public /models. Last-good on failure."""
    if os.environ.get("AGD_PRICEBOOK_DISABLE_REFRESH") == "1":
        return status()
    if not force and not _is_stale():
        return status()
    try:
        async with httpx.AsyncClient(timeout=20, verify=_tls_verify()) as client:
            resp = await client.get(OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            models = (resp.json() or {}).get("data", []) or []
    except Exception as e:
        logger.warning("price book refresh failed (keeping last-good): %s", e)
        return status()

    fetched: dict[str, dict[str, float]] = {}
    for m in models:
        mid = m.get("id") or ""
        pr = m.get("pricing") or {}
        try:
            p_in = float(pr.get("prompt", "0")) * 1e6
            p_out = float(pr.get("completion", "0")) * 1e6
            p_cr = float(pr.get("input_cache_read") or 0) * 1e6
        except (TypeError, ValueError):
            continue
        if p_in <= 0 and p_out <= 0:
            continue
        entry = {"in": round(p_in, 4), "out": round(p_out, 4)}
        if p_cr > 0:
            entry["cache_read"] = round(p_cr, 4)
        fetched[mid] = entry
        fetched.setdefault(normalize(mid), entry)

    cache = _load()
    cache["fetched"] = fetched
    cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
    _save(cache)
    logger.info("price book refreshed: %d models from OpenRouter", len(fetched))
    return status()
