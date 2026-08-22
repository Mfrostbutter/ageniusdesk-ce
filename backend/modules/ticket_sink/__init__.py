"""Ticket sink module — files a PSA ticket per error group via itops-mcp."""

from backend.modules.ticket_sink.router import router

__all__ = ["router"]
