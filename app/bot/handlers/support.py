from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.keyboards.inline import legal_keyboard
from app.config import Settings
from app.utils.tg import photo_to_text

router = Router()


@router.callback_query(F.data == "legal_docs")
async def legal_docs(callback: CallbackQuery, settings: Settings) -> None:
    await photo_to_text(
        callback.message,
        "📄 Правовая информация\n\n"
        "Ниже представлены документы, регулирующие использование сервиса ZyberVPN:",
        reply_markup=legal_keyboard(settings.privacy_policy_url, settings.terms_url),
    )
    await callback.answer()
