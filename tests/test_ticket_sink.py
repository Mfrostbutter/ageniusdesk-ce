"""Ticket sink: error-group to PSA ticket loop."""

import json
import uuid

import pytest

from backend.modules.ticket_sink import service
from backend.modules.ticket_sink.mcp_client import McpError, decode_result


class FakeMcp:
    """Behavioral itops-mcp stand-in: holds tickets, tracks calls."""

    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.status = {}

    async def call(self, tool, arguments=None):
        args = arguments or {}
        self.calls.append((tool, args))
        if tool == "create_ticket":
            tid = self.next_id
            self.next_id += 1
            self.status[tid] = 1
            return {"ticket_id": tid, "ticket_number": tid, "ticket_status": 1}
        if tool == "get_ticket":
            tid = args["ticket_id"]
            return {"ticket_id": tid, "ticket_status": self.status.get(tid, 1)}
        if tool == "add_ticket_reply":
            return {"reply_id": 1}
        raise AssertionError(f"unexpected tool {tool}")

    def close(self, ticket_id):
        self.status[ticket_id] = 5


def _error(wf: str, **over):
    from datetime import datetime, timezone

    base = {
        "instance_id": "test-instance",
        "workflow_id": wf,
        "workflow_name": f"WF {wf}",
        "node_name": "HTTP Request",
        "error_type": "AxiosError",
        "error_message": "connect ECONNREFUSED",
        "execution_id": "123",
        "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    base.update(over)
    return base


@pytest.fixture
def sink(monkeypatch, client):
    """Configured sink with a fake MCP client. `client` boots the DB tables."""
    monkeypatch.setenv("AGD_TICKET_SINK_ENABLED", "1")
    monkeypatch.setenv("ITOPS_MCP_URL", "http://mcp.test:8300")
    monkeypatch.setenv("AGD_TICKET_SINK_CLIENT_ID", "1")
    monkeypatch.setenv("AGD_TICKET_SINK_REPLY_INTERVAL_MIN", "0")
    fake = FakeMcp()
    monkeypatch.setattr(service, "_get_client", lambda cfg: fake)
    return fake


async def test_disabled_is_a_no_op(monkeypatch, client):
    monkeypatch.delenv("AGD_TICKET_SINK_ENABLED", raising=False)
    fake = FakeMcp()
    monkeypatch.setattr(service, "_get_client", lambda cfg: fake)
    result = await service.handle_error(_error(uuid.uuid4().hex))
    assert result is None
    assert fake.calls == []


async def test_new_group_creates_a_ticket(sink):
    wf = uuid.uuid4().hex
    result = await service.handle_error(_error(wf))
    assert result["action"] == "created"
    assert [t for t, _ in sink.calls] == ["create_ticket"]
    created = sink.calls[0][1]
    assert created["client_id"] == 1
    assert "AxiosError" in created["subject"]

    rows = await service.list_mappings()
    row = next(r for r in rows if r["workflow_id"] == wf)
    assert row["psa_ticket_id"] == result["ticket_id"]
    assert row["occurrences"] == 1


async def test_recurrence_replies_to_the_open_ticket(sink):
    wf = uuid.uuid4().hex
    first = await service.handle_error(_error(wf))
    second = await service.handle_error(_error(wf, execution_id="124"))
    assert second == {"action": "replied", "ticket_id": first["ticket_id"]}
    tools = [t for t, _ in sink.calls]
    assert tools == ["create_ticket", "get_ticket", "add_ticket_reply"]
    reply = sink.calls[2][1]
    assert reply["reply_type"] == "Internal"
    assert "124" in reply["reply"]

    rows = await service.list_mappings()
    row = next(r for r in rows if r["workflow_id"] == wf)
    assert row["occurrences"] == 2


async def test_throttle_window_skips_mcp(sink, monkeypatch):
    monkeypatch.setenv("AGD_TICKET_SINK_REPLY_INTERVAL_MIN", "15")
    wf = uuid.uuid4().hex
    first = await service.handle_error(_error(wf))
    second = await service.handle_error(_error(wf))
    assert second == {"action": "throttled", "ticket_id": first["ticket_id"]}
    # Only the create hit MCP; the throttled event stayed local.
    assert [t for t, _ in sink.calls] == ["create_ticket"]
    rows = await service.list_mappings()
    row = next(r for r in rows if r["workflow_id"] == wf)
    assert row["occurrences"] == 2


async def test_closed_ticket_rearms_with_reference(sink):
    wf = uuid.uuid4().hex
    first = await service.handle_error(_error(wf))
    sink.close(first["ticket_id"])

    result = await service.handle_error(_error(wf))
    assert result["action"] == "re-armed"
    assert result["prior_ticket_id"] == first["ticket_id"]
    assert result["ticket_id"] != first["ticket_id"]

    # The new ticket's details reference the closed one.
    create_calls = [a for t, a in sink.calls if t == "create_ticket"]
    assert f"#{first['ticket_id']}" in create_calls[1]["details"]

    rows = await service.list_mappings()
    row = next(r for r in rows if r["workflow_id"] == wf)
    assert row["psa_ticket_id"] == result["ticket_id"]
    assert json.loads(row["closed_ticket_ids"]) == [first["ticket_id"]]


async def test_backfilled_old_events_are_skipped(sink):
    result = await service.handle_error(
        _error(uuid.uuid4().hex, occurred_at="2020-01-01 00:00:00")
    )
    assert result is None
    assert sink.calls == []


async def test_maybe_file_error_never_raises(sink, monkeypatch):
    async def boom(error):
        raise RuntimeError("psa down")

    monkeypatch.setattr(service, "handle_error", boom)
    await service.maybe_file_error(_error(uuid.uuid4().hex))


OWNER = {"email": "owner@example.com", "password": "Fro5tbutt3r!"}


def _auth(client):
    """Establish (or recover) the owner session so gated endpoints are reachable."""
    client.cookies.clear()
    r = client.post("/api/auth/setup", json=OWNER)
    if r.status_code == 409:
        r = client.post(
            "/api/auth/login",
            json={"username": OWNER["email"], "password": OWNER["password"]},
        )
    assert r.status_code in (200, 201), r.text
    return client


def test_status_endpoint(client, sink):
    _auth(client)
    resp = client.get("/api/ticket-sink/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["mcp_url_set"] is True
    assert body["client_id_set"] is True


# --- decode_result: every failure shape must raise, not silently no-op --------


def _text_result(text, is_error=False):
    return {"result": {"isError": is_error, "content": [{"type": "text", "text": text}]}}


def test_decode_in_band_iserror_raises():
    with pytest.raises(McpError, match="ticket 999 not found"):
        decode_result("get_ticket", _text_result("ticket 999 not found", is_error=True))


def test_decode_jsonrpc_error_raises():
    with pytest.raises(McpError, match="boom"):
        decode_result("t", {"error": {"message": "boom"}})


def test_decode_payload_error_field_raises():
    with pytest.raises(McpError, match="denied"):
        decode_result("t", _text_result(json.dumps({"error": "denied"})))


def test_decode_empty_content_raises():
    with pytest.raises(McpError, match="empty response"):
        decode_result("t", {"result": {"content": []}})


def test_decode_json_and_bare_string():
    assert decode_result("t", _text_result('{"ticket_id": 7}')) == {"ticket_id": 7}
    assert decode_result("t", _text_result("plain text")) == "plain text"
