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
async def test_key_resolved_from_job_api_key_ref(set_cfg, monkeypatch):
    # Post-jobs config: no legacy global key, no convention secret. The key is a
    # $REF chosen in Models and stored on the "assistant" job. The bridge must
    # resolve it the same way chat does.
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-from-ref")
    set_cfg(provider="openrouter", model="x/y", api_key="")
    monkeypatch.setattr(completion, "get_job_config", lambda s: {
        "provider": "openrouter", "model": "deepseek/deepseek-v4-flash",
        "api_key_ref": "$OPEN_ROUTER_API_KEY",
    })
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    text = await completion.complete("s", "u", max_tokens=500)
    assert text == "ok"
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-from-ref"
    # The job's model is the default when the caller names none.
    assert json.loads(route.calls.last.request.content)["model"] == "deepseek/deepseek-v4-flash"


@respx.mock
async def test_provider_model_spec_switches_provider(set_cfg, monkeypatch):
    # Module UIs send "provider::model" to switch provider for one run. The
    # bridge must decode it and resolve THAT provider's key, not send the spec
    # verbatim as a model id.
    monkeypatch.setenv("ANTHROPIC_KEY", "sk-ant-test")
    set_cfg(provider="openrouter", model="x/y", api_key="sk-or")
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}]}))
    text = await completion.complete("s", "u", model="anthropic::claude-sonnet-4-20250514", max_tokens=500)
    assert text == "hi"
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "claude-sonnet-4-20250514"
    assert route.calls.last.request.headers["x-api-key"] == "sk-ant-test"


async def test_provider_model_spec_unconfigured_provider_errors(set_cfg):
    # "openai::" with no OpenAI key anywhere -> a clear config error, not a
    # provider call with an empty key.
    set_cfg(provider="openrouter", model="x/y", api_key="sk-or")
    with pytest.raises(completion.CompletionError) as exc:
        await completion.complete("s", "u", model="openai::gpt-4o", max_tokens=500)
    assert "not configured" in str(exc.value)


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


@respx.mock
async def test_provider_error_body_not_forwarded(set_cfg):
    # LOW-3: the provider's raw error body must NOT reach the worker (it could
    # echo the Authorization header). The CompletionError is generic; the body is
    # logged host-side only.
    set_cfg(provider="openrouter", model="m", api_key="sk-secret")
    leak = "error: your key Bearer sk-secret is invalid"
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(401, text=leak))
    with pytest.raises(completion.CompletionError) as exc:
        await completion.complete("s", "u", max_tokens=500)
    msg = str(exc.value)
    assert "401" in msg
    assert "Bearer" not in msg and "sk-secret" not in msg
