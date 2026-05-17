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

from app.bot.keyboards.inline import main_menu_keyboard, payment_success_keyboard, renewal_success_keyboard
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
from app.services.provisioning_failures import notify_provisioning_failed
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from app.utils.admin_notify import notify_admins
from app.utils.datetime import add_months, parse_iso_utc, utc_now

logger = logging.getLogger(__name__)


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
    if payment.get("status") in ("provisioning", "active", "failed"):
        logger.info("Platega: payment already finalized/processing, skipping transaction_id=%s", transaction_id)
        return

    tg_id = int(payment["tg_id"])
    purchase_type = str(payment.get("purchase_type") or "new")
    renew_key_id_raw = payment.get("renew_key_id")
    tariff_code = str(payment.get("tariff_code") or "m1")

    renew_key_id = int(renew_key_id_raw) if purchase_type == "renewal" and renew_key_id_raw else None
    tariff = TARIFFS.get(tariff_code, TARIFFS["m1"])

    # balance_applied: difference between full tariff price and what Platega actually charged.
    # When purchase.py stores amount=platega_amount, this recovers the balance portion.
    full_tariff_price = int(tariff.get("price_rub", 0))
    payment_amount = int(payment.get("amount") or 0)
    balance_applied = max(0, full_tariff_price - payment_amount)

    # Handle balance top-up — no VPN provisioning needed.
    if purchase_type == "topup":
        rub_amount = int(payment.get("amount") or 0)
        topup_idem_key = f"platega-topup:{transaction_id}"

        async def _process_topup() -> dict:
            # Re-fetch current status — the closure `payment` may be stale on retry.
            fresh = await payments_repo.get_by_payload(transaction_id)
            fresh_status = (fresh or {}).get("status", "")
            if fresh_status == "active":
                return {"rub_amount": rub_amount}
            if fresh_status not in ("paid", "provisioning"):
                await payments_repo.mark_paid(payload=transaction_id, telegram_charge_id=transaction_id)
            if rub_amount > 0:
                await users_repo.add_balance(tg_id, rub_amount)
            await payments_repo.mark_active(transaction_id)
            return {"rub_amount": rub_amount}

        try:
            result = await idem.execute("platega_topup_success", topup_idem_key, _process_topup)
        except Exception:
            logger.exception("Platega topup failed tg_id=%s transaction_id=%s", tg_id, transaction_id)
            await _send(bot, tg_id, "Оплата получена, но пополнение временно недоступно. Обратитесь в поддержку.")
            return

        credited = int((result or {}).get("rub_amount") or rub_amount)
        await _send(
            bot, tg_id,
            f"✅ <b>Баланс пополнен!</b>\n\n"
            f"💳 Зачислено: <b>{credited} ₽</b>\n\n"
            "Используйте баланс при следующей покупке подписки.",
            keyboard=main_menu_keyboard(settings.support_url),
        )
        return

    idem_key = f"platega-payment-success:{transaction_id}"

    async def _process() -> dict:
        if payment.get("status") not in ("paid", "provisioning", "active"):
            await payments_repo.mark_paid(
                payload=transaction_id,
                telegram_charge_id=transaction_id,
            )
            if purchase_type == "renewal":
                await subs_repo.create_or_extend(tg_id, months=tariff["months"])
                base_limit = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
                # Atomic increment — prevents lost updates when two payments process concurrently.
                await users_repo.add_traffic_limit(tg_id, base_limit)
                if renew_key_id is not None:
                    await keys_repo.add_traffic_limit(renew_key_id, tg_id, base_limit)
            else:
                base_limit = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
                await users_repo.add_traffic_limit(tg_id, base_limit)
            # Balance deduction inside idempotency — runs at most once per payment.
            if balance_applied > 0:
                deducted = await users_repo.deduct_balance(tg_id, balance_applied)
                if not deducted:
                    logger.error(
                        "event=BALANCE_DEDUCT_FAILED tg_id=%s amount=%s transaction_id=%s",
                        tg_id, balance_applied, transaction_id,
                    )
        return {
            "tg_id": tg_id,
            "tariff_code": tariff_code,
            "amount": full_tariff_price,  # full plan price for referral bonus calculation
            "balance_applied": balance_applied,
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
        # expires_dt computed below after fetching the specific key
        expires_dt = add_months(activated_dt, tariff["months"])  # safe placeholder
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
                tg_id, expires_at=expires_dt.isoformat(),
                is_active=True, plan="monthly", last_activated_at=activated_at,
            )

    expires_str = expires_dt.strftime("%d.%m.%Y")
    days_remaining = max(0, (expires_dt - utc_now()).days)
    expiry_ms = int(expires_dt.timestamp() * 1000)

    if purchase_type == "renewal" and renew_key_id is not None:
        from app.services.access import build_vpn_manager
        manager = build_vpn_manager(db, settings, bot=bot)
        renewed_key = await keys_repo.get_by_id_for_user(renew_key_id, tg_id)
        key_traffic_gb = int((renewed_key or {}).get("traffic_limit_gb") or 0) or None

        key_expires_raw = (renewed_key or {}).get("expires_at")
        if key_expires_raw:
            try:
                key_base = max(parse_iso_utc(key_expires_raw), activated_dt)
            except Exception:
                key_base = activated_dt
        else:
            key_base = activated_dt
        expires_dt = add_months(key_base, tariff["months"])
        expiry_ms = int(expires_dt.timestamp() * 1000)
        expires_str = expires_dt.strftime("%d.%m.%Y")
        days_remaining = max(0, (expires_dt - utc_now()).days)

        # users.expires_at — update immediately (watchdog safety net); safe before XUI.
        if user:
            current_user_expires_raw = (user or {}).get("expires_at")
            if current_user_expires_raw:
                try:
                    user_expires_dt = max(expires_dt, parse_iso_utc(current_user_expires_raw))
                except Exception:
                    user_expires_dt = expires_dt
            else:
                user_expires_dt = expires_dt
            await users_repo.set_expiry(
                tg_id, expires_at=user_expires_dt.isoformat(),
                is_active=True, plan="monthly", last_activated_at=activated_at,
            )

        # XUI first — only write keys.expires_at after XUI confirms success.
        # Separate idempotency key ensures Platega webhook retries don't call
        # renew_user_access more than once.
        renewal_idem_key = f"xui-renewal:{transaction_id}"
        renewal_idem = IdempotencyService(IdempotencyRepository())

        async def _renew_xui() -> dict:
            await manager.renew_user_access(tg_id, expiry_ms, key_id=renew_key_id, traffic_limit_gb=key_traffic_gb)
            await keys_repo.update_expires_at(renew_key_id, tg_id, expires_dt.isoformat())
            return {"renewed": True}

        await payments_repo.mark_provisioning(transaction_id)
        try:
            await renewal_idem.execute("xui_renewal", renewal_idem_key, _renew_xui)
        except Exception:
            logger.exception(
                "event=RENEWAL_XUI_FAILED tg_id=%s key_id=%s transaction_id=%s",
                tg_id, renew_key_id, transaction_id,
            )
            failed_payment = await payments_repo.mark_failed(transaction_id, "renewal_xui")
            if failed_payment:
                await notify_provisioning_failed(bot, tg_id, transaction_id, settings.admin_ids)
            return

        await payments_repo.mark_active(transaction_id)
        balance_applied = int(processed.get("balance_applied") or 0)
        balance_line = f"\n💰 Баланс списан: {balance_applied} ₽" if balance_applied > 0 else ""
        text = (
            f"🎉 <b>Подписка продлена!</b>\n\n"
            f"🔑 Ключ #{renew_key_id}\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)"
            f"{balance_line}"
        )
        await _send(bot, tg_id, text, keyboard=renewal_success_keyboard(renew_key_id))
        amount_rub = int(payment.get("amount") or 0)
        tariff_code = str(payment.get("tariff_code") or "")
        await notify_admins(
            bot, settings.admin_ids,
            f"💳 <b>Platega — Продление</b>\n\n"
            f"👤 <code>{tg_id}</code>\n"
            f"🔑 Ключ #{renew_key_id} | {tariff_code} | <b>{amount_rub} RUB</b>\n"
            f"📅 До: {expires_str}",
        )
        referral_idem = IdempotencyService(IdempotencyRepository())
        async def _accrue_ref_bonus() -> dict:
            svc = ReferralService(users_repo, settings.referral_bonus_percent)
            b = await svc.accrue_bonus(user, int(processed.get("amount") or 0))
            return {"bonus": b}
        try:
            ref_res = await referral_idem.execute("referral_bonus", f"referral-bonus:{idem_key}", _accrue_ref_bonus)
            bonus = int(ref_res.get("bonus") or 0)
        except Exception:
            logger.exception("Platega: referral bonus failed transaction_id=%s", transaction_id)
            bonus = 0
        if bonus > 0:
            inviter_tg_id = int((user or {}).get("ref_tg_id") or 0)
            if inviter_tg_id:
                await _send(bot, inviter_tg_id, f"🎁 Реферальный бонус: +{bonus} RUB\n\nВаш реферал продлил подписку!")
        return

    # New key: provision VPN access — wrapped in its own idempotency so that
    # webhook retries don't create duplicate keys.
    link = ""
    sub_token = ""
    provisioned_key_id = 0
    vpn_idem_key = f"vpn-provision:{transaction_id}"
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

    await payments_repo.mark_provisioning(transaction_id)
    try:
        vpn_result = await vpn_idem.execute("vpn_provision", vpn_idem_key, _provision_vpn)
        link = vpn_result.get("vpn_key", "")
        sub_token = vpn_result.get("key_sub_token", "")
        provisioned_key_id = int(vpn_result.get("key_id") or 0)
        link, provisioned_key_id, sub_token = await _repair_provision_result(
            keys_repo, tg_id, str(link or ""), provisioned_key_id, str(sub_token or "")
        )
        if provisioned_key_id:
            key_traffic_gb = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
            try:
                await keys_repo.update_expires_at(provisioned_key_id, tg_id, new_exp.isoformat())
            except Exception:
                logger.warning("Platega: failed to store key expiry tg_id=%s key_id=%s", tg_id, provisioned_key_id)
            try:
                await keys_repo.update_traffic_limit(provisioned_key_id, tg_id, key_traffic_gb)
            except Exception:
                logger.warning("Platega: failed to store key traffic tg_id=%s key_id=%s", tg_id, provisioned_key_id)
        await payments_repo.mark_active(transaction_id)
    except AccessEnsureError:
        logger.exception(
            "event=PROV_FAILED tg_id=%s transaction_id=%s error=access_ensure",
            tg_id, transaction_id,
        )
        failed_payment = await payments_repo.mark_failed(transaction_id, "access_ensure")
        if failed_payment:
            await notify_provisioning_failed(bot, tg_id, transaction_id, settings.admin_ids)
        return
    except Exception:
        logger.exception(
            "event=PROV_FAILED tg_id=%s transaction_id=%s error=vpn_provision",
            tg_id, transaction_id,
        )
        failed_payment = await payments_repo.mark_failed(transaction_id, "vpn_provision")
        if failed_payment:
            await notify_provisioning_failed(bot, tg_id, transaction_id, settings.admin_ids)
        return

    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if sub_token and settings.public_base_url else ""
    balance_applied = int(processed.get("balance_applied") or 0)
    balance_line = f"\n💰 Баланс списан: {balance_applied} ₽" if balance_applied > 0 else ""
    if sub_url:
        text = (
            "🎉 <b>Готово! VPN активирован.</b>\n\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)"
            f"{balance_line}\n\n"
            "Нажмите «📲 Подключить устройство» — настройка займёт 1 минуту."
        )
        await _send(bot, tg_id, text, keyboard=payment_success_keyboard(sub_url, key_id=provisioned_key_id))
    else:
        text = (
            "🎉 <b>Готово! VPN активирован.</b>\n\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)"
            f"{balance_line}\n\n"
            "⏳ Ключ создаётся, через минуту откройте «Мои ключи»."
        )
        await _send(bot, tg_id, text, keyboard=renewal_success_keyboard(provisioned_key_id))

    amount_rub = int(payment.get("amount") or 0)
    tariff_code = str(payment.get("tariff_code") or "")
    await notify_admins(
        bot, settings.admin_ids,
        f"💳 <b>Platega — Новый ключ</b>\n\n"
        f"👤 <code>{tg_id}</code>\n"
        f"📦 {tariff_code} | <b>{amount_rub} RUB</b>\n"
        f"🔑 Ключ #{provisioned_key_id} | до {expires_str}",
    )

    referral_idem = IdempotencyService(IdempotencyRepository())
    async def _accrue_ref_bonus() -> dict:
        svc = ReferralService(users_repo, settings.referral_bonus_percent)
        b = await svc.accrue_bonus(user, int(processed.get("amount") or 0))
        return {"bonus": b}
    try:
        ref_res = await referral_idem.execute("referral_bonus", f"referral-bonus:{idem_key}", _accrue_ref_bonus)
        bonus = int(ref_res.get("bonus") or 0)
    except Exception:
        logger.exception("Platega: referral bonus failed transaction_id=%s", transaction_id)
        bonus = 0
    if bonus > 0:
        inviter_tg_id = int((user or {}).get("ref_tg_id") or 0)
        if inviter_tg_id:
            await _send(bot, inviter_tg_id, f"🎁 Реферальный бонус: +{bonus} RUB\n\nВаш реферал купил подписку!")


async def _send(bot: Bot, tg_id: int, text: str, keyboard=None) -> None:
    try:
        await bot.send_message(tg_id, text, reply_markup=keyboard)
    except TelegramForbiddenError:
        logger.debug("Platega handler: user blocked bot tg_id=%s", tg_id)
    except Exception:
        logger.exception("Platega handler: send_message failed tg_id=%s", tg_id)
