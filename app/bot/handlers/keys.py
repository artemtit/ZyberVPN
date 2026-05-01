from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.inline import key_card_keyboard, keys_list_keyboard
from app.bot.states.keys import KeyCommentState
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager
from app.services.vpn import qr_png_from_text
from app.utils.datetime import parse_iso_utc, utc_diff, utc_now

router = Router()


def _remaining_parts(expires_at: datetime) -> tuple[int, int, int]:
    delta = utc_diff(expires_at, utc_now())
    total_seconds = int(max(delta.total_seconds(), 0))
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    return days, hours, minutes


def _key_protocol(key_str: str) -> str:
    if "security=reality" in key_str:
        return "REALITY"
    if "type=ws" in key_str or "path=" in key_str:
        return "WS+TLS"
    return "VPN"


def _key_label(key_str: str, is_primary: bool, months_left: int, is_active: bool) -> str:
    icon = "⭐" if is_primary else "🔑"
    protocol = _key_protocol(key_str)
    status = f"{months_left} мес." if is_active else "истёк"
    return f"{icon} {protocol} — {status}"


@router.callback_query(F.data == "menu_keys")
async def keys_list(callback: CallbackQuery, db: Database) -> None:
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)
    await users_repo.get_or_create(callback.from_user.id)
    keys = await keys_repo.list_by_user(callback.from_user.id)
    active_sub = await subs_repo.get_active(callback.from_user.id)

    sub_months_left = 0
    if active_sub:
        sub_expires_at = parse_iso_utc(active_sub["expires_at"])
        sub_days, _, _ = _remaining_parts(sub_expires_at)
        sub_months_left = max(sub_days // 30, 0)

    key_rows: list[tuple[str, str]] = []
    for key_data in keys:
        key_str = str(key_data.get("key") or "")
        is_primary = bool(key_data.get("is_primary", False))

        # Per-key expiry takes priority; fall back to user subscription expiry.
        key_expires_raw = key_data.get("expires_at")
        if key_expires_raw:
            key_expires_at = parse_iso_utc(key_expires_raw)
            key_days, _, _ = _remaining_parts(key_expires_at)
            key_months = max(key_days // 30, 0)
            key_is_active = key_days > 0
        elif active_sub:
            key_months = sub_months_left
            key_is_active = True
        else:
            key_months = 0
            key_is_active = False

        label = _key_label(key_str, is_primary, key_months, key_is_active)
        key_rows.append((label, str(key_data["id"])))

    await callback.message.edit_text(
        "🔑 Ваши ключи доступа\n\nНиже представлен список ваших активных и истекших ключей:",
        reply_markup=keys_list_keyboard(key_rows),
    )
    await callback.answer()


async def _show_key_card_edit(
    callback: CallbackQuery, db: Database, settings: Settings, key_id: int
) -> bool:
    """Render and edit the key card message. Returns False if the key was not found."""
    tg_id = callback.from_user.id
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)

    await users_repo.get_or_create(tg_id)
    key_data = await keys_repo.get_by_id_for_user(key_id, tg_id)
    if not key_data:
        return False

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

    supabase_user = await users_repo.get_by_tg_id(tg_id)
    sub_token = str((supabase_user or {}).get("sub_token") or "")
    traffic_limit_gb = int((supabase_user or {}).get("traffic_limit_gb") or 60)
    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if sub_token and settings.public_base_url else ""

    traffic_used_gb = 0.0
    online_devices = 0
    limit_exceeded = False
    try:
        manager = build_vpn_manager(db, settings, bot=callback.bot)
        bytes_used, online_devices = await manager.get_client_stats(tg_id)
        traffic_used_gb = bytes_used / (1024 ** 3)
        if bytes_used > 0 and bytes_used >= traffic_limit_gb * 1024 ** 3:
            limit_exceeded = True
            async def _enforce() -> None:
                try:
                    await build_vpn_manager(db, settings, bot=callback.bot).enforce_traffic_limit(tg_id)
                except Exception:
                    pass
            asyncio.create_task(_enforce())
    except Exception:
        pass

    if limit_exceeded:
        status_text = "Заблокирован (лимит трафика)"
        status_emoji = "🔴"

    is_primary = bool(key_data.get("is_primary", False))
    comment = str(key_data.get("comment") or "").strip()
    sub_line = f"\n🔗 Subscription URL:\n<code>{escape(sub_url)}</code>\n" if sub_url else ""
    comment_line = f"\n📝 Комментарий: {escape(comment)}" if comment else ""

    text = (
        f"🔑 Ключ #{key_data['id']}\n\n"
        f"{status_emoji} Статус: {status_text}\n"
        f"⏳ Истекает: {expires_at.strftime('%d.%m.%Y %H:%M UTC')} ({days}д. {hours}ч.)\n"
        f"{sub_line}\n"
        f"📡 Трафик: {traffic_used_gb:.1f} / {traffic_limit_gb} ГБ\n"
        f"📱 Устройств онлайн: {online_devices} / 3"
        f"{comment_line}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=key_card_keyboard(key_id, is_primary=is_primary, has_comment=bool(comment)),
    )
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
    supabase_user = await users_repo.get_by_tg_id(tg_id)
    sub_token = str((supabase_user or {}).get("sub_token") or "")
    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if sub_token and settings.public_base_url else ""
    if not sub_url:
        await callback.answer("Subscription URL не найден", show_alert=True)
        return
    qr_bytes = qr_png_from_text(sub_url)
    await callback.message.answer_photo(
        BufferedInputFile(qr_bytes, filename=f"subscription-{key_id}.png"),
        caption=f"QR-код для подключения\n<code>{escape(sub_url)}</code>",
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
    try:
        sub_token = await users_repo.ensure_sub_token_for_tg(callback.from_user.id)
    except Exception:
        await callback.answer("Не удалось подготовить subscription-ссылку", show_alert=True)
        return
    sub_url = f"{settings.public_base_url}/sub/{sub_token}"
    await callback.message.answer(
        "🔗 Ваша subscription-ссылка:\n"
        f"<code>{sub_url}</code>",
        disable_web_page_preview=True,
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

    current = str(key_data.get("comment") or "").strip()
    current_text = f"Текущий: <i>{escape(current)}</i>\n\n" if current else ""

    await state.set_state(KeyCommentState.waiting_for_comment)
    await state.update_data(key_id=key_id)

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"key_comment_cancel:{key_id}")]]
    )
    await callback.message.answer(
        f"📝 Комментарий к ключу #{key_id}\n\n{current_text}Введите новый комментарий (до 500 символов):",
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
    )


@router.message(KeyCommentState.waiting_for_comment)
async def key_comment_save(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    key_id = int(data.get("key_id") or 0)
    if not key_id:
        await state.clear()
        return

    comment = (message.text or "").strip()[:500]
    keys_repo = KeysRepository(db)
    await keys_repo.update_comment(key_id, message.from_user.id, comment)
    await state.clear()

    action = "удалён" if not comment else "сохранён"
    await message.answer(
        f"✅ Комментарий {action}.\n\nОткройте ключ #{key_id} чтобы увидеть изменения.",
    )
