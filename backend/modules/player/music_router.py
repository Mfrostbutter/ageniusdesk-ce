"""Your Vibe (music) API routes.

All routes live under /api/music. Backend-synced state for:
- appearance + behavior preferences
- custom embed library
- vibe playlists (named URL bundles)
- history (server-synced, replaces pure localStorage)
- n8n triggers (webhook + workflow map)
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from backend.modules.player.music_config import (
    DEFAULT_MUSIC,
    add_to_collection,
    ensure_trigger_token,
    load_music_config,
    remove_from_collection,
    rotate_trigger_token,
    save_music_config,
    update_in_collection,
    update_music_section,
)
from backend.modules.player.sanitizer import KNOWN_EMBED_HOSTS, sanitize_embed

router = APIRouter(prefix="/api/music", tags=["music"])


# ── Built-in embed templates ──────────────────────────────────────────────────

EMBED_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "bandcamp",
        "name": "Bandcamp",
        "icon": "💿",
        "color": "#629aa9",
        "hint": "On a Bandcamp album page click 'Share/Embed' → 'Embed this album' and paste the iframe here.",
        "example": '<iframe style="border: 0; width: 100%; height: 120px;" src="https://bandcamp.com/EmbeddedPlayer/album=XXXXXX/size=large/" seamless></iframe>',
    },
    {
        "key": "mixcloud",
        "name": "Mixcloud",
        "icon": "🎧",
        "color": "#52aaed",
        "hint": "On a Mixcloud mix click Share → Embed and paste the iframe.",
        "example": '<iframe width="100%" height="120" src="https://player-widget.mixcloud.com/widget/iframe/?feed=%2FUser%2FMixName%2F" frameborder="0"></iframe>',
    },
    {
        "key": "radio-garden",
        "name": "Radio Garden",
        "icon": "🌍",
        "color": "#69c261",
        "hint": "Paste a radio.garden station URL. The station will play in an iframe.",
        "example": "https://radio.garden/listen/STATION-ID",
    },
    {
        "key": "navidrome",
        "name": "Navidrome",
        "icon": "🎶",
        "color": "#1ed760",
        "hint": "Paste your Navidrome server URL. Make sure your Navidrome is reachable from the dashboard.",
        "example": "https://navidrome.example.com",
    },
    {
        "key": "jellyfin",
        "name": "Jellyfin",
        "icon": "🪼",
        "color": "#00a4dc",
        "hint": "Paste your Jellyfin web URL (make sure CSP allows embedding).",
        "example": "https://jellyfin.example.com",
    },
    {
        "key": "plex",
        "name": "Plex",
        "icon": "🟨",
        "color": "#e5a00d",
        "hint": "Paste your Plex Web URL. Plex may block embedding via X-Frame-Options.",
        "example": "https://app.plex.tv/desktop",
    },
    {
        "key": "lastfm",
        "name": "Last.fm",
        "icon": "🔴",
        "color": "#d51007",
        "hint": "Last.fm does not ship an official embed — paste a URL to an artist/track page.",
        "example": "https://www.last.fm/music/Radiohead",
    },
    {
        "key": "internet-radio",
        "name": "Internet Radio",
        "icon": "📻",
        "color": "#ff6d5a",
        "hint": "Paste a direct stream URL (e.g. .mp3, .aac, .ogg, icecast/shoutcast).",
        "example": "https://stream.somafm.com/groovesalad.pls",
    },
]


# ── Models ────────────────────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    appearance: dict[str, Any] | None = None
    behavior:   dict[str, Any] | None = None
    history_cap: int | None = None


class EmbedCreate(BaseModel):
    name: str
    raw: str                               # user-pasted iframe HTML or URL
    icon: str | None = "🎵"
    color: str | None = "#ff6d5a"
    template_key: str | None = None


class EmbedUpdate(BaseModel):
    name: str | None = None
    icon: str | None = None
    color: str | None = None
    raw: str | None = None                 # re-sanitize if provided


class VibeCreate(BaseModel):
    name: str
    description: str | None = ""
    urls: list[str] = Field(default_factory=list)
    icon: str | None = "🎵"
    color: str | None = "#a78bfa"


class VibeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    urls: list[str] | None = None
    icon: str | None = None
    color: str | None = None


class HistoryAdd(BaseModel):
    url: str
    title: str | None = None
    tags: list[str] | None = None


class HistoryPatch(BaseModel):
    pinned: bool | None = None
    tags: list[str] | None = None
    title: str | None = None


class TriggerUpdate(BaseModel):
    enabled: bool | None = None
    on_workflow_error:   dict[str, Any] | None = None
    on_workflow_success: dict[str, Any] | None = None
    workflow_map: list[dict[str, Any]] | None = None


class TriggerFire(BaseModel):
    action: str                            # play | pause | next | prev
    url: str | None = None                 # for action=play
    workflow_id: str | None = None         # optional, for logging/workflow_map lookup
    instance_id: str | None = None


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config() -> dict[str, Any]:
    """Full music config (includes all sections)."""
    return load_music_config()


@router.put("/config")
async def put_config(update: ConfigUpdate) -> dict[str, Any]:
    """Patch appearance / behavior / history_cap. Collection sections use their own routes."""
    music = load_music_config()
    if update.appearance is not None:
        music["appearance"] = {**music.get("appearance", {}), **update.appearance}
    if update.behavior is not None:
        music["behavior"] = {**music.get("behavior", {}), **update.behavior}
    if update.history_cap is not None:
        music["history_cap"] = max(10, min(1000, update.history_cap))
    return save_music_config(music)


@router.post("/config/reset")
async def reset_config() -> dict[str, Any]:
    """Reset appearance + behavior to defaults. Does not touch embeds/vibes/history."""
    music = load_music_config()
    music["appearance"] = dict(DEFAULT_MUSIC["appearance"])
    music["behavior"]   = dict(DEFAULT_MUSIC["behavior"])
    return save_music_config(music)


# ── Custom embeds ─────────────────────────────────────────────────────────────

@router.get("/embeds")
async def list_embeds() -> dict[str, Any]:
    music = load_music_config()
    return {
        "items":     music.get("custom_embeds", []),
        "templates": EMBED_TEMPLATES,
        "known_hosts": sorted(KNOWN_EMBED_HOSTS),
    }


@router.post("/embeds/preview")
async def preview_embed(req: EmbedCreate) -> dict[str, Any]:
    """Sanitize user input and return the cleaned iframe without saving."""
    result = sanitize_embed(req.raw)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Invalid embed"))
    return result


@router.post("/embeds")
async def create_embed(req: EmbedCreate) -> dict[str, Any]:
    result = sanitize_embed(req.raw)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Invalid embed"))

    item = {
        "name":         req.name.strip() or "Untitled",
        "icon":         req.icon or "🎵",
        "color":        req.color or "#ff6d5a",
        "template_key": req.template_key,
        "src":          result["src"],
        "host":         result["host"],
        "attrs":        result["attrs"],
        "html":         result["html"],
    }
    return add_to_collection("custom_embeds", item)


@router.put("/embeds/{embed_id}")
async def update_embed(embed_id: str, req: EmbedUpdate) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if req.name is not None:  patch["name"]  = req.name.strip() or "Untitled"
    if req.icon is not None:  patch["icon"]  = req.icon
    if req.color is not None: patch["color"] = req.color
    if req.raw is not None:
        result = sanitize_embed(req.raw)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Invalid embed"))
        patch["src"]   = result["src"]
        patch["host"]  = result["host"]
        patch["attrs"] = result["attrs"]
        patch["html"]  = result["html"]

    updated = update_in_collection("custom_embeds", embed_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Embed not found")
    return updated


@router.delete("/embeds/{embed_id}")
async def delete_embed(embed_id: str) -> dict[str, Any]:
    if not remove_from_collection("custom_embeds", embed_id):
        raise HTTPException(status_code=404, detail="Embed not found")
    return {"success": True}


# ── Vibes ─────────────────────────────────────────────────────────────────────

@router.get("/vibes")
async def list_vibes() -> dict[str, Any]:
    return {"items": load_music_config().get("vibes", [])}


@router.post("/vibes")
async def create_vibe(req: VibeCreate) -> dict[str, Any]:
    item = {
        "name":        req.name.strip() or "Untitled",
        "description": req.description or "",
        "urls":        [u for u in req.urls if u.strip()],
        "icon":        req.icon or "🎵",
        "color":       req.color or "#a78bfa",
    }
    return add_to_collection("vibes", item)


@router.put("/vibes/{vibe_id}")
async def update_vibe(vibe_id: str, req: VibeUpdate) -> dict[str, Any]:
    patch = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if "urls" in patch:
        patch["urls"] = [u for u in patch["urls"] if u and u.strip()]
    updated = update_in_collection("vibes", vibe_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Vibe not found")
    return updated


@router.delete("/vibes/{vibe_id}")
async def delete_vibe(vibe_id: str) -> dict[str, Any]:
    if not remove_from_collection("vibes", vibe_id):
        raise HTTPException(status_code=404, detail="Vibe not found")
    return {"success": True}


# ── History ───────────────────────────────────────────────────────────────────

@router.get("/history")
async def list_history(q: str = "", tag: str = "", pinned: bool | None = None) -> dict[str, Any]:
    music = load_music_config()
    items = list(music.get("history", []))

    if q:
        ql = q.lower()
        items = [h for h in items if ql in (h.get("url", "") + " " + h.get("title", "")).lower()]
    if tag:
        items = [h for h in items if tag in (h.get("tags") or [])]
    if pinned is not None:
        items = [h for h in items if bool(h.get("pinned")) == pinned]

    # Pinned first, then recency
    items.sort(key=lambda h: (not h.get("pinned"), -int(h.get("last_played") or h.get("added_at") or 0)))
    return {"items": items, "cap": music.get("history_cap", 100)}


@router.post("/history")
async def add_history(req: HistoryAdd) -> dict[str, Any]:
    music = load_music_config()
    hist: list[dict[str, Any]] = list(music.get("history", []))
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")

    now = int(time.time())
    # Dedupe by URL: bump existing entry
    existing = next((h for h in hist if h.get("url") == url), None)
    if existing:
        existing["last_played"] = now
        existing["play_count"]  = int(existing.get("play_count", 0)) + 1
        if req.title:
            existing["title"] = req.title
        if req.tags is not None:
            existing["tags"] = req.tags
    else:
        hist.insert(0, {
            "id":          __import__("secrets").token_hex(6),
            "url":         url,
            "title":       req.title or "",
            "added_at":    now,
            "last_played": now,
            "play_count":  1,
            "pinned":      False,
            "tags":        req.tags or [],
        })

    # Enforce cap, but never evict pinned items
    cap = int(music.get("history_cap", 100))
    pinned = [h for h in hist if h.get("pinned")]
    unpinned = [h for h in hist if not h.get("pinned")]
    unpinned.sort(key=lambda h: -int(h.get("last_played") or h.get("added_at") or 0))
    trimmed = pinned + unpinned[: max(0, cap - len(pinned))]

    music["history"] = trimmed
    save_music_config(music)
    return {"items": trimmed}


@router.patch("/history/{item_id}")
async def patch_history(item_id: str, req: HistoryPatch) -> dict[str, Any]:
    patch = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    updated = update_in_collection("history", item_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="History item not found")
    return updated


@router.delete("/history/{item_id}")
async def delete_history(item_id: str) -> dict[str, Any]:
    if not remove_from_collection("history", item_id):
        raise HTTPException(status_code=404, detail="History item not found")
    return {"success": True}


@router.delete("/history")
async def clear_history(keep_pinned: bool = True) -> dict[str, Any]:
    music = load_music_config()
    if keep_pinned:
        music["history"] = [h for h in music.get("history", []) if h.get("pinned")]
    else:
        music["history"] = []
    save_music_config(music)
    return {"success": True, "remaining": len(music["history"])}


@router.get("/history/export")
async def export_history() -> dict[str, Any]:
    music = load_music_config()
    return {
        "exported_at": int(time.time()),
        "history":     music.get("history", []),
        "vibes":       music.get("vibes", []),
        "custom_embeds": music.get("custom_embeds", []),
    }


# ── Triggers (n8n webhook + workflow map) ─────────────────────────────────────

@router.get("/triggers")
async def get_triggers() -> dict[str, Any]:
    return load_music_config().get("triggers", {})


@router.put("/triggers")
async def update_triggers(req: TriggerUpdate) -> dict[str, Any]:
    music = load_music_config()
    triggers = music.get("triggers", {})
    patch = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    triggers = {**triggers, **patch}

    # If enabling for the first time, mint a token
    if triggers.get("enabled") and not triggers.get("token"):
        import secrets as _secrets
        triggers["token"] = _secrets.token_urlsafe(24)

    music["triggers"] = triggers
    save_music_config(music)
    return triggers


@router.post("/triggers/token/rotate")
async def rotate_token() -> dict[str, Any]:
    return {"token": rotate_trigger_token()}


@router.post("/triggers/fire")
async def fire_trigger(
    req: TriggerFire,
    authorization: str | None = Header(default=None),
    x_vibe_token:  str | None = Header(default=None),
) -> dict[str, Any]:
    """Webhook endpoint for n8n (or any caller) to drive the player.

    Auth: pass the token either as ``Authorization: Bearer <token>`` or
    ``X-Vibe-Token: <token>``. Triggers must be enabled in settings.
    """
    music = load_music_config()
    triggers = music.get("triggers", {})
    if not triggers.get("enabled"):
        raise HTTPException(status_code=403, detail="Triggers disabled")

    expected = triggers.get("token", "")
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization.split(" ", 1)[1].strip()
    elif x_vibe_token:
        supplied = x_vibe_token.strip()

    if not expected or supplied != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    action = (req.action or "").lower()
    if action not in ("play", "pause", "next", "prev", "stop"):
        raise HTTPException(status_code=400, detail="action must be play|pause|next|prev|stop")

    # Persist last event so the frontend poller / WS can pick it up.
    event = {
        "action":      action,
        "url":         req.url,
        "workflow_id": req.workflow_id,
        "instance_id": req.instance_id,
        "fired_at":    int(time.time()),
    }
    update_music_section("last_trigger_event", event)
    return {"success": True, "event": event}


@router.get("/triggers/last-event")
async def get_last_trigger_event() -> dict[str, Any]:
    music = load_music_config()
    return music.get("last_trigger_event") or {}
