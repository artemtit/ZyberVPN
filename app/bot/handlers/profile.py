from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape

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
from app.bot.states.promo import PromoState
from app.bot.states.purchase import ProfileState
from app.config import Settings
from app.db.database import Database
from app.repositories.promo import PromoRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, ensure_user_access
from app.services.promo import validate_promo

router = Router()
logger = logging.getLogger(__name__)
PROMO_CONNECT_INSTRUCTION = (
    "📋 Инструкция подключения:\n"
    "1. Установите приложение v2rayNG / v2rayN / Shadowrocket\n"
    "2. Откройте приложение\n"
    "3. Выберите импорт из буфера обмена\n"
    "4. Вставьте ключ\n"
    "5. Подключитесь"
)


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
    await state.set_state(PromoState.waiting_code)
    await callback.message.edit_text(
        "🎁 Введите промокод",
        reply_markup=promo_keyboard(),
    )
    await callback.answer()


@router.message(PromoState.waiting_code)
async def promo_input(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer("❌ Промокод не найден")
        return

    users_repo = UsersRepository(db)
    promo_repo = PromoRepository()
    tg_id = message.from_user.id

    supabase_user = await users_repo.get_by_tg_id(tg_id)
    if supabase_user and bool(supabase_user.get("promo_used")):
        await state.clear()
        await message.answer("❌ Промокод уже использован")
        return

    validation = await validate_promo(code, promo_repo)
    if not validation.ok:
        await state.clear()
        if validation.error == "expired":
            await message.answer("❌ Срок действия истёк")
            return
        if validation.error in {"max_uses_reached"}:
            await message.answer("❌ Промокод уже использован")
            return
        await message.answer("❌ Промокод не найден")
        return

    promo = validation.promo or {}
    days = int(promo.get("days") or 30)
    activated_at = datetime.now(timezone.utc).isoformat()
    new_expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    updated = await users_repo.set_expiry(
        tg_id=tg_id,
        expires_at=new_expiry,
        is_active=True,
        plan="promo",
        promo_used=True,
        last_activated_at=activated_at,
    )
    if not updated:
        local_user = await users_repo.get_or_create(tg_id)
        sub_token = await users_repo.ensure_sub_token(local_user["id"])
        created = await users_repo.create(
            tg_id=tg_id,
            vpn_key="",
            sub_token=sub_token,
            expires_at=new_expiry,
            is_active=True,
            plan="promo",
            last_activated_at=activated_at,
        )
        if not created:
            logger.error("Promo activation failed: cannot create/update supabase user tg_id=%s", tg_id)
            await state.clear()
            await message.answer("❌ Не удалось активировать промокод, попробуйте позже")
            return
        await users_repo.update_promo_used(tg_id, True)

    usage = await promo_repo.increment_usage(code)
    if usage:
        max_uses = usage.get("max_uses")
        used_count = int(usage.get("used_count") or 0)
        if max_uses is not None and used_count >= int(max_uses):
            await promo_repo.deactivate(code)

    await state.clear()
    try:
        access_user = await ensure_user_access(tg_id=tg_id, db=db, settings=settings, require_active=True)
    except AccessEnsureError:
        logger.exception("Promo access bootstrap failed for tg_id=%s", tg_id)
        await message.answer(f"🎉 Промокод активирован! Подписка на {days} дней")
        await message.answer("⚠️ Подписка активна, но создать VPN-ключ сейчас не удалось. Попробуйте позже в разделе «Мои ключи».")
        return

    await message.answer(f"🎉 Промокод активирован! Подписка на {days} дней")
    vpn_key = (access_user or {}).get("vpn_key")
    if vpn_key:
        await message.answer(f"🔑 Ваш ключ:\n<code>{escape(vpn_key)}</code>")
        await message.answer(PROMO_CONNECT_INSTRUCTION)


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
