# AI Assistant & Models

AgeniusDesk runs three independent AI assistants, one per area of the app: Code Lab, Error Triage, and the General Assistant. Each area owns its own provider, model, instructions, and optional fallback. There is no global default and no instruction layering between areas. This page covers how to configure each area, the supported providers, how API keys are resolved, and the assistant's tool and RAG capabilities.

Sources: `backend/modules/assistant/router.py`, `backend/modules/assistant/providers.py`, `frontend/js/views/models.js`, `frontend/js/views/settings.js` (`renderModelsTab`), `frontend/js/components/assistant-dock.js`. Keys are stored in and resolved from [Secrets](secrets.md).

## The three areas

Each area is a self-contained assistant. Saving one area never touches the others (`POST /api/assistant/jobs` merges partial payloads).

| Area (surface id) | Powers | Default instructions |
|---|---|---|
| Code Lab (`codelab`) | The Code Lab assistant (Code Node + Workflow Builder). See [Code Lab](code-lab.md). | Help the user write, fix, and understand n8n Code-node code |
| Error Triage (`triage`) | The "Ask AI" analysis on workflow errors | Diagnose root cause of an n8n error and give concrete, actionable fixes |
| General Assistant (`assistant`) | The main dashboard assistant chat (the assistant dock) | General-purpose assistant system prompt |

The only thing shared across all three areas is the Harness constitution (house rules), which is prepended to every area's system prompt. See [The Harness](knowledge.md).

## Where to configure

Configure the areas in **Settings → Models** (also available as a focused **AI Models** view, `frontend/js/views/models.js`, which renders the same panel). Each area is a card with: Provider, Model, API key picker, Test & load models, Instructions, an optional Fallback model, and Save.

A shared card at the bottom holds the Ollama URL used whenever any area's provider is set to Ollama.

## Supported providers

| Provider | API key needed | Live model list source | Notes |
|---|---|---|---|
| OpenRouter | Yes (`$OPEN_ROUTER_KEY`) | `GET https://openrouter.ai/api/v1/models` | Routes most model families. The connection test hits the key-validation endpoint because `/v1/models` is public |
| OpenAI | Yes (`$OPEN_AI_KEY`) | `GET https://api.openai.com/v1/models` (filtered to chat-capable models) | Codex / o1 / o3 / o4 / gpt-5 models are routed to the Responses API automatically; embedding models are rejected as chat models |
| Anthropic | Yes (`$ANTHROPIC_KEY`) | `GET https://api.anthropic.com/v1/models` | Uses the Messages API with tool support |
| Ollama | No | `GET <ollama_url>/api/tags` | Local/LAN models. Set the shared Ollama URL. Operator-supplied URLs are SSRF-screened before the server fetches them |

When a live model list cannot be fetched (no key, auth failure, network error, timeout), the picker falls back to a hardcoded list so a model is always selectable. Live lists are cached in-memory for 5 minutes, keyed per resolved key.

## Per-area fields

| Field | Description |
|---|---|
| Provider | OpenRouter, OpenAI, Anthropic, or Ollama for this area |
| Model | The model id. Populated from the live list (or fallback). A saved model not in the live list is preserved and marked "(saved)" |
| API key | Dropdown of stored secrets. "Use provider default key" uses the per-provider convention key; or pick any specific `$NAME` secret for this area |
| Test & load models | Validates the selected key and pulls the live model list for this area |
| Instructions | The area's system prompt. Independent per area; nothing overrides another. "Reset to default" restores the built-in text |
| Fallback provider / model | Optional. Used only if the primary errors with 5xx / 429 / timeout |
| Save | Persists only this area (`POST /api/assistant/jobs`) |

### The API key dropdown

The API key dropdown lists every secret stored in the Secrets store, plus a "Use provider default key" entry.

- **Use provider default key** resolves the per-provider convention secret: `$ANTHROPIC_KEY`, `$OPEN_AI_KEY`, or `$OPEN_ROUTER_KEY`.
- Selecting a specific `$NAME` uses exactly that secret for this area, overriding the convention.
- Keys are referenced by name only. The reference (`api_key_ref`) is saved with the area; the plaintext key is resolved server-side at request time and never round-trips through the browser.
- A key hint under the dropdown shows whether a usable key was found (`key_status`), which secret is in use, or a warning with a link to add the missing key in [Secrets](secrets.md).

### Test & load models

The **Test & load models** button (and selecting a key) validates the area's key and reloads its live model list in one action:

1. It calls `POST /api/assistant/test-creds` with the chosen `provider` and `api_key_ref`. The ref is resolved to plaintext server-side.
2. On success the live model list is loaded and the hint shows "Connected — live models loaded".
3. On failure the hint shows the provider's error message; the fallback model list is used.

Ollama needs no key, so for Ollama this just loads the tag list from the shaped Ollama URL.

### Saving an area

1. Pick the Provider and Model.
2. Pick the API key (or leave "Use provider default key").
3. Click **Test & load models** to confirm the key works and refresh the model list.
4. Edit the Instructions (or Reset to default).
5. Optionally expand **Fallback model (optional)** and choose a fallback provider/model.
6. Click **Save**. Only this area is written. A mounted Code Lab or Assistant picker updates live and any sticky session override for that area is cleared.

## How a chat request resolves provider and model

When any area sends a chat (`POST /api/assistant/chat` with a `surface`):

1. The backend loads that area's job config (provider, model, instructions, fallback, key ref).
2. If the request carries an inline session **override** (the Code Lab or dock picker), that override's provider/model wins for that one request. The saved config is never mutated.
3. The provider key is resolved deterministically: the explicit `api_key_ref` if set, else the per-provider convention secret, else (for the matching provider) a legacy global key.
4. If the primary call fails with a transient error (5xx, 429, timeout, rate-limit/overloaded), the area's fallback (or a request-supplied fallback) is tried. Fatal errors (401, 403, 400, bad config) are returned immediately without a fallback attempt.
5. The response reports `served_by` ("primary" or "fallback"); when the fallback served, it also includes the primary's redacted error. A "Fallback model used" toast is broadcast.

## System prompt composition

Every chat builds the system prompt in this order (`providers.chat`):

1. The Harness constitution body (shared house rules; see [The Harness](knowledge.md)). Fail-soft: skipped on error.
2. The area's own instructions.
3. A baseline Environment block with grounded facts about the setup.
4. Any per-request context (e.g. the current editor code in Code Lab).
5. Uploaded knowledge files (text formats, 20 MB total cap, via `POST /api/assistant/files`).
6. Optional Qdrant RAG context.

## Function calling and tools

The OpenAI-compatible, OpenAI, and Anthropic backends all support tool use. On each turn the assistant is given a tool set and may call tools across up to 10 rounds before being forced to produce a final tool-less answer.

Built-in tools (`backend/modules/assistant/tools.py`) operate against the active n8n instance and dashboard data:

| Tool | Action |
|---|---|
| `list_workflows` | List workflows on the active instance |
| `get_workflow` | Fetch a workflow's definition |
| `trigger_workflow` | Run a workflow |
| `list_executions` | List recent executions |
| `get_execution` | Fetch one execution's detail |
| `set_workflow_active` | Activate/deactivate a workflow |
| `import_workflow` | Create a new workflow |
| `get_recent_errors` | Read recently collected errors |

Workspace tools are also registered. The Ollama backend and the OpenAI Responses API path (codex / reasoning models) run text-only without tools, so tool-equipped flows should pick chat-completions-compatible models.

> Note: tool use is the same on every area. There is no per-area tool toggle in the configuration UI.

### MCP server tools

Any MCP servers configured in the dashboard contribute their tools to the assistant alongside the built-in tools, scoped to the active instance. The built-in [`n8n-mcp`](https://github.com/czlonkowski/n8n-mcp) server by czlonkowski (MIT) gives the assistant deep n8n node knowledge plus workflow validation and create/update tools, which improves Workflow Builder accuracy; it auto-installs when Docker is available (Settings → MCP Servers → n8n Intelligence). Configure other MCP servers in the admin area; see [admin & users](admin-users.md).

## Optional Qdrant RAG

If a Qdrant URL and collection are configured for the assistant (`backend/modules/assistant/rag.py`), the last user message is used to pull matching documents from the collection and append them to the system prompt as additional context. RAG is enabled only when both the Qdrant URL and a collection name are set; otherwise it is skipped silently. The shipped search is a lightweight text-match scroll (no embedding endpoint required).

## The assistant dock

The General Assistant area also surfaces as a compact chat dock (`frontend/js/components/assistant-dock.js`), currently on the Dashboard. It shares the saved config with Settings → Models: its provider/model picker reads and writes the same configuration, so the two stay in sync. It has its own Test connection and optional fallback picker, and an "Open full settings" link.

## Permissions (RBAC)

| Action | Required role |
|---|---|
| Using any assistant (chat, listing models, testing creds) | operator |
| Changing area config, shared infra, or the constitution | admin |

The whole assistant surface has an operator floor (chat spends tokens; model/test routes reach operator-supplied URLs; `/config` returns masked keys). Config-mutating routes (`/jobs`, `/shared`, `/config`, `/baseline`) require admin. On an open install (`AGD_DISABLE_LOGIN`) the role checks are no-ops.

## Related

- [Code Lab](code-lab.md) - uses the Code Lab area
- [Secrets](secrets.md) - store the provider keys resolved by reference
- [The Harness](knowledge.md) - shared constitution prepended to every area
- [Admin & users](admin-users.md) - MCP server configuration and RBAC
- Architecture: [../architecture/](../architecture/)
