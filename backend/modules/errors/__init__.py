"""Error collector module — receives, stores, and broadcasts workflow errors."""

from backend.modules.errors.router import router

__all__ = ["router"]
