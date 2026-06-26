"""Theme API routes — list, get, save, and set active theme."""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config import load_config, save_config

router = APIRouter(prefix="/api/themes", tags=["themes"])

# Built-in themes ship with the frontend; custom themes go in data/themes/
BUILTIN_THEMES_DIR = Path(__file__).parent.parent.parent.parent / "frontend" / "themes"
CUSTOM_THEMES_DIR = Path("data/themes")
THEME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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


def _theme_id_from_name(name: str) -> str:
    theme_id = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    if not theme_id:
        raise HTTPException(status_code=400, detail="Theme name must contain letters or numbers")
    return theme_id[:64]


def _safe_theme_path(directory: Path, theme_id: str) -> Path:
    if not THEME_ID_RE.fullmatch(theme_id or ""):
        raise HTTPException(status_code=400, detail="Invalid theme id")
    base = directory.resolve()
    target = (base / f"{theme_id}.json").resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid theme path")
    return target


def _find_theme_path(theme_id: str) -> Path | None:
    for d in (BUILTIN_THEMES_DIR, CUSTOM_THEMES_DIR):
        path = _safe_theme_path(d, theme_id)
        if path.exists():
            return path
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
    path = _find_theme_path(theme_id)
    if path:
        theme = _load_theme_file(path)
        if theme:
            return theme
    raise HTTPException(status_code=404, detail="Theme not found")


@router.post("")
async def save_theme(theme: ThemeData):
    """Save a custom theme."""
    CUSTOM_THEMES_DIR.mkdir(parents=True, exist_ok=True)
    theme_id = _theme_id_from_name(theme.name)
    path = _safe_theme_path(CUSTOM_THEMES_DIR, theme_id)
    path.write_text(json.dumps(theme.model_dump(), indent=2))
    return {"id": theme_id, "name": theme.name}


@router.post("/active/{theme_id}")
async def set_active_theme(theme_id: str):
    """Set the active theme."""
    if _find_theme_path(theme_id) is None:
        raise HTTPException(status_code=404, detail="Theme not found")
    config = load_config()
    config["theme"] = theme_id
    save_config(config)
    return {"theme": theme_id}
