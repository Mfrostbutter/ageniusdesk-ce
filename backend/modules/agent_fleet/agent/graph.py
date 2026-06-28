"""The ops-triage agent: a classic investigate-then-answer tool loop.

    START -> agent -> (tools_condition) -> tools -> agent -> ... -> END

`agent` is the model bound to the triage tools; `tools` executes any tool calls the
model emits; the conditional edge routes back to the model until it stops calling
tools and writes the final triage.

This module is a pure factory on purpose: no LLM construction, no env reads, no
compiled graph at import time. The package imports before the dashboard resolves
secrets, so anything built at module scope would capture a missing API key and break
silently. Callers bind their own llm + toolset:

  - the dashboard runner binds the in-process async tools (tools_local.py),
  - studio.py binds the same tools for `langgraph dev`.
"""

from __future__ import annotations

from typing import Sequence

from langchain_core.messages import SystemMessage
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .prompts import SYSTEM_PROMPT
from .state import TriageState


def build_graph(llm, tools: Sequence, checkpointer=None):
    """Compile the triage graph around a model and a toolset.

    `llm` is any LangChain chat model; it gets `.bind_tools(tools)` here so the
    agent node and the ToolNode always see the same set. The agent node is async
    (`ainvoke`) so async in-process tools and the event-loop-driven `astream`
    both work without sync/async gymnastics.
    """
    bound = llm.bind_tools(list(tools))

    async def _agent(state: TriageState) -> dict:
        """One model turn: reason over the conversation + tool results, decide next move."""
        messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [await bound.ainvoke(messages)]}

    g = StateGraph(TriageState)
    g.add_node("agent", _agent)
    g.add_node("tools", ToolNode(list(tools)))
    g.add_edge(START, "agent")
    # tools_condition routes to "tools" when the last AI message has tool calls,
    # otherwise to END.
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
