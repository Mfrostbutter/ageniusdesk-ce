# Roadmap

AgeniusDesk Community Edition is a lightweight, open-source control plane for n8n. The roadmap prioritizes stability, extensibility, and the features operators need most.

## Current Release: v0.1.0 (2026-06-23)

### Completed Features

- Multi-instance n8n management with encrypted API key storage
- Real-time error feed and error grouping (by workflow, node, error type)
- Workflow management (list, activate/deactivate, trigger, import, export)
- Execution history with full-text search and filtering
- AI Assistant with OpenRouter, OpenAI, Anthropic, and local Ollama support
- MCP server integration for extending the assistant with external tools
- Code Lab with Monaco editor and AI code generation
- Knowledge management (sources, notes vault, full-text search)
- Encrypted secret store with `$VAR_NAME` references
- Docker container management with one-click deployment
- Community template library for common services
- Themes (Dark, Light, Cyberpunk, Matrix) with custom theme support
- Music player (Spotify, YouTube, SoundCloud, Apple Music, Tidal)
- Insights dashboard (success rates, error trends, busiest workflows)
- Docker Compose deployment with setup wizard
- Comprehensive documentation and contributing guidelines

---

## Near-Term (Next 2-3 Months)

- [ ] **More container templates**: MySQL, MongoDB, Minio, additional databases and services
- [ ] **Richer Code Lab**: code snippets library, n8n node documentation sidebar, template expansion
- [ ] **Additional knowledge connectors**: HTTP fetch, GitHub, API connectors beyond Qdrant
- [ ] **Workflow version history**: snapshot on import, diff viewer, restore from snapshot
- [ ] **Scheduled backups**: automated per-instance backup with configurable retention
- [ ] **Health monitoring**: configurable endpoint polling, uptime tracking, SLA dashboards
- [ ] **Expanded notification sinks**: email, PagerDuty, webhook routing per instance
- [ ] **Security audit scan**: detect missing error handlers, unused credentials, exposed webhooks

---

## Medium-Term (v0.2 Concept)

- [ ] **Multi-tenancy foundation**: group instances and workflows by client or team
- [ ] **User roles and permissions**: operator, viewer, workflow-trigger-only roles
- [ ] **Audit logging**: track all user actions for compliance
- [ ] **Cost tracking integration**: aggregate LLM spend and n8n execution metrics
- [ ] **Workflow promotion**: promote workflows across dev, staging, production instances
- [ ] **Public API stabilization**: versioned `/api/v1` endpoints with X-API-Key auth

---

## Future Directions

- Workflow diff viewer (visual side-by-side comparison)
- External secret sources (1Password, AWS Secrets Manager, Vault)
- Git integration (export workflows to repos, branch-based environments)
- SAML/LDAP for team authentication
- Agentic workflow management (LangChain integration, agent monitoring)
- Client-facing portal (scoped workflow access for non-operators)
- Home Assistant integration for homelab automation centers
- Support for other automation platforms (Make, Zapier)

---

## How to Contribute

We welcome pull requests for:
- Bug fixes and stability improvements
- New container templates
- Additional knowledge connectors
- UI/UX enhancements
- Documentation improvements
- Test coverage (pytest)

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and guidelines.

---

## Feedback

Found a bug or have a feature request? Please open a [GitHub issue](https://github.com/Mfrostbutter/ageniusdesk-ce/issues).
