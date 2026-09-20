"""Per-instance n8n capability record (backend/n8n_capabilities.py).

probe() builds the record from the unauthenticated /rest/settings edition
signal, /api/v1/discover key scopes, and one GET per licensed endpoint.
The routes persist it on the instance and expose it read-only.
"""

import importlib
import json

import httpx
import respx

from backend import auth_gate, n8n_capabilities
from backend.config import add_instance, get_instances, load_config, save_config
from backend.modules.n8n_proxy import client as n8n_client

n8n_router = importlib.import_module("backend.modules.n8n_proxy.router")

INST = {"id": "capstest01", "name": "Caps", "url": "http://n8n.test:5678", "api_key": "k"}
OTEL_BEARER = "Bearer otel-secret-token-never-stored"


class FakeClient:
    """Route table keyed by path -> (status, body). Unknown paths answer 404."""

    def __init__(self, routes: dict, settings_status: int = 200):
        self.routes = routes
        self.settings_status = settings_status
        self.calls: list[tuple[str, bool, bool]] = []

    async def probe_get(self, inst, path, params=None, *, authed=True, want_body=True, timeout=None):
        self.calls.append((path, authed, want_body))
        if path == "/rest/settings":
            if self.settings_status != 200:
                return (None if self.settings_status == 0 else self.settings_status), None
            return 200, self.routes.get(path, (200, {}))[1]
        status, body = self.routes.get(path, (404, None))
        return status, (body if want_body else None)


def _settings(licensed: bool, version: str = "2.37.7"):
    ent = {"saml": licensed, "ldap": licensed, "oidc": licensed}
    return 200, {"data": {"versionCli": version, "enterprise": ent}}


def _routes(*, licensed: bool, endpoint_status: int, scopes: list[str], with_workflow: bool = True):
    r = {
        "/rest/settings": _settings(licensed),
        "/api/v1/discover": (200, {"data": {"scopes": scopes}}),
        "/api/v1/data-tables": (200, {"data": []}),
        "/api/v1/workflows": (200, {"data": [{"id": "wf1"}] if with_workflow else []}),
        "/api/v1/workflows/wf1/history": (endpoint_status, {"data": []}),
        "/api/v1/settings/otel": (endpoint_status, {"exporterHeaders": {"Authorization": OTEL_BEARER}}),
        "/api/v1/source-control/pull": (405 if endpoint_status == 200 else endpoint_status, None),
    }
    for key, (path, _params) in n8n_capabilities.ENDPOINTS.items():
        r.setdefault(path, (endpoint_status, {"data": []}))
    return r


# ── probe() ──────────────────────────────────────────────────────────────────


async def test_licensed_full_scope_key_all_features_true():
    fake = FakeClient(_routes(licensed=True, endpoint_status=200, scopes=["project:list", "variable:list"]))
    rec = await n8n_capabilities.probe(INST, fake)
    assert rec["licensed"] is True
    assert rec["version"] == "2.37.7"
    assert rec["sso"] == {"saml": True, "ldap": True, "oidc": True}
    assert rec["key_scopes"] == ["project:list", "variable:list"]
    assert all(rec["features"].values()), rec["features"]
    assert rec["endpoints"]["source_control"] == 405
    assert rec["endpoints"]["workflow_history"] == 200
    assert rec["notes"] == []
    assert rec["probed_at"].endswith("+00:00")


async def test_licensed_frozen_key_403_adds_note():
    fake = FakeClient(_routes(licensed=True, endpoint_status=403, scopes=["workflow:list"]))
    rec = await n8n_capabilities.probe(INST, fake)
    assert rec["licensed"] is True
    assert rec["features"]["data_tables"] is True  # not licence-gated, still 200
    for feat in ("projects", "roles", "otel_settings", "workflow_history", "source_control"):
        assert rec["features"][feat] is False
    assert len(rec["notes"]) == 1
    assert rec["notes"][0].startswith(n8n_capabilities.FROZEN_KEY_NOTE)
    assert "roles" in rec["notes"][0]


async def test_community_instance_everything_false_no_frozen_note():
    routes = _routes(licensed=False, endpoint_status=403, scopes=[])
    routes["/api/v1/discover"] = (404, None)
    routes["/api/v1/data-tables"] = (404, None)
    fake = FakeClient(routes)
    rec = await n8n_capabilities.probe(INST, fake)
    assert rec["licensed"] is False
    assert rec["sso"] == {"saml": False, "ldap": False, "oidc": False}
    assert rec["key_scopes"] == []
    assert not any(rec["features"].values()), rec["features"]
    # a 403 on an unlicensed box is expected, never a frozen-key finding
    assert not any(n8n_capabilities.FROZEN_KEY_NOTE in n for n in rec["notes"])


async def test_settings_unreachable_treated_as_community_with_note():
    fake = FakeClient(_routes(licensed=True, endpoint_status=200, scopes=[]), settings_status=0)
    rec = await n8n_capabilities.probe(INST, fake)
    assert rec["licensed"] is False
    assert rec["version"] is None
    assert any("/rest/settings unreachable" in n and "no response" in n for n in rec["notes"])
    # endpoints still probed; a 403 without the licensed signal is not "frozen"
    assert rec["features"]["projects"] is True
    assert not any(n8n_capabilities.FROZEN_KEY_NOTE in n for n in rec["notes"])

    fake = FakeClient(_routes(licensed=True, endpoint_status=200, scopes=[]), settings_status=502)
    rec = await n8n_capabilities.probe(INST, fake)
    assert any("HTTP 502" in n for n in rec["notes"])


async def test_no_workflows_skips_history_with_note():
    fake = FakeClient(_routes(licensed=True, endpoint_status=200, scopes=[], with_workflow=False))
    rec = await n8n_capabilities.probe(INST, fake)
    assert rec["endpoints"]["workflow_history"] is None
    assert rec["features"]["workflow_history"] is False
    assert any("no workflows" in n for n in rec["notes"])
    assert not any(p.startswith("/api/v1/workflows/") for p, _a, _b in fake.calls)


async def test_otel_body_never_requested_or_stored():
    fake = FakeClient(_routes(licensed=True, endpoint_status=200, scopes=[]))
    rec = await n8n_capabilities.probe(INST, fake)
    otel_calls = [c for c in fake.calls if c[0] == "/api/v1/settings/otel"]
    assert otel_calls == [("/api/v1/settings/otel", True, False)]
    serialized = json.dumps(rec)
    assert "exporterHeaders" not in serialized
    assert OTEL_BEARER not in serialized
    assert rec["endpoints"]["settings_otel"] == 200
    # the settings probe is the only unauthenticated call
    assert [p for p, authed, _b in fake.calls if not authed] == ["/rest/settings"]


# ── client.probe_get ─────────────────────────────────────────────────────────


@respx.mock
async def test_probe_get_sends_key_drops_body_and_survives_transport_errors():
    inst = {"id": "x", "name": "x", "url": "http://probe.test:5678", "api_key": "plain-key"}
    route = respx.get("http://probe.test:5678/api/v1/settings/otel").mock(
        return_value=httpx.Response(200, json={"exporterHeaders": {"Authorization": OTEL_BEARER}})
    )
    status, body = await n8n_client.probe_get(inst, "/api/v1/settings/otel", want_body=False)
    assert (status, body) == (200, None)
    assert route.calls.last.request.headers["X-N8N-API-KEY"] == "plain-key"

    respx.get("http://probe.test:5678/rest/settings").mock(return_value=httpx.Response(200, json={"data": {"a": 1}}))
    status, body = await n8n_client.probe_get(inst, "/rest/settings", authed=False)
    assert (status, body) == (200, {"data": {"a": 1}})
    assert "X-N8N-API-KEY" not in respx.calls.last.request.headers

    respx.get("http://probe.test:5678/api/v1/source-control/pull").mock(return_value=httpx.Response(405, text="nope"))
    assert await n8n_client.probe_get(inst, "/api/v1/source-control/pull") == (405, None)

    respx.get("http://probe.test:5678/api/v1/roles").mock(side_effect=httpx.ConnectError("down"))
    assert await n8n_client.probe_get(inst, "/api/v1/roles") == (None, None)


# ── routes ───────────────────────────────────────────────────────────────────


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


_prior_active = ""


def _seed_instance(inst_id: str = "capsroute01") -> dict:
    global _prior_active
    config = load_config()
    config["instances"] = [i for i in config.get("instances", []) if i["id"] != inst_id]
    save_config(config)
    _prior_active = config.get("active_instance", "")
    add_instance({
        "id": inst_id, "name": "Caps Route", "url": "http://n8n.test:5678",
        "api_key": "old-key-1234567890", "color": "#123456",
    })
    return next(i for i in get_instances() if i["id"] == inst_id)


def _cleanup(inst_id: str):
    config = load_config()
    config["instances"] = [i for i in config.get("instances", []) if i["id"] != inst_id]
    if config.get("active_instance") == inst_id:
        config["active_instance"] = _prior_active
    save_config(config)


def _fake_probe(monkeypatch, licensed=True):
    seen = []

    async def _probe(inst, client=None):
        seen.append(inst["id"])
        rec = n8n_capabilities._empty_record()
        rec["licensed"] = licensed
        rec["features"]["projects"] = licensed
        return rec

    monkeypatch.setattr(n8n_capabilities, "probe", _probe)
    return seen


def test_refresh_route_persists_record_and_get_reads_it(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()
    seen = _fake_probe(monkeypatch, licensed=True)
    try:
        r = anon.get(f"/api/n8n/instances/{inst['id']}/capabilities")
        assert r.status_code == 200
        assert r.json() == {"instance_id": inst["id"], "capabilities": None}

        r = anon.post(f"/api/n8n/instances/{inst['id']}/capabilities/refresh")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert r.json()["capabilities"]["licensed"] is True
        assert seen == [inst["id"]]

        stored = next(i for i in get_instances() if i["id"] == inst["id"])
        assert stored["capabilities"]["licensed"] is True
        assert stored["api_key"] == inst["api_key"]  # nothing else touched

        r = anon.get(f"/api/n8n/instances/{inst['id']}/capabilities")
        assert r.json()["capabilities"]["features"]["projects"] is True

        listed = next(i for i in anon.get("/api/n8n/instances").json()["instances"] if i["id"] == inst["id"])
        assert listed["capabilities"]["licensed"] is True
        assert listed["key_hint"] == "...7890"

        assert anon.post("/api/n8n/instances/nope/capabilities/refresh").status_code == 404
        assert anon.get("/api/n8n/instances/nope/capabilities").status_code == 404
    finally:
        _cleanup(inst["id"])


def test_viewer_can_read_but_not_refresh(anon, monkeypatch):
    _as_role(monkeypatch, "viewer")
    inst = _seed_instance()
    _fake_probe(monkeypatch)
    try:
        assert anon.get(f"/api/n8n/instances/{inst['id']}/capabilities").status_code == 200
        assert anon.post(f"/api/n8n/instances/{inst['id']}/capabilities/refresh").status_code == 403
    finally:
        _cleanup(inst["id"])


def test_rotate_key_refreshes_capabilities_best_effort(anon, monkeypatch):
    _as_role(monkeypatch, "operator")
    inst = _seed_instance()

    async def fake_probe(url, api_key, verify=None):
        return {"connected": True, "error_class": "", "message": ""}

    monkeypatch.setattr(n8n_router.client, "test_connection_with", fake_probe)
    seen = _fake_probe(monkeypatch, licensed=False)
    try:
        r = anon.post(f"/api/n8n/instances/{inst['id']}/rotate-key", json={"api_key": "new-key-abcdef9999"})
        assert r.status_code == 200
        assert r.json() == {"success": True, "key_hint": "...9999"}  # response shape unchanged
        assert seen == [inst["id"]]
        stored = next(i for i in get_instances() if i["id"] == inst["id"])
        assert stored["capabilities"]["licensed"] is False

        # a probe blow-up never fails the rotation, and the prior record survives
        async def boom(inst, client=None):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(n8n_capabilities, "probe", boom)
        r = anon.post(f"/api/n8n/instances/{inst['id']}/rotate-key", json={"api_key": "new-key-abcdef0000"})
        assert r.status_code == 200
        assert r.json()["key_hint"] == "...0000"
        stored = next(i for i in get_instances() if i["id"] == inst["id"])
        assert stored["capabilities"]["licensed"] is False
    finally:
        _cleanup(inst["id"])
