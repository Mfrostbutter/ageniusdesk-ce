"""Mirror AgeniusDesk secrets → n8n credentials.

When the user adds `$ANTHROPIC_KEY` to the AgeniusDesk secrets store, we can
push that value into an n8n instance as a typed credential (e.g. `anthropicApi`)
via n8n's public API. Workflows built in n8n then reference the credential by
ID — no secret material ends up in workflow JSON.

Scope (MVP): three credential types — anthropicApi, openRouterApi, telegramApi.
Extensible via `mappings.CRED_TYPES`.
"""
from .router import router

__all__ = ["router"]
