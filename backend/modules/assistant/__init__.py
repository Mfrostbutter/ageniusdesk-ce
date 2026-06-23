"""AI Assistant module — chat with context from workflows, errors, and optional RAG + MCP."""

from fastapi import APIRouter

from backend.modules.assistant.mcp_router import router as mcp_router
from backend.modules.assistant.router import router as assistant_router

# Combine both routers
router = APIRouter()
router.include_router(assistant_router)
router.include_router(mcp_router)

__all__ = ["router"]
