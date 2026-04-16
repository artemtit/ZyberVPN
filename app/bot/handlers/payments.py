from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import F, Router
from aiogram.types import BufferedInputFile, Message, PreCheckoutQuery

from app.bot.keyboards.inline import main_menu_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, ensure_user_access
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from app.services.vpn import qr_png_from_text

router = Router()
logger = logging.getLogger(__name__)


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, db: Database, settings: Settings) -> None:
    payment_info = message.successful_payment
    payments_repo = PaymentsRepository(db)
    users_repo = UsersRepository(db)
    subs_repo = SubscriptionsRepository(db)

    payment = await payments_repo.get_by_payload(payment_info.invoice_payload)
    if not payment or payment["status"] == "paid":
        await message.answer("Платеж уже обработан.")
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

    link = ""
    qr_bytes = b""
    activated_at = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    sub_token = await users_repo.ensure_sub_token_for_tg(user["tg_id"])
    supabase_user = await users_repo.get_by_tg_id(user["tg_id"])
    if supabase_user:
        updated_sub = await users_repo.set_expiry(
            user["tg_id"],
            expires_at=expires_at,
            is_active=True,
            plan="monthly",
            last_activated_at=activated_at,
        )
        if not updated_sub:
            logger.error("Supabase set_expiry failed for tg_id=%s", user["tg_id"])
        if not supabase_user.get("sub_token"):
            await users_repo.update_sub_token(user["tg_id"], sub_token)
    else:
        created = await users_repo.create(
            tg_id=user["tg_id"],
            vpn_key=link,
            sub_token=sub_token,
            expires_at=expires_at,
            is_active=True,
            plan="monthly",
            last_activated_at=activated_at,
        )
        if not created:
            logger.error("Supabase create failed for tg_id=%s", user["tg_id"])

    try:
        access_user = await ensure_user_access(tg_id=user["tg_id"], db=db, settings=settings, require_active=True)
        link = str(access_user.get("vpn_key") or "")
    except AccessEnsureError:
        logger.exception("Failed to bootstrap access after payment for tg_id=%s", user["tg_id"])
        await message.answer("Оплата прошла, но ключ пока не создан. Нажмите «🔌 Подключиться» через пару минут.")

    if link:
        qr_bytes = qr_png_from_text(link)

    referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
    bonus = await referral_service.accrue_bonus(user, payment["amount"])

    if link:
        await message.answer(
            f"Оплата успешна ✅\nVPN подключен.\nСсылка:\n<code>{escape(link)}</code>",
        )
        await message.answer_photo(
            BufferedInputFile(qr_bytes, filename="vpn-qr.png"),
            caption="QR для подключения",
        )

    await message.answer(
        "🏠 Главное меню\nВыберите действие:",
        reply_markup=main_menu_keyboard(settings.support_url),
    )
    if bonus > 0:
        await message.answer(f"Реферальный бонус: +{bonus} RUB")
