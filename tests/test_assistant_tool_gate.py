"""A1 — the assistant's state-changing tools require human confirmation.

The threat: the assistant reads content the operator does not control (n8n error
payloads, execution run-data, RAG hits, MCP output). A prompt injection in any of
it can steer the model into calling trigger_workflow / set_workflow_active /
import_workflow. The system prompt tells the model to treat that content as data,
but a model is not a security boundary. The gate is.

These drive the REAL chat flow end to end — router -> providers.chat -> the
OpenAI-compatible tool loop -> _dispatch_tool — with only the HTTP call to the
LLM stubbed. That way the test fails if the gate is bypassed on any layer in
between, not just if the helper is wrong in isolation.
"""

import json

import pytest

import backend.auth_gate as auth_gate
import backend.modules.assistant.approvals as approvals
import backend.modules.assistant.mcp_client as mcp_client
import backend.modules.assistant.providers as providers
import backend.modules.assistant.tools as tools
from backend.config import settings


def _as_role(monkeypatch, role="operator"):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    approvals._reset()
    # Default posture: confirmation on. Individual tests opt out explicitly.
    monkeypatch.setattr(settings, "agd_assistant_autorun", False)
    monkeypatch.setattr(settings, "agd_assistant_confirm_mcp", True)
    yield
    approvals._reset()


def _stub_llm(monkeypatch, tool_name, arguments):
    """Make the LLM emit one tool call, then a final text answer.

    Mirrors the two rounds a real provider produces: round 1 returns tool_calls,
    round 2 (after the tool result is appended) returns prose.
    """
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp({
                    "choices": [{
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                            }],
                        },
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                })
            return _Resp({
                "choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": "Done thinking."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

    monkeypatch.setattr(providers.httpx, "AsyncClient", _Client)
    return calls


def _no_mcp(monkeypatch):
    async def _none(instance_id=None):
        return [], {}

    import backend.modules.assistant.mcp_client as mcp_client
    monkeypatch.setattr(mcp_client, "get_all_mcp_tools", _none)


def _chat(anon, text="turn off workflow 42"):
    return anon.post("/api/assistant/chat", json={
        "messages": [{"role": "user", "content": text}],
        "override": {"provider": "openrouter", "model": "test/model"},
    })


@pytest.fixture
def _executed(monkeypatch):
    """Record every n8n call the tool layer would make. Nothing real is touched."""
    seen = []

    async def _set_active(workflow_id, active):
        seen.append(("set_workflow_active", workflow_id, active))
        return {"success": True}

    monkeypatch.setattr(tools.n8n, "set_workflow_active", _set_active)
    return seen


# ── The gate ─────────────────────────────────────────────────────────────────


def test_state_changing_tool_is_not_executed_during_chat(anon, monkeypatch, _executed):
    """The whole point: the model asks, the tool does NOT run."""
    _as_role(monkeypatch)
    _no_mcp(monkeypatch)
    _stub_llm(monkeypatch, "set_workflow_active", {"workflow_id": "42", "active": False})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })

    r = _chat(anon)
    assert r.status_code == 200, r.text
    body = r.json()

    assert _executed == [], "state-changing tool ran without confirmation"
    pending = body["pending_actions"]
    assert len(pending) == 1
    assert pending[0]["tool"] == "set_workflow_active"
    assert pending[0]["arguments"] == {"workflow_id": "42", "active": False}
    assert pending[0]["id"]


def test_read_only_tool_still_runs_without_confirmation(anon, monkeypatch):
    """The gate must not break the assistant: reads are not proposals."""
    _as_role(monkeypatch)
    _no_mcp(monkeypatch)
    _stub_llm(monkeypatch, "list_workflows", {})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })
    called = {"n": 0}

    async def _list(**kw):
        called["n"] += 1
        return {"workflows": []}

    monkeypatch.setattr(tools.n8n, "list_workflows", _list)

    r = _chat(anon, "what workflows do I have")
    assert r.status_code == 200
    # >= 1, not == 1: chat() also lists workflows while building the system
    # context, so the tool-loop call is not the only one.
    assert called["n"] >= 1
    assert r.json()["pending_actions"] == []


def test_confirm_executes_the_proposed_tool(anon, monkeypatch, _executed):
    """The operator's click — and only that — runs it."""
    _as_role(monkeypatch)
    _no_mcp(monkeypatch)
    _stub_llm(monkeypatch, "set_workflow_active", {"workflow_id": "42", "active": False})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })

    pid = _chat(anon).json()["pending_actions"][0]["id"]
    assert _executed == []

    r = anon.post("/api/assistant/tools/confirm", json={"id": pid})
    assert r.status_code == 200, r.text
    assert _executed == [("set_workflow_active", "42", False)]


def test_proposal_is_single_use(anon, monkeypatch, _executed):
    """A confirmed id cannot be replayed into a second execution."""
    _as_role(monkeypatch)
    _no_mcp(monkeypatch)
    _stub_llm(monkeypatch, "set_workflow_active", {"workflow_id": "42", "active": False})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })

    pid = _chat(anon).json()["pending_actions"][0]["id"]
    assert anon.post("/api/assistant/tools/confirm", json={"id": pid}).status_code == 200
    assert anon.post("/api/assistant/tools/confirm", json={"id": pid}).status_code == 404
    assert len(_executed) == 1


def test_rejected_proposal_never_executes(anon, monkeypatch, _executed):
    _as_role(monkeypatch)
    _no_mcp(monkeypatch)
    _stub_llm(monkeypatch, "set_workflow_active", {"workflow_id": "42", "active": False})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })

    pid = _chat(anon).json()["pending_actions"][0]["id"]
    assert anon.post("/api/assistant/tools/reject", json={"id": pid}).json()["success"] is True
    assert anon.post("/api/assistant/tools/confirm", json={"id": pid}).status_code == 404
    assert _executed == []


def test_viewer_cannot_confirm(anon, monkeypatch, _executed):
    """The confirm route is the boundary, so it carries the operator floor."""
    _as_role(monkeypatch, "operator")
    _no_mcp(monkeypatch)
    _stub_llm(monkeypatch, "set_workflow_active", {"workflow_id": "42", "active": False})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })
    pid = _chat(anon).json()["pending_actions"][0]["id"]

    _as_role(monkeypatch, "viewer")
    assert anon.post("/api/assistant/tools/confirm", json={"id": pid}).status_code == 403
    assert _executed == []


def _fake_mcp(monkeypatch, tool_map):
    """Present a discovered MCP server to the tool loop, and record executions."""
    ran = []

    async def _tools(instance_id=None):
        defs = [{"type": "function", "function": {"name": n, "description": "d", "parameters": {}}}
                for n in tool_map]
        return defs, tool_map

    async def _exec(server_id, real_name, args):
        ran.append((server_id, real_name, args))
        return "mcp result"

    monkeypatch.setattr(mcp_client, "get_all_mcp_tools", _tools)
    monkeypatch.setattr(mcp_client, "execute_tool", _exec)
    return ran


def test_read_only_mcp_tool_runs_without_a_card(anon, monkeypatch):
    """Code Lab's docs lookups must not prompt. This is the regression that made
    the blunt all-MCP gate unusable."""
    _as_role(monkeypatch)
    ran = _fake_mcp(monkeypatch, {
        "mcp_s1_search_nodes": {
            "server_id": "s1", "tool_name": "search_nodes",
            "read_only": True, "confirm": "writes",
        },
    })
    _stub_llm(monkeypatch, "mcp_s1_search_nodes", {"query": "webhook"})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })

    body = _chat(anon, "which node handles webhooks").json()
    assert body["pending_actions"] == []
    assert ran == [("s1", "search_nodes", {"query": "webhook"})]


def test_write_mcp_tool_is_gated(anon, monkeypatch):
    """...while the live-instance half of the same server still stops."""
    _as_role(monkeypatch)
    ran = _fake_mcp(monkeypatch, {
        "mcp_s1_n8n_delete_workflow": {
            "server_id": "s1", "tool_name": "n8n_delete_workflow",
            "read_only": False, "confirm": "writes",
        },
    })
    _stub_llm(monkeypatch, "mcp_s1_n8n_delete_workflow", {"id": "42"})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })

    body = _chat(anon, "clean up workflow 42").json()
    assert ran == [], "MCP write ran without confirmation"
    assert len(body["pending_actions"]) == 1
    assert body["pending_actions"][0]["is_mcp"] is True

    # And the operator's click routes back to the right server + real tool name.
    pid = body["pending_actions"][0]["id"]
    assert anon.post("/api/assistant/tools/confirm", json={"id": pid}).status_code == 200
    assert ran == [("s1", "n8n_delete_workflow", {"id": "42"})]


def test_autorun_restores_unattended_execution(anon, monkeypatch, _executed):
    """Operators who opt in keep the old behavior."""
    _as_role(monkeypatch)
    _no_mcp(monkeypatch)
    monkeypatch.setattr(settings, "agd_assistant_autorun", True)
    _stub_llm(monkeypatch, "set_workflow_active", {"workflow_id": "42", "active": True})
    monkeypatch.setattr(providers, "_resolve_override", lambda cfg, o: {
        "provider": "openrouter", "model": "test/model", "api_key": "k",
    })

    body = _chat(anon).json()
    assert body["pending_actions"] == []
    assert _executed == [("set_workflow_active", "42", True)]


# ── Unit-level: the gate's decision table ────────────────────────────────────


def test_needs_confirmation_table_builtins():
    assert approvals.needs_confirmation("set_workflow_active", is_mcp=False) is True
    assert approvals.needs_confirmation("trigger_workflow", is_mcp=False) is True
    assert approvals.needs_confirmation("import_workflow", is_mcp=False) is True
    assert approvals.needs_confirmation("workspace_write", is_mcp=False) is True
    assert approvals.needs_confirmation("list_workflows", is_mcp=False) is False
    assert approvals.needs_confirmation("get_execution", is_mcp=False) is False


def _mcp(read_only=None, confirm="writes"):
    return {"server_id": "s1", "tool_name": "t", "read_only": read_only, "confirm": confirm}


def test_mcp_gate_is_per_tool_under_the_default_policy():
    """Code Lab has to stay usable: reads flow, writes stop."""
    assert approvals.needs_confirmation("mcp_s1_search_nodes", True, _mcp(read_only=True)) is False
    assert approvals.needs_confirmation("mcp_s1_n8n_create_workflow", True, _mcp(read_only=False)) is True


def test_unclassifiable_mcp_tool_fails_closed():
    """An unknown tool on an unrecognized server is a write until proven otherwise."""
    assert approvals.needs_confirmation("mcp_s1_mystery", True, _mcp(read_only=None)) is True


def test_mcp_server_policy_none_trusts_everything():
    """The docs-mode n8n-mcp: no instance creds, so nothing to confirm."""
    assert approvals.needs_confirmation("mcp_s1_anything", True, _mcp(read_only=None, confirm="none")) is False


def test_mcp_server_policy_all_gates_even_reads():
    assert approvals.needs_confirmation("mcp_s1_search_nodes", True, _mcp(read_only=True, confirm="all")) is True


def test_missing_mcp_info_fails_closed():
    """A tool map without a descriptor must not become a free pass."""
    assert approvals.needs_confirmation("mcp_s1_x", True, None) is True
    assert approvals.needs_confirmation("mcp_s1_x", True, {}) is True


def test_confirm_mcp_flag_disables_the_mcp_half(monkeypatch):
    monkeypatch.setattr(settings, "agd_assistant_confirm_mcp", False)
    assert approvals.needs_confirmation("mcp_s1_n8n_delete_workflow", True, _mcp(read_only=False)) is False
    # ...but the built-in write tools still gate.
    assert approvals.needs_confirmation("trigger_workflow", is_mcp=False) is True


# ── MCP read/write classification ────────────────────────────────────────────


def _tool(name, annotations=None):
    return {"_mcp_tool_name": name, "_mcp_annotations": annotations or {}}


def test_annotations_win_over_the_naming_convention():
    """The server author's declaration beats our heuristic, in both directions."""
    # An n8n_ tool the server says is read-only is read-only.
    assert mcp_client.classify_read_only(
        _tool("n8n_list_workflows", {"readOnlyHint": True}), "n8n-mcp") is True
    # A docs-looking tool the server says is destructive is a write.
    assert mcp_client.classify_read_only(
        _tool("search_nodes", {"destructiveHint": True}), "n8n-mcp") is False


def test_n8n_mcp_convention_splits_docs_from_live_tools():
    """The fallback when a server sends no annotations."""
    for read in ("search_nodes", "get_node", "validate_workflow", "tools_documentation",
                 "search_templates", "get_template", "validate_node"):
        assert mcp_client.classify_read_only(_tool(read), "n8n-mcp") is True, read
    for write in ("n8n_create_workflow", "n8n_delete_workflow", "n8n_update_partial_workflow",
                  "n8n_autofix_workflow", "n8n_deploy_template", "n8n_manage_credentials",
                  "n8n_trigger_webhook_workflow"):
        assert mcp_client.classify_read_only(_tool(write), "n8n-mcp") is False, write


def test_unknown_server_without_annotations_is_unclassifiable():
    """No profile, no annotations: say so rather than guess. Caller fails closed."""
    assert mcp_client.classify_read_only(_tool("search_nodes"), "") is None
    assert mcp_client.classify_read_only(_tool("delete_everything"), "") is None
    # The n8n_ convention must NOT be applied to a server we did not recognize.
    assert mcp_client.classify_read_only(_tool("harmless_lookup"), "") is None


def test_profile_detection_from_tool_inventory():
    assert mcp_client.detect_profile({"search_nodes", "get_node", "n8n_create_workflow"}) == "n8n-mcp"
    assert mcp_client.detect_profile({"validate_workflow", "tools_documentation"}) == "n8n-mcp"
    # One incidental name collision is not enough to claim the profile.
    assert mcp_client.detect_profile({"search_nodes", "unrelated"}) == ""
    assert mcp_client.detect_profile({"read_file", "write_file"}) == ""


def test_server_confirm_policy_defaults_and_validation():
    assert mcp_client.server_confirm_policy({}) == "writes"
    assert mcp_client.server_confirm_policy({"confirm": "none"}) == "none"
    assert mcp_client.server_confirm_policy({"confirm": "ALL"}) == "all"
    # Junk falls back to the safe default rather than being honored.
    assert mcp_client.server_confirm_policy({"confirm": "garbage"}) == "writes"


def test_proposal_arguments_are_scrubbed():
    p = approvals.create("trigger_workflow", {"workflow_id": "1", "payload": {"api_key": "sk-secret"}})
    assert "sk-secret" not in json.dumps(p)
    assert p["arguments"]["payload"]["api_key"] == "<redacted>"
