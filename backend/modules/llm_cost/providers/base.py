"""Source plugin contract: context, HTTP helper, base provider class."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

import httpx

from backend.modules.llm_cost.models import HEALTH_UNCONFIGURED, Snapshot, error_snapshot

MODE_POLL = "poll"
MODE_PUSH = "push"
MAX_PAGES = 12


class ProviderError(Exception):
    """A poll failed in a way worth showing the operator."""


class ConfigError(ProviderError):
    """The source exists but is not usably configured."""


def resolve_secret(ref: Any) -> Optional[str]:
    """Resolve a `$NAME` ref via env then the secrets store. None when unresolved."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    import os

    from backend.config import decrypt_value

    name = ref.strip().lstrip("$")
    if not name:
        return None
    env_val = os.environ.get(name.split(".", 1)[0]) if "." not in name else None
    if env_val:
        return env_val
    try:
        value = decrypt_value("$" + name)
    except Exception:
        return None
    # decrypt_value echoes the bare name on a miss.
    if not value or value == name or value.startswith("$"):
        return None
    return value


class Http:
    """Thin async JSON client. Errors never carry request headers."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None, timeout: float = 20.0) -> None:
        self._client = client
        self.timeout = timeout

    async def request_json(self, method: str, url: str, headers: Optional[dict] = None,
                           params: Optional[dict] = None) -> Any:
        from backend.net import tls_verify

        try:
            if self._client is not None:
                resp = await self._client.request(method, url, headers=headers, params=params)
            else:
                async with httpx.AsyncClient(timeout=self.timeout, verify=tls_verify()) as client:
                    resp = await client.request(method, url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{type(exc).__name__} reaching {httpx.URL(url).host}") from exc
        if resp.status_code < 200 or resp.status_code >= 300:
            raise ProviderError(f"HTTP {resp.status_code}: {_err_detail(resp)}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"bad JSON from {httpx.URL(url).path}") from exc

    async def get_json(self, url: str, headers: Optional[dict] = None, params: Optional[dict] = None) -> Any:
        return await self.request_json("GET", url, headers=headers, params=params)


def _err_detail(resp: httpx.Response) -> str:
    try:
        parsed = resp.json()
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "")[:200]
        if isinstance(err, str):
            return err[:200]
    except ValueError:
        pass
    return (resp.text or "")[:160]


@dataclass
class ProviderContext:
    """Everything a provider is handed at construction."""

    source_id: str
    source_type: str = ""
    display_name: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    interval_sec: int = 0
    secret: Callable[[Any], Optional[str]] = resolve_secret
    http: Http = field(default_factory=Http)

    def option(self, key: str, default: Any = None) -> Any:
        value = self.options.get(key, default)
        return default if value is None or value == "" else value

    def require_secret(self, *refs: Any) -> str:
        """First resolvable ref wins. Raises ConfigError naming what to set."""
        names = [r for r in refs if isinstance(r, str) and r]
        for ref in names:
            value = self.secret(ref)
            if value:
                return value
        shown = ", ".join(r if r.startswith("$") else "$" + r for r in names) or "a secret reference"
        raise ConfigError(f"set secret {shown}")


class Provider:
    """Base class for every usage source."""

    TYPE: str = ""
    DISPLAY_NAME: str = ""
    KIND: str = "api"
    ACCENT: str = "#38bdf8"
    MODE: str = MODE_POLL
    DEFAULT_INTERVAL: int = 300
    MIN_INTERVAL: int = 30
    DESCRIPTION: str = ""
    FIELDS: Sequence[dict] = ()
    DEV_ONLY: bool = False

    def __init__(self, context: ProviderContext) -> None:
        self.context = context
        self.source_id = context.source_id
        self.display_name = context.display_name or self.DISPLAY_NAME or self.TYPE
        self.accent = str(context.option("accent", self.ACCENT))

    @property
    def interval_sec(self) -> int:
        wanted = self.context.interval_sec or self.DEFAULT_INTERVAL
        try:
            wanted = int(wanted)
        except (TypeError, ValueError):
            wanted = self.DEFAULT_INTERVAL
        return max(self.MIN_INTERVAL, wanted)

    def validate(self) -> None:
        """Raise ConfigError when the source cannot possibly work."""

    async def fetch(self) -> Snapshot:
        raise NotImplementedError

    def snapshot(self, **kwargs: Any) -> Snapshot:
        kwargs.setdefault("source_id", self.source_id)
        kwargs.setdefault("display_name", self.display_name)
        kwargs.setdefault("observed_at", int(time.time()))
        kwargs.setdefault("kind", self.KIND)
        kwargs.setdefault("accent", self.accent)
        kwargs.setdefault("source_type", self.TYPE)
        return Snapshot(**kwargs)

    def unconfigured(self, message: str) -> Snapshot:
        return error_snapshot(self.source_id, self.display_name, message, kind=self.KIND,
                              accent=self.accent, health=HEALTH_UNCONFIGURED, source_type=self.TYPE)

    def float_option(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.context.option(key, default))
        except (TypeError, ValueError):
            return default

    def list_option(self, key: str) -> list[str]:
        raw = self.context.option(key, [])
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.split(",")]
        return [str(x) for x in (raw or []) if str(x).strip()]

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "type": cls.TYPE, "displayName": cls.DISPLAY_NAME, "kind": cls.KIND, "accent": cls.ACCENT,
            "mode": cls.MODE, "defaultInterval": cls.DEFAULT_INTERVAL, "minInterval": cls.MIN_INTERVAL,
            "description": cls.DESCRIPTION, "fields": list(cls.FIELDS), "devOnly": cls.DEV_ONLY,
        }


# ── shared helpers ─────────────────────────────────────────────────────────


def num(value: Any) -> float:
    """Lenient float; junk reads as 0 for summing provider rows."""
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def number_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def month_start() -> dt.datetime:
    return utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def today_start() -> dt.datetime:
    return utc_now().replace(hour=0, minute=0, second=0, microsecond=0)


def next_month_epoch() -> int:
    now = utc_now()
    nxt = (now.replace(day=28) + dt.timedelta(days=7)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(nxt.timestamp())


def rfc3339(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def paged(http: Http, url: str, params: dict, headers: Optional[dict] = None) -> list[dict]:
    """Follow has_more/next_page for admin report endpoints."""
    buckets: list[dict] = []
    page: Optional[str] = None
    for _ in range(MAX_PAGES):
        query = dict(params)
        if page:
            query["page"] = page
        payload = await http.get_json(url, headers=headers, params=query)
        if isinstance(payload, list):
            buckets.extend(b for b in payload if isinstance(b, dict))
            break
        if not isinstance(payload, dict):
            raise ProviderError(f"unexpected response from {httpx.URL(url).path}")
        buckets.extend(b for b in payload.get("data", []) if isinstance(b, dict))
        if not payload.get("has_more"):
            break
        page = payload.get("next_page")
        if not page:
            break
    return buckets


def zero_tokens() -> dict[str, float]:
    return {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0}


def add_tokens(into: dict[str, float], tokens: Mapping[str, float]) -> None:
    for k, v in tokens.items():
        into[k] = into.get(k, 0.0) + float(v or 0.0)


def model_rows(per_model: Mapping[tuple[str, str], Mapping[str, float]], basis: str = "estimated") -> list[dict]:
    """Per-model rows with a price-book estimate. Unpriced models carry cost None."""
    from backend import pricing

    rows = []
    for (model, window), tok in per_model.items():
        cache = tok.get("cache_read", 0.0) + tok.get("cache_write_5m", 0.0) + tok.get("cache_write_1h", 0.0)
        cost = pricing.estimate_cost(model, dict(tok))
        rows.append({
            "model": model, "window": window, "inputTokens": tok.get("input", 0.0),
            "outputTokens": tok.get("output", 0.0), "cacheTokens": cache,
            "tokens": tok.get("input", 0.0) + tok.get("output", 0.0) + cache,
            "cost": cost, "costBasis": basis if cost is not None else "unpriced",
        })
    rows.sort(key=lambda r: (r["cost"] is None, -(r["cost"] or 0.0)))
    return rows


def short_id(gid: Any) -> str:
    s = str(gid or "")
    return s if len(s) <= 14 else s[:6] + "…" + s[-6:]


def build_groups(dim: str, names: Mapping[str, str], cost_map: Mapping[Any, Mapping[str, float]],
                 usage_map: Mapping[Any, Mapping[str, Mapping[str, float]]]) -> list[dict]:
    """Actual per-group cost where the provider reports it, else a token estimate."""
    out = []
    for gid in set(cost_map) | set(usage_map):
        per_model = {(m, "mtd"): tok for m, tok in (usage_map.get(gid) or {}).items()}
        models = model_rows(per_model)
        if gid in cost_map:
            cost, basis = round(cost_map[gid]["mtd"], 6), "actual"
            spend = {"mtd": cost, "today": round(cost_map[gid].get("today", 0.0), 6)}
        else:
            priced = [m["cost"] for m in models if m["cost"] is not None]
            cost, basis = (round(sum(priced), 6) if priced else None), "estimated"
            spend = {"mtd": cost}
        if not cost and not models:
            continue
        name = names.get(str(gid)) or short_id(gid)
        out.append({"dim": dim, "id": str(gid), "name": name, "spend": spend, "cost": cost,
                    "costBasis": basis, "models": models})
    out.sort(key=lambda g: -(g["cost"] or 0.0))
    return out


async def safe_names(http: Http, url: str, headers: Optional[dict] = None) -> dict[str, str]:
    """Best-effort id -> display name map from an admin list endpoint."""
    try:
        rows = await paged(http, url, {"limit": 100}, headers)
    except ProviderError:
        return {}
    out: dict[str, str] = {}
    for r in rows:
        rid = r.get("id")
        if rid:
            out[str(rid)] = str(r.get("name") or r.get("display_name") or rid)[:60]
    return out


async def safe_groups(coro) -> tuple[list, dict]:
    """A breakdown is a bonus, never fatal."""
    try:
        return await coro
    except Exception:  # noqa: BLE001
        return [], {}
