# syntax=docker/dockerfile:1
#
# Faction Control — Discord bot + web panel in one process.
# Bot and uvicorn share one SQLite file and one asyncio loop (see app.py), so a
# rank change in the browser updates Discord roles immediately.

ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/app/data/faction.db

# Runtime user. Match the owner of ./data on the host: APP_UID=$(id -u) etc.
ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

# tzdata: filters.dt() renders timestamps in local time (TZ in compose), and the
# slim base image does not ship the zoneinfo tree. passwd: useradd/groupadd are
# not guaranteed to be present.
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tzdata passwd; \
    rm -rf /var/lib/apt/lists/*

# Dependencies first: cached until requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code. .venv, .env and data/ are excluded by .dockerignore.
COPY . .

RUN chmod +x /app/docker-entrypoint.sh

# Unprivileged runtime user. Guards keep the build working if an id is already
# taken in the base image.
RUN set -eux; \
    if ! getent group "${APP_GID}" >/dev/null; then groupadd --gid "${APP_GID}" app; fi; \
    if ! getent passwd "${APP_UID}" >/dev/null; then \
        useradd --uid "${APP_UID}" --gid "${APP_GID}" \
                --no-create-home --shell /usr/sbin/nologin app; \
    fi; \
    mkdir -p /app/data; \
    chown -R "${APP_UID}:${APP_GID}" /app

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

# Panel health endpoint (main.py: GET /health). No curl in the slim image, so
# use urllib from the stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health',timeout=3)"]

# Default SIGTERM is handled inside app.py: it closes the Discord connection and
# then uvicorn, so `docker stop` is a clean shutdown.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "app.py"]
