FROM python:3.12-slim

WORKDIR /app

# Install dependencies only. pyproject packages nothing, so this installs the
# declared deps but NOT the `backend` package: the host runs backend from the
# source tree below, keeping it out of site-packages so a sandboxed module
# worker can exclude it from sys.path.
#
# AGD_EXTRAS selects optional-dependency extras. Default is lean (assistant only);
# build with --build-arg AGD_EXTRAS="assistant,langgraph" to include the LangGraph
# stack the agent-fleet community module needs.
ARG AGD_EXTRAS=assistant
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[${AGD_EXTRAS}]"

# Copy application. backend/ runs from source (cwd /app is on sys.path).
# agd_module_worker/ is launched by absolute path for out-of-process modules.
COPY backend/ backend/
COPY frontend/ frontend/
COPY agd_module_worker/ agd_module_worker/

# Create data directory for SQLite + config
RUN mkdir -p /app/data/themes /app/data/templates

EXPOSE 3000

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "3000"]
