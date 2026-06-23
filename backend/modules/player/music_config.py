"""Music page config schema, defaults, and persistence helpers.

Stored under the ``music`` key in ``data/config.json``. One source of truth
for everything on the Your Vibe page: appearance, behavior, custom embeds,
vibe playlists, server-synced history, and n8n triggers.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from backend.config import load_config, save_config

MUSIC_KEY = "music"

# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_APPEARANCE: dict[str, Any] = {
    "eq_enabled": True,
    "eq_bars": 16,
    "eq_style": "bars",           # bars | wave | circular | off
    "banner_height": "normal",    # compact | normal | tall
    "banner_position": "top",     # top | bottom | floating
    "show_album_art": True,
    "show_progress": True,
    "show_controls": True,
    "accent_override": None,      # hex override or null
}

DEFAULT_BEHAVIOR: dict[str, Any] = {
    "default_service": None,      # spotify | youtube | soundcloud | apple | tidal | youtubemusic | custom
    "autoplay_on_paste": True,
    "auto_advance": False,
    "persist_across_reload": True,
    "auto_pause_on_error": False,
    "hotkey_toggle": None,        # e.g. "alt+m"
}

DEFAULT_TRIGGERS: dict[str, Any] = {
    "enabled": False,
    "token": "",                  # generated on first enable
    "on_workflow_error":   {"action": "none", "url": ""},   # play | pause | none
    "on_workflow_success": {"action": "none", "url": ""},
    "workflow_map": [],           # [{workflow_id, instance_id, on_success, on_error}]
}

DEFAULT_MUSIC: dict[str, Any] = {
    "appearance": DEFAULT_APPEARANCE,
    "behavior":   DEFAULT_BEHAVIOR,
    "custom_embeds": [],          # see embed schema in music_router.py
    "vibes":         [],          # [{id, name, description, urls, icon, color, created_at}]
    "history":       [],          # [{url, added_at, pinned, tags, play_count, last_played, title?}]
    "history_cap":   100,
    "triggers":      DEFAULT_TRIGGERS,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _merge_defaults(stored: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge defaults into stored so new fields auto-populate."""
    out = dict(defaults)
    for k, v in stored.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            out[k] = _merge_defaults(v, defaults[k])
        else:
            out[k] = v
    return out


def load_music_config() -> dict[str, Any]:
    """Load music config, filling in defaults for any missing keys."""
    cfg = load_config()
    stored = cfg.get(MUSIC_KEY, {}) or {}
    return _merge_defaults(stored, DEFAULT_MUSIC)


def save_music_config(music: dict[str, Any]) -> dict[str, Any]:
    """Persist music config, returning the merged result."""
    cfg = load_config()
    cfg[MUSIC_KEY] = music
    save_config(cfg)
    return music


def update_music_section(section: str, value: Any) -> dict[str, Any]:
    """Update a single top-level key in music config."""
    music = load_music_config()
    music[section] = value
    return save_music_config(music)


def ensure_trigger_token() -> str:
    """Generate a webhook auth token if triggers are enabled and no token set."""
    music = load_music_config()
    triggers = music.get("triggers", {})
    if not triggers.get("token"):
        triggers["token"] = secrets.token_urlsafe(24)
        music["triggers"] = triggers
        save_music_config(music)
    return triggers["token"]


def rotate_trigger_token() -> str:
    """Generate a fresh webhook token, invalidating the old one."""
    music = load_music_config()
    triggers = music.get("triggers", {})
    triggers["token"] = secrets.token_urlsafe(24)
    music["triggers"] = triggers
    save_music_config(music)
    return triggers["token"]


# ── Collection helpers (custom embeds, vibes, history) ────────────────────────

def _gen_id() -> str:
    return secrets.token_hex(8)


def add_to_collection(section: str, item: dict[str, Any]) -> dict[str, Any]:
    """Append an item to a list section, assigning id + created_at if missing."""
    music = load_music_config()
    coll = list(music.get(section, []))
    if "id" not in item:
        item["id"] = _gen_id()
    if "created_at" not in item:
        item["created_at"] = int(time.time())
    coll.append(item)
    music[section] = coll
    save_music_config(music)
    return item


def update_in_collection(section: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Shallow-patch an item in a list section by id."""
    music = load_music_config()
    coll = list(music.get(section, []))
    for i, it in enumerate(coll):
        if it.get("id") == item_id:
            coll[i] = {**it, **patch, "id": item_id}
            music[section] = coll
            save_music_config(music)
            return coll[i]
    return None


def remove_from_collection(section: str, item_id: str) -> bool:
    """Remove an item from a list section by id."""
    music = load_music_config()
    coll = list(music.get(section, []))
    new = [it for it in coll if it.get("id") != item_id]
    if len(new) == len(coll):
        return False
    music[section] = new
    save_music_config(music)
    return True
