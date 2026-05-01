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
from app.repositories.keys import KeysRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, build_vpn_manager, ensure_user_access
from app.services.idempotency import IdempotencyService
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from app.services.vpn import qr_png_from_text
from app.utils.datetime import parse_iso_utc, utc_now

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

    keys_repo = KeysRepository(db)
    payment = await payments_repo.get_by_payload(payment_info.invoice_payload)
    if not payment:
        await message.answer("Платеж не найден.")
        return

    idem_key = f"payment-success:{payment_info.invoice_payload}"
    logger.info("Payment callback received payload=%s tg_id=%s", payment_info.invoice_payload, payment.get("tg_id"))

    async def _process_payment() -> dict:
        purchase_type = payment.get("purchase_type") or "new"
        renew_key_id = payment.get("renew_key_id")

        if payment.get("status") != "paid":
            await payments_repo.mark_paid(
                payload=payment_info.invoice_payload,
                telegram_charge_id=payment_info.telegram_payment_charge_id,
            )
            tariff = TARIFFS[str(payment["tariff_code"])]
            if purchase_type == "renewal" and renew_key_id is not None:
                current_sub = await subs_repo.get_active(int(payment["tg_id"]))
                if current_sub:
                    all_keys = await keys_repo.list_by_user(int(payment["tg_id"]))
                    for k in all_keys:
                        if k["id"] != renew_key_id and not k.get("expires_at"):
                            await keys_repo.update_expires_at(
                                int(k["id"]), int(payment["tg_id"]), current_sub["expires_at"]
                            )
            await subs_repo.create_or_extend(int(payment["tg_id"]), months=tariff["months"])
            await users_repo.set_traffic_limit(int(payment["tg_id"]), tariff.get("traffic_gb", 60))
        return {
            "tg_id": int(payment["tg_id"]),
            "tariff_code": str(payment["tariff_code"]),
            "amount": int(payment["amount"]),
            "purchase_type": purchase_type,
            "renew_key_id": renew_key_id,
        }

    try:
        processed = await idem.execute("payment_success", idem_key, _process_payment)
    except Exception:
        logger.exception("Payment processing failed")
        await message.answer("Платеж получен, но обработка временно недоступна. Попробуйте позже.")
        return

    logger.info("Payment processed idempotently payload=%s tg_id=%s", payment_info.invoice_payload, processed["tg_id"])
    tg_id = int(processed["tg_id"])
    purchase_type = str(processed.get("purchase_type") or "new")
    renew_key_id = processed.get("renew_key_id")

    user = await users_repo.get_by_tg_id(tg_id)
    if not user:
        await message.answer("Пользователь не найден.")
        return

    # Read actual expiry from subscriptions table
    active_sub = await subs_repo.get_active(tg_id)
    activated_dt = utc_now()
    if active_sub:
        expires_dt = parse_iso_utc(active_sub["expires_at"])
    else:
        expires_dt = activated_dt + timedelta(days=30)
    activated_at = activated_dt.isoformat()

    supabase_user = await users_repo.get_by_tg_id(tg_id)
    if supabase_user:
        await users_repo.set_expiry(
            tg_id,
            expires_at=expires_dt.isoformat(),
            is_active=True,
            plan="monthly",
            last_activated_at=activated_at,
        )

    expires_str = expires_dt.strftime("%d.%m.%Y")
    days_remaining = max(0, (expires_dt - utc_now()).days)
    expiry_ms = int(expires_dt.timestamp() * 1000)

    if purchase_type == "renewal":
        # Renewal: extend subscription only, update XUI expiry for the specific key.
        key_id_int = int(renew_key_id) if renew_key_id is not None else None
        try:
            manager = build_vpn_manager(db, settings, bot=message.bot)
            await manager.update_user_expiry(tg_id, expiry_ms, key_id=key_id_int)
        except Exception:
            logger.exception("Failed to update XUI expiry after renewal tg_id=%s key_id=%s", tg_id, renew_key_id)
        if key_id_int is not None:
            try:
                await keys_repo.update_expires_at(key_id_int, tg_id, expires_dt.isoformat())
            except Exception:
                logger.exception("Failed to update key expires_at tg_id=%s key_id=%s", tg_id, renew_key_id)

        key_label = f" ключа #{renew_key_id}" if renew_key_id else ""
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"🔄 <b>Продление{key_label}</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
            "📊 Статус: <b>Активна</b>"
        )
        await message.answer(text)
        await message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))

        referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
        bonus = await referral_service.accrue_bonus(user, int(processed["amount"]))
        if bonus > 0:
            await message.answer(f"Реферальный бонус: +{bonus} RUB")
        return

    # New key: provision a fresh VPN key.
    link = ""
    sub_token = ""

    try:
        access_user = await ensure_user_access(
            tg_id=tg_id,
            db=db,
            settings=settings,
            require_active=True,
            idempotency_key=f"vpn-after-payment:{payment_info.invoice_payload}",
            force_new_key=True,
        )
        link = str(access_user.get("vpn_key") or "")
        sub_token = str(access_user.get("sub_token") or "")
    except AccessEnsureError:
        logger.exception("Failed to bootstrap access after payment for tg_id=%s", tg_id)
        await message.answer("Оплата прошла, но ключ пока не создан. Попробуйте позже.")

    sub_url = f"{settings.public_base_url}/sub/{escape(sub_token)}" if sub_token and settings.public_base_url else ""

    if link and sub_url:
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
async def show_payment_qr(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(callback.from_user.id)
    sub_token = str((user or {}).get("sub_token") or "")
    if not sub_token or not settings.public_base_url:
        await callback.answer("Subscription URL не найден", show_alert=True)
        return
    sub_url = f"{settings.public_base_url}/sub/{sub_token}"
    qr_bytes = qr_png_from_text(sub_url)
    await callback.message.answer_photo(
        BufferedInputFile(qr_bytes, filename="subscription-qr.png"),
        caption=f"QR-код для подключения\n<code>{escape(sub_url)}</code>",
    )
    await callback.answer()
