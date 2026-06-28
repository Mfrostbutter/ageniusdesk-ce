"""Drives an agent run: graph.astream -> persisted event log -> SQLite.

Agent-agnostic. The AgentDef (registry.py) supplies the graph factory, the kickoff
text, and the initial state; this module streams any graph whose nodes emit AI/Tool
messages on the `messages` channel, and handles the human-in-the-loop pause.

Transport: runs broadcast `langgraph:run` events over the dashboard WebSocket, so
the Agent Fleet core view updates live, and the growing event log is also persisted
to storage on every emit so a past run replays and survives a refresh.

Everything heavyweight happens lazily, per run:
  - LangChain/LangGraph imports (the langgraph extra may not be installed; the
    module must still register at boot),
  - Anthropic key resolution (the key may live only in the encrypted secrets store),
  - graph compilation (build is a pure factory; nothing at module scope).

Event contract (one event type, `phase` discriminator), persisted into the run's
events log and rendered by the frontend:

  { run_id, phase: "started",            task, model, agent_id, agent_name }
  { run_id, phase: "thinking",           node, text }
  { run_id, phase: "tool_call",          node, tool, args }
  { run_id, phase: "tool_result",        node, tool, preview }
  { run_id, phase: "node",               node, label, text }
  { run_id, phase: "node_light",         node }
  { run_id, phase: "awaiting_approval",  proposal_md, choices }   # HITL pause
  { run_id, phase: "resumed",            action }                 # HITL resume
  { run_id, phase: "final",              triage_md, trace_url, total_tokens, total_cost, usage_detail }
  { run_id, phase: "error",              message }

Human-in-the-loop: an agent whose graph calls interrupt() halts at the approval
node. The runner detects the `__interrupt__` update, emits `awaiting_approval`,
parks the LIVE compiled graph (with its checkpointer) in `_PAUSED`, and returns. A
later resume() re-enters the same graph with Command(resume=decision) and the graph
continues from exactly where it paused. The checkpointer is in-memory, so a parked
run survives a browser refresh but not a process restart (acceptable; a stale parked
run is just re-run).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Optional

from backend.websocket import manager

from . import storage

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 25
_PREVIEW_CHARS = 240
_TRACE_URL_RETRIES = 3
_TRACE_URL_RETRY_DELAY = 2.0

# Single-flight guard: one *running* agent at a time. A double-click must not
# interleave two streams. A run parked at an interrupt is NOT live (it released the
# slot), so the operator can approve it without the button stuck disabled.
_live_run_id: Optional[str] = None

# Runs parked at a human-approval interrupt: run_id -> parked context. Holds the
# live compiled graph (its in-memory checkpointer carries the paused state) plus
# everything resume() needs to continue and finalize.
_PAUSED: dict[str, dict] = {}


def is_live() -> Optional[str]:
    """Return the in-flight (actively running) run id, or None when idle."""
    return _live_run_id


def is_paused(run_id: str) -> bool:
    """True when the run is parked awaiting human approval."""
    return run_id in _PAUSED


def resolve_anthropic_key() -> str:
    """Find the Anthropic key the same way the assistant provider does.

    Order: process env, then AgeniusDesk's encrypted secret store ($ANTHROPIC_KEY),
    then the assistant config (if the operator configured anthropic there). The
    secret resolver echoes the bare name on a miss, so an echo counts as not-found.
    """
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY"):
        val = os.environ.get(name, "")
        if val:
            return val
    try:
        from backend.config import decrypt_value

        val = decrypt_value("$ANTHROPIC_KEY")
        if val and val != "ANTHROPIC_KEY":
            return val
    except Exception:  # noqa: BLE001 - resolution must never crash a run
        pass
    try:
        from backend.modules.assistant.providers import get_assistant_config

        cfg = get_assistant_config()
        if cfg.get("provider") == "anthropic" and cfg.get("api_key"):
            return cfg["api_key"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def resolve_openai_key() -> str:
    """Find the OpenAI key the same way resolve_anthropic_key finds Anthropic's.

    Used only by a future cross-provider reviewer agent (none in the v1 fleet).
    Order: process env, then the encrypted secret store ($OPEN_AI_KEY is CE's name).
    """
    for name in ("OPENAI_API_KEY", "OPENAI_KEY", "OPEN_AI_KEY"):
        val = os.environ.get(name, "")
        if val:
            return val
    try:
        from backend.config import decrypt_value

        for name in ("OPEN_AI_KEY", "OPENAI_API_KEY", "OPENAI_KEY"):
            val = decrypt_value(f"${name}")
            if val and val != name:
                return val
    except Exception:  # noqa: BLE001
        pass
    return ""


def make_chat_model(provider: str, model: str, max_tokens: int, api_key: str):
    """Provider-agnostic chat-model factory: the seam that keeps the fleet model-

    agnostic. `provider` is 'anthropic' (default) or 'openai'. Imports are lazy so the
    module stays boot-safe when an optional provider package isn't installed. Both
    providers honor temperature=0 + max_tokens; structured output works on either via
    LangChain's .with_structured_output().
    """
    if (provider or "anthropic").lower() == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, temperature=0, max_tokens=max_tokens, api_key=api_key)
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model, temperature=0, max_tokens=max_tokens, api_key=api_key)


def _langsmith_tracing_active() -> bool:
    """Self-disable tracing when no key is present (avoids 401 noise), and report
    whether traces will actually ship for this run."""
    tracing_on = os.environ.get("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes")
    if tracing_on and not os.environ.get("LANGSMITH_API_KEY"):
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return False
    return tracing_on and bool(os.environ.get("LANGSMITH_API_KEY"))


def _target_str(error_id: Optional[int], prompt: str) -> str:
    """The compact target label persisted on the run + fed to graph state."""
    if error_id is not None:
        return str(error_id)
    return prompt.strip() or "latest"


def _message_text(msg: Any) -> str:
    """Flatten AIMessage content (str or Anthropic content-block list) to text."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _trace_steps(client, run_id: str) -> list[dict]:
    """Per-LLM-call breakdown across the run's trace (the model calls the graph
    made). Best-effort; [] on any failure. Each step: {name, input, output, total, cost}."""
    try:
        runs = list(client.list_runs(trace_id=uuid.UUID(run_id), run_type="llm"))
    except Exception:
        return []
    steps = []
    for r in sorted(runs, key=lambda r: getattr(r, "start_time", None) or 0):
        steps.append({
            "name": getattr(r, "name", "") or "llm",
            "input": _i(getattr(r, "prompt_tokens", 0)),
            "output": _i(getattr(r, "completion_tokens", 0)),
            "total": _i(getattr(r, "total_tokens", 0)),
            "cost": _f(getattr(r, "total_cost", 0)),
        })
    return steps


async def _capture_run_meta(run_id: str) -> dict:
    """Best-effort LangSmith trace URL + usage aggregate + per-call breakdown.

    The run id was passed as config["run_id"], so the root trace shares it and
    LangSmith rolls the whole tree's token/cost onto that root. Retries briefly for
    flush lag; any failure just hides the link and leaves usage at zero. Returns
    {url, total_tokens, total_cost, detail:{...input/output + steps...}}.
    """
    meta = {"url": None, "total_tokens": 0, "total_cost": 0.0, "detail": {}}
    try:
        from langsmith import Client

        client = Client()
        for attempt in range(_TRACE_URL_RETRIES):
            try:
                run = await asyncio.to_thread(client.read_run, run_id)
                meta["url"] = getattr(run, "url", None) or meta["url"]
                meta["total_tokens"] = _i(getattr(run, "total_tokens", 0))
                meta["total_cost"] = _f(getattr(run, "total_cost", 0))
                meta["detail"] = {
                    "input_tokens": _i(getattr(run, "prompt_tokens", 0)),
                    "output_tokens": _i(getattr(run, "completion_tokens", 0)),
                    "total_tokens": meta["total_tokens"],
                    "input_cost": _f(getattr(run, "prompt_cost", 0)),
                    "output_cost": _f(getattr(run, "completion_cost", 0)),
                    "total_cost": meta["total_cost"],
                }
                # Cost is computed slightly after tokens land; keep retrying until we
                # have a URL AND a nonzero token count (or we run out of tries).
                if meta["url"] and meta["total_tokens"]:
                    steps = await asyncio.to_thread(_trace_steps, client, run_id)
                    _reconcile_to_steps(meta, steps)
                    return meta
            except Exception:
                pass
            if attempt < _TRACE_URL_RETRIES - 1:
                await asyncio.sleep(_TRACE_URL_RETRY_DELAY)
        # Ran out of retries with a partial result: still try for steps.
        if meta["total_tokens"] and "steps" not in meta["detail"]:
            try:
                _reconcile_to_steps(meta, await asyncio.to_thread(_trace_steps, client, run_id))
            except Exception:
                pass
    except Exception:
        pass
    return meta


def _reconcile_to_steps(meta: dict, steps: list[dict]) -> None:
    """Prefer the sum of per-call usage as the run total. The LangSmith root
    aggregate can under-report vs. the actual model calls; the sum across every LLM
    call is the true 'tokens through the whole run' and makes the breakdown add up.
    Falls back to the root aggregate when there are no steps."""
    meta["detail"]["steps"] = steps
    if not steps:
        return
    si = sum(s["input"] for s in steps)
    so = sum(s["output"] for s in steps)
    st = sum(s["total"] for s in steps) or (si + so)
    sc = round(sum(s["cost"] for s in steps), 6)
    meta["total_tokens"] = st or meta["total_tokens"]
    meta["total_cost"] = sc or meta["total_cost"]
    meta["detail"].update({
        "input_tokens": si, "output_tokens": so, "total_tokens": st, "total_cost": sc,
    })


def _make_emit(run_id: str, events: list[dict]):
    """Build the emit closure for a run: broadcast over the WebSocket + persist.

    Core view subscribers get live `langgraph:run` events; persisting the growing log
    on every event also lets a past run replay and survives a refresh.
    """

    async def emit(payload: dict) -> None:
        event = {"run_id": run_id, "ts": int(time.time() * 1000), **payload}
        events.append(event)
        await manager.broadcast("langgraph:run", event)
        await storage.update_run(run_id, events=events)

    return emit


def _interrupt_payload(update: dict) -> Optional[dict]:
    """Extract an interrupt's value from a stream update, or None."""
    intr = update.get("__interrupt__")
    if not intr:
        return None
    first = intr[0] if isinstance(intr, (list, tuple)) else intr
    val = getattr(first, "value", first)
    return val if isinstance(val, dict) else {"proposal": str(val)}


# Per-model token pricing (USD per 1M tokens: input, output), matched by substring.
# Powers a native cost estimate so the UI shows tokens + cost without LangSmith.
# Update as prices change; LangSmith (when on) overrides these with exact figures.
_TOKEN_PRICES = {
    "haiku": (1.0, 5.0),
    "sonnet": (3.0, 15.0),
    "opus": (15.0, 75.0),
    "fable": (10.0, 50.0),
    "gpt": (2.5, 10.0),
}


def _price_for(model: str) -> tuple:
    m = (model or "").lower()
    for key, price in _TOKEN_PRICES.items():
        if key in m:
            return price
    return (0.0, 0.0)


def _native_meta(usage_by_model: dict) -> dict:
    """Usage/cost summary from a UsageMetadataCallbackHandler's usage_metadata
    ({model: {input_tokens, output_tokens, ...}}). Lets the UI show tokens + an
    estimated cost without LangSmith."""
    ti = to = 0
    tc = 0.0
    steps = []
    for model, u in (usage_by_model or {}).items():
        i = int((u or {}).get("input_tokens", 0) or 0)
        o = int((u or {}).get("output_tokens", 0) or 0)
        pi, po = _price_for(model)
        c = i / 1_000_000 * pi + o / 1_000_000 * po
        ti += i
        to += o
        tc += c
        steps.append({"name": model, "input": i, "output": o, "total": i + o, "cost": round(c, 6)})
    return {
        "url": None, "total_tokens": ti + to, "total_cost": round(tc, 6),
        "detail": {"input_tokens": ti, "output_tokens": to, "total_tokens": ti + to,
                   "total_cost": round(tc, 6), "steps": steps},
    }


def _make_usage_cb():
    """A callback that accumulates per-model token usage across every LLM call in a
    run, structured-output calls included. None if the handler is unavailable."""
    try:
        from langchain_core.callbacks import UsageMetadataCallbackHandler

        return UsageMetadataCallbackHandler()
    except Exception:
        return None


def _usage_of(cb) -> dict:
    return getattr(cb, "usage_metadata", {}) if cb is not None else {}


async def _drive(graph, inp, config, emit, AIMessage, ToolMessage) -> dict:
    """Stream one graph segment to its next stopping point.

    Returns {"status": "paused", "proposal": str} when the graph hit an interrupt,
    or {"status": "done", "final_md": str} when it ran to the end. Raises
    GraphRecursionError to the caller. Token usage is captured by a
    UsageMetadataCallbackHandler on the config, not here (it also sees structured
    -output calls, which never appear on the messages channel).
    """
    final_md = ""
    paused_proposal: Optional[str] = None
    paused_choices = None   # optional pick-list for a "choose one" gate (rendered as buttons)
    # Own the async generator so we can close it deterministically. Breaking out of
    # `astream` on an interrupt would otherwise leave it to be GC-closed later, which
    # surfaces a stray GeneratorExit inside whatever traced context is active then.
    stream = graph.astream(inp, config, stream_mode="updates")
    try:
        async for update in stream:
            payload = _interrupt_payload(update)
            if payload is not None:
                paused_proposal = payload.get("proposal", "")
                paused_choices = payload.get("choices")
                break
            for node, node_payload in (update or {}).items():
                for msg in (node_payload or {}).get("messages", []):
                    # `node` is the real LangGraph node id for this update. Carry it on
                    # every event so the live graph lights the exact node (independent of
                    # the message's name), instead of guessing from name/phase heuristics.
                    if isinstance(msg, AIMessage):
                        text = _message_text(msg).strip()
                        tool_calls = getattr(msg, "tool_calls", None) or []
                        if tool_calls:
                            if text:
                                await emit({"phase": "thinking", "node": node, "text": text})
                            for call in tool_calls:
                                await emit({
                                    "phase": "tool_call", "node": node,
                                    "tool": call.get("name", ""),
                                    "args": call.get("args", {}),
                                })
                        elif getattr(msg, "name", None):
                            # A named intermediate node (a parallel lens, a plan step):
                            # a labeled timeline step, not the final result.
                            await emit({"phase": "node", "node": node, "label": msg.name, "text": text})
                        else:
                            # No tool calls, no name: the latest such message is the
                            # result so far (diagnosis, proposal, or finalized outcome).
                            # Emit a light-only event (no text/label) so the producing
                            # node lights in the live graph without a stray timeline step.
                            final_md = text
                            await emit({"phase": "node_light", "node": node})
                    elif isinstance(msg, ToolMessage):
                        await emit({
                            "phase": "tool_result", "node": node,
                            "tool": getattr(msg, "name", "") or "",
                            "preview": _message_text(msg)[:_PREVIEW_CHARS],
                        })
    finally:
        # Deterministic close. No-op if already exhausted (the run-to-completion path);
        # for the interrupt path it tears the suspended stream down cleanly, in-context.
        await stream.aclose()

    if paused_proposal is not None:
        return {"status": "paused", "proposal": paused_proposal, "choices": paused_choices}
    return {"status": "done", "final_md": final_md}


async def _run_pydantic(agent, task: str, api_key: str, emit) -> tuple:
    """Run a PydanticAI agent and emit the normalized run events so the same
    waterfall + timeline render. Uses agent.run() then replays the message history
    as tool_call/tool_result events (best-effort; the run is not live-streamed yet).
    Returns (final_md, native_meta). The graph.py factory built a pydantic_ai.Agent
    and ignores the llm/checkpointer args."""
    os.environ["ANTHROPIC_API_KEY"] = api_key  # pydantic-ai resolves the key from env
    pa = agent.build(None, None)
    await emit({"phase": "thinking", "node": "agent", "text": "Running the PydanticAI agent."})
    result = await pa.run(task)

    # Replay the message history as tool steps (shape varies across versions).
    try:
        for msg in result.all_messages():
            for part in getattr(msg, "parts", []) or []:
                kind = type(part).__name__
                if kind == "ToolCallPart":
                    args = getattr(part, "args", {})
                    await emit({
                        "phase": "tool_call", "node": "tools",
                        "tool": getattr(part, "tool_name", "tool"),
                        "args": args if isinstance(args, dict) else {"input": str(args)[:200]},
                    })
                elif kind == "ToolReturnPart":
                    await emit({
                        "phase": "tool_result", "node": "tools",
                        "tool": getattr(part, "tool_name", "tool"),
                        "preview": str(getattr(part, "content", ""))[:_PREVIEW_CHARS],
                    })
    except Exception:  # noqa: BLE001 - steps are a nicety; never fail the run on them
        pass

    output = getattr(result, "output", None)
    if output is None:
        output = getattr(result, "data", "")
    final_md = output if isinstance(output, str) else str(output)

    meta = {"total_tokens": 0, "total_cost": 0.0, "detail": {}}
    try:
        u = result.usage()
        it = int(getattr(u, "request_tokens", 0) or getattr(u, "input_tokens", 0) or 0)
        ot = int(getattr(u, "response_tokens", 0) or getattr(u, "output_tokens", 0) or 0)
        tt = int(getattr(u, "total_tokens", 0) or (it + ot))
        pi, po = _price_for(agent.default_model)
        cost = round(it / 1_000_000 * pi + ot / 1_000_000 * po, 6)
        meta = {
            "total_tokens": tt, "total_cost": cost,
            "detail": {"input_tokens": it, "output_tokens": ot, "total_tokens": tt,
                       "total_cost": cost, "steps": []},
        }
    except Exception:  # noqa: BLE001
        pass
    return final_md, meta


async def run(run_id: str, agent_id: str, error_id: Optional[int], prompt: str) -> None:
    """Execute one agent run end to end. Fire-and-forget from the router.

    Agent-agnostic: the AgentDef supplies the graph, the kickoff text, and the
    initial state. HITL agents pause at their interrupt; everything else runs
    straight through.
    """
    global _live_run_id
    _live_run_id = run_id

    events: list[dict] = []
    emit = _make_emit(run_id, events)

    async def fail(message: str) -> None:
        logger.warning("agent-fleet run %s failed: %s", run_id, message)
        await emit({"phase": "error", "message": message})
        await storage.update_run(run_id, status="error", error=message)

    try:
        try:
            from langchain_core.messages import AIMessage, ToolMessage
            from langgraph.errors import GraphRecursionError
        except ImportError as e:
            await fail(f"LangGraph dependencies not installed (pip install '.[langgraph]'): {e}")
            return

        from . import registry

        agent = registry.get_agent(agent_id)
        if agent is None:
            await fail(f"Unknown agent '{agent_id}'.")
            return

        api_key = resolve_anthropic_key()
        if not api_key:
            await fail(
                "No Anthropic API key found. Set ANTHROPIC_API_KEY in the environment "
                "or add ANTHROPIC_KEY to the secrets store."
            )
            return

        tracing = _langsmith_tracing_active()
        model = os.environ.get(agent.model_env, agent.default_model) if agent.model_env else agent.default_model
        max_tokens = (
            int(os.environ.get(agent.max_tokens_env, agent.max_tokens))
            if agent.max_tokens_env else agent.max_tokens
        )

        # PydanticAI agents run through their own adapter, not the LangGraph driver.
        if getattr(agent, "framework", "langgraph") == "pydantic-ai":
            task = agent.kickoff(error_id, prompt)
            await emit({
                "phase": "started", "task": task, "model": model,
                "agent_id": agent.id, "agent_name": agent.name,
            })
            try:
                final_md, meta = await _run_pydantic(agent, task, api_key, emit)
            except Exception as e:  # noqa: BLE001 - terminal state must be written
                await fail(f"PydanticAI run failed: {type(e).__name__}: {e}")
                return
            await emit({
                "phase": "final", "triage_md": final_md, "trace_url": None,
                "total_tokens": meta["total_tokens"], "total_cost": meta["total_cost"],
                "usage_detail": meta.get("detail", {}),
            })
            await storage.update_run(
                run_id, status="done", triage_md=final_md, trace_url="",
                total_tokens=meta["total_tokens"], total_cost=meta["total_cost"],
                usage_detail=meta.get("detail", {}), events=events,
            )
            return

        # Primary model through the provider seam (anthropic today, not hard-wired).
        llm = make_chat_model("anthropic", model, max_tokens, api_key)

        # Cross-provider reviewer hook: an agent that declares a reviewer_provider gets
        # a SECOND model built from a different vendor's key. None of the v1 agents use
        # it; the branch is kept so the runner stays agent-agnostic.
        reviewer_llm = None
        if agent.reviewer_provider:
            reviewer_model = (
                os.environ.get(agent.reviewer_model_env, agent.reviewer_model)
                if agent.reviewer_model_env else agent.reviewer_model
            )
            if agent.reviewer_provider == "openai":
                reviewer_key = resolve_openai_key()
                if not reviewer_key:
                    await fail(
                        "No OpenAI API key found for the cross-provider reviewer. Set "
                        "OPENAI_API_KEY in the environment or the secrets store."
                    )
                    return
            else:
                reviewer_key = api_key
            reviewer_llm = make_chat_model(agent.reviewer_provider, reviewer_model, max_tokens, reviewer_key)

        # HITL agents need a checkpointer to support interrupt/resume; the live graph
        # is parked in _PAUSED across the pause so resume reuses it.
        checkpointer = None
        if agent.hitl:
            from langgraph.checkpoint.memory import MemorySaver

            checkpointer = MemorySaver()
        graph = (
            agent.build(llm, checkpointer, reviewer_llm=reviewer_llm)
            if agent.reviewer_provider else agent.build(llm, checkpointer)
        )

        task = agent.kickoff(error_id, prompt)
        target = _target_str(error_id, prompt)
        await emit({
            "phase": "started", "task": task, "model": model,
            "agent_id": agent.id, "agent_name": agent.name,
        })

        # thread_config resumes the same checkpoint; run_id sets the LangSmith root.
        # The usage callback rides on thread_config so it also accumulates across a
        # pause/resume (resume reuses the parked thread_config), capturing every LLM
        # call (structured-output ones too) for the native token/cost figures.
        usage_cb = _make_usage_cb()
        thread_config = {"recursion_limit": RECURSION_LIMIT, "configurable": {"thread_id": run_id}}
        if usage_cb is not None:
            thread_config["callbacks"] = [usage_cb]
        config = {**thread_config, "run_id": uuid.UUID(run_id)}
        state = agent.initial_state(task, target)

        try:
            result = await _drive(graph, state, config, emit, AIMessage, ToolMessage)
        except GraphRecursionError:
            await fail(f"Hit the step limit ({RECURSION_LIMIT} graph steps) without a conclusion.")
            return

        if result["status"] == "paused":
            # Park the live graph; release the single-flight slot for approval.
            _PAUSED[run_id] = {
                "graph": graph, "thread_config": thread_config, "agent": agent,
                "events": events, "tracing": tracing, "trace_run_id": run_id,
                "usage_cb": usage_cb,
            }
            await emit({
                "phase": "awaiting_approval",
                "proposal_md": result["proposal"],
                "choices": result.get("choices"),
            })
            await storage.update_run(run_id, status="paused", triage_md=result["proposal"], events=events)
            return

        # Native token/cost is available immediately (no network). Flip the run to DONE
        # right away so the tile never waits on LangSmith; _capture_run_meta retries for
        # trace-flush lag, and a run with few/no LLM calls has no trace to find, which
        # used to leave the run stuck on "running" for the whole retry budget. Then, only
        # if tracing, fetch the exact figures + trace URL and patch them in.
        meta = _native_meta(_usage_of(usage_cb))
        await emit({
            "phase": "final", "triage_md": result["final_md"], "trace_url": meta.get("url"),
            "total_tokens": meta["total_tokens"], "total_cost": meta["total_cost"],
            "usage_detail": meta.get("detail", {}),
        })
        await storage.update_run(
            run_id, status="done", triage_md=result["final_md"], trace_url=meta.get("url") or "",
            total_tokens=meta["total_tokens"], total_cost=meta["total_cost"],
            usage_detail=meta.get("detail", {}), events=events,
        )
        if tracing:
            ls = await _capture_run_meta(run_id)
            if ls.get("total_tokens") or ls.get("url"):
                m2 = ls if ls.get("total_tokens") else {**meta, "url": ls.get("url")}
                await emit({
                    "phase": "final", "triage_md": result["final_md"], "trace_url": m2.get("url"),
                    "total_tokens": m2["total_tokens"], "total_cost": m2["total_cost"],
                    "usage_detail": m2.get("detail", {}),
                })
                await storage.update_run(
                    run_id, status="done", trace_url=m2.get("url") or "",
                    total_tokens=m2["total_tokens"], total_cost=m2["total_cost"],
                    usage_detail=m2.get("detail", {}),
                )

    except Exception as e:  # noqa: BLE001 - terminal state must always be written
        logger.exception("agent-fleet run %s crashed", run_id)
        try:
            await fail(f"{type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        _live_run_id = None


async def resume(run_id: str, decision: dict) -> None:
    """Resume a parked HITL run with the human's decision. Fire-and-forget."""
    global _live_run_id

    parked = _PAUSED.get(run_id)
    if not parked:
        logger.warning("resume: run %s is not parked", run_id)
        return

    _live_run_id = run_id
    graph = parked["graph"]
    thread_config = parked["thread_config"]
    events = parked["events"]
    tracing = parked["tracing"]
    emit = _make_emit(run_id, events)

    async def fail(message: str) -> None:
        logger.warning("agent-fleet resume %s failed: %s", run_id, message)
        await emit({"phase": "error", "message": message})
        await storage.update_run(run_id, status="error", error=message)

    try:
        from langchain_core.messages import AIMessage, ToolMessage
        from langgraph.errors import GraphRecursionError
        from langgraph.types import Command

        await emit({"phase": "resumed", "action": (decision or {}).get("action", "approve")})
        await storage.update_run(run_id, status="running")

        usage_cb = parked.get("usage_cb")   # rides on the parked thread_config; keeps accumulating
        try:
            result = await _drive(graph, Command(resume=decision), thread_config, emit, AIMessage, ToolMessage)
        except GraphRecursionError:
            await fail(f"Hit the step limit ({RECURSION_LIMIT} graph steps) without a conclusion.")
            _PAUSED.pop(run_id, None)
            return

        if result["status"] == "paused":
            # A second interrupt (not expected for these graphs): re-park.
            await emit({
                "phase": "awaiting_approval",
                "proposal_md": result["proposal"],
                "choices": result.get("choices"),
            })
            await storage.update_run(run_id, status="paused", events=events)
            return

        # Flip to DONE on native cost first (same reason as the initial-run path);
        # enrich with LangSmith after so status never waits on the trace fetch.
        meta = _native_meta(_usage_of(usage_cb))
        await emit({
            "phase": "final", "triage_md": result["final_md"], "trace_url": meta.get("url"),
            "total_tokens": meta["total_tokens"], "total_cost": meta["total_cost"],
            "usage_detail": meta.get("detail", {}),
        })
        await storage.update_run(
            run_id, status="done", triage_md=result["final_md"], trace_url=meta.get("url") or "",
            total_tokens=meta["total_tokens"], total_cost=meta["total_cost"],
            usage_detail=meta.get("detail", {}), events=events,
        )
        if tracing:
            ls = await _capture_run_meta(parked["trace_run_id"])
            if ls.get("total_tokens") or ls.get("url"):
                m2 = ls if ls.get("total_tokens") else {**meta, "url": ls.get("url")}
                await emit({
                    "phase": "final", "triage_md": result["final_md"], "trace_url": m2.get("url"),
                    "total_tokens": m2["total_tokens"], "total_cost": m2["total_cost"],
                    "usage_detail": m2.get("detail", {}),
                })
                await storage.update_run(
                    run_id, status="done", trace_url=m2.get("url") or "",
                    total_tokens=m2["total_tokens"], total_cost=m2["total_cost"],
                    usage_detail=m2.get("detail", {}),
                )
        _PAUSED.pop(run_id, None)

    except Exception as e:  # noqa: BLE001
        logger.exception("agent-fleet resume %s crashed", run_id)
        try:
            await fail(f"{type(e).__name__}: {e}")
        except Exception:
            pass
        _PAUSED.pop(run_id, None)
    finally:
        _live_run_id = None
