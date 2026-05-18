from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.inline import key_card_keyboard, keys_list_keyboard, trial_expired_keyboard
from app.bot.keyboards.main import get_main_menu_keyboard
from app.bot.states.keys import KeyCommentState
from app.utils.tg import photo_to_text
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager
from app.services.vpn import qr_png_from_text
from app.utils.datetime import parse_iso_utc, to_moscow, utc_diff, utc_now

router = Router()
logger = logging.getLogger(__name__)


def _remaining_parts(expires_at: datetime) -> tuple[int, int, int]:
    delta = utc_diff(expires_at, utc_now())
    total_seconds = int(max(delta.total_seconds(), 0))
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    return days, hours, minutes



def _key_label(is_primary: bool, num: int, days_left: int, hours_left: int, is_active: bool) -> str:
    icon = "⭐" if is_primary else "🔑"
    if not is_active:
        status = "истёк"
    elif days_left == 0:
        status = f"{hours_left} ч."
    elif days_left < 30:
        status = f"{days_left} дн."
    else:
        status = f"{days_left // 30} мес."
    return f"{icon} Ключ #{num} — {status}"


def _get_expired_trial_keys(all_keys: list[dict]) -> list[dict]:
    return [
        k for k in all_keys
        if k.get("disabled_at") and str(k.get("key") or "").startswith("vless://")
    ]


@router.message(Command("keys"))
async def keys_command(message: Message, db: Database) -> None:
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)
    await users_repo.get_or_create(message.from_user.id)
    all_keys_raw = await keys_repo.list_by_user(message.from_user.id)
    keys = [k for k in all_keys_raw if not k.get("disabled_at")]

    user = await users_repo.get_by_tg_id(message.from_user.id)
    is_trial = str((user or {}).get("plan") or "") == "trial"
    expired_trial = _get_expired_trial_keys(all_keys_raw) if is_trial and not keys else []

    if not keys and not expired_trial:
        if user and user.get("trial_used"):
            await message.answer(
                "⏰ <b>Пробный период завершён</b>\n\n"
                "Выберите тариф, чтобы продолжить:",
                reply_markup=trial_expired_keyboard(),
            )
        else:
            await message.answer(
                "🔑 <b>Мои ключи</b>\n\n"
                "У вас пока нет активных ключей.\n\n"
                "👇 Попробуйте VPN бесплатно на 1 день или купите подписку:",
                reply_markup=keys_list_keyboard([]),
            )
        return

    active_sub = await subs_repo.get_active(message.from_user.id)
    key_rows: list[tuple[str, str]] = []
    for num, key_data in enumerate(keys, start=1):
        is_primary = bool(key_data.get("is_primary"))
        exp_raw = key_data.get("expires_at") or (active_sub["expires_at"] if active_sub else None)
        if exp_raw:
            exp_dt = parse_iso_utc(exp_raw)
            days, hours, _ = _remaining_parts(exp_dt)
            is_active_key = exp_dt > utc_now()
        else:
            days, hours, is_active_key = 0, 0, False
        label = _key_label(is_primary, num, days, hours, is_active_key)
        key_rows.append((label, str(key_data["id"])))

    expired_trial_rows = [
        (f"🔴 Ключ #{len(keys)+i+1} — истёк (пробный, нажмите для продления)", str(k["id"]))
        for i, k in enumerate(expired_trial)
    ]
    await message.answer("🔑 <b>Мои ключи</b>", reply_markup=keys_list_keyboard(key_rows, expired_trial_rows))


@router.callback_query(F.data == "menu_keys")
async def keys_list(callback: CallbackQuery, db: Database) -> None:
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)
    await users_repo.get_or_create(callback.from_user.id)
    all_keys_raw = await keys_repo.list_by_user(callback.from_user.id)
    keys = [k for k in all_keys_raw if not k.get("disabled_at")]

    user = await users_repo.get_by_tg_id(callback.from_user.id)
    is_trial = str((user or {}).get("plan") or "") == "trial"
    expired_trial = _get_expired_trial_keys(all_keys_raw) if is_trial and not keys else []

    if not keys and not expired_trial:
        if user and user.get("trial_used"):
            await photo_to_text(
                callback.message,
                "⏰ <b>Пробный период завершён</b>\n\n"
                "Выберите тариф, чтобы продолжить:",
                reply_markup=trial_expired_keyboard(),
            )
        else:
            await photo_to_text(
                callback.message,
                "🔑 <b>Мои ключи</b>\n\n"
                "У вас пока нет активных ключей.\n\n"
                "👇 Попробуйте VPN бесплатно на 1 день или купите подписку:",
                reply_markup=keys_list_keyboard([]),
            )
        await callback.answer()
        return

    active_sub = await subs_repo.get_active(callback.from_user.id)
    key_rows: list[tuple[str, str]] = []
    for num, key_data in enumerate(keys, start=1):
        key_str = str(key_data.get("key") or "")
        is_primary = bool(key_data.get("is_primary", False))

        # Per-key expiry takes priority; fall back to user subscription expiry.
        key_expires_raw = key_data.get("expires_at")
        if key_expires_raw:
            key_expires_at = parse_iso_utc(key_expires_raw)
            key_days, key_hours, _ = _remaining_parts(key_expires_at)
            key_is_active = key_expires_at > utc_now()
        elif active_sub:
            sub_expires_at = parse_iso_utc(active_sub["expires_at"])
            key_days, key_hours, _ = _remaining_parts(sub_expires_at)
            key_is_active = True
        else:
            key_days, key_hours = 0, 0
            key_is_active = False

        label = _key_label(is_primary, num, key_days, key_hours, key_is_active)
        key_rows.append((label, str(key_data["id"])))

    expired_trial_rows = [
        (f"🔴 Ключ #{len(keys)+i+1} — истёк (пробный, нажмите для продления)", str(k["id"]))
        for i, k in enumerate(expired_trial)
    ]
    await photo_to_text(
        callback.message,
        "🔑 <b>Мои ключи</b>",
        reply_markup=keys_list_keyboard(key_rows, expired_trial_rows),
    )
    await callback.answer()


async def _build_key_card(
    tg_id: int, key_id: int, db: Database, settings: Settings, bot=None
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Build key card text + keyboard. Returns None if key not found."""
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)

    await users_repo.get_or_create(tg_id)
    all_keys = [k for k in await keys_repo.list_by_user(tg_id) if not k.get("disabled_at")]
    key_data = next((k for k in all_keys if k["id"] == key_id), None)
    if not key_data:
        return None
    display_num = next(
        (i for i, k in enumerate(all_keys, start=1) if k["id"] == key_id), 1
    )

    created_at = parse_iso_utc(key_data["created_at"])
    key_expires_raw = key_data.get("expires_at")
    if key_expires_raw:
        expires_at = parse_iso_utc(key_expires_raw)
        key_is_active = expires_at > utc_now()
        status_text = "Активен" if key_is_active else "Истек"
        status_emoji = "🟢" if key_is_active else "🔴"
    else:
        active_sub = await subs_repo.get_active(tg_id)
        if active_sub:
            expires_at = parse_iso_utc(active_sub["expires_at"])
            status_text = "Активен"
            status_emoji = "🟢"
        else:
            expires_at = created_at
            status_text = "Истек"
            status_emoji = "🔴"

    days, hours, _ = _remaining_parts(expires_at)

    # Per-key traffic limit (stored in keys table); fall back to users.traffic_limit_gb.
    traffic_limit_gb = int((key_data or {}).get("traffic_limit_gb") or 0)
    if not traffic_limit_gb:
        supabase_user = await users_repo.get_by_tg_id(tg_id)
        traffic_limit_gb = int((supabase_user or {}).get("traffic_limit_gb") or 60)

    # Per-key sub_token: generate one on first access if missing
    key_sub_token = str(key_data.get("sub_token") or "")
    if not key_sub_token and settings.public_base_url:
        try:
            key_sub_token = await keys_repo.ensure_sub_token(key_id, tg_id)
        except Exception:
            logger.warning("Failed to generate sub_token for key_id=%s tg_id=%s", key_id, tg_id)
    sub_url = f"{settings.public_base_url}/sub/{key_sub_token}" if key_sub_token and settings.public_base_url else ""

    traffic_used_gb = 0.0
    online_devices = 0
    limit_exceeded = False
    try:
        manager = build_vpn_manager(db, settings, bot=bot)
        bytes_used, online_devices = await manager.get_client_stats(tg_id, key_id=key_id)
        traffic_used_gb = round(bytes_used / (1024 ** 3), 2)
        if bytes_used > 0 and bytes_used >= traffic_limit_gb * 1024 ** 3:
            limit_exceeded = True
            async def _enforce() -> None:
                try:
                    await build_vpn_manager(db, settings, bot=bot).enforce_traffic_limit(tg_id, key_id=key_id)
                except Exception:
                    pass
            asyncio.create_task(_enforce())
    except Exception:
        logger.warning("get_client_stats failed tg_id=%s", tg_id)

    if limit_exceeded:
        status_text = "Заблокирован (лимит трафика)"
        status_emoji = "🔴"

    supabase_user_for_plan = await users_repo.get_by_tg_id(tg_id)
    is_trial = str((supabase_user_for_plan or {}).get("plan") or "") == "trial"

    is_primary = bool(key_data.get("is_primary", False))
    comment = str(key_data.get("comment") or "").strip()
    sub_line = f"\n🔗 Subscription URL:\n<code>{escape(sub_url)}</code>\n" if sub_url else ""
    comment_line = f"\n📝 Комментарий: {escape(comment)}" if comment else ""

    trial_notice = "\n\n⚠️ <i>Пробный ключ — продление недоступно.\nКупите подписку, чтобы продолжить пользоваться VPN.</i>" if is_trial else ""

    text = (
        f"🔑 Ключ #{display_num}\n\n"
        f"{status_emoji} Статус: {status_text}\n"
        f"⏳ Истекает: {to_moscow(expires_at).strftime('%d.%m.%Y %H:%M')} МСК ({days}д. {hours}ч.)\n"
        f"{sub_line}\n"
        f"📡 Трафик: {traffic_used_gb:.1f} / {traffic_limit_gb} ГБ"
        f"{comment_line}"
        f"{trial_notice}"
    )
    return text, key_card_keyboard(key_id, is_primary=is_primary, has_comment=bool(comment), is_trial=is_trial)


async def _show_key_card_edit(
    callback: CallbackQuery, db: Database, settings: Settings, key_id: int
) -> bool:
    """Edit the current message with the key card. Returns False if key not found."""
    result = await _build_key_card(callback.from_user.id, key_id, db, settings, bot=callback.bot)
    if not result:
        return False
    text, keyboard = result
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(text, reply_markup=keyboard)
    else:
        await callback.message.edit_text(text, reply_markup=keyboard)
    return True


@router.callback_query(F.data.startswith("key_open:"))
async def key_open(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key_id = int(callback.data.split(":")[1])
    ok = await _show_key_card_edit(callback, db, settings, key_id)
    if not ok:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("key_set_primary:"))
async def key_set_primary(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key_id = int(callback.data.split(":")[1])
    tg_id = callback.from_user.id
    keys_repo = KeysRepository(db)
    users_repo = UsersRepository(db)

    key_data = await keys_repo.get_by_id_for_user(key_id, tg_id)
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    await keys_repo.set_primary(tg_id, key_id)
    key_vless = str(key_data.get("key") or "")
    if key_vless:
        await users_repo.update_key(tg_id, key_vless)

    await _show_key_card_edit(callback, db, settings, key_id)
    await callback.answer(f"⭐ Ключ #{key_id} теперь основной", show_alert=True)


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("key_qr:"))
async def key_qr(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key_id = int(callback.data.split(":")[1])
    tg_id = callback.from_user.id
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    await users_repo.get_or_create(tg_id)
    key_data = await keys_repo.get_by_id_for_user(key_id, tg_id)
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    key_sub_token = str(key_data.get("sub_token") or "")
    if not key_sub_token:
        try:
            key_sub_token = await keys_repo.ensure_sub_token(key_id, tg_id)
        except Exception:
            logger.warning("Failed to generate sub_token key_id=%s", key_id)
    sub_url = f"{settings.public_base_url}/sub/{key_sub_token}" if key_sub_token and settings.public_base_url else ""
    if not sub_url:
        await callback.answer("Subscription URL не найден", show_alert=True)
        return
    qr_bytes = qr_png_from_text(sub_url)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer_photo(
        BufferedInputFile(qr_bytes, filename=f"subscription-{key_id}.png"),
        caption=f"QR-код для подключения\n<code>{escape(sub_url)}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад к ключу", callback_data=f"key_open:{key_id}")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key_sub:"))
async def key_subscription(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key_id = int(callback.data.split(":")[1])
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)

    await users_repo.get_or_create(callback.from_user.id)
    key_data = await keys_repo.get_by_id_for_user(key_id, callback.from_user.id)
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    if not settings.public_base_url:
        await callback.answer("Сервис подписки не настроен", show_alert=True)
        return

    supabase_user = await users_repo.get_by_tg_id(callback.from_user.id)
    if supabase_user and not users_repo.is_user_active(supabase_user):
        await users_repo.update_status(callback.from_user.id, False)
        await callback.answer("❌ Подписка истекла", show_alert=True)
        return
    key_sub_token = str(key_data.get("sub_token") or "")
    if not key_sub_token:
        try:
            key_sub_token = await keys_repo.ensure_sub_token(key_id, callback.from_user.id)
        except Exception:
            await callback.answer("Не удалось подготовить subscription-ссылку", show_alert=True)
            return
    sub_url = f"{settings.public_base_url}/sub/{key_sub_token}"
    await callback.message.answer(
        "🔗 Ваша subscription-ссылка:\n"
        f"<code>{sub_url}</code>",
        disable_web_page_preview=True,
        reply_markup=get_main_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key_comment:"))
async def key_comment_open(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    key_id = int(callback.data.split(":")[1])
    keys_repo = KeysRepository(db)
    key_data = await keys_repo.get_by_id_for_user(key_id, callback.from_user.id)
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    all_keys = [k for k in await keys_repo.list_by_user(callback.from_user.id) if not k.get("disabled_at")]
    display_num = next((i for i, k in enumerate(all_keys, start=1) if k["id"] == key_id), key_id)

    current = str(key_data.get("comment") or "").strip()
    current_text = f"Текущий: <i>{escape(current)}</i>\n\n" if current else ""

    await state.set_state(KeyCommentState.waiting_for_comment)
    await state.update_data(key_id=key_id)

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"key_comment_cancel:{key_id}")]]
    )
    await callback.message.answer(
        f"📝 Комментарий к ключу #{display_num}\n\n{current_text}Введите новый комментарий (до 500 символов):",
        reply_markup=cancel_kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key_comment_delete:"))
async def key_comment_delete(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key_id = int(callback.data.split(":")[1])
    tg_id = callback.from_user.id
    keys_repo = KeysRepository(db)

    key_data = await keys_repo.get_by_id_for_user(key_id, tg_id)
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    await keys_repo.update_comment(key_id, tg_id, "")
    await _show_key_card_edit(callback, db, settings, key_id)
    await callback.answer("🗑 Комментарий удалён", show_alert=True)


@router.callback_query(F.data.startswith("key_comment_cancel:"))
async def key_comment_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    key_id = int(callback.data.split(":")[1])
    await callback.message.delete()
    await callback.answer("Отменено")
    await callback.message.answer(
        f"Редактирование комментария отменено. Откройте ключ #{key_id} снова.",
        reply_markup=get_main_menu_keyboard(),
    )


@router.message(KeyCommentState.waiting_for_comment)
async def key_comment_save(message: Message, db: Database, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    key_id = int(data.get("key_id") or 0)
    if not key_id:
        await state.clear()
        return

    comment = (message.text or "").strip()[:500]
    keys_repo = KeysRepository(db)
    tg_id = message.from_user.id
    await keys_repo.update_comment(key_id, tg_id, comment)
    await state.clear()
    result = await _build_key_card(tg_id, key_id, db, settings, bot=message.bot)
    if not result:
        await message.answer("✅ Комментарий сохранён.", reply_markup=get_main_menu_keyboard())
        return
    text, keyboard = result
    await message.answer(text, reply_markup=keyboard)
