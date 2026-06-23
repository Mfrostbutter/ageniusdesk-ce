"""Module manager — the module that manages modules.

Exposes /api/modules endpoints for listing, installing, and uninstalling
both built-in and community modules. Also serves /modules/{id}/static/
for community module frontend assets.
"""

from fastapi import APIRouter

from .router import router as _api_router
from .static_router import router as _static_router

router = APIRouter()
router.include_router(_api_router)
router.include_router(_static_router)
