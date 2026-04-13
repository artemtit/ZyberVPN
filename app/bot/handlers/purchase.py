from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message

from app.bot.keyboards.inline import email_keyboard, payment_methods_keyboard, tariffs_keyboard
from app.bot.states.purchase import PurchaseState
from app.db.database import Database
from app.repositories.payments import PaymentsRepository
from app.repositories.users import UsersRepository
from app.services.payments import generate_payload
from app.services.tariffs import TARIFFS

router = Router()


@router.callback_query(F.data == "buy_open")
async def buy_open(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Выберите тариф:", reply_markup=tariffs_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("tariff:"))
async def choose_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_code = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_code)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    await state.set_state(PurchaseState.waiting_email)
    await state.update_data(tariff_code=tariff_code)
    await callback.message.edit_text(
        f"Тариф: {tariff['title']} ({tariff['price_rub']} RUB)\nВведите email для чека:",
        reply_markup=email_keyboard(),
    )
    await callback.answer()


@router.message(PurchaseState.waiting_email)
async def input_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if "@" not in email or "." not in email:
        await message.answer("Введите корректный email или нажмите «Продолжить без email».")
        return
    data = await state.get_data()
    tariff_code = data["tariff_code"]
    await state.update_data(email=email)
    await state.set_state(PurchaseState.waiting_payment)
    await message.answer(
        "Выберите способ оплаты:",
        reply_markup=payment_methods_keyboard(tariff_code),
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
    await callback.message.edit_text(
        "Выберите способ оплаты:",
        reply_markup=payment_methods_keyboard(tariff_code),
    )
    await callback.answer()


@router.callback_query(F.data == "pay:other")
async def pay_other(callback: CallbackQuery) -> None:
    await callback.answer("Метод в разработке", show_alert=True)


@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    tariff_code = callback.data.split(":")[2]
    tariff = TARIFFS.get(tariff_code)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    data = await state.get_data()
    email = data.get("email")
    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)
    user = await users_repo.get_or_create(callback.from_user.id)
    payload = generate_payload(user["id"], tariff_code)
    await payments_repo.create_pending(
        user_id=user["id"],
        amount=tariff["price_rub"],
        tariff_code=tariff_code,
        email=email,
        payload=payload,
    )
    await state.clear()

    await callback.message.answer_invoice(
        title=f"ZyberVPN — {tariff['title']}",
        description=f"Подписка ZyberVPN на {tariff['title']}",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=tariff["title"], amount=tariff["price_stars"])],
        provider_token="",
    )
    await callback.answer()


@router.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Выберите действие в главном меню.")
    await callback.answer()
