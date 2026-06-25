"""Auth module — local accounts, login sessions, and optional TOTP 2FA."""

from backend.modules.auth.router import router

__all__ = ["router"]
