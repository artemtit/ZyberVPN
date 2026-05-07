from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message
import logging

from app.bot.keyboards.inline import email_keyboard, main_menu_keyboard, payment_keyboard, payment_success_keyboard, tariffs_keyboard
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
from app.utils.datetime import add_months, parse_iso_utc, utc_now

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
async def choose_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_code = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_code)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    await state.update_data(tariff_code=tariff_code)
    await state.set_state(PurchaseState.waiting_email)
    await callback.message.edit_text(
        "📧 Ввод Email\nВведите адрес почты или пропустите этот шаг:",
        reply_markup=email_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("buy_plan:"))
async def choose_plan(callback: CallbackQuery, state: FSMContext) -> None:
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

    await state.update_data(plan_id=plan_id, tariff_code=plan["tariff_code"])
    await state.set_state(PurchaseState.waiting_email)
    await callback.message.edit_text(
        "✅ Вы выбрали: "
        f"{plan['name']} ({plan['traffic_gb']} ГБ, {plan['price_rub']}₽)\n\n"
        "Введите email для получения доступа:",
        reply_markup=email_keyboard(),
    )
    await callback.answer()


@router.message(PurchaseState.waiting_email)
async def input_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if "@" not in email or "." not in email:
        await message.answer(
            "Введите корректный email или нажмите кнопку «Продолжить без почты».",
            reply_markup=get_main_menu_keyboard(),
        )
        return
    await state.update_data(email=email)
    await message.answer(f"✅ Email сохранен: {email}", reply_markup=get_main_menu_keyboard())
    await state.set_state(PurchaseState.waiting_payment)
    data = await state.get_data()
    plan = _selected_plan(data)
    if not plan:
        await state.clear()
        await message.answer("Сначала выберите тариф.", reply_markup=get_main_menu_keyboard())
        return
    await message.answer(
        f"💰 К оплате: {float(plan['price_rub']):.2f} RUB\n\nВыберите удобный способ оплаты:",
        reply_markup=payment_keyboard(),
    )


@router.callback_query(F.data == "email_skip")
async def skip_email(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    plan = _selected_plan(data)
    if not plan:
        await callback.answer("Сначала выберите тариф", show_alert=True)
        return
    await state.update_data(email=None)
    await state.set_state(PurchaseState.waiting_payment)
    await callback.message.edit_text(
        f"💰 К оплате: {float(plan['price_rub']):.2f} RUB\n\nВыберите удобный способ оплаты:",
        reply_markup=payment_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"pay:sbp", "pay:crypto"}))
async def pay_other_methods(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if callback.data != "pay:sbp":
        await callback.answer("Метод временно недоступен", show_alert=True)
        return
    if not (settings.test_mode and callback.from_user.id in settings.admin_ids):
        await callback.answer("СБП пока недоступен", show_alert=True)
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
                current_user = await users_repo.get_by_tg_id(int(payment["tg_id"]))
                current_limit = int((current_user or {}).get("traffic_limit_gb") or 0)
                await users_repo.set_traffic_limit(int(payment["tg_id"]), current_limit + base_limit)
                # Also accumulate per-key traffic limit for the specific renewed key.
                if renew_key_id is not None:
                    renew_row = await keys_repo.get_by_id_for_user(int(renew_key_id), int(payment["tg_id"]))
                    old_key_limit = int((renew_row or {}).get("traffic_limit_gb") or 60)
                    await keys_repo.update_traffic_limit(int(renew_key_id), int(payment["tg_id"]), old_key_limit + base_limit)
            else:
                # New key: independent subscription — do NOT chain off the existing one.
                await users_repo.set_traffic_limit(int(payment["tg_id"]), base_limit)
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
        active_sub = await subs_repo.get_active(tg_id)
        expires_dt = parse_iso_utc(active_sub["expires_at"]) if active_sub else add_months(activated_dt, plan_months)
    else:
        # New key: fresh independent expiry from NOW.
        # users.expires_at = MAX(current, new) so the watchdog doesn't deactivate
        # the user when a short-term key expires while longer keys remain active.
        new_key_expires_dt = add_months(activated_dt, plan_months)
        current_user_pre = await users_repo.get_by_tg_id(tg_id)
        current_raw = (current_user_pre or {}).get("expires_at")
        if current_raw:
            try:
                current_dt = parse_iso_utc(current_raw)
                expires_dt = max(new_key_expires_dt, current_dt)
            except Exception:
                expires_dt = new_key_expires_dt
        else:
            expires_dt = new_key_expires_dt

    await users_repo.set_expiry(
        tg_id,
        expires_at=expires_dt.isoformat(),
        is_active=True,
        plan="monthly",
        last_activated_at=activated_dt.isoformat(),
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
        renewed_key = await keys_repo.get_by_id_for_user(key_id_int, tg_id)
        key_traffic_gb = int((renewed_key or {}).get("traffic_limit_gb") or 0) or None
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
        await callback.message.answer("✅ Тестовая СБП-оплата проведена. Подписка продлена.", reply_markup=get_main_menu_keyboard())
        await callback.message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))
        return

    link = ""
    sub_token = ""
    try:
        access_user = await ensure_user_access(
            tg_id=tg_id,
            db=db,
            settings=settings,
            require_active=True,
            idempotency_key=f"vpn-after-payment:{payload}",
            force_new_key=True,
            action="create",
        )
        link = str(access_user.get("vpn_key") or "")
        sub_token = str(access_user.get("key_sub_token") or "")
        # Store per-key expiry so each key has an independent timeline.
        new_key_id = int(access_user.get("key_id") or 0)
        if new_key_id and purchase_type != "renewal":
            try:
                await keys_repo.update_expires_at(new_key_id, tg_id, expires_dt.isoformat())
            except Exception:
                logger.warning("Failed to store per-key expiry tg_id=%s key_id=%s", tg_id, new_key_id)
            try:
                await keys_repo.update_traffic_limit(new_key_id, tg_id, int(plan["traffic_gb"]))
            except Exception:
                logger.warning("Failed to store per-key traffic limit tg_id=%s key_id=%s", tg_id, new_key_id)
    except AccessEnsureError:
        logger.exception("Failed to bootstrap access after test SBP payment for tg_id=%s", tg_id)

    referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
    await referral_service.accrue_bonus(user, int(processed["amount"]))
    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if sub_token and settings.public_base_url else ""
    if link and sub_url:
        await callback.message.answer(
            f"✅ Тестовая СБП-оплата проведена.\n\nСсылка:\n<code>{sub_url}</code>",
            reply_markup=payment_success_keyboard(sub_url),
        )
    else:
        await callback.message.answer("✅ Тестовая СБП-оплата проведена. Ключ создаётся.", reply_markup=get_main_menu_keyboard())
    await callback.message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))
    await callback.answer("Тестовая СБП-оплата успешно проведена", show_alert=True)


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

    await callback.message.answer_invoice(
        title=f"ZyberVPN — {plan['name']}",
        description=f"Подписка ZyberVPN на {plan['name']}",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=plan["name"], amount=int(plan["price_stars"]))],
        provider_token="",
    )
    await callback.answer()
