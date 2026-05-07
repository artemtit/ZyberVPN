from __future__ import annotations

"""Post-payment processing for Platega confirmed payments.

Called from the webhook handler after status verification.
Mirrors the logic in bot/handlers/payments.py but uses bot.send_message
instead of message.answer (since there is no Telegram update to reply to).
"""

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from app.bot.keyboards.inline import main_menu_keyboard, payment_success_keyboard
from app.bot.keyboards.main import get_main_menu_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.keys import KeysRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, ensure_user_access
from app.services.idempotency import IdempotencyService
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from app.utils.datetime import add_months, parse_iso_utc, utc_now

logger = logging.getLogger(__name__)


async def process_confirmed_platega_payment(
    transaction_id: str,
    bot: Bot,
    db: Database,
    settings: Settings,
) -> None:
    """Process a confirmed Platega payment end-to-end.

    Idempotent — safe to call more than once for the same transaction.
    The transaction_id is stored as `payload` in the payments table.
    """
    payments_repo = PaymentsRepository(db)
    users_repo = UsersRepository(db)
    subs_repo = SubscriptionsRepository(db)
    keys_repo = KeysRepository(db)
    idem = IdempotencyService(IdempotencyRepository())

    payment = await payments_repo.get_by_payload(transaction_id)
    if not payment:
        logger.error("Platega webhook: payment not found transaction_id=%s", transaction_id)
        return

    tg_id = int(payment["tg_id"])
    purchase_type = str(payment.get("purchase_type") or "new")
    renew_key_id_raw = payment.get("renew_key_id")
    tariff_code = str(payment.get("tariff_code") or "m1")
    tariff = TARIFFS.get(tariff_code, TARIFFS["m1"])

    renew_key_id = int(renew_key_id_raw) if purchase_type == "renewal" and renew_key_id_raw else None

    idem_key = f"platega-payment-success:{transaction_id}"

    async def _process() -> dict:
        if payment.get("status") != "paid":
            await payments_repo.mark_paid(
                payload=transaction_id,
                telegram_charge_id=transaction_id,
            )
            if purchase_type == "renewal":
                await subs_repo.create_or_extend(tg_id, months=tariff["months"])
                base_limit = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
                current_user = await users_repo.get_by_tg_id(tg_id)
                current_limit = int((current_user or {}).get("traffic_limit_gb") or 0)
                await users_repo.set_traffic_limit(tg_id, current_limit + base_limit)
                if renew_key_id is not None:
                    key_row = await keys_repo.get_by_id_for_user(renew_key_id, tg_id)
                    old_limit = int((key_row or {}).get("traffic_limit_gb") or 60)
                    await keys_repo.update_traffic_limit(renew_key_id, tg_id, old_limit + base_limit)
            else:
                base_limit = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
                await users_repo.set_traffic_limit(tg_id, base_limit)
        return {
            "tg_id": tg_id,
            "tariff_code": tariff_code,
            "amount": int(payment.get("amount") or 0),
            "purchase_type": purchase_type,
            "renew_key_id": renew_key_id,
        }

    try:
        processed = await idem.execute("platega_payment_success", idem_key, _process)
    except Exception:
        logger.exception("Platega payment processing failed transaction_id=%s", transaction_id)
        await _send(bot, tg_id, "⚠️ Платеж получен, но обработка временно недоступна. Напишите в поддержку.")
        return

    activated_dt = utc_now()
    activated_at = activated_dt.isoformat()
    user = await users_repo.get_by_tg_id(tg_id)
    if not user:
        logger.error("Platega handler: user not found tg_id=%s", tg_id)
        return

    if purchase_type == "renewal" and renew_key_id is not None:
        active_sub = await subs_repo.get_active(tg_id)
        expires_dt = parse_iso_utc(active_sub["expires_at"]) if active_sub else add_months(activated_dt, tariff["months"])
    else:
        new_exp = add_months(activated_dt, tariff["months"])
        current_raw = (user or {}).get("expires_at")
        if current_raw:
            try:
                expires_dt = max(new_exp, parse_iso_utc(current_raw))
            except Exception:
                expires_dt = new_exp
        else:
            expires_dt = new_exp

    if user:
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

    if purchase_type == "renewal" and renew_key_id is not None:
        try:
            from app.services.access import build_vpn_manager
            manager = build_vpn_manager(db, settings, bot=bot)
            renewed_key = await keys_repo.get_by_id_for_user(renew_key_id, tg_id)
            key_traffic_gb = int((renewed_key or {}).get("traffic_limit_gb") or 0) or None
            await manager.renew_user_access(tg_id, expiry_ms, key_id=renew_key_id, traffic_limit_gb=key_traffic_gb)
            await keys_repo.update_expires_at(renew_key_id, tg_id, expires_dt.isoformat())
        except Exception:
            logger.exception("Platega renewal: XUI update failed tg_id=%s key_id=%s", tg_id, renew_key_id)
        text = (
            "✅ <b>Оплата через Platega прошла успешно!</b>\n\n"
            f"🔄 <b>Продление ключа #{renew_key_id}</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
            "📊 Статус: <b>Активна</b>"
        )
        await _send(bot, tg_id, text)
        await _send(bot, tg_id, "Главное меню", keyboard=main_menu_keyboard(settings.support_url))
        referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
        bonus = await referral_service.accrue_bonus(user, int(processed.get("amount") or 0))
        if bonus > 0:
            await _send(bot, tg_id, f"Реферальный бонус: +{bonus} RUB")
        return

    # New key: provision VPN access.
    link = ""
    sub_token = ""
    new_key_id = 0
    try:
        access_user = await ensure_user_access(
            tg_id=tg_id,
            db=db,
            settings=settings,
            require_active=True,
            force_new_key=True,
            action="create",
        )
        link = str(access_user.get("vpn_key") or "")
        sub_token = str(access_user.get("key_sub_token") or "")
        new_key_id = int(access_user.get("key_id") or 0)
        if new_key_id:
            try:
                await keys_repo.update_expires_at(new_key_id, tg_id, expires_dt.isoformat())
            except Exception:
                logger.warning("Platega: failed to store key expiry tg_id=%s key_id=%s", tg_id, new_key_id)
            try:
                key_traffic_gb = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
                await keys_repo.update_traffic_limit(new_key_id, tg_id, key_traffic_gb)
            except Exception:
                logger.warning("Platega: failed to store key traffic tg_id=%s key_id=%s", tg_id, new_key_id)
    except AccessEnsureError:
        logger.exception("Platega: key provisioning failed tg_id=%s", tg_id)
        await _send(bot, tg_id, "✅ Оплата прошла, но VPN-ключ пока не создан. Попробуйте позже через «Мои ключи».")
        return

    sub_url = f"{settings.public_base_url}/sub/{escape(sub_token)}" if sub_token and settings.public_base_url else ""
    if link and sub_url:
        text = (
            "✅ <b>Оплата через Platega прошла успешно!</b>\n\n"
            "📦 <b>Подписка активирована</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
            "📊 Статус: <b>Активна</b>\n\n"
            "🔗 <b>Ссылка для подключения:</b>\n"
            f"<code>{sub_url}</code>\n\n"
            "Нажмите «Подключить» чтобы открыть в VPN-клиенте."
        )
        await _send(bot, tg_id, text, keyboard=payment_success_keyboard(sub_url))
    else:
        text = (
            "✅ <b>Оплата через Platega прошла успешно!</b>\n\n"
            "📦 <b>Подписка активирована</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n\n"
            "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
        )
        await _send(bot, tg_id, text)

    await _send(bot, tg_id, "Главное меню", keyboard=main_menu_keyboard(settings.support_url))
    referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
    bonus = await referral_service.accrue_bonus(user, int(processed.get("amount") or 0))
    if bonus > 0:
        await _send(bot, tg_id, f"Реферальный бонус: +{bonus} RUB")


async def _send(bot: Bot, tg_id: int, text: str, keyboard=None) -> None:
    try:
        await bot.send_message(tg_id, text, reply_markup=keyboard)
    except TelegramForbiddenError:
        logger.debug("Platega handler: user blocked bot tg_id=%s", tg_id)
    except Exception:
        logger.exception("Platega handler: send_message failed tg_id=%s", tg_id)
