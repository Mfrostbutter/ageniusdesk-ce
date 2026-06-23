"""Dashboard-as-MCP — expose AgeniusDesk's own API surface to Claude Code.

Mounts a FastMCP streamable-HTTP server at /api/mcp-dashboard that wraps
read/observe operations on workflows, executions, errors, instances,
secrets metadata, and messages. Gives `claude` running in the terminal
sidecar first-class access to the same data the dashboard UI shows.

Auth: bearer token in the `Authorization: Bearer ...` header. Token is
read from the DASHBOARD_MCP_TOKEN env var (shared via .env with the
terminal sidecar). Missing token = endpoint is open — intended for
local dev only. Set the env var before exposing publicly.

The server registers itself as a known MCP in AgeniusDesk's own config
on first run so the terminal sync picks it up automatically.
"""

from .server import router  # noqa: F401
