from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message, PreCheckoutQuery

from app.bot.keyboards.inline import main_menu_keyboard, payment_success_keyboard
from app.bot.keyboards.main import get_main_menu_keyboard
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
        await message.answer("Платеж не найден.", reply_markup=get_main_menu_keyboard())
        return

    idem_key = f"payment-success:{payment_info.invoice_payload}"
    logger.info("Payment callback received payload=%s tg_id=%s", payment_info.invoice_payload, payment.get("tg_id"))

    # Handle balance top-up separately — no VPN provisioning needed.
    if payment.get("purchase_type") == "topup":
        stars_amount = int(payment.get("amount") or 0)
        if stars_amount > 0:
            try:
                await payments_repo.mark_paid(
                    payload=payment_info.invoice_payload,
                    telegram_charge_id=payment_info.telegram_payment_charge_id,
                )
                await users_repo.add_balance(int(payment["tg_id"]), stars_amount)
            except Exception:
                logger.exception("Failed to process topup tg_id=%s", payment.get("tg_id"))
                await message.answer("Оплата получена, но пополнение временно недоступно. Обратитесь в поддержку.", reply_markup=get_main_menu_keyboard())
                return
        await message.answer(
            f"✅ <b>Баланс пополнен!</b>\n\n"
            f"💳 Зачислено: <b>{stars_amount} RUB</b>",
            reply_markup=get_main_menu_keyboard(),
        )
        await message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))
        return

    async def _process_payment() -> dict:
        purchase_type = str(payment.get("purchase_type") or "new")
        renew_key_id = _require_renew_key_id(payment.get("renew_key_id")) if purchase_type == "renewal" else None
        if purchase_type == "renewal":
            key_row = await keys_repo.get_by_id_for_user(renew_key_id, int(payment["tg_id"]))
            if not key_row:
                raise RuntimeError(f"renewal key not found tg_id={payment['tg_id']} key_id={renew_key_id}")

        if payment.get("status") != "paid":
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

        if user:
            await users_repo.set_expiry(
                tg_id, expires_at=expires_dt.isoformat(),
                is_active=True, plan="monthly", last_activated_at=activated_at,
            )
        try:
            manager = build_vpn_manager(db, settings, bot=message.bot)
            await manager.renew_user_access(
                tg_id, expiry_ms, key_id=key_id_int, traffic_limit_gb=per_key_traffic_gb
            )
        except Exception:
            logger.exception("Failed to update XUI expiry after renewal tg_id=%s key_id=%s", tg_id, renew_key_id)
        try:
            await keys_repo.update_expires_at(key_id_int, tg_id, expires_dt.isoformat())
        except Exception:
            logger.exception("Failed to update key expires_at tg_id=%s key_id=%s", tg_id, renew_key_id)

        key_label = f" ключа #{key_id_int}"
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"🔄 <b>Продление{key_label}</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
            "📊 Статус: <b>Активна</b>"
        )
        await message.answer(text, reply_markup=get_main_menu_keyboard())
        await message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))

        referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
        bonus = await referral_service.accrue_bonus(user, int(processed["amount"]))
        if bonus > 0:
            await message.answer(f"Реферальный бонус: +{bonus} RUB", reply_markup=get_main_menu_keyboard())
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

    try:
        vpn_result = await vpn_idem.execute("vpn_provision", vpn_idem_key, _provision_vpn)
        link = vpn_result.get("vpn_key", "")
        sub_token = vpn_result.get("key_sub_token", "")
        # Update key metadata outside idempotency — both calls are idempotent SETs that
        # repair any previous run where the key was provisioned but metadata not written.
        provisioned_key_id = int(vpn_result.get("key_id") or 0)
        if provisioned_key_id:
            key_traffic_gb = int(tariff.get("traffic_gb", tariff.get("months", 1) * 60))
            try:
                # Use the plan's own duration, not the global MAX used for users.expires_at.
                await keys_repo.update_expires_at(provisioned_key_id, tg_id, new_key_expires_dt.isoformat())
            except Exception:
                logger.warning("Failed to store per-key expiry tg_id=%s key_id=%s", tg_id, provisioned_key_id)
            try:
                await keys_repo.update_traffic_limit(provisioned_key_id, tg_id, key_traffic_gb)
            except Exception:
                logger.warning("Failed to store per-key traffic limit tg_id=%s key_id=%s", tg_id, provisioned_key_id)
    except AccessEnsureError:
        logger.exception("Failed to bootstrap access after payment for tg_id=%s", tg_id)
        await message.answer("Оплата прошла, но ключ пока не создан. Попробуйте позже.", reply_markup=get_main_menu_keyboard())
        return
    except Exception:
        logger.exception("VPN provisioning idempotency failed tg_id=%s", tg_id)
        await message.answer("Оплата прошла, но ключ пока не создан. Попробуйте позже.", reply_markup=get_main_menu_keyboard())
        return

    sub_url = f"{settings.public_base_url}/sub/{escape(sub_token)}" if sub_token and settings.public_base_url else ""

    if link and sub_url:
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            "📦 <b>Подписка активирована</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n"
            "📊 Статус: <b>Активна</b>\n\n"
            "Нажмите «Подключить» чтобы настроить VPN-клиент,\n"
            "или «Показать QR» для сканирования."
        )
        await message.answer(text, reply_markup=payment_success_keyboard(sub_url, key_id=provisioned_key_id))
    else:
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            "📦 <b>Подписка активирована</b>\n"
            f"📅 Действует до: <b>{expires_str}</b> ({days_remaining} дн.)\n\n"
            "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
        )
        await message.answer(text, reply_markup=get_main_menu_keyboard())

    referral_service = ReferralService(users_repo, settings.referral_bonus_percent)
    bonus = await referral_service.accrue_bonus(user, int(processed["amount"]))
    paid_count = await payments_repo.count_paid(tg_id)
    friend_bonus = await referral_service.accrue_friend_bonus(user, paid_count, settings.referral_friend_bonus_rub)
    await message.answer("Главное меню", reply_markup=main_menu_keyboard(settings.support_url))
    if friend_bonus > 0:
        await message.answer(f"🎁 Вам начислен реферальный бонус: +{friend_bonus} RUB на баланс", reply_markup=get_main_menu_keyboard())


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
