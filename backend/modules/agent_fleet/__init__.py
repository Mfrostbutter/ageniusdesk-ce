"""Agent Fleet: a managed fleet of LangGraph agents (built-in core module).

Catalog + run with a live graph/timeline + human-in-the-loop approve/resume +
LangSmith tracing. Agents are built in Code Lab and operated here, the way n8n
workflows are. The package imports cleanly at boot even when the optional
langgraph extra is absent: the heavy graph/langchain imports are lazy, inside the
runner and the registry builders, never at module scope.
"""

from backend.modules.agent_fleet.router import router

__all__ = ["router"]
