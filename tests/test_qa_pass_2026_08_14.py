"""Regressions from the 2026-08-14 feature QA pass.

Covers the four fixes logged in docs/code-review/2026-08-14-feature-qa-pass.md:
instance-scoped Observe status (QA-001), the mcp version ceiling and the
dual-API shim that survives it lifting (QA-002 / QA-004), lockfile-based image
builds (QA-003), and loud failure for a built-in module (QA-005).
"""

import logging
import re
from pathlib import Path

import pytest

from backend.modules.observability import storage

REPO_ROOT = Path(__file__).resolve().parent.parent

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


# ── QA-001: Observe status is instance-scoped ───────────────────────────────


@pytest.mark.asyncio
async def test_count_spans_scopes_to_instance(client):
    """The header badge must count the active instance, not the fleet.

    A fleet-wide count next to an instance-scoped trace list reads as "data is
    arriving" on an instance that has never exported a span.
    """
    from backend.database import get_db
    db = await get_db()
    await db.execute("DELETE FROM otel_spans")
    rows = [("t1", "s1", "inst-a"), ("t2", "s2", "inst-a"), ("t3", "s3", "inst-b")]
    for trace_id, span_id, iid in rows:
        await db.execute(
            "INSERT INTO otel_spans (trace_id, span_id, instance_id, name, start_ns, end_ns) "
            "VALUES (?, ?, ?, 'workflow.execute', 1, 2)",
            (trace_id, span_id, iid),
        )
    await db.commit()

    assert await storage.count_spans() == 3, "unfiltered count is fleet-wide"
    assert await storage.count_spans("inst-a") == 2
    assert await storage.count_spans("inst-b") == 1
    assert await storage.count_spans("never-exported") == 0

    await db.execute("DELETE FROM otel_spans")
    await db.commit()


def test_otel_status_reports_both_counts(client):
    """/status carries the per-instance count and the instance identity, so the
    view can tell "nothing has ever arrived" from "nothing from this one"."""
    _auth(client)
    r = client.get("/api/otel/status")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("span_count", "instance_span_count", "instance_id", "instance_name"):
        assert key in body, f"/api/otel/status must expose {key}"
    assert body["instance_span_count"] <= body["span_count"], \
        "an instance cannot hold more spans than the fleet"


def test_observe_view_shows_setup_when_instance_has_no_spans():
    """The setup panel must render for receiver-on-but-unwired, not only
    receiver-off. That case was previously a dead end reading "run a workflow"."""
    src = (REPO_ROOT / "frontend" / "js" / "views" / "observability.js").read_text(encoding="utf-8")
    assert "instance_span_count" in src, "badge must use the instance-scoped count"
    assert re.search(r"if \(!mine\)\s*\{\s*body\.innerHTML = setupHtml\(status\)", src), \
        "receiver on + zero spans for this instance must render the setup panel"
    assert "window.location.origin" in src, \
        "setup snippet should show the real endpoint, not a <this-host> placeholder"


# ── QA-002 / QA-004: mcp pin + dual-API shim ────────────────────────────────


def test_mcp_pin_has_upper_bound():
    """mcp 2.0 removed mcp.server.fastmcp. Without a ceiling, a fresh build
    silently unmounts the built-in MCP server."""
    # Plain-text scan, not tomllib: that module is 3.11+ and CI still runs 3.10.
    raw = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pins = re.findall(r"\"(mcp\s*[><=~!][^\"]*)\"", raw)
    assert pins, "mcp must remain a declared dependency"
    assert "<2.0.0" in pins[0], (
        "mcp needs an upper bound while pydantic-ai's fastmcp-slim requires mcp<2.0; "
        "lifting it requires the langgraph extra to resolve too"
    )


def test_dashboard_mcp_supports_both_sdk_lines():
    """The server module must import and build under either mcp line, so the
    ceiling can lift without a rewrite."""
    from backend.modules.dashboard_mcp import server

    assert isinstance(server._MCP_V2, bool)
    assert server._STREAMABLE_HTTP_PATH == "/"
    assert server._TRANSPORT_SECURITY.allowed_hosts, "DNS-rebind allowlist must be populated"

    src = (REPO_ROOT / "backend" / "modules" / "dashboard_mcp" / "server.py").read_text(encoding="utf-8")
    assert "from mcp.server.mcpserver import MCPServer" in src, "must handle the 2.x class"
    assert "from mcp.server.fastmcp import FastMCP" in src, "must still handle the 1.x class"

    app = server.mcp.streamable_http_app(
        streamable_http_path=server._STREAMABLE_HTTP_PATH,
        transport_security=server._TRANSPORT_SECURITY,
    ) if server._MCP_V2 else server.mcp.streamable_http_app()
    assert app is not None


def test_dashboard_mcp_module_is_registered(client):
    """dashboard_mcp is a built-in; a dependency drift that unmounts it must
    fail the suite rather than show up as one WARNING line in production."""
    from backend import module_registry

    entry = module_registry.get_registry().get("dashboard_mcp")
    assert entry is not None, "dashboard_mcp built-in did not register at all"
    assert entry.status != "failed", f"dashboard_mcp failed to load: {entry.error}"


# ── QA-003: image builds resolve from the lockfile ──────────────────────────


def test_dockerfile_installs_from_lockfile():
    """pip install '.[extras]' re-resolved against PyPI on every build, which is
    how an upstream major release reached users' images."""
    df = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "uv.lock" in df, "the build must copy the lockfile"
    assert "--frozen" in df, "export must be frozen so the lock is authoritative"
    assert "--require-hashes" in df, "locked installs should be hash-verified"
    assert not re.search(r'pip install[^\n]*"\.\[', df), \
        "unlocked project install re-resolves dependencies at build time"


def test_lockfile_agrees_with_the_mcp_ceiling():
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    m = re.search(r'\[\[package\]\]\nname = "mcp"\nversion = "(\d+)\.', lock)
    assert m, "mcp must be present in uv.lock"
    assert m.group(1) == "1", f"lock resolved mcp {m.group(1)}.x, outside the declared ceiling"


# ── QA-005: a built-in failing to load is loud ──────────────────────────────


def test_builtin_load_failure_logs_error(caplog, tmp_path, monkeypatch):
    """A failing built-in is a degraded install, not an optional add-on
    declining to load, so it must log at ERROR and register as failed."""
    import importlib

    from backend import module_registry
    from backend import modules as modpkg

    def _boom(name):
        raise ImportError("No module named 'mcp.server.fastmcp'")

    monkeypatch.setattr(importlib, "import_module", _boom)

    fake = tmp_path / "sample_builtin"
    fake.mkdir()
    (fake / "__init__.py").write_text("", encoding="utf-8")

    class _App:
        def include_router(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("router should not be included on failure")

    with caplog.at_level(logging.ERROR, logger="backend.modules"):
        modpkg._register_builtin(_App(), fake)

    assert any(r.levelno >= logging.ERROR for r in caplog.records), \
        "a built-in failing to import must log at ERROR, not WARNING"
    assert any("FAILED to load" in r.getMessage() for r in caplog.records)

    entry = module_registry.get_registry().get("sample_builtin")
    assert entry is not None and entry.status == "failed"
    module_registry.unregister("sample_builtin")


def test_main_restates_failed_builtins_after_the_roster():
    """The roster line is the one an operator reads; a failure logged hundreds
    of lines earlier is missed."""
    src = (REPO_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert "_failed_builtins" in src
    assert "did not load" in src
