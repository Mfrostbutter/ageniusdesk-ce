"""BUG-014: the global error handler's Code node vs the real Error Trigger shape.

tests/fixtures/error_trigger_payload.json is a sanitized live capture from an
n8n 2.x Error Trigger (webhook-mode failure). The Code node runs in n8n, so the
test mirrors its field reads in Python and asserts them against the capture:
a contract drift (like the original `execution.workflow` read, which yielded
"Unknown Workflow" for every error) fails here.
"""

import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "error_trigger_payload.json"
HANDLER = Path(__file__).parent.parent / "backend" / "n8n_workflows" / "global-error-handler.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]


def _extract(err: dict) -> dict:
    """Python mirror of the Extract Error Details jsCode."""
    execution = err.get("execution") or {}
    workflow = err.get("workflow") or execution.get("workflow") or {}
    error = execution.get("error") or {}
    return {
        "workflow_id": workflow.get("id") or "unknown",
        "workflow_name": workflow.get("name") or "Unknown Workflow",
        "execution_id": str(execution.get("id") or ""),
        "node_name": execution.get("lastNodeExecuted") or "unknown",
        "error_message": error.get("message") or err.get("message") or "Unknown error",
        "error_type": error.get("name") or "Error",
    }


def test_workflow_is_top_level_in_live_capture():
    payload = _payload()
    assert "workflow" in payload, "Error Trigger emits workflow as a top-level sibling"
    assert "workflow" not in payload["execution"], "execution.workflow does not exist in the live shape"


def test_extract_reads_real_values_not_fallbacks():
    out = _extract(_payload())
    assert out["workflow_id"] == "wfExample123"
    assert out["workflow_name"] == "Example Failing Workflow"
    assert out["execution_id"] == "10042"
    assert out["node_name"] == "Explode"
    assert "deliberate failure" in out["error_message"]
    # error.name is absent in the live shape; the fallback is the contract.
    assert out["error_type"] == "Error"


def test_shipped_jscode_matches_the_mirrored_read():
    """The handler's actual jsCode must contain the tolerant top-level read and
    the version marker; a revert to `execution.workflow`-only fails loudly."""
    wf = json.loads(HANDLER.read_text(encoding="utf-8"))
    extract = next(n for n in wf["nodes"] if n["name"] == "Extract Error Details")
    js = extract["parameters"]["jsCode"]
    assert "err.workflow || execution.workflow" in js
    assert "agd-handler-version" in js
