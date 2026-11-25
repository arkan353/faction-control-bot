#!/bin/sh
# Entrypoint: fail loudly when the mounted data directory is not writable.
# Without this the first sign of trouble is an opaque sqlite3 traceback.
set -eu

DB="${DB_PATH:-/app/data/faction.db}"
DIR=$(dirname "$DB")

if ! mkdir -p "$DIR" 2>/dev/null || [ ! -w "$DIR" ]; then
    echo "ERROR: каталог данных $DIR недоступен для записи (uid $(id -u))." >&2
    echo "На хосте: mkdir -p data && chown -R $(id -u):$(id -g) data" >&2
    echo "Или соберите образ с нужными правами: APP_UID=$(id -u) APP_GID=$(id -g)." >&2
    exit 1
fi

exec "$@"
