from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import main_menu_keyboard as inline_main_menu_keyboard
from app.bot.keyboards.main import get_main_menu_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.users import UsersRepository
from app.utils.tg import send_main_menu

router = Router()


def _extract_ref_tg_id(start_arg: str | None) -> int | None:
    if not start_arg or not start_arg.startswith("ref_"):
        return None
    raw = start_arg.removeprefix("ref_")
    return int(raw) if raw.isdigit() else None


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    users_repo = UsersRepository(db)
    ref_tg_id = _extract_ref_tg_id(command.args if command else None)
    await users_repo.get_or_create(message.from_user.id, ref_tg_id=ref_tg_id)
    if message.from_user.username:
        await users_repo.update_username(message.from_user.id, message.from_user.username)
    await send_main_menu(message, inline_main_menu_keyboard(settings.support_url))


@router.message(Command("menu"))
@router.message(F.text == "🏠 Главное меню")
async def menu_button(message: Message, settings: Settings) -> None:
    await send_main_menu(message, inline_main_menu_keyboard(settings.support_url))


@router.callback_query(F.data == "back_menu")
async def back_menu(callback: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await send_main_menu(callback.message, inline_main_menu_keyboard(settings.support_url))
    await callback.answer()
