from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import ClientSession, ClientTimeout, web

from app.api.subscription import register_subscription_routes
from app.bot.handlers import setup_routers
from app.config import load_settings
from app.db.database import Database
from app.repositories.users import UsersRepository


async def _start_health_server(db: Database) -> web.AppRunner:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    register_subscription_routes(app, db)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Health server started on 0.0.0.0:%s", port)
    return runner


async def _keepalive_ping_loop(url: str, interval_seconds: int) -> None:
    timeout = ClientTimeout(total=10)
    async with ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                async with session.get(url) as response:
                    logging.info("Keepalive ping %s -> %s", url, response.status)
            except Exception as error:
                logging.warning("Keepalive ping failed for %s: %s", url, error)


async def _subscription_watchdog_loop(db: Database, interval_seconds: int = 3600) -> None:
    users_repo = UsersRepository(db)
    while True:
        try:
            updated = await users_repo.deactivate_expired_users()
            if updated:
                logging.info("Subscription watchdog deactivated %s expired users", updated)
        except Exception as error:
            logging.warning("Subscription watchdog failed: %s", error)
        await asyncio.sleep(interval_seconds)


async def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    db = Database(settings.db_path)
    await db.init()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["settings"] = settings
    setup_routers(dp)

    web_runner = await _start_health_server(db)
    keepalive_task: asyncio.Task | None = None
    subscription_watchdog_task = asyncio.create_task(_subscription_watchdog_loop(db))
    keepalive_url = os.getenv("KEEPALIVE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    keepalive_interval = int(os.getenv("KEEPALIVE_INTERVAL_SECONDS", "300"))
    if keepalive_url:
        keepalive_task = asyncio.create_task(_keepalive_ping_loop(keepalive_url, keepalive_interval))

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logging.info("Polling cancelled")
    finally:
        if keepalive_task:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
        subscription_watchdog_task.cancel()
        try:
            await subscription_watchdog_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("Bot stopped")
