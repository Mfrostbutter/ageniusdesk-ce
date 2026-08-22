FROM python:3.12-slim

WORKDIR /app

# Install dependencies only. --no-emit-project keeps the `backend` package out
# of site-packages: the host runs backend from the source tree below, so a
# sandboxed module worker can exclude it from sys.path.
#
# Resolution comes from the committed uv.lock, not a fresh PyPI resolve. Plain
# `pip install .[extras]` ignored the lockfile and re-resolved on every build,
# so an upstream major release landed straight in a user's image: mcp 2.0
# dropped mcp.server.fastmcp and silently unmounted the built-in MCP server on
# any rebuild. Locked builds make the shipped artifact the tested one.
#
# AGD_EXTRAS selects optional-dependency extras. Default is lean (assistant only);
# build with --build-arg AGD_EXTRAS="assistant,langgraph" to include the LangGraph
# stack the agent-fleet community module needs.
ARG AGD_EXTRAS=assistant
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /bin/uv
COPY pyproject.toml uv.lock ./
RUN set -eux; \
    extras=""; \
    for e in $(echo "${AGD_EXTRAS}" | tr ',' ' '); do extras="${extras} --extra ${e}"; done; \
    uv export --frozen --no-dev --no-emit-project ${extras} -o /tmp/requirements.txt; \
    pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt; \
    rm /tmp/requirements.txt

# Copy application. backend/ runs from source (cwd /app is on sys.path).
# agd_module_worker/ is launched by absolute path for out-of-process modules.
COPY backend/ backend/
COPY frontend/ frontend/
COPY agd_module_worker/ agd_module_worker/

# Create data directory for SQLite + config
RUN mkdir -p /app/data/themes /app/data/templates

EXPOSE 3000

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "3000"]
