from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message, PreCheckoutQuery

from app.bot.keyboards.inline import main_menu_keyboard, payment_success_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, ensure_user_access
from app.services.idempotency import IdempotencyService
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from app.services.vpn import qr_png_from_text
from app.utils.datetime import utc_now

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
    idem = IdempotencyService(IdempotencyRepository())

    payment = await payments_repo.get_by_payload(payment_info.invoice_payload)
    if not payment:
        await message.answer("Платеж не найден.")
        return

    idem_key = f"payment-success:{payment_info.invoice_payload}"
    logger.info("Payment callback received payload=%s tg_id=%s", payment_info.invoice_payload, payment.get("tg_id"))

    async def _process_payment() -> dict:
        if payment.get("status") != "paid":
            await payments_repo.mark_paid(
                payload=payment_info.invoice_payload,
                telegram_charge_id=payment_info.telegram_payment_charge_id,
            )
            tariff = TARIFFS[str(payment["tariff_code"])]
            await subs_repo.create_or_extend(int(payment["tg_id"]), months=tariff["months"])
        return {
            "tg_id": int(payment["tg_id"]),
            "tariff_code": str(payment["tariff_code"]),
            "amount": int(payment["amount"]),
        }

    try:
        processed = await idem.execute("payment_success", idem_key, _process_payment)
    except Exception:
        logger.exception("Payment processing failed")
        await message.answer("Платеж получен, но обработка временно недоступна. Попробуйте позже.")
        return

    logger.info("Payment processed idempotently payload=%s tg_id=%s", payment_info.invoice_payload, processed["tg_id"])
    tg_id = int(processed["tg_id"])
    user = await users_repo.get_by_tg_id(tg_id)
    if not user:
        await message.answer("Пользователь не найден.")
        return

    link = ""
    activated_dt = utc_now()
    expires_dt = activated_dt + timedelta(days=30)
    activated_at = activated_dt.isoformat()
    expires_at = expires_dt.isoformat()
    try:
        sub_token = await users_repo.ensure_sub_token_for_tg(tg_id)
    except Exception:
        logger.exception("Failed to issue subscription token for tg_id=%s", tg_id)
        sub_token = await users_repo.ensure_sub_token(tg_id)
    supabase_user = await users_repo.get_by_tg_id(tg_id)
    if supabase_user:
        await users_repo.set_expiry(
            tg_id,
            expires_at=expires_at,
            is_active=True,
            plan="monthly",
            last_activated_at=activated_at,
        )
        if not users_repo.is_valid_sub_token_hash(str(supabase_user.get("sub_token") or "")):
            await users_repo.update_sub_token(tg_id, sub_token)

    try:
        access_user = await ensure_user_access(
            tg_id=tg_id,
            db=db,
            settings=settings,
            require_active=True,
            idempotency_key=f"vpn-after-payment:{payment_info.invoice_payload}",
        )
        link = str(access_user.get("vpn_key") or "")
    except AccessEnsureError:
        logger.exception("Failed to bootstrap access after payment for tg_id=%s", tg_id)
        await message.answer("Оплата прошла, но ключ пока не создан. Попробуйте позже.")

    expires_str = expires_dt.strftime("%d.%m.%Y")
    days_remaining = max(0, (expires_dt - utc_now()).days)
    sub_url = f"https://sub.zybervpn.ru/sub/{escape(sub_token)}"

    if link:
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            "📦 <b>Подписка активирована</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
            "📊 Статус: <b>Активна</b>\n\n"
            "🔗 <b>Ссылка для подключения:</b>\n"
            f"<code>{sub_url}</code>\n\n"
            "Нажмите «Подключить» чтобы открыть в VPN-клиенте,\n"
            "или «Показать QR» для сканирования."
        )
        await message.answer(text, reply_markup=payment_success_keyboard(sub_url))
    else:
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            "📦 <b>Подписка активирована</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n\n"
            "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
        )
        await message.answer(text)

    referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
    bonus = await referral_service.accrue_bonus(user, int(processed["amount"]))
    await message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))
    if bonus > 0:
        await message.answer(f"Реферальный бонус: +{bonus} RUB")


@router.callback_query(F.data == "payment_show_qr")
async def show_payment_qr(callback: CallbackQuery, db: Database) -> None:
    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(callback.from_user.id)
    vpn_key = str((user or {}).get("vpn_key") or "")
    if not vpn_key.startswith("vless://"):
        await callback.answer("VPN-ключ не найден", show_alert=True)
        return
    qr_bytes = qr_png_from_text(vpn_key)
    await callback.message.answer_photo(
        BufferedInputFile(qr_bytes, filename="vpn-qr.png"),
        caption="QR-код для подключения",
    )
    await callback.answer()
