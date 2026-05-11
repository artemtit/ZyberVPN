from __future__ import annotations

import logging
import time
from datetime import timedelta
from html import escape
from urllib.parse import quote

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
    topup_back_keyboard,
    topup_keyboard,
)
from app.bot.keyboards.main import get_main_menu_keyboard
from app.bot.states.promo import PromoState
from app.bot.states.purchase import ProfileState
from app.config import Settings
from app.db.database import Database
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.keys import KeysRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.promo import PromoRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, build_vpn_manager, ensure_user_access
from app.services.idempotency import IdempotencyService
from app.services.promo import validate_promo
from app.utils.datetime import parse_iso_utc, to_moscow, utc_now

router = Router()
logger = logging.getLogger(__name__)
_promo_attempts: dict[int, list[float]] = {}
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 3600


def _check_promo_rate_limit(tg_id: int) -> bool:
    now = time.time()
    attempts = [t for t in _promo_attempts.get(tg_id, []) if now - t < WINDOW_SECONDS]
    if not attempts:
        # All previous timestamps are stale — remove the entry to prevent unbounded growth.
        _promo_attempts.pop(tg_id, None)
    if len(attempts) >= MAX_ATTEMPTS:
        _promo_attempts[tg_id] = attempts
        return False
    attempts.append(now)
    _promo_attempts[tg_id] = attempts
    return True


def _promo_success_text(expires_dt, *, include_status: bool = True) -> str:
    expires_str = to_moscow(expires_dt).strftime("%d.%m.%Y")
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
    return to_moscow(dt).strftime("%d.%m.%Y %H:%M") + " МСК"


def _status_text(is_active: bool) -> str:
    return "активна ✅" if is_active else "истекла ❌"


@router.callback_query(F.data == "menu_profile")
async def profile(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    users_repo = UsersRepository(db)
    supabase_user = await users_repo.get_or_create(callback.from_user.id)

    full_user = await users_repo.get_by_tg_id(callback.from_user.id) or supabase_user
    is_active = users_repo.is_user_active(full_user) if full_user else False
    if full_user and not is_active:
        await users_repo.update_status(callback.from_user.id, False)

    username = callback.from_user.username or callback.from_user.full_name
    invited = await users_repo.count_referrals(callback.from_user.id)

    keys_repo = KeysRepository(db)
    user_keys = await keys_repo.list_by_user(callback.from_user.id)
    primary_expiry_raw = user_keys[0].get("expires_at") if user_keys else None
    expires_raw = primary_expiry_raw or (full_user or {}).get("expires_at")

    days_left = 0
    hours_left = 0
    months_count = 0
    if is_active and expires_raw:
        try:
            expires_dt = parse_iso_utc(expires_raw)
            delta = expires_dt - utc_now()
            total_seconds = max(0, int(delta.total_seconds()))
            days_left = total_seconds // 86400
            hours_left = (total_seconds % 86400) // 3600
            months_count = max(1, days_left // 30)
        except Exception:
            pass

    status_line = "Активна ✅" if is_active else "Не активна ❌"
    balance_rub = int((full_user or {}).get("balance") or 0)

    news_url = "https://t.me/ZyberVPN_News"
    support_url = "https://t.me/ZyberVPN_Support_bot"

    await callback.message.edit_text(
        f"👤 ПРОФИЛЬ: {username} / iD: {callback.from_user.id}\n\n"
        "💎 ПОДПИСКА\n"
        f"🛡 Подписка: {status_line}\n"
        f"⏳ Осталось: {days_left} д. {hours_left} ч.\n"
        f"📅 Приобретено месяцев: {months_count}\n\n"
        "💼 ФИНАНСЫ\n"
        f"💳 Баланс: {balance_rub} RUB\n"
        f"🤝 Рефералов: {invited}\n\n"
        f"📄 <a href=\"{news_url}\">Новости</a>\n"
        f"💬 <a href=\"{support_url}\">Поддержка</a>",
        reply_markup=profile_keyboard(),
        disable_web_page_preview=True,
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
    await state.clear()
    await callback.message.edit_text(
        "💰 Пополнение баланса\n\n"
        "Выберите сумму пополнения (1 Star = 1 RUB):",
        reply_markup=topup_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("topup_stars:"))
async def topup_stars_pay(callback: CallbackQuery, db: Database) -> None:
    raw = callback.data.split(":", 1)[1]
    try:
        stars_amount = int(raw)
    except ValueError:
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    if stars_amount not in {100, 300, 500, 1000}:
        await callback.answer("Некорректная сумма", show_alert=True)
        return

    from app.services.payments import generate_payload
    from aiogram.types import LabeledPrice

    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)
    await users_repo.get_or_create(callback.from_user.id)
    payload = generate_payload(callback.from_user.id, f"topup{stars_amount}")
    idem_key = f"topup-create:{callback.from_user.id}:{stars_amount}:{payload}"
    await payments_repo.create_pending(
        tg_id=callback.from_user.id,
        amount=stars_amount,
        tariff_code=f"topup{stars_amount}",
        email=None,
        payload=payload,
        idempotency_key=idem_key,
        purchase_type="topup",
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer_invoice(
        title="ZyberVPN — Пополнение баланса",
        description=f"Пополнение баланса на {stars_amount} RUB",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"Баланс +{stars_amount} RUB", amount=stars_amount)],
        provider_token="",
        reply_markup=topup_back_keyboard(),
    )
    await callback.answer()


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
        await message.answer("❌ Промокод не найден", reply_markup=get_main_menu_keyboard())
        return

    users_repo = UsersRepository(db)
    promo_repo = PromoRepository()
    tg_id = message.from_user.id
    if not _check_promo_rate_limit(tg_id):
        await message.answer("❌ Слишком много попыток. Попробуйте через час.", reply_markup=get_main_menu_keyboard())
        return

    supabase_user = await users_repo.get_by_tg_id(tg_id)
    if supabase_user and bool(supabase_user.get("promo_used")):
        await state.clear()
        await message.answer("❌ Промокод уже использован", reply_markup=get_main_menu_keyboard())
        return

    validation = await validate_promo(code, promo_repo)
    if not validation.ok:
        await state.clear()
        if validation.error == "expired":
            await message.answer("❌ Срок действия истёк", reply_markup=get_main_menu_keyboard())
            return
        if validation.error in {"max_uses_reached"}:
            await message.answer("❌ Промокод уже использован", reply_markup=get_main_menu_keyboard())
            return
        await message.answer("❌ Промокод не найден", reply_markup=get_main_menu_keyboard())
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
        # Re-check promo_used inside idempotency to guard against concurrent double-activation.
        current_user = await users_repo.get_by_tg_id(tg_id)
        if current_user and bool(current_user.get("promo_used")):
            return {"expires_at": str(current_user.get("expires_at") or "")}

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
        if not updated:
            await users_repo.get_or_create(tg_id)
            sub_token_val = await users_repo.ensure_sub_token(tg_id)
            created = await users_repo.create(
                tg_id=tg_id,
                vpn_key="",
                sub_token=sub_token_val,
                expires_at=expires_at,
                is_active=True,
                plan="promo",
                last_activated_at=activated_at,
            )
            if not created:
                raise RuntimeError("promo activation failed")
            await users_repo.update_promo_used(tg_id, True)

        # Increment usage inside idempotency so retries don't double-count.
        usage = await promo_repo.increment_usage(code)
        if usage:
            max_uses = usage.get("max_uses")
            used_count = int(usage.get("used_count") or 0)
            if max_uses is not None and used_count >= int(max_uses):
                await promo_repo.deactivate(code)

        return {"expires_at": expires_at}

    try:
        result = await idem.execute("promo_activation", f"promo-activate:{tg_id}:{code.lower()}:{apply_mode}", _activate)
    except Exception:
        logger.error("Promo activation failed: cannot create/update supabase user tg_id=%s", tg_id)
        await state.clear()
        await reply("Promo activation failed, please try again later")
        return

    await state.clear()
    expires_raw = str((result or {}).get("expires_at") or "")
    expires_dt = parse_iso_utc(expires_raw) if expires_raw else utc_now() + timedelta(days=days)
    expiry_ms = int(expires_dt.timestamp() * 1000)

    if apply_mode == "active":
        try:
            manager = build_vpn_manager(db, settings)
            keys_repo = KeysRepository(db)
            for key_row in await keys_repo.list_by_user(tg_id):
                key_id = key_row.get("id")
                if key_id is None:
                    continue
                key_traffic_gb = int((key_row or {}).get("traffic_limit_gb") or 0) or None
                await manager.update_user_expiry(tg_id, expiry_ms, key_id=int(key_id), traffic_limit_gb=key_traffic_gb)
        except Exception:
            logger.exception("Failed to update XUI expiry after promo tg_id=%s", tg_id)
        await reply(_promo_success_text(expires_dt, include_status=False) + "Подписка продлена.")
        return

    # apply_mode == "new": provision VPN key wrapped in its own idempotency to prevent
    # duplicate key creation if the callback fires twice (double-tap, Telegram retry).
    vpn_idem_key = f"vpn-provision:promo:{tg_id}:{code.lower()}"
    vpn_idem = IdempotencyService(IdempotencyRepository())

    async def _provision_promo_vpn() -> dict:
        au = await ensure_user_access(
            tg_id=tg_id,
            db=db,
            settings=settings,
            require_active=True,
            force_new_key=True,
            action="create",
        )
        return {
            "vpn_key": str(au.get("vpn_key") or ""),
            "key_sub_token": str(au.get("key_sub_token") or ""),
            "key_id": int(au.get("key_id") or 0),
        }

    sub_token = ""
    try:
        vpn_result = await vpn_idem.execute("vpn_provision", vpn_idem_key, _provision_promo_vpn)
        sub_token = str(vpn_result.get("key_sub_token") or "")
    except AccessEnsureError:
        logger.exception("Promo access bootstrap failed for tg_id=%s", tg_id)
        await reply(
            _promo_success_text(expires_dt)
            + "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
        )
        return
    except Exception:
        logger.exception("Promo VPN provisioning idempotency failed tg_id=%s", tg_id)
        await reply(
            _promo_success_text(expires_dt)
            + "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
        )
        return

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


def _build_share_url(bot_username: str, tg_id: int) -> str:
    ref_link = f"https://t.me/{bot_username}?start=ref_{tg_id}"
    share_text = "Попробуй ZyberVPN — быстрый и надёжный VPN-сервис 🚀"
    return f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_text)}"


@router.callback_query(F.data == "profile_ref")
async def referral_open(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    users_repo = UsersRepository(db)
    local_user = await users_repo.get_or_create(callback.from_user.id)
    invited = await users_repo.count_referrals(callback.from_user.id)
    me = await callback.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    share_url = _build_share_url(me.username, callback.from_user.id)
    await callback.message.edit_text(
        "🌟 Реферальная программа\n\n"
        "Приглашайте друзей и получайте бонусы! 💰\n\n"
        "💎 Ваша награда:\n"
        f"• Вы зарабатываете {settings.referral_bonus_percent}% от каждой покупки ваших друзей\n\n"
        "🎁 Бонус другу:\n"
        "• Скидка 5% на первую покупку\n\n"
        "📊 Статистика:\n"
        f"👤 Приглашено: {invited}\n"
        "💰 Заработано: 0.00 RUB\n\n"
        "🔗 Реферальная ссылка:\n"
        f"{ref_link}",
        reply_markup=referral_keyboard(share_url),
    )
    await callback.answer()


@router.callback_query(F.data == "ref_share")
async def referral_share(callback: CallbackQuery) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    me = await callback.bot.get_me()
    share_url = _build_share_url(me.username, callback.from_user.id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📤 Поделиться в Telegram", url=share_url)]]
    )
    await callback.message.answer("Поделитесь ссылкой с друзьями:", reply_markup=keyboard)
    await callback.answer()

