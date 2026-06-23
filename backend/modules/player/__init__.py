"""Music player module.

Exposes a single aggregate router that combines:
- Spotify OAuth + Web API proxy (/api/spotify)
- Your Vibe config, embeds, vibes, history, triggers (/api/music)
"""

from fastapi import APIRouter

from backend.modules.player.music_router import router as music_router
from backend.modules.player.router import router as spotify_router

router = APIRouter()
router.include_router(spotify_router)
router.include_router(music_router)

__all__ = ["router"]
