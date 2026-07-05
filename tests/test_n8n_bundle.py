"""n8n template = two-container bundle (n8n + runners sidecar) for Python support.

Pure builder logic; no docker daemon. Verifies external-mode wiring, the shared
auth token, version-matched images, and the stdlib-open config seed step.
"""

from __future__ import annotations

import pytest

from backend.modules.docker_mgr import bundle as bundle_mod
from backend.modules.docker_mgr import templates


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    # update_field would import backend.config and write encrypted state to disk.
    monkeypatch.setattr(templates.template_state, "update_field", lambda *a, **k: None)
    monkeypatch.setattr(templates.template_state, "load", lambda *a, **k: {})


def _fields(**over):
    base = {
        "instance_name": "n8n-1",
        "port": 5678,
        "username": "admin",
        "password": "supersecret",
        "timezone": "America/New_York",
        "webhook_url": "",
        "n8n_version": "2.25.6",
        "runners_auth_token": "TOKEN123",  # deploy_bundle mints this before build()
    }
    base.update(over)
    return base


def _by_name(specs):
    return {s.name: s for s in specs}


def test_template_is_a_bundle():
    t = templates.get("n8n")
    assert t.bundle_id == "n8n"
    assert t.auto_secrets == ["runners_auth_token"]
    j = {x["id"]: x for x in templates.as_json()}["n8n"]
    assert j["bundle"] is True
    assert any(fd["id"] == "n8n_version" for fd in j["fields"])


def test_build_returns_two_valid_specs():
    specs = templates.get("n8n").build(_fields())
    assert [s.name for s in specs] == ["n8n", "runners"]
    norm = bundle_mod.normalise_build_result(specs, fallback_name="n8n-1")
    bundle_mod.validate_bundle(norm)  # exactly one primary, deps resolve
    assert [s.name for s in bundle_mod.topological_sort(norm)] == ["n8n", "runners"]


def test_n8n_main_external_mode_env():
    n8n = _by_name(templates.get("n8n").build(_fields()))["n8n"]
    assert n8n.role == "primary" and n8n.expose_port == 5678
    assert n8n.config["Image"] == "n8nio/n8n:2.25.6"
    env = n8n.config["Env"]
    for needed in (
        "N8N_RUNNERS_ENABLED=true",
        "N8N_RUNNERS_MODE=external",
        "N8N_RUNNERS_BROKER_LISTEN_ADDRESS=0.0.0.0",
        "N8N_RUNNERS_AUTH_TOKEN=TOKEN123",
        "N8N_NATIVE_PYTHON_RUNNER=true",
        "N8N_SECURE_COOKIE=false",
    ):
        assert needed in env
    assert not any(e.startswith("N8N_RUNNERS_MODE=internal") for e in env)


def test_runners_sidecar_and_shared_token():
    by = _by_name(templates.get("n8n").build(_fields()))
    n8n, runners = by["n8n"], by["runners"]
    assert runners.role == "service" and runners.depends_on == ["n8n"]
    assert runners.config["Image"] == "n8nio/runners:2.25.6"
    renv = runners.config["Env"]
    assert "N8N_RUNNERS_TASK_BROKER_URI=http://n8n:5679" in renv
    assert "N8N_RUNNERS_CONFIG_PATH=/config/n8n-task-runners.json" in renv
    # same token on both sides of the broker/runner link
    assert "N8N_RUNNERS_AUTH_TOKEN=TOKEN123" in renv
    assert "N8N_RUNNERS_AUTH_TOKEN=TOKEN123" in n8n.config["Env"]


def test_stdlib_open_config_seed():
    runners = _by_name(templates.get("n8n").build(_fields()))["runners"]
    assert runners.volumes == ["agd-n8n-n8n-1-runnercfg"]
    assert runners.config["HostConfig"]["Binds"] == ["agd-n8n-n8n-1-runnercfg:/config"]
    assert runners.init is not None
    assert runners.init["image"] == "n8nio/runners:2.25.6"
    assert runners.init["binds"] == ["agd-n8n-n8n-1-runnercfg:/config"]
    seed = runners.init["cmd"][0]
    assert "N8N_RUNNERS_STDLIB_ALLOW" in seed and '"*"' in seed
    assert seed.endswith("/config/n8n-task-runners.json")
    assert "|| cp /etc/n8n-task-runners.json" in seed  # graceful fallback


def test_version_defaults_to_latest_for_both_images():
    by = _by_name(templates.get("n8n").build(_fields(n8n_version="")))
    assert by["n8n"].config["Image"] == "n8nio/n8n:latest"
    assert by["runners"].config["Image"] == "n8nio/runners:latest"
    assert by["runners"].init["image"] == "n8nio/runners:latest"


def test_webhook_url_optional():
    by = _by_name(templates.get("n8n").build(_fields(webhook_url="https://n8n.example.com")))
    assert "WEBHOOK_URL=https://n8n.example.com/" in by["n8n"].config["Env"]
    by2 = _by_name(templates.get("n8n").build(_fields(webhook_url="")))
    assert not any(e.startswith("WEBHOOK_URL=") for e in by2["n8n"].config["Env"])
