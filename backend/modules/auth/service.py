"""Auth service: password hashing, sessions, throttle, TOTP orchestration.

The HTTP surface lives in `router.py`; everything stateful and security-relevant
lives here so it can be unit-tested without FastAPI. No third-party crypto: PBKDF2
from hashlib, Fernet (already used app-wide) for TOTP secret-at-rest, stdlib TOTP.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response

from backend import totp
from backend.config import (
    DATA_DIR,
    USERS_FILE,
    decrypt_value,
    encrypt_value,
    settings,
)
from backend.database import get_db

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000
LEGACY_ITERATIONS = 100_000  # pre-this-spec users default

# The session cookie has two names. `__Host-agd_session` is issued over HTTPS:
# the prefix is a browser-enforced promise that the cookie is Secure, Path=/, and
# host-locked (no Domain), so a sibling subdomain cannot plant one. Browsers
# reject the prefix over plain HTTP, so a localhost/HTTP install keeps the legacy
# name. Readers must accept either — use session_cookie_value(), never index
# cookies by one name.
SESSION_COOKIE = "agd_session"
HOST_SESSION_COOKIE = "__Host-agd_session"
CSRF_COOKIE = "agd_csrf"
CSRF_HEADER = "x-agd-csrf"


def session_cookie_value(cookies) -> str | None:
    """Read the session cookie under either name, preferring the __Host- one."""
    return cookies.get(HOST_SESSION_COOKIE) or cookies.get(SESSION_COOKIE)

ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── User store ───────────────────────────────────────────────────────────────


def load_users() -> list[dict]:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            logger.warning("users.json unreadable; treating as empty")
    return []


def save_users(users: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2))
    try:
        USERS_FILE.chmod(0o600)
    except OSError:
        pass


def find_user(username: str) -> dict | None:
    for u in load_users():
        if u.get("username") == username:
            return u
    return None


def find_user_by_email(email: str) -> dict | None:
    """Case-insensitive email lookup (used by password recovery)."""
    target = (email or "").strip().lower()
    if not target:
        return None
    for u in load_users():
        if (u.get("email") or "").strip().lower() == target:
            return u
    return None


def accounts_exist() -> bool:
    return bool(load_users())


def public_user(user: dict) -> dict:
    """Browser-safe projection: never expose hash/salt/secret."""
    totp_block = user.get("totp") or {}
    return {
        "username": user["username"],
        "email": user.get("email", ""),
        "display_name": user.get("display_name", ""),
        "role": user.get("role", "viewer"),
        "totp": {"enabled": bool(totp_block.get("enabled"))},
    }


# ── Password hashing ─────────────────────────────────────────────────────────


def hash_password(password: str, salt: str | None = None, iterations: int = PBKDF2_ITERATIONS) -> dict:
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return {"password_hash": hashed, "salt": salt, "algo": ALGO, "iterations": iterations}


def verify_password(user: dict, password: str) -> bool:
    salt = user.get("salt", "")
    iterations = int(user.get("iterations") or LEGACY_ITERATIONS)
    expected = user.get("password_hash", "")
    if not salt or not expected:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return hmac.compare_digest(candidate, expected)


def needs_rehash(user: dict) -> bool:
    return int(user.get("iterations") or LEGACY_ITERATIONS) != PBKDF2_ITERATIONS or user.get("algo") != ALGO


def set_password(username: str, new_password: str) -> bool:
    users = load_users()
    for u in users:
        if u.get("username") == username:
            u.update(hash_password(new_password))
            u["password_changed_at"] = _iso(_now())
            save_users(users)
            return True
    return False


def create_owner(username: str, password: str, display_name: str = "", email: str = "") -> dict:
    users = load_users()
    now = _iso(_now())
    user = {
        "username": username,
        "email": (email or "").strip(),
        "display_name": display_name or username,
        "role": "admin",
        "created_at": now,
        "password_changed_at": now,
        **hash_password(password),
        "totp": {"enabled": False, "secret_enc": "", "recovery_codes": []},
    }
    users.append(user)
    save_users(users)
    return user


# ── Sessions (DB-backed; only the token hash is stored) ──────────────────────


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if settings.agd_trust_forwarded_for and fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


async def create_session(username: str, request: Request) -> str:
    raw = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(days=settings.agd_session_ttl_days)
    db = await get_db()
    await db.execute(
        "INSERT INTO auth_sessions (id_hash, username, created_at, expires_at, last_seen, user_agent, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _hash_token(raw),
            username,
            _iso(now),
            _iso(expires),
            _iso(now),
            (request.headers.get("user-agent") or "")[:300],
            _client_ip(request),
        ),
    )
    await db.commit()
    return raw


async def session_user(raw: str | None) -> dict | None:
    """Validate a session token, slide its expiry, return the public user dict."""
    if not raw:
        return None
    db = await get_db()
    cur = await db.execute(
        "SELECT username, created_at, expires_at FROM auth_sessions WHERE id_hash = ?",
        (_hash_token(raw),),
    )
    row = await cur.fetchone()
    if not row:
        return None
    now = _now()
    if now >= _parse(row["expires_at"]):
        await db.execute("DELETE FROM auth_sessions WHERE id_hash = ?", (_hash_token(raw),))
        await db.commit()
        return None
    user = find_user(row["username"])
    if not user:
        return None
    # Slide expiry up to the absolute cap measured from creation.
    cap = _parse(row["created_at"]) + timedelta(days=settings.agd_session_absolute_days)
    new_expires = min(now + timedelta(days=settings.agd_session_ttl_days), cap)
    await db.execute(
        "UPDATE auth_sessions SET last_seen = ?, expires_at = ? WHERE id_hash = ?",
        (_iso(now), _iso(new_expires), _hash_token(raw)),
    )
    await db.commit()
    return public_user(user)


async def revoke_session(raw: str | None) -> None:
    if not raw:
        return
    db = await get_db()
    await db.execute("DELETE FROM auth_sessions WHERE id_hash = ?", (_hash_token(raw),))
    await db.commit()


async def revoke_all_for_user(username: str, keep_raw: str | None = None) -> None:
    db = await get_db()
    if keep_raw:
        await db.execute(
            "DELETE FROM auth_sessions WHERE username = ? AND id_hash != ?",
            (username, _hash_token(keep_raw)),
        )
    else:
        await db.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))
    await db.commit()


async def list_sessions(username: str, current_raw: str | None) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id_hash, created_at, last_seen, user_agent, ip FROM auth_sessions "
        "WHERE username = ? ORDER BY last_seen DESC",
        (username,),
    )
    rows = await cur.fetchall()
    current_hash = _hash_token(current_raw) if current_raw else ""
    return [
        {
            "id": r["id_hash"][:12],
            "created_at": r["created_at"],
            "last_seen": r["last_seen"],
            "user_agent": r["user_agent"] or "",
            "ip": r["ip"] or "",
            "current": r["id_hash"] == current_hash,
        }
        for r in rows
    ]


# ── Password-reset tokens (DB-backed, single-use, short-lived) ───────────────


async def create_reset_token(username: str) -> str:
    """Issue a single active reset token for a user (supersedes any prior one)."""
    raw = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(minutes=settings.agd_password_reset_ttl_minutes)
    db = await get_db()
    await db.execute("DELETE FROM auth_resets WHERE username = ?", (username,))
    await db.execute(
        "INSERT INTO auth_resets (token_hash, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_hash_token(raw), username, _iso(now), _iso(expires)),
    )
    await db.commit()
    return raw


async def consume_reset_token(raw: str | None) -> str | None:
    """Validate and burn a reset token. Returns the username, or None if invalid."""
    if not raw:
        return None
    db = await get_db()
    cur = await db.execute(
        "SELECT username, expires_at FROM auth_resets WHERE token_hash = ?", (_hash_token(raw),)
    )
    row = await cur.fetchone()
    if not row:
        return None
    # Single-use: delete on first touch regardless of expiry outcome.
    await db.execute("DELETE FROM auth_resets WHERE token_hash = ?", (_hash_token(raw),))
    await db.commit()
    if _now() >= _parse(row["expires_at"]):
        return None
    return row["username"]


async def revoke_session_by_id(username: str, id_prefix: str) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT id_hash FROM auth_sessions WHERE username = ?", (username,)
    )
    rows = await cur.fetchall()
    for r in rows:
        if r["id_hash"].startswith(id_prefix):
            await db.execute("DELETE FROM auth_sessions WHERE id_hash = ?", (r["id_hash"],))
            await db.commit()
            return True
    return False


# ── Cookies ──────────────────────────────────────────────────────────────────


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def issue_csrf_cookie(response: Response, request: Request) -> str:
    """Set (or refresh) just the readable CSRF cookie, leaving the session alone.

    The CSRF token is a pure double-submit value (not bound to the session), so
    re-minting it is always safe. Used to self-heal a still-valid session whose
    agd_csrf cookie was cleared out from under it, e.g. another AgeniusDesk on a
    different localhost port clearing the shared-domain cookie (cookies are not
    isolated by port). Without this, the session stays logged in but every
    mutation 403s because the double-submit token is gone."""
    secure = _is_https(request)
    max_age = settings.agd_session_ttl_days * 86400
    csrf = secrets.token_urlsafe(24)
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, httponly=False,
        samesite="strict", secure=secure, path="/",
    )
    return csrf


def set_session_cookies(response: Response, request: Request, raw: str) -> str:
    """Set the HttpOnly session cookie + a readable CSRF cookie. Returns csrf.

    Over HTTPS the session cookie is issued under the `__Host-` prefix, which
    browsers only accept when it is Secure, Path=/, and carries no Domain. That
    makes it un-settable by a sibling subdomain, closing the session-fixation
    path where evil.example.com plants a cookie for app.example.com. Over plain
    HTTP the prefix is not permitted at all, so the legacy name is used and the
    reader accepts both — an existing session survives the upgrade, and a
    localhost install keeps working.
    """
    secure = _is_https(request)
    max_age = settings.agd_session_ttl_days * 86400
    response.set_cookie(
        HOST_SESSION_COOKIE if secure else SESSION_COOKIE,
        raw, max_age=max_age, httponly=True,
        samesite="strict", secure=secure, path="/",
    )
    if secure:
        # Retire any legacy-named cookie so the two cannot drift apart.
        response.delete_cookie(SESSION_COOKIE, path="/")
    return issue_csrf_cookie(response, request)


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(HOST_SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


# ── Login throttle (in-memory; lost on restart, acceptable single-node) ──────

_failures: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}


def _keys(username: str, ip: str, stage: str = "password") -> list[str]:
    """Throttle keys for one auth stage.

    Password and TOTP get separate counters. Each stage still has its own hard
    attempt ceiling — which is the property S3 required, and it is preserved:
    a wrong-code loop trips the TOTP lockout rather than running unbounded. What
    separation buys is that the two stages stop bleeding into each other. An
    attacker who holds the password can no longer burn down the victim's
    password-login budget by failing at the TOTP prompt, and a user fumbling
    their authenticator does not get locked out of the password step too.
    """
    return [f"{stage}:u:{username}", f"{stage}:ip:{ip}"]


def throttle_blocked(username: str, ip: str, stage: str = "password") -> bool:
    now = time.time()
    for k in _keys(username, ip, stage):
        until = _lockouts.get(k, 0)
        if until and now < until:
            return True
    return False


def throttle_record_failure(username: str, ip: str, stage: str = "password") -> None:
    now = time.time()
    for k in _keys(username, ip, stage):
        window = [t for t in _failures.get(k, []) if now - t < settings.agd_login_lockout_minutes * 60]
        window.append(now)
        _failures[k] = window
        if len(window) >= settings.agd_login_max_attempts:
            _lockouts[k] = now + settings.agd_login_lockout_minutes * 60


def throttle_reset(username: str, ip: str, stage: str = "password") -> None:
    for k in _keys(username, ip, stage):
        _failures.pop(k, None)
        _lockouts.pop(k, None)


# Password-reset (/forgot) throttle. Keyed ONLY on a namespaced IP so flooding
# the endpoint can neither lock a victim's login (no u:<email> key) nor lock
# every IP at once (no shared global key). Reuses the login window/lockout knobs.
def forgot_blocked(ip: str) -> bool:
    until = _lockouts.get(f"forgot-ip:{ip}", 0)
    return bool(until and time.time() < until)


def forgot_record(ip: str) -> None:
    now = time.time()
    k = f"forgot-ip:{ip}"
    window = [t for t in _failures.get(k, []) if now - t < settings.agd_login_lockout_minutes * 60]
    window.append(now)
    _failures[k] = window
    if len(window) >= settings.agd_login_max_attempts:
        _lockouts[k] = now + settings.agd_login_lockout_minutes * 60


# Reset-token consumption (/reset) throttle. Same shape as /forgot and keyed
# only on a namespaced IP for the same reasons. The tokens are 32+ bytes of
# urandom, so this is not what stops a guessing attack — it stops an unbounded
# grind against the endpoint (and the argon2 hash behind a valid one) from being
# free. Only FAILED consumes are recorded, so a legitimate reset never counts.
def reset_blocked(ip: str) -> bool:
    until = _lockouts.get(f"reset-ip:{ip}", 0)
    return bool(until and time.time() < until)


def reset_record_failure(ip: str) -> None:
    now = time.time()
    k = f"reset-ip:{ip}"
    window = [t for t in _failures.get(k, []) if now - t < settings.agd_login_lockout_minutes * 60]
    window.append(now)
    _failures[k] = window
    if len(window) >= settings.agd_login_max_attempts:
        _lockouts[k] = now + settings.agd_login_lockout_minutes * 60


# ── Pending-2FA tokens (in-memory, single-use, short-lived) ──────────────────

_pending: dict[str, tuple[str, float]] = {}
_PENDING_TTL = 300  # 5 minutes


def make_pending(username: str) -> str:
    token = secrets.token_urlsafe(24)
    _pending[token] = (username, time.time() + _PENDING_TTL)
    return token


def consume_pending(token: str) -> str | None:
    entry = _pending.pop(token, None)
    if not entry:
        return None
    username, expires = entry
    if time.time() >= expires:
        return None
    return username


# ── TOTP orchestration (secret stored Fernet-encrypted on the user) ──────────


def totp_enroll(username: str) -> tuple[str, str]:
    """Generate a pending (not-yet-enabled) secret. Returns (secret, otpauth_uri)."""
    secret = totp.generate_secret()
    users = load_users()
    for u in users:
        if u.get("username") == username:
            block = u.get("totp") or {}
            block["secret_enc"] = encrypt_value(secret)
            block["enabled"] = False
            u["totp"] = block
            save_users(users)
            break
    return secret, totp.provisioning_uri(secret, account=username)


def _user_secret(user: dict) -> str:
    enc = (user.get("totp") or {}).get("secret_enc", "")
    return decrypt_value(enc) if enc else ""


def totp_activate(username: str, code: str) -> list[str] | None:
    """Verify the pending secret; on success enable + return recovery codes."""
    users = load_users()
    for u in users:
        if u.get("username") == username:
            secret = _user_secret(u)
            step = totp.verify_step(secret, code) if secret else None
            if step is None:
                return None
            codes = totp.generate_recovery_codes()
            block = u.get("totp") or {}
            block["enabled"] = True
            block["recovery_codes"] = [totp.hash_recovery_code(c) for c in codes]
            # Seed the replay guard so the activation code can't be reused to log in.
            block["last_totp_step"] = step
            u["totp"] = block
            save_users(users)
            return codes
    return None


def totp_disable(username: str) -> None:
    users = load_users()
    for u in users:
        if u.get("username") == username:
            u["totp"] = {"enabled": False, "secret_enc": "", "recovery_codes": []}
            save_users(users)
            break


def totp_enabled(user: dict) -> bool:
    return bool((user.get("totp") or {}).get("enabled"))


def verify_second_factor(username: str, code: str) -> tuple[bool, int]:
    """Check a TOTP code or consume a recovery code.

    Returns (ok, recovery_codes_remaining). A matched recovery code is deleted
    from the stored array (consumption = deletion, no separate consumed list).
    """
    users = load_users()
    for u in users:
        if u.get("username") != username:
            continue
        block = u.get("totp") or {}
        secret = _user_secret(u)
        if secret:
            step = totp.verify_step(secret, code)
            if step is not None:
                # Reject replay of a code already used this step (or an earlier
                # one still inside the skew window) — #17 intra-window lockout.
                last = block.get("last_totp_step")
                if last is not None and step <= last:
                    return False, len(block.get("recovery_codes", []))
                block["last_totp_step"] = step
                u["totp"] = block
                save_users(users)
                return True, len(block.get("recovery_codes", []))
        # Recovery-code path: single-use by deletion.
        target = totp.hash_recovery_code(code)
        codes = block.get("recovery_codes", [])
        for i, stored in enumerate(codes):
            if hmac.compare_digest(stored, target):
                del codes[i]
                block["recovery_codes"] = codes
                u["totp"] = block
                save_users(users)
                return True, len(codes)
        return False, len(codes)
    return False, 0


def recovery_codes_remaining(user: dict) -> int:
    return len((user.get("totp") or {}).get("recovery_codes", []))
