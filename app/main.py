from __future__ import annotations

import asyncio
import json
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from app.api.middlewares import (
    InMemoryRateLimiter,
    RateLimitConfig,
    RedisRateLimiter,
    build_rate_limit_middleware,
    error_middleware,
    request_logging_middleware,
)
from app.api.subscription import register_subscription_routes
from app.bot.handlers import setup_routers
from app.config import load_settings
from app.db.database import Database
from app.repositories.servers import ServersRepository
from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager
from app.services.subscription import build_subscription_service

try:
    from aiogram.fsm.storage.redis import RedisStorage
except Exception:  # pragma: no cover
    RedisStorage = None  # type: ignore[assignment]

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore[assignment]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]


async def _start_health_server(db: Database, settings) -> web.AppRunner:
    middlewares = [
        error_middleware,
        request_logging_middleware,
        build_rate_limit_middleware(RateLimitConfig(per_minute=settings.api_rate_limit_per_minute)),
    ]
    app = web.Application(middlewares=middlewares)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def metrics(request: web.Request) -> web.Response:
        manager = build_vpn_manager(request.app["db"], request.app["settings"])
        data = await manager.get_metrics()
        return web.json_response(data)

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/metrics", metrics)
    app["db"] = db
    app["settings"] = settings
    app["subscription_service"] = build_subscription_service(db, settings)
    if settings.redis_url and Redis is not None:
        redis = Redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
        app["rate_limiter"] = RedisRateLimiter(redis, settings.api_rate_limit_per_minute)
    else:
        app["rate_limiter"] = InMemoryRateLimiter(settings.api_rate_limit_per_minute)
    register_subscription_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Health server started on 0.0.0.0:%s", port)
    return runner


def _build_dispatcher(settings) -> Dispatcher:
    if settings.redis_url and RedisStorage is not None:
        try:
            storage = RedisStorage.from_url(settings.redis_url)
            return Dispatcher(storage=storage)
        except Exception:
            logging.exception("Redis storage init failed, fallback to MemoryStorage")
    return Dispatcher(storage=MemoryStorage())


async def _subscription_watchdog_loop(db: Database, interval_seconds: int = 3600) -> None:
    users_repo = UsersRepository(db)
    while True:
        try:
            updated = await users_repo.deactivate_expired_users()
            if updated:
                logging.info("Subscription watchdog deactivated %s expired users", updated)
        except Exception:
            logging.exception("Subscription watchdog failed")
        await asyncio.sleep(interval_seconds)


async def _vpn_healthcheck_loop(db: Database, settings) -> None:
    manager = build_vpn_manager(db, settings)
    while True:
        try:
            await manager.refresh_server_health()
        except RuntimeError as error:
            logging.error("VPN healthcheck degraded operation=vpn.refresh_server_health error=%s", error)
        except Exception:
            logging.exception("VPN healthcheck failed")
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
        except Exception:
            logging.exception("Disable expired access loop failed")
        await asyncio.sleep(interval_seconds)


async def run() -> None:
    configure_logging()
    settings = load_settings()
    db = Database(settings.db_path)
    await db.init()
    await ServersRepository(db).startup_probe()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = _build_dispatcher(settings)
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
        for task in (subscription_watchdog_task, healthcheck_task, disable_expired_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.session.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("Bot stopped")
