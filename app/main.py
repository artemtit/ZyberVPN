from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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
from app.api.platega_webhook import register_platega_webhook_routes
from app.bot.handlers.admin import router as admin_router, _BANNED_IDS as BANNED_IDS
from app.bot.handlers import setup_routers
from app.config import load_settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.servers import ServersRepository
from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager
from app.services.subscription import build_subscription_service
from app.utils.datetime import parse_iso_utc, to_moscow

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
        # Require a secret token so metrics are not publicly accessible.
        # If METRICS_TOKEN is not configured, deny all access rather than open it.
        expected = os.getenv("METRICS_TOKEN", "").strip()
        if not expected or request.headers.get("X-Metrics-Token", "") != expected:
            return web.json_response({"error": "forbidden"}, status=403)
        manager = build_vpn_manager(request.app["db"], request.app["settings"])
        data = await manager.get_metrics()
        return web.json_response(data)

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/metrics", metrics)
    app["db"] = db
    app["settings"] = settings
    app["subscription_service"] = build_subscription_service(db, settings)
    # Platega payment client — only created when credentials are configured.
    if settings.platega_merchant_id and settings.platega_api_key:
        from app.services.platega import PlategaClient
        return_url = settings.public_base_url or "https://t.me/"
        app["platega_client"] = PlategaClient(
            merchant_id=settings.platega_merchant_id,
            api_key=settings.platega_api_key,
            return_url=return_url,
            failed_url=return_url,
        )
        logging.info("Platega payment integration enabled merchant_id=***")
    else:
        app["platega_client"] = None
        logging.info("Platega not configured — set PLATEGA_MERCHANT_ID and PLATEGA_API_KEY to enable")
    if settings.redis_url and Redis is not None:
        redis = Redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
        app["rate_limiter"] = RedisRateLimiter(redis, settings.api_rate_limit_per_minute)
    else:
        app["rate_limiter"] = InMemoryRateLimiter(settings.api_rate_limit_per_minute)
    register_subscription_routes(app)
    register_platega_webhook_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT") or "10000")
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


async def _enforce_traffic_loop(db: Database, settings, bot, interval_seconds: int = 120) -> None:
    manager = build_vpn_manager(db, settings, bot=bot)
    while True:
        try:
            await manager.enforce_all_users()
        except Exception:
            logging.exception("Traffic enforcement loop failed")
        await asyncio.sleep(interval_seconds)


async def _provisioning_reconciliation_loop(db: Database, settings, bot: Bot, interval_seconds: int = 600) -> None:
    """Detect and heal payments stuck in 'provisioning' or 'paid' state.

    Every 10 minutes:
    - provisioning > 15 min → VPN creation hung or crashed
    - paid > 60 min         → provisioning was never started
    For each stuck payment: if the user already has a ready user_vpn row the
    payment status simply wasn't updated → mark active.  Otherwise log the
    structured event so support can investigate / reconciliation retries next run.
    After 2 hours in a stuck state, mark as failed so the payment is not silently
    ignored forever.
    """
    from app.repositories.payments import PaymentsRepository
    from app.repositories.user_vpn import UserVpnRepository
    from app.services.provisioning_failures import notify_provisioning_failed
    from app.utils.datetime import utc_now, parse_iso_utc

    payments_repo = PaymentsRepository(db)
    user_vpn_repo = UserVpnRepository(db)

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            stuck = await payments_repo.list_stuck_payments(prov_age_minutes=15, paid_age_minutes=60)
            if not stuck:
                continue
            logging.info("event=RECON_START stuck_count=%s", len(stuck))

            for payment in stuck:
                tg_id = int(payment.get("tg_id") or 0)
                payload = str(payment.get("payload") or "")
                status = str(payment.get("status") or "")
                created_at_raw = str(payment.get("created_at") or "")
                if not tg_id or not payload:
                    continue

                # If the payment has been stuck for > 2 hours, give up and mark failed.
                if created_at_raw:
                    try:
                        age_minutes = (utc_now() - parse_iso_utc(created_at_raw)).total_seconds() / 60
                    except Exception:
                        age_minutes = 0
                    if age_minutes > 120:
                        failed_payment = await payments_repo.mark_failed(payload, "reconciliation_timeout_2h")
                        if failed_payment:
                            await notify_provisioning_failed(bot, tg_id, payload)
                        logging.error(
                            "event=PROV_TIMEOUT_FAILED payload=%s tg_id=%s status=%s age_minutes=%.0f",
                            payload, tg_id, status, age_minutes,
                        )
                        continue

                # Check if user already has a ready VPN (provisioning succeeded but
                # mark_active was never called due to crash).
                vpn_rows = await user_vpn_repo.list_user_vpns(tg_id)
                has_ready = any(r.get("status") == "ready" for r in vpn_rows)

                if has_ready:
                    await payments_repo.mark_active(payload)
                    logging.info(
                        "event=PROV_RESOLVED payload=%s tg_id=%s status_was=%s",
                        payload, tg_id, status,
                    )
                    continue

                # No ready VPN found — log for admin visibility.
                logging.error(
                    "event=PROV_STUCK payload=%s tg_id=%s status=%s "
                    "tariff=%s purchase_type=%s renew_key_id=%s",
                    payload, tg_id, status,
                    payment.get("tariff_code"),
                    payment.get("purchase_type"),
                    payment.get("renew_key_id"),
                )
        except Exception:
            logging.exception("Provisioning reconciliation loop failed")


async def _disable_expired_access_loop(db: Database, settings, interval_seconds: int = 120) -> None:
    users_repo = UsersRepository(db)
    manager = build_vpn_manager(db, settings)
    while True:
        try:
            expired_tg_ids = await users_repo.list_expired_active_tg_ids(limit=300)
        except Exception:
            logging.exception("Disable expired access loop: failed to fetch expired users")
            await asyncio.sleep(interval_seconds)
            continue
        for tg_id in expired_tg_ids:
            try:
                await manager.disable_user_access(tg_id)
                await users_repo.update_status(tg_id, False)
            except Exception:
                logging.exception("Disable expired access loop: failed to disable user tg_id=%s", tg_id)
        await asyncio.sleep(interval_seconds)


async def _per_key_expiry_loop(db: Database, settings, bot: Bot | None = None, interval_seconds: int = 120) -> None:
    """Disable individual expired keys without touching other active keys for the same user.

    Unlike _disable_expired_access_loop (which acts on users.expires_at and kills all
    of a user's access), this loop acts on keys.expires_at and disables only that key's
    XUI client(s).  It then stamps keys.disabled_at so the key is not re-processed.
    """
    keys_repo = KeysRepository(db)
    manager = build_vpn_manager(db, settings)
    while True:
        try:
            expired_keys = await keys_repo.list_expired_enabled_keys(limit=300)
        except Exception:
            logging.exception("per_key_expiry_loop: failed to list expired keys")
            await asyncio.sleep(interval_seconds)
            continue

        for key_row in expired_keys:
            tg_id = int(key_row.get("tg_id") or 0)
            key_id = int(key_row.get("id") or 0)
            if not tg_id or not key_id:
                continue
            try:
                await manager.disable_key_access(tg_id, key_id)
            except Exception:
                logging.exception("per_key_expiry_loop: disable_key_access failed tg_id=%s key_id=%s", tg_id, key_id)
            try:
                await keys_repo.mark_disabled(key_id, tg_id)
            except Exception:
                logging.exception("per_key_expiry_loop: mark_disabled failed tg_id=%s key_id=%s", tg_id, key_id)
            if bot is not None:
                try:
                    await bot.send_message(
                        tg_id,
                        "⏰ <b>Ваш VPN-ключ заблокирован: истёк срок действия.</b>\n\n"
                        "Для продления нажмите кнопку ниже.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_open")]]
                        ),
                    )
                except TelegramForbiddenError:
                    pass
                except Exception:
                    logging.warning("per_key_expiry_loop: notify failed tg_id=%s key_id=%s", tg_id, key_id)

        await asyncio.sleep(interval_seconds)


async def _disk_alert_loop(bot: Bot, settings, threshold_pct: int = 85, interval_seconds: int = 3600) -> None:
    alerted = False
    while True:
        try:
            usage = shutil.disk_usage("/")
            used_pct = usage.used * 100 // usage.total
            if used_pct >= threshold_pct:
                if not alerted:
                    msg = (
                        f"⚠️ <b>Диск заполнен на {used_pct}%</b>\n"
                        f"Использовано: {usage.used // (1024**3)} ГБ / {usage.total // (1024**3)} ГБ\n"
                        "Очистите логи или расширьте диск."
                    )
                    for admin_id in settings.admin_ids:
                        try:
                            await bot.send_message(admin_id, msg)
                        except Exception:
                            pass
                    logging.warning("Disk usage critical: %d%%", used_pct)
                    alerted = True
            else:
                alerted = False
        except Exception:
            logging.exception("Disk alert loop failed")
        await asyncio.sleep(interval_seconds)


async def _expiry_notification_loop(bot: Bot, db: Database, settings) -> None:  # noqa: ARG001
    users_repo = UsersRepository(db)
    while True:
        try:
            expiring_3d = await users_repo.get_users_expiring_soon(72)
            for user in expiring_3d:
                if user.get("notified_3d_at"):
                    continue
                tg_id = int(user["tg_id"])
                expires_str = to_moscow(parse_iso_utc(user["expires_at"])).strftime("%d.%m.%Y")
                try:
                    await bot.send_message(
                        tg_id,
                        f"⚠️ Ваша подписка ZyberVPN истекает {expires_str}.\n\n"
                        "Продлите её, чтобы не потерять доступ.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_open")]]
                        ),
                    )
                    await users_repo.set_notified(tg_id, "3d")
                except TelegramForbiddenError:
                    pass
                except Exception:
                    logging.exception("Failed to send 3d expiry notification tg_id=%s", tg_id)
                await asyncio.sleep(0.1)

            expiring_1d = await users_repo.get_users_expiring_soon(24)
            for user in expiring_1d:
                if user.get("notified_1d_at"):
                    continue
                tg_id = int(user["tg_id"])
                expires_str = to_moscow(parse_iso_utc(user["expires_at"])).strftime("%d.%m.%Y")
                try:
                    await bot.send_message(
                        tg_id,
                        f"🔴 Подписка ZyberVPN истекает сегодня ({expires_str}).\n\n"
                        "Продлите прямо сейчас, чтобы не прерывать работу.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(text="⚡ Продлить сейчас", callback_data="buy_open")]]
                        ),
                    )
                    await users_repo.set_notified(tg_id, "1d")
                except TelegramForbiddenError:
                    pass
                except Exception:
                    logging.exception("Failed to send 1d expiry notification tg_id=%s", tg_id)
                await asyncio.sleep(0.1)
        except Exception:
            logging.exception("Expiry notification loop error")
        await asyncio.sleep(3600)


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

    # Middleware: block banned users before any handler runs.
    from aiogram import BaseMiddleware
    from aiogram.types import TelegramObject
    from typing import Any, Callable, Awaitable

    class BanMiddleware(BaseMiddleware):
        async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                           event: TelegramObject, data: dict[str, Any]) -> Any:
            user = data.get("event_from_user")
            if user and user.id in BANNED_IDS:
                return  # silently drop — user already received a ban message
            return await handler(event, data)

    dp.update.outer_middleware(BanMiddleware())

    setup_routers(dp)
    dp.include_router(admin_router)

    web_runner = await _start_health_server(db, settings)
    # Expose bot to the aiohttp app so the webhook handler can send Telegram messages.
    web_runner.app["bot"] = bot
    healthcheck_task = asyncio.create_task(_vpn_healthcheck_loop(db, settings))
    disable_expired_task = asyncio.create_task(_disable_expired_access_loop(db, settings))
    enforce_traffic_task = asyncio.create_task(_enforce_traffic_loop(db, settings, bot))
    per_key_expiry_task = asyncio.create_task(_per_key_expiry_loop(db, settings, bot=bot))
    expiry_notification_task = asyncio.create_task(_expiry_notification_loop(bot, db, settings))
    disk_alert_task = asyncio.create_task(_disk_alert_loop(bot, settings))
    reconciliation_task = asyncio.create_task(_provisioning_reconciliation_loop(db, settings, bot))

    # Restore banned-user set from DB so bans survive bot restarts.
    try:
        _ban_repo = UsersRepository(db)
        banned_ids = await _ban_repo.list_banned_tg_ids()
        BANNED_IDS.update(banned_ids)
        if banned_ids:
            logging.info("Loaded %d banned user IDs from DB", len(banned_ids))
    except Exception:
        logging.exception("Failed to load banned IDs from DB on startup")

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logging.info("Polling cancelled")
    finally:
        for task in (
            healthcheck_task,
            disable_expired_task,
            enforce_traffic_task,
            per_key_expiry_task,
            expiry_notification_task,
            disk_alert_task,
            reconciliation_task,
        ):
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
