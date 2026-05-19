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


def _extract_ref_info(start_arg: str | None) -> tuple[int | None, str | None]:
    """Parse start arg. Returns (ref_tg_id, ref_label).

    Formats:
      ref_123456789           — legacy, no label
      refl_123456789_name     — labeled referral link
    """
    if not start_arg:
        return None, None
    # Labeled format: refl_<tg_id>_<label>  (must check first — it also starts with "ref")
    if start_arg.startswith("refl_"):
        rest = start_arg.removeprefix("refl_")
        parts = rest.split("_", 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1]:
            return int(parts[0]), parts[1][:50]
        return None, None
    # Legacy format: ref_<tg_id>
    if start_arg.startswith("ref_"):
        raw = start_arg.removeprefix("ref_")
        return (int(raw) if raw.isdigit() else None), None
    return None, None


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    users_repo = UsersRepository(db)
    ref_tg_id, ref_label = _extract_ref_info(command.args if command else None)

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

    if is_new:
        await message.answer(
            "👋 <b>Добро пожаловать в ZyberVPN!</b>\n\n"
            "Быстрый и надёжный VPN — без логов и ограничений.\n\n"
            "🌍 Серверы в Нидерландах и Польше\n"
            "📱 Android, iOS, Windows, macOS, Linux\n"
            "🔒 Протокол VLESS+Reality — стабильно и безопасно\n\n"
            + ("Попробуйте <b>1 день бесплатно</b> или выберите тариф ниже 👇"
               if show_trial else
               "Выберите тариф в меню ниже 👇")
        )

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
