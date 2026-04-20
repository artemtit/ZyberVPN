from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from app.api.subscription import register_subscription_routes
from app.bot.handlers import setup_routers
from app.config import load_settings
from app.db.database import Database
from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager


async def _start_health_server(db: Database, settings) -> web.AppRunner:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app["settings"] = settings
    register_subscription_routes(app, db)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Health server started on 0.0.0.0:%s", port)
    return runner


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


async def _vpn_healthcheck_loop(db: Database, settings) -> None:
    manager = build_vpn_manager(db, settings)
    while True:
        try:
            await manager.refresh_server_health()
        except Exception as error:
            logging.warning("VPN healthcheck failed: %s", error)
        await asyncio.sleep(settings.vpn_healthcheck_interval_seconds)


async def _disable_expired_access_loop(db: Database, settings, interval_seconds: int = 120) -> None:
    users_repo = UsersRepository(db)
    manager = build_vpn_manager(db, settings)
    while True:
        try:
            expired_tg_ids = await users_repo.list_expired_active_tg_ids(limit=300)
            for tg_id in expired_tg_ids:
                await manager.disable_user_access(tg_id)
                await users_repo.update_status(tg_id, False)
        except Exception as error:
            logging.warning("Disable expired access loop failed: %s", error)
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

    web_runner = await _start_health_server(db, settings)
    subscription_watchdog_task = asyncio.create_task(_subscription_watchdog_loop(db))
    healthcheck_task = asyncio.create_task(_vpn_healthcheck_loop(db, settings))
    disable_expired_task = asyncio.create_task(_disable_expired_access_loop(db, settings))

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logging.info("Polling cancelled")
    finally:
        subscription_watchdog_task.cancel()
        healthcheck_task.cancel()
        disable_expired_task.cancel()
        try:
            await subscription_watchdog_task
        except asyncio.CancelledError:
            pass
        try:
            await healthcheck_task
        except asyncio.CancelledError:
            pass
        try:
            await disable_expired_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("Bot stopped")
