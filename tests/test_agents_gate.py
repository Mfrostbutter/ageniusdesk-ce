"""The agent-surface gate: AGD_AGENTS_ENABLED resolution + auto-detect."""

import importlib.util

from backend.config import agents_enabled, settings


def test_explicit_true_wins(monkeypatch):
    monkeypatch.setattr(settings, "agd_agents_enabled", True)
    assert agents_enabled() is True


def test_explicit_false_wins(monkeypatch):
    # n8n-only experience even if the extra happens to be installed.
    monkeypatch.setattr(settings, "agd_agents_enabled", False)
    assert agents_enabled() is False


def test_auto_detects_extra_when_unset(monkeypatch):
    # None => auto: tracks whether the langgraph extra is importable.
    monkeypatch.setattr(settings, "agd_agents_enabled", None)
    expected = importlib.util.find_spec("langgraph") is not None
    assert agents_enabled() is expected
