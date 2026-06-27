"""Coverage for the low-severity full-application review follow-ups.

#6 — the OTel `/v1/metrics` path had no handler but was allowlisted in the
internal-API middleware (dead machine-ingest surface). It is now removed from the
allowlist; only `/v1/traces` (which has a handler) remains.

#7 — legacy `enc:` secrets were decryptable but never migrated. A startup sweep
now re-encrypts them to authenticated `fernet:`.
"""

import base64
import hashlib
import os

from backend import config

# ── #6 OTel metrics allowlist ────────────────────────────────────────────────


def test_otel_metrics_path_not_allowlisted():
    from backend.main import _OTEL_INGEST_EXACT

    assert "/api/otel/v1/traces" in _OTEL_INGEST_EXACT
    assert "/api/otel/v1/metrics" not in _OTEL_INGEST_EXACT


# ── #7 legacy enc: -> fernet: migration ──────────────────────────────────────


def _legacy_encrypt(plaintext: str) -> str:
    """Produce an `enc:` value in the exact pre-Fernet format _decrypt_legacy reads."""
    key = hashlib.pbkdf2_hmac("sha256", config._get_secret_key().encode(), b"n8n-flow-dashboard", 100_000)
    salt = os.urandom(16)
    data = plaintext.encode()
    stream = hashlib.pbkdf2_hmac("sha256", key, salt, 1, dklen=len(data))
    enc = bytes(a ^ b for a, b in zip(data, stream))
    return config._LEGACY_PREFIX + base64.urlsafe_b64encode(salt + enc).decode()


def test_migrate_legacy_enc_to_fernet_roundtrip():
    original = config.load_secrets()
    try:
        legacy_simple = _legacy_encrypt("super-secret-value")
        legacy_field = _legacy_encrypt("field-value")
        assert legacy_simple.startswith(config._LEGACY_PREFIX)
        config.save_secrets({
            "MY_KEY": legacy_simple,
            "COMPOUND": {"type": "basic_auth", "fields": {"password": legacy_field}},
        })

        config.migrate_legacy_enc_to_fernet()

        after = config.load_secrets()
        # Top-level legacy value migrated to fernet: and still decrypts.
        assert after["MY_KEY"].startswith(config._FERNET_PREFIX)
        assert config.decrypt_value(after["MY_KEY"]) == "super-secret-value"
        # Compound field migrated too.
        assert after["COMPOUND"]["fields"]["password"].startswith(config._FERNET_PREFIX)
        assert config.decrypt_value(after["COMPOUND"]["fields"]["password"]) == "field-value"

        # Idempotent: a second run leaves the now-fernet values untouched.
        config.migrate_legacy_enc_to_fernet()
        again = config.load_secrets()
        assert again["MY_KEY"] == after["MY_KEY"]
    finally:
        config.save_secrets(original)
