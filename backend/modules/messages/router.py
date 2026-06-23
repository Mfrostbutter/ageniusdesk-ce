"""Messages API routes — generic notification webhook for n8n workflows."""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.modules.messages import collector

router = APIRouter(prefix="/api/messages", tags=["messages"])


class MessagePayload(BaseModel):
    title: str = ""
    body: str = ""
    level: str = "info"  # info | success | warning | error
    source: str = ""     # workflow name, system component, etc.


@router.post("/webhook")
async def receive_message(payload: MessagePayload):
    """Receive a message from any source (n8n workflow, script, integration).

    Stored in SQLite and broadcast over WebSocket so the dashboard surfaces it
    as a toast in real time.
    """
    message_id = await collector.store_message(payload.model_dump())
    return {"success": True, "message_id": message_id}


@router.get("")
async def list_messages(limit: int = 50, offset: int = 0):
    return {"messages": await collector.get_messages(limit, offset)}


@router.delete("/{message_id}")
async def delete_message(message_id: int):
    deleted = await collector.delete_message(message_id)
    return {"deleted": deleted}


@router.delete("")
async def clear_messages(before_date: str = ""):
    return {"deleted": await collector.clear_messages(before_date)}
