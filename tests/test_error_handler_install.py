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


def test_handler_template_carries_webhook_token_header():
    """The handler POSTs to the login-exempt /api/errors/webhook, which is
    token-gated when AGD_WEBHOOK_TOKEN is set. Without the token header the POST
    401s and is silently dropped (continueOnFail), so the template must reference
    $env.AGD_WEBHOOK_TOKEN as a header."""
    from backend.modules.errors.router import _load_handler_template

    wf = _load_handler_template("http://10.0.0.1:3066")
    http_nodes = [n for n in wf["nodes"] if n.get("type") == "n8n-nodes-base.httpRequest"]
    assert http_nodes, "template must contain an httpRequest node"
    params = http_nodes[0]["parameters"]
    assert params.get("sendHeaders") is True
    headers = params.get("headerParameters", {}).get("parameters", [])
    names = {(h.get("name") or "").lower() for h in headers}
    assert "x-agd-webhook-token" in names
    token_hdr = next(h for h in headers if (h.get("name") or "").lower() == "x-agd-webhook-token")
    assert "AGD_WEBHOOK_TOKEN" in token_hdr.get("value", "")
    # URL was rewritten to the dashboard base passed in.
    assert "10.0.0.1:3066" in params["url"]
