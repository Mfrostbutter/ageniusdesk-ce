"""The MCP gate, tested against what the real n8n-mcp server actually declares.

tests/fixtures/n8n_mcp_tools_list.json is a verbatim capture of tools/list from
n8n-documentation-mcp 2.59.2 running in docs mode. Testing classification against
a hand-written guess would only prove the code agrees with my assumptions; this
proves it agrees with the server Code Lab will actually talk to.

Two things this pinned down that guessing got wrong:
  - Docs mode advertises the whole live-instance tool set (n8n_create_workflow,
    n8n_delete_workflow, n8n_manage_credentials, ...) even with no credentials.
    So "docs mode is harmless" is not inferable from the mode.
  - Five n8n_-prefixed tools are actually read-only (n8n_get_workflow,
    n8n_list_workflows, n8n_health_check, n8n_validate_workflow,
    n8n_audit_instance), so the naming convention is a coarse fallback, not truth.
"""

import json
from pathlib import Path

import pytest

import backend.modules.assistant.approvals as approvals
import backend.modules.assistant.mcp_client as mcp_client
from backend.config import settings

_FIXTURE = Path(__file__).parent / "fixtures" / "n8n_mcp_tools_list.json"


def _load():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["tools"]


def _as_discovered(entry):
    """Shape a fixture entry the way _normalize_tool hands it downstream."""
    return {"_mcp_tool_name": entry["name"], "_mcp_annotations": entry["annotations"]}


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(settings, "agd_assistant_autorun", False)
    monkeypatch.setattr(settings, "agd_assistant_confirm_mcp", True)


# ── What the real server declares ────────────────────────────────────────────


def test_real_server_annotates_every_tool():
    """The annotation path carries the load. If a future n8n-mcp stops sending
    readOnlyHint, this fails and the n8n_ fallback silently takes over instead —
    which is safe, but we want to know it happened."""
    tools = _load()
    assert len(tools) == 24
    missing = [t["name"] for t in tools if "readOnlyHint" not in (t["annotations"] or {})]
    assert missing == [], f"tools with no readOnlyHint: {missing}"


def test_docs_mode_still_advertises_live_instance_tools():
    """The bug the live probe caught: docs mode is NOT a tool-free server, so the
    provisioner must not trust it wholesale on the strength of its mode."""
    names = {t["name"] for t in _load()}
    for dangerous in ("n8n_create_workflow", "n8n_delete_workflow", "n8n_manage_credentials"):
        assert dangerous in names


def test_the_builtin_server_is_never_registered_as_trusted():
    """Guards the fix directly: whatever mode we launch, confirm stays 'writes'.
    Registering the built-in server as 'none' would auto-run the tools above."""
    import inspect

    from backend.modules.assistant import n8n_mcp_provision

    src = inspect.getsource(n8n_mcp_provision._register)
    assert "CONFIRM_NONE" not in src
    assert "CONFIRM_WRITES" in src


# ── Classification against the real declarations ─────────────────────────────


def test_every_real_tool_classifies_the_way_the_server_says():
    profile = mcp_client.detect_profile({t["name"] for t in _load()})
    assert profile == "n8n-mcp"
    for entry in _load():
        ann = entry["annotations"]
        got = mcp_client.classify_read_only(_as_discovered(entry), profile)
        # destructiveHint wins over a contradictory readOnlyHint; no real tool
        # sets both today, so this reduces to readOnlyHint for the whole fixture.
        want = False if ann.get("destructiveHint") is True else ann.get("readOnlyHint")
        assert got is want, f"{entry['name']}: classified {got}, server declares {ann}"


def test_code_lab_authoring_tools_never_prompt():
    """The regression that made the blunt all-MCP gate unusable. These are the
    calls Code Lab makes while building and validating a workflow."""
    by_name = {t["name"]: t for t in _load()}
    profile = "n8n-mcp"
    for name in ("search_nodes", "get_node", "validate_node", "validate_workflow",
                 "search_templates", "get_template", "tools_documentation",
                 "n8n_validate_workflow", "n8n_list_workflows", "n8n_get_workflow"):
        info = {
            "server_id": "s1", "tool_name": name, "confirm": "writes",
            "read_only": mcp_client.classify_read_only(_as_discovered(by_name[name]), profile),
        }
        assert approvals.needs_confirmation(f"mcp_s1_{name}", True, info) is False, name


def test_live_instance_mutations_always_prompt():
    by_name = {t["name"]: t for t in _load()}
    profile = "n8n-mcp"
    for name in ("n8n_create_workflow", "n8n_delete_workflow", "n8n_update_full_workflow",
                 "n8n_update_partial_workflow", "n8n_autofix_workflow", "n8n_deploy_template",
                 "n8n_manage_credentials", "n8n_manage_datatable", "n8n_test_workflow",
                 "n8n_generate_workflow"):
        info = {
            "server_id": "s1", "tool_name": name, "confirm": "writes",
            "read_only": mcp_client.classify_read_only(_as_discovered(by_name[name]), profile),
        }
        assert approvals.needs_confirmation(f"mcp_s1_{name}", True, info) is True, name


def test_naming_fallback_only_errs_toward_gating():
    """If a future server drops annotations, the n8n_ convention takes over. It
    may over-gate, but it must never let a real write through."""
    for entry in _load():
        declared_read_only = (
            False if entry["annotations"].get("destructiveHint") is True
            else entry["annotations"].get("readOnlyHint")
        )
        stripped = {"_mcp_tool_name": entry["name"], "_mcp_annotations": {}}
        fallback = mcp_client.classify_read_only(stripped, "n8n-mcp")
        if declared_read_only is False:
            assert fallback is False, (
                f"{entry['name']} is a write, but the fallback would let it run unattended"
            )


def test_contradictory_annotations_resolve_to_write():
    """A tool claiming read-only AND destructive is not to be believed on the
    reassuring half."""
    tool = {"_mcp_tool_name": "weird", "_mcp_annotations": {"readOnlyHint": True, "destructiveHint": True}}
    assert mcp_client.classify_read_only(tool, "n8n-mcp") is False
