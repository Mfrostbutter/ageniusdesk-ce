"""Entrypoint for `langgraph dev` / LangGraph Studio.

Best-effort developer tool; the in-app Agent Fleet view is the primary live view.
The dashboard never imports this module — the runner builds the graphs itself with
the in-process tools and a lazily resolved key. Repo-root `langgraph.json` points
here (dotted module path), so normal package-relative imports work.

Needs in env (.env): ANTHROPIC_API_KEY (or ANTHROPIC_KEY), optionally LANGSMITH_*.
The n8n tools call backend.* and only work inside AgeniusDesk; in Studio use the
graph view to inspect topology and run agents for real from the dashboard.
"""

from __future__ import annotations

import os

# Don't ship traces without a key (avoids noisy 401s); lights up the moment one exists.
if os.environ.get("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes") and not os.environ.get("LANGSMITH_API_KEY"):
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

# langchain-anthropic reads ANTHROPIC_API_KEY; accept ANTHROPIC_KEY too.
if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANTHROPIC_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_KEY"]

from langchain_anthropic import ChatAnthropic  # noqa: E402

from ..tools_local import TOOLS  # noqa: E402
from .graph import build_graph  # noqa: E402
from .graph_health import build_health_graph  # noqa: E402
from .graph_hitl import build_fix_graph  # noqa: E402

_llm = ChatAnthropic(
    model=os.environ.get("OPS_TRIAGE_MODEL", "claude-sonnet-4-6"),
    temperature=0,
    max_tokens=int(os.environ.get("OPS_TRIAGE_MAX_TOKENS", "2048")),
)

# ops_triage: the classic ReAct tool loop.
graph = build_graph(_llm, TOOLS)

# fix_proposer: human-in-the-loop. The dev server supplies the checkpointer, so its
# interrupt() pauses are inspectable + resumable right in Studio.
fix_proposer = build_fix_graph(_llm, TOOLS)

# health_reporter: parallel fan-out (3 lenses) -> synthesize.
health_reporter = build_health_graph(_llm, TOOLS)
