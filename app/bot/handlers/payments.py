from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import BufferedInputFile, Message, PreCheckoutQuery

from app.bot.keyboards.reply import main_menu_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from app.services.vpn import create_vpn_key

router = Router()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, db: Database, settings: Settings) -> None:
    payment_info = message.successful_payment
    payments_repo = PaymentsRepository(db)
    users_repo = UsersRepository(db)
    subs_repo = SubscriptionsRepository(db)
    keys_repo = KeysRepository(db)

    payment = await payments_repo.get_by_payload(payment_info.invoice_payload)
    if not payment or payment["status"] == "paid":
        await message.answer("Оплата обработана ранее.")
        return

    await payments_repo.mark_paid(
        payload=payment_info.invoice_payload,
        telegram_charge_id=payment_info.telegram_payment_charge_id,
    )

    user = await users_repo.get_by_id(payment["user_id"])
    if not user:
        await message.answer("Пользователь не найден.")
        return

    tariff = TARIFFS[payment["tariff_code"]]
    await subs_repo.create_or_extend(user["id"], months=tariff["months"])
    link, qr_bytes = create_vpn_key(user["id"])
    await keys_repo.create(user["id"], link)

    referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
    bonus = await referral_service.accrue_bonus(user, payment["amount"])

    await message.answer(
        f"Оплата успешна ✅\nКлюч создан.\nСсылка:\n<code>{escape(link)}</code>",
        reply_markup=main_menu_keyboard(),
    )
    await message.answer_photo(
        BufferedInputFile(qr_bytes, filename="vpn-qr.png"),
        caption="QR-код для подключения",
    )
    if bonus > 0:
        await message.answer(f"Реферальный бонус начислен: {bonus} RUB")
