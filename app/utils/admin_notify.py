from __future__ import annotations

import logging

from aiogram import Bot

logger = logging.getLogger(__name__)


async def notify_admins(bot: Bot, admin_ids: list[int], text: str) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            logger.warning("admin_notify: failed to send to admin_id=%s", admin_id)
