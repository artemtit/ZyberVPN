from aiogram import F, Router
from aiogram.types import Message

from app.bot.keyboards.inline import support_keyboard
from app.config import Settings

router = Router()


@router.message(F.text == "🆘 Поддержка")
async def support(message: Message, settings: Settings) -> None:
    await message.answer("Поддержка ZyberVPN", reply_markup=support_keyboard(settings.support_url))
