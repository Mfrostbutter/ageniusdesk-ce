"""Phase 1 of out-of-process backend isolation: the worker sandbox primitives.

Pure-function tests for the env allowlist, sys.path curation, and the host-import
blocker, plus a subprocess test that runs the real bootstrap and proves
`import backend` is refused inside a worker. No host (`backend`) import needed
here, so these run independent of the app.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agd_module_worker import sandbox

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKER_MAIN = _REPO_ROOT / "agd_module_worker" / "main.py"

# A representative slice of host secrets that must NEVER reach a worker.
_HOST_SECRETS = {
    "SECRET_KEY": "fernet-master",
    "OPEN_AI_KEY": "sk-openai",
    "ANTHROPIC_KEY": "sk-anthropic",
    "OPEN_ROUTER_KEY": "sk-or",
    "QDRANT_API_KEY": "qdrant",
    "DASHBOARD_MCP_TOKEN": "mcp",
    "AGD_ADMIN_TOKEN": "admin",
    "AGD_WEBHOOK_TOKEN": "hook",
    "DATABASE_PASSWORD": "pw",
}


# ── Env allowlist ─────────────────────────────────────────────────────────────


def test_build_worker_env_drops_all_host_secrets():
    parent = {"PATH": "/usr/bin", "LANG": "C", **_HOST_SECRETS}
    env = sandbox.build_worker_env(parent, injected={"AGD_MODULE_ID": "m"})
    for name in _HOST_SECRETS:
        assert name not in env, f"{name} leaked into the worker env"
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C"
    assert env["AGD_MODULE_ID"] == "m"


def test_build_worker_env_forwards_declared_nonsecret_only():
    parent = {"PATH": "/b", "WHISPER_URL": "http://w", "OPEN_AI_KEY": "sk", "EXTRA_TOKEN": "t"}
    env = sandbox.build_worker_env(
        parent, injected={}, declared_env=["WHISPER_URL", "OPEN_AI_KEY", "EXTRA_TOKEN", "MISSING"]
    )
    assert env["WHISPER_URL"] == "http://w"      # declared, non-secret, present
    assert "OPEN_AI_KEY" not in env               # declared but secret-like -> refused
    assert "EXTRA_TOKEN" not in env               # declared but secret-like -> refused
    assert "MISSING" not in env                   # declared but absent in parent


def test_build_worker_env_injected_wins():
    parent = {"PATH": "/b", "AGD_MODULE_ID": "stale"}
    env = sandbox.build_worker_env(parent, injected={"AGD_MODULE_ID": "real", "AGD_PROXY_SECRET": "s"})
    assert env["AGD_MODULE_ID"] == "real"
    assert env["AGD_PROXY_SECRET"] == "s"


def test_build_worker_env_keeps_lc_prefixed():
    parent = {"PATH": "/b", "LC_ALL": "C.UTF-8", "LC_TIME": "en_US"}
    env = sandbox.build_worker_env(parent, injected={})
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["LC_TIME"] == "en_US"


@pytest.mark.parametrize(
    "name", ["SECRET_KEY", "OPEN_AI_KEY", "ANTHROPIC_KEY", "x_token", "db_password", "MY_API_KEY", "passphrase"]
)
def test_is_secret_like_true(name):
    assert sandbox.is_secret_like(name) is True


@pytest.mark.parametrize("name", ["PATH", "WHISPER_URL", "OLLAMA_URL", "QDRANT_URL", "LANG", "key", "monkey"])
def test_is_secret_like_false(name):
    assert sandbox.is_secret_like(name) is False


# ── sys.path curation ─────────────────────────────────────────────────────────


def test_curate_sys_path_drops_host_root_keeps_runtime(tmp_path):
    host_root = str(_REPO_ROOT)
    module_parent = str(tmp_path / "data" / "modules")
    site_dir = str(tmp_path / "site-packages")
    current = [host_root, "", site_dir]
    result = sandbox.curate_sys_path(current, module_parent, host_root)
    # The host source root is dropped...
    assert host_root not in [os.path.realpath(p) for p in result if p]
    # ...the module parent is added...
    assert module_parent in result
    # ...and every other entry (stdlib, DLLs, site-packages) is preserved.
    assert site_dir in result


# ── Host-import blocker (direct) ──────────────────────────────────────────────


def test_blocker_refuses_host_package():
    blocker = sandbox.HostImportBlocker()
    with pytest.raises(sandbox.BlockedHostImportError):
        blocker.find_spec("backend")
    with pytest.raises(sandbox.BlockedHostImportError):
        blocker.find_spec("backend.config")


def test_blocker_allows_third_party():
    blocker = sandbox.HostImportBlocker()
    assert blocker.find_spec("httpx") is None
    assert blocker.find_spec("os") is None


# ── End-to-end bootstrap (subprocess, the real launch path) ───────────────────


def _run_worker_selfcheck(extra_env=None):
    env = dict(os.environ)
    env["AGD_HOST_ROOT"] = str(_REPO_ROOT)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(_WORKER_MAIN), "--selfcheck"],
        capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT), timeout=60,
    )


def test_control_backend_importable_without_sandbox():
    # Proves the selfcheck is meaningful: with host_root on the path and no
    # blocker, `import backend` succeeds. If this failed, the selfcheck below
    # would pass vacuously.
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import backend; print('OK')",
         str(_REPO_ROOT)],
        capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_worker_blocks_backend_import_end_to_end():
    proc = _run_worker_selfcheck()
    assert proc.returncode == 0, f"worker failed to block host import: {proc.stderr}"
