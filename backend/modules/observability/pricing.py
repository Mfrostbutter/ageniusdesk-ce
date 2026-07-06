"""LLM price book for cost observability.

Resolution is layered, highest wins: operator override > OpenRouter-fetched >
bundled default. Prices are USD per 1,000,000 tokens, split input/output. The
fetched table is refreshed from OpenRouter's public /models API (no key) on a TTL
with a last-good fallback, and cached to data/price_book.json alongside operator
overrides. OpenRouter is the breadth/estimate tier; it is NOT authoritative for
direct-provider calls (see the cost-observability spec), so every priced cost is
flagged as an estimate here.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from backend.config import DATA_DIR, settings

logger = logging.getLogger(__name__)

PRICE_BOOK_FILE = DATA_DIR / "price_book.json"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Bundled defaults (USD per 1M tokens). Minimal offline seed for the models most
# likely in use; the OpenRouter refresh layers current, broad pricing on top.
_BUNDLED: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0},
    "claude-sonnet-4-5": {"in": 3.0, "out": 15.0},
    "claude-sonnet-4": {"in": 3.0, "out": 15.0},
    "claude-3-5-sonnet": {"in": 3.0, "out": 15.0},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0},
    "claude-3-5-haiku": {"in": 0.8, "out": 4.0},
    "claude-opus-4": {"in": 15.0, "out": 75.0},
    "gpt-4o": {"in": 2.5, "out": 10.0},
    "gpt-4o-mini": {"in": 0.15, "out": 0.6},
    "gpt-4.1": {"in": 2.0, "out": 8.0},
    "gpt-4.1-mini": {"in": 0.4, "out": 1.6},
    "o4-mini": {"in": 1.1, "out": 4.4},
}

_cache: Optional[dict[str, Any]] = None


def _normalize(model: str) -> str:
    """Loose key for matching across vendor prefixes, date suffixes, and dot/dash."""
    m = (model or "").lower().strip()
    if "/" in m:
        m = m.split("/", 1)[1]  # drop vendor prefix (anthropic/..., openai/...)
    m = re.sub(r"-\d{8}$", "", m)        # drop a trailing -YYYYMMDD date stamp
    m = re.sub(r":\w+$", "", m)          # drop an OpenRouter variant tag (:free, :beta)
    m = m.replace(".", "-")              # claude-sonnet-4.6 -> claude-sonnet-4-6
    return m


def _load() -> dict[str, Any]:
    global _cache
    if _cache is None:
        if PRICE_BOOK_FILE.exists():
            try:
                _cache = json.loads(PRICE_BOOK_FILE.read_text())
            except Exception:
                logger.warning("price_book.json unreadable; treating as empty")
                _cache = {}
        else:
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


def _match(table: dict[str, Any], model: str, norm: str) -> Optional[dict[str, float]]:
    if not table:
        return None
    if model in table:
        return table[model]
    if norm in table:
        return table[norm]
    for k, v in table.items():
        if _normalize(k) == norm:
            return v
    return None


def price_for(model: str, is_local: bool = False) -> Optional[dict[str, Any]]:
    """Resolve a model to {in, out, source, estimate} per 1M tokens, or None.

    An operator override still wins over everything (a "local" model billed by
    GPU-hour elsewhere can be pinned to a real rate). Absent an override, a
    ``is_local`` model resolves to an exact $0 (``source="local"``,
    ``estimate=False``) instead of falling through to None -> "unknown".
    """
    norm = _normalize(model) if model else ""
    cache = _load()
    if model:
        # Overrides outrank the local tier so an operator can pin a real rate.
        override = _match(cache.get("overrides", {}), model, norm)
        if override:
            return {"in": float(override["in"]), "out": float(override["out"]),
                    "source": "override", "estimate": True}
    if is_local:
        # A local model's cost is exactly zero, not an approximation.
        return {"in": 0.0, "out": 0.0, "source": "local", "estimate": False}
    if not model:
        return None
    for source, table in (
        ("openrouter", cache.get("fetched", {})),
        ("bundled", _BUNDLED),
    ):
        hit = _match(table, model, norm)
        if hit:
            return {
                "in": float(hit["in"]),
                "out": float(hit["out"]),
                "source": source,
                # OpenRouter/bundled are resale/list approximations; overrides are
                # operator-pinned but still not provider-returned, so all estimate.
                "estimate": True,
            }
    return None


def status() -> dict[str, Any]:
    cache = _load()
    return {
        "fetched_models": len(cache.get("fetched", {})),
        "override_models": len(cache.get("overrides", {})),
        "bundled_models": len(_BUNDLED),
        "fetched_at": cache.get("fetched_at"),
    }


def _is_stale() -> bool:
    cache = _load()
    ts = cache.get("fetched_at")
    if not ts:
        return True
    try:
        when = datetime.fromisoformat(ts)
        age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600
        return age_h >= max(1, int(settings.agd_pricebook_refresh_hours))
    except Exception:
        return True


async def refresh(force: bool = False) -> dict[str, Any]:
    """Refresh the fetched table from OpenRouter. Last-good fallback on failure."""
    import os
    if os.environ.get("AGD_PRICEBOOK_DISABLE_REFRESH") == "1":
        return status()  # offline/test mode: bundled + any cached data only
    if not force and not _is_stale():
        return status()
    try:
        async with httpx.AsyncClient(timeout=20, verify=settings_tls_verify()) as client:
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
        except (TypeError, ValueError):
            continue
        if p_in <= 0 and p_out <= 0:
            continue
        entry = {"in": round(p_in, 4), "out": round(p_out, 4)}
        fetched[mid] = entry
        fetched.setdefault(_normalize(mid), entry)

    cache = _load()
    cache["fetched"] = fetched
    cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
    _save(cache)
    logger.info("price book refreshed: %d models from OpenRouter", len(fetched))
    return status()


def settings_tls_verify() -> bool:
    """Honor AGD_TLS_VERIFY for the outbound OpenRouter call, like other clients."""
    import os
    return os.environ.get("AGD_TLS_VERIFY", "true").lower() != "false"
