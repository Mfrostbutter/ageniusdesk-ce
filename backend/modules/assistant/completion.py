"""Tool-free LLM completion for the host bridge (assistant.complete).

A sandboxed community module holds no provider keys, so it asks the host to run a
completion on its behalf. This path is TOOL-FREE BY CONSTRUCTION: unlike the
assistant's _dispatch_chat, it never fetches or offers MCP/n8n tools, so a module
cannot reach mutating tools through a "completion". The provider and key come from
the saved assistant config and never cross to the worker; only the text returns.

Mirrors the (proven) youtube-research llm.py, but host-side and reusing the
provider registry. The caller supplies only system/user/model/max_tokens; it can
never set the provider base URL (that comes from host config), so there is no
SSRF surface here.
"""

from __future__ import annotations

import logging
import re

import httpx

from backend.config import decrypt_value
from backend.modules.assistant.providers import (
    DEFAULT_JOB,
    OPENAI_COMPAT_PROVIDERS,
    PROVIDER_KEY_MAP,
    _custom_base_url,
    _resolve_override,
    get_assistant_config,
    get_job_config,
)

logger = logging.getLogger(__name__)

MAX_TOKENS = 8000
HARD_MAX_TOKENS = 16000  # ceiling the bridge clamps to (cost/DoS guard)
_FALLBACK_FLOORS = [4096, 2048]
TIMEOUT = 300.0


class CompletionError(RuntimeError):
    """A completion failure with an operator-facing message."""


def _resolved_config() -> dict:
    """Saved assistant config with the provider key resolved host-side.

    Since the per-job migration the key normally lives on the "assistant" job as
    an api_key_ref chosen in Models, not on the legacy global api_key field, so
    resolve through the job the same way the chat path does. When that fails
    (pre-jobs config, deleted ref), fall back to the legacy global key or the
    conventional provider secret. The key stays on the host either way.
    """
    cfg = dict(get_assistant_config())
    try:
        job = get_job_config(DEFAULT_JOB)
        resolved = _resolve_override(cfg, {
            "provider": job.get("provider"),
            "model": job.get("model"),
            "api_key_ref": job.get("api_key_ref"),
        })
        if isinstance(resolved, dict):
            return resolved
        logger.warning("assistant job key resolution failed (%s); trying legacy config", resolved)
    except Exception:
        logger.exception("assistant job resolution failed; trying legacy config")
    provider = cfg.get("provider", "")
    if provider != "ollama" and not cfg.get("api_key"):
        name = PROVIDER_KEY_MAP.get(provider)
        if name:
            fallback = decrypt_value(f"${name}")
            if fallback and fallback != name:
                cfg["api_key"] = fallback
    return cfg


def _parse_supported_max(body: str) -> int | None:
    m = re.search(r"(?:at most|maximum(?: of)?|up to)\s+(\d{3,6})", body, re.I)
    if m:
        return int(m.group(1))
    nums = [int(n) for n in re.findall(r"\b(\d{3,6})\b", body)]
    return min(nums) if nums else None


async def complete(system: str, user: str, *, model: str = "", max_tokens: int = MAX_TOKENS) -> str:
    """Run one tool-free completion against the operator-configured provider.

    `model` overrides the saved default for this call. It accepts either a bare
    model id, or the "provider::model" spec module UIs send to switch provider
    for one run (model part optional: "openrouter::" means that provider's
    default). The provider's key is resolved host-side either way. Retries with
    a lower max_tokens if the provider rejects it as too large. Raises
    CompletionError on misconfiguration or an unrecoverable provider failure.
    """
    cfg = _resolved_config()
    model = (model or "").strip()
    if "::" in model:
        ov_provider, _, ov_model = model.partition("::")
        resolved = _resolve_override(
            cfg, {"provider": ov_provider.strip(), "model": ov_model.strip() or None})
        if isinstance(resolved, str):
            raise CompletionError(resolved)
        cfg = resolved
        model = (cfg.get("model") or "").strip()
    provider = cfg.get("provider", "")
    model = (model or cfg.get("model") or "").strip()
    api_key = cfg.get("api_key") or ""

    if provider != "ollama" and not api_key:
        raise CompletionError("No AI provider configured. Add an API key in Settings > AI.")
    if not model:
        raise CompletionError("No model configured for the assistant.")

    tried: set[int] = set()
    attempt = max(1, min(max_tokens, HARD_MAX_TOKENS))
    while True:
        tried.add(attempt)
        try:
            return await _dispatch(provider, cfg, system, user, model, api_key, attempt)
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response is not None else ""
            status = e.response.status_code if e.response is not None else 0
            if status == 400 and "max_tokens" in body.lower() and "large" in body.lower():
                ceiling = _parse_supported_max(body)
                nxt = ceiling if ceiling and ceiling < attempt else next(
                    (f for f in _FALLBACK_FLOORS if f < attempt and f not in tried), None
                )
                if nxt and nxt not in tried:
                    attempt = nxt
                    continue
            # Do NOT forward the provider's raw error body to the worker: it is an
            # information path from the provider into the sandbox (a misbehaving
            # endpoint could echo the Authorization header). Log it host-side only.
            logger.warning("provider HTTP %s on completion: %s", status, body[:500])
            raise CompletionError(f"Provider returned HTTP {status}") from e
        except httpx.TimeoutException as e:
            raise CompletionError(f"Provider timed out after {TIMEOUT}s") from e


async def _dispatch(provider, cfg, system, user, model, api_key, max_tokens) -> str:
    if provider == "anthropic":
        return await _anthropic(system, user, model, api_key, max_tokens)
    if provider == "ollama":
        return await _ollama(system, user, model, cfg.get("ollama_url", ""), max_tokens)
    if provider == "openai":
        return await _openai_compat(
            system, user, model, api_key, max_tokens, "https://api.openai.com/v1/chat/completions")
    if provider in OPENAI_COMPAT_PROVIDERS:
        spec = OPENAI_COMPAT_PROVIDERS[provider]
        if provider == "custom":
            base = (cfg.get("custom_base_url") or _custom_base_url()).strip().rstrip("/")
            if not base:
                raise CompletionError("Custom provider selected but no base URL is configured.")
            url = f"{base}/chat/completions"
        else:
            url = spec["chat_url"]
        return await _openai_compat(system, user, model, api_key, max_tokens, url)
    # OpenRouter is the default / fallback.
    return await _openai_compat(
        system, user, model, api_key, max_tokens,
        "https://openrouter.ai/api/v1/chat/completions",
        extra_headers={
            "HTTP-Referer": "https://github.com/Mfrostbutter/ageniusdesk-ce",
            "X-Title": "AgeniusDesk Module Bridge",
        },
    )


async def _openai_compat(system, user, model, api_key, max_tokens, url, extra_headers=None) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }  # NOTE: no "tools" key, ever — tool-free by construction.
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise CompletionError("Provider returned an empty completion.")
    return content


async def _anthropic(system, user, model, api_key, max_tokens) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "temperature": 0.4,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text:
        raise CompletionError("Anthropic returned an empty completion.")
    return text


async def _ollama(system, user, model, ollama_url, max_tokens) -> str:
    if not ollama_url:
        raise CompletionError("Ollama selected but no Ollama URL is configured.")
    url = f"{ollama_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = data.get("message", {}).get("content", "")
    if not content:
        raise CompletionError("Ollama returned an empty completion.")
    return content
