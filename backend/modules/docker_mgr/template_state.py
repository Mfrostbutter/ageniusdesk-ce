"""Per-instance template state persistence.

Stores key material that must survive container redeploys but lives outside
the container's data volume so it can be loaded BEFORE the container is
rebuilt. The classic case is n8n's N8N_ENCRYPTION_KEY: if a fresh key is
generated on every redeploy, the existing /home/node/.n8n data volume's
encrypted credentials become unreadable and the container crash-loops.

Storage layout:
    data/template_state/{template_id}/{instance_name}.json
Owned solely by the docker_mgr module. The JSON shape is opaque to this
module: callers stash whatever they like under the keys they care about.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_DIR = Path("data/template_state")

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_segment(value: str) -> str:
    """Clamp a path segment to a filesystem-safe form."""
    return _SAFE_NAME.sub("_", value.strip())[:128] or "_"


def _path_for(template_id: str, instance_name: str) -> Path:
    return STATE_DIR / _safe_segment(template_id) / f"{_safe_segment(instance_name)}.json"


def load(template_id: str, instance_name: str) -> dict[str, Any]:
    """Return persisted state for (template, instance). Empty dict if none."""
    p = _path_for(template_id, instance_name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("template_state: failed to read %s: %s", p, exc)
        return {}


def save(template_id: str, instance_name: str, state: dict[str, Any]) -> None:
    """Atomically persist state for (template, instance).

    Merge-on-write: existing keys not present in `state` are preserved so
    callers can update one field at a time.
    """
    p = _path_for(template_id, instance_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = load(template_id, instance_name)
    merged.update(state)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    tmp.replace(p)
    try:
        p.chmod(0o600)
    except OSError:
        pass


def update_field(template_id: str, instance_name: str, key: str, value: Any) -> None:
    save(template_id, instance_name, {key: value})
