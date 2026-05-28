from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.keyboards.inline import access_activated_text, main_menu_keyboard, payment_success_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.users import UsersRepository
from app.services.access import ensure_user_access
from app.utils.admin_notify import notify_admins
from app.utils.datetime import utc_now
from app.utils.analytics import track
from app.utils.tg import is_trial_eligible

router = Router()
logger = logging.getLogger(__name__)

TRIAL_HOURS = 24
TRIAL_TRAFFIC_GB = 10


@router.callback_query(F.data == "trial_start")
async def trial_start(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    tg_id = callback.from_user.id
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)

    track(tg_id, "trial_button_clicked")
    eligible = await is_trial_eligible(tg_id, db)
    if not eligible:
        await callback.answer(
            "Пробный период уже использован или у вас есть активный ключ.",
            show_alert=True,
        )
        return

    await callback.answer()

    now = utc_now()
    trial_expires = now + timedelta(hours=TRIAL_HOURS)

    await users_repo.set_expiry(
        tg_id,
        expires_at=trial_expires.isoformat(),
        is_active=True,
        plan="trial",
        last_activated_at=now.isoformat(),
    )
    await users_repo.mark_trial_used(tg_id)

    try:
        result = await ensure_user_access(
            tg_id=tg_id,
            db=db,
            settings=settings,
            require_active=True,
            force_new_key=True,
            action="create",
        )
    except Exception:
        logger.exception("Trial provisioning failed tg_id=%s", tg_id)
        await users_repo.set_expiry(tg_id, expires_at=now.isoformat(), is_active=False, plan="none")
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            "⚠️ Не удалось активировать пробный период. Попробуйте позже или обратитесь в поддержку.",
            reply_markup=main_menu_keyboard(settings.support_url),
        )
        return

    key_id = int(result.get("key_id") or 0)
    sub_token = str(result.get("key_sub_token") or "")

    if key_id:
        try:
            await keys_repo.update_expires_at(key_id, tg_id, trial_expires.isoformat())
        except Exception:
            logger.warning("Trial: failed to set key expiry tg_id=%s key_id=%s", tg_id, key_id)
        try:
            await keys_repo.update_traffic_limit(key_id, tg_id, TRIAL_TRAFFIC_GB)
        except Exception:
            logger.warning("Trial: failed to set key traffic tg_id=%s key_id=%s", tg_id, key_id)

    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if sub_token and settings.public_base_url else ""

    try:
        await callback.message.delete()
    except Exception:
        pass

    if sub_url:
        text = access_activated_text("24 часа", f"{TRIAL_TRAFFIC_GB} ГБ", sub_url)
        await callback.message.answer(text, reply_markup=payment_success_keyboard(sub_url, key_id=key_id))
    else:
        text = (
            "🎁 <b>Пробный период активирован!</b>\n\n"
            "⏳ Действует: <b>24 часа</b>\n"
            f"📊 Трафик: <b>{TRIAL_TRAFFIC_GB} ГБ</b>\n\n"
            "⏳ VPN-ключ создаётся. Используйте «Мои ключи» через минуту."
        )
        await callback.message.answer(text, reply_markup=main_menu_keyboard(settings.support_url))

    u = callback.from_user
    uname = f"@{u.username}" if u.username else str(tg_id)
    await notify_admins(
        callback.bot, settings.admin_ids,
        f"🎁 <b>Пробный период</b>\n\n"
        f"👤 {uname} / <code>{tg_id}</code>\n"
        f"🔑 Ключ #{key_id} | 24 часа / {TRIAL_TRAFFIC_GB} ГБ",
    )
    logger.info("event=TRIAL_ACTIVATED tg_id=%s key_id=%s", tg_id, key_id)
    track(tg_id, "trial_started", {"key_id": key_id})
