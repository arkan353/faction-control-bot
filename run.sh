#!/usr/bin/env bash
# Запуск системы управления фракцией (бот + веб-панель в одном процессе).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Создаю виртуальное окружение…"
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "Нет .env — создаю из .env.example. Заполните DISCORD_TOKEN, CLIENT_ID, CLIENT_SECRET."
  cp .env.example .env
  chmod 600 .env
  exit 1
fi

exec .venv/bin/python app.py
