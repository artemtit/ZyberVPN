from __future__ import annotations

import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Settings
from app.db.database import Database
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository

router = Router()


def _is_admin(tg_id: int, settings: Settings) -> bool:
    return tg_id in settings.admin_ids


@router.message(Command("admin"))
async def admin_help(message: Message, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    await message.answer(
        "🔧 Admin commands:\n"
        "/stats — project statistics\n"
        "/broadcast <text> — send message to all active users"
    )


@router.message(Command("stats"))
async def admin_stats(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    users_repo = UsersRepository(db)
    subs_repo = SubscriptionsRepository(db)
    payments_repo = PaymentsRepository(db)

    total_users = await users_repo.count_all()
    active_users = await users_repo.count_active()
    total_revenue = await payments_repo.total_revenue()

    await message.answer(
        f"📊 Stats:\n"
        f"👥 Total users: {total_users}\n"
        f"✅ Active subscribers: {active_users}\n"
        f"💰 Total revenue: {total_revenue} RUB"
    )


@router.message(Command("broadcast"))
async def admin_broadcast(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    text = (message.text or "").removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Usage: /broadcast <your message>")
        return
    users_repo = UsersRepository(db)
    active_tg_ids = await users_repo.list_active_tg_ids()
    sent = 0
    failed = 0
    for tg_id in active_tg_ids:
        try:
            await message.bot.send_message(tg_id, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"Broadcast done: {sent} sent, {failed} failed.")
