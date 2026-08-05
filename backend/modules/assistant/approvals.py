"""Human confirmation gate for state-changing assistant tool calls.

Why this exists: the assistant reads content the operator does not control — n8n
error payloads, execution run-data, RAG/Qdrant hits, MCP tool output — and a
model cannot reliably tell "the operator asked for this" from "some text I read
told me to do this". The system prompt says to treat retrieved content as data
(see ``providers._ASSISTANT_INJECTION_GUARD``), and that stays, but a textual
instruction is not a security boundary. This is the boundary: a state-changing
tool does not execute during a chat turn. It becomes a proposal the operator
confirms with an explicit click.

Flow:
  1. The model selects e.g. ``set_workflow_active``. The tool loop calls
     :func:`create` instead of executing, and hands the model back
     :func:`pending_notice` as the tool result, so the turn completes normally
     with the model reporting what it wants to do.
  2. ``providers.chat`` returns the proposals as ``pending_actions``; the
     assistant dock renders each with a Confirm button.
  3. The click POSTs to ``/api/assistant/tools/confirm``, which is operator-gated
     and CSRF-checked like every other browser mutation. Only then does
     :func:`execute_approved` run the tool.

Proposals are in-memory and single-use: a restart drops them (the operator just
asks again), and a confirmed id cannot be replayed.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time

from backend import audit
from backend.config import settings

logger = logging.getLogger(__name__)

# A proposal the operator never acts on is garbage after a while. 15 minutes is
# well past the span of a chat turn but short enough that a stale "activate this
# workflow" cannot be confirmed hours later out of context.
_TTL_SECONDS = 900.0
_MAX_PENDING = 200

_lock = threading.Lock()
_pending: dict[str, dict] = {}


def autorun_enabled() -> bool:
    """True when the operator opted into unattended tool execution."""
    return bool(settings.agd_assistant_autorun)


def needs_confirmation(tool_name: str, is_mcp: bool, mcp_info: dict | None = None) -> bool:
    """Whether this tool call must be confirmed by a human before it runs.

    Built-ins: only the state-changing ones gate. Read-only built-ins
    (list_workflows, get_execution, ...) never do — they change nothing, and
    gating them would make the assistant useless.

    MCP: decided per server, then per tool.
      - The server's `confirm` policy is the operator's control. "none" trusts
        the server outright (correct for a docs-only n8n-mcp with no instance
        creds), "all" gates everything, "writes" (default) gates what we classify
        as a write.
      - Under "writes", classification comes from the server's own MCP
        annotations, else a known convention profile (see
        mcp_client.classify_read_only). If neither can tell, we fail closed and
        gate: an unknown tool on an unrecognized server is a write until proven
        otherwise.

    AGD_ASSISTANT_CONFIRM_MCP=false disables the MCP half wholesale, for an
    operator who wants the gate only on the built-in write tools.
    """
    from backend.modules.assistant import mcp_client
    from backend.modules.assistant.tools import STATE_CHANGING_TOOLS

    if autorun_enabled():
        return False
    if not is_mcp:
        return tool_name in STATE_CHANGING_TOOLS
    if not settings.agd_assistant_confirm_mcp:
        return False

    info = mcp_info or {}
    policy = info.get("confirm") or mcp_client.DEFAULT_CONFIRM
    if policy == mcp_client.CONFIRM_NONE:
        return False
    if policy == mcp_client.CONFIRM_ALL:
        return True
    # CONFIRM_WRITES: read-only runs, write and unclassifiable gate.
    return info.get("read_only") is not True


def _reap_locked(now: float) -> None:
    for key in [k for k, v in _pending.items() if now - v["created_at"] > _TTL_SECONDS]:
        del _pending[key]
    while len(_pending) > _MAX_PENDING:
        oldest = min(_pending, key=lambda k: _pending[k]["created_at"])
        del _pending[oldest]


def create(
    tool: str,
    arguments: dict,
    *,
    is_mcp: bool = False,
    server_id: str = "",
    real_name: str = "",
    reasoning: str = "",
) -> dict:
    """Record a proposed tool call and return its public view."""
    now = time.time()
    record = {
        "id": secrets.token_urlsafe(12),
        "tool": tool,
        "arguments": arguments or {},
        "is_mcp": is_mcp,
        "server_id": server_id,
        "real_name": real_name or tool,
        "reasoning": reasoning,
        "created_at": now,
    }
    with _lock:
        _reap_locked(now)
        _pending[record["id"]] = record
    audit.record(
        "assistant.tool.proposed",
        tool=tool,
        proposal_id=record["id"],
        is_mcp=is_mcp,
        server_id=server_id,
        arguments=arguments,
    )
    return public_view(record)


def public_view(record: dict) -> dict:
    """The proposal as the frontend sees it. Arguments are scrubbed: the operator
    needs to see what would run, not any credential that rode along in it."""
    return {
        "id": record["id"],
        "tool": record["tool"],
        "arguments": audit.scrub(record["arguments"]),
        "is_mcp": record["is_mcp"],
        "server_id": record["server_id"],
        "reasoning": record["reasoning"],
    }


def pop(proposal_id: str) -> dict | None:
    """Claim a proposal for execution. Single-use: a second pop returns None."""
    now = time.time()
    with _lock:
        record = _pending.pop(proposal_id, None)
    if record is None:
        return None
    if now - record["created_at"] > _TTL_SECONDS:
        return None
    return record


def pending_notice(tool: str) -> str:
    """The tool result handed back to the model in place of execution.

    Phrased so the model reports the proposal and stops, rather than retrying the
    same call in the next round and burning the tool-round budget.
    """
    return (
        f"NOT EXECUTED. `{tool}` is a state-changing action, so it was not run. "
        "A confirmation card has been shown to the operator, who must approve it "
        "explicitly. Tell the operator what you proposed and why, and do not "
        "attempt this tool again in this turn."
    )


async def execute_approved(record: dict) -> str:
    """Run a proposal the operator confirmed. Returns the tool's result string."""
    from backend.modules.assistant.mcp_client import execute_tool as mcp_execute
    from backend.modules.assistant.tools import execute_tool

    tool = record["tool"]
    try:
        if record["is_mcp"]:
            result = await mcp_execute(record["server_id"], record["real_name"], record["arguments"])
        else:
            result = await execute_tool(tool, record["arguments"])
    except Exception as e:  # noqa: BLE001 - the audit line is the point
        audit.record(
            "assistant.tool.executed",
            outcome="error",
            tool=tool,
            proposal_id=record["id"],
            is_mcp=record["is_mcp"],
            arguments=record["arguments"],
            error=str(e),
        )
        raise
    audit.record(
        "assistant.tool.executed",
        tool=tool,
        proposal_id=record["id"],
        is_mcp=record["is_mcp"],
        server_id=record["server_id"],
        arguments=record["arguments"],
        result=result,
    )
    return result


def reject(proposal_id: str) -> bool:
    """Discard a proposal the operator declined. True if it existed."""
    record = pop(proposal_id)
    if record is None:
        return False
    audit.record(
        "assistant.tool.rejected",
        outcome="rejected",
        tool=record["tool"],
        proposal_id=proposal_id,
        arguments=record["arguments"],
    )
    return True


def _reset() -> None:
    """Drop all pending proposals. Test hook."""
    with _lock:
        _pending.clear()
