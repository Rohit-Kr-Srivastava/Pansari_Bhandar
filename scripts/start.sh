#!/bin/sh
# Container entry point.
set -e
if [ "$(id -u)" = "0" ]; then
  # Railway mounts the volume owned by root; make it writable by the app user, then drop root.
  media="${MEDIA_ROOT:-/data/media}"
  mkdir -p "$media"
  [ "$(stat -c %U "$media")" = "app" ] || chown -R app:app "$(dirname "$media")"
  exec setpriv --reuid=app --regid=app --init-groups sh "$0" "$@"
fi
# Migrations normally run as Railway's pre-deploy step; set RUN_MIGRATIONS=1 to run them here
# instead (docker compose does).
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  alembic upgrade head
fi
# One worker: the rate limiter and page cache live in process memory (see docs/deployment.md).
# Client IPs come from X-Forwarded-For via TRUSTED_PROXY_HOPS (app/web/deps.py:client_ip),
# never from uvicorn's proxy handling, which trusts the client-controlled left-most entry.
exec uvicorn app.main:app_factory --factory \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --no-proxy-headers --no-access-log
