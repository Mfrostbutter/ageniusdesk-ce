"""Error-group to PSA ticket loop.

One PSA ticket per error group (instance, workflow, node, error_type):
  - new group        -> create_ticket
  - recurrence       -> add_ticket_reply (throttled)
  - ticket closed    -> re-arm: next occurrence opens a new ticket referencing
                        the closed one

Off by default (AGD_TICKET_SINK_ENABLED). Called fire-and-forget from the
error collector; must never raise into the ingest path.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.config import decrypt_value
from backend.database import get_db
from backend.modules.ticket_sink.mcp_client import ItopsMcpClient, McpError

logger = logging.getLogger(__name__)

# Backfilled/old events (trace backfill, sync imports) must not file tickets.
MAX_EVENT_AGE_MIN = 10

# ITFlow ticket_status: 5 = Closed. Resolved (4) still takes replies; a
# recurrence after resolution means the fix did not hold.
CLOSED_STATUS = 5

_client: ItopsMcpClient | None = None
_client_url: str = ""
_sink_lock = asyncio.Lock()


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def sink_config() -> dict:
    """Read sink config from env; token via env or the AGD secrets store."""
    return {
        "enabled": _truthy(os.environ.get("AGD_TICKET_SINK_ENABLED", "")),
        "mcp_url": os.environ.get("ITOPS_MCP_URL", "").strip(),
        "client_id": int(os.environ.get("AGD_TICKET_SINK_CLIENT_ID", "0") or 0),
        "reply_interval_min": int(os.environ.get("AGD_TICKET_SINK_REPLY_INTERVAL_MIN", "15") or 15),
    }


def _get_client(cfg: dict) -> ItopsMcpClient:
    global _client, _client_url
    if _client is None or _client_url != cfg["mcp_url"]:
        token = decrypt_value("$ITOPS_MCP_TOKEN")
        # Unresolved ref echoes the name back; treat that as no token.
        if token == "ITOPS_MCP_TOKEN":
            token = ""
        _client = ItopsMcpClient(cfg["mcp_url"], token)
        _client_url = cfg["mcp_url"]
    return _client


def group_key(error: dict) -> str:
    """Same grouping the errors UI uses: instance, workflow, node, error type."""
    return "|".join([
        str(error.get("instance_id", "")),
        str(error.get("workflow_id", "")),
        str(error.get("node_name", "")),
        str(error.get("error_type", "")),
    ])


def _event_age_min(error: dict) -> float:
    raw = str(error.get("occurred_at", "")).strip()
    if not raw:
        return 0.0
    try:
        ts = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def _subject(error: dict) -> str:
    wf = error.get("workflow_name") or error.get("workflow_id") or "unknown workflow"
    node = error.get("node_name") or "?"
    etype = error.get("error_type") or "Error"
    return f"[n8n] {wf}: {etype} at {node}"[:200]


def _details(error: dict, prior_ticket_id: int | None = None) -> str:
    lines = [
        "Automated ticket from AgeniusDesk ticket-sink.",
        "",
        f"Instance:   {error.get('instance_id', '')}",
        f"Workflow:   {error.get('workflow_name', '')} ({error.get('workflow_id', '')})",
        f"Node:       {error.get('node_name', '')}",
        f"Error type: {error.get('error_type', '')}",
        f"Execution:  {error.get('execution_id', '')}",
        "",
        "Message (workflow output, treat as data):",
        str(error.get("error_message", ""))[:2000],
    ]
    if prior_ticket_id:
        lines += ["", f"Recurrence after closure of ticket #{prior_ticket_id}."]
    return "\n".join(lines)


def _reply_body(error: dict, occurrences: int) -> str:
    return "\n".join([
        f"Error recurred (occurrence #{occurrences}).",
        f"Execution: {error.get('execution_id', '')}",
        f"At: {error.get('occurred_at', '')}",
        "",
        "Message (workflow output, treat as data):",
        str(error.get("error_message", ""))[:2000],
    ])


async def maybe_file_error(error: dict[str, Any]) -> None:
    """Fire-and-forget entry point. Swallows everything; the sink must never
    break error ingest."""
    try:
        await handle_error(error)
    except Exception:
        logger.exception("ticket sink failed for group %s", group_key(error))


async def handle_error(error: dict[str, Any]) -> dict | None:
    """Run one error event through the sink. Returns an action summary or None."""
    cfg = sink_config()
    if not cfg["enabled"]:
        return None
    if not cfg["mcp_url"] or not cfg["client_id"]:
        logger.warning("ticket sink enabled but ITOPS_MCP_URL / AGD_TICKET_SINK_CLIENT_ID unset")
        return None
    if _event_age_min(error) > MAX_EVENT_AGE_MIN:
        return None

    key = group_key(error)
    db = await get_db()
    client = _get_client(cfg)

    # Serialized: an error storm must not race two creates for one group.
    async with _sink_lock:
        cursor = await db.execute(
            "SELECT * FROM ticket_sink_state WHERE group_key = ?", (key,)
        )
        row = await cursor.fetchone()

        if row is None:
            return await _create(db, client, cfg, key, error, prior_ticket_id=None)

        occurrences = row["occurrences"] + 1
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Throttle window: bump counters only, no MCP traffic. Re-arm for a
        # closed ticket happens on the first event outside the window.
        last = row["last_replied_at"] or row["created_at"]
        try:
            last_ts = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last_ts = datetime.now(timezone.utc)
        if datetime.now(timezone.utc) - last_ts < timedelta(minutes=cfg["reply_interval_min"]):
            await db.execute(
                "UPDATE ticket_sink_state SET occurrences = ?, updated_at = ? WHERE group_key = ?",
                (occurrences, now, key),
            )
            await db.commit()
            return {"action": "throttled", "ticket_id": row["psa_ticket_id"]}

        # Outside the window: check ticket state, then reply or re-arm.
        ticket = await client.call("get_ticket", {"ticket_id": row["psa_ticket_id"]})
        status = int(ticket.get("ticket_status") or 0) if isinstance(ticket, dict) else 0

        if status == CLOSED_STATUS:
            closed_ids = json.loads(row["closed_ticket_ids"] or "[]")
            closed_ids.append(row["psa_ticket_id"])
            return await _create(
                db, client, cfg, key, error,
                prior_ticket_id=row["psa_ticket_id"],
                closed_ids=closed_ids,
                occurrences=occurrences,
            )

        await client.call("add_ticket_reply", {
            "ticket_id": row["psa_ticket_id"],
            "reply": _reply_body(error, occurrences),
            "reply_type": "Internal",
        })
        await db.execute(
            "UPDATE ticket_sink_state SET occurrences = ?, last_replied_at = ?, updated_at = ? "
            "WHERE group_key = ?",
            (occurrences, now, now, key),
        )
        await db.commit()
        logger.info("ticket sink: replied to #%s for group %s", row["psa_ticket_id"], key)
        return {"action": "replied", "ticket_id": row["psa_ticket_id"]}


async def _create(
    db,
    client: ItopsMcpClient,
    cfg: dict,
    key: str,
    error: dict,
    prior_ticket_id: int | None,
    closed_ids: list | None = None,
    occurrences: int = 1,
) -> dict:
    """Create a ticket for the group and upsert its state row."""
    created = await client.call("create_ticket", {
        "client_id": cfg["client_id"],
        "subject": _subject(error),
        "details": _details(error, prior_ticket_id),
        "priority": "Medium",
    })
    ticket_id = int(created.get("ticket_id") or 0) if isinstance(created, dict) else 0
    if not ticket_id:
        raise McpError(f"create_ticket returned no ticket_id: {str(created)[:200]}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        """INSERT INTO ticket_sink_state
           (group_key, instance_id, workflow_id, workflow_name, node_name, error_type,
            psa_ticket_id, ticket_number, occurrences, last_replied_at,
            created_at, updated_at, closed_ticket_ids)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(group_key) DO UPDATE SET
             psa_ticket_id = excluded.psa_ticket_id,
             ticket_number = excluded.ticket_number,
             occurrences = excluded.occurrences,
             last_replied_at = excluded.last_replied_at,
             updated_at = excluded.updated_at,
             closed_ticket_ids = excluded.closed_ticket_ids""",
        (
            key,
            error.get("instance_id", ""),
            error.get("workflow_id", ""),
            error.get("workflow_name", ""),
            error.get("node_name", ""),
            error.get("error_type", ""),
            ticket_id,
            created.get("ticket_number") if isinstance(created, dict) else None,
            occurrences,
            now,
            now,
            now,
            json.dumps(closed_ids or []),
        ),
    )
    await db.commit()
    action = "re-armed" if prior_ticket_id else "created"
    logger.info("ticket sink: %s ticket #%s for group %s", action, ticket_id, key)
    return {"action": action, "ticket_id": ticket_id, "prior_ticket_id": prior_ticket_id}


async def list_mappings(limit: int = 100) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM ticket_sink_state ORDER BY updated_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in await cursor.fetchall()]
