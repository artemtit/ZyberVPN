from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message

if TYPE_CHECKING:
    from app.db.database import Database

_MENU_PHOTO_FILE_ID: str | None = None
_MENU_PHOTO_PATH = Path(__file__).parent.parent / "assets" / "menu_photo.png"
_MENU_CAPTION = "🛡 ZyberVPN\n\nВыберите раздел:"


async def send_main_menu(message: Message, keyboard: InlineKeyboardMarkup) -> None:
    global _MENU_PHOTO_FILE_ID
    photo = _MENU_PHOTO_FILE_ID or FSInputFile(str(_MENU_PHOTO_PATH))
    sent = await message.answer_photo(
        photo=photo,
        caption=_MENU_CAPTION,
        reply_markup=keyboard,
    )
    if not _MENU_PHOTO_FILE_ID and sent.photo:
        _MENU_PHOTO_FILE_ID = sent.photo[-1].file_id


async def is_trial_eligible(tg_id: int, db: "Database") -> bool:
    from app.repositories.users import UsersRepository
    from app.repositories.keys import KeysRepository
    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(tg_id)
    if not user or user.get("trial_used"):
        return False
    keys_repo = KeysRepository(db)
    keys = await keys_repo.list_by_user(tg_id)
    active = [k for k in keys if not k.get("disabled_at") and str(k.get("key") or "").startswith("vless://")]
    return len(active) == 0


async def photo_to_text(message: Message, text: str, **kwargs) -> None:
    """Transition from a photo message to a text message.

    Deletes the photo message and sends a new text message in its place.
    Falls back to edit_text if the message is already text.
    """
    if message.photo:
        await message.delete()
        await message.answer(text, **kwargs)
    else:
        await message.edit_text(text, **kwargs)
