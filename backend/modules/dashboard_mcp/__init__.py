"""Dashboard-as-MCP: expose AgeniusDesk's own API surface to Claude Code.

Mounts a FastMCP streamable-HTTP server at /api/mcp-dashboard that wraps
read/observe operations on workflows, executions, errors, instances, secrets
metadata, and messages. It gives `claude` running in the terminal sidecar
first-class access to the same data the dashboard UI shows.

Auth: browser sessions can reach it through the dashboard's internal API gate.
External clients should send `Authorization: Bearer ...` with the token read
from the DASHBOARD_MCP_TOKEN env var. Set the env var before exposing this
endpoint to non-browser clients.

The server registers itself as a known MCP in AgeniusDesk's own config on first
run so the terminal sync picks it up automatically.
"""

from .server import router  # noqa: F401
