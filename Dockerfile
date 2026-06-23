FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir '.[assistant]'

# Copy application
COPY backend/ backend/
COPY frontend/ frontend/

# Create data directory for SQLite + config
RUN mkdir -p /app/data/themes /app/data/templates

EXPOSE 3000

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "3000"]
