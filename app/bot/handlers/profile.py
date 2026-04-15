from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import (
    profile_keyboard,
    promo_keyboard,
    referral_keyboard,
    subscription_info_keyboard,
    topup_keyboard,
)
from app.bot.states.purchase import ProfileState
from app.db.database import Database
from app.repositories.users import UsersRepository

router = Router()


def _format_expiry(raw_value: str | None) -> str:
    if not raw_value:
        return "не задан"
    try:
        dt = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except Exception:
        return str(raw_value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _status_text(is_active: bool) -> str:
    return "активна ✅" if is_active else "истекла ❌"


@router.callback_query(F.data == "menu_profile")
async def profile(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    users_repo = UsersRepository(db)
    local_user = await users_repo.get_or_create(callback.from_user.id)
    supabase_user = await users_repo.get_by_tg_id(callback.from_user.id)

    is_active = users_repo.is_user_active(supabase_user) if supabase_user else False
    if supabase_user and not is_active:
        await users_repo.update_status(callback.from_user.id, False)

    username = callback.from_user.username or callback.from_user.full_name
    invited = await users_repo.count_referrals(local_user["id"])

    await callback.message.edit_text(
        f"👤 ПРОФИЛЬ: {username} / ID: {callback.from_user.id}\n\n"
        "💎 ПОДПИСКА\n"
        f"🏅 Статус: {_status_text(is_active)}\n"
        f"📅 Действует до: {_format_expiry((supabase_user or {}).get('expires_at'))}\n"
        f"📦 План: {(supabase_user or {}).get('plan') or 'не задан'}\n\n"
        "💼 ФИНАНСЫ\n"
        f"💳 Основной баланс: {local_user['balance']} RUB\n"
        f"👥 Рефералов: {invited}\n"
        "💰 Заработано: 0.00 RUB",
        reply_markup=profile_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile_subscription")
async def profile_subscription(callback: CallbackQuery, db: Database) -> None:
    users_repo = UsersRepository(db)
    supabase_user = await users_repo.get_by_tg_id(callback.from_user.id)

    if not supabase_user:
        await callback.message.edit_text(
            "👤 Моя подписка\n\nПодписка не найдена. Нажмите «🔌 Подключиться», чтобы создать триал.",
            reply_markup=subscription_info_keyboard(),
        )
        await callback.answer()
        return

    is_active = users_repo.is_user_active(supabase_user)
    if not is_active:
        await users_repo.update_status(callback.from_user.id, False)

    await callback.message.edit_text(
        "👤 Моя подписка\n\n"
        f"Статус: {_status_text(is_active)}\n"
        f"Срок действия: {_format_expiry(supabase_user.get('expires_at'))}\n"
        f"План: {supabase_user.get('plan') or 'не задан'}",
        reply_markup=subscription_info_keyboard(),
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
    local_user = await users_repo.get_or_create(callback.from_user.id)
    invited = await users_repo.count_referrals(local_user["id"])
    me = await callback.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    await callback.message.edit_text(
        "🌟 Реферальная программа\n\n"
        "Приглашайте друзей и получайте бонусы! 💰\n\n"
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