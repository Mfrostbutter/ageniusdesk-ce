"""The Health Reporter graph: a parallel fan-out / fan-in agent.

    START -> plan -> [ lens_failures | lens_health | lens_volume ] -> synthesize -> END
                       (these three run concurrently in one superstep)

`plan` announces the sweep. The three `lens_*` nodes each run a different diagnostic
lens AT THE SAME TIME: LangGraph schedules them in a single superstep because they
share `plan` as their only predecessor. Each writes its finding to the `findings`
channel (an additive reducer merges the concurrent writes). `synthesize` is the
fan-in: it has all three lenses as predecessors, so it only runs once they all
finish, then reconciles them into one report.

This is the topology showcase: real parallelism + a reduce, visible as parallel
branches in LangGraph Studio and as concurrent runs in a LangSmith trace. Pure
factory, NO host imports (Studio-safe). Each lens calls ONE specific tool directly
(by name, from the injected toolset) rather than a tool loop, so the branches stay
independent and finish fast.

Timeline + live-graph convention: every intermediate node emits a *named* AIMessage
whose name EQUALS its node id (plan, lens_failures, lens_health, lens_volume,
synthesize), because the live graph view highlights a node only on an exact node-id
match (graph.js). `synthesize` emits its named step plus a trailing unnamed
AIMessage; that unnamed message is the final result the runner surfaces.
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, Sequence

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from .prompts import HEALTH_LENS_PROMPT, HEALTH_SYNTH_PROMPT


class HealthState(MessagesState):
    triage_target: str
    # Concurrent lens writes accumulate here (additive reducer), then synthesize reads.
    findings: Annotated[list, operator.add]


def _text(msg) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else (b if isinstance(b, str) else "")
            for b in content
        ]
        return "\n".join(p for p in parts if p)
    return str(content)


def build_health_graph(llm, tools: Sequence, checkpointer=None):
    """Compile the parallel fleet-health graph around a model + toolset."""
    tools_by_name = {getattr(t, "name", ""): t for t in tools}

    async def _run_tool(name: str, args: dict):
        t = tools_by_name.get(name)
        if t is None:
            return f"(tool {name!r} unavailable)"
        try:
            return await t.ainvoke(args)
        except Exception as e:  # noqa: BLE001 - a dead lens must not kill the sweep
            return f"(tool {name!r} errored: {e})"

    async def _summarize(lens: str, data) -> str:
        try:
            blob = json.dumps(data, default=str)[:4000]
        except Exception:
            blob = str(data)[:4000]
        human = HumanMessage(content=f"Lens: {lens}\n\nRaw data:\n{blob}\n\nSummarize what this lens shows.")
        ai = await llm.ainvoke([SystemMessage(content=HEALTH_LENS_PROMPT), human])
        return _text(ai).strip()

    def plan(state: HealthState) -> dict:
        text = (
            "Planning a parallel fleet sweep: recent failures, fleet-health metrics, and "
            "error-execution volume, three lenses investigated concurrently, then synthesized."
        )
        return {"messages": [AIMessage(content=text, name="plan")]}

    async def lens_failures(state: HealthState) -> dict:
        data = await _run_tool("list_recent_errors", {"limit": 15})
        summary = await _summarize("recent failures", data)
        return {"messages": [AIMessage(content=summary, name="lens_failures")], "findings": [summary]}

    async def lens_health(state: HealthState) -> dict:
        data = await _run_tool("fleet_health", {})
        summary = await _summarize("fleet health metrics", data)
        return {"messages": [AIMessage(content=summary, name="lens_health")], "findings": [summary]}

    async def lens_volume(state: HealthState) -> dict:
        data = await _run_tool("list_executions", {"status": "error", "limit": 15})
        summary = await _summarize("error-execution volume", data)
        return {"messages": [AIMessage(content=summary, name="lens_volume")], "findings": [summary]}

    async def synthesize(state: HealthState) -> dict:
        findings = state.get("findings", [])
        body = "\n\n".join(f"- {f}" for f in findings) or "(no findings)"
        human = HumanMessage(
            content="The three parallel investigators returned:\n\n" + body
            + "\n\nWrite the fleet health report."
        )
        ai = await llm.ainvoke([SystemMessage(content=HEALTH_SYNTH_PROMPT), human])
        # Named step lights the `synthesize` node in the live graph; the trailing
        # unnamed message is the final result (fills the RESULT panel).
        return {"messages": [
            AIMessage(content="Reconciled the three lenses into the fleet health report.", name="synthesize"),
            AIMessage(content=_text(ai).strip()),
        ]}

    g = StateGraph(HealthState)
    g.add_node("plan", plan)
    g.add_node("lens_failures", lens_failures)
    g.add_node("lens_health", lens_health)
    g.add_node("lens_volume", lens_volume)
    g.add_node("synthesize", synthesize)

    g.add_edge(START, "plan")
    # Three edges out of plan: all three lenses run in one parallel superstep.
    g.add_edge("plan", "lens_failures")
    g.add_edge("plan", "lens_health")
    g.add_edge("plan", "lens_volume")
    # Three edges into synthesize: fan-in barrier; runs after all lenses finish.
    g.add_edge("lens_failures", "synthesize")
    g.add_edge("lens_health", "synthesize")
    g.add_edge("lens_volume", "synthesize")
    g.add_edge("synthesize", END)

    return g.compile(checkpointer=checkpointer)
