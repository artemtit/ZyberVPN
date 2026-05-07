from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.main import get_main_menu_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.servers import ServersRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager
from app.utils.datetime import parse_iso_utc, to_moscow, utc_now

router = Router()
logger = logging.getLogger(__name__)


class AdminState(StatesGroup):
    waiting_broadcast_confirm = State()


def _is_admin(tg_id: int, settings: Settings) -> bool:
    return tg_id in settings.admin_ids


def _parse_tg_id(text: str, command: str) -> int | None:
    """Extract tg_id from '/command <tg_id>' text. Returns None on parse error."""
    raw = text.removeprefix(f"/{command}").strip().split()[0] if text else ""
    try:
        return int(raw)
    except (ValueError, IndexError):
        return None


def _format_expiry(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        return to_moscow(parse_iso_utc(raw)).strftime("%d.%m.%Y %H:%M МСК")
    except Exception:
        return str(raw)


# ──────────────────────────────────────────
# /admin
# ──────────────────────────────────────────
@router.message(Command("admin"))
async def admin_help(message: Message, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    await message.answer(
        "🔧 <b>Admin commands:</b>\n\n"
        "/stats — статистика проекта\n"
        "/user &lt;tg_id&gt; — профиль пользователя\n"
        "/ban &lt;tg_id&gt; — заблокировать пользователя\n"
        "/unban &lt;tg_id&gt; — разблокировать пользователя\n"
        "/servers — список серверов\n"
        "/broadcast &lt;текст&gt; — рассылка с подтверждением",
    )


# ──────────────────────────────────────────
# /stats
# ──────────────────────────────────────────
@router.message(Command("stats"))
async def admin_stats(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    users_repo = UsersRepository(db)
    payments_repo = PaymentsRepository(db)
    keys_repo = KeysRepository(db)

    total_users = await users_repo.count_all()
    active_users = await users_repo.count_active()
    total_revenue = await payments_repo.total_revenue()

    # Count total keys across all users (rough: list active tg_ids and sum)
    active_ids = await users_repo.list_active_tg_ids()
    total_keys = 0
    for tid in active_ids[:100]:  # cap to avoid flooding Supabase
        ks = await keys_repo.list_by_user(tid)
        total_keys += len(ks)
    keys_label = f"{total_keys}+" if len(active_ids) > 100 else str(total_keys)

    await message.answer(
        "📊 <b>Статистика проекта</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"✅ Активных подписчиков: <b>{active_users}</b>\n"
        f"🔑 Ключей (активные юзеры): <b>{keys_label}</b>\n"
        f"💰 Общий доход: <b>{total_revenue} RUB</b>",
    )


# ──────────────────────────────────────────
# /user <tg_id>
# ──────────────────────────────────────────
@router.message(Command("user"))
async def admin_user(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    target_id = _parse_tg_id(message.text or "", "user")
    if not target_id:
        await message.answer("Использование: /user &lt;tg_id&gt;")
        return

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    subs_repo = SubscriptionsRepository(db)

    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    is_active = bool(user.get("is_active"))
    expires_raw = user.get("expires_at")
    plan = user.get("plan") or "—"
    traffic_gb = user.get("traffic_limit_gb") or 0
    balance = user.get("balance") or 0

    active_sub = await subs_repo.get_active(target_id)
    sub_expires = active_sub["expires_at"] if active_sub else expires_raw

    keys = await keys_repo.list_by_user(target_id)
    keys_text = ""
    for i, k in enumerate(keys, 1):
        k_exp = _format_expiry(k.get("expires_at"))
        k_limit = k.get("traffic_limit_gb") or "—"
        primary = "⭐" if k.get("is_primary") else "🔑"
        keys_text += f"  {primary} Ключ #{k['id']} | до {k_exp} | {k_limit} ГБ\n"
    if not keys_text:
        keys_text = "  нет ключей\n"

    status_emoji = "✅" if is_active else "❌"
    await message.answer(
        f"👤 <b>Пользователь {target_id}</b>\n\n"
        f"🛡 Статус: {status_emoji} {'Активен' if is_active else 'Неактивен'}\n"
        f"📅 Expires_at: {_format_expiry(sub_expires)}\n"
        f"📦 План: {plan}\n"
        f"📡 Traffic limit: {traffic_gb} ГБ\n"
        f"💳 Баланс: {balance} RUB\n\n"
        f"🔑 <b>Ключи:</b>\n{keys_text}",
    )


# ──────────────────────────────────────────
# /broadcast <text>  — с подтверждением
# ──────────────────────────────────────────
@router.message(Command("broadcast"))
async def admin_broadcast(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    text = (message.text or "").removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Использование: /broadcast &lt;текст&gt;")
        return

    await state.set_state(AdminState.waiting_broadcast_confirm)
    await state.update_data(broadcast_text=text)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="broadcast_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await message.answer(
        "📢 <b>Превью рассылки:</b>\n\n"
        f"{text}\n\n"
        "──────────────────\n"
        "Подтвердить отправку всем активным пользователям?",
        reply_markup=kb,
    )


@router.callback_query(F.data == "broadcast_confirm", AdminState.waiting_broadcast_confirm)
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа", show_alert=True)
        return

    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)

    if not text:
        await callback.answer("Текст не найден", show_alert=True)
        return

    users_repo = UsersRepository(db)
    active_tg_ids = await users_repo.list_active_tg_ids()
    sent = failed = 0
    for tg_id in active_tg_ids:
        try:
            await callback.bot.send_message(tg_id, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await callback.message.answer(f"✅ Рассылка завершена: {sent} отправлено, {failed} ошибок.")
    await callback.answer()


@router.callback_query(F.data == "broadcast_cancel", AdminState.waiting_broadcast_confirm)
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ Рассылка отменена.")
    await callback.answer()


# ──────────────────────────────────────────
# /ban <tg_id>
# ──────────────────────────────────────────
@router.message(Command("ban"))
async def admin_ban(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    target_id = _parse_tg_id(message.text or "", "ban")
    if not target_id:
        await message.answer("Использование: /ban &lt;tg_id&gt;")
        return

    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    # Disable all VPN clients in XUI
    try:
        manager = build_vpn_manager(db, settings)
        await manager.disable_user_access(target_id)
    except Exception:
        logger.exception("admin_ban: disable_user_access failed tg_id=%s", target_id)
        await message.answer(f"⚠️ XUI отключение не удалось для {target_id}, но БД будет обновлена.")

    await users_repo.update_status(target_id, False)
    logger.warning("ADMIN BAN | admin=%s target=%s", message.from_user.id, target_id)
    await message.answer(f"🚫 Пользователь {target_id} заблокирован. VPN-доступ отключён.")


# ──────────────────────────────────────────
# /unban <tg_id>
# ──────────────────────────────────────────
@router.message(Command("unban"))
async def admin_unban(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    target_id = _parse_tg_id(message.text or "", "unban")
    if not target_id:
        await message.answer("Использование: /unban &lt;tg_id&gt;")
        return

    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    # Re-enable account in DB — new key is NOT created automatically.
    await users_repo.update_status(target_id, True)
    logger.warning("ADMIN UNBAN | admin=%s target=%s", message.from_user.id, target_id)
    await message.answer(
        f"✅ Пользователь {target_id} разблокирован.\n"
        "Доступ к существующим ключам восстановлен (новый ключ не создаётся).\n"
        "Пользователю нужно открыть бот — ключи загрузятся автоматически."
    )


# ──────────────────────────────────────────
# /servers
# ──────────────────────────────────────────
@router.message(Command("servers"))
async def admin_servers(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return

    servers_repo = ServersRepository(db)
    servers = await servers_repo.list_all()

    if not servers:
        await message.answer("ℹ️ Серверов не найдено.")
        return

    from app.repositories.user_vpn import UserVpnRepository
    user_vpn_repo = UserVpnRepository(db)
    counts = await user_vpn_repo.count_users_by_server()

    lines = ["🖥 <b>Список серверов:</b>\n"]
    for srv in servers:
        status_emoji = "🟢" if srv.is_active else "🔴"
        errors = srv.health_errors or 0
        load = counts.get(srv.id, 0)
        last_check = "—"
        if srv.last_health_check:
            try:
                last_check = to_moscow(srv.last_health_check).strftime("%d.%m %H:%M МСК")
            except Exception:
                pass
        lines.append(
            f"{status_emoji} <b>{srv.name}</b> ({srv.country})\n"
            f"   🌐 {srv.host}:{srv.public_port}\n"
            f"   👥 Нагрузка: {load} пользователей\n"
            f"   ⚠️ Ошибок: {errors} | Проверка: {last_check}\n"
        )

    await message.answer("\n".join(lines))
