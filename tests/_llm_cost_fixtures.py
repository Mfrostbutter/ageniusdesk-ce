"""Fixtures shared by the LLM Cost tests (imported into each module's namespace).

CE runs one SQLite database for the whole session, so every test wipes the
llm_cost_* tables first. The async client authenticates as an admin by patching
`backend.auth_gate.current_user`, the same seam the role tests use, and marks the
forwarder ingest path self-authenticating the way `backend/main.py` is wired for
device-token ingest.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

_TABLES = (
    "llm_cost_sources", "llm_cost_snapshots", "llm_cost_push", "llm_cost_devices", "llm_cost_quota_samples",
    "llm_cost_spend_samples", "llm_cost_events", "llm_cost_alert_state", "llm_cost_settings",
)

INGEST_SELF_AUTH = frozenset({"/api/llm-cost/ingest"})

__all__ = ["as_admin", "client", "test_db", "tmp_data_dir"]


class _Db:
    """The enterprise test handle's shape (fetch/execute) over CE's aiosqlite connection."""

    def __init__(self, conn):
        self.conn = conn

    async def fetch(self, sql, params=()):
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchall()

    async def execute(self, sql, params=()):
        cur = await self.conn.execute(sql, tuple(params))
        await cur.close()
        await self.conn.commit()


async def _wipe(conn) -> None:
    from backend.modules.llm_cost import store

    await store.ensure_schema(conn)
    for table in _TABLES:
        await conn.execute(f"DELETE FROM {table}")
    await conn.execute("DELETE FROM messages")
    await conn.commit()


@pytest.fixture
async def test_db():
    """A connection bound to this test's event loop.

    aiosqlite ties a connection to the loop that opened it; pytest-asyncio gives
    every test a fresh loop, so the process-wide singleton is reopened per test.
    """
    from backend import database

    stale = database._db
    database._db = None
    if stale is not None:
        try:
            await stale.close()
        except Exception:  # noqa: BLE001 - it belonged to a dead loop
            pass
    conn = await database.get_db()
    await _wipe(conn)
    yield _Db(conn)
    database._db = None
    try:
        await conn.close()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def tmp_data_dir():
    """The conftest already chdir'd into a throwaway tree; start from an empty config."""
    from backend.config import CONFIG_FILE, DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("{}")
    yield DATA_DIR
    CONFIG_FILE.write_text("{}")


@pytest.fixture
def as_admin(monkeypatch):
    from backend import auth_gate

    async def _fake(_request):
        return {"username": "admin-user", "source": "session", "role": "admin", "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)
    return _fake


@pytest.fixture
async def client(test_db, as_admin, monkeypatch):
    from backend import main as app_main

    exempt = app_main._SELF_AUTHENTICATING_EXACT | INGEST_SELF_AUTH
    monkeypatch.setattr(app_main, "_SELF_AUTHENTICATING_EXACT", exempt)
    async with AsyncClient(transport=ASGITransport(app=app_main.app), base_url="http://test") as ac:
        yield ac
