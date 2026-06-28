"""Auth HTTP surface: setup, login, 2FA, password, session management.

Unauthenticated endpoints are intentionally open (status/setup/login). The rest
require a local login session; edge/token identities manage credentials out of
band, so they are not given a password/2FA surface here.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from backend.auth_gate import current_user, edge_identity, login_enforced
from backend.config import settings
from backend.modules.auth import mailer, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Models ───────────────────────────────────────────────────────────────────


# Pragmatic email shape check (not full RFC 5322): one @, a dot in the domain,
# no spaces. Deliverability is proven by the recovery flow, not by setup.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SetupBody(BaseModel):
    email: str
    password: str
    display_name: str = ""


class LoginBody(BaseModel):
    username: str
    password: str


class TotpLoginBody(BaseModel):
    pending_token: str
    code: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class ForgotBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    new_password: str


class CodeBody(BaseModel):
    code: str


class DisableTotpBody(BaseModel):
    password: str
    code: str = ""


# ── Auth dependency (local session required) ─────────────────────────────────


async def require_session(request: Request) -> dict:
    user = await current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user["source"] != "session":
        raise HTTPException(status_code=403, detail="Local account required for this action")
    return user


def _password_policy() -> dict:
    return {
        "min_length": settings.agd_password_min_length,
        "require_upper": settings.agd_password_require_upper,
        "require_lower": settings.agd_password_require_lower,
        "require_number": settings.agd_password_require_number,
        "require_symbol": settings.agd_password_require_symbol,
    }


def _validate_password(pw: str) -> None:
    p = _password_policy()
    unmet = []
    if len(pw) < p["min_length"]:
        unmet.append(f"at least {p['min_length']} characters")
    if p["require_upper"] and not re.search(r"[A-Z]", pw):
        unmet.append("an uppercase letter")
    if p["require_lower"] and not re.search(r"[a-z]", pw):
        unmet.append("a lowercase letter")
    if p["require_number"] and not re.search(r"[0-9]", pw):
        unmet.append("a number")
    if p["require_symbol"] and not re.search(r"[^A-Za-z0-9]", pw):
        unmet.append("a symbol")
    if unmet:
        raise HTTPException(status_code=400, detail="Password needs " + ", ".join(unmet) + ".")


def _validate_email(email: str) -> str:
    email = (email or "").strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    return email


# ── Unauthenticated ──────────────────────────────────────────────────────────


@router.get("/status")
async def auth_status(request: Request, response: Response):
    user = await current_user(request)
    edge = edge_identity(request)
    # CSRF self-heal: a valid session whose readable agd_csrf cookie was cleared
    # (e.g. another AgeniusDesk on a different localhost port clears the
    # shared-domain cookie) would 403 every mutation. Re-mint the double-submit
    # token on this safe GET so the session stays usable without a re-login.
    if user is not None and not request.cookies.get(service.CSRF_COOKIE):
        service.issue_csrf_cookie(response, request)
    return {
        "accounts_exist": service.accounts_exist(),
        "authenticated": user is not None,
        "user": user,
        "login_disabled": not login_enforced(),
        "edge_identity": edge or None,
        "password_min_length": settings.agd_password_min_length,
        "password_policy": _password_policy(),
    }


@router.post("/setup")
async def setup_owner(body: SetupBody, request: Request, response: Response):
    if service.accounts_exist():
        raise HTTPException(status_code=409, detail="An account already exists")
    email = _validate_email(body.email)
    _validate_password(body.password)
    # Email is the login identity: the internal username IS the email, so the
    # rest of the auth stack (sessions, find_user, TOTP label) needs no rework.
    user = service.create_owner(email, body.password, body.display_name.strip(), email)
    raw = await service.create_session(email, request)
    service.set_session_cookies(response, request, raw)
    logger.info("Owner account created: %s", email)
    return {"user": service.public_user(user)}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    username = body.username.strip()
    ip = service._client_ip(request)
    if service.throttle_blocked(username, ip):
        raise HTTPException(status_code=429, detail="Too many attempts; try again later")

    user = service.find_user(username)
    if not user or not service.verify_password(user, body.password):
        service.throttle_record_failure(username, ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    service.throttle_reset(username, ip)

    # Login-time rehash if the stored params are below current defaults.
    if service.needs_rehash(user):
        service.set_password(username, body.password)

    if service.totp_enabled(user):
        return {"totp_required": True, "pending_token": service.make_pending(username)}

    raw = await service.create_session(username, request)
    service.set_session_cookies(response, request, raw)
    return {"user": service.public_user(user)}


@router.post("/login/totp")
async def login_totp(body: TotpLoginBody, request: Request, response: Response):
    username = service.consume_pending(body.pending_token)
    if not username:
        raise HTTPException(status_code=401, detail="Expired or invalid 2FA challenge")
    ok, remaining = service.verify_second_factor(username, body.code)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid code")
    user = service.find_user(username)
    raw = await service.create_session(username, request)
    service.set_session_cookies(response, request, raw)
    return {"user": service.public_user(user), "recovery_codes_remaining": remaining}


def _public_base(request: Request) -> str:
    """Base URL for links in emails: explicit config, then host, then origin."""
    if settings.agd_public_url:
        return settings.agd_public_url.rstrip("/")
    if settings.agd_public_host:
        scheme = "https" if service._is_https(request) else "http"
        return f"{scheme}://{settings.agd_public_host}"
    return str(request.base_url).rstrip("/")


@router.post("/forgot")
async def forgot_password(body: ForgotBody, request: Request):
    """Begin password recovery. Always 200 — never reveal if an email exists."""
    ip = service._client_ip(request)
    # Per-IP rate limit so the endpoint can't be used to flood an inbox or to
    # mine the response for valid accounts. Blocked callers still get the
    # uniform 200 so the limiter itself leaks nothing.
    if service.forgot_blocked(ip):
        return {"ok": True}
    service.forgot_record(ip)

    user = service.find_user_by_email(body.email)
    if user:
        raw = await service.create_reset_token(user["username"])
        reset_url = f"{_public_base(request)}/?reset={raw}"
        # Fire-and-forget: awaiting the SMTP send only when the account exists
        # is a timing oracle. The mailer swallows its own errors.
        asyncio.create_task(
            mailer.send_password_reset(user.get("email") or body.email.strip(), reset_url)
        )
    return {"ok": True}


@router.post("/reset")
async def reset_password(body: ResetBody):
    """Complete password recovery with a single-use token."""
    username = await service.consume_reset_token(body.token)
    if not username:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired")
    _validate_password(body.new_password)
    service.set_password(username, body.new_password)
    # Recovery invalidates every existing session for the account.
    await service.revoke_all_for_user(username)
    logger.info("Password reset completed for %s", username)
    return {"ok": True}


# ── Authenticated (local session) ────────────────────────────────────────────


@router.post("/logout")
async def logout(request: Request, response: Response, _u: dict = Depends(require_session)):
    raw = request.cookies.get(service.SESSION_COOKIE)
    await service.revoke_session(raw)
    service.clear_session_cookies(response)
    return {"success": True}


@router.get("/me")
async def me(request: Request, response: Response, user: dict = Depends(require_session)):
    # Reaching here means the session is valid; re-mint the CSRF cookie if it was
    # cleared from under it (see auth_status). Keeps mutations working without a
    # reload when another localhost-port instance clobbered the shared cookie.
    if not request.cookies.get(service.CSRF_COOKIE):
        service.issue_csrf_cookie(response, request)
    full = service.find_user(user["username"])
    return {"user": service.public_user(full)} if full else {"user": None}


@router.post("/password")
async def change_password(body: PasswordBody, request: Request, user: dict = Depends(require_session)):
    full = service.find_user(user["username"])
    if not full or not service.verify_password(full, body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    _validate_password(body.new_password)
    service.set_password(user["username"], body.new_password)
    # Invalidate every other session for this account.
    raw = request.cookies.get(service.SESSION_COOKIE)
    await service.revoke_all_for_user(user["username"], keep_raw=raw)
    return {"success": True}


@router.post("/totp/enroll")
async def totp_enroll(user: dict = Depends(require_session)):
    secret, uri = service.totp_enroll(user["username"])
    return {"secret": secret, "otpauth_uri": uri}


@router.post("/totp/activate")
async def totp_activate(body: CodeBody, user: dict = Depends(require_session)):
    codes = service.totp_activate(user["username"], body.code)
    if codes is None:
        raise HTTPException(status_code=400, detail="Code did not verify; try again")
    return {"recovery_codes": codes}


@router.post("/totp/disable")
async def totp_disable(body: DisableTotpBody, user: dict = Depends(require_session)):
    full = service.find_user(user["username"])
    if not full or not service.verify_password(full, body.password):
        raise HTTPException(status_code=401, detail="Password is incorrect")
    if service.totp_enabled(full):
        ok, _ = service.verify_second_factor(user["username"], body.code)
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid 2FA code")
    service.totp_disable(user["username"])
    return {"success": True}


@router.get("/sessions")
async def sessions(request: Request, user: dict = Depends(require_session)):
    raw = request.cookies.get(service.SESSION_COOKIE)
    return {"sessions": await service.list_sessions(user["username"], raw)}


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, user: dict = Depends(require_session)):
    ok = await service.revoke_session_by_id(user["username"], session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True}
