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


async def notify_provisioning_failed(bot: Bot, tg_id: int, payload: str) -> None:
    text = f"{PROVISIONING_FAILURE_TEXT}\n\nID платежа: <code>{escape(payload)}</code>"
    try:
        await bot.send_message(tg_id, text)
    except TelegramForbiddenError:
        logger.debug("Provisioning failure notification skipped: user blocked bot tg_id=%s", tg_id)
    except Exception:
        logger.exception("Provisioning failure notification failed tg_id=%s payload=%s", tg_id, payload)
