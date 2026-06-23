"""API key storage — hashes only, never raw values.

Stored at data/api_keys.json as a list of records:
  [{id, name, scope, created_at, key_hash}]

The raw key is never persisted; key_hash = sha256(raw_key).
A leaked api_keys.json cannot be replayed — only the original bearer token matches.
"""

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path("data")
_API_KEYS_FILE = _DATA_DIR / "api_keys.json"

VALID_SCOPES = {"read", "trigger"}


def load_api_keys() -> list[dict]:
    if _API_KEYS_FILE.exists():
        try:
            return json.loads(_API_KEYS_FILE.read_text())
        except Exception:
            return []
    return []


def save_api_keys(keys: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _API_KEYS_FILE.write_text(json.dumps(keys, indent=2))


def create_api_key(name: str, scope: str) -> tuple[str, dict]:
    """Generate a new API key. Returns (raw_key, record).

    raw_key is shown to the operator once and never stored.
    record.key_hash = sha256hex(raw_key) used for future verification.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {VALID_SCOPES}")
    raw = "agd_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    record: dict = {
        "id": secrets.token_hex(8),
        "name": name,
        "scope": scope,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "key_hash": key_hash,
    }
    keys = load_api_keys()
    keys.append(record)
    save_api_keys(keys)
    return raw, record


def delete_api_key(key_id: str) -> bool:
    """Remove a key by ID. Returns True if found and deleted."""
    keys = load_api_keys()
    before = len(keys)
    keys = [k for k in keys if k["id"] != key_id]
    if len(keys) == before:
        return False
    save_api_keys(keys)
    return True


def lookup_by_hash(key_hash: str) -> dict | None:
    """Return the key record whose key_hash matches, or None."""
    for entry in load_api_keys():
        if entry.get("key_hash") == key_hash:
            return entry
    return None
