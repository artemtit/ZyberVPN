from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message
import logging

from app.bot.keyboards.inline import email_keyboard, payment_keyboard, tariffs_keyboard
from app.bot.states.purchase import PurchaseState
from app.db.database import Database
from app.repositories.payments import PaymentsRepository
from app.repositories.users import UsersRepository
from app.services.payments import generate_payload
from app.services.tariffs import TARIFFS

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "buy_open")
async def buy_open(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "💳 Выбор тарифа: Основной\n\nВыберите подходящий период подписки:",
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
async def pay_other_methods(callback: CallbackQuery) -> None:
    await callback.answer("Метод временно недоступен", show_alert=True)


@router.callback_query(F.data == "pay:stars")
async def pay_stars(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    if not tariff_code:
        await callback.answer("Сначала выберите тариф", show_alert=True)
        return
    tariff = TARIFFS[tariff_code]
    email = data.get("email")

    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)
    try:
        await users_repo.get_or_create(callback.from_user.id)
        payload = generate_payload(callback.from_user.id, tariff_code)
        idempotency_key = f"payment-create:{callback.from_user.id}:{tariff_code}:{str(email or '').lower()}"
        payment = await payments_repo.create_pending(
            tg_id=callback.from_user.id,
            amount=tariff["price_rub"],
            tariff_code=tariff_code,
            email=email,
            payload=payload,
            idempotency_key=idempotency_key,
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
