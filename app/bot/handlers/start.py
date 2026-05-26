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
from app.utils.admin_notify import notify_admins
from app.utils.datetime import utc_now
from app.utils.tg import is_trial_eligible, send_main_menu

router = Router()


def _extract_ref_info(start_arg: str | None, admin_ids: list[int] | None = None) -> tuple[int | None, str | None]:
    if not start_arg:
        return None, None
    if start_arg.startswith("refl_"):
        rest = start_arg.removeprefix("refl_")
        # Старый формат с tg_id: refl_123456789_label (обратная совместимость)
        parts = rest.split("_", 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1]:
            return int(parts[0]), parts[1][:50]
        # Новый чистый формат: refl_label
        label = rest[:50]
        if label:
            ref_tg_id = admin_ids[0] if admin_ids else None
            return ref_tg_id, label
        return None, None
    if start_arg.startswith("ref_"):
        raw = start_arg.removeprefix("ref_")
        return (int(raw) if raw.isdigit() else None), None
    return None, None


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    users_repo = UsersRepository(db)
    ref_tg_id, ref_label = _extract_ref_info(
        command.args if command else None,
        admin_ids=list(settings.admin_ids) if settings.admin_ids else None,
    )

    existing = await users_repo.get_by_tg_id(message.from_user.id)
    is_new = existing is None

    await users_repo.get_or_create(message.from_user.id, ref_tg_id=ref_tg_id, ref_label=ref_label)
    await users_repo.update_user_info(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    if is_new and settings.admin_ids:
        u = message.from_user
        name = u.full_name or u.first_name or "—"
        uname = f"@{u.username}" if u.username else "без username"
        ref_line = f"\n🔗 Реферал от: <code>{ref_tg_id}</code>" if ref_tg_id else ""
        label_line = f"\n🏷 Метка: <b>{ref_label}</b>" if ref_label else ""
        await notify_admins(
            message.bot,
            settings.admin_ids,
            f"🆕 <b>Новый пользователь!</b>\n\n"
            f"👤 {name} / {uname}\n"
            f"🆔 ID: <code>{u.id}</code>{ref_line}{label_line}",
        )

    show_trial = await is_trial_eligible(message.from_user.id, db)

    await send_main_menu(message, inline_main_menu_keyboard(settings.support_url, show_trial=show_trial))


@router.message(Command("menu"))
@router.message(F.text == "🏠 Главное меню")
async def menu_button(message: Message, db: Database, settings: Settings) -> None:
    show_trial = await is_trial_eligible(message.from_user.id, db)
    await send_main_menu(message, inline_main_menu_keyboard(settings.support_url, show_trial=show_trial))


@router.callback_query(F.data == "back_menu")
async def back_menu(callback: CallbackQuery, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    show_trial = await is_trial_eligible(callback.from_user.id, db)
    await send_main_menu(callback.message, inline_main_menu_keyboard(settings.support_url, show_trial=show_trial))
    await callback.answer()
