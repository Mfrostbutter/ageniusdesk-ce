"""n8n tools for the AI assistant — allows the LLM to interact with n8n via function calling."""

import json
import logging

from backend.modules.n8n_proxy import client as n8n
from backend.modules.assistant.workspace_tools import (
    WORKSPACE_TOOL_DEFINITIONS,
    execute_workspace_tool,
)

logger = logging.getLogger(__name__)

# State-changing tools the chat LLM can call. The assistant ingests
# attacker-influenceable content (RAG results, MCP output, n8n error/execution
# payloads), so a prompt injection could steer the model to invoke one of these
# without the operator asking. We can't fully prevent that from the backend, but
# every invocation is logged to an audit trail so a rogue action is visible
# after the fact. The system prompt (see providers._ASSISTANT_INJECTION_GUARD)
# instructs the model to treat tool/RAG/MCP content as data, never instructions.
_STATE_CHANGING_TOOLS = frozenset({
    "trigger_workflow", "set_workflow_active", "import_workflow",
    "workspace_write", "workspace_append", "workspace_archive",
})
_audit = logging.getLogger("agd.assistant.audit")


def _audit_state_change(name: str, arguments: dict) -> None:
    """Emit an audit line for a state-changing assistant tool call."""
    keys = ("workflow_id", "active", "name", "path")
    summary = {k: arguments[k] for k in keys if k in arguments}
    _audit.warning("assistant tool %s invoked args=%s", name, summary)


# Tool definitions in OpenAI function calling format (works with OpenRouter)
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_workflows",
            "description": (
                "List all n8n workflows with their names, "
                "active status, and trigger type. Use this "
                "when the user asks about their workflows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "active_only": {"type": "boolean", "description": "Only show active workflows", "default": False},
                    "name_contains": {"type": "string", "description": "Filter by name substring", "default": ""},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workflow",
            "description": (
                "Get details of a specific workflow including "
                "nodes, webhook URL, and tags. Use when the "
                "user asks about a specific workflow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "The workflow ID"},
                },
                "required": ["workflow_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_workflow",
            "description": (
                "Trigger/run an n8n workflow. Use when the "
                "user asks to run, trigger, or execute a workflow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "The workflow ID to trigger"},
                    "payload": {
                        "type": "object",
                        "description": "Optional JSON payload to pass to the workflow",
                        "default": {},
                    },
                },
                "required": ["workflow_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_executions",
            "description": (
                "List recent workflow executions with status "
                "and timing. Use when the user asks about "
                "recent runs or execution history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "Filter by workflow ID", "default": ""},
                    "status": {
                        "type": "string",
                        "enum": [
                            "success", "error", "running",
                            "waiting", "",
                        ],
                        "description": "Filter by status",
                        "default": "",
                    },
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_execution",
            "description": (
                "Get details of a specific execution including "
                "error info and node results. Use when "
                "diagnosing a specific failed execution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "execution_id": {"type": "string", "description": "The execution ID"},
                },
                "required": ["execution_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_workflow_active",
            "description": (
                "Activate or deactivate an n8n workflow. "
                "Use when the user asks to enable, disable, "
                "turn on, or turn off a workflow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "The workflow ID"},
                    "active": {"type": "boolean", "description": "True to activate, False to deactivate"},
                },
                "required": ["workflow_id", "active"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_workflow",
            "description": (
                "Create and import a new workflow into the "
                "active n8n instance. Use when the user asks "
                "you to build, create, or set up a new "
                "workflow. Provide the full workflow JSON "
                "with nodes and connections."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new workflow"},
                    "nodes": {
                        "type": "array",
                        "description": "Array of n8n node objects with parameters, type, position",
                        "items": {"type": "object"},
                    },
                    "connections": {"type": "object", "description": "Connection map between nodes"},
                },
                "required": ["name", "nodes", "connections"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_errors",
            "description": "Get recent workflow errors from the dashboard error log. Use when diagnosing problems.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max errors to return", "default": 10},
                },
            },
        },
    },
]

# Workspace (harness) file tools — agents can read/write the containerized
# workspace. Appended so they ship to both provider paths automatically.
TOOL_DEFINITIONS += WORKSPACE_TOOL_DEFINITIONS


async def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool call and return the result as a string for the LLM."""
    try:
        if name in _STATE_CHANGING_TOOLS:
            _audit_state_change(name, arguments)

        if name.startswith("workspace_"):
            return await execute_workspace_tool(name, arguments)

        if name == "list_workflows":
            result = await n8n.list_workflows(
                active_only=arguments.get("active_only", False),
                name_contains=arguments.get("name_contains", ""),
                limit=50,
            )
            workflows = result.get("workflows", [])
            if not workflows:
                return "No workflows found."
            lines = [f"Found {len(workflows)} workflows:\n"]
            for w in workflows:
                status = "ACTIVE" if w["active"] else "off"
                lines.append(f"- **{w['name']}** (ID: `{w['id']}`) — {status}, trigger: {w['trigger_type']}")
            return "\n".join(lines)

        elif name == "get_workflow":
            result = await n8n.get_workflow(arguments["workflow_id"])
            if not result:
                return "Workflow not found."
            return json.dumps(result, indent=2)

        elif name == "trigger_workflow":
            result = await n8n.trigger_workflow(
                arguments["workflow_id"],
                arguments.get("payload"),
            )
            if result.get("success"):
                method = result.get("method", "API")
                exec_id = result.get("execution_id", "N/A")
                return (
                    f"Workflow triggered successfully via "
                    f"{method}. Execution ID: {exec_id}"
                )
            return f"Failed to trigger workflow: {result.get('error', 'Unknown error')}"

        elif name == "list_executions":
            result = await n8n.list_executions(
                workflow_id=arguments.get("workflow_id", ""),
                status=arguments.get("status", ""),
                limit=arguments.get("limit", 10),
            )
            executions = result.get("executions", [])
            if not executions:
                return "No executions found."
            lines = [f"Found {len(executions)} executions:\n"]
            for e in executions:
                lines.append(f"- **{e['workflow_name']}** (Exec `{e['id']}`) — {e['status']}, {e['started_at'][:19]}")
            return "\n".join(lines)

        elif name == "get_execution":
            result = await n8n.get_execution(arguments["execution_id"])
            if not result:
                return "Execution not found."
            return json.dumps(result, indent=2)

        elif name == "set_workflow_active":
            result = await n8n.set_workflow_active(
                arguments["workflow_id"],
                arguments["active"],
            )
            action = "activated" if arguments["active"] else "deactivated"
            if result.get("success"):
                return f"Workflow {action} successfully."
            return f"Failed to {action[:-1]}e workflow: {result.get('error', 'Unknown error')}"

        elif name == "import_workflow":
            workflow_data = {
                "name": arguments.get("name", "AI-Generated Workflow"),
                "nodes": arguments.get("nodes", []),
                "connections": arguments.get("connections", {}),
                "settings": {"executionOrder": "v1"},
            }
            result = await n8n.import_workflow(workflow_data)
            if result.get("success"):
                wf_name = result.get("name", workflow_data["name"])
                wf_id = result.get("workflow_id")
                return (
                    f"Workflow **{wf_name}** created "
                    f"successfully! ID: `{wf_id}`. It's "
                    "imported as inactive, activate it "
                    "when ready."
                )
            return f"Failed to create workflow: {result.get('error', 'Unknown error')}"

        elif name == "get_recent_errors":
            from backend.modules.errors.collector import get_errors
            errors = await get_errors(limit=arguments.get("limit", 10))
            if not errors:
                return "No recent errors."
            lines = ["Recent errors:\n"]
            for e in errors:
                node = e["node_name"] or "unknown node"
                msg = e["error_message"][:150]
                lines.append(
                    f"- **{e['workflow_name']}** ({node}): "
                    f"{msg}, {e['occurred_at']}"
                )
            return "\n".join(lines)

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        logger.error("Tool execution error (%s): %s", name, e)
        return f"Tool error: {e}"
