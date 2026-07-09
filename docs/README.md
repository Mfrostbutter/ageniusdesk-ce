# AgeniusDesk CE — Documentation

The command center for n8n automation operators: multi-instance management,
real-time error tracking, AI-assisted debugging, a Code Lab, container
management, and an encrypted secrets store — self-hosted, source-available.

This is the documentation root. It is split into two tracks:

- **User Guide** — how to *use* each feature. Task-oriented, for operators.
- **Architecture & Developer Reference** — how AgeniusDesk is *built*. For
  contributors, integrators, and anyone extending it.

Start with [Getting Started](guide/getting-started.md) if you just deployed, or
[Architecture Overview](architecture/overview.md) if you want the system model.

---

## User Guide

| Page | What it covers |
|------|----------------|
| [Getting Started](guide/getting-started.md) | First run, the setup wizard, creating the owner account, connecting your first n8n instance |
| [n8n Instances](guide/instances.md) | Adding, switching, and managing multiple n8n deployments |
| [Workflows](guide/workflows.md) | Listing, searching, activating, and triggering workflows |
| [Executions & Errors](guide/errors.md) | The error feed, grouping, AI triage, and installing the global error handler |
| [Insights](guide/insights.md) | Execution analytics, success rates, busiest/slowest workflows |
| [Containers](guide/containers.md) | One-click Docker deployment, templates, lifecycle management |
| [Code Lab](guide/code-lab.md) | The Monaco editor, AI code generation, the Prompt Builder, Agent Builder, and Send to n8n |
| [Agent Fleet](guide/agent-fleet.md) | Build LangGraph / PydanticAI agents in Code Lab and run + monitor them: the catalog, live graph + run waterfall, human-in-the-loop, and vault-stored agents you own |
| [AI Assistant & Models](guide/ai-assistant.md) | Per-area model config, providers, the assistant chat, MCP tools, and RAG |
| [The Harness (Knowledge)](guide/knowledge.md) | Knowledge sources, connectors, agent instructions, and the notes vault |
| [Secrets](guide/secrets.md) | The encrypted local store, `$NAME` references, compound typed secrets, and syncing credentials into n8n |
| [Import & Export](guide/import-export.md) | Importing workflows and backing up / restoring them |
| [Admin & Users](guide/admin-users.md) | Dashboard accounts, roles, n8n user management, and system settings |
| [Themes & Music](guide/themes-music.md) | Built-in and custom themes, and the music player |

## Architecture & Developer Reference

| Page | What it covers |
|------|----------------|
| [Architecture Overview](architecture/overview.md) | The big picture: backend, frontend, storage, real-time, request lifecycle |
| [Module System](architecture/modules.md) | Auto-discovered modules, manifests, version gating, community modules |
| [Data Model & Storage](architecture/data-model.md) | SQLite schema, encrypted config/secrets, the secret-resolution order |
| [Authentication & RBAC](architecture/auth.md) | Local accounts, sessions, 2FA, password reset, the internal API gate, edge auth |
| [Frontend Architecture](architecture/frontend.md) | Zero-build ES modules, views, the WebSocket bus, coachmarks |
| [API Reference](architecture/api.md) | The public `/api/v1` surface and the Dashboard-as-MCP server |
| [Security Posture](architecture/security.md) | Threat model, hardening knobs, deployment guidance |
| [Silent-failure detection](architecture/silent-failure-detection.md) | Catching "green but broken" n8n runs: why status can't see them, the output-shape classifier, and how they surface across the views |

## Operations

| Page | What it covers |
|------|----------------|
| [Configuration](CONFIG.md) | Every environment variable |
| [Deployment](DEPLOY.md) | Docker Compose, reverse proxy, backup, rollback |
| [Contributing](../CONTRIBUTING.md) | Dev setup and PR guidelines |
| [Changelog](../CHANGELOG.md) | Release history |

---

## Tech stack at a glance

- **Backend:** Python 3.10+, FastAPI (async), httpx, aiosqlite
- **Frontend:** Vanilla JavaScript ES modules, zero build step, Monaco from CDN
- **Storage:** SQLite + Fernet-encrypted `config.json` / `secrets.json`
- **Real-time:** WebSocket broadcast bus
- **AI:** OpenRouter / OpenAI / Anthropic / Ollama with function calling, MCP
  client integration, and optional Qdrant RAG
- **Packaging:** Docker Compose; bare-metal Python supported

See [Architecture Overview](architecture/overview.md) for how these fit together.
