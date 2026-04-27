from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery

from app.bot.keyboards.inline import key_card_keyboard, keys_list_keyboard
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


@router.callback_query(F.data == "menu_keys")
async def keys_list(callback: CallbackQuery, db: Database) -> None:
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)
    await users_repo.get_or_create(callback.from_user.id)
    keys = await keys_repo.list_by_user(callback.from_user.id)
    active_sub = await subs_repo.get_active(callback.from_user.id)

    months_left = 0
    if active_sub:
        expires_at = parse_iso_utc(active_sub["expires_at"])
        days, _, _ = _remaining_parts(expires_at)
        months_left = max(days // 30, 0)

    key_rows: list[tuple[str, str]] = []
    for index, key_data in enumerate(keys, start=1):
        status = "✅" if active_sub else "⚪"
        key_rows.append((f"{status} #{index} (Основной) ({months_left} мес)", str(key_data["id"])))

    await callback.message.edit_text(
        "🔑 Ваши ключи доступа\n\nНиже представлен список ваших активных и истекших ключей:",
        reply_markup=keys_list_keyboard(key_rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key_open:"))
async def key_open(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key_id = int(callback.data.split(":")[1])
    tg_id = callback.from_user.id
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)

    await users_repo.get_or_create(tg_id)
    key_data = await keys_repo.get_by_id_for_user(key_id, tg_id)
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    created_at = parse_iso_utc(key_data["created_at"])
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

    # Best-effort: query 3x-ui for live traffic and device stats
    traffic_used_gb = 0.0
    online_devices = 0
    try:
        manager = build_vpn_manager(db, settings)
        bytes_used, online_devices = await manager.get_client_stats(tg_id)
        traffic_used_gb = bytes_used / (1024 ** 3)
    except Exception:
        pass

    sub_line = f"\n🔗 Subscription URL:\n<code>{escape(sub_url)}</code>\n" if sub_url else ""

    text = (
        f"🔑 Ключ #{key_data['id']}\n\n"
        f"{status_emoji} Статус: {status_text}\n"
        f"⏳ Истекает: {expires_at.strftime('%d.%m.%Y')} ({days}д. {hours}ч.)\n"
        f"{sub_line}\n"
        f"📡 Трафик: {traffic_used_gb:.1f} / {traffic_limit_gb} ГБ\n"
        f"📱 Устройств онлайн: {online_devices} / 3"
    )
    await callback.message.edit_text(text, reply_markup=key_card_keyboard(key_id))
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
async def key_comment(callback: CallbackQuery) -> None:
    await callback.message.answer("Комментарии к ключу отсутствуют.")
    await callback.answer()
