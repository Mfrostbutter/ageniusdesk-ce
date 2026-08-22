"""Instance rename and API-key rotation from the Instances page.

PATCH /api/n8n/instances/{id} renames without touching any other field.
POST /api/n8n/instances/{id}/rotate-key verifies the new key connects before
swapping the stored one; a failed probe leaves the old key untouched.
"""

import importlib

from backend import auth_gate
from backend.config import add_instance, decrypt_value, get_instances, load_config, save_config

# the package re-exports `router` as the APIRouter; import the module itself
n8n_router = importlib.import_module("backend.modules.n8n_proxy.router")


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


def _seed_instance(inst_id: str = "renametest01") -> dict:
    """Insert a throwaway instance directly into config; returns the stored dict."""
    config = load_config()
    config["instances"] = [i for i in config.get("instances", []) if i["id"] != inst_id]
    save_config(config)
    global _prior_active
    _prior_active = config.get("active_instance", "")
    add_instance({
        "id": inst_id,
        "name": "Old Name",
        "url": "http://n8n.test:5678",
        "api_key": "old-key-1234567890",
        "color": "#123456",
        "owner_email": "owner@n8n.test",
        "owner_password": "pw",
        "login_url": "http://browser.test:5678",
    })
    return next(i for i in get_instances() if i["id"] == inst_id)


_prior_active = ""


def _cleanup(inst_id: str):
    """Remove the throwaway instance and restore the prior active id."""
    config = load_config()
    config["instances"] = [i for i in config.get("instances", []) if i["id"] != inst_id]
    if config.get("active_instance") == inst_id:
        config["active_instance"] = _prior_active
    save_config(config)


# ── Rename ───────────────────────────────────────────────────────────────────


def test_rename_changes_name_only(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    before = _seed_instance()
    try:
        r = anon.patch(f"/api/n8n/instances/{before['id']}", json={"name": "  New Name  "})
        assert r.status_code == 200
        assert r.json() == {"success": True, "name": "New Name"}
        after = next(i for i in get_instances() if i["id"] == before["id"])
        assert after["name"] == "New Name"
        # every other field survives byte-for-byte, including the encrypted key
        for field in ("url", "api_key", "color", "owner_email", "owner_password", "login_url"):
            assert after[field] == before[field], field
    finally:
        _cleanup(before["id"])


def test_rename_rejects_empty_and_unknown(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()
    try:
        assert anon.patch(f"/api/n8n/instances/{inst['id']}", json={"name": "   "}).status_code == 400
        assert anon.patch("/api/n8n/instances/nope", json={"name": "X"}).status_code == 404
    finally:
        _cleanup(inst["id"])


def test_rename_viewer_blocked(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert anon.patch("/api/n8n/instances/whatever", json={"name": "X"}).status_code == 403


# ── Key rotation ─────────────────────────────────────────────────────────────


def test_rotate_swaps_key_after_successful_probe(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()
    probes = []

    async def fake_probe(url, api_key, verify=None):
        probes.append((url, api_key, verify))
        return {"connected": True, "error_class": "", "message": ""}

    monkeypatch.setattr(n8n_router.client, "test_connection_with", fake_probe)
    try:
        r = anon.post(f"/api/n8n/instances/{inst['id']}/rotate-key", json={"api_key": "new-key-abcdef9999"})
        assert r.status_code == 200
        assert r.json() == {"success": True, "key_hint": "...9999"}
        # probed with the instance's decrypted url and the NEW key
        assert probes == [("http://n8n.test:5678", "new-key-abcdef9999", None)]
        after = next(i for i in get_instances() if i["id"] == inst["id"])
        assert decrypt_value(after["api_key"]) == "new-key-abcdef9999"
        assert after["name"] == "Old Name"  # nothing else touched
    finally:
        _cleanup(inst["id"])


def test_rotate_failed_probe_keeps_old_key(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()

    async def fake_probe(url, api_key, verify=None):
        return {"connected": False, "error_class": "auth", "message": "401 from n8n"}

    monkeypatch.setattr(n8n_router.client, "test_connection_with", fake_probe)
    try:
        r = anon.post(f"/api/n8n/instances/{inst['id']}/rotate-key", json={"api_key": "bad-key-xxxxxxxxx"})
        assert r.status_code == 400
        assert r.json()["detail"]["message"] == "401 from n8n"
        after = next(i for i in get_instances() if i["id"] == inst["id"])
        assert after["api_key"] == inst["api_key"]
        assert decrypt_value(after["api_key"]) == "old-key-1234567890"
    finally:
        _cleanup(inst["id"])


def test_rotate_rejects_blank_key_and_unknown_instance(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()

    async def fake_probe(url, api_key, verify=None):  # pragma: no cover - must not be reached
        raise AssertionError("probe must not run for a blank key")

    monkeypatch.setattr(n8n_router.client, "test_connection_with", fake_probe)
    try:
        assert anon.post(f"/api/n8n/instances/{inst['id']}/rotate-key", json={"api_key": "  "}).status_code == 400
        assert anon.post("/api/n8n/instances/nope/rotate-key", json={"api_key": "k"}).status_code == 404
    finally:
        _cleanup(inst["id"])


def test_rotate_respects_instance_tls_setting(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()
    from backend.config import update_instance
    update_instance(inst["id"], {"tls_verify": False})
    probes = []

    async def fake_probe(url, api_key, verify=None):
        probes.append(verify)
        return {"connected": True, "error_class": "", "message": ""}

    monkeypatch.setattr(n8n_router.client, "test_connection_with", fake_probe)
    try:
        r = anon.post(f"/api/n8n/instances/{inst['id']}/rotate-key", json={"api_key": "new-key-abcdef9999"})
        assert r.status_code == 200
        assert probes == [False]
    finally:
        _cleanup(inst["id"])
