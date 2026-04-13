from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import profile_keyboard, promo_keyboard, referral_keyboard, topup_keyboard
from app.bot.states.purchase import ProfileState
from app.db.database import Database
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.utils.datetime import utcnow

router = Router()


def _remaining(subscription: dict | None) -> tuple[str, str, int]:
    if not subscription:
        return "Не активна ❌", "0 д. 0 ч.", 0
    expires_at = datetime.fromisoformat(subscription["expires_at"])
    if expires_at <= utcnow() or subscription["status"] != "active":
        return "Не активна ❌", "0 д. 0 ч.", 0
    delta = expires_at - utcnow()
    total_seconds = int(delta.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    return "Активна ✅", f"{days} д. {hours} ч.", max(days // 30, 0)


@router.callback_query(F.data == "menu_profile")
async def profile(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    users_repo = UsersRepository(db)
    subs_repo = SubscriptionsRepository(db)
    user = await users_repo.get_or_create(callback.from_user.id)
    sub = await subs_repo.get_latest(user["id"])
    sub_status, left, months_total = _remaining(sub)
    username = callback.from_user.username or callback.from_user.full_name
    invited = await users_repo.count_referrals(user["id"])

    await callback.message.edit_text(
        f"👤 ПРОФИЛЬ: {username} / ID: {callback.from_user.id}\n\n"
        "💎 ПОДПИСКА\n"
        f"🏅 Подписка: {sub_status}\n"
        f"⏳ Осталось: {left}\n"
        f"📅 Приобретено месяцев: {months_total}\n\n"
        "💼 ФИНАНСЫ\n"
        f"💳 Основной баланс: {user['balance']} RUB\n"
        f"👥 Рефералов: {invited}\n"
        "💰 Заработано: 0.00 RUB\n\n"
        "📢 Новости\n"
        "🆘 Поддержка",
        reply_markup=profile_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile_topup")
async def topup_open(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileState.waiting_topup_amount)
    await callback.message.edit_text(
        "💰 Пополнение баланса\n\n"
        "Введите сумму пополнения в рублях:\n\n"
        "🔹 Минимум: 10 RUB\n"
        "🔹 Максимум: 100 000 RUB",
        reply_markup=topup_keyboard(),
    )
    await callback.answer()


@router.message(ProfileState.waiting_topup_amount)
async def topup_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Пополнение временно недоступно.")


@router.callback_query(F.data == "profile_promo")
async def promo_open(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileState.waiting_promo_code)
    await callback.message.edit_text(
        "🎁 Активация бонусного промокода\n\nВведите ваш универсальный промокод:",
        reply_markup=promo_keyboard(),
    )
    await callback.answer()


@router.message(ProfileState.waiting_promo_code)
async def promo_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Промокод принят.")


@router.callback_query(F.data == "profile_ref")
async def referral_open(callback: CallbackQuery, db: Database) -> None:
    users_repo = UsersRepository(db)
    user = await users_repo.get_or_create(callback.from_user.id)
    invited = await users_repo.count_referrals(user["id"])
    me = await callback.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    await callback.message.edit_text(
        "🌟 Реферальная программа\n\n"
        "Приглашайте друзей и получайте бонусы! 💸\n\n"
        "💎 Ваша награда:\n"
        "• Вы зарабатываете 20% от каждой покупки ваших друзей\n\n"
        "🎁 Бонус другу:\n"
        "• Скидка 5% на первую покупку\n\n"
        "📊 Статистика:\n"
        f"👤 Приглашено: {invited}\n"
        "💰 Заработано: 0.00 RUB\n\n"
        "🔗 Реферальная ссылка:\n"
        f"{link}",
        reply_markup=referral_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "ref_share")
async def referral_share(callback: CallbackQuery) -> None:
    me = await callback.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    await callback.message.answer(link, disable_web_page_preview=True)
    await callback.answer()
