"""Phase 4 P3 misc regressions (bug-hunt remediation 2026-08-16).

BUG-034 blank-keeps-existing on update_instance, BUG-036/037 credential-type
keys, BUG-039 active-id resolution parity, BUG-042 config deep-merge,
BUG-047 backup path slug, BUG-048 concurrent note appends, BUG-049 docker
self-guard fail-closed.
"""

import asyncio

import pytest

from backend.config import (
    add_instance,
    get_active_instance,
    get_active_instance_id,
    load_config,
    save_config,
    update_instance,
)
from backend.modules.n8n_credentials.known_types import KNOWN_TYPES, detect_type_from_name

_IDS = ("p4misc1",)


def _cleanup():
    config = load_config()
    config["instances"] = [i for i in config.get("instances", []) if i["id"] not in _IDS]
    if config.get("active_instance") in _IDS:
        config["active_instance"] = ""
    save_config(config)


# ── BUG-036 / BUG-037 ────────────────────────────────────────────────────────


def test_aws_type_key_matches_n8n():
    keys = {t[0] for t in KNOWN_TYPES}
    assert "aws" in keys and "awsApi" not in keys


def test_open_ai_key_auto_detects():
    assert detect_type_from_name("OPEN_AI_KEY") == "openAiApi"
    assert detect_type_from_name("OPENAI_API_KEY") == "openAiApi"


# ── BUG-034 / BUG-039 ────────────────────────────────────────────────────────


def test_update_instance_blank_keeps_existing(client):
    add_instance({
        "id": "p4misc1", "name": "Misc", "url": "http://localhost:5678",
        "api_key": "k-1234567890", "color": "#123456", "tls_verify": True,
    })
    try:
        assert update_instance("p4misc1", {"name": "", "url": "", "color": "", "tls_verify": None})
        inst = next(i for i in load_config()["instances"] if i["id"] == "p4misc1")
        assert inst["name"] == "Misc"
        assert inst["url"] == "http://localhost:5678"
        assert inst["color"] == "#123456"
        assert inst["tls_verify"] is True
    finally:
        _cleanup()


def test_active_id_and_active_instance_agree_on_stale_id(client):
    add_instance({
        "id": "p4misc1", "name": "Misc", "url": "http://localhost:5678",
        "api_key": "k-1234567890", "color": "#123456",
    })
    config = load_config()
    config["active_instance"] = "gone-instance"
    save_config(config)
    try:
        active = get_active_instance()
        assert active is not None
        assert get_active_instance_id() == active["id"]
    finally:
        _cleanup()


# ── BUG-042 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_knowledge_partial_config_merges(client):
    from backend.modules.knowledge import storage as kstorage

    src = await kstorage.create_source(
        "p4-merge-src", "qdrant", "merge test",
        config={"url": "http://q:6333", "collection": "a", "api_key_ref": "$K"},
    )
    try:
        updated = await kstorage.update_source(src["id"], config={"collection": "b"})
        assert updated["config"] == {"url": "http://q:6333", "collection": "b", "api_key_ref": "$K"}
    finally:
        await kstorage.delete_source(src["id"])


# ── BUG-047 ──────────────────────────────────────────────────────────────────


def test_backup_path_rejects_hostile_instance_id(client):
    from backend.modules.backups.service import resolve_backup_path

    assert resolve_backup_path("../../etc", "n8n-dev_2026-08-17_020000.json") is None
    assert resolve_backup_path("a/b", "n8n-dev_2026-08-17_020000.json") is None
    assert resolve_backup_path("", "n8n-dev_2026-08-17_020000.json") is None


# ── BUG-048 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_note_appends_both_survive(client):
    from backend.modules.notes import storage as nstorage

    rel = "p4-append-race.md"
    await nstorage.write(rel, "base\n")
    try:
        await asyncio.gather(nstorage.append(rel, "one"), nstorage.append(rel, "two"))
        body = nstorage.read(rel)
        assert "one" in body and "two" in body, "a concurrent append was lost"
    finally:
        await nstorage.archive(rel)


# ── BUG-049 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_guard_fails_closed_when_unresolved(client, monkeypatch):
    from fastapi import HTTPException

    from backend.modules.docker_mgr import client as dclient
    from backend.modules.docker_mgr.router import _guard_not_self

    monkeypatch.setattr(dclient, "is_containerized", lambda: True)

    async def unresolved():
        return "", ""

    async def not_self(cid):
        return False

    monkeypatch.setattr(dclient, "self_container", unresolved)
    monkeypatch.setattr(dclient, "is_self_container", not_self)
    with pytest.raises(HTTPException) as exc:
        await _guard_not_self("deadbeef", "destroy")
    assert exc.value.status_code == 403

    # Not containerized: no self to protect, action allowed.
    monkeypatch.setattr(dclient, "is_containerized", lambda: False)
    await _guard_not_self("deadbeef", "destroy")
