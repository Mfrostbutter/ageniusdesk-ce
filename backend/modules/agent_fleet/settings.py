"""Fleet provider, key and per-agent model choices, saved in config.json.

Edited from Models › Agent Fleet. Empty provider means "follow the AI
assistant's provider"; an empty model means the agent's own default (or its
env override). The runner reads these on every run, so a save takes effect
without a restart.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from backend.config import load_config, save_config

CONFIG_KEY = "agent_fleet"
PROVIDERS = ("anthropic", "openai", "openrouter")
PROVIDER_LABELS = {"anthropic": "Anthropic", "openai": "OpenAI", "openrouter": "OpenRouter"}
_REF_RE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)?$")


def load() -> dict:
    raw = load_config().get(CONFIG_KEY) or {}
    provider = str(raw.get("provider") or "").strip().lower()
    models = raw.get("models") or {}
    return {
        "provider": provider if provider in PROVIDERS else "",
        "api_key_ref": str(raw.get("api_key_ref") or ""),
        "models": {str(k): str(v) for k, v in models.items() if isinstance(v, str) and v.strip()},
    }


def save(provider: Optional[str] = None, api_key_ref: Optional[str] = None,
         models: Optional[dict] = None) -> dict:
    """Persist any subset. `models` merges: an empty value clears that agent's choice."""
    config = load_config()
    state = dict(config.get(CONFIG_KEY) or {})
    if provider is not None:
        p = provider.strip().lower()
        if p and p not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        state["provider"] = p
    if api_key_ref is not None:
        ref = api_key_ref.strip()
        if ref and not _REF_RE.match(ref):
            raise ValueError("api_key_ref must be a $NAME secret reference; raw keys are never stored")
        state["api_key_ref"] = ref
    if models is not None:
        cur = dict(state.get("models") or {})
        for agent_id, model in models.items():
            m = model.strip() if isinstance(model, str) else ""
            if m:
                cur[str(agent_id)] = m
            else:
                cur.pop(str(agent_id), None)
        state["models"] = cur
    config[CONFIG_KEY] = state
    save_config(config)
    return load()


def model_for(agent) -> str:
    """Saved choice, then the agent's env override, then its default."""
    chosen = load()["models"].get(agent.id)
    if chosen:
        return chosen
    if agent.model_env:
        return os.environ.get(agent.model_env, agent.default_model)
    return agent.default_model
