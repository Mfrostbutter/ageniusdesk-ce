"""API key storage — hashes only, never raw values.

Stored at data/api_keys.json as a list of records:
  [{id, name, scope, created_at, key_hash,
    expires_at?, allowed_ips?, allowed_instances?, allowed_workflows?}]

The raw key is never persisted; key_hash = sha256(raw_key).
A leaked api_keys.json cannot be replayed — only the original bearer token matches.

The four optional restriction fields all follow the same rule: absent or empty
means unrestricted. That keeps every key issued before they existed working
exactly as it did, while letting an operator narrow a new key to "trigger only
this workflow, only from the CI runner's IP, and only until the end of the
quarter".
"""

import hashlib
import hmac
import ipaddress
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


def _normalize_cidrs(values: list[str] | None) -> list[str]:
    """Validate and normalize an IP/CIDR allowlist. Raises ValueError on junk.

    A bare address is accepted and stored as a host network (/32, /128), so an
    operator can type "10.0.0.5" and mean that one machine.
    """
    out: list[str] = []
    for raw in values or []:
        item = (raw or "").strip()
        if not item:
            continue
        try:
            out.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError as e:
            raise ValueError(f"Invalid IP or CIDR: {item}") from e
    return out


def _parse_expiry(value: str | None) -> str:
    """Validate an ISO-8601 expiry and normalize it to UTC. '' = never expires."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Invalid expires_at (want ISO-8601): {raw}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def create_api_key(
    name: str,
    scope: str,
    *,
    expires_at: str = "",
    allowed_ips: list[str] | None = None,
    allowed_instances: list[str] | None = None,
    allowed_workflows: list[str] | None = None,
) -> tuple[str, dict]:
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
        "expires_at": _parse_expiry(expires_at),
        "allowed_ips": _normalize_cidrs(allowed_ips),
        "allowed_instances": [s.strip() for s in (allowed_instances or []) if s.strip()],
        "allowed_workflows": [s.strip() for s in (allowed_workflows or []) if s.strip()],
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


def is_expired(record: dict, now: datetime | None = None) -> bool:
    """Whether a key's expires_at has passed. Missing/blank = never expires.

    An unparseable expires_at is treated as expired: a key whose expiry we cannot
    read must fail closed, not silently become immortal.
    """
    raw = (record.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= dt


def ip_allowed(record: dict, client: str) -> bool:
    """Whether a client address satisfies the key's allowed_ips. Empty = any."""
    nets = record.get("allowed_ips") or []
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address((client or "").strip())
    except ValueError:
        return False  # no usable client address: fail closed against a scoped key
    for net in nets:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


def resource_allowed(record: dict, field: str, value: str) -> bool:
    """Whether `value` satisfies the key's allowlist for `field`. Empty = any."""
    allowed = record.get(field) or []
    if not allowed:
        return True
    return value in allowed


def lookup_by_hash(key_hash: str) -> dict | None:
    """Return the key record whose key_hash matches, or None.

    Expired records are treated as non-existent, so an expired key gets the same
    401 as an invalid one and leaks nothing about which case it hit.
    """
    for entry in load_api_keys():
        stored = entry.get("key_hash") or ""
        # Constant-time compare for consistency with the other token paths, even
        # though both sides are already sha256 hashes (no usable preimage leaks).
        if stored and hmac.compare_digest(stored, key_hash):
            if is_expired(entry):
                return None
            return entry
    return None
