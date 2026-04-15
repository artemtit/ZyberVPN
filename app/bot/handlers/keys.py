from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery

from app.bot.keyboards.inline import key_card_keyboard, keys_list_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.vpn import qr_png_from_text
from app.utils.datetime import utcnow

router = Router()


def _remaining_parts(expires_at: datetime) -> tuple[int, int, int]:
    delta = expires_at - utcnow()
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
    user = await users_repo.get_or_create(callback.from_user.id)
    keys = await keys_repo.list_by_user(user["id"])
    active_sub = await subs_repo.get_active(user["id"])

    months_left = 0
    if active_sub:
        expires_at = datetime.fromisoformat(active_sub["expires_at"])
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
async def key_open(callback: CallbackQuery, db: Database) -> None:
    key_id = int(callback.data.split(":")[1])
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)
    user = await users_repo.get_or_create(callback.from_user.id)
    key_data = await keys_repo.get_by_id_for_user(key_id, user["id"])
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    created_at = datetime.fromisoformat(key_data["created_at"])
    active_sub = await subs_repo.get_active(user["id"])
    if active_sub:
        expires_at = datetime.fromisoformat(active_sub["expires_at"])
        status_text = "Активен"
        status_emoji = "🟢"
    else:
        expires_at = created_at
        status_text = "Истек"
        status_emoji = "🔴"

    days, hours, minutes = _remaining_parts(expires_at)
    key_uid = f"trial_ligr{key_data['id']}@bot"
    key_link = key_data["key"]

    text = (
        f"🔑 Информация о ключе #{key_data['id']}\n\n"
        "📅 Сроки действия:\n"
        f"{status_emoji} Статус: {status_text}\n"
        f"➕ Куплен: {created_at.strftime('%d.%m.%Y')}\n"
        f"⏳ Истекает: {expires_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"⌛ Осталось: {days}д. {hours}ч. {minutes}мин\n"
        f"💊 ID ключа: {key_uid}\n\n"
        "📉 Использование:\n"
        "📡 Лимит трафика: 466.20 GiB / ∞\n"
        "📱 Лимит устройств: 20 / ∞\n\n"
        "🔗 Ваш ключ:\n"
        f"{key_link}"
    )
    await callback.message.edit_text(text, reply_markup=key_card_keyboard(key_id))
    await callback.answer()


@router.callback_query(F.data.startswith("key_qr:"))
async def key_qr(callback: CallbackQuery, db: Database) -> None:
    key_id = int(callback.data.split(":")[1])
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    user = await users_repo.get_or_create(callback.from_user.id)
    key_data = await keys_repo.get_by_id_for_user(key_id, user["id"])
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    qr_bytes = qr_png_from_text(key_data["key"])
    await callback.message.answer_photo(
        BufferedInputFile(qr_bytes, filename=f"vpn-key-{key_id}.png"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key_sub:"))
async def key_subscription(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key_id = int(callback.data.split(":")[1])
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)

    user = await users_repo.get_or_create(callback.from_user.id)
    key_data = await keys_repo.get_by_id_for_user(key_id, user["id"])
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
    sub_token = (supabase_user or {}).get("sub_token")
    if not sub_token:
        sub_token = await users_repo.ensure_sub_token(user["id"])
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
