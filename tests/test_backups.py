"""Scheduled backups: the interval scheduler, the backup job, and the API.

The scheduler is exercised directly (single-flight, error capture, live gating).
The backup service is driven with a mocked per-instance export so no real n8n is
needed, and the API is checked for role gating and the download traversal guard.
"""

import asyncio

import pytest

OWNER = {"email": "owner@example.com", "password": "Fro5tbutt3r!"}


def _auth(client):
    client.cookies.clear()
    r = client.post("/api/auth/setup", json=OWNER)
    if r.status_code == 409:
        r = client.post("/api/auth/login", json={"username": OWNER["email"], "password": OWNER["password"]})
    assert r.status_code in (200, 201), r.text
    return client


def _csrf(client) -> dict:
    return {"x-agd-csrf": client.cookies.get("agd_csrf", "")}


# ── Scheduler ────────────────────────────────────────────────────────────────


async def test_scheduler_run_now_records_status():
    from backend.scheduler import Scheduler

    calls = []

    async def job():
        calls.append(1)
        return {"ran": True}

    sch = Scheduler()
    sch.register("j", job, interval_fn=lambda: 3600.0, enabled_fn=lambda: True)
    out = await sch.run_now("j")
    assert calls == [1]
    assert out["last_status"] == "ok"
    assert out["last_result"] == {"ran": True}
    st = next(s for s in sch.status() if s["id"] == "j")
    assert st["last_status"] == "ok" and st["last_error"] == ""


async def test_scheduler_captures_job_error():
    from backend.scheduler import Scheduler

    async def boom():
        raise RuntimeError("kaboom")

    sch = Scheduler()
    sch.register("j", boom, interval_fn=lambda: 3600.0, enabled_fn=lambda: True)
    out = await sch.run_now("j")
    assert out["last_status"] == "error"
    assert "kaboom" in out["last_error"]


async def test_scheduler_single_flight():
    from backend.scheduler import Scheduler

    started = asyncio.Event()
    release = asyncio.Event()
    runs = []

    async def slow():
        runs.append(1)
        started.set()
        await release.wait()

    sch = Scheduler()
    sch.register("j", slow, interval_fn=lambda: 3600.0, enabled_fn=lambda: True)
    task = asyncio.create_task(sch.run_now("j"))
    await started.wait()
    # A second trigger while the first is in-flight is skipped, not queued.
    second = await sch.run_now("j")
    assert second.get("skipped") == "already running"
    release.set()
    await task
    assert runs == [1]


async def test_scheduler_disabled_job_does_not_fire():
    from backend.scheduler import Scheduler

    runs = []

    async def job():
        runs.append(1)

    sch = Scheduler()
    # Tiny interval, but disabled: the loop must never fire it.
    sch.register("j", job, interval_fn=lambda: 0.01, enabled_fn=lambda: False)
    sch.start()
    await asyncio.sleep(0.05)
    await sch.stop()
    assert runs == []


# ── Backup service ───────────────────────────────────────────────────────────


@pytest.fixture
def two_instances(monkeypatch):
    insts = [
        {"id": "aaaaaaaa", "name": "Prod", "url": "enc-url-a", "api_key": "enc-key-a"},
        {"id": "bbbbbbbb", "name": "Dev", "url": "enc-url-b", "api_key": "enc-key-b"},
    ]
    monkeypatch.setattr("backend.modules.backups.service.get_instances", lambda: insts)
    return insts


async def test_run_backup_writes_and_prunes(two_instances, monkeypatch):
    from backend.modules.backups import service

    async def fake_export(inst, active_only=False):
        return [{"id": "1", "name": f"wf-{inst['id']}"}]

    monkeypatch.setattr(service.n8n_client, "export_all_workflows_for", fake_export)
    service.save_settings({"retention": 2, "enabled": True})

    # Three runs; retention=2 keeps the two newest per instance. Stamps are
    # second-granular, so force distinct names by patching the stamp per run.
    stamps = ["20260101T000000Z", "20260101T000100Z", "20260101T000200Z"]
    for s in stamps:
        monkeypatch.setattr(
            "backend.modules.backups.service.datetime",
            _FrozenDatetime(s),
        )
        summary = await service.run_backup()
        assert summary["instances_ok"] == 2
        assert summary["workflows_total"] == 2

    listing = {i["instance_id"]: i for i in service.list_backups()}
    assert set(listing) == {"aaaaaaaa", "bbbbbbbb"}
    for inst_id in ("aaaaaaaa", "bbbbbbbb"):
        files = [f["filename"] for f in listing[inst_id]["files"]]
        assert files == ["20260101T000200Z.json", "20260101T000100Z.json"]  # newest first, pruned to 2


async def test_run_backup_one_instance_failure_is_isolated(two_instances, monkeypatch):
    from backend.modules.backups import service

    async def fake_export(inst, active_only=False):
        if inst["id"] == "aaaaaaaa":
            raise RuntimeError("unreachable")
        return [{"id": "1"}]

    monkeypatch.setattr(service.n8n_client, "export_all_workflows_for", fake_export)
    summary = await service.run_backup()
    assert summary["instances_ok"] == 1
    by_id = {r["id"]: r for r in summary["instances"]}
    assert by_id["aaaaaaaa"]["ok"] is False and "unreachable" in by_id["aaaaaaaa"]["error"]
    assert by_id["bbbbbbbb"]["ok"] is True


def test_settings_clamp_and_persist():
    from backend.modules.backups import service

    st = service.save_settings({"interval_hours": 0, "retention": 99999, "enabled": True})
    assert st["interval_hours"] == 1          # clamped up to the floor
    assert st["retention"] == 500             # clamped to the ceiling
    assert st["enabled"] is True
    assert service.get_settings()["interval_hours"] == 1  # persisted


def test_resolve_backup_path_rejects_traversal(monkeypatch, tmp_path):
    from backend.modules.backups import service

    # A well-formed name outside the dir still resolves to None (no file);
    # a traversal-shaped name is rejected by the regex before touching disk.
    assert service.resolve_backup_path("inst", "../../etc/passwd") is None
    assert service.resolve_backup_path("inst", "20260101T000000Z.json") is None  # no such file
    assert service.resolve_backup_path("inst", "not-a-stamp.json") is None


# ── API ──────────────────────────────────────────────────────────────────────


def test_backups_api_requires_auth(anon):
    assert anon.get("/api/backups/settings").status_code in (401, 403)
    assert anon.get("/api/backups").status_code in (401, 403)


def test_backups_settings_roundtrip(client):
    _auth(client)
    r = client.put("/api/backups/settings", json={"enabled": True, "interval_hours": 12, "retention": 7},
                   headers=_csrf(client))
    assert r.status_code == 200, r.text
    st = r.json()["settings"]
    assert st["enabled"] is True and st["interval_hours"] == 12 and st["retention"] == 7
    got = client.get("/api/backups/settings").json()
    assert got["settings"]["interval_hours"] == 12
    assert got["job"]["id"] == "workflow-backup"


def test_backups_run_and_download(client, monkeypatch):
    _auth(client)
    from backend.modules.backups import service

    insts = [{"id": "cccccccc", "name": "One", "url": "u", "api_key": "k"}]
    monkeypatch.setattr("backend.modules.backups.service.get_instances", lambda: insts)

    async def fake_export(inst, active_only=False):
        return [{"id": "1", "name": "hello"}]

    monkeypatch.setattr(service.n8n_client, "export_all_workflows_for", fake_export)

    r = client.post("/api/backups/run", headers=_csrf(client))
    assert r.status_code == 200, r.text
    assert r.json()["last_result"]["instances_ok"] == 1

    listing = client.get("/api/backups").json()["instances"]
    inst = next(i for i in listing if i["instance_id"] == "cccccccc")
    fname = inst["files"][0]["filename"]
    dl = client.get(f"/api/backups/cccccccc/{fname}")
    assert dl.status_code == 200
    body = dl.json()
    assert body["count"] == 1 and body["workflows"][0]["name"] == "hello"

    # Traversal-shaped download is a 404, never a file outside the dir.
    bad = client.get("/api/backups/cccccccc/..%2f..%2fconfig.json")
    assert bad.status_code == 404


# ── Offsite / S3 sink ────────────────────────────────────────────────────────


class _FakeMinio:
    """In-memory stand-in for the minio client: key -> bytes."""

    def __init__(self):
        self.objects = {}

    def put_object(self, bucket, key, data, length, content_type=None):
        self.objects[key] = data.read()

    def remove_object(self, bucket, key):
        self.objects.pop(key, None)

    def list_objects(self, bucket, prefix, recursive=True):
        for k in list(self.objects):
            if k.startswith(prefix):
                yield type("O", (), {"object_name": k})()

    def remove_objects(self, bucket, delete_iter):
        for d in delete_iter:
            self.objects.pop(d.name, None)  # minio DeleteObject exposes .name
        return []  # minio yields only errors


@pytest.fixture
def fake_s3(monkeypatch):
    from backend.modules.backups import remote
    fake = _FakeMinio()
    monkeypatch.setattr(remote, "_build_client", lambda cfg: fake)
    monkeypatch.setattr(remote, "remote_available", lambda: True)
    return fake


_CFG = {"bucket": "b", "prefix": "agd/", "mirror_retention": True, "encrypt": False}


async def test_remote_upload_and_mirror_retention(fake_s3):
    from backend.modules.backups import remote

    for stamp in ("20260101T000000Z.json", "20260101T000100Z.json", "20260101T000200Z.json"):
        await remote.upload_snapshot(_CFG, "inst1", stamp, b'{"x":1}')
    assert len(fake_s3.objects) == 3
    removed = await remote.mirror_retention(_CFG, "inst1", 2)
    assert removed == 1
    assert set(fake_s3.objects) == {
        "agd/inst1/20260101T000200Z.json",
        "agd/inst1/20260101T000100Z.json",
    }


async def test_remote_encrypt_roundtrips(fake_s3):
    from backend.config import _fernet
    from backend.modules.backups import remote

    cfg = {**_CFG, "encrypt": True}
    key = await remote.upload_snapshot(cfg, "inst1", "20260101T000000Z.json", b'{"secret":1}')
    assert key.endswith(".json.enc")
    assert _fernet().decrypt(fake_s3.objects[key]) == b'{"secret":1}'


async def test_remote_probe_cleans_up(fake_s3):
    from backend.modules.backups import remote

    out = await remote.test_remote(_CFG)
    assert out["ok"] is True and out["latency_ms"] is not None
    assert fake_s3.objects == {}  # probe object put then removed


def test_endpoint_guard_and_scheme():
    from backend.modules.backups import remote

    assert remote._parse_endpoint("") == ("s3.amazonaws.com", True)
    assert remote._parse_endpoint("http://10.10.0.5:9000") == ("10.10.0.5:9000", False)  # LAN MinIO allowed
    assert remote._parse_endpoint("https://x.r2.cloudflarestorage.com") == ("x.r2.cloudflarestorage.com", True)
    with pytest.raises(ValueError):
        remote._parse_endpoint("http://169.254.169.254")  # cloud metadata blocked


def test_missing_extra_raises_clear_error(monkeypatch):
    from backend.modules.backups import remote
    monkeypatch.setattr(remote, "remote_available", lambda: False)
    with pytest.raises(RuntimeError, match="s3"):
        remote._build_client(_CFG)


async def test_run_backup_pushes_remote(two_instances, fake_s3, monkeypatch):
    from backend.modules.backups import service

    async def fake_export(inst, active_only=False):
        return [{"id": "1"}]

    monkeypatch.setattr(service.n8n_client, "export_all_workflows_for", fake_export)
    service.save_settings({
        "enabled": True,
        "remote": {"enabled": True, "bucket": "b", "prefix": "agd/"},
    })
    summary = await service.run_backup()
    assert summary["remote_enabled"] is True
    assert all(r["remote_ok"] for r in summary["instances"])
    # One object per instance landed in the bucket.
    assert len(fake_s3.objects) == 2


async def test_run_backup_remote_failure_keeps_local(two_instances, monkeypatch):
    from backend.modules.backups import remote, service

    async def fake_export(inst, active_only=False):
        return [{"id": "1"}]

    def boom(cfg):
        raise RuntimeError("bad creds")

    monkeypatch.setattr(service.n8n_client, "export_all_workflows_for", fake_export)
    monkeypatch.setattr(remote, "remote_available", lambda: True)
    monkeypatch.setattr(remote, "_build_client", boom)
    service.save_settings({"enabled": True, "remote": {"enabled": True, "bucket": "b"}})

    summary = await service.run_backup()
    for r in summary["instances"]:
        assert r["ok"] is True            # local snapshot still written
        assert r["remote_ok"] is False    # offsite push failed, isolated
        assert "bad creds" in r["remote_error"]
    # Local files exist regardless of the offsite failure.
    assert service.list_backups()


def test_settings_remote_merge_and_redaction(client):
    _auth(client)
    r = client.put("/api/backups/settings", headers=_csrf(client), json={
        "remote": {
            "enabled": True, "bucket": "mybucket", "endpoint_url": "https://r2.example.com",
            "access_key_id_ref": "$AGD_S3_KEY", "secret_access_key_ref": "$AGD_S3_SECRET",
        },
    })
    assert r.status_code == 200, r.text
    remote = r.json()["settings"]["remote"]
    assert remote["enabled"] is True and remote["bucket"] == "mybucket"
    # Ref names are returned (not secrets); no resolved value key leaks.
    assert remote["access_key_id_ref"] == "$AGD_S3_KEY"
    assert "available" in remote
    assert "secret_access_key" not in remote  # only the *_ref form is exposed
    # A later partial save keeps prior fields (deep merge).
    r2 = client.put("/api/backups/settings", headers=_csrf(client), json={"remote": {"prefix": "p/"}})
    m2 = r2.json()["settings"]["remote"]
    assert m2["bucket"] == "mybucket" and m2["prefix"] == "p/"


class _FrozenDatetime:
    """Minimal datetime stand-in so run_backup produces deterministic stamps."""

    def __init__(self, stamp: str):
        self._stamp = stamp
        from datetime import datetime as _dt
        self._real = _dt

    def now(self, tz=None):
        return self

    def strftime(self, fmt):
        return self._stamp

    def isoformat(self):
        return "2026-01-01T00:00:00+00:00"

    def strptime(self, *args, **kwargs):
        # list_backups() parses stamps back to ISO; delegate to the real impl.
        return self._real.strptime(*args, **kwargs)
