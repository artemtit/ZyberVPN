from __future__ import annotations

from pathlib import Path

from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message

_MENU_PHOTO_FILE_ID: str | None = None
_MENU_PHOTO_PATH = Path(__file__).parent.parent / "assets" / "menu_photo.png"
_MENU_CAPTION = "🏠 Главное меню\nВыберите действие:"


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
