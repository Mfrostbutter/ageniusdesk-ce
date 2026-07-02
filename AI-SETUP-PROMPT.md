# AI Setup Prompt

Copy the prompt below and paste it to your AI assistant (Claude, ChatGPT, Cursor, etc.) to have it set up AgeniusDesk CE for you.

---

You are helping me set up **AgeniusDesk Community Edition**, an open-source command center for n8n automation: multi-instance management, a real-time error feed, OpenTelemetry traces, a Code Lab with AI assistance, container management, and an encrypted secrets store, all from one dashboard. It is a Python 3.10+ FastAPI backend with a zero-build vanilla JS frontend, MIT licensed, from https://github.com/Mfrostbutter/ageniusdesk-ce.

Set it up as follows:

1. **Clone the repo:**
   ```bash
   git clone https://github.com/Mfrostbutter/ageniusdesk-ce.git
   cd ageniusdesk-ce
   ```

2. **Create the environment file:**
   ```bash
   cp .env.example .env
   ```
   Nothing in `.env` is strictly required to boot. Optional keys to set now or later: `PORT` (host port, default 3000), `SECRET_KEY` (auto-generated if unset), and AI provider credentials such as `ANTHROPIC_KEY`, `OPEN_AI_KEY`, `OPEN_ROUTER_KEY`, or `OLLAMA_URL`. The full reference is `docs/CONFIG.md`.

3. **Run it (Docker, recommended):**
   ```bash
   docker compose up -d --build
   ```
   Then open http://localhost:3000. A setup wizard walks through creating the owner account and adding the first n8n instance (URL + API key).

   If port 3000 is taken (`Bind for 0.0.0.0:3000 failed`), set a free host port and re-run: `PORT=8080 docker compose up -d --build`, then open http://localhost:8080. The container always listens on 3000 internally.

   **Optional Agent Fleet (LangGraph + PydanticAI agents):** off by default. To enable, add `echo "AGD_EXTRAS=assistant,langgraph" >> .env` before building, and set an Anthropic key (`ANTHROPIC_KEY` in `.env`, or Settings > Secrets after first boot).

   **Bare metal alternative (no Docker),** requires Python 3.10+:
   ```bash
   pip install '.[assistant]'
   cp .env.example .env
   python -m uvicorn backend.main:app --host 0.0.0.0 --port 3000
   ```

4. **After it is running:** connect an n8n instance via the setup wizard, then wire real-time error reporting. Adding an instance auto-installs the global error handler workflow; the one manual step n8n requires is selecting it under **Settings > Workflows > Error Workflow** in n8n (details in the README's "Error Handler Setup" section).

For navigating the codebase: `KNOWLEDGE-REGISTRY.yaml` at the repo root is a machine-readable index (read-first docs, hard rules, one-line map of every backend module), and `CLAUDE.md` carries the compact project context (tech stack, module system, conventions). Full docs live under `docs/` starting at `docs/README.md`.
