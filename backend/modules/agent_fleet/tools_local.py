"""The in-process read toolset the fleet agents use over the n8n fleet.

Agent Fleet runs in_process (Option A), so each tool calls the owning AgeniusDesk
module directly rather than going out over HTTP. Each tool is `async def` and the
graph's agent node is async too, so the whole loop stays on the event loop with no
run_coroutine_threadsafe gymnastics.

These imports reach `backend.*`. That is expected for an in_process community
module (see CONTRIBUTING): there is no sandbox in_process, so the module really
does call host internals, and the static scanner flags it as a host-import HIGH.
The tool docstrings are the spec the model sees; keep them tight and read-only.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool


@tool
async def list_recent_errors(limit: int = 10) -> list[dict]:
    """List the most recent n8n workflow errors AgeniusDesk has collected.

    Returns rows with id, workflow_id, workflow_name, execution_id, node_name,
    error_message, error_type, occurred_at. Start here to pick what to triage.
    """
    from backend.config import get_active_instance_id
    from backend.modules.errors import collector

    scope = get_active_instance_id() or ""
    return await collector.get_errors(limit=limit, instance_id=scope)


@tool
async def errors_for_workflow(workflow_id: str, limit: int = 20) -> list[dict]:
    """List recent errors for ONE workflow id. Use to judge whether a failure is a
    one-off or a recurring pattern."""
    from backend.config import get_active_instance_id
    from backend.modules.errors import collector

    scope = get_active_instance_id() or ""
    return await collector.get_errors(limit=limit, workflow_id=workflow_id, instance_id=scope)


@tool
async def get_workflow(workflow_id: str) -> dict:
    """Fetch a workflow's definition (its nodes, connections, settings) from the
    active n8n instance. Use to understand the node that failed and what feeds it."""
    from backend.modules.n8n_proxy import client

    return await client.get_workflow(workflow_id)


@tool
async def list_executions(workflow_id: str = "", status: str = "", limit: int = 10) -> Any:
    """List recent executions, optionally filtered by workflow_id and status
    (e.g. 'error'). Use to see how often a workflow runs and how often it fails."""
    from backend.modules.n8n_proxy import client

    return await client.list_executions(workflow_id, status, limit)


@tool
async def get_execution(execution_id: str) -> dict:
    """Fetch a single execution by id, including its run data / error detail. This
    is where the actual failure payload and stack live; inspect it before
    proposing a fix."""
    from backend.modules.n8n_proxy import client

    return await client.get_execution(execution_id)


@tool
async def fleet_health() -> dict:
    """Get the HA summary for the active instance: health_status, workflow_count,
    error_count_24h, last_execution_at. Use for context on whether this error is
    part of a broader outage."""
    from backend.modules.public_api.summary import build_ha_summary

    return await build_ha_summary()


TOOLS = [
    list_recent_errors,
    errors_for_workflow,
    get_workflow,
    list_executions,
    get_execution,
    fleet_health,
]

# Name -> tool, so a vault agent's manifest can declare the tools it uses by name
# and the runtime injects the @tool objects into the agent's pure graph factory.
TOOL_REGISTRY = {t.name: t for t in TOOLS}


def resolve_tools(names: list[str] | None) -> list:
    """Resolve tool names to @tool objects (unknown names skipped). Empty/None means
    the full built-in toolset, a sensible default for an unscoped agent."""
    if not names:
        return list(TOOLS)
    return [TOOL_REGISTRY[n] for n in names if n in TOOL_REGISTRY]


def tool_catalog() -> list[dict]:
    """[{name, description}] for the builder UI / agent introspection."""
    return [{"name": t.name, "description": (t.description or "").strip()} for t in TOOLS]
