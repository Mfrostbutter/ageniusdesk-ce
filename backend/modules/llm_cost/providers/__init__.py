"""Source type registry."""

from __future__ import annotations

import os

from backend.modules.llm_cost.providers.anthropic_admin import AnthropicAdminProvider
from backend.modules.llm_cost.providers.base import Provider
from backend.modules.llm_cost.providers.claude_code_local import ClaudeCodeLocalProvider
from backend.modules.llm_cost.providers.demo import DemoProvider
from backend.modules.llm_cost.providers.http_json import HttpJsonProvider
from backend.modules.llm_cost.providers.openai_admin import OpenAIAdminProvider
from backend.modules.llm_cost.providers.openrouter import OpenRouterProvider
from backend.modules.llm_cost.providers.push import PushProvider

TYPES: dict[str, type[Provider]] = {
    cls.TYPE: cls
    for cls in (AnthropicAdminProvider, OpenAIAdminProvider, OpenRouterProvider, HttpJsonProvider,
                PushProvider, ClaudeCodeLocalProvider, DemoProvider)
}

# Source type -> billing provider, for billed-vs-attributed reconciliation.
BILLING_PROVIDER = {"anthropic_admin": "anthropic", "openai_admin": "openai", "openrouter": "openrouter"}


def demo_enabled() -> bool:
    return os.environ.get("AGD_LLM_COST_DEMO", "").strip().lower() in ("1", "true", "yes")


def available_types() -> list[dict]:
    return [cls.describe() for cls in TYPES.values() if not cls.DEV_ONLY or demo_enabled()]
