FROM python:3.12-slim AS base

WORKDIR /app

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files, sample cases, and runner
COPY app/ ./app/
COPY data/ ./data/
COPY run.py .

# Configurable port fallback
ENV PORT=8000
EXPOSE 8000

# Run as non-root user for security
RUN useradd -m -u 1000 appuser
USER appuser

# Health check dynamically queries configurable PORT
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, os; port = os.environ.get('PORT', '8000'); urllib.request.urlopen(f'http://localhost:{port}/health')"

# Startup via run.py which respects PORT environment variable
CMD ["python", "run.py"]
