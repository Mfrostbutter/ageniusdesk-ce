"""Workspace (harness) file tools for the AI assistant.

Thin wrappers over notes.storage / notes.index so every agent can read and
write the containerized workspace, the harness all agents work within. All
paths are relative to the workspace root and sandboxed by storage.resolve()
(no `..`, no absolute paths, no escape outside the root).

Wired into assistant.tools.TOOL_DEFINITIONS / execute_tool, so the tools flow
through both the OpenAI-compatible and Anthropic provider paths automatically.
"""

from __future__ import annotations

import logging

from backend.modules.notes import index as _index
from backend.modules.notes import storage

logger = logging.getLogger(__name__)

_LAYOUT_HINT = (
    "Paths are relative to the workspace root. Conventions: user/ human notes, "
    "agent/ your scratchpads, docs/ documentation, workflows/ saved n8n workflow "
    "JSON, research/ add-in output, shared/ canonical facts, sessions/ session logs. "
    "AGENTS.md at the root holds the instructions that steer all agents."
)

WORKSPACE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "workspace_list",
            "description": (
                "List files and folders in the workspace (the harness filesystem). "
                + _LAYOUT_HINT
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Optional folder prefix to list under, e.g. 'workflows/'. Empty lists the whole tree.",
                        "default": "",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_read",
            "description": "Read a file from the workspace and return its full contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'docs/runbook.md'."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_write",
            "description": (
                "Create or overwrite a file in the workspace. Creates parent folders. "
                "Use this to save workflows, documentation, or notes. " + _LAYOUT_HINT
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'workflows/daily-sync.json'."},
                    "content": {"type": "string", "description": "Full file contents to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_append",
            "description": "Append content to a workspace file (creates it if missing). Good for accumulating scratchpads or logs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'agent/scratch.md'."},
                    "content": {"type": "string", "description": "Content to append."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_search",
            "description": "Full-text search across the workspace (title, body, tags). Returns matching paths with snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "tag": {"type": "string", "description": "Optional tag to filter by.", "default": ""},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_archive",
            "description": "Soft-delete a workspace file by moving it to the archive. Never hard-deletes; the file can be recovered.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to archive."},
                },
                "required": ["path"],
            },
        },
    },
]


def _flatten_tree(node: dict, prefix: str = "") -> list[str]:
    out: list[str] = []
    for child in node.get("children", []):
        name = child.get("name", "")
        rel = f"{prefix}{name}"
        if child.get("type") == "dir":
            out.append(rel + "/")
            out.extend(_flatten_tree(child, rel + "/"))
        else:
            out.append(rel)
    return out


async def execute_workspace_tool(name: str, arguments: dict) -> str:
    """Execute a workspace_* tool and return a string result for the LLM."""
    try:
        storage.ensure_vault()

        if name == "workspace_list":
            prefix = (arguments.get("path") or "").lstrip("/")
            entries = _flatten_tree(storage.list_tree())
            if prefix:
                entries = [e for e in entries if e.startswith(prefix)]
            if not entries:
                return f"No files found{f' under {prefix}' if prefix else ' in the workspace'}."
            return "Workspace files:\n" + "\n".join(f"- {e}" for e in entries)

        if name == "workspace_read":
            path = arguments["path"]
            try:
                return storage.read(path)
            except FileNotFoundError:
                return f"File not found: {path}"

        if name == "workspace_write":
            path = arguments["path"]
            meta = await storage.write(path, arguments.get("content", ""))
            return f"Wrote {meta['path']} ({meta['size']} bytes)."

        if name == "workspace_append":
            path = arguments["path"]
            meta = await storage.append(path, arguments.get("content", ""))
            return f"Appended to {meta['path']} (now {meta['size']} bytes)."

        if name == "workspace_search":
            query = arguments["query"]
            tag = arguments.get("tag") or None
            rows = await _index.search(query, tag=tag, limit=20)
            if not rows:
                return f"No results for '{query}'."
            lines = [f"{len(rows)} result(s) for '{query}':\n"]
            for r in rows:
                snippet = (r.get("snippet") or "").replace("\n", " ").strip()
                lines.append(f"- **{r.get('path')}**{f' — {snippet}' if snippet else ''}")
            return "\n".join(lines)

        if name == "workspace_archive":
            path = arguments["path"]
            try:
                result = await storage.archive(path)
            except FileNotFoundError:
                return f"File not found: {path}"
            return f"Archived {path} -> {result.get('archived_to', 'archive')}."

        return f"Unknown workspace tool: {name}"

    except ValueError as e:
        # storage.resolve() raises ValueError on path-escape / invalid path.
        return f"Invalid path: {e}"
    except Exception as e:  # noqa: BLE001 - tool results must be strings, never raise
        logger.error("Workspace tool error (%s): %s", name, e)
        return f"Workspace tool error: {e}"
