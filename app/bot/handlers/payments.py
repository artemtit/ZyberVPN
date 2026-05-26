from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message, PreCheckoutQuery

from app.bot.keyboards.inline import access_activated_text, main_menu_keyboard, payment_success_keyboard, renewal_success_keyboard
from app.bot.keyboards.main import get_main_menu_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.keys import KeysRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.plans import get_plan_by_tariff_code
from app.services.access import AccessEnsureError, build_vpn_manager, ensure_user_access
from app.services.idempotency import IdempotencyService
from app.services.provisioning_failures import notify_provisioning_failed
from app.services.referrals import ReferralService
from app.services.tariffs import TARIFFS
from app.services.vpn import qr_png_from_text
from app.utils.admin_notify import notify_admins
from app.utils.datetime import add_months, parse_iso_utc, to_moscow, utc_now

router = Router()
logger = logging.getLogger(__name__)


def _require_renew_key_id(raw: object) -> int:
    if raw is None:
        raise ValueError("renew_key_id is required for renewal")
    key_id = int(raw)
    if key_id <= 0:
        raise ValueError("renew_key_id must be positive")
    return key_id


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


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


async def _handle_subscription_payment(message: Message, db: Database, settings: Settings) -> None:
    """Handle Stars subscription renewal (payload starts with 'sub:')."""
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    charge_id = payment_info.telegram_payment_charge_id
    tg_id = message.from_user.id

    # Parse payload: sub:m1:{tg_id}:{key_id}
    try:
        _, tariff_code, payload_tg_id_str, key_id_str = payload.split(":", 3)
        payload_tg_id = int(payload_tg_id_str)
        key_id = int(key_id_str)
    except (ValueError, TypeError):
        logger.error("Invalid subscription payload tg_id=%s payload=%s", tg_id, payload)
        await message.answer("Ошибка обработки подписки. Обратитесь в поддержку.", reply_markup=get_main_menu_keyboard())
        return

    if payload_tg_id != tg_id:
        logger.error("Sub payment tg_id mismatch payload_tg_id=%s actual=%s", payload_tg_id, tg_id)
        return

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)
    idem = IdempotencyService(IdempotencyRepository())
    idem_key = f"sub-payment:{charge_id}"

    async def _process_sub() -> dict:
        plan = get_plan_by_tariff_code(tariff_code) or get_plan_by_tariff_code("m1")
        months = 1
        traffic_gb = int((plan or {}).get("traffic_gb", 60))

        key_row = await keys_repo.get_by_id_for_user(key_id, tg_id)
        if not key_row:
            raise RuntimeError(f"sub renewal: key not found tg_id={tg_id} key_id={key_id}")

        now = utc_now()
        key_expires_raw = key_row.get("expires_at")
        if key_expires_raw:
            try:
                key_base = max(parse_iso_utc(key_expires_raw), now)
            except Exception:
                key_base = now
        else:
            key_base = now
        new_expires_dt = add_months(key_base, months)
        new_expiry_ms = int(new_expires_dt.timestamp() * 1000)

        await subs_repo.create_or_extend(tg_id, months=months)
        await users_repo.add_traffic_limit(tg_id, traffic_gb)

        user = await users_repo.get_by_tg_id(tg_id)
        current_user_expires = (user or {}).get("expires_at")
        user_expires_dt = new_expires_dt
        if current_user_expires:
            try:
                user_expires_dt = max(new_expires_dt, parse_iso_utc(current_user_expires))
            except Exception:
                pass
        await users_repo.set_expiry(
            tg_id, expires_at=user_expires_dt.isoformat(),
            is_active=True, plan="monthly", last_activated_at=now.isoformat(),
        )

        per_key_traffic = int(key_row.get("traffic_limit_gb") or traffic_gb)
        manager = build_vpn_manager(db, settings, bot=message.bot)
        await manager.renew_user_access(tg_id, new_expiry_ms, key_id=key_id, traffic_limit_gb=per_key_traffic)

        await keys_repo.update_expires_at(key_id, tg_id, new_expires_dt.isoformat())
        await keys_repo.add_traffic_limit(key_id, tg_id, traffic_gb)
        await users_repo.set_auto_renew(tg_id, key_id)

        return {
            "expires_str": to_moscow(new_expires_dt).strftime("%d.%m.%Y"),
            "days_remaining": max(0, (new_expires_dt - now).days),
        }

    try:
        result = await idem.execute("sub_payment", idem_key, _process_sub)
    except Exception:
        logger.exception("Subscription payment failed tg_id=%s key_id=%s charge_id=%s", tg_id, key_id, charge_id)
        await message.answer(
            "⚠️ Платёж получен, но продление временно недоступно. Обратитесь в поддержку.",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    expires_str = result.get("expires_str", "—")
    days_remaining = result.get("days_remaining", 0)
    logger.info("event=SUB_RENEWED tg_id=%s key_id=%s charge_id=%s expires=%s", tg_id, key_id, charge_id, expires_str)
    await message.answer(
        f"✅ <b>Авто-продление выполнено!</b>\n\n"
        f"🔑 Ключ #{key_id} продлён\n"
        f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
        "📊 Статус: <b>Активна</b>",
        reply_markup=get_main_menu_keyboard(),
    )
    u = message.from_user
    uname = f"@{u.username}" if u.username else str(tg_id)
    await notify_admins(
        message.bot, settings.admin_ids,
        f"⭐ <b>Авто-продление Stars</b>\n\n"
        f"👤 {uname} / <code>{tg_id}</code>\n"
        f"🔑 Ключ #{key_id} | <b>m1</b> — 1 мес.\n"
        f"📅 До: {expires_str}",
    )


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, db: Database, settings: Settings) -> None:
    payment_info = message.successful_payment

    # Subscription renewals have a stable payload starting with "sub:"
    if (payment_info.invoice_payload or "").startswith("sub:"):
        await _handle_subscription_payment(message, db, settings)
        return

    payments_repo = PaymentsRepository(db)
    users_repo = UsersRepository(db)
    subs_repo = SubscriptionsRepository(db)
    idem = IdempotencyService(IdempotencyRepository())

    keys_repo = KeysRepository(db)
    payment = await payments_repo.get_by_payload(payment_info.invoice_payload)
    if not payment:
        await message.answer("Платеж не найден.", reply_markup=get_main_menu_keyboard())
        return

    idem_key = f"payment-success:{payment_info.invoice_payload}"
    logger.info("Payment callback received payload=%s tg_id=%s", payment_info.invoice_payload, payment.get("tg_id"))

    # Handle balance top-up separately — no VPN provisioning needed.
    if payment.get("purchase_type") == "topup":
        rub_amount = int(payment.get("amount") or 0)
        topup_idem_key = f"payment-success:{payment_info.invoice_payload}"

        async def _process_topup() -> dict:
            # Re-fetch current status — stale closure on Telegram retry.
            fresh = await payments_repo.get_by_payload(payment_info.invoice_payload)
            fresh_status = (fresh or {}).get("status", "")
            if fresh_status == "active":
                return {"tg_id": int(payment["tg_id"]), "rub_amount": rub_amount}
            if fresh_status not in ("paid", "provisioning"):
                await payments_repo.mark_paid(
                    payload=payment_info.invoice_payload,
                    telegram_charge_id=payment_info.telegram_payment_charge_id,
                )
            if rub_amount > 0:
                await users_repo.add_balance(int(payment["tg_id"]), rub_amount)
            await payments_repo.mark_active(payment_info.invoice_payload)
            return {"tg_id": int(payment["tg_id"]), "rub_amount": rub_amount}

        try:
            result = await idem.execute("topup_success", topup_idem_key, _process_topup)
        except Exception:
            logger.exception("Failed to process topup tg_id=%s", payment.get("tg_id"))
            await message.answer(
                "Оплата получена, но пополнение временно недоступно. Обратитесь в поддержку.",
                reply_markup=get_main_menu_keyboard(),
            )
            return
        credited = int((result or {}).get("rub_amount") or rub_amount)
        await message.answer(
            f"✅ <b>Баланс пополнен!</b>\n\n"
            f"💳 Зачислено: <b>{credited} ₽</b>\n\n"
            "Используйте баланс при следующей покупке подписки.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(settings.support_url),
        )
        return

    async def _process_payment() -> dict:
        purchase_type = str(payment.get("purchase_type") or "new")
        renew_key_id = _require_renew_key_id(payment.get("renew_key_id")) if purchase_type == "renewal" else None
        if purchase_type == "renewal":
            key_row = await keys_repo.get_by_id_for_user(renew_key_id, int(payment["tg_id"]))
            if not key_row:
                raise RuntimeError(f"renewal key not found tg_id={payment['tg_id']} key_id={renew_key_id}")

        if payment.get("status") not in ("paid", "provisioning", "active"):
            await payments_repo.mark_paid(
                payload=payment_info.invoice_payload,
                telegram_charge_id=payment_info.telegram_payment_charge_id,
            )
            tariff = TARIFFS[str(payment["tariff_code"])]
            if purchase_type == "renewal":
                # Renewal: extend the existing subscription from its current end date.
                # Do NOT touch other keys — each key has independent expiry in keys.expires_at.
                await subs_repo.create_or_extend(int(payment["tg_id"]), months=tariff["months"])
                base_limit = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
                # Atomic increment — prevents lost updates when two payments process concurrently.
                await users_repo.add_traffic_limit(int(payment["tg_id"]), base_limit)
                # Also accumulate per-key traffic limit for the specific renewed key.
                if renew_key_id is not None and key_row:
                    await keys_repo.add_traffic_limit(renew_key_id, int(payment["tg_id"]), base_limit)
            else:
                # New key: accumulate rather than overwrite — a user may already have other
                # keys with accrued traffic. set_traffic_limit would erase prior renewals.
                base_limit = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
                await users_repo.add_traffic_limit(int(payment["tg_id"]), base_limit)
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
        await message.answer(
            "Платеж получен, но обработка временно недоступна. Попробуйте позже.",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    logger.info("Payment processed idempotently payload=%s tg_id=%s", payment_info.invoice_payload, processed["tg_id"])
    tg_id = int(processed["tg_id"])
    purchase_type = str(processed.get("purchase_type") or "new")
    renew_key_id = processed.get("renew_key_id")

    user = await users_repo.get_by_tg_id(tg_id)
    if not user:
        await message.answer("Пользователь не найден.", reply_markup=get_main_menu_keyboard())
        return

    activated_dt = utc_now()
    activated_at = activated_dt.isoformat()
    tariff_code = str(processed.get("tariff_code") or "m1")
    tariff = TARIFFS.get(tariff_code, TARIFFS["m1"])

    if purchase_type == "renewal":
        # expires_dt computed below after fetching the specific key
        expires_dt = add_months(activated_dt, tariff["months"])  # safe placeholder
    else:
        # New key: its OWN independent expiry from NOW (plan duration only).
        # users.expires_at = MAX(new, current) so the watchdog doesn't deactivate
        # the user when a short-term key expires while longer keys remain active.
        new_key_expires_dt = add_months(activated_dt, tariff["months"])
        current_raw = (user or {}).get("expires_at")
        if current_raw:
            try:
                expires_dt = max(new_key_expires_dt, parse_iso_utc(current_raw))
            except Exception:
                expires_dt = new_key_expires_dt
        else:
            expires_dt = new_key_expires_dt
        if user:
            await users_repo.set_expiry(
                tg_id, expires_at=expires_dt.isoformat(),
                is_active=True, plan="monthly", last_activated_at=activated_at,
            )

    expires_str = to_moscow(expires_dt).strftime("%d.%m.%Y")
    days_remaining = max(0, (expires_dt - utc_now()).days)
    expiry_ms = int(expires_dt.timestamp() * 1000)

    if purchase_type == "renewal":
        try:
            key_id_int = _require_renew_key_id(renew_key_id)
        except ValueError:
            logger.error("Renewal payment has no valid key_id tg_id=%s payload=%s", tg_id, payment_info.invoice_payload)
            await message.answer(
                "Платеж получен, но не удалось определить ключ для продления. Напишите в поддержку.",
                reply_markup=get_main_menu_keyboard(),
            )
            return
        # Read the key BEFORE computing new expiry — key.expires_at is the pre-renewal value.
        renewed_key = await keys_repo.get_by_id_for_user(key_id_int, tg_id)
        per_key_traffic_gb = int((renewed_key or {}).get("traffic_limit_gb") or 0) or None

        # Compute true new expiry: extend from the KEY's own current expiry, not from
        # the global subscription (which can be out of sync with this specific key).
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
        expires_str = to_moscow(expires_dt).strftime("%d.%m.%Y")
        days_remaining = max(0, (expires_dt - utc_now()).days)

        # users.expires_at — update immediately (watchdog safety net); safe to do before XUI.
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

        # XUI first — only update keys.expires_at in DB after XUI succeeds.
        # Separate idempotency key ensures Telegram retries don't call
        # renew_user_access more than once.
        renewal_idem_key = f"xui-renewal:{payment_info.invoice_payload}"
        renewal_idem = IdempotencyService(IdempotencyRepository())

        async def _renew_xui() -> dict:
            mgr = build_vpn_manager(db, settings, bot=message.bot)
            await mgr.renew_user_access(
                tg_id, expiry_ms, key_id=key_id_int, traffic_limit_gb=per_key_traffic_gb
            )
            await keys_repo.update_expires_at(key_id_int, tg_id, expires_dt.isoformat())
            return {"renewed": True}

        await payments_repo.mark_provisioning(payment_info.invoice_payload)
        try:
            await renewal_idem.execute("xui_renewal", renewal_idem_key, _renew_xui)
        except Exception:
            logger.exception(
                "event=RENEWAL_XUI_FAILED tg_id=%s key_id=%s payload=%s",
                tg_id, key_id_int, payment_info.invoice_payload,
            )
            failed_payment = await payments_repo.mark_failed(payment_info.invoice_payload, "renewal_xui")
            if failed_payment:
                await notify_provisioning_failed(message.bot, tg_id, payment_info.invoice_payload, settings.admin_ids)
            return

        await payments_repo.mark_active(payment_info.invoice_payload)

        text = (
            f"🎉 <b>Подписка продлена!</b>\n\n"
            f"🔑 Ключ #{key_id_int}\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)"
        )
        await message.answer(text, reply_markup=renewal_success_keyboard(key_id_int))
        u = message.from_user
        uname = f"@{u.username}" if u.username else str(tg_id)
        amount_rub = int(processed.get("amount") or 0)
        tariff_str = str(processed.get("tariff_code") or "")
        await notify_admins(
            message.bot, settings.admin_ids,
            f"⭐ <b>Stars — Продление</b>\n\n"
            f"👤 {uname} / <code>{tg_id}</code>\n"
            f"🔑 Ключ #{key_id_int} | {tariff_str} | <b>{amount_rub} RUB</b>\n"
            f"📅 До: {expires_str}",
        )

        referral_idem = IdempotencyService(IdempotencyRepository())
        async def _accrue_ref_renewal() -> dict:
            svc = ReferralService(users_repo, settings.referral_bonus_percent)
            b = await svc.accrue_bonus(user, int(processed["amount"]))
            return {"bonus": b}
        try:
            ref_res = await referral_idem.execute("referral_bonus", f"referral-bonus:{idem_key}", _accrue_ref_renewal)
            bonus = int(ref_res.get("bonus") or 0)
        except Exception:
            logger.exception("Referral bonus failed payload=%s", payment_info.invoice_payload)
            bonus = 0
        if bonus > 0:
            inviter_tg_id = int((user or {}).get("ref_tg_id") or 0)
            if inviter_tg_id:
                try:
                    await message.bot.send_message(
                        inviter_tg_id,
                        f"🎁 Реферальный бонус: +{bonus} RUB\n\nВаш реферал продлил подписку!",
                    )
                except Exception:
                    logger.warning("Failed to notify inviter tg_id=%s", inviter_tg_id)
        return

    # New key: provision a fresh VPN key — wrapped in its own idempotency so that
    # webhook retries don't create duplicate keys.
    link = ""
    sub_token = ""
    provisioned_key_id = 0
    vpn_idem_key = f"vpn-provision:{payment_info.invoice_payload}"
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

    await payments_repo.mark_provisioning(payment_info.invoice_payload)
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
                await keys_repo.update_expires_at(provisioned_key_id, tg_id, new_key_expires_dt.isoformat())
            except Exception:
                logger.warning("Failed to store per-key expiry tg_id=%s key_id=%s", tg_id, provisioned_key_id)
            try:
                await keys_repo.update_traffic_limit(provisioned_key_id, tg_id, key_traffic_gb)
            except Exception:
                logger.warning("Failed to store per-key traffic limit tg_id=%s key_id=%s", tg_id, provisioned_key_id)
        await payments_repo.mark_active(payment_info.invoice_payload)
    except AccessEnsureError:
        logger.exception(
            "event=PROV_FAILED tg_id=%s payload=%s error=access_ensure",
            tg_id, payment_info.invoice_payload,
        )
        failed_payment = await payments_repo.mark_failed(payment_info.invoice_payload, "access_ensure")
        if failed_payment:
            await notify_provisioning_failed(message.bot, tg_id, payment_info.invoice_payload, settings.admin_ids)
        return
    except Exception:
        logger.exception(
            "event=PROV_FAILED tg_id=%s payload=%s error=vpn_provision",
            tg_id, payment_info.invoice_payload,
        )
        failed_payment = await payments_repo.mark_failed(payment_info.invoice_payload, "vpn_provision")
        if failed_payment:
            await notify_provisioning_failed(message.bot, tg_id, payment_info.invoice_payload, settings.admin_ids)
        return

    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if sub_token and settings.public_base_url else ""

    if sub_url:
        traffic_gb = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
        text = access_activated_text(f"до {expires_str} ({days_remaining} дн.)", f"{traffic_gb} ГБ", sub_url)
        await message.answer(text, reply_markup=payment_success_keyboard(sub_url, key_id=provisioned_key_id))
    else:
        text = (
            "🎉 <b>Готово! VPN активирован.</b>\n\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n\n"
            "⏳ Ключ создаётся, через минуту откройте «Мои ключи»."
        )
        await message.answer(text, reply_markup=renewal_success_keyboard(provisioned_key_id))

    u = message.from_user
    uname = f"@{u.username}" if u.username else str(tg_id)
    amount_rub = int(processed.get("amount") or 0)
    tariff_str = str(processed.get("tariff_code") or "")
    await notify_admins(
        message.bot, settings.admin_ids,
        f"⭐ <b>Stars — Новый ключ</b>\n\n"
        f"👤 {uname} / <code>{tg_id}</code>\n"
        f"📦 {tariff_str} | <b>{amount_rub} RUB</b>\n"
        f"🔑 Ключ #{provisioned_key_id} | до {expires_str}",
    )

    referral_idem = IdempotencyService(IdempotencyRepository())
    async def _accrue_referral() -> dict:
        svc = ReferralService(users_repo, settings.referral_bonus_percent)
        b = await svc.accrue_bonus(user, int(processed["amount"]))
        pc = await payments_repo.count_paid(tg_id)
        fb = await svc.accrue_friend_bonus(user, pc, settings.referral_friend_bonus_rub)
        return {"inviter_bonus": b, "friend_bonus": fb}
    try:
        referral_result = await referral_idem.execute("referral_bonus", f"referral-bonus:{idem_key}", _accrue_referral)
        inviter_bonus = int(referral_result.get("inviter_bonus") or 0)
        friend_bonus = int(referral_result.get("friend_bonus") or 0)
    except Exception:
        logger.exception("Referral bonus failed payload=%s", payment_info.invoice_payload)
        inviter_bonus = 0
        friend_bonus = 0
    if friend_bonus > 0:
        await message.answer(f"🎁 Реферальный бонус зачислен: +{friend_bonus} ₽ на баланс")
    if inviter_bonus > 0:
        inviter_tg_id = int((user or {}).get("ref_tg_id") or 0)
        if inviter_tg_id:
            try:
                await message.bot.send_message(
                    inviter_tg_id,
                    f"🎁 Реферальный бонус: +{inviter_bonus} RUB\n\nВаш реферал купил подписку!",
                )
            except Exception:
                logger.warning("Failed to notify inviter tg_id=%s", inviter_tg_id)


@router.callback_query(F.data == "payment_show_qr")
async def show_payment_qr(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    user_keys = await keys_repo.list_by_user(callback.from_user.id)
    primary_key = next((k for k in user_keys if k.get("is_primary")), user_keys[0] if user_keys else None)
    key_sub_token = str((primary_key or {}).get("sub_token") or "")
    if not key_sub_token and primary_key:
        try:
            key_sub_token = await keys_repo.ensure_sub_token(int(primary_key["id"]), callback.from_user.id)
        except Exception:
            pass
    if not key_sub_token or not settings.public_base_url:
        await callback.answer("Subscription URL не найден", show_alert=True)
        return
    sub_url = f"{settings.public_base_url}/sub/{key_sub_token}"
    qr_bytes = qr_png_from_text(sub_url)
    await callback.message.answer_photo(
        BufferedInputFile(qr_bytes, filename="subscription-qr.png"),
        caption=f"QR-код для подключения\n<code>{escape(sub_url)}</code>",
    )
    await callback.answer()
