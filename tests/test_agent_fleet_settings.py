"""Agent Fleet settings: provider and model choices, key resolution order, the settings API."""

import sys
import types

import pytest

from backend import auth_gate
from backend.config import load_config, save_config
from backend.modules.agent_fleet import registry, runner
from backend.modules.agent_fleet import settings as fleet_settings


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("LANGGRAPH_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_KEY", "OPENAI_API_KEY", "OPENAI_KEY",
                 "OPEN_AI_KEY", "OPENROUTER_API_KEY", "OPEN_ROUTER_KEY", "OPS_TRIAGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runner, "_assistant_settings", lambda: {})
    monkeypatch.setattr(runner, "_secret_value", lambda ref: "")
    config = load_config()
    config.pop(fleet_settings.CONFIG_KEY, None)
    save_config(config)
    yield
    config = load_config()
    config.pop(fleet_settings.CONFIG_KEY, None)
    save_config(config)


def _as_role(monkeypatch, role):
    async def _fake(_request):
        return {"username": f"{role}-user", "source": "session", "role": role, "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


# ── settings store ────────────────────────────────────────────────────────────


def test_defaults_are_empty():
    assert fleet_settings.load() == {"provider": "", "api_key_ref": "", "models": {}}


def test_save_merges_models_and_validates():
    fleet_settings.save(provider="openrouter", api_key_ref="$OPEN_ROUTER_FDE_API_KEY",
                        models={"ops-triage": "claude-sonnet-4-6"})
    fleet_settings.save(models={"health-reporter": "gpt-4.1", "ops-triage": ""})
    s = fleet_settings.load()
    assert s["provider"] == "openrouter" and s["api_key_ref"] == "$OPEN_ROUTER_FDE_API_KEY"
    assert s["models"] == {"health-reporter": "gpt-4.1"}
    with pytest.raises(ValueError):
        fleet_settings.save(provider="groq")
    with pytest.raises(ValueError):
        fleet_settings.save(api_key_ref="sk-raw-key")
    fleet_settings.save(provider="")
    assert fleet_settings.load()["provider"] == ""


def test_model_for_prefers_saved_then_env_then_default(monkeypatch):
    agent = registry.get_agent("ops-triage")
    assert fleet_settings.model_for(agent) == agent.default_model
    monkeypatch.setenv("OPS_TRIAGE_MODEL", "claude-sonnet-4-6")
    assert fleet_settings.model_for(agent) == "claude-sonnet-4-6"
    fleet_settings.save(models={"ops-triage": "claude-opus-4-1"})
    assert fleet_settings.model_for(agent) == "claude-opus-4-1"


# ── provider and key resolution ───────────────────────────────────────────────


def test_provider_order_settings_env_assistant_default(monkeypatch):
    assert runner.fleet_provider() == ("anthropic", "default")
    monkeypatch.setattr(runner, "_assistant_settings", lambda: {"provider": "openrouter"})
    assert runner.fleet_provider() == ("openrouter", "assistant")
    monkeypatch.setattr(runner, "_assistant_settings", lambda: {"provider": "groq"})
    assert runner.fleet_provider() == ("anthropic", "default")
    monkeypatch.setenv("LANGGRAPH_PROVIDER", "openai")
    assert runner.fleet_provider() == ("openai", "env")
    fleet_settings.save(provider="openrouter")
    assert runner.fleet_provider() == ("openrouter", "settings")


def test_key_order_ref_env_store_assistant(monkeypatch):
    store = {}
    monkeypatch.setattr(runner, "_secret_value", lambda ref: store.get(ref.lstrip("$"), ""))
    monkeypatch.setattr(runner, "_assistant_settings",
                        lambda: {"provider": "openrouter", "api_key": "sk-or-assistant"})
    assert runner.resolve_provider_key("openrouter") == "sk-or-assistant"
    assert runner.resolve_provider_key("anthropic") == ""
    store["OPEN_ROUTER_KEY"] = "sk-or-store"
    assert runner.resolve_provider_key("openrouter") == "sk-or-store"
    monkeypatch.setenv("OPEN_ROUTER_KEY", "sk-or-env")
    assert runner.resolve_provider_key("openrouter") == "sk-or-env"
    store["MY_ROUTER"] = "sk-or-chosen"
    fleet_settings.save(api_key_ref="$MY_ROUTER")
    assert runner.resolve_provider_key("openrouter") == "sk-or-chosen"


@pytest.mark.parametrize("given, expected", [
    ("claude-haiku-4-5", "anthropic/claude-haiku-4.5"),
    ("claude-sonnet-4-6", "anthropic/claude-sonnet-4.6"),
    ("gpt-4.1", "openai/gpt-4.1"),
    ("anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4.6"),
    ("mistral-large", "mistral-large"),
])
def test_openrouter_model_id(given, expected):
    assert runner.openrouter_model_id(given) == expected
    assert runner.effective_model("anthropic", given) == given


def test_make_chat_model_openrouter_uses_openai_client(monkeypatch):
    calls = []

    class ChatOpenAI:
        def __init__(self, **kw):
            calls.append(kw)

    monkeypatch.setitem(sys.modules, "langchain_openai", types.SimpleNamespace(ChatOpenAI=ChatOpenAI))
    runner.make_chat_model("openrouter", "claude-haiku-4-5", 1024, "sk-or")
    assert calls[0]["model"] == "anthropic/claude-haiku-4.5"
    assert calls[0]["base_url"] == runner.OPENROUTER_BASE_URL and calls[0]["api_key"] == "sk-or"


# ── API ───────────────────────────────────────────────────────────────────────


def test_settings_api_round_trip(client, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert client.get("/api/agent-fleet/settings").status_code in (401, 403)
    _as_role(monkeypatch, "admin")
    r = client.get("/api/agent-fleet/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider_effective"] == "anthropic" and body["key_present"] is False
    ids = {a["id"] for a in body["agents"]}
    assert "ops-triage" in ids and [p["id"] for p in body["providers"]] == ["anthropic", "openai", "openrouter"]
    r = client.put("/api/agent-fleet/settings", json={
        "provider": "openrouter", "api_key_ref": "$OPEN_ROUTER_KEY", "models": {"ops-triage": "claude-sonnet-4-6"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "openrouter" and body["provider_source"] == "settings"
    triage = next(a for a in body["agents"] if a["id"] == "ops-triage")
    assert triage["model"] == "claude-sonnet-4-6" and triage["model_effective"] == "anthropic/claude-sonnet-4.6"
    assert client.put("/api/agent-fleet/settings", json={"provider": "groq"}).status_code == 400
    assert client.put("/api/agent-fleet/settings", json={"api_key_ref": "sk-raw"}).status_code == 400
