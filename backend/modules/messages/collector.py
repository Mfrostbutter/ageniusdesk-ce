"""Message storage and WebSocket broadcast."""

import logging
from datetime import datetime, timezone
from typing import Any

from backend.database import get_db
from backend.websocket import manager

logger = logging.getLogger(__name__)

ALLOWED_LEVELS = {"info", "success", "warning", "error"}


async def store_message(message: dict[str, Any]) -> int:
    """Persist a message and broadcast it to connected clients. Returns the row id."""
    occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    level = message.get("level", "info")
    if level not in ALLOWED_LEVELS:
        level = "info"

    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO messages (title, body, level, source, occurred_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            message.get("title", ""),
            message.get("body", ""),
            level,
            message.get("source", ""),
            occurred_at,
        ),
    )
    await db.commit()
    message_id = cursor.lastrowid

    broadcast_data = {
        "id": message_id,
        "occurred_at": occurred_at,
        "title": message.get("title", ""),
        "body": message.get("body", ""),
        "level": level,
        "source": message.get("source", ""),
    }
    await manager.broadcast("message", broadcast_data)
    logger.info("Message [%s] %s: %s", level, message.get("source", ""), (message.get("title") or message.get("body") or "")[:80])

    return message_id


async def get_messages(limit: int = 50, offset: int = 0) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM messages ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def clear_messages(before_date: str = "") -> int:
    db = await get_db()
    if before_date:
        cursor = await db.execute("DELETE FROM messages WHERE occurred_at < ?", (before_date,))
    else:
        cursor = await db.execute("DELETE FROM messages")
    await db.commit()
    return cursor.rowcount


async def delete_message(message_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    await db.commit()
    return cursor.rowcount > 0
