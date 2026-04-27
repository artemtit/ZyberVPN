from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import (
    payment_success_keyboard,
    profile_keyboard,
    promo_apply_target_keyboard,
    promo_keyboard,
    referral_keyboard,
    subscription_info_keyboard,
    topup_keyboard,
)
from app.bot.states.promo import PromoState
from app.bot.states.purchase import ProfileState
from app.config import Settings
from app.db.database import Database
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.keys import KeysRepository
from app.repositories.promo import PromoRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, build_vpn_manager, ensure_user_access
from app.services.idempotency import IdempotencyService
from app.services.promo import validate_promo
from app.utils.datetime import parse_iso_utc, utc_now

router = Router()
logger = logging.getLogger(__name__)


def _promo_success_text(expires_dt, *, include_status: bool = True) -> str:
    expires_str = expires_dt.strftime("%d.%m.%Y")
    days_remaining = max(0, (expires_dt - utc_now()).days)
    status_line = "📊 Статус: <b>Активна</b>\n\n" if include_status else ""
    return (
        "✅ <b>Промокод успешно активирован!</b>\n\n"
        "📦 <b>Подписка активирована</b>\n"
        f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
        f"{status_line}"
    )


def _format_expiry(raw_value: str | None) -> str:
    if not raw_value:
        return "не задан"
    try:
        dt = parse_iso_utc(raw_value)
    except Exception:
        return str(raw_value)
    return dt.strftime("%d.%m.%Y %H:%M UTC")


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
    invited = await users_repo.count_referrals(callback.from_user.id)

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
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)
    active_sub = await subs_repo.get_active(tg_id)
    has_existing_key = bool(await keys_repo.list_by_user(tg_id))

    if active_sub and has_existing_key:
        await state.set_state(PromoState.waiting_apply_target)
        await state.update_data(promo_code=code, promo_days=days)
        await message.answer(
            "🎁 Промокод найден.\n\n"
            f"Куда зачислить +{days} дней?\n"
            "Если продлить активную подписку, ключ не будет отправлен повторно.",
            reply_markup=promo_apply_target_keyboard(),
        )
        return

    await _apply_promo(
        tg_id=tg_id,
        code=code,
        days=days,
        apply_mode="new",
        db=db,
        settings=settings,
        users_repo=users_repo,
        promo_repo=promo_repo,
        state=state,
        reply=message.answer,
    )


@router.callback_query(PromoState.waiting_apply_target, F.data.startswith("promo_apply:"))
async def promo_apply_choice(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not callback.message:
        await callback.answer()
        return
    apply_mode = callback.data.split(":", 1)[1]
    if apply_mode not in {"active", "new"}:
        await callback.answer("Некорректный выбор", show_alert=True)
        return

    data = await state.get_data()
    code = str(data.get("promo_code") or "").strip()
    days = int(data.get("promo_days") or 0)
    if not code or days <= 0:
        await state.clear()
        await callback.message.answer("Сессия промокода истекла. Введите промокод снова.")
        await callback.answer()
        return

    users_repo = UsersRepository(db)
    promo_repo = PromoRepository()
    await _apply_promo(
        tg_id=callback.from_user.id,
        code=code,
        days=days,
        apply_mode=apply_mode,
        db=db,
        settings=settings,
        users_repo=users_repo,
        promo_repo=promo_repo,
        state=state,
        reply=callback.message.answer,
    )
    await callback.answer()


async def _apply_promo(
    *,
    tg_id: int,
    code: str,
    days: int,
    apply_mode: str,
    db: Database,
    settings: Settings,
    users_repo: UsersRepository,
    promo_repo: PromoRepository,
    state: FSMContext,
    reply,
) -> None:
    validation = await validate_promo(code, promo_repo)
    if not validation.ok:
        await state.clear()
        if validation.error == "expired":
            await reply("❌ Срок действия промокода истёк")
            return
        if validation.error in {"max_uses_reached"}:
            await reply("❌ Промокод уже использован")
            return
        await reply("❌ Промокод не найден")
        return

    subs_repo = SubscriptionsRepository(db)
    idem = IdempotencyService(IdempotencyRepository())
    activated_at = utc_now().isoformat()

    async def _activate() -> dict:
        subscription = await subs_repo.create_or_extend_days(tg_id=tg_id, days=days)
        expires_at = str(subscription.get("expires_at") or "")
        if not expires_at:
            raise RuntimeError("promo activation failed: expires_at is empty")

        updated = await users_repo.set_expiry(
            tg_id=tg_id,
            expires_at=expires_at,
            is_active=True,
            plan="promo",
            promo_used=True,
            last_activated_at=activated_at,
        )
        if updated:
            return {"expires_at": expires_at}

        await users_repo.get_or_create(tg_id)
        sub_token = await users_repo.ensure_sub_token(tg_id)
        created = await users_repo.create(
            tg_id=tg_id,
            vpn_key="",
            sub_token=sub_token,
            expires_at=expires_at,
            is_active=True,
            plan="promo",
            last_activated_at=activated_at,
        )
        if not created:
            raise RuntimeError("promo activation failed")
        await users_repo.update_promo_used(tg_id, True)
        return {"expires_at": expires_at}

    try:
        result = await idem.execute("promo_activation", f"promo-activate:{tg_id}:{code.lower()}:{apply_mode}", _activate)
    except Exception:
        logger.error("Promo activation failed: cannot create/update supabase user tg_id=%s", tg_id)
        await state.clear()
        await reply("Promo activation failed, please try again later")
        return

    usage = await promo_repo.increment_usage(code)
    if usage:
        max_uses = usage.get("max_uses")
        used_count = int(usage.get("used_count") or 0)
        if max_uses is not None and used_count >= int(max_uses):
            await promo_repo.deactivate(code)

    await state.clear()
    expires_raw = str((result or {}).get("expires_at") or "")
    expires_dt = parse_iso_utc(expires_raw) if expires_raw else utc_now() + timedelta(days=days)

    try:
        access_user = await ensure_user_access(tg_id=tg_id, db=db, settings=settings, require_active=True)
    except AccessEnsureError:
        logger.exception("Promo access bootstrap failed for tg_id=%s", tg_id)
        if apply_mode == "active":
            await reply(_promo_success_text(expires_dt, include_status=False) + "Подписка продлена.")
        else:
            await reply(
                _promo_success_text(expires_dt)
                + "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
            )
        return

    expiry_ms = int(expires_dt.timestamp() * 1000)
    try:
        manager = build_vpn_manager(db, settings)
        await manager.update_user_expiry(tg_id, expiry_ms)
    except Exception:
        logger.exception("Failed to update XUI expiry after promo tg_id=%s", tg_id)

    if apply_mode == "active":
        await reply(_promo_success_text(expires_dt, include_status=False) + "Подписка продлена.")
        return

    sub_token = str((access_user or {}).get("sub_token") or "")
    sub_url = f"{settings.public_base_url}/sub/{escape(sub_token)}" if sub_token and settings.public_base_url else ""
    if sub_url:
        await reply(
            _promo_success_text(expires_dt)
            + "🔗 <b>Ссылка для подключения:</b>\n"
            f"<code>{sub_url}</code>\n\n"
            "Нажмите «Подключить» чтобы открыть в VPN-клиенте,\n"
            "или «Показать QR» для сканирования.",
            reply_markup=payment_success_keyboard(sub_url),
        )
    else:
        await reply(
            _promo_success_text(expires_dt)
            + "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
        )


@router.callback_query(F.data == "profile_ref")
async def referral_open(callback: CallbackQuery, db: Database) -> None:
    users_repo = UsersRepository(db)
    local_user = await users_repo.get_or_create(callback.from_user.id)
    invited = await users_repo.count_referrals(callback.from_user.id)
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

