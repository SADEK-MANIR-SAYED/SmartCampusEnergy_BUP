# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application ───────────────────────────────────────────────────────────────
COPY app/ ./app/
COPY data/ ./data/

# ── Runtime configuration ─────────────────────────────────────────────────────
# All secrets are supplied at runtime via environment variables.
# Do NOT bake API keys into the image.

EXPOSE 8000

# Run as non-root user for security
RUN useradd -m -u 1000 appuser
USER appuser

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# ── Startup ───────────────────────────────────────────────────────────────────
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
