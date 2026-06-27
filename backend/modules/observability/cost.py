"""Cost enrichment: pull LLM token usage from n8n run-data and price it.

n8n's OTel spans carry no token/cost data; the usage lives in the execution
run-data at ``data.ai_languageModel[0][0].json.tokenUsage`` on each AI
language-model node (one run -> one node.execute span). This fetches the
execution for a captured trace, prices each run via the price book, and writes
per-span cost. Idempotent and best-effort: only the active instance's executions
are fetchable through the n8n client, and an already-priced trace is skipped.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from backend.config import get_active_instance_id
from backend.modules.n8n_proxy import client as n8n_client

from . import pricing, storage

logger = logging.getLogger(__name__)


def _model_for(run: dict) -> str:
    for path in ("inputOverride", "data"):
        try:
            return str(run[path]["ai_languageModel"][0][0]["json"]["options"]["model"] or "")
        except Exception:
            continue
    return ""


def _token_usage(run: dict):
    try:
        tu = run["data"]["ai_languageModel"][0][0]["json"].get("tokenUsage")
    except Exception:
        return None
    return tu if isinstance(tu, dict) else None


async def enrich_trace(trace_id: str) -> int:
    """Enrich one trace with per-AI-span cost. Returns spans priced (0 if skipped)."""
    if await storage.has_cost(trace_id):
        return 0
    spans = await storage.get_trace(trace_id)
    if not spans:
        return 0
    exec_id = next((s["execution_id"] for s in spans if s.get("execution_id")), "")
    inst = next((s["instance_id"] for s in spans if s.get("instance_id")), "")
    if not exec_id:
        return 0
    # Only the active instance's run-data is fetchable through the n8n client.
    if inst and inst != get_active_instance_id():
        return 0
    try:
        # Bound the fetch: run-data for a big execution can be multi-MB; never let
        # a slow fetch stall the trace-open request. Best-effort, retries next open.
        raw = await asyncio.wait_for(n8n_client.get_execution_raw(exec_id), timeout=8.0)
    except Exception as e:
        logger.debug("cost enrich: fetch failed/slow for exec %s: %s", exec_id, e)
        return 0
    run_data = ((raw or {}).get("data") or {}).get("resultData", {}).get("runData", {})
    if not run_data:
        return 0

    # node name -> its node.execute spans in execution order (one span per run).
    by_node: dict[str, list[dict]] = {}
    for s in sorted(spans, key=lambda x: x["start_ns"]):
        nn = (s.get("attributes") or {}).get("n8n.node.name")
        if nn:
            by_node.setdefault(nn, []).append(s)

    now = datetime.now(timezone.utc).isoformat()
    updates: list[dict] = []
    for node, runs in run_data.items():
        node_spans = by_node.get(node, [])
        if not node_spans:
            continue
        for i, run in enumerate(runs):
            tu = _token_usage(run)
            if not tu:
                continue
            tin = int(tu.get("promptTokens") or 0)
            tout = int(tu.get("completionTokens") or 0)
            if not (tin or tout):
                continue
            model = _model_for(run)
            pr = pricing.price_for(model)
            if pr:
                cost = round(tin / 1e6 * pr["in"] + tout / 1e6 * pr["out"], 6)
                p_in, p_out, p_src, est = pr["in"], pr["out"], pr["source"], 1 if pr["estimate"] else 0
            else:
                cost, p_in, p_out, p_src, est = None, None, None, "unknown", 1
            span = node_spans[i] if i < len(node_spans) else node_spans[-1]
            updates.append({
                "span_id": span["span_id"],
                "model": model,
                "tokens_in": tin,
                "tokens_out": tout,
                "cost_usd": cost,
                "cost_source": "n8n-rundata",
                "price_in_per_mtok": p_in,
                "price_out_per_mtok": p_out,
                "price_source": p_src,
                "cost_is_estimate": est,
                "priced_at": now,
            })
    n = await storage.set_costs(updates)
    if n:
        logger.info("cost enrich: priced %d AI spans for exec %s", n, exec_id)
    return n
