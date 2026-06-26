# Data Model

AgeniusDesk CE persists state across a single SQLite database and a handful of JSON files under `data/`. Secrets and instance credentials are encrypted at rest with Fernet keyed off a persisted master key; everything else is plaintext SQLite. This page documents every table, every on-disk file, the encryption scheme, the master-key lifecycle, the secret resolution order, and the idempotent boot-time migrations. See also [Architecture Overview](overview.md), [Module System](modules.md), [Security](security.md), and [API Reference](api.md).

## Storage layout

All persistent state lives under the `data/` directory (`DATA_DIR = Path("data")` in `backend/config.py`). The SQLite file is `data/dashboard.db` (`DB_FILE`); the rest are JSON or raw-key files. In production the whole directory is a Docker volume, so backing up `data/` backs up everything including the master key.

## SQLite tables

The connection is a process-wide singleton opened lazily by `get_db()` in `backend/database.py` (`aiosqlite`, `row_factory = aiosqlite.Row`). On first open it runs `_init_tables()` then `_migrate()`. Tables created in `_init_tables` via `executescript`:

| Table | Created in | Purpose | Key columns |
|---|---|---|---|
| `errors` | `_init_tables` | n8n workflow execution failures, received via webhook/sync and broadcast over WebSocket. | `id` PK; `instance_id` (added by migration, scopes the row to one n8n instance); `workflow_id`, `workflow_name`, `execution_id`, `node_name`, `error_message`, `error_type`; `occurred_at` defaults to `datetime('now')` |
| `health_checks` | `_init_tables` | Per-endpoint health probe results. | `id` PK; `endpoint_name`, `endpoint_url`, `status`, `response_ms`; `checked_at` defaults to `datetime('now')` |
| `messages` | `_init_tables` | Generic notifications received via webhook, broadcast as dashboard toasts. | `id` PK; `title`, `body`, `level` (default `info`), `source`; `occurred_at` defaults to `datetime('now')` |
| `knowledge_sources` | `_init_tables` | Registered external knowledge sources (Qdrant collections, remote search APIs) that MCP-using agents route over. No secrets stored inline; `config_json` references secrets by name. | `id` PK; `name` UNIQUE; `kind`; `description` (the routing signal); `config_json` (kind-specific JSON blob); `enabled` (default 1); `created_at`, `updated_at` |
| `auth_sessions` | `_migrate` | Local-account login sessions. Only the SHA-256 of the session token is stored, so a DB leak cannot be replayed as a live session. | `id_hash` PK (SHA-256 of raw token); `username`; `created_at`, `expires_at`, `last_seen`; `user_agent`, `ip` |
| `auth_resets` | `_migrate` | Single-use, short-lived password-reset tokens. Only the SHA-256 of the token is stored; rows are deleted on use and lazily pruned when expired. | `token_hash` PK; `username`; `created_at`, `expires_at` |

Indexes: `idx_errors_occurred`, `idx_errors_workflow`, `idx_health_endpoint`, `idx_messages_occurred` (created in `_init_tables`); `idx_errors_instance`, `idx_auth_sessions_user`, `idx_auth_resets_user` (created in `_migrate`).

`auth_sessions` and `auth_resets` are created in `_migrate` rather than `_init_tables` so they land identically on both fresh and upgraded installs without ordering concerns. `idx_errors_instance` is likewise deferred to `_migrate` because it depends on the `instance_id` column that an upgraded DB only gains via `ALTER TABLE`.

### Boot-time migrations

`_migrate()` is idempotent and runs on every boot after `_init_tables()`. Each step inspects the live schema (`PRAGMA table_info`) or uses `CREATE TABLE IF NOT EXISTS` / `DROP TABLE IF EXISTS`, so re-runs are harmless. Migrations are kept additive (`ALTER TABLE ADD COLUMN`) so a rollback needs no destructive work. Current steps:

1. **`errors.instance_id`** — added via `ALTER TABLE ... ADD COLUMN instance_id TEXT NOT NULL DEFAULT ''` when absent. Existing rows are backfilled with the current active instance id (`get_active_instance_id()`), then `idx_errors_instance` is created. The backfill is imperfect (pre-migration rows could have originated from any instance) but better than leaving them invisible behind the new filter.
2. **Drop removed-feature tables** — `voice_items`, `research_jobs`, `langgraph_runs`, `notification_routes`, `metric_snapshots`, `agent_runs` are dropped if present. These features were stripped from CE and removed from `_init_tables`, so fresh installs never create them; the drop only cleans existing databases.
3. **Create `auth_sessions`** and its `idx_auth_sessions_user` index.
4. **Create `auth_resets`** and its `idx_auth_resets_user` index.

## On-disk files

Defined as path constants in `backend/config.py`. Only the credential-bearing files are encrypted; the rest are plaintext JSON. `harden_file_permissions()` runs once at startup and best-effort `chmod 600`s each of these (effective on the Linux container, a partial no-op on Windows dev).

| File | Constant | Format | Encrypted? | Contents |
|---|---|---|---|---|
| `data/.secret_key` | `SECRET_KEY_FILE` | raw token | n/a (this **is** the key) | The master key. Auto-generated on first use if absent and `SECRET_KEY` env is unset. Losing it makes all Fernet values unrecoverable. |
| `data/config.json` | `CONFIG_FILE` | JSON | partial — embedded credential fields are Fernet-wrapped | n8n instance list (`url`, `api_key`, optional `owner_password`), assistant/MCP config, active instance, theme, setup flags, `_migrations` markers. `api_key` and `owner_password` are passed through `encrypt_value` on write. |
| `data/secrets.json` | `SECRETS_FILE` | JSON | yes — values are Fernet tokens (or `$NAME` refs) | The user-defined secrets store. Maps `NAME` to either an encrypted string (legacy single-value secret) or a compound dict `{type, fields: {field: encrypted}}`. |
| `data/secret_scope.json` | `SECRET_SCOPE_FILE` | JSON | no | Per-secret instance scope map `{"SECRET_NAME": ["inst_id", ...]}`. Empty/missing entry means "all instances". Consulted **only** by the n8n credential mirror, not by general secret resolution. |
| `data/users.json` | `USERS_FILE` | JSON | partial — TOTP secret is Fernet-wrapped | Local dashboard accounts: `username` (the email), `email`, `display_name`, `role`, PBKDF2 `password_hash`/`salt`/`iterations`/`algo`, and a `totp` block whose `secret_enc` is Fernet-encrypted and whose `recovery_codes` are SHA-256 hashes. |
| `data/dashboard.db` | `DB_FILE` | SQLite | no | The tables above. |

Config-file mutations go through helpers in `config.py` (`load_config`/`save_config`, `add_instance`, `update_instance`, `remove_instance`, `set_active_instance`). `add_instance`/`update_instance` `encrypt_value` the `api_key` and `owner_password` before persisting; on update, a blank value means "keep existing" (the field is popped from the update dict) so partial updates do not clobber stored credentials.

## Encryption

Stored sensitive values use one of these forms (`backend/config.py`):

| Prefix | Meaning |
|---|---|
| `fernet:<token>` | Current format. Authenticated encryption (AES-128-CBC + HMAC-SHA256) via `cryptography.fernet`. |
| `$NAME` / `$NAME.field` | A reference resolved at read time from env or `secrets.json`. Never encrypted at rest because the real value lives elsewhere. |
| `enc:<base64>` | Legacy homegrown XOR-stream format. Unauthenticated and broken if `SECRET_KEY` ever rotated. Read-only path kept so old installs are not silently dropped; reading one logs a deprecation warning. |
| (anything else) | Plaintext fallback (legacy / pre-encryption installs). |

`encrypt_value(plaintext)` returns `fernet:<token>`. It is a pass-through for empty strings, `$NAME` refs, and already-wrapped values (both `fernet:` and `enc:`) so callers can re-save without double-wrapping.

`decrypt_value(stored)` dispatches on prefix: `$` -> `_resolve_secret_ref`, `fernet:` -> Fernet decrypt, `enc:` -> best-effort legacy decrypt with a warning, otherwise the input is returned as plaintext. A Fernet `InvalidToken` (the stored `SECRET_KEY` does not match the one used to encrypt) is logged and the raw ciphertext is returned unchanged so the breakage surfaces rather than corrupting data.

The Fernet key is derived in `_fernet()`: `SHA-256(SECRET_KEY)` produces a 32-byte buffer that is `urlsafe_b64encode`d to a valid Fernet key, so any `SECRET_KEY` string is accepted regardless of length.

### Master key lifecycle

`_get_secret_key()` resolves the master key with this priority:

1. `SECRET_KEY` environment variable, if set.
2. `data/.secret_key` file, if it exists.
3. Otherwise, generate `secrets.token_urlsafe(32)`, write it to `data/.secret_key` (chmod 600), and log a warning.

The generated-key warning is load-bearing:

> Generated new SECRET_KEY at `data/.secret_key`. Back this file up with your `data/` directory; losing it makes encrypted values unrecoverable.

Because the key is persisted to the data volume, it survives container rebuilds. Setting `SECRET_KEY` in `.env` overrides the file. Rotating the key without re-encrypting existing values will break decryption of every `fernet:` and `enc:` value, which is why rotation is not a supported in-place operation.

## Secret resolution order

`$NAME` (and the compound `$NAME.field`) references are resolved by `_resolve_secret_ref()`:

1. **Bare `$NAME`:** check `os.environ[NAME]` first. If set and non-empty, return it. (For dotted `$NAME.field` refs the env var is skipped, because an env var can only hold a single string and cannot carry per-field structure; the secrets store is authoritative.)
2. **`data/secrets.json`:** load the store and look up `NAME`.
   - If the entry is missing, the reference string is returned unchanged (`NAME` or `NAME.field`).
   - If the entry is a **compound** secret (a dict with `type` and a `fields` map) and a field was requested, return `decrypt_value` of that field (or `""` if the field is absent).
   - If the entry is compound and **no** field was requested (bare `$NAME`), every field is decrypted and the result is returned as a JSON string. Use the dotted form in string contexts.
   - If the entry is a legacy string secret, a dotted ref returns `""` (no subfield exists) and a bare ref returns `decrypt_value(entry)`.

Boot order matters: because `os.environ` is checked first, any value present in the container environment (from `.env` or a compose `environment:` entry) shadows the secrets store for that bare ref. CE does not ship a remote-vault hydration step, so the environment is the only thing that can win over the store.

Compound secrets are detected by `_is_compound`: a dict carrying both a `type` key and a dict-typed `fields` key. `get_secret_field(name, field)` is the programmatic equivalent of `$NAME.field`, returning `""` when the secret is missing, not compound, or missing the field.

### Scope is not a security boundary

`secret_scope.json` (via `is_secret_allowed_on_instance`) gates **only** the n8n credential mirror in `backend/modules/n8n_credentials/`. `_resolve_secret_ref` ignores it entirely, so any module calling `decrypt_value("$NAME")` resolves the secret regardless of scope. Do not treat scoping as a general access-control mechanism. Unscoped secrets (empty/missing list) are allowed everywhere.

### Promotion and one-time config migration

`promote_to_secret(value, prefix, context)` moves a raw or already-encrypted value into the secrets store: it decrypts `fernet:`/`enc:` inputs first so the store owns the plaintext, derives an `UPPER_SNAKE_CASE` name (`<PREFIX>_<CONTEXT>`, suffixed `_2`, `_3`, ... on collision), encrypts for storage, sets the value on `os.environ`, and returns the `$NAME` reference. `migrate_inline_to_secrets()` is a one-time scan over `config.json` that promotes inline instance API keys, the assistant API key/Qdrant URL, and MCP server tokens into the secrets store, guarded by a `config["_migrations"]["inline_to_secrets_v1"]` marker so it runs once per install.
