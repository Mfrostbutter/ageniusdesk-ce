"""LLM providers — OpenRouter, OpenAI, Anthropic, Ollama."""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx

from backend.config import decrypt_value, load_config

logger = logging.getLogger(__name__)


def _ollama_default() -> str:
    """Default Ollama base URL. Reads OLLAMA_URL env first so operators can
    point the dashboard at a host-reachable Ollama (e.g. host.docker.internal
    on Docker Desktop, or a LAN IP for the host). In a Docker container the bare
    `localhost:11434` default is the container itself and almost never useful.
    """
    return os.environ.get("OLLAMA_URL", "http://localhost:11434")

TIMEOUT = 60.0

# Cap on assistant tool-call iterations per chat turn. Most real tasks finish
# in 2-4 rounds; "Ask AI" on workflow errors occasionally pulls execution +
# workflow + node detail which pushes into 5-7. 10 gives headroom without
# letting a stuck-in-a-loop model run wild. When the cap is hit we force one
# final tool-less summary round so the user still gets a useful response.
MAX_TOOL_ROUNDS = 10

# In-memory cache for live model listings. Keyed by provider name. Cached for
# 5 minutes to avoid hammering the provider API on every dropdown open, while
# still letting newly-released models show up within a reasonable window.
# Shape: {provider: {"at": float, "models": list[dict], "key_fp": str}}
_MODEL_CACHE: dict[str, dict] = {}
_MODEL_CACHE_TTL = 300.0  # 5 minutes

# ── Model Lists ─────────────────────────────────────────────────────────────

OPENROUTER_MODELS = [
    {"id": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4", "provider": "Anthropic"},
    {"id": "anthropic/claude-haiku-4", "name": "Claude Haiku 4", "provider": "Anthropic"},
    {"id": "openai/gpt-4o", "name": "GPT-4o", "provider": "OpenAI"},
    {"id": "openai/gpt-4o-mini", "name": "GPT-4o Mini", "provider": "OpenAI"},
    {"id": "google/gemini-2.5-flash-preview", "name": "Gemini 2.5 Flash", "provider": "Google"},
    {"id": "google/gemini-2.5-pro-preview", "name": "Gemini 2.5 Pro", "provider": "Google"},
    {"id": "meta-llama/llama-4-maverick", "name": "Llama 4 Maverick", "provider": "Meta"},
    {"id": "meta-llama/llama-4-scout", "name": "Llama 4 Scout", "provider": "Meta"},
    {"id": "mistralai/mistral-medium-3", "name": "Mistral Medium 3", "provider": "Mistral"},
    {"id": "deepseek/deepseek-chat-v3-0324", "name": "DeepSeek V3", "provider": "DeepSeek"},
    {"id": "qwen/qwen-2.5-72b-instruct", "name": "Qwen 2.5 72B", "provider": "Qwen"},
]

OPENAI_MODELS = [
    {"id": "gpt-4o", "name": "GPT-4o", "provider": "OpenAI"},
    {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "provider": "OpenAI"},
    {"id": "gpt-4.1", "name": "GPT-4.1", "provider": "OpenAI"},
    {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "provider": "OpenAI"},
    {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano", "provider": "OpenAI"},
    {"id": "o3-mini", "name": "o3-mini", "provider": "OpenAI"},
]

# Hardcoded fallback list. These are only surfaced when the provider's live
# /v1/models endpoint is unreachable OR no API key is configured. We prefer
# date-suffixed IDs that Anthropic has actually published, but the live fetch
# is what should be used in practice; update this list sparingly.
ANTHROPIC_MODELS = [
    {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5", "provider": "Anthropic"},
    {"id": "claude-opus-4-5", "name": "Claude Opus 4.5", "provider": "Anthropic"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "provider": "Anthropic"},
    {"id": "claude-3-7-sonnet-latest", "name": "Claude Sonnet 3.7", "provider": "Anthropic"},
    {"id": "claude-3-5-haiku-latest", "name": "Claude Haiku 3.5", "provider": "Anthropic"},
]

# Map provider to default model
PROVIDER_DEFAULTS = {
    "openrouter": "anthropic/claude-sonnet-4",
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
    "ollama": "llama3",
}


def get_assistant_config() -> dict:
    """Get assistant config from saved config, resolving $VAR references."""
    config = load_config()
    ai = config.get("assistant", {})
    provider = ai.get("provider", "openrouter")
    return {
        "provider": provider,
        "api_key": decrypt_value(ai.get("api_key", "")),
        "model": ai.get("model", PROVIDER_DEFAULTS.get(provider, "anthropic/claude-sonnet-4")),
        "ollama_url": ai.get("ollama_url") or _ollama_default(),
        "qdrant_url": decrypt_value(ai.get("qdrant_url", "")),
        "qdrant_collection": ai.get("qdrant_collection", ""),
        "system_prompt": ai.get("system_prompt", ""),
        "codelab_instructions": ai.get("codelab_instructions", ""),
        "fallback_provider": ai.get("fallback_provider", ""),
        "fallback_model": ai.get("fallback_model", ""),
        "fallback_api_key": ai.get("fallback_api_key", ""),
    }


PROVIDER_KEY_MAP = {
    "anthropic": "ANTHROPIC_KEY",
    "openai": "OPEN_AI_KEY",
    "openrouter": "OPEN_ROUTER_KEY",
}

# ── Per-job (per-area) configuration ────────────────────────────────────────
# Three self-contained agents. Each has its own provider, model, instructions,
# and optional fallback. There is no global default and no instruction layering
# between jobs; the only shared baseline is the Harness AGENTS.md constitution,
# which is prepended to every job (the Harness is left intact).
JOB_SURFACES = ("codelab", "triage", "assistant")
JOB_LABELS = {"codelab": "Code Lab", "triage": "Error Triage", "assistant": "General Assistant"}
DEFAULT_JOB = "assistant"


def default_triage_instructions() -> str:
    return (
        "You are an n8n error-triage assistant inside AgeniusDesk. You are given a "
        "workflow error: the workflow name, the failing node, the error type, the "
        "error message, and (when available) the execution id.\n\n"
        "Explain the most likely root cause in plain language, then give specific, "
        "actionable fixes. Prefer concrete n8n steps (node settings, expressions, "
        "credentials, data shape) over generic advice. Be concise and lead with the "
        "fix. If the message is ambiguous, state the top one or two hypotheses and "
        "how to confirm each."
    )


def default_assistant_instructions() -> str:
    return _default_system_prompt()


def _default_instructions_for(surface: str) -> str:
    if surface == "codelab":
        return default_codelab_instructions()
    if surface == "triage":
        return default_triage_instructions()
    return default_assistant_instructions()


def build_jobs(ai: dict) -> dict:
    """Return the three job configs, migrating legacy global fields on read.

    Idempotent and non-destructive: reads ai['jobs'] if present and backfills
    any missing field from the legacy global config (provider/model/system_prompt/
    codelab_instructions/surface_models/fallback). Does not persist; callers that
    save do so explicitly via the /jobs endpoint.
    """
    jobs = ai.get("jobs") if isinstance(ai.get("jobs"), dict) else {}
    legacy_sm = ai.get("surface_models", {}) or {}
    g_provider = ai.get("provider", "openrouter")
    g_model = ai.get("model", "") or PROVIDER_DEFAULTS.get(g_provider, "")
    out: dict = {}
    for s in JOB_SURFACES:
        j = dict(jobs.get(s) or {})
        sm = legacy_sm.get(s) or {}
        j["provider"] = j.get("provider") or sm.get("provider") or g_provider
        j["model"] = j.get("model") or sm.get("model") or g_model
        if not j.get("instructions"):
            if s == "codelab":
                j["instructions"] = ai.get("codelab_instructions") or default_codelab_instructions()
            elif s == "assistant":
                j["instructions"] = ai.get("system_prompt") or default_assistant_instructions()
            else:
                j["instructions"] = default_triage_instructions()
        # Fallback: the legacy global fallback seeds the assistant job only.
        if "fallback_provider" not in j:
            j["fallback_provider"] = ai.get("fallback_provider", "") if s == "assistant" else ""
        if "fallback_model" not in j:
            j["fallback_model"] = ai.get("fallback_model", "") if s == "assistant" else ""
        out[s] = j
    return out


def get_job_config(surface: str) -> dict:
    """Resolve one job's config by surface. Unknown/empty surface -> assistant."""
    ai = load_config().get("assistant", {})
    jobs = build_jobs(ai)
    return jobs[surface if surface in JOB_SURFACES else DEFAULT_JOB]


def provider_key_status() -> dict:
    """Which providers have a usable key (secrets store convention, or the legacy
    global key for the saved global provider). Ollama needs no key."""
    ai = load_config().get("assistant", {})
    legacy_provider = ai.get("provider", "")
    legacy_key = bool(decrypt_value(ai.get("api_key", "")))
    status = {"ollama": True}
    for prov, name in PROVIDER_KEY_MAP.items():
        resolved = decrypt_value(f"${name}")
        has = bool(resolved) and resolved != name
        if not has and prov == legacy_provider and legacy_key:
            has = True
        status[prov] = has
    return status


def _resolve_override(cfg: dict, override: dict | None) -> dict | str:
    """Apply an {provider, model, api_key_ref} override to a cfg dict.

    Key resolution is deterministic for non-ollama providers, regardless of
    whether the provider changed:
      1. If `api_key_ref` is given (a $NAME chosen in the Models area), resolve
         exactly that secret.
      2. Otherwise resolve the per-provider convention secret
         ($ANTHROPIC_KEY / $OPEN_AI_KEY / $OPEN_ROUTER_KEY).
      3. Otherwise, if the provider matches the saved global provider and a
         legacy global key exists, use that.

    Returns the new cfg on success, or an error message string if the override
    can't be applied (unknown provider, missing/empty secret). Does not mutate
    the caller's dict.
    """
    cfg = dict(cfg)
    if not override:
        return cfg
    prev_provider = cfg.get("provider")
    ov_provider = override.get("provider") or prev_provider
    ov_model = override.get("model")
    ov_key_ref = (override.get("api_key_ref") or "").strip()

    if ov_provider == "ollama":
        cfg["provider"] = "ollama"
        cfg["api_key"] = ""
    else:
        if ov_key_ref:
            # Explicit secret chosen for this area. Resolve exactly it.
            ref = ov_key_ref if ov_key_ref.startswith("$") else f"${ov_key_ref}"
            bare = ref[1:]
            resolved = decrypt_value(ref)
            # decrypt_value returns the bare name when the $VAR is not found.
            if not resolved or resolved == bare:
                return (
                    f"The selected key {ref} is empty or not found. "
                    f"Pick a different secret for this area in Models."
                )
        else:
            name = PROVIDER_KEY_MAP.get(ov_provider)
            if not name:
                return f"Unknown provider: {ov_provider}"
            resolved = decrypt_value(f"${name}")
            if not resolved or resolved == name:
                # Fall back to the legacy global key when the provider matches.
                if ov_provider == prev_provider and cfg.get("api_key"):
                    resolved = cfg["api_key"]
                else:
                    return (
                        f"{ov_provider} is not configured. Add ${name} in "
                        f"Settings > Secrets, or pick a saved key for this area in Models."
                    )
        cfg["provider"] = ov_provider
        cfg["api_key"] = resolved

    if ov_model:
        cfg["model"] = ov_model
    elif ov_provider != prev_provider:
        cfg["model"] = PROVIDER_DEFAULTS.get(ov_provider, cfg["model"])

    return cfg


def _redact_key_from_reason(reason: str) -> str:
    """Remove any bearer token / API key patterns from an error reason string.

    We match common API key shapes (sk-..., long alphanumeric blobs after
    'Bearer' or 'key'). The goal is belt-and-suspenders: the _chat_* functions
    never include the key in the error string, but this runs as a final guard
    before the string is stored in the messages table or broadcast over WebSocket.
    """
    # Remove Bearer token values.
    reason = re.sub(r"Bearer\s+[A-Za-z0-9\-_./+=]{8,}", "Bearer [REDACTED]", reason)
    # Remove OpenAI/Anthropic-style secret key patterns (sk-... / sk-ant-...).
    reason = re.sub(r"\bsk-[A-Za-z0-9\-_]{8,}", "[REDACTED]", reason)
    # Remove long alphanumeric tokens that look like API keys (32+ hex/base64 chars).
    reason = re.sub(r"\b[A-Za-z0-9]{32,}\b", "[REDACTED]", reason)
    # Truncate to a safe display length.
    return reason[:200]


def _is_transient_result(result: dict) -> bool:
    """Return True when the error in a _dispatch_chat result is worth retrying.

    Transient = provider-side 5xx, HTTP 429, httpx timeout, or a response body
    containing the phrases 'rate limit' or 'overloaded' (case-insensitive).
    Fatal errors (4xx that are not 429, malformed responses, config errors) are
    NOT transient and should surface immediately to the caller.

    Relies on the `_transient` bool set by each _chat_* function on the error
    dict. Falls back to string-matching the `error` key for any error paths
    that do not set the flag.
    """
    if not result.get("error"):
        return False
    if "_transient" in result:
        return bool(result["_transient"])
    # String-match fallback for any path that didn't set _transient.
    msg = str(result.get("error", "")).lower()
    return "rate limit" in msg or "overloaded" in msg


async def _dispatch_chat(messages: list[dict], system: str, cfg: dict) -> dict[str, Any]:
    """Route to the right provider backend based on cfg['provider'] and model.

    For OpenAI-family providers (openai, openrouter), the model name decides
    whether to use /v1/chat/completions (default) or /v1/responses (codex,
    o1/o3/o4, gpt-5* per _api_surface_for).
    """
    provider = cfg["provider"]
    if provider != "ollama" and not cfg["api_key"]:
        return {"error": "AI assistant not configured. Add an API key in Settings."}

    if provider == "ollama":
        return await _chat_ollama(messages, system, cfg)
    elif provider == "anthropic":
        return await _chat_anthropic(messages, system, cfg)
    elif provider == "openai":
        surface = _api_surface_for(cfg.get("model", ""))
        if surface == "responses":
            return await _chat_openai_responses(
                messages, system, cfg,
                base_url="https://api.openai.com/v1/responses",
                provider_name="openai",
            )
        if surface == "embeddings":
            return {"error": "Embedding models cannot be used as chat models. Pick a chat or codex model."}
        return await _chat_openai_compat(
            messages, system, cfg,
            base_url="https://api.openai.com/v1/chat/completions",
            provider_name="openai",
        )
    else:
        # OpenRouter — default
        surface = _api_surface_for(cfg.get("model", ""))
        if surface == "embeddings":
            return {"error": "Embedding models cannot be used as chat models. Pick a chat or codex model."}
        # OpenRouter routes most providers (including codex models proxied
        # through it) via chat-completions; no separate /v1/responses endpoint.
        return await _chat_openai_compat(
            messages, system, cfg,
            base_url="https://openrouter.ai/api/v1/chat/completions",
            provider_name="openrouter",
            extra_headers={
                "HTTP-Referer": "https://github.com/Mfrostbutter/ageniusdesk-ce",
                "X-Title": "AgeniusDesk",
            },
        )


async def chat(
    messages: list[dict],
    context: str = "",
    model_override: str = "",
    override: dict | None = None,
    fallback: dict | None = None,
    surface: str = "",
    instructions: str | None = None,
) -> dict[str, Any]:
    """Send messages to the configured LLM. Returns {response, model, provider}.

    Overrides come in three flavors (for backward compat with Code Lab/Assistant):
      model_override: str   just swap the model id, keep provider + key.
      override: dict        full {provider, model} override. When a provider is
                            named we look up the conventional secret key for it
                            ($ANTHROPIC_KEY / $OPEN_AI_KEY / $OPEN_ROUTER_KEY). If
                            the secret is missing we return an error so the
                            caller can surface it and fall back.
      fallback: dict        optional {provider, model}. Used only if primary
                            returns an error. Resolved the same way as override.
      surface: str          identifies the calling UI (e.g. "codelab") so
                            surface-specific instructions can be layered. Empty = default.

    Response:
      - Normal: {response, model, provider, usage, served_by: "primary"}
      - After fallback retry: adds served_by: "fallback", primary_error: "..."
      - All failures: {error: "..."}. When fallback was attempted and also
        failed, the primary's error is returned (that is what the user asked
        for first).
    """
    base_cfg = get_assistant_config()
    if model_override:
        base_cfg["model"] = model_override

    # Resolve primary cfg.
    primary_resolved = _resolve_override(base_cfg, override)
    if isinstance(primary_resolved, str):
        # Override resolution itself failed. Don't attempt fallback because the
        # caller asked for a specific primary and we couldn't even set it up.
        return {"error": primary_resolved}

    primary_cfg = primary_resolved

    # C3: constitution prefix — operator-authored house rules injected before
    # per-agent text.  Fail-soft: any error returns "" so chat never breaks.
    try:
        from backend.modules.assistant.baseline import loader as _constitution
        _const_text = await _constitution.render(
            tenant_id=primary_cfg.get("tenant_id", "default"),
            per_agent_overrides=primary_cfg.get("agent_overrides"),
        )
    except Exception as _e:
        logger.debug("constitution render skipped: %s", _e)
        _const_text = ""

    # Build system prompt (shared across primary + fallback).
    # Composition order:
    #   1. Constitution body (Harness AGENTS.md house rules) — left intact.
    #   2. This job's own instructions (no cross-job layering).
    #   3. Environment, context, files, RAG (appended below)
    # `instructions` is the per-job system prompt passed by the router. When a
    # caller omits it (legacy path) we fall back to the saved global prompt.
    if instructions is not None:
        _per_agent = instructions
    else:
        _per_agent = primary_cfg.get("system_prompt") or _default_system_prompt()

    system = "\n\n".join(p for p in [_const_text, _per_agent] if p)

    # Baseline environment context runs on EVERY chat so the assistant has
    # grounded facts about the user's setup regardless of per-request toggles.
    try:
        baseline = await _build_baseline_context()
    except Exception as e:
        logger.debug("baseline build raised (swallowed): %s", e)
        baseline = ""
    if baseline:
        system += f"\n\n## Environment\n{baseline}"

    if context:
        system += f"\n\n## Requested Context\n{context}"

    # Inject knowledge files
    try:
        from pathlib import Path
        files_dir = Path("data/assistant_files")
        if files_dir.exists():
            file_blocks = []
            for f in sorted(files_dir.iterdir()):
                if f.is_file() and f.stat().st_size > 0:
                    try:
                        content = f.read_text(
                            encoding="utf-8", errors="replace"
                        )[:8000]
                        file_blocks.append(
                            f"### {f.name}\n```\n{content}\n```"
                        )
                    except Exception:
                        pass
            if file_blocks:
                system += "\n\n## Knowledge Files\n\n" + "\n\n".join(file_blocks)
    except Exception as e:
        logger.debug("File context skipped: %s", e)

    # Enrich with RAG if configured
    try:
        from backend.modules.assistant.rag import build_rag_context
        last_user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "",
        )
        if last_user_msg:
            rag_context = await build_rag_context(last_user_msg)
            if rag_context:
                system += f"\n\n{rag_context}"
    except Exception as e:
        logger.debug("RAG enrichment skipped: %s", e)

    # Attempt primary.
    primary_result = await _dispatch_chat(messages, system, primary_cfg)
    if "error" not in primary_result:
        primary_result["served_by"] = "primary"
        return primary_result

    primary_error = primary_result.get("error", "")
    logger.info("Primary LLM failed: %s", primary_error)

    # Only attempt fallback for transient errors (5xx, 429, timeout, rate-limit
    # / overloaded body text). Fatal errors (401, 403, 400, bad config, etc.)
    # are returned immediately — falling back on an auth error wastes a call and
    # confuses the user.
    if not _is_transient_result(primary_result):
        logger.info("Primary error is not transient; skipping fallback")
        # Strip internal key before returning.
        primary_result.pop("_transient", None)
        return primary_result

    # No fallback requested, or fallback has no provider -> return primary error.
    fallback_provider = (fallback or {}).get("provider") if fallback else None
    if not fallback_provider:
        primary_result.pop("_transient", None)
        return primary_result

    # Resolve fallback cfg from a fresh base config (not primary_cfg, to avoid
    # leaking primary's api_key into a different provider).
    fb_base = get_assistant_config()
    fb_resolved = _resolve_override(fb_base, fallback)
    if isinstance(fb_resolved, str):
        logger.info("Fallback resolution failed: %s", fb_resolved)
        primary_result.pop("_transient", None)
        return primary_result
    # If a dedicated fallback key is stored, it takes precedence over the
    # convention key that _resolve_override looked up.
    _raw_fb_key = fb_base.get("fallback_api_key", "")
    if _raw_fb_key:
        fb_resolved["api_key"] = decrypt_value(_raw_fb_key)

    fallback_model_name = (fallback or {}).get("model") or fb_resolved.get("model", "")
    fb_result = await _dispatch_chat(messages, system, fb_resolved)
    if "error" in fb_result:
        logger.info("Fallback LLM also failed: %s", fb_result.get("error"))
        fb_result.pop("_transient", None)
        primary_result.pop("_transient", None)
        # Per spec, return primary's error — that's what the user asked for.
        return primary_result

    # Fallback succeeded. Broadcast a warning toast so the user knows which
    # model actually served the response. Reason is truncated and never contains
    # raw API keys (the error string from _chat_* never includes key values).
    _safe_reason = _redact_key_from_reason(primary_error)
    try:
        from backend.modules.messages.collector import store_message
        await store_message({
            "title": "Fallback model used",
            "body": f"Primary model failed ({_safe_reason}), used fallback {fallback_model_name}.",
            "level": "warning",
            "source": "assistant",
        })
    except Exception as _te:
        logger.debug("Failed to broadcast fallback toast: %s", _te)

    fb_result.pop("_transient", None)
    fb_result["served_by"] = "fallback"
    fb_result["primary_error"] = _safe_reason
    return fb_result


# ── OpenAI-Compatible (OpenRouter + OpenAI direct) ─────────────────────────


def _extract_message_text(message: dict) -> str:
    """Pull the assistant's text out of a chat-completions message.

    `message.get("content", "")` is not enough: the API sends `content: null`
    for some responses (reasoning models, or a turn that only emitted tool
    calls), and a missing-key default does not cover an explicit null. Content
    can also arrive as a list of typed parts. When content is empty, some
    OpenRouter-proxied reasoning models leave the answer in a `reasoning`
    field instead. Normalize all of these to a plain string.
    """
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            elif isinstance(p, str):
                parts.append(p)
        content = "".join(parts)
    if isinstance(content, str) and content.strip():
        return content
    for key in ("reasoning", "reasoning_content"):
        alt = message.get(key)
        if isinstance(alt, str) and alt.strip():
            return alt
    return ""


async def _chat_openai_compat(messages: list[dict], system: str, cfg: dict,
                              base_url: str, provider_name: str,
                              extra_headers: dict | None = None) -> dict[str, Any]:
    """Chat via OpenAI-compatible API with tool use support."""
    from backend.config import get_active_instance_id
    from backend.modules.assistant.mcp_client import execute_tool as mcp_execute
    from backend.modules.assistant.mcp_client import get_all_mcp_tools
    from backend.modules.assistant.tools import TOOL_DEFINITIONS, execute_tool

    active_instance_id = get_active_instance_id()
    mcp_tools, mcp_tool_map = await get_all_mcp_tools(instance_id=active_instance_id)
    all_tools = TOOL_DEFINITIONS + mcp_tools

    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    all_messages = [{"role": "system", "content": system}] + messages
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    for _round in range(MAX_TOOL_ROUNDS):
        payload = {
            "model": cfg["model"],
            "messages": all_messages,
            "max_tokens": 4096,
            "temperature": 0.7,
            "tools": all_tools,
        }

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(base_url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

                usage = data.get("usage", {})
                total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
                total_usage["completion_tokens"] += usage.get("completion_tokens", 0)

                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                finish_reason = choice.get("finish_reason", "")

                tool_calls = message.get("tool_calls")
                if tool_calls and finish_reason in ("tool_calls", "stop"):
                    all_messages.append(message)

                    for tc in tool_calls:
                        func = tc.get("function", {})
                        tool_name = func.get("name", "")
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}

                        logger.info("Tool call: %s(%s)", tool_name, json.dumps(args)[:100])

                        if tool_name in mcp_tool_map:
                            server_id, real_name = mcp_tool_map[tool_name]
                            result = await mcp_execute(server_id, real_name, args)
                        else:
                            result = await execute_tool(tool_name, args)

                        all_messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                    continue

                content = _extract_message_text(message)
                if not content:
                    # Empty final answer (often finish_reason "length" on a
                    # token-hungry workflow JSON). Surface it rather than a
                    # silent blank that the UI renders as "No response".
                    if finish_reason == "length":
                        content = (
                            "(The model hit its output token limit before finishing. "
                            "Try a more specific request or a model with a larger output budget.)"
                        )
                    else:
                        content = "(The model returned an empty response. Try again or pick a different model.)"
                return {
                    "response": content,
                    "model": cfg["model"],
                    "provider": provider_name,
                    "usage": {
                        "input_tokens": total_usage["prompt_tokens"],
                        "output_tokens": total_usage["completion_tokens"],
                    },
                }

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            body = e.response.text[:300]
            logger.error("%s error: HTTP %s: %s", provider_name, status, body)
            # Transient: 5xx, 429. Fatal: other 4xx (auth, bad request, etc.).
            body_lower = body.lower()
            transient = (
                status >= 500
                or status == 429
                or "rate limit" in body_lower
                or "overloaded" in body_lower
            )
            return {
                "error": f"LLM request failed (HTTP {status}): {body}",
                "_transient": transient,
            }
        except httpx.TimeoutException as e:
            logger.error("%s timeout: %s", provider_name, e)
            return {"error": f"LLM request timed out: {e}", "_transient": True}
        except Exception as e:
            logger.error("%s error: %s", provider_name, e)
            return {"error": f"LLM request failed: {e}", "_transient": False}

    # Cap hit — force one more round with tools disabled so the model has to
    # produce a final text answer from whatever it already gathered.
    logger.warning(
        "%s tool-call loop hit %d rounds; forcing tool-less summary",
        provider_name, MAX_TOOL_ROUNDS,
    )
    all_messages.append({
        "role": "user",
        "content": (
            "You've used your allotted tool calls. Based on what you've gathered, "
            "please give a final answer to my question now, without calling more tools."
        ),
    })
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(base_url, headers=headers, json={
                "model": cfg["model"],
                "messages": all_messages,
                "max_tokens": 4096,
                "temperature": 0.7,
            })
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            content = _extract_message_text(data.get("choices", [{}])[0].get("message", {}))
            return {
                "response": content or "(The assistant exhausted its tool-call budget without a final answer.)",
                "model": cfg["model"],
                "provider": provider_name,
                "usage": {
                    "input_tokens": total_usage["prompt_tokens"],
                    "output_tokens": total_usage["completion_tokens"],
                },
                "truncated": True,
            }
    except Exception as e:
        logger.error("%s forced-summary error: %s", provider_name, e)
        return {"error": f"Reached {MAX_TOOL_ROUNDS}-round tool limit and summary retry failed: {e}"}


# ── OpenAI Responses API (codex, o1/o3/o4, gpt-5* models) ────────────────────


async def _chat_openai_responses(messages: list[dict], system: str, cfg: dict,
                                  base_url: str = "https://api.openai.com/v1/responses",
                                  provider_name: str = "openai",
                                  extra_headers: dict | None = None) -> dict[str, Any]:
    """Chat via OpenAI Responses API for codex / reasoning / gpt-5* models.

    These models 404 on /v1/chat/completions and require /v1/responses with a
    different request shape (input array instead of messages, max_output_tokens
    instead of max_tokens, output array instead of choices).

    Tool use in the Responses API has a distinct shape from chat-completions
    tools; v1 of this path ships text-only chat. Tool-equipped agent flows
    should select chat-completions-compatible models.
    """
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    # Responses API takes "input" — a string for single-turn or an array of
    # role/content items for multi-turn. We pass the messages array intact;
    # the API accepts the same role/content shape that chat-completions uses.
    input_messages: list[dict] = []
    if system:
        input_messages.append({"role": "system", "content": system})
    input_messages.extend(messages)

    payload = {
        "model": cfg["model"],
        "input": input_messages,
        "max_output_tokens": 2048,
    }
    # Responses API rejects temperature on some reasoning models (o1/o3) but
    # accepts it on gpt-5*. Omit by default; chat-completions handles temperature.

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(base_url, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.error("%s /responses HTTP %s: %s", provider_name, resp.status_code, resp.text[:300])
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()
    except httpx.RequestError as e:
        logger.error("%s /responses request error: %s", provider_name, e)
        return {"error": f"Network error: {e}"}

    # Responses API returns {output: [{type: "message", role: "assistant",
    # content: [{type: "output_text", text: "..."}]}], usage: {input_tokens, output_tokens}}
    output = data.get("output", [])
    response_text = ""
    for item in output:
        if item.get("type") == "message" and item.get("role") == "assistant":
            for content_part in item.get("content", []):
                if content_part.get("type") in ("output_text", "text"):
                    response_text += content_part.get("text", "")

    if not response_text:
        # Fallback: some Responses API variants return an "output_text" top-level shortcut.
        response_text = data.get("output_text", "")

    usage = data.get("usage", {}) or {}
    return {
        "response": response_text or "(empty response)",
        "model": cfg["model"],
        "provider": provider_name,
        "api_surface": "responses",
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    }


# ── Anthropic Messages API ──────────────────────────────────────────────────


async def _chat_anthropic(messages: list[dict], system: str, cfg: dict) -> dict[str, Any]:
    """Chat via Anthropic Messages API with tool use support."""
    from backend.config import get_active_instance_id
    from backend.modules.assistant.mcp_client import execute_tool as mcp_execute
    from backend.modules.assistant.mcp_client import get_all_mcp_tools
    from backend.modules.assistant.tools import TOOL_DEFINITIONS, execute_tool

    active_instance_id = get_active_instance_id()
    mcp_tools, mcp_tool_map = await get_all_mcp_tools(instance_id=active_instance_id)

    # Convert OpenAI tool format to Anthropic tool format
    anthropic_tools = []
    for t in TOOL_DEFINITIONS + mcp_tools:
        func = t["function"]
        anthropic_tools.append({
            "name": func["name"],
            "description": func["description"],
            "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
        })

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    # Anthropic expects messages without system role — system is a top-level param
    api_messages = [m for m in messages if m["role"] != "system"]

    total_usage = {"input_tokens": 0, "output_tokens": 0}

    for _round in range(MAX_TOOL_ROUNDS):
        payload = {
            "model": cfg["model"],
            "max_tokens": 2048,
            "system": system,
            "messages": api_messages,
        }
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

                usage = data.get("usage", {})
                total_usage["input_tokens"] += usage.get("input_tokens", 0)
                total_usage["output_tokens"] += usage.get("output_tokens", 0)

                stop_reason = data.get("stop_reason", "")
                content_blocks = data.get("content", [])

                # Check for tool use
                tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
                text_blocks = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
                text_response = "\n".join(text_blocks)

                if tool_uses and stop_reason == "tool_use":
                    # Add assistant message with all content blocks
                    api_messages.append({"role": "assistant", "content": content_blocks})

                    # Execute tools and build tool_result blocks
                    tool_results = []
                    for tu in tool_uses:
                        tool_name = tu.get("name", "")
                        args = tu.get("input", {})
                        tool_use_id = tu.get("id", "")

                        logger.info("Tool call: %s(%s)", tool_name, json.dumps(args)[:100])

                        if tool_name in mcp_tool_map:
                            server_id, real_name = mcp_tool_map[tool_name]
                            result = await mcp_execute(server_id, real_name, args)
                        else:
                            result = await execute_tool(tool_name, args)

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": result,
                        })

                    api_messages.append({"role": "user", "content": tool_results})
                    continue

                # No tool use — return text response
                return {
                    "response": text_response,
                    "model": cfg["model"],
                    "provider": "anthropic",
                    "usage": total_usage,
                }

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            body = e.response.text[:300]
            logger.error("Anthropic error: HTTP %s: %s", status, body)
            body_lower = body.lower()
            transient = (
                status >= 500
                or status == 429
                or "rate limit" in body_lower
                or "overloaded" in body_lower
            )
            return {
                "error": f"LLM request failed (HTTP {status}): {body}",
                "_transient": transient,
            }
        except httpx.TimeoutException as e:
            logger.error("Anthropic timeout: %s", e)
            return {"error": f"LLM request timed out: {e}", "_transient": True}
        except Exception as e:
            logger.error("Anthropic error: %s", e)
            return {"error": f"LLM request failed: {e}", "_transient": False}

    # Cap hit — force one tool-less round so the model has to answer with
    # whatever it has gathered. Anthropic's tool-free API accepts the same
    # message history; we just drop the tools param.
    logger.warning(
        "Anthropic tool-call loop hit %d rounds; forcing tool-less summary",
        MAX_TOOL_ROUNDS,
    )
    api_messages.append({
        "role": "user",
        "content": (
            "You've used your allotted tool calls. Based on what you've gathered, "
            "please give a final answer to my question now, without calling more tools."
        ),
    })
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json={
                "model": cfg["model"],
                "max_tokens": 2048,
                "system": system,
                "messages": api_messages,
            })
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            total_usage["input_tokens"] += usage.get("input_tokens", 0)
            total_usage["output_tokens"] += usage.get("output_tokens", 0)
            text = "\n".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            )
            return {
                "response": text or "(The assistant exhausted its tool-call budget without a final answer.)",
                "model": cfg["model"],
                "provider": "anthropic",
                "usage": total_usage,
                "truncated": True,
            }
    except Exception as e:
        logger.error("Anthropic forced-summary error: %s", e)
        return {"error": f"Reached {MAX_TOOL_ROUNDS}-round tool limit and summary retry failed: {e}"}


# ── Ollama ──────────────────────────────────────────────────────────────────


async def _chat_ollama(messages: list[dict], system: str, cfg: dict) -> dict[str, Any]:
    """Chat via local Ollama instance."""
    url = f"{cfg['ollama_url'].rstrip('/')}/api/chat"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            return {
                "response": content,
                "model": cfg["model"],
                "provider": "ollama",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        body = e.response.text[:300]
        logger.error("Ollama error: HTTP %s: %s", status, body)
        body_lower = body.lower()
        transient = status >= 500 or status == 429 or "overloaded" in body_lower
        return {"error": f"Ollama request failed (HTTP {status}): {body}", "_transient": transient}
    except httpx.TimeoutException as e:
        logger.error("Ollama timeout: %s", e)
        return {"error": f"Ollama request timed out: {e}", "_transient": True}
    except Exception as e:
        return {"error": f"Ollama request failed: {e}", "_transient": False}


async def list_ollama_models(ollama_url: str = "") -> list[dict]:
    """Fetch available models from an Ollama instance."""
    base = (ollama_url or _ollama_default()).rstrip("/")
    url = f"{base}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return [{"id": m["name"], "name": m["name"], "provider": "Ollama"} for m in data.get("models", [])]
    except Exception as e:
        logger.warning("Ollama tags fetch failed for %s: %s", url, e)
        return []


# ── Live model listing per provider ────────────────────────────────────────

def _resolve_provider_key(provider: str) -> str:
    """Resolve the conventional API key for a provider via secrets store.

    Returns the plaintext key, or "" if unavailable.
    """
    name = PROVIDER_KEY_MAP.get(provider)
    if not name:
        return ""
    resolved = decrypt_value(f"${name}")
    if not resolved or resolved == name:
        return ""
    return resolved


def _key_fingerprint(key: str) -> str:
    """Short fingerprint used as a cache-bust signal if the secret changes."""
    if not key:
        return ""
    return f"{len(key)}:{key[:2]}:{key[-2:]}"


async def _fetch_anthropic_models(api_key: str) -> list[dict]:
    """Call Anthropic /v1/models. Returns shaped list. Raises on any failure."""
    url = "https://api.anthropic.com/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    out = []
    for m in data.get("data", []):
        mid = m.get("id")
        if not mid:
            continue
        out.append({
            "id": mid,
            "name": m.get("display_name") or mid,
            "provider": "Anthropic",
        })
    # Sort newest first by created_at when present, otherwise by id desc.
    out.sort(key=lambda x: x["id"], reverse=True)
    return out


def _is_openai_chat_model(mid: str) -> bool:
    """Conservative filter to chat-completions-capable OpenAI models."""
    mid_l = mid.lower()
    # Exclusions — non-chat models we never want in the chat picker.
    excluded_substrings = (
        "embedding", "whisper", "tts", "dall-e", "davinci",
        "babbage", "ada", "moderation", "realtime", "audio", "image",
    )
    if any(s in mid_l for s in excluded_substrings):
        return False
    # Inclusions — chat-capable families.
    if mid_l.startswith(("gpt-", "o1-", "o3-", "o4-", "chatgpt-")):
        return True
    # Bare o1 / o3 / o4 slugs.
    if mid_l in ("o1", "o3", "o4"):
        return True
    return False


async def _fetch_openai_models(api_key: str) -> list[dict]:
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    out = []
    for m in data.get("data", []):
        mid = m.get("id")
        if not mid or not _is_openai_chat_model(mid):
            continue
        out.append({"id": mid, "name": mid, "provider": "OpenAI"})
    out.sort(key=lambda x: x["id"])
    return out


async def _fetch_openrouter_models(api_key: str) -> list[dict]:
    url = "https://openrouter.ai/api/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    out = []
    for m in data.get("data", []):
        mid = m.get("id")
        if not mid:
            continue
        # Derive a provider-ish label from the id prefix (anthropic/..., openai/...)
        prov = mid.split("/")[0].title() if "/" in mid else "OpenRouter"
        out.append({
            "id": mid,
            "name": m.get("name") or mid,
            "provider": prov,
        })
    # Sort alphabetically for a stable dropdown.
    out.sort(key=lambda x: x["name"].lower())
    return out


_FALLBACK_MAP = {
    "anthropic": ANTHROPIC_MODELS,
    "openai": OPENAI_MODELS,
    "openrouter": OPENROUTER_MODELS,
}

_LIVE_FETCHERS = {
    "anthropic": _fetch_anthropic_models,
    "openai": _fetch_openai_models,
    "openrouter": _fetch_openrouter_models,
}


async def list_provider_models(provider: str, api_key_ref: str = "") -> dict[str, Any]:
    """Return a shaped model list for a provider, preferring live data.

    Result: {"models": [...], "source": "live" | "cached" | "fallback", "cached_at": float?}

    - `api_key_ref` (a $NAME chosen for a Models area) takes precedence; the live
      models endpoint is called with that exact secret. When empty, the
      per-provider convention key is used.
    - Result is cached in-memory for 5 minutes, keyed per resolved key so two
      areas using different keys for the same provider don't evict each other.
    - On any error (no key, network, auth, timeout) we log WARNING and return
      the hardcoded fallback list. Never hard-errors.
    """
    p = (provider or "").lower()
    if p not in _LIVE_FETCHERS:
        # Providers without a live fetcher (e.g. something we haven't wired)
        return {"models": _FALLBACK_MAP.get(p, []), "source": "fallback"}

    if api_key_ref:
        ref = api_key_ref if api_key_ref.startswith("$") else f"${api_key_ref}"
        resolved = decrypt_value(ref)
        api_key = "" if (not resolved or resolved == ref[1:]) else resolved
    else:
        api_key = _resolve_provider_key(p)
    key_fp = _key_fingerprint(api_key)
    cache_key = f"{p}:{key_fp}"

    # Cache hit check (still within TTL for this exact key)
    cached = _MODEL_CACHE.get(cache_key)
    now = time.time()
    if cached and (now - cached["at"]) < _MODEL_CACHE_TTL:
        return {
            "models": cached["models"],
            "source": "cached",
            "cached_at": cached["at"],
        }

    if not api_key:
        # No key configured — return fallback list without trying.
        return {"models": _FALLBACK_MAP.get(p, []), "source": "fallback"}

    # Try live fetch.
    try:
        models = await _LIVE_FETCHERS[p](api_key)
        if not models:
            # Provider returned 200 but no models — treat as fallback.
            logger.warning("%s /v1/models returned empty list; using fallback", p)
            return {"models": _FALLBACK_MAP.get(p, []), "source": "fallback"}
        _MODEL_CACHE[cache_key] = {"at": now, "models": models}
        return {"models": models, "source": "live", "cached_at": now}
    except httpx.HTTPStatusError as e:
        body = e.response.text[:200] if e.response is not None else ""
        logger.warning(
            "%s /v1/models HTTP %s: %s — falling back to hardcoded list",
            p, e.response.status_code, body,
        )
    except Exception as e:
        logger.warning(
            "%s /v1/models fetch failed (%s) — falling back to hardcoded list",
            p, e,
        )
    return {"models": _FALLBACK_MAP.get(p, []), "source": "fallback"}


def _api_surface_for(model: str) -> str:
    """Heuristic: detect which OpenAI-compatible API a model belongs on.

    chat_completions (default) — POST /v1/chat/completions, messages-shaped
    responses                  — POST /v1/responses, input-shaped (codex line, o1, gpt-5.x)
    embeddings                 — POST /v1/embeddings, input-shaped (text-embedding-*)

    Codex models 404 on /v1/chat/completions; they need the
    Responses API endpoint instead.
    """
    m = (model or "").lower()
    if not m:
        return "chat_completions"
    if "embedding" in m:
        return "embeddings"
    # Responses-API-only models: codex line + reasoning models (o1, o3) + future
    # gpt-5 variants that use the Responses API exclusively.
    if "codex" in m or m.startswith(("o1", "o3", "o4")) or "gpt-5" in m:
        return "responses"
    return "chat_completions"


async def ping_provider(
    provider: str,
    api_key: str = "",
    model: str = "",
    ollama_url: str = "",
) -> dict[str, Any]:
    """Send a minimal request to the provider to verify creds + model.

    Returns {"ok": True, "model": "..."} or {"ok": False, "error": "..."}.
    Does not mutate saved config.

    For OpenAI-family providers (openai, openrouter), the request endpoint and
    payload shape are routed via _api_surface_for(model) to handle codex
    (Responses API) and embedding models correctly.
    """
    p = (provider or "").lower()
    try:
        if p == "ollama":
            base = (ollama_url or _ollama_default()).rstrip("/")
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(f"{base}/api/tags")
                    if r.status_code == 200:
                        return {"ok": True, "model": model or "llama3"}
                    return {"ok": False, "error": f"HTTP {r.status_code} at {base}: {r.text[:200]}"}
            except Exception as e:
                # Containers can't reach host `localhost`. Point the hint at the
                # common fixes so the UI can show something actionable.
                return {"ok": False, "error": f"Could not reach Ollama at {base}: {e}. Set OLLAMA_URL to host.docker.internal:11434 (Mac) or the host LAN IP."}

        if not api_key:
            return {"ok": False, "error": "API key required"}

        if p == "anthropic":
            # Use a 1-token completion to validate both key AND model.
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": model or "claude-sonnet-4-20250514",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(url, headers=headers, json=payload)
                if r.status_code == 200:
                    return {"ok": True, "model": payload["model"]}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

        if p == "openai":
            target_model = model or "gpt-4o-mini"
            surface = _api_surface_for(target_model)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            if surface == "responses":
                url = "https://api.openai.com/v1/responses"
                payload = {"model": target_model, "input": "hi", "max_output_tokens": 16}
            elif surface == "embeddings":
                url = "https://api.openai.com/v1/embeddings"
                payload = {"model": target_model, "input": "hi"}
            else:
                url = "https://api.openai.com/v1/chat/completions"
                payload = {
                    "model": target_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(url, headers=headers, json=payload)
                if r.status_code == 200:
                    return {"ok": True, "model": target_model, "api_surface": surface}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

        if p == "openrouter":
            target_model = model or "anthropic/claude-sonnet-4"
            surface = _api_surface_for(target_model)
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/Mfrostbutter/ageniusdesk-ce",
                "X-Title": "AgeniusDesk",
            }
            # OpenRouter normalizes most providers into chat-completions shape,
            # but exposes a /completions endpoint for legacy/codex use cases.
            if surface == "responses":
                # OpenRouter doesn't expose /v1/responses directly; fall back to
                # chat completions which works for most codex models on OpenRouter.
                payload = {
                    "model": target_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            elif surface == "embeddings":
                # OpenRouter passes embeddings through to providers; same path.
                url = "https://openrouter.ai/api/v1/embeddings"
                payload = {"model": target_model, "input": "hi"}
            else:
                payload = {
                    "model": target_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(url, headers=headers, json=payload)
                if r.status_code == 200:
                    return {"ok": True, "model": target_model, "api_surface": surface}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

        return {"ok": False, "error": f"Unknown provider: {provider}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def default_codelab_instructions() -> str:
    """Generic, customizable Code Lab instructions. Layered onto Code Lab chats
    when the operator has not set their own (Settings, AI Settings, Code Lab
    Instructions)."""
    return (
        "You are the AgeniusDesk Code Lab assistant. You help build automations for n8n.\n\n"
        "General:\n"
        "- Be concise and practical. Prefer complete, working examples over fragments.\n"
        "- Assume the user is building inside n8n unless they say otherwise.\n\n"
        "Writing n8n Code-node code:\n"
        "- Read input with $input.all(), $input.first(), or $json; reference other nodes via $node[\"Name\"].json.\n"
        "- Return an array of items shaped like [{ json: {...} }]; never return bare values.\n"
        "- Use built-in helpers ($now, $today via Luxon) and $env[\"VAR\"] for secrets. Keep it dependency-free.\n\n"
        "Building a whole workflow (Workflow Builder mode):\n"
        "- Output a complete, importable n8n workflow as JSON with top-level \"name\", \"nodes\" (array), "
        "\"connections\" (object), and \"settings\". Include a trigger node.\n"
        "- Use real node types and typeVersions; wire connections by node name.\n"
        "- If n8n node-knowledge or workflow-validation MCP tools are available, use them so the node "
        "types and parameters are valid.\n"
    )


def _default_system_prompt() -> str:
    return (
        "You are an AI assistant for AgeniusDesk, "
        "a monitoring and management tool for n8n workflow "
        "automation instances.\n\n"
        "You help users with:\n"
        "- Understanding workflow errors and suggesting fixes\n"
        "- n8n best practices and configuration\n"
        "- Debugging failed executions\n"
        "- Workflow design and optimization\n"
        "- General n8n questions\n\n"
        "IMPORTANT: You have tools to query the live n8n instance. "
        "When the user asks about errors, failing workflows, "
        "or recent activity:\n"
        "1. ALWAYS use `list_executions` to check for recent "
        "failed executions, do not rely solely on the context "
        "provided.\n"
        "2. Use `get_execution` to get details on specific "
        "failures.\n"
        "3. Use `get_recent_errors` to check the dashboard's "
        "error log.\n"
        "4. Use `list_workflows` to see active workflows "
        "when relevant.\n\n"
        "Do NOT say \"no recent errors\" without first calling "
        "these tools to verify. The context section may be "
        "incomplete.\n\n"
        "You may freely describe your environment context and "
        "available tools when the user asks about them. Do not "
        "say \"I do not have a system prompt\" or \"I do not have "
        "instructions.\" You do, and it is the text you are "
        "reading now. The Environment section below gives you "
        "grounded facts about the user's AgeniusDesk setup; feel "
        "free to share those facts when asked.\n\n"
        "Be concise and practical. When discussing errors, "
        "suggest specific fixes. When you don't know something, "
        "say so rather than guessing. Format responses with "
        "markdown."
    )


# ── Baseline environment context ────────────────────────────────────────────
#
# Runs on every chat call. Produces a short markdown block describing the
# user's environment so the assistant always has grounded context, even when
# the per-request Context toggles are off. Each section is best-effort; a
# failure in one piece must not break chat.


def _baseline_environment_line() -> str:
    """App + version line. Cheap, in-process."""
    return "AgeniusDesk dashboard, version 0.1.0"


def _baseline_instance_line() -> str:
    """Active n8n instance name + URL + color tag."""
    try:
        from backend.config import get_active_instance
        inst = get_active_instance()
        if not inst:
            return "No instance configured"
        name = inst.get("name", "unnamed")
        url = decrypt_value(inst.get("url", "")) or inst.get("url", "")
        color = inst.get("color", "")
        color_part = f" [color {color}]" if color else ""
        return f"{name} ({url}){color_part}"
    except Exception as e:
        logger.debug("baseline instance line failed: %s", e)
        return ""


async def _baseline_workflow_count() -> str:
    """Total workflow count for the active instance. Short timeout, best-effort."""
    try:
        from backend.modules.n8n_proxy import client as n8n_client

        async def _fetch():
            data = await n8n_client.list_workflows(limit=250)
            workflows = data.get("workflows", []) if isinstance(data, dict) else []
            active = sum(1 for w in workflows if w.get("active"))
            return f"{len(workflows)} workflows ({active} active)"

        return await asyncio.wait_for(_fetch(), timeout=2.5)
    except asyncio.TimeoutError:
        logger.debug("baseline workflow count timed out")
        return ""
    except Exception as e:
        logger.debug("baseline workflow count failed: %s", e)
        return ""


async def _baseline_recent_errors() -> str:
    """Count of errors in the last 24h + most-recent age. Best-effort."""
    try:
        from backend.database import get_db

        async def _fetch():
            db = await get_db()
            cur = await db.execute(
                "SELECT COUNT(*) AS c, MAX(occurred_at) AS latest "
                "FROM errors WHERE occurred_at >= datetime('now', '-1 day')"
            )
            row = await cur.fetchone()
            await cur.close()
            if not row:
                return "0 errors in last 24h"
            try:
                count = row["c"]
                latest = row["latest"]
            except (IndexError, KeyError):
                count, latest = row[0], row[1]
            if not count:
                return "0 errors in last 24h"
            if latest:
                # Compute a quick humanized delta. SQLite returns 'YYYY-MM-DD HH:MM:SS' UTC.
                from datetime import datetime, timezone
                try:
                    dt = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    delta = datetime.now(timezone.utc) - dt
                    secs = int(delta.total_seconds())
                    if secs < 60:
                        ago = f"{secs}s ago"
                    elif secs < 3600:
                        ago = f"{secs // 60} minutes ago"
                    else:
                        ago = f"{secs // 3600}h ago"
                    return f"{count} errors in last 24h, most recent: {ago}"
                except Exception:
                    return f"{count} errors in last 24h"
            return f"{count} errors in last 24h"

        return await asyncio.wait_for(_fetch(), timeout=1.5)
    except asyncio.TimeoutError:
        logger.debug("baseline recent errors timed out")
        return ""
    except Exception as e:
        logger.debug("baseline recent errors failed: %s", e)
        return ""


def _baseline_tools_line() -> str:
    try:
        from backend.modules.assistant.tools import TOOL_DEFINITIONS
        names = [
            t.get("function", {}).get("name", "")
            for t in TOOL_DEFINITIONS
        ]
        names = [n for n in names if n]
        if not names:
            return ""
        return ", ".join(names)
    except Exception as e:
        logger.debug("baseline tools line failed: %s", e)
        return ""


async def _build_baseline_context() -> str:
    """Assemble the baseline environment block.

    Best-effort across all sections. Total budget ~500ms; pieces that miss
    the budget are silently omitted. Returns a markdown-ready string (or ""
    on total failure), and never raises.
    """
    try:
        start = time.monotonic()

        # Synchronous pieces first (fast).
        env_line = _baseline_environment_line()
        instance_line = _baseline_instance_line()
        tools_line = _baseline_tools_line()

        # Async pieces run concurrently so the slowest bounds the total.
        async_remaining = max(0.05, 0.5 - (time.monotonic() - start))
        workflow_line = ""
        errors_line = ""
        try:
            workflow_line, errors_line = await asyncio.wait_for(
                asyncio.gather(
                    _baseline_workflow_count(),
                    _baseline_recent_errors(),
                    return_exceptions=False,
                ),
                timeout=async_remaining,
            )
        except asyncio.TimeoutError:
            logger.debug("baseline async sections exceeded budget; omitting")
        except Exception as e:
            logger.debug("baseline async gather failed: %s", e)

        lines: list[str] = []
        if env_line:
            lines.append(f"- Environment: {env_line}")
        if instance_line:
            lines.append(f"- Active instance: {instance_line}")
        if workflow_line:
            lines.append(f"- Workflows: {workflow_line}")
        if errors_line:
            lines.append(f"- Recent errors: {errors_line}")
        if tools_line:
            lines.append(f"- Available tools: {tools_line}")

        return "\n".join(lines)
    except Exception as e:
        logger.debug("baseline context assembly failed: %s", e)
        return ""
