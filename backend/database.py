"""SQLite database for error storage and activity tracking."""

import aiosqlite

from backend.config import DATA_DIR, DB_FILE

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(str(DB_FILE))
        _db.row_factory = aiosqlite.Row
        await _init_tables(_db)
        await _migrate(_db)
    return _db


async def _migrate(db: aiosqlite.Connection) -> None:
    """Idempotent schema migrations for upgraded installs.

    Each step reads the current schema via PRAGMA and only runs when needed.
    Keep migrations additive (ALTER TABLE ADD COLUMN) so rollbacks do not
    need destructive work.
    """
    # errors.instance_id — lets the UI filter errors to the active n8n
    # instance so switching instances does not surface the wrong feed.
    cursor = await db.execute("PRAGMA table_info(errors)")
    cols = {row["name"] for row in await cursor.fetchall()}
    if "instance_id" not in cols:
        await db.execute("ALTER TABLE errors ADD COLUMN instance_id TEXT NOT NULL DEFAULT ''")
        # Backfill existing rows with the current active instance so they
        # are not orphaned behind the new filter. Imperfect (pre-migration
        # rows could have come from any instance) but better than invisible.
        from backend.config import get_active_instance_id
        active = get_active_instance_id()
        if active:
            await db.execute("UPDATE errors SET instance_id = ? WHERE instance_id = ''", (active,))
        await db.execute("CREATE INDEX IF NOT EXISTS idx_errors_instance ON errors(instance_id, occurred_at DESC)")
        await db.commit()

    # Removed-feature tables — drop on upgraded installs so they do not linger.
    # Each feature was stripped from this edition and removed from _init_tables,
    # so fresh installs never create them; this only cleans existing databases.
    #   voice_items                       — Voice capture (removed 2026-06-10)
    #   research_jobs                     — YouTube research module (not in CE)
    #   langgraph_runs                    — LangGraph agent module (not in CE)
    #   notification_routes               — outbound notification router (not in CE)
    #   metric_snapshots, agent_runs      — internal-only telemetry (not in CE)
    for _orphan in (
        "voice_items",
        "research_jobs",
        "langgraph_runs",
        "notification_routes",
        "metric_snapshots",
        "agent_runs",
    ):
        await db.execute(f"DROP TABLE IF EXISTS {_orphan}")
    await db.commit()

    # auth_sessions — local-account login sessions. Only the sha256 of the
    # session token is stored, so a DB leak cannot be replayed as a live
    # session. Created in _migrate (not _init_tables) so it lands on both
    # fresh and upgraded installs without ordering concerns.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id_hash     TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            user_agent  TEXT,
            ip          TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(username)")
    await db.commit()

    # auth_resets — single-use, short-lived password-reset tokens. Only the
    # sha256 of the token is stored; a DB leak cannot be replayed as a live
    # reset link. Rows are deleted on use and lazily pruned when expired.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS auth_resets (
            token_hash  TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_auth_resets_user ON auth_resets(username)")
    await db.commit()


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


async def _init_tables(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL DEFAULT '',
            workflow_id TEXT NOT NULL,
            workflow_name TEXT NOT NULL DEFAULT 'Unknown Workflow',
            execution_id TEXT DEFAULT '',
            node_name TEXT DEFAULT '',
            error_message TEXT NOT NULL,
            error_type TEXT DEFAULT 'Error',
            occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS health_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint_name TEXT NOT NULL,
            endpoint_url TEXT NOT NULL,
            status TEXT NOT NULL,
            response_ms INTEGER DEFAULT 0,
            checked_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT DEFAULT '',
            body TEXT DEFAULT '',
            level TEXT NOT NULL DEFAULT 'info',
            source TEXT DEFAULT '',
            occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_errors_occurred ON errors(occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_errors_workflow ON errors(workflow_id);
        -- idx_errors_instance lives in _migrate because upgraded installs add
        -- the instance_id column via ALTER; creating the index here would fail
        -- on first boot of an existing DB before the migration runs.
        CREATE INDEX IF NOT EXISTS idx_health_endpoint ON health_checks(endpoint_name, checked_at DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_occurred ON messages(occurred_at DESC);

        -- Registered external knowledge sources (Qdrant collections, remote
        -- search APIs, etc). `description` is the routing signal — MCP-using
        -- agents read it to pick which sources to query. `config_json` is a
        -- kind-specific JSON blob (qdrant: url/collection/vector_name/
        -- api_key_secret/embedder). Secrets referenced by name via the
        -- dashboard secrets store; nothing sensitive lives in this row.
        CREATE TABLE IF NOT EXISTS knowledge_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            config_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    await db.commit()
