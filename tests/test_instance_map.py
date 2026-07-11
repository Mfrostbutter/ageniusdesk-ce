"""Instance mapping: an OTLP trace is attributed to its SOURCE instance.

n8n's native OTel resource carries an opaque ``n8n.instance.id`` hash and nothing
AGD can match to a configured instance, so the old matcher fell back to whatever
instance was active at ingest, mis-attributing cost/health/counts. These prove
the replacement: deterministic stamp > learned hash pin > a stable
``unknown-<hash>`` bucket, and never the active instance.
"""

import pytest

from backend.modules.docker_mgr import templates
from backend.modules.observability import instance_map

NAME_TO_ID = {"test": "eb570e", "n8n template lab": "a39368", "silent-error-patch": "e5c41f"}


# ── Pure mapper: map_from_attrs ───────────────────────────────────────────────

def test_deterministic_stamp_wins():
    # A provisioned n8n stamps agd.instance.name; it maps with no pin needed.
    attrs = {"agd.instance.name": "n8n Template Lab", "n8n.instance.id": "hashX"}
    assert instance_map.map_from_attrs(attrs, {}, NAME_TO_ID) == "a39368"


def test_stamp_is_case_insensitive():
    attrs = {"agd.instance.name": "TEST"}
    assert instance_map.map_from_attrs(attrs, {}, NAME_TO_ID) == "eb570e"


def test_learned_hash_pin_used():
    attrs = {"n8n.instance.id": "895500a3", "service.name": "n8n"}
    pins = {"895500a3": "eb570e"}
    assert instance_map.map_from_attrs(attrs, pins, NAME_TO_ID) == "eb570e"


def test_stamp_beats_pin():
    # If both are present, the deterministic stamp is authoritative.
    attrs = {"agd.instance.name": "Test", "n8n.instance.id": "895500a3"}
    pins = {"895500a3": "e5c41f"}  # stale/wrong pin
    assert instance_map.map_from_attrs(attrs, pins, NAME_TO_ID) == "eb570e"


def test_unknown_hash_bucket_not_active():
    # The whole point: an unrecognized exporter is NOT attributed to any real
    # instance. It gets its own stable bucket so it can be learned later.
    attrs = {"n8n.instance.id": "deadbeef", "service.name": "n8n", "host.name": "abc123"}
    got = instance_map.map_from_attrs(attrs, {}, NAME_TO_ID)
    assert got == "unknown-deadbeef"
    assert got not in NAME_TO_ID.values()


def test_no_hash_at_all_is_unattributed():
    # A degenerate exporter with no instance identity is left unattributed (""),
    # which downstream enrichment treats as the default/active instance. Real n8n
    # always sends n8n.instance.id, so this is the test/non-n8n path only.
    assert instance_map.map_from_attrs({"service.name": "n8n"}, {}, NAME_TO_ID) == ""


def test_stamp_for_unknown_name_falls_through_to_hash_bucket():
    # A stamp naming an instance AGD does not have must not match; fall to the hash.
    attrs = {"agd.instance.name": "ghost", "n8n.instance.id": "abc"}
    assert instance_map.map_from_attrs(attrs, {}, NAME_TO_ID) == "unknown-abc"


# ── Probe resolution: resolve_hash ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_hash_single_owner(monkeypatch):
    insts = [{"id": "eb570e"}, {"id": "e5c41f"}]
    monkeypatch.setattr(instance_map, "get_instances", lambda: insts)

    async def fake_probe(inst, exec_id, wf_id):
        return inst["id"] == "eb570e"  # only Test owns the execution

    monkeypatch.setattr(instance_map, "_probe_instance", fake_probe)
    assert await instance_map.resolve_hash("h", "11706", "wfA") == "eb570e"


@pytest.mark.asyncio
async def test_resolve_hash_no_owner(monkeypatch):
    monkeypatch.setattr(instance_map, "get_instances", lambda: [{"id": "eb570e"}])

    async def fake_probe(inst, exec_id, wf_id):
        return False

    monkeypatch.setattr(instance_map, "_probe_instance", fake_probe)
    assert await instance_map.resolve_hash("h", "999", "wfA") == ""


@pytest.mark.asyncio
async def test_resolve_hash_ambiguous_refuses(monkeypatch):
    insts = [{"id": "a"}, {"id": "b"}]
    monkeypatch.setattr(instance_map, "get_instances", lambda: insts)

    async def fake_probe(inst, exec_id, wf_id):
        return True  # both claim it -> ambiguous

    monkeypatch.setattr(instance_map, "_probe_instance", fake_probe)
    assert await instance_map.resolve_hash("h", "1", "wfA") == ""


# ── Deterministic stamp is emitted by the provisioner ─────────────────────────

def test_otel_env_stamps_instance_name(monkeypatch):
    monkeypatch.setattr(templates.settings, "agd_otel_enabled", True)
    monkeypatch.setattr(templates.settings, "agd_public_url", "http://10.10.0.15:3066")
    monkeypatch.setattr(templates.settings, "agd_otel_token", "")
    env = templates._otel_export_env("n8n Template Lab")
    stamp = [e for e in env if e.startswith("OTEL_RESOURCE_ATTRIBUTES=")]
    assert stamp == ["OTEL_RESOURCE_ATTRIBUTES=agd.instance.name=n8n%20Template%20Lab"]


def test_otel_env_no_stamp_without_name(monkeypatch):
    monkeypatch.setattr(templates.settings, "agd_otel_enabled", True)
    monkeypatch.setattr(templates.settings, "agd_public_url", "http://10.10.0.15:3066")
    monkeypatch.setattr(templates.settings, "agd_otel_token", "")
    env = templates._otel_export_env("")
    assert not any(e.startswith("OTEL_RESOURCE_ATTRIBUTES=") for e in env)


# ── Enrichment fetches run-data from the trace's OWNING instance ──────────────

@pytest.mark.asyncio
async def test_raw_by_instance_active_uses_shared_client(monkeypatch):
    # get_active_instance_id / get_instances are imported function-locally from
    # backend.config, so patch them there.
    import backend.config as cfg
    from backend.modules.n8n_proxy import client as n8nc
    monkeypatch.setattr(cfg, "get_active_instance_id", lambda: "act")

    async def fake_active(exec_id):
        return {"data": "active"}

    monkeypatch.setattr(n8nc, "get_execution_raw", fake_active)
    # active id -> shared client; empty id -> also shared client (default).
    assert await n8nc.get_execution_raw_by_instance("5", "act") == {"data": "active"}
    assert await n8nc.get_execution_raw_by_instance("5", "") == {"data": "active"}


@pytest.mark.asyncio
async def test_raw_by_instance_other_uses_per_instance(monkeypatch):
    import backend.config as cfg
    from backend.modules.n8n_proxy import client as n8nc
    monkeypatch.setattr(cfg, "get_active_instance_id", lambda: "act")
    monkeypatch.setattr(cfg, "get_instances", lambda: [{"id": "other", "name": "Other"}])
    seen = {}

    async def fake_for(inst, exec_id):
        seen["inst"] = inst["id"]
        return {"data": "other"}

    monkeypatch.setattr(n8nc, "get_execution_raw_for", fake_for)
    assert await n8nc.get_execution_raw_by_instance("5", "other") == {"data": "other"}
    assert seen["inst"] == "other"


@pytest.mark.asyncio
async def test_raw_by_instance_unknown_id_empty(monkeypatch):
    import backend.config as cfg
    from backend.modules.n8n_proxy import client as n8nc
    monkeypatch.setattr(cfg, "get_active_instance_id", lambda: "act")
    monkeypatch.setattr(cfg, "get_instances", lambda: [{"id": "other"}])
    assert await n8nc.get_execution_raw_by_instance("5", "ghost") == {}
