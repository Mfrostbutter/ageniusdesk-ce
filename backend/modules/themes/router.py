"""Theme API routes — list, get, save, and set active theme."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config import load_config, save_config

router = APIRouter(prefix="/api/themes", tags=["themes"])

# Built-in themes ship with the frontend; custom themes go in data/themes/
BUILTIN_THEMES_DIR = Path(__file__).parent.parent.parent.parent / "frontend" / "themes"
CUSTOM_THEMES_DIR = Path("data/themes")


class ThemeData(BaseModel):
    name: str
    author: str = ""
    version: str = "1.0"
    colors: dict[str, str]
    fonts: dict[str, str] = {}
    effects: dict[str, bool] = {}


def _load_theme_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


@router.get("")
async def list_themes():
    """List all available themes (built-in + custom)."""
    themes = []

    for d in (BUILTIN_THEMES_DIR, CUSTOM_THEMES_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            theme = _load_theme_file(f)
            if theme:
                themes.append({
                    "id": f.stem,
                    "name": theme.get("name", f.stem),
                    "author": theme.get("author", ""),
                    "builtin": d == BUILTIN_THEMES_DIR,
                    "colors": theme.get("colors", {}),
                })

    return {"themes": themes}


@router.get("/{theme_id}")
async def get_theme(theme_id: str):
    """Get a specific theme by ID."""
    for d in (BUILTIN_THEMES_DIR, CUSTOM_THEMES_DIR):
        path = d / f"{theme_id}.json"
        if path.exists():
            theme = _load_theme_file(path)
            if theme:
                return theme
    raise HTTPException(status_code=404, detail="Theme not found")


@router.post("")
async def save_theme(theme: ThemeData):
    """Save a custom theme."""
    CUSTOM_THEMES_DIR.mkdir(parents=True, exist_ok=True)
    theme_id = theme.name.lower().replace(" ", "-")
    path = CUSTOM_THEMES_DIR / f"{theme_id}.json"
    path.write_text(json.dumps(theme.model_dump(), indent=2))
    return {"id": theme_id, "name": theme.name}


@router.post("/active/{theme_id}")
async def set_active_theme(theme_id: str):
    """Set the active theme."""
    config = load_config()
    config["theme"] = theme_id
    save_config(config)
    return {"theme": theme_id}
