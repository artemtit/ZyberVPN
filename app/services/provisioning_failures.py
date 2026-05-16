from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

logger = logging.getLogger(__name__)

PROVISIONING_FAILURE_TEXT = (
    "⚠️ Ошибка активации VPN. Платёж получен, но доступ не выдан. "
    "Напишите в поддержку."
)


async def notify_provisioning_failed(
    bot: Bot,
    tg_id: int,
    payload: str,
    admin_ids: list[int] | None = None,
) -> None:
    user_text = f"{PROVISIONING_FAILURE_TEXT}\n\nID платежа: <code>{escape(payload)}</code>"
    try:
        await bot.send_message(tg_id, user_text)
    except TelegramForbiddenError:
        logger.debug("Provisioning failure notification skipped: user blocked bot tg_id=%s", tg_id)
    except Exception:
        logger.exception("Provisioning failure notification failed tg_id=%s payload=%s", tg_id, payload)

    if admin_ids:
        admin_text = (
            f"⚠️ <b>Ошибка провизионинга</b>\n\n"
            f"👤 Пользователь: <code>{tg_id}</code>\n"
            f"💳 Платёж: <code>{escape(payload)}</code>\n\n"
            "VPN-ключ не выдан, платёж получен. Требуется ручная проверка."
        )
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, admin_text)
            except Exception:
                logger.warning("Failed to notify admin tg_id=%s about provisioning failure", admin_id)
