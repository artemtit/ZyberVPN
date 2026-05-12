from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice
import logging

from app.bot.keyboards.inline import main_menu_keyboard, payment_back_keyboard, payment_keyboard, payment_success_keyboard, stars_back_keyboard, tariffs_keyboard
from app.bot.keyboards.main import get_main_menu_keyboard
from app.bot.states.purchase import PurchaseState
from app.config import Settings
from app.db.database import Database
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.keys import KeysRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, build_vpn_manager, ensure_user_access
from app.services.idempotency import IdempotencyService
from app.services.payments import generate_payload
from app.services.plans import get_plan_by_id, get_plan_by_tariff_code
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from html import escape

from app.utils.datetime import add_months, parse_iso_utc, to_moscow, utc_now

try:
    from app.services.platega import PlategaClient, PlategaError
    _PLATEGA_AVAILABLE = True
except ImportError:
    _PLATEGA_AVAILABLE = False

router = Router()
logger = logging.getLogger(__name__)


def _require_renew_key_id(raw: object) -> int:
    if raw is None:
        raise ValueError("renew_key_id is required for renewal")
    key_id = int(raw)
    if key_id <= 0:
        raise ValueError("renew_key_id must be positive")
    return key_id


def _selected_plan(data: dict) -> dict | None:
    plan_id = data.get("plan_id")
    if plan_id is not None:
        try:
            return get_plan_by_id(int(plan_id))
        except (TypeError, ValueError):
            return None
    tariff_code = data.get("tariff_code")
    if tariff_code:
        return get_plan_by_tariff_code(str(tariff_code))
    return None


async def _repair_provision_result(
    keys_repo: KeysRepository,
    tg_id: int,
    link: str,
    key_id: int,
    sub_token: str,
) -> tuple[str, int, str]:
    key_row = None
    if key_id > 0:
        key_row = await keys_repo.get_by_id_for_user(key_id, tg_id)
    if not key_row and link:
        user_keys = await keys_repo.list_by_user(tg_id)
        key_row = next((k for k in user_keys if str(k.get("key") or "") == link), None)
    if not key_row:
        user_keys = await keys_repo.list_by_user(tg_id)
        valid_keys = [k for k in user_keys if str(k.get("key") or "").startswith("vless://")]
        key_row = valid_keys[-1] if valid_keys else None
    if key_row:
        key_id = int(key_row.get("id") or key_id or 0)
        link = link or str(key_row.get("key") or "")
        sub_token = sub_token or str(key_row.get("sub_token") or "")
    if key_id > 0 and not sub_token:
        sub_token = await keys_repo.ensure_sub_token(key_id, tg_id)
    return link, key_id, sub_token


@router.callback_query(F.data == "buy_open")
async def buy_new_key(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(purchase_type="new", renew_key_id=None)
    await callback.message.edit_text(
        "💳 Выбор тарифа: Новый ключ\n\nВыберите подходящий период подписки:",
        reply_markup=tariffs_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key_renew:"))
async def buy_renew_key(callback: CallbackQuery, state: FSMContext) -> None:
    key_id = int(callback.data.split(":")[1])
    await state.clear()
    await state.update_data(purchase_type="renewal", renew_key_id=key_id)
    await callback.message.edit_text(
        f"🔄 Продление ключа #{key_id}\n\nВыберите подходящий период подписки:",
        reply_markup=tariffs_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tariff:"))
async def choose_tariff(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    tariff_code = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_code)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    await state.update_data(tariff_code=tariff_code, email=None)
    await state.set_state(PurchaseState.waiting_payment)
    plan = get_plan_by_tariff_code(tariff_code)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    platega_on = bool(getattr(settings, "platega_merchant_id", "") and getattr(settings, "platega_api_key", ""))
    platega_crypto_on = platega_on and bool(getattr(settings, "platega_crypto_method", 0))
    is_admin = callback.from_user.id in settings.admin_ids
    await callback.message.edit_text(
        f"💰 К оплате: {float(plan['price_rub']):.2f} RUB\n\nВыберите удобный способ оплаты:",
        reply_markup=payment_keyboard(platega_enabled=platega_on, platega_crypto_enabled=platega_crypto_on, show_test_pay=is_admin),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("buy_plan:"))
async def choose_plan(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    raw = callback.data.split(":", 1)[1]
    try:
        plan_id = int(raw)
    except ValueError:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    plan = get_plan_by_id(plan_id)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    await state.update_data(plan_id=plan_id, tariff_code=plan["tariff_code"], email=None)
    await state.set_state(PurchaseState.waiting_payment)
    platega_on = bool(getattr(settings, "platega_merchant_id", "") and getattr(settings, "platega_api_key", ""))
    platega_crypto_on = platega_on and bool(getattr(settings, "platega_crypto_method", 0))
    is_admin = callback.from_user.id in settings.admin_ids
    await callback.message.edit_text(
        f"💰 К оплате: {float(plan['price_rub']):.2f} RUB\n\nВыберите удобный способ оплаты:",
        reply_markup=payment_keyboard(platega_enabled=platega_on, platega_crypto_enabled=platega_crypto_on, show_test_pay=is_admin),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"pay:sbp", "pay:crypto"}))
async def pay_other_methods(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if callback.data != "pay:sbp":
        await callback.answer("Метод временно недоступен", show_alert=True)
        return
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Только для администраторов", show_alert=True)
        return

    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    if not tariff_code:
        await callback.answer("Сначала выберите тариф", show_alert=True)
        return
    plan = _selected_plan(data)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    email = data.get("email")
    purchase_type = str(data.get("purchase_type") or "new")
    renew_key_id = data.get("renew_key_id")

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    payments_repo = PaymentsRepository(db)
    subs_repo = SubscriptionsRepository(db)
    idem = IdempotencyService(IdempotencyRepository())
    try:
        if purchase_type == "renewal":
            renew_key_id = _require_renew_key_id(renew_key_id)
            key_row = await keys_repo.get_by_id_for_user(renew_key_id, callback.from_user.id)
            if not key_row:
                await callback.answer("Ключ для продления не найден", show_alert=True)
                return
        await users_repo.get_or_create(callback.from_user.id)
        payload = generate_payload(callback.from_user.id, tariff_code)
        idempotency_key = (
            f"payment-create:{callback.from_user.id}:{tariff_code}:{str(email or '').lower()}:{purchase_type}:{renew_key_id}"
        )
        payment = await payments_repo.create_pending(
            tg_id=callback.from_user.id,
            amount=int(plan["price_rub"]),
            tariff_code=tariff_code,
            email=email,
            payload=payload,
            idempotency_key=idempotency_key,
            purchase_type=purchase_type,
            renew_key_id=int(renew_key_id) if renew_key_id is not None else None,
        )
        payload = str(payment.get("payload") or payload)
        await state.clear()
    except Exception:
        logger.exception("Failed to initialize test SBP payment tg_id=%s tariff=%s", callback.from_user.id, tariff_code)
        await callback.answer("Платёж временно недоступен. Попробуйте позже.", show_alert=True)
        return

    async def _process_payment() -> dict:
        if payment.get("status") != "paid":
            await payments_repo.mark_paid(payload=payload, telegram_charge_id="test-sbp")
            base_limit = int(plan["traffic_gb"])
            if purchase_type == "renewal":
                # Renewal: extend the existing subscription from its current end date.
                # Do NOT touch other keys — each key has independent expiry in keys.expires_at.
                months = max(1, int(plan["duration_days"]) // 30)
                await subs_repo.create_or_extend(int(payment["tg_id"]), months=months)
                # Atomic increment — prevents lost updates when two payments process concurrently.
                await users_repo.add_traffic_limit(int(payment["tg_id"]), base_limit)
                if renew_key_id is not None:
                    await keys_repo.add_traffic_limit(int(renew_key_id), int(payment["tg_id"]), base_limit)
            else:
                # New key: accumulate rather than overwrite — user may have other keys.
                await users_repo.add_traffic_limit(int(payment["tg_id"]), base_limit)
        return {
            "tg_id": int(payment["tg_id"]),
            "amount": int(payment["amount"]),
            "purchase_type": purchase_type,
            "renew_key_id": renew_key_id,
        }

    try:
        processed = await idem.execute("payment_success", f"payment-success:{payload}", _process_payment)
    except Exception:
        logger.exception("Test SBP payment processing failed payload=%s", payload)
        await callback.answer("Ошибка обработки оплаты.", show_alert=True)
        return

    tg_id = int(processed["tg_id"])
    purchase_type = str(processed.get("purchase_type") or "new")
    renew_key_id = processed.get("renew_key_id")
    user = await users_repo.get_by_tg_id(tg_id)
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    activated_dt = utc_now()
    plan_months = max(1, int(plan["duration_days"]) // 30)

    if purchase_type == "renewal":
        # expires_dt computed below after fetching the specific key
        expires_dt = add_months(activated_dt, plan_months)  # safe placeholder
    else:
        # New key: its OWN independent expiry from NOW (plan duration only).
        # users.expires_at = MAX(new, current) so the watchdog doesn't deactivate
        # the user when a short-term key expires while longer keys remain active.
        new_key_expires_dt = add_months(activated_dt, plan_months)
        current_user_pre = await users_repo.get_by_tg_id(tg_id)
        current_raw = (current_user_pre or {}).get("expires_at")
        if current_raw:
            try:
                expires_dt = max(new_key_expires_dt, parse_iso_utc(current_raw))
            except Exception:
                expires_dt = new_key_expires_dt
        else:
            expires_dt = new_key_expires_dt
        await users_repo.set_expiry(
            tg_id, expires_at=expires_dt.isoformat(),
            is_active=True, plan="monthly", last_activated_at=activated_dt.isoformat(),
        )

    expiry_ms = int(expires_dt.timestamp() * 1000)

    if purchase_type == "renewal":
        try:
            key_id_int = _require_renew_key_id(renew_key_id)
        except ValueError:
            logger.error("Test SBP renewal has no valid key_id tg_id=%s payload=%s", tg_id, payload)
            await callback.message.answer(
                "Платеж получен, но не удалось определить ключ для продления. Напишите в поддержку.",
                reply_markup=get_main_menu_keyboard(),
            )
            return
        # Fetch key BEFORE computing new expiry — key.expires_at is the pre-renewal value.
        renewed_key = await keys_repo.get_by_id_for_user(key_id_int, tg_id)
        key_traffic_gb = int((renewed_key or {}).get("traffic_limit_gb") or 0) or None

        # Extend from the KEY's own current expiry, not from the global subscription.
        key_expires_raw = (renewed_key or {}).get("expires_at")
        if key_expires_raw:
            try:
                key_base = max(parse_iso_utc(key_expires_raw), activated_dt)
            except Exception:
                key_base = activated_dt
        else:
            key_base = activated_dt
        expires_dt = add_months(key_base, plan_months)
        expiry_ms = int(expires_dt.timestamp() * 1000)

        await users_repo.set_expiry(
            tg_id, expires_at=expires_dt.isoformat(),
            is_active=True, plan="monthly", last_activated_at=activated_dt.isoformat(),
        )
        try:
            manager = build_vpn_manager(db, settings, bot=callback.bot)
            await manager.renew_user_access(
                tg_id, expiry_ms, key_id=key_id_int, traffic_limit_gb=key_traffic_gb
            )
        except Exception:
            logger.exception("Failed to update XUI expiry after test SBP renewal tg_id=%s", tg_id)
        try:
            await keys_repo.update_expires_at(key_id_int, tg_id, expires_dt.isoformat())
        except Exception:
            logger.exception("Failed to update key expires_at tg_id=%s key_id=%s", tg_id, renew_key_id)
        expires_str = to_moscow(expires_dt).strftime("%d.%m.%Y")
        days_remaining = max(0, (expires_dt - utc_now()).days)
        key_label = f" ключа #{key_id_int}"
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"🔄 <b>Продление{key_label}</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
            "📊 Статус: <b>Активна</b>"
        )
        await callback.message.answer(text, reply_markup=get_main_menu_keyboard())
        await callback.message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))
        return

    link = ""
    sub_token = ""
    provisioned_key_id = 0
    vpn_idem_key = f"vpn-provision:{payload}"
    vpn_idem = IdempotencyService(IdempotencyRepository())

    async def _provision_vpn() -> dict:
        au = await ensure_user_access(
            tg_id=tg_id,
            db=db,
            settings=settings,
            require_active=True,
            force_new_key=True,
            action="create",
        )
        provisioned_key_id = int(au.get("key_id") or 0)
        return {
            "vpn_key": str(au.get("vpn_key") or ""),
            "key_sub_token": str(au.get("key_sub_token") or ""),
            "key_id": provisioned_key_id,
        }

    try:
        vpn_result = await vpn_idem.execute("vpn_provision", vpn_idem_key, _provision_vpn)
        link = vpn_result.get("vpn_key", "")
        sub_token = vpn_result.get("key_sub_token", "")
        provisioned_key_id = int(vpn_result.get("key_id") or 0)
        link, provisioned_key_id, sub_token = await _repair_provision_result(
            keys_repo, tg_id, str(link or ""), provisioned_key_id, str(sub_token or "")
        )
        if provisioned_key_id:
            try:
                # Use the plan's own duration, not the global MAX used for users.expires_at.
                await keys_repo.update_expires_at(provisioned_key_id, tg_id, new_key_expires_dt.isoformat())
            except Exception:
                logger.warning("Failed to store per-key expiry tg_id=%s key_id=%s", tg_id, provisioned_key_id)
            try:
                await keys_repo.update_traffic_limit(provisioned_key_id, tg_id, int(plan["traffic_gb"]))
            except Exception:
                logger.warning("Failed to store per-key traffic limit tg_id=%s key_id=%s", tg_id, provisioned_key_id)
    except AccessEnsureError:
        logger.exception("Failed to bootstrap access after test SBP payment for tg_id=%s", tg_id)
    except Exception:
        logger.exception("VPN provisioning idempotency failed tg_id=%s", tg_id)

    referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
    await referral_service.accrue_bonus(user, int(processed["amount"]))
    paid_count = await payments_repo.count_paid(tg_id)
    friend_bonus = await referral_service.accrue_friend_bonus(user, paid_count, settings.referral_friend_bonus_rub)
    expires_str = to_moscow(expires_dt).strftime("%d.%m.%Y")
    days_remaining = max(0, (expires_dt - utc_now()).days)
    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if sub_token and settings.public_base_url else ""
    if sub_url:
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            "📦 <b>Подписка активирована</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
            "📊 Статус: <b>Активна</b>\n\n"
            "🔗 <b>Ссылка для подключения:</b>\n"
            f"<code>{escape(sub_url)}</code>\n\n"
            "Нажмите «Подключить» чтобы настроить VPN-клиент,\n"
            "или «Показать QR» для сканирования."
        )
        await callback.message.answer(text, reply_markup=payment_success_keyboard(sub_url, key_id=provisioned_key_id))
    else:
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            "📦 <b>Подписка активирована</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n\n"
            "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
        )
        await callback.message.answer(text, reply_markup=get_main_menu_keyboard())
    await callback.message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))
    if friend_bonus > 0:
        await callback.message.answer(f"🎁 Вам начислен реферальный бонус: +{friend_bonus} RUB на баланс", reply_markup=get_main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "pay:stars")
async def pay_stars(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    if not tariff_code:
        await callback.answer("Сначала выберите тариф", show_alert=True)
        return
    plan = _selected_plan(data)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    email = data.get("email")
    purchase_type = str(data.get("purchase_type") or "new")
    renew_key_id = data.get("renew_key_id")

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    payments_repo = PaymentsRepository(db)
    try:
        if purchase_type == "renewal":
            renew_key_id = _require_renew_key_id(renew_key_id)
            key_row = await keys_repo.get_by_id_for_user(renew_key_id, callback.from_user.id)
            if not key_row:
                await callback.answer("Ключ для продления не найден", show_alert=True)
                return
        await users_repo.get_or_create(callback.from_user.id)
        payload = generate_payload(callback.from_user.id, tariff_code)
        idempotency_key = f"payment-create:{callback.from_user.id}:{tariff_code}:{str(email or '').lower()}:{purchase_type}:{renew_key_id}"
        payment = await payments_repo.create_pending(
            tg_id=callback.from_user.id,
            amount=int(plan["price_rub"]),
            tariff_code=tariff_code,
            email=email,
            payload=payload,
            idempotency_key=idempotency_key,
            purchase_type=purchase_type,
            renew_key_id=int(renew_key_id) if renew_key_id is not None else None,
        )
        payload = str(payment.get("payload") or payload)
        await state.clear()
    except Exception:
        logger.exception("Failed to initialize payment tg_id=%s tariff=%s", callback.from_user.id, tariff_code)
        await callback.answer("Платёж временно недоступен. Попробуйте позже.", show_alert=True)
        return

    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer_invoice(
        title=f"ZyberVPN — {plan['name']}",
        description=f"Подписка ZyberVPN на {plan['name']}",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=plan["name"], amount=int(plan["price_stars"]))],
        provider_token="",
        reply_markup=stars_back_keyboard(
            tariff_code=str(tariff_code),
            purchase_type=str(purchase_type),
            renew_key_id=str(renew_key_id or 0),
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("payment_back:"))
async def payment_select_back(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    """Return to payment method selection.

    tariff_code is encoded in callback_data as payment_back:{tc}:{pt}:{rkid}
    so the button works even after state.clear() (e.g. after creating a Platega order).
    """
    parts = callback.data.split(":", 3)
    tariff_code = parts[1] if len(parts) > 1 else ""
    purchase_type = parts[2] if len(parts) > 2 else "new"
    renew_raw = parts[3] if len(parts) > 3 else "0"

    plan = get_plan_by_tariff_code(tariff_code) if tariff_code else None
    if not plan:
        # Fallback: FSM state (present if state was not cleared yet)
        data = await state.get_data()
        plan = _selected_plan(data)
    if not plan:
        await callback.answer("Сессия истекла, выберите тариф заново.", show_alert=True)
        return

    # Restore FSM context so the payment buttons work again.
    renew_key_id = int(renew_raw) if renew_raw.isdigit() and int(renew_raw) > 0 else None
    await state.update_data(tariff_code=tariff_code, purchase_type=purchase_type, renew_key_id=renew_key_id)
    await state.set_state(PurchaseState.waiting_payment)

    platega_on = bool(getattr(settings, "platega_merchant_id", "") and getattr(settings, "platega_api_key", ""))
    platega_crypto_on = platega_on and bool(getattr(settings, "platega_crypto_method", 0))
    is_admin = callback.from_user.id in settings.admin_ids
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"💰 К оплате: {float(plan['price_rub']):.2f} RUB\n\nВыберите удобный способ оплаты:",
        reply_markup=payment_keyboard(platega_enabled=platega_on, platega_crypto_enabled=platega_crypto_on, show_test_pay=is_admin),
    )
    await callback.answer()


async def _pay_via_platega(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
    payment_method: int,
    method_label: str,
) -> None:
    """Shared logic for Platega SBP and Crypto payment buttons."""
    merchant_id = getattr(settings, "platega_merchant_id", "")
    api_key = getattr(settings, "platega_api_key", "")
    if not merchant_id or not api_key:
        await callback.answer("Platega не настроена", show_alert=True)
        return

    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    if not tariff_code:
        await callback.answer("Сначала выберите тариф", show_alert=True)
        return
    plan = _selected_plan(data)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    purchase_type = str(data.get("purchase_type") or "new")
    renew_key_id = data.get("renew_key_id")
    email = data.get("email")

    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)

    try:
        if purchase_type == "renewal":
            renew_key_id = _require_renew_key_id(renew_key_id)
        await users_repo.get_or_create(callback.from_user.id)

        idem_key = (
            f"platega-create-{payment_method}:{callback.from_user.id}:{tariff_code}:"
            f"{str(email or '').lower()}:{purchase_type}:{renew_key_id}"
        )

        webhook_secret = getattr(settings, "platega_webhook_secret", "")
        return_url = settings.public_base_url or "https://t.me/"

        client = PlategaClient(
            merchant_id=merchant_id,
            api_key=api_key,
            return_url=return_url,
            failed_url=return_url,
        )
        result = await client.create_payment(
            amount=int(plan["price_rub"]),
            description=f"ZyberVPN — {plan['name']}",
            internal_payload=idem_key,
            payment_method=payment_method,
        )
        transaction_id = result["transaction_id"]
        redirect_url = result["redirect_url"]

        await payments_repo.create_pending(
            tg_id=callback.from_user.id,
            amount=int(plan["price_rub"]),
            tariff_code=tariff_code,
            email=email,
            payload=transaction_id,
            idempotency_key=idem_key,
            purchase_type=purchase_type,
            renew_key_id=int(renew_key_id) if renew_key_id is not None else None,
        )
        await state.clear()
    except PlategaError as exc:
        logger.exception("Platega payment creation failed tg_id=%s method=%s error=%s",
                         callback.from_user.id, payment_method, exc)
        await callback.answer("Platega временно недоступна. Попробуйте позже.", show_alert=True)
        return
    except Exception:
        logger.exception("pay:platega_%s unexpected error tg_id=%s", payment_method, callback.from_user.id)
        await callback.answer("Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
        return

    # Delete the payment method selection message before showing payment details.
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"💳 <b>Оплата через {method_label} (Platega)</b>\n\n"
        f"💰 Сумма: <b>{int(plan['price_rub'])} RUB</b>\n"
        f"📦 Тариф: <b>{plan['name']}</b>\n\n"
        "Нажмите кнопку ниже для перехода к оплате.\n"
        "После оплаты бот автоматически выдаст VPN-ключ.",
        reply_markup=payment_back_keyboard(
            redirect_url,
            tariff_code=str(tariff_code),
            purchase_type=str(purchase_type),
            renew_key_id=str(renew_key_id or 0),
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "pay:platega")
async def pay_platega(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    await _pay_via_platega(callback, state, db, settings, payment_method=2, method_label="СБП / QR")


@router.callback_query(F.data == "pay:platega_crypto")
async def pay_platega_crypto(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    method_code = int(getattr(settings, "platega_crypto_method", 0))
    if not method_code:
        await callback.answer("Крипто-оплата не настроена", show_alert=True)
        return
    await _pay_via_platega(callback, state, db, settings, payment_method=method_code, method_label="Криптовалюта")
