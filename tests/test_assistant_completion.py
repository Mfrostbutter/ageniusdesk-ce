"""Phase 4: the tool-free bridge completion executor (assistant.complete).

The security-critical guarantee: this path never offers tools (a module cannot
reach mutating assistant tools through a "completion"), the provider key is
resolved host-side, and the caller cannot set the provider base URL.
"""

import json

import httpx
import pytest
import respx

from backend.modules.assistant import completion


@pytest.fixture
def set_cfg(monkeypatch):
    def _set(provider="openrouter", model="x/y", api_key="sk-test", **extra):
        cfg = {"provider": provider, "model": model, "api_key": api_key,
               "ollama_url": "", "custom_base_url": "", **extra}
        monkeypatch.setattr(completion, "get_assistant_config", lambda: dict(cfg))
    return _set


@respx.mock
async def test_complete_is_tool_free(set_cfg):
    set_cfg(provider="openrouter", model="anthropic/claude-sonnet-4", api_key="sk-or")
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]}))
    text = await completion.complete("be terse", "hi", max_tokens=1000)
    assert text == "hello"
    sent = json.loads(route.calls.last.request.content)
    assert "tools" not in sent              # the load-bearing guarantee
    assert sent["max_tokens"] == 1000
    assert sent["messages"][0]["role"] == "system"


@respx.mock
async def test_max_tokens_clamped_to_hard_ceiling(set_cfg):
    set_cfg(provider="openrouter", model="m", api_key="sk")
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}))
    await completion.complete("s", "u", max_tokens=10_000_000)
    sent = json.loads(route.calls.last.request.content)
    assert sent["max_tokens"] == completion.HARD_MAX_TOKENS


@respx.mock
async def test_key_resolved_from_convention_secret(set_cfg, monkeypatch):
    # provider configured but no stored key -> resolve $OPEN_AI_KEY from env.
    monkeypatch.setenv("OPEN_AI_KEY", "sk-from-env")
    set_cfg(provider="openai", model="gpt-4o", api_key="")
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    text = await completion.complete("s", "u", max_tokens=500)
    assert text == "ok"
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-from-env"


@respx.mock
async def test_retries_when_max_tokens_too_large(set_cfg):
    set_cfg(provider="openrouter", model="m", api_key="sk")
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(side_effect=[
        httpx.Response(400, text="max_tokens is too large: maximum 4096"),
        httpx.Response(200, json={"choices": [{"message": {"content": "second"}}]}),
    ])
    text = await completion.complete("s", "u", max_tokens=8000)
    assert text == "second"


async def test_missing_key_errors(set_cfg):
    set_cfg(provider="openai", model="gpt-4o", api_key="")
    with pytest.raises(completion.CompletionError):
        await completion.complete("s", "u")
