from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards.inline import key_detail_keyboard, keys_menu_keyboard
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.vpn import qr_png_from_text
from app.utils.datetime import utcnow

router = Router()


def _subscription_status(subscription: dict | None) -> tuple[str, str]:
    if not subscription:
        return "истёк", "—"
    expires_at = datetime.fromisoformat(subscription["expires_at"])
    if expires_at > utcnow():
        return "активен", expires_at.strftime("%d.%m.%Y %H:%M")
    return "истёк", expires_at.strftime("%d.%m.%Y %H:%M")


async def _keys_overview(tg_id: int, db: Database) -> tuple[dict, list[dict], dict | None]:
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)
    user = await users_repo.get_or_create(tg_id)
    keys = await keys_repo.list_by_user(user["id"])
    sub = await subs_repo.get_latest(user["id"])
    return user, keys, sub


@router.message(F.text == "🔑 Мои ключи")
async def my_keys(message: Message, db: Database) -> None:
    _, keys, sub = await _keys_overview(message.from_user.id, db)
    status, expires = _subscription_status(sub)
    text = (
        f"Ваши ключи: {len(keys)}\n"
        f"Статус подписки: {status}\n"
        f"Срок: {expires}"
    )
    await message.answer(text, reply_markup=keys_menu_keyboard(keys))


@router.callback_query(F.data == "keys_back")
async def keys_back(callback: CallbackQuery, db: Database) -> None:
    _, keys, sub = await _keys_overview(callback.from_user.id, db)
    status, expires = _subscription_status(sub)
    text = (
        f"Ваши ключи: {len(keys)}\n"
        f"Статус подписки: {status}\n"
        f"Срок: {expires}"
    )
    await callback.message.edit_text(text, reply_markup=keys_menu_keyboard(keys))
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
    sub = await subs_repo.get_latest(user["id"])
    status, expires = _subscription_status(sub)
    await callback.message.edit_text(
        f"Ключ #{key_data['id']}\nСтатус: {status}\nСрок: {expires}",
        reply_markup=key_detail_keyboard(key_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key_connect:"))
async def key_connect(callback: CallbackQuery, db: Database) -> None:
    key_id = int(callback.data.split(":")[1])
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    user = await users_repo.get_or_create(callback.from_user.id)
    key_data = await keys_repo.get_by_id_for_user(key_id, user["id"])
    if not key_data:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await callback.message.answer(f"Ссылка для подключения:\n<code>{escape(key_data['key'])}</code>")
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
        caption=f"QR для ключа #{key_id}",
    )
    await callback.answer()
