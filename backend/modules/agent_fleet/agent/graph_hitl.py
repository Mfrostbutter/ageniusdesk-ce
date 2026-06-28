"""The Fix Proposer graph: a human-in-the-loop agent.

    START -> investigate (tools loop) -> propose -> approval[interrupt] -> finalize -> END

`investigate` is the same ReAct tool loop as the triage agent: it gathers evidence
until it stops calling tools. `propose` turns that evidence into one concrete,
reversible fix. `approval` calls LangGraph's `interrupt()`: the graph HALTS there,
surfaces the proposal to the human, and only resumes when the runner re-enters with
`Command(resume=decision)`. `finalize` reflects the human's decision into the result.

This is the LangGraph-specific showcase: durable interrupt + checkpointer, resumed
from exactly the paused node. Like graph.py this module is a pure factory with NO
host imports, so it stays importable from `langgraph dev` / Studio. The interrupt
only works when the caller compiles with a checkpointer and runs with a
`configurable.thread_id`; the runner supplies both.
"""

from __future__ import annotations

from typing import Sequence

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import interrupt

from .prompts import FIX_INVESTIGATE_PROMPT, FIX_PROPOSE_PROMPT


class FixState(MessagesState):
    # The error/context the run was kicked off with.
    triage_target: str
    # The fix the agent drafted (surfaced to the human at the interrupt).
    proposal: str
    # The human's verdict: {"action": approve|edit|reject, "edited": str}.
    decision: dict


def _text(msg) -> str:
    """Flatten AIMessage content (str or content-block list) to text."""
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


def build_fix_graph(llm, tools: Sequence, checkpointer=None):
    """Compile the human-in-the-loop fix graph.

    `llm` is bound to the toolset for the investigation loop; `propose` and
    `finalize` use the bare model / plain composition so they never call tools.
    """
    bound = llm.bind_tools(list(tools))

    async def investigate(state: FixState) -> dict:
        messages = [SystemMessage(content=FIX_INVESTIGATE_PROMPT), *state["messages"]]
        return {"messages": [await bound.ainvoke(messages)]}

    async def propose(state: FixState) -> dict:
        """Turn the investigation into one concrete fix.

        Built as a clean 2-turn prompt (system + a human turn carrying the
        diagnosis) rather than replaying the tool-loop history: that keeps the
        conversation ending on a user message (Anthropic rejects a trailing
        assistant turn as prefill) and avoids re-validating tool_use blocks
        against a tool-less call.
        """
        diagnosis = ""
        for m in reversed(state["messages"]):
            if isinstance(m, AIMessage) and not (getattr(m, "tool_calls", None) or []):
                diagnosis = _text(m).strip()
                break
        human = HumanMessage(
            content="Here is the investigation diagnosis:\n\n" + (diagnosis or "(none)")
            + "\n\nNow draft the fix in the required format."
        )
        ai = await llm.ainvoke([SystemMessage(content=FIX_PROPOSE_PROMPT), human])
        text = _text(ai).strip()
        return {"messages": [AIMessage(content=text)], "proposal": text}

    def approval(state: FixState) -> dict:
        """HALT for human approval. Resumes with the decision payload."""
        decision = interrupt({"proposal": state.get("proposal", "")})
        return {"decision": decision or {}}

    def finalize(state: FixState) -> dict:
        """Compose the outcome from the human's decision. Deterministic, no LLM."""
        d = state.get("decision") or {}
        action = (d.get("action") or "approve").lower()
        proposal = state.get("proposal", "")
        edited = (d.get("edited") or "").strip()

        if action == "reject":
            final = (
                "## Decision: Rejected\n\n"
                "The operator rejected the proposed fix. No change was applied; "
                "the issue is escalated for manual handling.\n\n---\n\n"
                "_Rejected proposal:_\n\n" + proposal
            )
        elif action == "edit" and edited:
            final = (
                "## Decision: Approved (edited)\n\n"
                "The operator approved an edited version of the fix. Applied change:\n\n"
                + edited + "\n\n---\n\n_Original proposal:_\n\n" + proposal
            )
        else:
            final = (
                "## Decision: Approved\n\n"
                "The operator approved the proposed fix as written.\n\n" + proposal
            )
        return {"messages": [AIMessage(content=final)]}

    g = StateGraph(FixState)
    g.add_node("investigate", investigate)
    g.add_node("tools", ToolNode(list(tools)))
    g.add_node("propose", propose)
    g.add_node("approval", approval)
    g.add_node("finalize", finalize)

    g.add_edge(START, "investigate")
    # tools_condition routes to "tools" while the model is calling tools; when it
    # stops, route to "propose" (not END) so the fix step always runs.
    g.add_conditional_edges("investigate", tools_condition, {"tools": "tools", END: "propose"})
    g.add_edge("tools", "investigate")
    g.add_edge("propose", "approval")
    g.add_edge("approval", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
