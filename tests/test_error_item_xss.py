"""Stored-XSS regression for the shared error-item frontend component.

workflow_id / execution_id on an error are attacker-controlled (posted to the
login-exempt /api/errors/webhook) and rendered into onclick="..." / href="..."
contexts by frontend/js/components/error-item.js. The component was extracted
from views/errors.js in the shared-error-item refactor; if its escaper drifts
weaker than the source's, a hostile id breaks out of the attribute -> stored XSS
in the operator's authenticated dashboard.

Runs the behavioral harness (tests/js/error_item_xss_check.mjs) under node with a
DOM shim and asserts no payload escapes as live markup.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_HARNESS = Path(__file__).parent / "js" / "error_item_xss_check.mjs"


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_error_item_no_xss_breakout():
    res = subprocess.run(
        [_NODE, str(_HARNESS)], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, (res.stdout + res.stderr).strip()
