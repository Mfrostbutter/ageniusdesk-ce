"""Offsite backup sink: push snapshots to S3-compatible object storage.

One integration (minio-py) reaches AWS S3, Cloudflare R2, Backblaze B2, Wasabi,
and self-hosted MinIO via ``endpoint_url``. Opt-in: the ``s3`` extra is not in
the default image, so every entry point degrades gracefully when ``minio`` is
absent. The minio client is synchronous; callers await these through
``asyncio.to_thread`` so the event loop is never blocked.

Credentials come only from the encrypted secret store via ``$VAR`` refs
(``decrypt_value``); they are never read from plaintext config.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from backend.config import decrypt_value

logger = logging.getLogger(__name__)

# Cloud instance-metadata endpoints. A private LAN address (RFC1918) is NOT
# blocked: a self-hosted MinIO on the LAN is a legitimate, primary target, so the
# usual SSRF guard would break the main use case. We only refuse the metadata IP.
_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal", "fd00:ec2::254"}

PROBE_KEY = "___agd-probe"


def remote_available() -> bool:
    """True when the optional minio dependency is importable."""
    try:
        import minio  # noqa: F401
        return True
    except Exception:
        return False


def _parse_endpoint(endpoint_url: str) -> tuple[str, bool]:
    """Return (host[:port], secure) for the minio client.

    Blank endpoint means AWS S3 (``s3.amazonaws.com``, TLS). A scheme on the URL
    sets ``secure``; without one, default to TLS on. Raises on a blocked host.
    """
    if not endpoint_url:
        return "s3.amazonaws.com", True
    url = endpoint_url if "://" in endpoint_url else f"https://{endpoint_url}"
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTS:
        raise ValueError(f"refusing S3 endpoint host: {host or '(empty)'}")
    secure = parts.scheme != "http"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return netloc, secure


def _build_client(cfg: dict[str, Any]):
    """Construct a minio client from resolved credentials. Raises with a clear
    message when the extra is missing, creds are unresolved, or config is bad."""
    if not remote_available():
        raise RuntimeError("offsite backup needs the 's3' extra: pip install '.[s3]'")
    from minio import Minio

    access_key = decrypt_value(cfg.get("access_key_id_ref", "") or "")
    secret_key = decrypt_value(cfg.get("secret_access_key_ref", "") or "")
    if not access_key or not secret_key:
        raise RuntimeError("S3 credentials are unset or their secret refs did not resolve")
    if not cfg.get("bucket"):
        raise RuntimeError("S3 bucket is required")

    endpoint, secure = _parse_endpoint(cfg.get("endpoint_url", "") or "")
    region = cfg.get("region") or None
    import os
    # Honor AGD_TLS_VERIFY like every other outbound path (self-signed LAN MinIO).
    if os.environ.get("AGD_TLS_VERIFY", "true").strip().lower() in ("0", "false", "no", "off"):
        import urllib3
        http = urllib3.PoolManager(cert_reqs="CERT_NONE")
        return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure,
                     region=region, http_client=http)
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure, region=region)


def _prefix(cfg: dict[str, Any]) -> str:
    p = (cfg.get("prefix") or "").lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


def _object_key(cfg: dict[str, Any], instance_id: str, filename: str) -> str:
    return f"{_prefix(cfg)}{instance_id}/{filename}"


def _maybe_encrypt(cfg: dict[str, Any], data: bytes, filename: str) -> tuple[bytes, str]:
    """Fernet-encrypt with the app SECRET_KEY when configured. Restoring an
    encrypted offsite copy then requires the same SECRET_KEY."""
    if not cfg.get("encrypt"):
        return data, filename
    from backend.config import _fernet
    return _fernet().encrypt(data), filename + ".enc"


async def upload_snapshot(cfg: dict[str, Any], instance_id: str, filename: str, data: bytes) -> str:
    """Upload one snapshot; returns the object key written. Runs the sync client
    off the event loop."""
    body, name = _maybe_encrypt(cfg, data, filename)
    key = _object_key(cfg, instance_id, name)
    bucket = cfg["bucket"]

    def _put():
        client = _build_client(cfg)
        client.put_object(bucket, key, io.BytesIO(body), length=len(body), content_type="application/json")

    await asyncio.to_thread(_put)
    return key


async def mirror_retention(cfg: dict[str, Any], instance_id: str, retention: int) -> int:
    """Keep the newest ``retention`` objects under this instance's prefix; delete
    the rest. Best-effort; returns the number removed. Object names embed the
    lexically-sortable UTC stamp, so a reverse name sort is newest-first."""
    bucket = cfg["bucket"]
    prefix = f"{_prefix(cfg)}{instance_id}/"

    def _prune() -> int:
        from minio.deleteobjects import DeleteObject
        client = _build_client(cfg)
        names = [o.object_name for o in client.list_objects(bucket, prefix=prefix, recursive=True)]
        names.sort(reverse=True)
        stale = names[retention:]
        removed = 0
        for _err in client.remove_objects(bucket, (DeleteObject(n) for n in stale)):
            # remove_objects yields only errors; count is derived from the input.
            logger.debug("remote prune error: %s", _err)
        if stale:
            removed = len(stale)
        return removed

    try:
        return await asyncio.to_thread(_prune)
    except Exception as e:  # noqa: BLE001 - prune failure is non-fatal to the backup
        logger.warning("remote retention prune failed for %s: %s", instance_id, e)
        return 0


async def test_remote(cfg: dict[str, Any]) -> dict[str, Any]:
    """Put then delete a tiny probe object to validate creds/bucket/endpoint."""
    bucket = cfg.get("bucket", "")
    key = f"{_prefix(cfg)}{PROBE_KEY}"

    def _probe() -> None:
        client = _build_client(cfg)
        payload = b"agd-probe"
        client.put_object(bucket, key, io.BytesIO(payload), length=len(payload), content_type="text/plain")
        client.remove_object(bucket, key)

    started = time.monotonic()
    try:
        await asyncio.to_thread(_probe)
        return {"ok": True, "error": "", "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception as e:  # noqa: BLE001 - surface any failure to the operator
        return {"ok": False, "error": str(e)[:300], "latency_ms": None}


def redacted(cfg: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Remote config safe to return over the API: keeps the $VAR ref names (which
    are not secrets), never resolves them to values."""
    cfg = cfg or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "provider": cfg.get("provider", "s3"),
        "bucket": cfg.get("bucket", ""),
        "prefix": cfg.get("prefix", ""),
        "endpoint_url": cfg.get("endpoint_url", ""),
        "region": cfg.get("region", ""),
        "access_key_id_ref": cfg.get("access_key_id_ref", ""),
        "secret_access_key_ref": cfg.get("secret_access_key_ref", ""),
        "mirror_retention": bool(cfg.get("mirror_retention", True)),
        "encrypt": bool(cfg.get("encrypt", False)),
        "available": remote_available(),
    }
