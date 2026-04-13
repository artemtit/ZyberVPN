from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from app.bot.keyboards.reply import main_menu_keyboard
from app.db.database import Database
from app.repositories.users import UsersRepository

router = Router()


def _extract_ref_tg_id(start_arg: str | None) -> int | None:
    if not start_arg or not start_arg.startswith("ref_"):
        return None
    raw = start_arg.removeprefix("ref_")
    return int(raw) if raw.isdigit() else None


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database) -> None:
    users_repo = UsersRepository(db)
    ref_tg_id = _extract_ref_tg_id(command.args if command else None)
    await users_repo.get_or_create(message.from_user.id, ref_tg_id=ref_tg_id)
    await message.answer(
        "ZyberVPN\nВыберите действие:",
        reply_markup=main_menu_keyboard(),
    )


@router.message(F.text == "⬅️ В меню")
async def go_menu(message: Message) -> None:
    await message.answer("Главное меню", reply_markup=main_menu_keyboard())
