"""Scanner detection for the `docker` capability (2026-07-02 follow-up).

Docker-daemon access (the docker/aiodocker SDKs or /var/run/docker.sock) is
root-equivalent on the host, so it is its own declared capability:
  - undeclared use  -> HIGH "docker"
  - declared use    -> INFO transparency (no HIGH)
  - declared, unused -> INFO over-declared
The declared-vs-detected diff carries a `docker` block either way.
"""

import pytest

from backend.module_registry import Capabilities, ModuleManifest
from backend.modules.modules.scanner import scan_module


def _write(tmp_path, code: str):
    (tmp_path / "mod.py").write_text(code, encoding="utf-8")
    return tmp_path


def _manifest(docker: bool):
    return ModuleManifest(id="dk", name="DK", capabilities=Capabilities(docker=docker))


@pytest.mark.parametrize("code", [
    "import aiodocker\nasync def run():\n    return aiodocker.Docker()\n",
    "import docker\ndef run():\n    return docker.from_env()\n",
    "def run():\n    return open('/var/run/docker.sock')\n",  # raw socket, no SDK
])
def test_undeclared_docker_is_high(tmp_path, code):
    report = scan_module(_write(tmp_path, code), ModuleManifest(id="dk", name="DK"))
    assert report.has("HIGH")
    assert any(f.category == "docker" and f.severity == "HIGH" for f in report.findings)
    diff = report.declared_vs_detected["docker"]
    assert diff["detected"] is True
    assert diff["declared"] is False


def test_declared_docker_is_info_not_high(tmp_path):
    code = "import aiodocker\nasync def run():\n    return aiodocker.Docker()\n"
    report = scan_module(_write(tmp_path, code), _manifest(docker=True))
    assert not any(f.category == "docker" and f.severity == "HIGH" for f in report.findings)
    assert any(f.category == "docker" and f.severity == "INFO" for f in report.findings)
    diff = report.declared_vs_detected["docker"]
    assert diff["declared"] is True and diff["detected"] is True


def test_docker_socket_literal_not_flagged_as_secret_access(tmp_path):
    # The socket path must be routed to the docker detector, not misread as a
    # sensitive-path secret-access finding.
    code = "SOCK = '/var/run/docker.sock'\n"
    report = scan_module(_write(tmp_path, code), ModuleManifest(id="dk", name="DK"))
    assert any(f.category == "docker" for f in report.findings)
    assert not any(f.category == "secret-access" for f in report.findings)


def test_over_declared_docker_is_info(tmp_path):
    report = scan_module(_write(tmp_path, "def add(a, b):\n    return a + b\n"), _manifest(docker=True))
    assert any(
        f.category == "over-declared" and "docker" in f.detail for f in report.findings
    )
    assert not report.has("HIGH")


def test_builtin_docker_modules_declare_capability():
    # The two built-ins that touch the daemon declare it, so they model the
    # capability correctly for community authors that copy them.
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for mid in ("docker_mgr", "assistant"):
        d = json.loads((root / "backend" / "modules" / mid / "manifest.json").read_text(encoding="utf-8"))
        assert ModuleManifest(**d).capabilities.docker is True, mid
