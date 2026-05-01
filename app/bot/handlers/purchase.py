from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message
import logging

from app.bot.keyboards.inline import email_keyboard, main_menu_keyboard, payment_keyboard, payment_success_keyboard, tariffs_keyboard
from app.bot.states.purchase import PurchaseState
from app.config import Settings
from app.db.database import Database
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, build_vpn_manager, ensure_user_access
from app.services.idempotency import IdempotencyService
from app.services.payments import generate_payload
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from app.utils.datetime import parse_iso_utc, utc_now

router = Router()
logger = logging.getLogger(__name__)


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


@router.message(PurchaseState.waiting_email)
async def input_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if "@" not in email or "." not in email:
        await message.answer("Введите корректный email или нажмите кнопку «Продолжить без почты».")
        return
    await state.update_data(email=email)
    await state.set_state(PurchaseState.waiting_payment)
    data = await state.get_data()
    tariff = TARIFFS[data["tariff_code"]]
    await message.answer(
        f"💰 К оплате: {tariff['price_rub']:.2f} RUB\n\nВыберите удобный способ оплаты:",
        reply_markup=payment_keyboard(),
    )


@router.callback_query(F.data == "email_skip")
async def skip_email(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    if not tariff_code:
        await callback.answer("Сначала выберите тариф", show_alert=True)
        return
    await state.update_data(email=None)
    await state.set_state(PurchaseState.waiting_payment)
    tariff = TARIFFS[tariff_code]
    await callback.message.edit_text(
        f"💰 К оплате: {tariff['price_rub']:.2f} RUB\n\nВыберите удобный способ оплаты:",
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
    tariff = TARIFFS[tariff_code]
    email = data.get("email")
    purchase_type = str(data.get("purchase_type") or "new")
    renew_key_id = data.get("renew_key_id")

    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)
    subs_repo = SubscriptionsRepository(db)
    idem = IdempotencyService(IdempotencyRepository())
    try:
        await users_repo.get_or_create(callback.from_user.id)
        payload = generate_payload(callback.from_user.id, tariff_code)
        idempotency_key = (
            f"payment-create:{callback.from_user.id}:{tariff_code}:{str(email or '').lower()}:{purchase_type}:{renew_key_id}"
        )
        payment = await payments_repo.create_pending(
            tg_id=callback.from_user.id,
            amount=tariff["price_rub"],
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
            await subs_repo.create_or_extend(int(payment["tg_id"]), months=tariff["months"])
            await users_repo.set_traffic_limit(int(payment["tg_id"]), tariff.get("traffic_gb", 60))
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
    user = await users_repo.get_by_tg_id(tg_id)
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    active_sub = await subs_repo.get_active(tg_id)
    activated_dt = utc_now()
    expires_dt = parse_iso_utc(active_sub["expires_at"]) if active_sub else (activated_dt)
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
            manager = build_vpn_manager(db, settings, bot=callback.bot)
            key_id_int = int(renew_key_id) if renew_key_id is not None else None
            await manager.update_user_expiry(tg_id, expiry_ms, key_id=key_id_int)
        except Exception:
            logger.exception("Failed to update XUI expiry after test SBP renewal tg_id=%s", tg_id)
        await callback.message.answer("✅ Тестовая СБП-оплата проведена. Подписка продлена.")
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
        )
        link = str(access_user.get("vpn_key") or "")
        sub_token = str(access_user.get("sub_token") or "")
    except AccessEnsureError:
        logger.exception("Failed to bootstrap access after test SBP payment for tg_id=%s", tg_id)

    try:
        manager = build_vpn_manager(db, settings, bot=callback.bot)
        await manager.update_user_expiry(tg_id, expiry_ms)
    except Exception:
        logger.exception("Failed to update XUI expiry after test SBP payment tg_id=%s", tg_id)

    referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
    await referral_service.accrue_bonus(user, int(processed["amount"]))
    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if sub_token and settings.public_base_url else ""
    if link and sub_url:
        await callback.message.answer(
            f"✅ Тестовая СБП-оплата проведена.\n\nСсылка:\n<code>{sub_url}</code>",
            reply_markup=payment_success_keyboard(sub_url),
        )
    else:
        await callback.message.answer("✅ Тестовая СБП-оплата проведена. Ключ создаётся.")
    await callback.message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))
    await callback.answer("Тестовая СБП-оплата успешно проведена", show_alert=True)


@router.callback_query(F.data == "pay:stars")
async def pay_stars(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    if not tariff_code:
        await callback.answer("Сначала выберите тариф", show_alert=True)
        return
    tariff = TARIFFS[tariff_code]
    email = data.get("email")
    purchase_type = str(data.get("purchase_type") or "new")
    renew_key_id = data.get("renew_key_id")

    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)
    try:
        await users_repo.get_or_create(callback.from_user.id)
        payload = generate_payload(callback.from_user.id, tariff_code)
        idempotency_key = f"payment-create:{callback.from_user.id}:{tariff_code}:{str(email or '').lower()}:{purchase_type}:{renew_key_id}"
        payment = await payments_repo.create_pending(
            tg_id=callback.from_user.id,
            amount=tariff["price_rub"],
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
        title=f"ZyberVPN — {tariff['title']}",
        description=f"Подписка ZyberVPN на {tariff['title']}",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=tariff["title"], amount=tariff["price_stars"])],
        provider_token="",
    )
    await callback.answer()
