"""Central audit sink.

One place to emit security-relevant events (assistant tool execution, public API
key usage) so an operator can point a log shipper at a single logger name:
``agd.audit``. Lines are ``EVENT key=value ...`` with a JSON tail for structured
fields, and every value passes through :func:`scrub` first so a credential that
wandered into a tool argument is never written to the log.

The actor is carried in a context variable rather than threaded through every
call: the request handler sets it once (see ``with_actor``), and code far down
the stack — deep inside a provider's tool loop — can record who is responsible
without every intermediate function growing a ``user`` parameter.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("agd.audit")

_actor: ContextVar[str] = ContextVar("agd_audit_actor", default="")

# Argument/field names whose value is never safe to log, matched as a substring
# of the lowercased key.
_SECRET_KEY_MARKERS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "private_key", "session",
)

_MAX_VALUE_CHARS = 200


def _looks_secret(key: str) -> bool:
    k = key.lower()
    return any(m in k for m in _SECRET_KEY_MARKERS)


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-looking fields and truncate long values.

    Depth-bounded: a deeply nested tool argument (an imported workflow's node
    tree) is summarized rather than serialized in full, because the audit line
    exists to say *what was done*, not to mirror the payload.
    """
    if _depth > 4:
        return "<nested>"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = "<redacted>" if _looks_secret(str(k)) else scrub(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > 20:
            return [scrub(v, _depth + 1) for v in value[:20]] + [f"<+{len(value) - 20} more>"]
        return [scrub(v, _depth + 1) for v in value]
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + f"<+{len(value) - _MAX_VALUE_CHARS} chars>"
    return value


def actor() -> str:
    """The identity responsible for the current unit of work, or "anonymous"."""
    return _actor.get() or "anonymous"


def set_actor(user: dict | None) -> None:
    """Set the audit actor from an auth_gate ``current_user`` dict.

    A None user means an open install (AGD_DISABLE_LOGIN) where no identity
    exists; it is recorded as "anonymous" rather than dropped, so the audit trail
    still shows the action happened.
    """
    if not user:
        _actor.set("anonymous")
        return
    name = user.get("username") or user.get("email") or "unknown"
    _actor.set(f"{name}({user.get('source', '?')})")


@contextmanager
def with_actor(user: dict | None):
    """Scope the audit actor to a block, restoring the previous value after."""
    token = _actor.set("")
    try:
        set_actor(user)
        yield
    finally:
        _actor.reset(token)


def record(event: str, outcome: str = "ok", **fields: Any) -> None:
    """Emit one audit line. Never raises — auditing must not break the action."""
    try:
        payload = json.dumps(scrub(fields), default=str)[:2000]
        logger.warning("%s actor=%s outcome=%s %s", event, actor(), outcome, payload)
    except Exception:  # noqa: BLE001 - an unloggable event must not fail the request
        logger.warning("%s actor=%s outcome=%s <unserializable fields>", event, actor(), outcome)
