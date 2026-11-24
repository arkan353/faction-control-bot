"""Single entrypoint: runs the Discord bot and the web panel in one process.

Both share one SQLite database and one asyncio loop, so a mark issued in the
browser updates Discord roles immediately through the bot connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import uvicorn

import bot as bot_module
import db
from config import DISCORD_TOKEN, HOST, PORT, oauth_configured

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("faction")


class WebServer(uvicorn.Server):
    """uvicorn server that leaves signal handling to the bot's loop."""

    @contextlib.contextmanager
    def capture_signals(self):  # noqa: D401 - contextlib override
        yield


def _require_config() -> bool:
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN не задан. Заполните .env (см. .env.example).")
        return False
    if not oauth_configured():
        log.warning(
            "CLIENT_ID/CLIENT_SECRET не заданы — панель запустится, но вход через Discord недоступен."
        )
    return True


async def _run() -> None:
    db.init_db()
    db.seed_defaults()

    discord_bot = bot_module.build_bot()
    server = WebServer(
        uvicorn.Config(
            "main:app",
            host=HOST,
            port=PORT,
            log_level="info",
            loop="asyncio",
            lifespan="on",
        )
    )
    web_task = asyncio.create_task(server.serve(), name="web")
    bot_task = asyncio.create_task(discord_bot.start(DISCORD_TOKEN), name="discord")

    done, pending = await asyncio.wait(
        {web_task, bot_task}, return_when=asyncio.FIRST_COMPLETED
    )

    for task in done:
        exc = task.exception()
        if exc is not None:
            log.error("Задача %s завершилась с ошибкой: %s", task.get_name(), exc)

    # One side stopped: ask the other to finish its own shutdown first.
    server.should_exit = True
    if not discord_bot.is_closed():
        await discord_bot.close()
    if pending:
        still_running, _ = await asyncio.wait(pending, timeout=10)
        for task in still_running:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def run() -> None:
    if not _require_config():
        sys.exit(1)
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")


if __name__ == "__main__":
    run()
