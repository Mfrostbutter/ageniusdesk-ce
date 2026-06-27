"""Tests for the OpenAI-compatible provider registry (Perplexity, Groq, DeepSeek,
Mistral, xAI, Together, and the custom base-URL provider).

These exercise the wiring that lets AgeniusDesk drive any OpenAI-compatible
provider through the shared chat/list/ping code paths, without reaching a real
provider (respx mocks the HTTP).
"""

import json

import httpx
import respx

from backend.modules.assistant import providers


def test_registry_integrity():
    """Every registry provider is wired into key-map/defaults and has a full spec."""
    for p, spec in providers.OPENAI_COMPAT_PROVIDERS.items():
        assert p in providers.PROVIDER_KEY_MAP, f"{p} missing from PROVIDER_KEY_MAP"
        for field in ("label", "key_env", "supports_tools", "chat_url", "models_url", "default_model", "fallback"):
            assert field in spec, f"{p} spec missing {field}"
        if p != "custom":
            assert spec["chat_url"], f"{p} needs a chat_url"
            assert spec["default_model"], f"{p} needs a default_model"
            assert p in providers.PROVIDER_DEFAULTS


def test_perplexity_marked_no_tools():
    """Perplexity rejects an unknown tools field, so it must be tools-disabled."""
    assert providers.OPENAI_COMPAT_PROVIDERS["perplexity"]["supports_tools"] is False
    assert providers.OPENAI_COMPAT_PROVIDERS["groq"]["supports_tools"] is True


def test_resolve_override_resolves_compat_key(monkeypatch):
    """A {provider: groq} override resolves the $GROQ_KEY convention secret."""
    monkeypatch.setenv("GROQ_KEY", "env-groq-key")
    cfg = {"provider": "openrouter", "model": "x", "api_key": "orig"}
    out = providers._resolve_override(cfg, {"provider": "groq", "model": "llama-3.3-70b-versatile"})
    assert isinstance(out, dict)
    assert out["provider"] == "groq"
    assert out["api_key"] == "env-groq-key"
    assert out["model"] == "llama-3.3-70b-versatile"


def test_resolve_override_compat_missing_key_errors(monkeypatch):
    """No key for a compat provider returns a helpful error string, not a cfg."""
    monkeypatch.delenv("MISTRAL_KEY", raising=False)
    cfg = {"provider": "openrouter", "model": "x", "api_key": "orig"}
    out = providers._resolve_override(cfg, {"provider": "mistral", "model": "mistral-large-latest"})
    assert isinstance(out, str)
    assert "MISTRAL_KEY" in out


async def test_perplexity_chat_omits_tools():
    """Dispatching to Perplexity hits its endpoint and sends NO tools field."""
    cfg = {"provider": "perplexity", "model": "sonar", "api_key": "k", "custom_base_url": ""}
    with respx.mock:
        route = respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            })
        )
        result = await providers._dispatch_chat([{"role": "user", "content": "hi"}], "sys", cfg)
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert "tools" not in sent, "Perplexity must not receive a tools field"
    assert result["response"] == "hello"
    assert result["provider"] == "perplexity"
    assert result["usage"]["input_tokens"] == 3


async def test_custom_provider_uses_base_url():
    """The custom provider posts to <base_url>/chat/completions from config."""
    cfg = {"provider": "custom", "model": "my-model", "api_key": "k",
           "custom_base_url": "https://proxy.example.com/v1"}
    with respx.mock:
        route = respx.post("https://proxy.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            })
        )
        result = await providers._dispatch_chat([{"role": "user", "content": "hi"}], "sys", cfg)
    assert route.called
    assert result["response"] == "ok"
    assert result["provider"] == "custom"


async def test_custom_provider_without_base_url_errors():
    cfg = {"provider": "custom", "model": "x", "api_key": "k", "custom_base_url": ""}
    result = await providers._dispatch_chat([{"role": "user", "content": "hi"}], "sys", cfg)
    assert "error" in result
    assert "base URL" in result["error"]


async def test_ping_compat_provider_ok():
    with respx.mock:
        respx.post("https://api.mistral.ai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        )
        res = await providers.ping_provider("mistral", api_key="k", model="mistral-large-latest")
    assert res["ok"] is True
    assert res["model"] == "mistral-large-latest"


async def test_list_models_perplexity_fallback_no_models_endpoint():
    """Perplexity has no /models endpoint, so it always serves the curated list."""
    res = await providers.list_provider_models("perplexity")
    assert res["source"] == "fallback"
    assert res["models"] == providers.PERPLEXITY_MODELS


async def test_list_models_compat_live(monkeypatch):
    """A compat provider with a key + /models endpoint returns the live list."""
    monkeypatch.setenv("GROQ_KEY", "env-groq-key")
    providers._MODEL_CACHE.clear()
    with respx.mock:
        respx.get("https://api.groq.com/openai/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "llama-live-1"}, {"id": "llama-live-2"}]})
        )
        res = await providers.list_provider_models("groq")
    assert res["source"] == "live"
    ids = {m["id"] for m in res["models"]}
    assert ids == {"llama-live-1", "llama-live-2"}
