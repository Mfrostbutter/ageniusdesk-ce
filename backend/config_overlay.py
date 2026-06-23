"""Non-secret runtime configuration discovered by post-deploy hooks. Secrets live in data/secrets.json (Fernet-encrypted). Never write a secret here."""

import json
import logging
from typing import Any

from backend.config import CONFIG_FILE

logger = logging.getLogger(__name__)

# Module-level alias so tests can patch it via `patch("backend.config_overlay._CONFIG_FILE", ...)`.
_CONFIG_FILE = CONFIG_FILE

# Overlay key → settings attribute mapping for namespaced sections.
# Each entry maps (section, sub_key) → settings_attr_name.
# Empty by default; post-deploy hooks register non-secret runtime config here.
_OVERLAY_MAP: dict[tuple[str, str], str] = {}


def load_config_overlay() -> dict[str, Any]:
    """Read data/config.json and return the parsed dict.

    Returns {} when the file is missing. Logs a warning and returns {} when
    the file exists but cannot be parsed (malformed JSON or IO error).
    """
    if not _CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(_CONFIG_FILE.read_text())
    except Exception as exc:
        logger.warning("config_overlay: could not parse %s: %s", _CONFIG_FILE, exc)
        return {}


def apply_overlay_to_settings(settings: Any, overlay: dict[str, Any]) -> None:
    """Apply non-secret overlay values onto the settings object.

    Rules:
    - Env wins: a setting is only updated when it is currently at its default
      value (None for Optional fields, empty string for str fields).
    - Only known (section, sub_key) pairs listed in _OVERLAY_MAP are applied;
      unknown keys are silently skipped so future overlay additions don't break
      older code.
    - Nested dicts in the overlay are iterated; scalar top-level values are
      ignored.
    """
    for section, section_val in overlay.items():
        if not isinstance(section_val, dict):
            continue
        for sub_key, value in section_val.items():
            attr = _OVERLAY_MAP.get((section, sub_key))
            if attr is None:
                continue
            current = getattr(settings, attr, None)
            # Only apply if the current value looks like a default (None or "").
            if current is None or current == "":
                setattr(settings, attr, value)
                logger.info("config_overlay: applied %s.%s → settings.%s = %r", section, sub_key, attr, value)
