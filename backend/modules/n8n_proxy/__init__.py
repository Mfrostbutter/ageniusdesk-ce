"""n8n API proxy module: workflow listing, execution history, triggering, instance capabilities."""

from fastapi import APIRouter

from backend.modules.n8n_proxy.router import router as _operator_router
from backend.modules.n8n_proxy.router import viewer_router as _viewer_router

# Operator routes plus the viewer-readable capability read.
router = APIRouter()
router.include_router(_operator_router)
router.include_router(_viewer_router)

__all__ = ["router"]
