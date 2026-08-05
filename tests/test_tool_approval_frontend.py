"""Frontend checks for the assistant tool-approval card.

The card is rendered from data the MODEL produced (tool name, arguments), and
the model may be repeating an instruction injected into an n8n error payload or
a RAG hit. So the card renders attacker-influenced strings by design. If its
escaper drifts, the same injection that reached the model becomes XSS in the
operator's authenticated dashboard, on the exact surface built to protect them.

Runs the behavioral harness (tests/js/tool_approval_check.mjs) under node and
asserts no payload escapes as live markup, plus the card's basic contract (an
approve button always carries a proposal id; arguments are shown, not summarized).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_HARNESS = Path(__file__).parent / "js" / "tool_approval_check.mjs"


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_tool_approval_no_xss_breakout():
    res = subprocess.run(
        [_NODE, str(_HARNESS)], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, (res.stdout + res.stderr).strip()
