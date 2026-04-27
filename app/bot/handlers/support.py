from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.bot.keyboards.inline import legal_keyboard
from app.config import Settings

router = Router()


@router.callback_query(F.data == "legal_docs")
async def legal_docs(callback: CallbackQuery, settings: Settings) -> None:
    try:
        await callback.message.edit_text(
            "📄 Правовая информация\n\n"
            "Ниже представлены документы, регулирующие использование сервиса ZyberVPN:",
            reply_markup=legal_keyboard(settings.privacy_policy_url, settings.terms_url),
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise
    await callback.answer()
