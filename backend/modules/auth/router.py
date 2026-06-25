"""Auth HTTP surface: setup, login, 2FA, password, session management.

Unauthenticated endpoints are intentionally open (status/setup/login). The rest
require a local login session; edge/token identities manage credentials out of
band, so they are not given a password/2FA surface here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from backend.auth_gate import current_user, edge_identity, login_enforced
from backend.config import settings
from backend.modules.auth import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Models ───────────────────────────────────────────────────────────────────


class SetupBody(BaseModel):
    username: str
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


def _validate_password(pw: str) -> None:
    if len(pw) < settings.agd_password_min_length:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {settings.agd_password_min_length} characters",
        )


# ── Unauthenticated ──────────────────────────────────────────────────────────


@router.get("/status")
async def auth_status(request: Request):
    user = await current_user(request)
    edge = edge_identity(request)
    return {
        "accounts_exist": service.accounts_exist(),
        "authenticated": user is not None,
        "user": user,
        "login_disabled": not login_enforced(),
        "edge_identity": edge or None,
    }


@router.post("/setup")
async def setup_owner(body: SetupBody, request: Request, response: Response):
    if service.accounts_exist():
        raise HTTPException(status_code=409, detail="An account already exists")
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    _validate_password(body.password)
    user = service.create_owner(username, body.password, body.display_name.strip())
    raw = await service.create_session(username, request)
    service.set_session_cookies(response, request, raw)
    logger.info("Owner account created: %s", username)
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


# ── Authenticated (local session) ────────────────────────────────────────────


@router.post("/logout")
async def logout(request: Request, response: Response, _u: dict = Depends(require_session)):
    raw = request.cookies.get(service.SESSION_COOKIE)
    await service.revoke_session(raw)
    service.clear_session_cookies(response)
    return {"success": True}


@router.get("/me")
async def me(user: dict = Depends(require_session)):
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
