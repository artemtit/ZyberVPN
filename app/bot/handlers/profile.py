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
    auto_renew_active_keyboard,
    auto_renew_confirm_keyboard,
    auto_renew_keys_keyboard,
    payment_success_keyboard,
    profile_keyboard,
    promo_apply_target_keyboard,
    promo_keyboard,
    referral_keyboard,
    subscription_info_keyboard,
    topup_keyboard,
    topup_payment_keyboard,
    topup_platega_keyboard,
    topup_stars_keyboard,
)

try:
    from app.services.platega import PlategaClient, PlategaError
    _PLATEGA_AVAILABLE = True
except ImportError:
    _PLATEGA_AVAILABLE = False
from app.bot.keyboards.main import get_main_menu_keyboard
from app.bot.states.promo import PromoState
from app.utils.tg import photo_to_text
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
_MAX_TRACKED_USERS = 5000


def _check_promo_rate_limit(tg_id: int) -> bool:
    now = time.time()
    attempts = [t for t in _promo_attempts.get(tg_id, []) if now - t < WINDOW_SECONDS]
    if not attempts:
        _promo_attempts.pop(tg_id, None)
    if len(attempts) >= MAX_ATTEMPTS:
        _promo_attempts[tg_id] = attempts
        return False
    attempts.append(now)
    _promo_attempts[tg_id] = attempts
    # Evict oldest entries when dict grows too large.
    if len(_promo_attempts) > _MAX_TRACKED_USERS:
        oldest = min(_promo_attempts, key=lambda k: min(_promo_attempts[k], default=now))
        _promo_attempts.pop(oldest, None)
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
    auto_renew_key_id = int((full_user or {}).get("auto_renew_stars_key_id") or 0)

    news_url = "https://t.me/ZyberVPN_News"
    support_url = "https://t.me/ZyberVPN_Support_bot"

    await photo_to_text(
        callback.message,
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
        reply_markup=profile_keyboard(auto_renew_active=bool(auto_renew_key_id)),
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


_TOPUP_MIN = 50
_TOPUP_MAX = 10000


def _is_valid_topup_amount(amount: int) -> bool:
    return _TOPUP_MIN <= amount <= _TOPUP_MAX


def _topup_payment_text(rub_amount: int, stars_rate: float) -> str:
    import math
    stars = math.ceil(rub_amount / stars_rate)
    return (
        f"💰 Пополнение баланса: <b>{rub_amount} ₽</b>\n"
        f"⭐ Стоимость в Stars: <b>{stars}</b>\n\n"
        "Выберите способ оплаты:"
    )


@router.callback_query(F.data == "profile_topup")
async def topup_open(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ProfileState.waiting_topup_input)
    await callback.message.edit_text(
        "💰 <b>Пополнение баланса</b>\n\n"
        "Выберите сумму из списка или введите свою от <b>50</b> до <b>10 000 ₽</b>:",
        parse_mode="HTML",
        reply_markup=topup_keyboard(),
    )
    await callback.answer()


@router.message(ProfileState.waiting_topup_input)
async def topup_input_amount(message: Message, state: FSMContext, settings: Settings) -> None:
    text = (message.text or "").strip().replace(" ", "").replace(" ", "")
    try:
        rub_amount = int(float(text.replace(",", ".")))
    except (ValueError, OverflowError):
        await message.answer(f"Введите целое число от {_TOPUP_MIN} до {_TOPUP_MAX}")
        return
    if not _is_valid_topup_amount(rub_amount):
        await message.answer(f"Сумма должна быть от {_TOPUP_MIN} до {_TOPUP_MAX} ₽. Попробуйте снова:")
        return
    await state.update_data(topup_rub_amount=rub_amount)
    await state.set_state(ProfileState.waiting_topup_amount)
    platega_on = bool(settings.platega_merchant_id and settings.platega_api_key)
    platega_crypto_on = platega_on and bool(settings.platega_crypto_method)
    await message.answer(
        _topup_payment_text(rub_amount, settings.stars_rate),
        parse_mode="HTML",
        reply_markup=topup_payment_keyboard(platega_on, platega_crypto_on),
    )


@router.callback_query(F.data.startswith("topup_rub:"))
async def topup_select_amount(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    raw = callback.data.split(":", 1)[1]
    try:
        rub_amount = int(raw)
    except ValueError:
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    if not _is_valid_topup_amount(rub_amount):
        await callback.answer(f"Сумма должна быть от {_TOPUP_MIN} до {_TOPUP_MAX} ₽", show_alert=True)
        return

    await state.update_data(topup_rub_amount=rub_amount)
    await state.set_state(ProfileState.waiting_topup_amount)

    platega_on = bool(settings.platega_merchant_id and settings.platega_api_key)
    platega_crypto_on = platega_on and bool(settings.platega_crypto_method)

    await callback.message.edit_text(
        _topup_payment_text(rub_amount, settings.stars_rate),
        parse_mode="HTML",
        reply_markup=topup_payment_keyboard(platega_on, platega_crypto_on),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("topup_method_back:"))
async def topup_method_back(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    raw = callback.data.split(":", 1)[1]
    try:
        rub_amount = int(raw)
    except ValueError:
        rub_amount = 0
    if not _is_valid_topup_amount(rub_amount):
        await callback.answer("Сессия истекла, выберите сумму заново", show_alert=True)
        return

    await state.update_data(topup_rub_amount=rub_amount)
    await state.set_state(ProfileState.waiting_topup_amount)

    platega_on = bool(settings.platega_merchant_id and settings.platega_api_key)
    platega_crypto_on = platega_on and bool(settings.platega_crypto_method)

    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        _topup_payment_text(rub_amount, settings.stars_rate),
        parse_mode="HTML",
        reply_markup=topup_payment_keyboard(platega_on, platega_crypto_on),
    )
    await callback.answer()


@router.callback_query(F.data == "topup_pay:stars")
async def topup_pay_stars(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    import math
    from app.services.payments import generate_payload
    from aiogram.types import LabeledPrice

    data = await state.get_data()
    rub_amount = int(data.get("topup_rub_amount") or 0)
    if not _is_valid_topup_amount(rub_amount):
        await callback.answer("Сессия истекла, выберите сумму заново", show_alert=True)
        return

    stars_count = math.ceil(rub_amount / settings.stars_rate)

    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)
    await users_repo.get_or_create(callback.from_user.id)
    payload = generate_payload(callback.from_user.id, f"topup{rub_amount}")
    idem_key = f"topup-create:{callback.from_user.id}:{rub_amount}:{payload}"
    await payments_repo.create_pending(
        tg_id=callback.from_user.id,
        amount=rub_amount,
        tariff_code=f"topup{rub_amount}",
        email=None,
        payload=payload,
        idempotency_key=idem_key,
        purchase_type="topup",
    )
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer_invoice(
        title="ZyberVPN — Пополнение баланса",
        description=f"Пополнение баланса на {rub_amount} ₽",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"Баланс +{rub_amount} ₽", amount=stars_count)],
        provider_token="",
        reply_markup=topup_stars_keyboard(rub_amount, stars_count),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"topup_pay:platega", "topup_pay:platega_crypto"}))
async def topup_pay_platega(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    from app.services.payments import generate_payload

    if not _PLATEGA_AVAILABLE:
        await callback.answer("Platega недоступна", show_alert=True)
        return

    merchant_id = settings.platega_merchant_id
    api_key = settings.platega_api_key
    if not merchant_id or not api_key:
        await callback.answer("Platega не настроена", show_alert=True)
        return

    is_crypto = callback.data == "topup_pay:platega_crypto"
    if is_crypto and not settings.platega_crypto_method:
        await callback.answer("Крипто-оплата не настроена", show_alert=True)
        return

    data = await state.get_data()
    rub_amount = int(data.get("topup_rub_amount") or 0)
    if not _is_valid_topup_amount(rub_amount):
        await callback.answer("Сессия истекла, выберите сумму заново", show_alert=True)
        return

    payment_method = settings.platega_crypto_method if is_crypto else 2
    method_label = "Криптовалюта" if is_crypto else "СБП / QR"

    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)
    await users_repo.get_or_create(callback.from_user.id)

    return_url = f"https://t.me/{settings.bot_username}"
    idem_key = f"platega-topup-{payment_method}:{callback.from_user.id}:{rub_amount}"

    try:
        client = PlategaClient(
            merchant_id=merchant_id,
            api_key=api_key,
            return_url=return_url,
            failed_url=return_url,
        )
        result = await client.create_payment(
            amount=rub_amount,
            description=(
                f"Пополнение баланса {rub_amount} ₽ "
                f"(ID: {callback.from_user.id}, "
                f"{'@' + callback.from_user.username if callback.from_user.username else callback.from_user.full_name})"
            ),
            internal_payload=idem_key,
            payment_method=payment_method,
        )
        transaction_id = result["transaction_id"]
        redirect_url = result["redirect_url"]

        await payments_repo.create_pending(
            tg_id=callback.from_user.id,
            amount=rub_amount,
            tariff_code=f"topup{rub_amount}",
            email=None,
            payload=transaction_id,
            idempotency_key=idem_key,
            purchase_type="topup",
        )
        await state.clear()
    except PlategaError as exc:
        logger.exception("Platega topup creation failed tg_id=%s error=%s", callback.from_user.id, exc)
        await callback.answer("Platega временно недоступна. Попробуйте позже.", show_alert=True)
        return
    except Exception:
        logger.exception("Platega topup unexpected error tg_id=%s", callback.from_user.id)
        await callback.answer("Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
        return

    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"💳 <b>Оплата через {method_label} (Platega)</b>\n\n"
        f"💰 Сумма: <b>{rub_amount} ₽</b>\n\n"
        "Нажмите кнопку ниже для перехода к оплате.\n"
        "После оплаты баланс пополнится автоматически.",
        parse_mode="HTML",
        reply_markup=topup_platega_keyboard(redirect_url, rub_amount),
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
    provisioned_key_id = 0
    try:
        vpn_result = await vpn_idem.execute("vpn_provision", vpn_idem_key, _provision_promo_vpn)
        sub_token = str(vpn_result.get("key_sub_token") or "")
        provisioned_key_id = int(vpn_result.get("key_id") or 0)
        if provisioned_key_id and not sub_token:
            sub_token = await KeysRepository(db).ensure_sub_token(provisioned_key_id, tg_id)
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
            reply_markup=payment_success_keyboard(sub_url, key_id=provisioned_key_id),
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
    payments_repo = PaymentsRepository(db)
    await users_repo.get_or_create(callback.from_user.id)
    invited = await users_repo.count_referrals(callback.from_user.id)

    referral_tg_ids = await users_repo.list_referral_tg_ids(callback.from_user.id)
    total_paid = await payments_repo.sum_paid_for_tg_ids(referral_tg_ids)
    earned = total_paid * settings.referral_bonus_percent // 100

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
        f"💰 Заработано: {earned} RUB\n\n"
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


# ──────────────────────────────────────────
# Авто-продление Stars
# ──────────────────────────────────────────

async def _send_autorenew_invoice(message, tg_id: int, key_id: int, stars: int) -> None:
    from aiogram.types import LabeledPrice
    await message.answer_invoice(
        title="ZyberVPN — Авто-продление",
        description=f"Ежемесячное продление VPN-ключа #{key_id} (30 дней)",
        payload=f"sub:m1:{tg_id}:{key_id}",
        currency="XTR",
        prices=[LabeledPrice(label="1 месяц VPN", amount=stars)],
        provider_token="",
        subscription_period=2592000,
        reply_markup=auto_renew_confirm_keyboard(stars),
    )


@router.callback_query(F.data == "profile_autorenew")
async def profile_autorenew(callback: CallbackQuery, db: Database) -> None:
    from app.services.plans import get_plan_by_tariff_code
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)

    user = await users_repo.get_by_tg_id(callback.from_user.id)
    auto_renew_key_id = int((user or {}).get("auto_renew_stars_key_id") or 0)
    active_keys = [k for k in await keys_repo.list_by_user(callback.from_user.id) if not k.get("disabled_at")]

    if auto_renew_key_id:
        matched = next((k for k in active_keys if int(k.get("id") or 0) == auto_renew_key_id), None)
        exp_raw = (matched or {}).get("expires_at")
        key_line = f"🔑 Ключ #{auto_renew_key_id}"
        if exp_raw:
            try:
                exp_dt = parse_iso_utc(exp_raw)
                days_left = max(0, (exp_dt - utc_now()).days)
                key_line = f"🔑 Ключ #{auto_renew_key_id} — до {to_moscow(exp_dt).strftime('%d.%m.%Y')} ({days_left} дн.)"
            except Exception:
                pass
        await callback.message.edit_text(
            "🔄 <b>Авто-продление включено</b>\n\n"
            f"{key_line}\n\n"
            "⭐ Telegram автоматически списывает Stars каждые 30 дней.\n\n"
            "Чтобы отменить подписку, перейдите в:\n"
            "<b>Telegram → Настройки → Конфиденциальность → Платежи → Подписки</b>",
            reply_markup=auto_renew_active_keyboard(),
        )
        await callback.answer()
        return

    if not active_keys:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        await callback.message.edit_text(
            "🔄 <b>Авто-продление</b>\n\nУ вас нет активных ключей.\nСначала купите подписку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_open")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile")],
            ]),
        )
        await callback.answer()
        return

    plan = get_plan_by_tariff_code("m1")
    stars = plan["price_stars"] if plan else 39

    if len(active_keys) == 1:
        key_id = int(active_keys[0].get("id") or 0)
        # Redirect to the confirm handler so invoice is sent cleanly
        try:
            await callback.message.delete()
        except Exception:
            pass
        await _send_autorenew_invoice(callback.message, callback.from_user.id, key_id, stars)
    else:
        await callback.message.edit_text(
            f"🔄 <b>Авто-продление ⭐</b>\n\n"
            f"Стоимость: <b>{stars} Stars/мес.</b>\n\n"
            "Выберите ключ для авто-продления:",
            reply_markup=auto_renew_keys_keyboard(active_keys),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("autorenew_confirm:"))
async def autorenew_confirm(callback: CallbackQuery, db: Database) -> None:
    from app.services.plans import get_plan_by_tariff_code
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return

    keys_repo = KeysRepository(db)
    key_row = await keys_repo.get_by_id_for_user(key_id, callback.from_user.id)
    if not key_row:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    plan = get_plan_by_tariff_code("m1")
    stars = plan["price_stars"] if plan else 39

    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_autorenew_invoice(callback.message, callback.from_user.id, key_id, stars)
    await callback.answer()


@router.callback_query(F.data == "autorenew_disable")
async def autorenew_disable(callback: CallbackQuery, db: Database) -> None:
    users_repo = UsersRepository(db)
    await users_repo.set_auto_renew(callback.from_user.id, None)
    await callback.message.edit_text(
        "🔄 <b>Авто-продление отключено</b>\n\n"
        "Запись об авто-продлении удалена.\n\n"
        "Если Telegram-подписка ещё активна, отмените её вручную:\n"
        "<b>Telegram → Настройки → Конфиденциальность → Платежи → Подписки</b>",
        reply_markup=auto_renew_active_keyboard(),
    )
    await callback.answer("Отключено")

