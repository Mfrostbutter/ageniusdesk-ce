"""Scheduled workflow backups.

Per connected n8n instance, export every workflow as JSON and write a
timestamped snapshot under ``data/backups/<instance_id>/``, pruning to the
configured retention. Runs as a scheduler job (best-effort, survives restarts)
and on demand via "Back up now". Settings live in ``config.json`` under
``backups`` so they persist with the rest of the app config.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import DATA_DIR, get_instances, load_config, save_config
from backend.modules.n8n_proxy import client as n8n_client

logger = logging.getLogger(__name__)

BACKUPS_DIR = DATA_DIR / "backups"

# Filesystem-safe UTC stamp (no colons, so it is valid on Windows too).
_STAMP_FMT = "%Y%m%dT%H%M%SZ"
_FILE_RE = re.compile(r"^\d{8}T\d{6}Z\.json$")

_REMOTE_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "provider": "s3",
    "bucket": "",
    "prefix": "",
    "endpoint_url": "",       # blank = AWS S3; set for R2/B2/Wasabi/MinIO
    "region": "",
    "access_key_id_ref": "",       # $VAR ref into the secret store
    "secret_access_key_ref": "",   # $VAR ref into the secret store
    "mirror_retention": True,      # apply keep-N pruning offsite too
    "encrypt": False,              # Fernet-encrypt bytes before upload
}

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "interval_hours": 24,
    "retention": 14,      # keep the N most recent snapshots per instance
    "active_only": False,  # back up only active workflows when True
    "remote": _REMOTE_DEFAULTS,
}

_MIN_INTERVAL_HOURS = 1
_MAX_INTERVAL_HOURS = 24 * 30
_MIN_RETENTION = 1
_MAX_RETENTION = 500


def get_settings() -> dict[str, Any]:
    """Current backup settings, defaults merged over any saved values."""
    saved = load_config().get("backups", {}) or {}
    out = {k: v for k, v in _DEFAULTS.items() if k != "remote"}
    out.update({k: saved[k] for k in out if k in saved})
    # Deep-merge the nested remote object so a saved partial keeps the defaults.
    remote = dict(_REMOTE_DEFAULTS)
    remote.update({k: v for k, v in (saved.get("remote") or {}).items() if k in _REMOTE_DEFAULTS})
    out["remote"] = remote
    return out


_REMOTE_STR_KEYS = ("bucket", "prefix", "endpoint_url", "region",
                    "access_key_id_ref", "secret_access_key_ref")


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a settings patch; returns the effective settings."""
    cur = get_settings()
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    if "active_only" in patch:
        cur["active_only"] = bool(patch["active_only"])
    if "interval_hours" in patch:
        cur["interval_hours"] = _clamp(int(patch["interval_hours"]), _MIN_INTERVAL_HOURS, _MAX_INTERVAL_HOURS)
    if "retention" in patch:
        cur["retention"] = _clamp(int(patch["retention"]), _MIN_RETENTION, _MAX_RETENTION)
    if isinstance(patch.get("remote"), dict):
        cur["remote"] = _merge_remote(cur["remote"], patch["remote"])
    config = load_config()
    config["backups"] = cur
    save_config(config)
    return cur


def _merge_remote(cur: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(cur)
    for k in ("enabled", "mirror_retention", "encrypt"):
        if k in patch:
            out[k] = bool(patch[k])
    for k in _REMOTE_STR_KEYS:
        if k in patch:
            out[k] = str(patch[k] or "").strip()
    # Only S3 is supported today; ignore any other provider value.
    out["provider"] = "s3"
    return out


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def interval_seconds() -> float:
    return get_settings()["interval_hours"] * 3600.0


def is_enabled() -> bool:
    return bool(get_settings()["enabled"])


def _instance_dir(instance_id: str) -> Path:
    return BACKUPS_DIR / instance_id


async def run_backup() -> dict[str, Any]:
    """Back up every configured instance. Best-effort per instance: one failure
    does not abort the others. Returns a per-instance summary."""
    settings = get_settings()
    active_only = settings["active_only"]
    retention = settings["retention"]
    remote_cfg = settings["remote"]
    remote_on = bool(remote_cfg.get("enabled"))
    instances = get_instances()
    stamp = datetime.now(timezone.utc).strftime(_STAMP_FMT)

    results: list[dict[str, Any]] = []
    for inst in instances:
        inst_id = inst.get("id", "")
        entry: dict[str, Any] = {"id": inst_id, "name": inst.get("name", ""), "ok": False, "count": 0, "error": ""}
        try:
            workflows = await n8n_client.export_all_workflows_for(inst, active_only=active_only)
            path = _write_snapshot(inst_id, inst.get("name", ""), stamp, workflows, active_only)
            _prune(inst_id, retention)
            entry.update(ok=True, count=len(workflows), file=path.name)
        except Exception as e:  # noqa: BLE001 - one bad instance must not sink the run
            entry["error"] = str(e)[:200]
            logger.warning("backup failed for instance %s: %s", inst_id, e)
            results.append(entry)
            continue
        # Offsite push is a separate best-effort step: a failed upload leaves the
        # local snapshot intact (ok=True) with remote_ok=False.
        if remote_on:
            entry["remote_ok"], entry["remote_error"] = await _push_remote(remote_cfg, inst_id, path, retention)
        results.append(entry)

    ok = sum(1 for r in results if r["ok"])
    total_wf = sum(r["count"] for r in results)
    logger.info("scheduled backup: %d/%d instances ok, %d workflows", ok, len(results), total_wf)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instances": results,
        "instances_ok": ok,
        "instances_total": len(results),
        "workflows_total": total_wf,
        "remote_enabled": remote_on,
    }


async def _push_remote(remote_cfg: dict[str, Any], inst_id: str, path: Path, retention: int) -> tuple[bool, str]:
    """Upload one snapshot offsite and mirror retention. Never raises: returns
    (ok, error) so a push failure is recorded, not fatal to the run."""
    from backend.modules.backups import remote as remote_sink
    try:
        data = path.read_bytes()
        await remote_sink.upload_snapshot(remote_cfg, inst_id, path.name, data)
        if remote_cfg.get("mirror_retention", True):
            await remote_sink.mirror_retention(remote_cfg, inst_id, retention)
        return True, ""
    except Exception as e:  # noqa: BLE001 - offsite failure must not lose the local copy
        logger.warning("offsite backup push failed for %s: %s", inst_id, e)
        return False, str(e)[:200]


def _write_snapshot(instance_id: str, name: str, stamp: str, workflows: list[dict], active_only: bool) -> Path:
    d = _instance_dir(instance_id)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "backup_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instance": {"id": instance_id, "name": name},
        "active_only": active_only,
        "count": len(workflows),
        "workflows": workflows,
    }
    path = d / f"{stamp}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _prune(instance_id: str, retention: int) -> int:
    """Keep the newest ``retention`` snapshots for an instance; delete the rest.
    Returns the number removed."""
    files = _list_files(instance_id)
    removed = 0
    for f in files[retention:]:
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            logger.debug("prune could not remove %s: %s", f, e)
    return removed


def _list_files(instance_id: str) -> list[Path]:
    d = _instance_dir(instance_id)
    if not d.is_dir():
        return []
    # Names are lexically sortable UTC stamps; newest first.
    return sorted((f for f in d.iterdir() if f.is_file() and _FILE_RE.match(f.name)), reverse=True)


def list_backups() -> list[dict[str, Any]]:
    """List snapshots per instance for the UI (newest first)."""
    known = {i.get("id", ""): i.get("name", "") for i in get_instances()}
    out: list[dict[str, Any]] = []
    if not BACKUPS_DIR.is_dir():
        return out
    for inst_dir in sorted(BACKUPS_DIR.iterdir()):
        if not inst_dir.is_dir():
            continue
        inst_id = inst_dir.name
        files = []
        for f in _list_files(inst_id):
            st = f.stat()
            files.append({
                "filename": f.name,
                "size_bytes": st.st_size,
                "created_at": _stamp_to_iso(f.stem),
            })
        if files:
            out.append({
                "instance_id": inst_id,
                "instance_name": known.get(inst_id, inst_id),
                "known": inst_id in known,
                "count": len(files),
                "files": files,
            })
    return out


def resolve_backup_path(instance_id: str, filename: str) -> Path | None:
    """Return the on-disk path for a snapshot, or None if the name is malformed
    or would escape the instance's backup directory (traversal guard)."""
    if not _FILE_RE.match(filename or ""):
        return None
    base = _instance_dir(instance_id).resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target if target.is_file() else None


def delete_backup(instance_id: str, filename: str) -> bool:
    path = resolve_backup_path(instance_id, filename)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _stamp_to_iso(stem: str) -> str:
    try:
        dt = datetime.strptime(stem, _STAMP_FMT).replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return ""
