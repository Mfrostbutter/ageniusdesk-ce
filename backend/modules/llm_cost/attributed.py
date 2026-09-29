"""Attributed spend: what n8n workflows (Observe spans) and AGD agents consumed,
and how that reconciles against provider-billed spend per day.

Every query is defensive: the Observe tables may be empty or absent.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any, Optional, Sequence

from backend.database import get_db

logger = logging.getLogger(__name__)

MAX_AGENT_ROWS = 5000


def provider_of_model(model: str) -> str:
    """Heuristic billing provider for a model id."""
    m = (model or "").lower()
    if "/" in m:
        return "openrouter"
    if "claude" in m or m.startswith(("opus", "sonnet", "haiku")):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-embedding", "davinci", "whisper", "dall-e")):
        return "openai"
    return "other"


async def _fetch(db, sql: str, params=()) -> list:
    async with db.execute(sql, tuple(params)) as cur:
        return await cur.fetchall()


async def _exists(db, table: str) -> bool:
    try:
        rows = await _fetch(db, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
        return bool(rows)
    except Exception:
        return False


async def _agent_fleet_db():
    """Read-only handle on the Agent Fleet's own database, or None when absent."""
    import aiosqlite

    from backend.modules.agent_fleet import storage as fleet_storage

    path = fleet_storage.DB_PATH
    if not path.exists():
        return None
    db = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = aiosqlite.Row
    return db


def _day_utc(epoch: float, tz_offset_sec: int = 0) -> str:
    return dt.datetime.fromtimestamp(epoch + tz_offset_sec, dt.timezone.utc).strftime("%Y-%m-%d")


async def otel_rows(days: int, instance_ids: Optional[Sequence[str]], tz_offset_sec: int = 0) -> dict:
    """n8n LLM spans grouped by instance, workflow (from the root span), model, and day."""
    if instance_ids is not None and not instance_ids:
        return {"available": True, "rows": []}
    db = await get_db()
    if not await _exists(db, "otel_spans"):
        return {"available": False, "rows": [], "reason": "Observe traces are not enabled on this install"}
    since_ns = int((time.time() - int(days) * 86400) * 1_000_000_000)
    scope_sql = ""
    params: list[Any] = [since_ns, int(tz_offset_sec), since_ns]
    if instance_ids is not None:
        marks = ",".join("?" for _ in instance_ids)
        scope_sql = f" AND COALESCE(NULLIF(r.instance_id, ''), s.instance_id, '') IN ({marks})"
        params.extend(instance_ids)
    sql = (
        "WITH ranked AS ("
        "  SELECT trace_id, instance_id, workflow_id, workflow_name,"
        "         ROW_NUMBER() OVER (PARTITION BY trace_id ORDER BY start_ns) AS rnk"
        "  FROM otel_spans WHERE name = 'workflow.execute' AND start_ns >= ?"
        "), roots AS (SELECT trace_id, instance_id, workflow_id, workflow_name FROM ranked WHERE rnk = 1) "
        "SELECT COALESCE(NULLIF(r.instance_id, ''), s.instance_id, '') AS instance_id,"
        "  COALESCE(r.workflow_id, '') AS workflow_id, COALESCE(r.workflow_name, '') AS workflow_name,"
        "  s.model AS model,"
        "  strftime('%Y-%m-%d', (s.start_ns / 1000000000) + ?, 'unixepoch') AS day,"
        "  SUM(COALESCE(s.cost_usd, 0)) AS cost,"
        "  SUM(CASE WHEN s.cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced,"
        "  SUM(COALESCE(s.tokens_in, 0)) AS tokens_in, SUM(COALESCE(s.tokens_out, 0)) AS tokens_out,"
        "  MAX(CASE WHEN CAST(s.cost_is_estimate AS TEXT) IN ('1', 'true', 't') THEN 1 ELSE 0 END) AS est,"
        "  COUNT(*) AS calls "
        "FROM otel_spans s LEFT JOIN roots r ON r.trace_id = s.trace_id "
        "WHERE s.start_ns >= ? AND s.model IS NOT NULL AND s.model <> ''" + scope_sql + " "
        "GROUP BY 1, 2, 3, 4, 5"
    )
    try:
        rows = await _fetch(db, sql, tuple(params))
    except Exception as exc:
        logger.warning("llm-cost otel attribution query failed: %s", exc)
        return {"available": False, "rows": [], "reason": "Observe trace schema is not readable"}
    out = []
    for r in rows:
        out.append({
            "instanceId": r["instance_id"], "workflowId": r["workflow_id"], "workflowName": r["workflow_name"],
            "model": r["model"], "day": r["day"], "cost": float(r["cost"] or 0.0),
            "unpricedCalls": int(r["unpriced"] or 0), "tokensIn": float(r["tokens_in"] or 0),
            "tokensOut": float(r["tokens_out"] or 0), "estimated": bool(r["est"]), "calls": int(r["calls"] or 0),
            "provider": provider_of_model(r["model"]),
        })
    return {"available": True, "rows": out}


async def langgraph_rows(days: int) -> dict:
    """Agent Fleet runs; CE keeps them in data/agentfleet.db, not dashboard.db."""
    try:
        db = await _agent_fleet_db()
    except Exception as exc:  # noqa: BLE001 - the extra may be absent
        logger.debug("llm-cost agent fleet db unavailable: %s", exc)
        db = None
    if db is None:
        return {"available": False, "rows": []}
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        if not await _exists(db, "langgraph_runs"):
            return {"available": False, "rows": []}
        rows = await _fetch(db,
            "SELECT agent_id, COALESCE(model, '') AS model, substr(created_at, 1, 10) AS day,"
            " SUM(total_cost) AS cost, SUM(total_tokens) AS tokens, COUNT(*) AS runs,"
            " SUM(CASE WHEN total_cost > 0 THEN 0 ELSE 1 END) AS unpriced"
            " FROM langgraph_runs WHERE created_at >= ? GROUP BY 1, 2, 3", (cutoff,))
    except Exception as exc:
        logger.warning("llm-cost langgraph attribution query failed: %s", exc)
        return {"available": False, "rows": []}
    finally:
        await db.close()
    return {"available": True, "rows": [
        {"agentId": r["agent_id"], "model": r["model"], "day": r["day"], "cost": float(r["cost"] or 0.0),
         "tokens": float(r["tokens"] or 0), "runs": int(r["runs"] or 0), "unpricedRuns": int(r["unpriced"] or 0),
         "provider": provider_of_model(r["model"])} for r in rows]}


def _usage_tokens(usage: dict) -> dict[str, float]:
    def n(key: str) -> float:
        v = usage.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

    return {"input": n("input_tokens") or n("prompt_tokens"), "output": n("output_tokens") or n("completion_tokens"),
            "cache_read": n("cache_read_input_tokens"), "cache_write_5m": n("cache_creation_input_tokens")}


async def agent_session_rows(days: int) -> dict:
    """Claude Code hook-captured agent runs; tokens always, cost only when a model is known."""
    from backend import pricing

    db = await get_db()
    if not await _exists(db, "agent_runs"):
        return {"available": False, "rows": []}
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = await _fetch(db,
            "SELECT subagent_type, token_usage, substr(started_at, 1, 10) AS day FROM agent_runs"
            " WHERE started_at >= ? AND token_usage <> '' ORDER BY started_at DESC LIMIT ?",
            (cutoff, MAX_AGENT_ROWS))
    except Exception as exc:
        logger.warning("llm-cost agent_runs attribution query failed: %s", exc)
        return {"available": False, "rows": []}
    grouped: dict[tuple, dict] = {}
    for r in rows:
        try:
            usage = json.loads(r["token_usage"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(usage.get("usage"), dict):
            usage = {**usage["usage"], "model": usage.get("model") or usage["usage"].get("model")}
        if not isinstance(usage, dict):
            continue
        tok = _usage_tokens(usage)
        model = str(usage.get("model") or "")
        key = (r["subagent_type"] or "agent", model, r["day"])
        g = grouped.setdefault(key, {"tokens": 0.0, "cost": 0.0, "priced": False, "runs": 0})
        g["tokens"] += sum(tok.values())
        g["runs"] += 1
        cost = pricing.estimate_cost(model, tok) if model else None
        if cost is not None:
            g["cost"] += cost
            g["priced"] = True
    return {"available": True, "rows": [
        {"agent": k[0], "model": k[1], "day": k[2], "tokens": g["tokens"],
         "cost": round(g["cost"], 6) if g["priced"] else None, "estimated": True, "runs": g["runs"]}
        for k, g in grouped.items()]}


def _rollup(rows: list[dict], key_fn, extra=None) -> list[dict]:
    out: dict[Any, dict] = {}
    for r in rows:
        k = key_fn(r)
        acc = out.setdefault(k, {"cost": 0.0, "tokensIn": 0.0, "tokensOut": 0.0, "calls": 0, "estimated": False,
                                 "unpricedCalls": 0})
        acc["cost"] += r["cost"]
        acc["tokensIn"] += r.get("tokensIn", 0.0)
        acc["tokensOut"] += r.get("tokensOut", 0.0)
        acc["calls"] += r.get("calls", 0)
        acc["unpricedCalls"] += r.get("unpricedCalls", 0)
        acc["estimated"] = acc["estimated"] or r.get("estimated", False)
        if extra:
            extra(acc, r)
    return [{"key": k, **{kk: (round(vv, 6) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
            for k, v in sorted(out.items(), key=lambda kv: -kv[1]["cost"])]


def summarize_otel(rows: list[dict], instance_names: dict[str, str]) -> dict:
    def _wf_extra(acc, r):
        acc["instanceId"] = r["instanceId"]
        acc["workflowName"] = r["workflowName"] or acc.get("workflowName") or ""

    by_instance = _rollup(rows, lambda r: r["instanceId"])
    for row in by_instance:
        row["name"] = instance_names.get(row["key"], row["key"] or "unknown")
    by_workflow = _rollup(rows, lambda r: f"{r['instanceId']}:{r['workflowId']}", _wf_extra)
    for row in by_workflow:
        row["instanceName"] = instance_names.get(row.get("instanceId", ""), row.get("instanceId", ""))
        row["workflowName"] = row.get("workflowName") or (row["key"].split(":", 1)[1] or "(no root span)")
    by_model = _rollup(rows, lambda r: r["model"])
    return {"byInstance": by_instance, "byWorkflow": by_workflow[:200], "byModel": by_model,
            "total": round(sum(r["cost"] for r in rows), 6)}


def billed_by_provider_day(sources: list[dict], snapshots: dict, history: list[dict], days: int,
                           tz_offset_sec: int = 0) -> dict[str, dict[str, float]]:
    """provider -> day -> billed USD. Provider-reported daily trend wins over sampled history."""
    from backend.modules.llm_cost.providers import BILLING_PROVIDER

    cutoff = _day_utc(time.time() - int(days) * 86400, tz_offset_sec)
    by_source_type = {s["id"]: s["type"] for s in sources}
    out: dict[str, dict[str, float]] = {}
    trend_days: dict[str, set] = {}
    for sid, snap in snapshots.items():
        provider = BILLING_PROVIDER.get(by_source_type.get(sid, ""))
        if not provider or snap is None:
            continue
        for row in (snap.detail or {}).get("trend") or []:
            day = row.get("date")
            if not day or day < cutoff:
                continue
            out.setdefault(provider, {})
            out[provider][day] = out[provider].get(day, 0.0) + float(row.get("amount") or 0.0)
            trend_days.setdefault(sid, set()).add(day)
    for row in history:
        provider = BILLING_PROVIDER.get(by_source_type.get(row["sourceId"], ""))
        if not provider or row["day"] in trend_days.get(row["sourceId"], set()) or row["day"] < cutoff:
            continue
        out.setdefault(provider, {})
        out[provider][row["day"]] = out[provider].get(row["day"], 0.0) + float(row["amount"] or 0.0)
    return out


def reconcile(billed: dict[str, dict[str, float]], attributed_rows: list[dict]) -> list[dict]:
    """Per provider per day where both sides exist: billed, attributed, the gap, coverage."""
    attributed: dict[str, dict[str, float]] = {}
    for r in attributed_rows:
        if r.get("cost") is None:
            continue
        p = attributed.setdefault(r["provider"], {})
        p[r["day"]] = p.get(r["day"], 0.0) + float(r["cost"])
    out = []
    for provider, days in billed.items():
        for day, amount in days.items():
            att = attributed.get(provider, {}).get(day)
            if att is None:
                continue
            out.append({"provider": provider, "day": day, "billed": round(amount, 6), "attributed": round(att, 6),
                        "unattributed": round(amount - att, 6),
                        "coveragePct": round(att / amount * 100.0, 1) if amount > 0 else None})
    out.sort(key=lambda r: (r["day"], r["provider"]), reverse=True)
    return out
