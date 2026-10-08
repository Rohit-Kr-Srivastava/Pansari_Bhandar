# Pansari Bhandar: production image (also used by docker-compose for local runs).
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY requirements.txt ./
RUN python -m venv /venv && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    ENVIRONMENT=production \
    MEDIA_ROOT=/data/media \
    TRUSTED_PROXY_HOPS=1 \
    PORT=8000
RUN groupadd --system app && useradd --system --gid app --home /app app \
    && mkdir -p /data/media && chown -R app:app /data
WORKDIR /app
COPY --from=build /venv /venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
# Starts as root only to fix volume ownership (Railway mounts volumes as root), then start.sh
# drops to the unprivileged 'app' user before the web server runs.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)"
CMD ["sh", "scripts/start.sh"]
