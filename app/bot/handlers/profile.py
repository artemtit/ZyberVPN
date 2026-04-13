from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import profile_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.utils.datetime import utcnow

router = Router()


def _subscription_label(subscription: dict | None) -> tuple[str, str]:
    if not subscription:
        return "Нет активной", "—"
    expires_at = datetime.fromisoformat(subscription["expires_at"])
    status = "Активна" if expires_at > utcnow() and subscription["status"] == "active" else "Истекла"
    return status, expires_at.strftime("%d.%m.%Y %H:%M")


@router.message(F.text == "👤 Личный кабинет")
async def profile(message: Message, db: Database) -> None:
    users_repo = UsersRepository(db)
    subs_repo = SubscriptionsRepository(db)
    user = await users_repo.get_or_create(message.from_user.id)
    sub = await subs_repo.get_latest(user["id"])
    status, expires = _subscription_label(sub)
    await message.answer(
        f"ID: {message.from_user.id}\nПодписка: {status}\nСрок: {expires}\nБаланс: {user['balance']} RUB",
        reply_markup=profile_keyboard(),
    )


@router.callback_query(F.data == "profile_ref")
async def profile_referrals(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    users_repo = UsersRepository(db)
    user = await users_repo.get_or_create(callback.from_user.id)
    invited = await users_repo.count_referrals(user["id"])
    me = await callback.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    await callback.message.answer(
        f"Реферальная ссылка:\n{link}\nПриглашено: {invited}\nНачисление: {settings.referral_bonus_percent}%"
    )
    await callback.answer()


@router.callback_query(F.data == "profile_promo")
async def profile_promo(callback: CallbackQuery) -> None:
    await callback.message.answer("Ввод промокода появится в следующем релизе.")
    await callback.answer()
