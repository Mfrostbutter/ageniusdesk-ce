"""Graph state.

MessagesState already gives us the `messages` channel (the agent loop). We extend
it with one explicit field so the error under investigation is visible as its own
piece of state, which is handy in LangGraph Studio and a clean place to show
state extension.
"""

from __future__ import annotations

from langgraph.graph import MessagesState


class TriageState(MessagesState):
    # The error/context the run was kicked off with (free text or "latest").
    triage_target: str
