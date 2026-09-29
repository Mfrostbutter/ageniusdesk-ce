"""Feature switches: resolution, the HTTP gate, and the admin API."""

import pytest

from backend import auth_gate, features
from backend.config import load_config, save_config


@pytest.fixture(autouse=True)
def _clean_features():
    config = load_config()
    config.pop("features", None)
    save_config(config)
    features._cache.update(mtime=None, state=None)
    yield
    config = load_config()
    config.pop("features", None)
    save_config(config)
    features._cache.update(mtime=None, state=None)


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


def test_default_is_everything_on():
    assert features.profile_id() is None
    assert features.enabled("llm_cost") and features.enabled("codelab")
    assert features.enabled("not-in-the-catalog")
    assert features.gate("/api/llm-cost/state") is None


def test_profile_then_overrides():
    features.set_state(profile="agency")
    assert features.enabled("llm_cost") and not features.enabled("codelab")
    features.set_state(override={"codelab": True, "llm_cost": False})
    on = features.enabled_set()
    assert "codelab" in on and "llm_cost" not in on
    features.set_state(profile="builder", reset_overrides=True)
    assert features.overrides() == {} and not features.enabled("llm_cost") and features.enabled("codelab")


def test_core_cannot_be_switched_off():
    with pytest.raises(ValueError):
        features.set_state(override={"workflows": False})
    with pytest.raises(ValueError):
        features.set_state(profile="nope")
    with pytest.raises(ValueError):
        features.set_state(override={"nope": True})


def test_gate_prefers_the_longest_prefix_and_keeps_ingest_open():
    features.set_state(override={"llm_cost": False})
    assert features.gate("/api/llm-cost/card") == "llm_cost"
    assert features.gate("/api/llm-cost/ingest") is None
    assert features.gate("/api/llm-cost/forwarder/llm-cost-forward.py") is None
    assert features.gate("/api/llm-costly") is None
    features.set_state(override={"observe": False})
    assert features.gate("/api/otel/status") == "observe"
    assert features.gate("/api/otel/v1/traces") is None
    features.set_state(override={"agents": False})
    assert features.gate("/api/agent-fleet/agents") == "agents"


def test_client_map_maps_surfaces():
    m = features.client_map()
    assert m["views"]["llm-cost"] == "llm_cost"
    assert m["views"]["agent-fleet"] == "agents"
    assert m["settings_tabs"]["features"] == "admin"
    assert m["enabled"]["insights"] is True


def test_api_read_for_viewer_change_for_admin(client, monkeypatch):
    _as_role(monkeypatch, "viewer")
    r = client.get("/api/features")
    assert r.status_code == 200 and r.json()["profile"] is None
    r = client.put("/api/features", json={"profile": "agency"})
    assert r.status_code in (401, 403)
    _as_role(monkeypatch, "admin")
    r = client.put("/api/features", json={"profile": "agency"})
    assert r.status_code == 200 and r.json()["profile"] == "agency"
    r = client.put("/api/features/codelab", json={"enabled": True})
    assert r.status_code == 200
    assert next(f for f in r.json()["features"] if f["id"] == "codelab")["enabled"] is True
    r = client.put("/api/features", json={})
    assert r.status_code == 400
    r = client.put("/api/features/workflows", json={"enabled": False})
    assert r.status_code == 400


def test_gate_middleware_hides_a_switched_off_feature(client, monkeypatch):
    _as_role(monkeypatch, "admin")
    features.set_state(override={"insights": False})
    r = client.get("/api/insights/summary")
    assert r.status_code == 404 and r.json().get("feature") == "insights"
    status = client.get("/api/status").json()
    assert status["features"]["enabled"]["insights"] is False
