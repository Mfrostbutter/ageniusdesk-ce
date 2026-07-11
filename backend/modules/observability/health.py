"""Silent-failure detection: flag "green but broken" runs.

n8n marks an execution `success` even when a node failed under Continue-On-Fail
(or a half-wired error output). The failure survives only in the execution
run-data and in per-node item counts, never in span status: the OTel exporter
reports the continued node `OK` (confirmed 2026-07-07, spec
2026-07-07-silent-failure-detection). This enriches captured traces with per-node
health so a run the execution log called "success" surfaces loud.

Two detectors, split by cost:

- **Low output (mode 3)** — read ``n8n.node.items.output``/``.input`` off the
  span (free; no n8n round-trip) and classify against the node's own history
  (``_classify_low_output``). A zero is ``EMPTY`` (informational) unless the node
  is a historically reliable producer that had input and dropped it, in which
  case it is ``LOW`` (alertable) — this also catches magnitude drops (200 -> 3),
  not just zeros. Idle pollers and cold-start stay quiet.
- **Silent error (modes 1/2)** — the demoted error has no span signal, so fetch
  run-data once per execution and read, in priority order:
  0. ``runData[node][i].continuation`` — the typed continued-error rollup a
     patched n8n records (sound: fires only on a real swallow). Preferred.
  1. ``runData[node][i].executionStatus == "error"``
  2. ``runData[node][i].error`` (an object with ``.message``)
  3. item-level ``runData[node][i].data.main[*][*].json.error`` (object or string)
     — legacy content-scan, unsound (a legitimate ``error`` field trips it), so
     it is gated behind ``agd_health_scan_loose_json_error`` and only consulted
     when the sound signals above are silent.

A trace is a **silent failure** when its root ``workflow.execute`` reports
``n8n.execution.status = success`` yet a node span resolves to ``ERROR`` or
``LOW``.
Idempotent and best-effort, mirroring ``cost.py``: only the active instance's
run-data is fetchable, and an already-checked trace is skipped.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from statistics import median

from backend.config import settings
from backend.modules.n8n_proxy import client as n8n_client
from backend.websocket import manager

from . import storage

logger = logging.getLogger(__name__)

# Distinct error class for silent failures written into the errors table, so the
# Overview / Insights / Executions-Errors views (all driven by that table) can
# style and count them apart from loud n8n errors. Mirrored in the frontend
# (components/error-item.js) and the insights aggregator; keep the string in sync.
SILENT_ERROR_TYPE = "Silent failure"


def _normalize_error(raw) -> tuple[str, str, int | None]:
    """Collapse n8n's inconsistent error shapes to (type, summary, http_status).

    HTTP failures arrive as an object with ``.status``; a thrown Code error
    arrives as a bare string. Same meaning, different shape.
    """
    if isinstance(raw, dict):
        etype = raw.get("name") or raw.get("code") or "error"
        summary = raw.get("message") or raw.get("description") or ""
        http = raw.get("status") if raw.get("status") is not None else raw.get("httpCode")
        try:
            http = int(http) if http is not None else None
        except (TypeError, ValueError):
            http = None
        return str(etype)[:80], str(summary)[:500], http
    return "thrown", str(raw)[:500], None


def _continuation_error(run: dict):
    """Return (type, summary, http) from the typed continued-error rollup, else None.

    A patched n8n records ``taskData.continuation`` (``{count, first, byType}``)
    when a node swallowed a per-item error under Continue-On-Fail. This is the
    sound signal: the engine only folds a node's typed ``INodeExecutionData.error``
    marker into it, never a loose ``json.error`` a node emitted on purpose. So it
    fires on a real swallow and stays quiet on a node that legitimately outputs a
    field named ``error``. Prefer it whenever present.
    """
    cont = run.get("continuation")
    if not isinstance(cont, dict):
        return None
    count = cont.get("count")
    if not count:
        return None
    first = cont.get("first") or {}
    etype = first.get("errorType") or "error"
    http = first.get("httpCode")
    try:
        http = int(http) if http is not None else None
    except (TypeError, ValueError):
        http = None
    plural = "s" if count != 1 else ""
    return str(etype)[:80], f"continued past {count} item error{plural}", http


def _node_error_from_run(run: dict):
    """Return (type, summary, http) if this run errored, else None.

    Signal priority, sound before heuristic:
    1. ``taskData.continuation`` — the typed continued-error rollup a patched n8n
       records. Sound; preferred whenever present (``_continuation_error``).
    2. ``executionStatus == "error"`` / ``run.error`` — n8n set the node status
       itself. Always loud, always sound.
    3. item-level ``json.error`` content-scan — the legacy path, unsound (a
       legitimate ``error`` field trips it). Gated behind
       ``agd_health_scan_loose_json_error`` and only consulted when the sound
       signals are silent, so it still covers the loose-emitter nodes the engine
       scan cannot see while a patched instance can turn the false positives off.
    """
    if not isinstance(run, dict):
        return None

    cont_err = _continuation_error(run)
    if cont_err is not None:
        return cont_err

    exec_status = run.get("executionStatus")
    run_err = run.get("error")
    if exec_status == "error" or run_err:
        return _normalize_error(run_err if run_err else "error")

    if not settings.agd_health_scan_loose_json_error:
        return None

    item_err = None
    data = run.get("data") or {}
    for out in (data.get("main") or []):
        for item in (out or []):
            j = (item or {}).get("json") or {}
            if isinstance(j, dict) and "error" in j:
                item_err = j["error"]
                break
        if item_err is not None:
            break

    if item_err is None:
        return None
    return _normalize_error(item_err)


def _input_dropped(input_history: list[int], in_items) -> bool:
    """True when this node's input is itself anomalously low vs its own input
    history, i.e. an upstream node already dropped and this node merely passed the
    reduced volume through. Used to suppress downstream victims of a magnitude
    drop so only the origin fires. Needs enough input history to be sure; when
    unsure it returns False (do not suppress), keeping recall on the origin.
    """
    if not isinstance(in_items, int):
        return False
    if len(input_history) < settings.agd_health_min_samples:
        return False
    nonzero = [x for x in input_history if x > 0]
    if not nonzero:
        return False
    return in_items < median(nonzero) * settings.agd_health_drop_factor


def _classify_low_output(
    history: list[int], out_items, in_items, input_history: list[int] | None = None
) -> tuple[str, str]:
    """Classify a node run's output volume against its own history.

    Returns (status, reason). ``OK`` and ``EMPTY`` are informational; only ``LOW``
    is an alertable anomaly. Precision over recall (spec, locked 2026-07-09): only
    a historically reliable producer that had input fires, so idle pollers,
    dormant nodes, and cold-start stay quiet.
    """
    if out_items is None:
        return "OK", ""
    n = len(history)
    if n < settings.agd_health_min_samples:
        # Cold start: not enough history to know this node's normal. Never fire.
        return ("EMPTY" if out_items == 0 else "OK"), "cold_start"

    zero_rate = sum(1 for x in history if x == 0) / n
    nonzero = [x for x in history if x > 0]
    median_nz = median(nonzero) if nonzero else 0
    steady = zero_rate <= settings.agd_health_steady_zero_rate and median_nz >= 1
    if not steady:
        # Intermittent or dormant: a low/zero count is within this node's normal.
        return ("EMPTY" if out_items == 0 else "OK"), "expected"

    # Steady producer. The real failure is a node that had work and dropped it; a
    # node with no input just inherited emptiness from upstream, so flag the origin
    # of the cascade, not its downstream victims. Unknown input -> stay quiet.
    had_input = isinstance(in_items, int) and in_items > 0
    if not had_input:
        return ("EMPTY" if out_items == 0 else "OK"), "inherited"
    if out_items == 0:
        return "LOW", "empty"
    if out_items < median_nz * settings.agd_health_drop_factor:
        # Magnitude drop. Flag only the ORIGIN of the cascade: a node whose input
        # is itself anomalously low just carried an upstream drop through (a
        # victim), so stay quiet. The origin's input held normal while its own
        # output collapsed (a data source's input is the steady trigger count).
        if _input_dropped(input_history or [], in_items):
            return "OK", "inherited_drop"
        return "LOW", "drop"
    return "OK", ""


def _low_summary(reason: str, out_items, in_items) -> str:
    if reason == "empty":
        return f"reliable producer returned 0 items (had {in_items} input)"
    if reason == "drop":
        return f"output {out_items} far below this node's normal volume"
    return ""


async def enrich_trace_health(trace_id: str) -> int:
    """Enrich one trace with per-node health. Returns spans written (0 if skipped)."""
    if await storage.has_health(trace_id):
        return 0
    spans = await storage.get_trace(trace_id)
    if not spans:
        return 0

    root = next((s for s in spans if s.get("name") == "workflow.execute"), None)
    exec_status = ((root or {}).get("attributes") or {}).get("n8n.execution.status", "")
    node_spans = [s for s in spans if s.get("name") == "node.execute"]
    if not node_spans:
        return 0

    exec_id = next((s["execution_id"] for s in spans if s.get("execution_id")), "")
    inst = next((s["instance_id"] for s in spans if s.get("instance_id")), "")

    # Run-data is fetched from the trace's OWNING instance (not just the active
    # one) and is only needed for the error detectors (mode 3 works from span
    # attributes alone). An unresolved unknown-<hash> trace is skipped until the
    # learn step re-attributes it. Best-effort and bounded so a slow fetch never
    # blocks the caller.
    run_by_node: dict[str, list] = {}
    if exec_id and not inst.startswith("unknown-"):
        try:
            raw = await asyncio.wait_for(
                n8n_client.get_execution_raw_by_instance(exec_id, inst), timeout=8.0
            )
            run_by_node = ((raw or {}).get("data") or {}).get("resultData", {}).get("runData", {}) or {}
        except Exception as e:  # noqa: BLE001 - best-effort, retries on next open
            logger.debug("health enrich: run-data fetch failed/slow for exec %s: %s", exec_id, e)

    # node name -> its node.execute spans in execution order (one span per run).
    by_node: dict[str, list[dict]] = {}
    for s in sorted(node_spans, key=lambda x: x["start_ns"]):
        nn = (s.get("attributes") or {}).get("n8n.node.name")
        if nn:
            by_node.setdefault(nn, []).append(s)

    now = datetime.now(timezone.utc).isoformat()
    updates: list[dict] = []
    silent_hits: list[dict] = []
    for nn, sps in by_node.items():
        runs = run_by_node.get(nn, [])
        for i, s in enumerate(sps):
            attrs = s.get("attributes") or {}
            out = attrs.get("n8n.node.items.output")
            out_items = int(out) if isinstance(out, (int, float)) else None
            inp = attrs.get("n8n.node.items.input")
            in_items = int(inp) if isinstance(inp, (int, float)) else None
            node_id = str(attrs.get("n8n.node.id") or "")
            run = runs[i] if i < len(runs) else (runs[-1] if runs else None)
            err = _node_error_from_run(run) if run else None

            if err:
                status, etype, summary, http = "ERROR", err[0], err[1], err[2]
            else:
                http = None
                history = await storage.node_output_history(
                    node_id, settings.agd_health_window, exclude_trace_id=trace_id
                )
                input_history = await storage.node_input_history(
                    node_id, settings.agd_health_window, exclude_trace_id=trace_id
                )
                status, reason = _classify_low_output(history, out_items, in_items, input_history)
                if status == "LOW":
                    etype = "empty_output" if reason == "empty" else "low_output"
                    summary = _low_summary(reason, out_items, in_items)
                else:
                    etype, summary = "", ""

            # Silent = the node broke but the execution reported success. A node
            # error under a run n8n already flagged "error" is loud, not silent.
            is_silent = 1 if (status in ("ERROR", "LOW") and exec_status == "success") else 0
            updates.append({
                "span_id": s["span_id"],
                "health_status": status,
                "error_type": etype,
                "error_summary": summary,
                "http_status": http,
                "output_items": out_items,
                "input_items": in_items,
                "node_id": node_id,
                "silent": is_silent,
                "checked_at": now,
            })
            if is_silent:
                silent_hits.append({"node": nn, "error_type": etype, "error_summary": summary})

    n = await storage.set_health(updates)
    if silent_hits:
        logger.info("silent-failure: %d node(s) errored under a green run, exec %s", len(silent_hits), exec_id)
        try:
            await manager.broadcast("otel:silent", {
                "trace_id": trace_id,
                "execution_id": exec_id,
                "instance_id": inst,
                "workflow_name": (root or {}).get("workflow_name", ""),
                "nodes": silent_hits,
            })
        except Exception:  # noqa: BLE001 - notification is best-effort
            pass

        # Surface into the errors pipeline too. The Overview, Insights, and
        # Executions/Errors views are all driven by the errors table; most
        # operators live there, not in the trace waterfall. store_error broadcasts
        # an "error" event, so a silent failure appears live in those views the
        # moment it is detected. Distinct error_type keeps it a separate class.
        from backend.modules.errors import collector as errors_collector

        wf_id = (root or {}).get("workflow_id", "") or "unknown"
        wf_name = (root or {}).get("workflow_name", "") or "Unknown Workflow"
        for hit in silent_hits:
            try:
                await errors_collector.store_error({
                    "instance_id": inst,
                    "workflow_id": wf_id,
                    "workflow_name": wf_name,
                    "execution_id": exec_id,
                    "node_name": hit["node"],
                    "error_message": hit.get("error_summary")
                    or "Node produced no/low output on a run n8n reported as success.",
                    "error_type": SILENT_ERROR_TYPE,
                })
            except Exception:  # noqa: BLE001 - surfacing is best-effort
                logger.debug("silent-failure: could not store error row for node %s", hit.get("node"))
    return n
