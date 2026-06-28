"""Auto-install of the global error handler into a specific instance (on connect).

install_handler_into talks to ONE instance's n8n API directly (not the active
one). Mock that API and assert the create / idempotent / auth-fail paths.
"""

import httpx
import respx

from backend.modules.errors.router import install_handler_into


@respx.mock
async def test_install_handler_creates_and_activates():
    inst = {"url": "http://n.test", "api_key": "k"}
    respx.get("http://n.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": []}))
    create = respx.post("http://n.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"id": "wf99"}))
    act = respx.post("http://n.test/api/v1/workflows/wf99/activate").mock(return_value=httpx.Response(200, json={}))

    out = await install_handler_into(inst, dashboard_url="http://10.0.0.1:3066")

    assert out["installed"] is True
    assert out["activated"] is True
    assert out["already"] is False
    assert out["error"] == ""
    assert create.called and act.called


@respx.mock
async def test_install_handler_idempotent_when_present():
    inst = {"url": "http://n.test", "api_key": "k"}
    respx.get("http://n.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "x", "name": "Global Error Handler → AgeniusDesk", "active": True},
    ]}))
    create = respx.post("http://n.test/api/v1/workflows").mock(return_value=httpx.Response(200, json={"id": "nope"}))

    out = await install_handler_into(inst)

    assert out["already"] is True
    assert out["installed"] is False
    assert out["activated"] is True
    assert not create.called  # never creates a duplicate


@respx.mock
async def test_install_handler_auth_failure_is_soft():
    inst = {"url": "http://n.test", "api_key": "bad"}
    respx.get("http://n.test/api/v1/workflows").mock(return_value=httpx.Response(401))

    out = await install_handler_into(inst)

    assert out["error"] == "auth"
    assert out["installed"] is False
    assert out["activated"] is False
