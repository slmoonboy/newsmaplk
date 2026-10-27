# syntax=docker/dockerfile:1
#
# LankaPulse — free/open-source runtime image.
# No API keys or paid services are required; the default configuration
# ingests public RSS feeds and uses the built-in rule extractor unless a
# local OpenAI-compatible LLM (e.g. Ollama) is reachable.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System packages kept minimal: lxml wheels exist for this platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# SQLite lives on a volume so events survive container restarts.
RUN mkdir -p /app/data \
    && addgroup --system lankapulse \
    && adduser --system --ingroup lankapulse --home /app lankapulse \
    && chown -R lankapulse:lankapulse /app

USER lankapulse

ENV HOST=0.0.0.0 \
    PORT=8000 \
    ENABLE_PIPELINE=true \
    LLM_PROVIDER=auto \
    DATABASE_URL=sqlite+aiosqlite:////app/data/lankapulse.db

WORKDIR /app/backend

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
