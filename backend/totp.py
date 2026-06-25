"""RFC 6238 TOTP, stdlib only.

No third-party dependency: HMAC-SHA1, 30-second step, 6 digits, matching what
Google Authenticator / Authy / 1Password produce. The QR code is rendered in the
browser from the `otpauth://` URI, so there is no server-side image library.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

_DIGITS = 6
_STEP = 30
_ALGO = "SHA1"  # authenticator-app standard


def generate_secret(length: int = 20) -> str:
    """Return a base32 TOTP secret (no padding), `length` random bytes wide."""
    raw = secrets.token_bytes(length)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, account: str, issuer: str = "AgeniusDesk") -> str:
    """Build the `otpauth://totp/...` URI an authenticator app scans."""
    label = quote(f"{issuer}:{account}")
    params = (
        f"secret={secret}"
        f"&issuer={quote(issuer)}"
        f"&algorithm={_ALGO}"
        f"&digits={_DIGITS}"
        f"&period={_STEP}"
    )
    return f"otpauth://totp/{label}?{params}"


def _b32decode(secret: str) -> bytes:
    pad = "=" * (-len(secret) % 8)
    return base64.b32decode(secret.upper() + pad)


def _hotp(key: bytes, counter: int) -> str:
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**_DIGITS)
    return str(code).zfill(_DIGITS)


def verify(secret: str, code: str, window: int = 1, at: float | None = None) -> bool:
    """Verify a 6-digit TOTP. Accepts +/- `window` steps for clock skew.

    Constant-time compare on each candidate so a timing side-channel cannot
    distinguish a near-miss from a far-miss.
    """
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if len(code) != _DIGITS or not code.isdigit():
        return False
    try:
        key = _b32decode(secret)
    except Exception:
        return False
    now = int((at if at is not None else time.time()) // _STEP)
    ok = False
    for drift in range(-window, window + 1):
        candidate = _hotp(key, now + drift)
        # Compare every candidate (no early break) to keep timing uniform.
        if hmac.compare_digest(candidate, code):
            ok = True
    return ok


def generate_recovery_codes(n: int = 10) -> list[str]:
    """Return `n` human-friendly one-time recovery codes (shown once)."""
    codes = []
    for _ in range(n):
        raw = secrets.token_hex(5)  # 10 hex chars
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    """Hash a recovery code for storage. Normalizes case and separators."""
    normalized = code.strip().lower().replace("-", "").replace(" ", "")
    return hashlib.sha256(normalized.encode()).hexdigest()
