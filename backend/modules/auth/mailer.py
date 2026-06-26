"""Transactional email for auth flows (password reset). Stdlib SMTP, no new deps.

When SMTP is not configured the message is logged at WARNING (including the
reset link) instead of being sent, so a self-hosted operator without a mail
server can still recover access by reading the container logs.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from backend.config import settings

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(settings.agd_smtp_host)


def _send_blocking(to_email: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.agd_smtp_from or settings.agd_smtp_user or "ageniusdesk@localhost"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.agd_smtp_host, settings.agd_smtp_port, timeout=15) as server:
        if settings.agd_smtp_starttls:
            server.starttls()
        if settings.agd_smtp_user:
            server.login(settings.agd_smtp_user, settings.agd_smtp_password)
        server.send_message(msg)


async def send_password_reset(to_email: str, reset_url: str) -> str:
    """Send (or log) a password-reset link. Returns 'sent' | 'logged' | 'error'."""
    minutes = settings.agd_password_reset_ttl_minutes
    subject = "Reset your AgeniusDesk password"
    body = (
        "We received a request to reset your AgeniusDesk password.\n\n"
        f"Open this link to choose a new password (valid for {minutes} minutes):\n"
        f"{reset_url}\n\n"
        "If you did not request this, you can ignore this email and your "
        "password will stay the same."
    )
    if not smtp_configured():
        logger.warning(
            "SMTP not configured; password-reset link for %s: %s", to_email, reset_url
        )
        return "logged"
    try:
        await asyncio.to_thread(_send_blocking, to_email, subject, body)
        logger.info("Password-reset email sent to %s", to_email)
        return "sent"
    except Exception as exc:  # noqa: BLE001 — never surface SMTP errors to the caller
        logger.error(
            "Password-reset email to %s failed: %s; link: %s", to_email, exc, reset_url
        )
        return "error"
