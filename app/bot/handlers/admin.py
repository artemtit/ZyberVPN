from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from app.bot.keyboards.main import get_main_menu_keyboard
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.payments import PaymentsRepository
from app.repositories.servers import ServersRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.user_vpn import UserVpnRepository
from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager, ensure_user_access
from app.services.vpn.manager import VPNManager
from app.utils.datetime import add_months, parse_iso_utc, to_moscow, utc_now
from datetime import timedelta


class IsAdmin(BaseFilter):
    """Router-level guard: silently drops any update from non-admins."""
    async def __call__(self, event: TelegramObject, settings: Settings, **_: Any) -> bool:  # type: ignore[override]
        user = getattr(event, "from_user", None)
        return user is not None and user.id in settings.admin_ids


router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())
logger = logging.getLogger(__name__)

# In-memory set of banned tg_ids. Survives until bot restart.
# On first /ban the id is added; on /unban it is removed.
_BANNED_IDS: set[int] = set()


class AdminState(StatesGroup):
    waiting_broadcast_confirm = State()
    waiting_broadcastall_confirm = State()


def _is_admin(tg_id: int, settings: Settings) -> bool:
    return tg_id in settings.admin_ids


def _parse_tg_id(text: str, command: str) -> int | None:
    """Extract tg_id from '/command <tg_id>' text. Returns None on parse error."""
    raw = text.removeprefix(f"/{command}").strip().split()[0] if text else ""
    try:
        return int(raw)
    except (ValueError, IndexError):
        return None


async def _resolve_user(text: str, command: str, db: Database) -> tuple[int | None, str]:
    """Parse tg_id or @username from command arg. Returns (tg_id, error_msg)."""
    raw = text.removeprefix(f"/{command}").strip().split()[0] if text else ""
    if not raw:
        return None, f"Использование: /{command} &lt;tg_id&gt; или @username"
    if raw.startswith("@") or not raw.lstrip("-").isdigit():
        users_repo = UsersRepository(db)
        user = await users_repo.get_by_username(raw)
        if not user:
            return None, f"❌ Пользователь {raw} не найден (поиск по username)."
        return int(user["tg_id"]), ""
    try:
        return int(raw), ""
    except ValueError:
        return None, f"❌ Неверный аргумент: {raw}"


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
        "<b>👤 Пользователи</b>\n"
        "/user &lt;tg_id|@username&gt; — профиль пользователя\n"
        "/payments &lt;tg_id|@username&gt; — история платежей\n"
        "/ban &lt;tg_id&gt; — заблокировать\n"
        "/unban &lt;tg_id&gt; — разблокировать\n\n"
        "<b>🔑 Ключи</b>\n"
        "/givekey &lt;tg_id&gt; — выдать ключ на 30 дней\n"
        "/delkey &lt;tg_id&gt; &lt;key_id&gt; — удалить ключ\n"
        "/reenable_key &lt;tg_id&gt; [key_id] — переактивировать ключ(и)\n"
        "/restore_keys &lt;tg_id&gt; — восстановить после бана\n\n"
        "<b>💰 Финансы</b>\n"
        "/addbalance &lt;tg_id&gt; &lt;сумма&gt; — добавить на баланс\n"
        "/setexpiry &lt;tg_id&gt; &lt;дней&gt; — установить срок подписки\n\n"
        "<b>📊 Общее</b>\n"
        "/stats — статистика проекта\n"
        "/newusers [N] — последние N пользователей\n"
        "/servers — список серверов\n"
        "/sync_servers — синхронизировать серверы\n"
        "/broadcast &lt;текст&gt; — рассылка активным\n"
        "/broadcastall &lt;текст&gt; — рассылка ВСЕМ\n\n"
        "<b>🔗 Реферальные метки</b>\n"
        "/reflink &lt;метка&gt; — сгенерировать уникальную ссылку для друга\n"
        "/refstats — статистика по реферальным меткам",
    )


# ──────────────────────────────────────────
# /newusers [N]
# ──────────────────────────────────────────
@router.message(Command("newusers"))
async def admin_new_users(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    parts = (message.text or "").split()
    limit = 15
    if len(parts) >= 2:
        try:
            limit = max(1, min(50, int(parts[1])))
        except ValueError:
            pass

    users_repo = UsersRepository(db)
    rows = await users_repo.list_recent(limit)
    if not rows:
        await message.answer("Пользователей не найдено.")
        return

    lines = [f"👥 <b>Последние {len(rows)} пользователей:</b>\n"]
    for i, row in enumerate(rows, 1):
        tg_id = row.get("tg_id", "?")
        username = row.get("username")
        first_name = row.get("first_name")
        is_active = bool(row.get("is_active"))
        created_raw = row.get("created_at")

        name_parts = []
        if first_name:
            name_parts.append(escape(first_name))
        if username:
            name_parts.append(f"@{username}")
        name_str = " / ".join(name_parts) if name_parts else "—"

        try:
            joined_str = to_moscow(parse_iso_utc(created_raw)).strftime("%d.%m.%Y %H:%M") if created_raw else "—"
        except Exception:
            joined_str = "—"

        status = "✅" if is_active else "⬜"
        lines.append(f"{i}. {status} <code>{tg_id}</code> {name_str}\n   📅 {joined_str} МСК")

    await message.answer("\n".join(lines))


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
    servers_repo = ServersRepository(db)

    excl = list(settings.admin_ids) if settings.admin_ids else []
    (
        total_users, active_users, new_24h, new_7d,
        unique_payers, total_revenue, stars_revenue,
        active_keys, disabled_keys,
    ) = await asyncio.gather(
        users_repo.count_all(exclude_tg_ids=excl),
        users_repo.count_active(exclude_tg_ids=excl),
        users_repo.count_new_last_24h(exclude_tg_ids=excl),
        users_repo.count_new_last_7d(exclude_tg_ids=excl),
        payments_repo.count_unique_payers(exclude_tg_ids=excl),
        payments_repo.total_revenue(exclude_tg_ids=excl),
        payments_repo.revenue_stars(exclude_tg_ids=excl),
        keys_repo.count_active(exclude_tg_ids=excl),
        keys_repo.count_disabled(exclude_tg_ids=excl),
    )

    inactive_users = total_users - active_users
    platega_revenue = total_revenue - stars_revenue
    never_paid = total_users - unique_payers
    conversion = round(unique_payers / total_users * 100) if total_users else 0
    avg_check = round(total_revenue / unique_payers) if unique_payers else 0

    # Real-time online counts from XUI (cached by healthcheck loop).
    online_counts = VPNManager.get_online_counts()

    servers = await servers_repo.list_all()
    server_lines = ""
    for srv in servers:
        status = "🟢" if srv.is_active else "🔴"
        online = online_counts.get(srv.id)
        if online is not None:
            load_str = f"{online} онлайн"
        else:
            load_str = "нет данных"
        server_lines += f"  {status} <b>{srv.name}</b> ({srv.country}): {load_str}\n"

    await message.answer(
        "📊 <b>Статистика проекта</b>\n\n"
        "👥 <b>Пользователи</b>\n"
        f"  Всего в боте: <b>{total_users}</b> (нажали /start)\n"
        f"  Когда-либо платили: <b>{unique_payers}</b> | Никогда: <b>{never_paid}</b>\n"
        f"  Активных сейчас: <b>{active_users}</b> | Неактивных: <b>{inactive_users}</b>\n"
        f"  Новых за 24ч: <b>{new_24h}</b> | за 7 дн: <b>{new_7d}</b>\n"
        f"  Конверсия: <b>{conversion}%</b>\n\n"
        "🔑 <b>Ключи</b>\n"
        f"  Активных: <b>{active_keys}</b> | Отключённых: <b>{disabled_keys}</b>\n\n"
        "💰 <b>Доход</b>\n"
        f"  Итого: <b>{total_revenue} RUB</b>\n"
        f"  ⭐ Stars: <b>{stars_revenue} RUB</b> | 💳 Platega: <b>{platega_revenue} RUB</b>\n"
        f"  Средний чек: <b>{avg_check} RUB</b>\n\n"
        f"🖥 <b>Серверы (онлайн прямо сейчас):</b>\n{server_lines}",
    )


# ──────────────────────────────────────────
# /user <tg_id>
# ──────────────────────────────────────────
@router.message(Command("user"))
async def admin_user(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    target_id, err = await _resolve_user(message.text or "", "user", db)
    if not target_id:
        await message.answer(err or "Использование: /user &lt;tg_id&gt; или @username")
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
    username = user.get("username")
    username_str = f" (@{username})" if username else ""

    active_sub = await subs_repo.get_active(target_id)
    sub_expires = active_sub["expires_at"] if active_sub else expires_raw

    keys = await keys_repo.list_by_user(target_id)
    active_keys = [k for k in keys if not k.get("disabled_at")]
    disabled_keys = [k for k in keys if k.get("disabled_at")]
    keys_text = ""
    for k in active_keys:
        k_exp = _format_expiry(k.get("expires_at"))
        k_limit = k.get("traffic_limit_gb") or "—"
        icon = "⭐" if k.get("is_primary") else "🔑"
        keys_text += f"  {icon} Ключ #{k['id']} | до {k_exp} | {k_limit} ГБ\n"
    if disabled_keys:
        keys_text += f"  🚫 Удалённых: {len(disabled_keys)} шт. (id: {', '.join(str(k['id']) for k in disabled_keys)})\n"
    if not keys_text:
        keys_text = "  нет ключей\n"

    status_emoji = "✅" if is_active else "❌"
    await message.answer(
        f"👤 <b>Пользователь {target_id}</b>{username_str}\n\n"
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

    logger.warning("ADMIN BROADCAST | admin=%s recipients=active preview=%s", callback.from_user.id, text[:60])
    users_repo = UsersRepository(db)
    active_tg_ids = await users_repo.list_active_tg_ids()
    sent = failed = 0
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramBadRequest
    for tg_id in active_tg_ids:
        try:
            await callback.bot.send_message(tg_id, text, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramBadRequest:
            # HTML failed — send as plain text (admin may have forgotten to escape).
            try:
                await callback.bot.send_message(tg_id, escape(text), parse_mode=None)
                sent += 1
            except Exception:
                failed += 1
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
    target_id, err = await _resolve_user(message.text or "", "ban", db)
    if not target_id:
        await message.answer(err or "Использование: /ban &lt;tg_id&gt; или @username")
        return

    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    # Disable all VPN clients in XUI
    xui_ok = False
    try:
        manager = build_vpn_manager(db, settings)
        await manager.disable_user_access(target_id)
        xui_ok = True
    except Exception:
        logger.exception("admin_ban: disable_user_access failed tg_id=%s", target_id)
        await message.answer(
            f"⚠️ XUI отключение не удалось для {target_id}. "
            "Пользователь заблокирован в БД, но VPN может продолжать работать. "
            "Отключите вручную через XUI-панель."
        )

    await users_repo.update_status(target_id, False)
    await users_repo.set_banned(target_id, True)
    # Always block in-memory set regardless of XUI result: the DB is the source of
    # truth for ban state and the BanMiddleware must reflect it immediately.
    _BANNED_IDS.add(target_id)
    logger.warning("ADMIN BAN | admin=%s target=%s xui_ok=%s", message.from_user.id, target_id, xui_ok)

    # Notify the banned user.
    try:
        await message.bot.send_message(
            target_id,
            "🚫 <b>Ваш аккаунт заблокирован.</b>\n\n"
            "Если вы считаете это ошибкой, пожалуйста, обратитесь в поддержку:\n"
            f"@ZyberVPN_Support_bot",
        )
    except Exception:
        pass

    await message.answer(f"🚫 Пользователь {target_id} заблокирован. VPN-доступ отключён, уведомление отправлено.")


# ──────────────────────────────────────────
# /unban <tg_id>
# ──────────────────────────────────────────
@router.message(Command("unban"))
async def admin_unban(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    target_id, err = await _resolve_user(message.text or "", "unban", db)
    if not target_id:
        await message.answer(err or "Использование: /unban &lt;tg_id&gt; или @username")
        return

    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    # Re-enable account in DB — new key is NOT created automatically.
    await users_repo.update_status(target_id, True)
    await users_repo.set_banned(target_id, False)
    _BANNED_IDS.discard(target_id)
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


# ──────────────────────────────────────────
# /broadcastall <text>  — рассылка ВСЕМ пользователям
# ──────────────────────────────────────────
@router.message(Command("broadcastall"))
async def admin_broadcastall(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    text = (message.text or "").removeprefix("/broadcastall").strip()
    if not text:
        await message.answer("Использование: /broadcastall &lt;текст&gt;")
        return

    await state.set_state(AdminState.waiting_broadcastall_confirm)
    await state.update_data(broadcast_text=text)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить (всем)", callback_data="broadcastall_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcastall_cancel")],
    ])
    await message.answer(
        "📢 <b>Превью рассылки (ВСЕ пользователи):</b>\n\n"
        f"{text}\n\n"
        "──────────────────\n"
        "⚠️ Сообщение будет отправлено ВСЕМ пользователям бота, включая тех, у кого нет подписки.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "broadcastall_confirm", AdminState.waiting_broadcastall_confirm)
async def broadcastall_confirm(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
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
    # Fetch ALL tg_ids (not just active subscribers)
    all_tg_ids = await users_repo.list_all_tg_ids()
    sent = failed = 0
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramBadRequest
    for tg_id in all_tg_ids:
        try:
            await callback.bot.send_message(tg_id, text, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramBadRequest:
            try:
                await callback.bot.send_message(tg_id, escape(text), parse_mode=None)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await callback.message.answer(
        f"✅ Рассылка (все пользователи) завершена: {sent} отправлено, {failed} ошибок."
    )
    await callback.answer()


@router.callback_query(F.data == "broadcastall_cancel", AdminState.waiting_broadcastall_confirm)
async def broadcastall_cancel(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ Рассылка отменена.")
    await callback.answer()


# ──────────────────────────────────────────
# /restore_keys <tg_id>  — after a ban wipe
# ──────────────────────────────────────────
@router.message(Command("restore_keys"))
async def admin_restore_keys(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    target_id = _parse_tg_id(message.text or "", "restore_keys")
    if not target_id:
        await message.answer("Использование: /restore_keys &lt;tg_id&gt;")
        return

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)

    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    all_keys = await keys_repo.list_by_user(target_id)
    if not all_keys:
        await message.answer("❌ Ключи в таблице keys не найдены — нечего восстанавливать.")
        return

    expiry_dt = utc_now() + timedelta(days=30)
    expiry_ms = int(expiry_dt.timestamp() * 1000)
    expiry_iso = expiry_dt.isoformat()

    # Update keys table: extend expiry, clear disabled_at.
    for key_row in all_keys:
        key_id = int(key_row.get("id") or 0)
        if key_id:
            await keys_repo.update_expires_at(key_id, target_id, expiry_iso)

    logger.warning("ADMIN RESTORE_KEYS | admin=%s target=%s keys=%s", message.from_user.id, target_id, len(all_keys))
    manager = build_vpn_manager(db, settings)
    ok, failed = await manager.restore_user_keys(target_id, all_keys, expiry_ms)

    # Re-activate user account.
    await users_repo.update_status(target_id, True)
    await users_repo.set_banned(target_id, False)

    status = "✅" if ok > 0 else "❌"
    await message.answer(
        f"{status} Восстановление завершено.\n"
        f"Ключей восстановлено: <b>{ok}</b> | Ошибок: <b>{failed}</b>\n"
        f"Подписка до: <b>{expiry_dt.strftime('%d.%m.%Y')}</b>\n"
        f"Пользователь разбанен и активирован."
    )


# ──────────────────────────────────────────
# /reenable_key <tg_id> [key_id]
# ──────────────────────────────────────────
@router.message(Command("reenable_key"))
async def admin_reenable_key(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /reenable_key &lt;tg_id&gt; [key_id]")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Неверный tg_id.")
        return
    filter_key_id: int | None = None
    if len(parts) >= 3:
        try:
            filter_key_id = int(parts[2])
        except ValueError:
            await message.answer("❌ Неверный key_id.")
            return

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)

    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    all_keys = await keys_repo.list_by_user(target_id)
    if filter_key_id is not None:
        all_keys = [k for k in all_keys if int(k.get("id") or 0) == filter_key_id]
    if not all_keys:
        await message.answer("❌ Ключи не найдены.")
        return

    expiry_dt = utc_now() + timedelta(days=30)
    expiry_ms = int(expiry_dt.timestamp() * 1000)
    expiry_iso = expiry_dt.isoformat()

    manager = build_vpn_manager(db, settings)
    ok = failed = 0
    for key_row in all_keys:
        key_id = int(key_row.get("id") or 0)
        if not key_id:
            continue
        traffic_gb = int(key_row.get("traffic_limit_gb") or 0)
        if traffic_gb <= 0:
            traffic_gb = settings.vpn_total_gb
        try:
            await keys_repo.update_expires_at(key_id, target_id, expiry_iso)
            if traffic_gb != int(key_row.get("traffic_limit_gb") or 0):
                await keys_repo.update_traffic_limit(key_id, target_id, traffic_gb)
            result = await manager.reenable_key_access(target_id, key_id, expiry_ms, traffic_gb)
            if result:
                ok += 1
            else:
                failed += 1
                logger.warning("reenable_key_access returned False user_id=%s key_id=%s", target_id, key_id)
        except Exception:
            logger.exception("reenable_key failed user_id=%s key_id=%s", target_id, key_id)
            failed += 1

    if not bool(user.get("is_active")):
        await users_repo.update_status(target_id, True)

    status = "✅" if ok > 0 else "❌"
    await message.answer(
        f"{status} Ключи переактивированы.\n"
        f"Успешно: <b>{ok}</b> | Ошибок: <b>{failed}</b>\n"
        f"Подписка до: <b>{expiry_dt.strftime('%d.%m.%Y')}</b>"
    )


# ──────────────────────────────────────────
# /sync_servers — provision all existing keys on missing servers
# ──────────────────────────────────────────
@router.message(Command("sync_servers"))
async def admin_sync_servers(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    await message.answer("🔄 Запускаю синхронизацию серверов для всех ключей...")
    manager = build_vpn_manager(db, settings)
    keys_repo = KeysRepository(db)
    users_repo = UsersRepository(db)

    active_ids = await users_repo.list_active_tg_ids()
    ok = skipped = failed = 0
    for tg_id in active_ids:
        try:
            keys = await keys_repo.list_by_user(tg_id)
            for key_row in keys:
                key_id = key_row.get("id")
                if not key_id:
                    continue
                try:
                    await manager.sync_secondary_servers_for_key(tg_id, int(key_id))
                    ok += 1
                except Exception:
                    failed += 1
        except Exception:
            skipped += 1

    await message.answer(
        f"✅ Синхронизация завершена.\n"
        f"Обработано ключей: <b>{ok}</b>\n"
        f"Ошибок: <b>{failed}</b>\n"
        f"Пропущено пользователей: <b>{skipped}</b>"
    )


# ──────────────────────────────────────────
# /addbalance <tg_id> <amount>
# ──────────────────────────────────────────
@router.message(Command("addbalance"))
async def admin_add_balance(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /addbalance &lt;tg_id&gt; &lt;сумма&gt;")
        return
    try:
        target_id = int(parts[1])
        amount = int(parts[2])
    except ValueError:
        await message.answer("❌ Неверные аргументы. Пример: /addbalance 123456789 100")
        return
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return

    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    old_balance = int((user or {}).get("balance") or 0)
    await users_repo.add_balance(target_id, amount)
    logger.warning("ADMIN ADDBALANCE | admin=%s target=%s amount=%s", message.from_user.id, target_id, amount)
    try:
        await message.bot.send_message(
            target_id,
            f"💳 <b>Администратор пополнил ваш баланс на {amount} ₽</b>\n\n"
            f"Текущий баланс: {old_balance + amount} ₽",
        )
    except Exception:
        pass
    await message.answer(
        f"✅ Баланс пополнен.\n"
        f"Пользователь: <code>{target_id}</code>\n"
        f"Было: <b>{old_balance} ₽</b> → Стало: <b>{old_balance + amount} ₽</b>"
    )


# ──────────────────────────────────────────
# /setexpiry <tg_id> <days>  — с выбором ключа
# ──────────────────────────────────────────

def _setexpiry_keyboard(tg_id: int, days: int, active_keys: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for k in active_keys:
        k_id = int(k.get("id") or 0)
        exp_raw = k.get("expires_at")
        try:
            exp_str = to_moscow(parse_iso_utc(exp_raw)).strftime("%d.%m.%Y") if exp_raw else "—"
        except Exception:
            exp_str = "—"
        rows.append([InlineKeyboardButton(
            text=f"🔑 Ключ #{k_id} (до {exp_str})",
            callback_data=f"sexpk:{tg_id}:{k_id}:{days}",
        )])
    if len(active_keys) > 1:
        rows.append([InlineKeyboardButton(
            text="🔑 Все ключи",
            callback_data=f"sexpall:{tg_id}:{days}",
        )])
    rows.append([InlineKeyboardButton(
        text="🌐 Только подписку (без XUI)",
        callback_data=f"sexpsub:{tg_id}:{days}",
    )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="sexpcancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("setexpiry"))
async def admin_set_expiry(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /setexpiry &lt;tg_id&gt; &lt;дней&gt;\nПример: /setexpiry 123456789 30")
        return
    try:
        target_id = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.answer("❌ Неверные аргументы.")
        return
    if days <= 0 or days > 3650:
        await message.answer("❌ Количество дней: от 1 до 3650.")
        return

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    keys = await keys_repo.list_by_user(target_id)
    active_keys = [k for k in keys if not k.get("disabled_at")]
    expires_dt = utc_now() + timedelta(days=days)

    await message.answer(
        f"👤 <code>{target_id}</code> — продление на <b>{days} дн.</b>\n"
        f"Новая дата: <b>{to_moscow(expires_dt).strftime('%d.%m.%Y')}</b>\n\n"
        "Что продлить?",
        reply_markup=_setexpiry_keyboard(target_id, days, active_keys),
    )


async def _notify_user_expiry(bot, tg_id: int, expires_dt, key_label: str = "") -> None:
    try:
        suffix = f" (ключ {key_label})" if key_label else ""
        await bot.send_message(
            tg_id,
            f"✅ <b>Администратор продлил вашу подписку{suffix}!</b>\n\n"
            f"📅 Действует до: <b>{to_moscow(expires_dt).strftime('%d.%m.%Y')}</b>",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("sexpk:"))
async def setexpiry_key_cb(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, tg_id_str, key_id_str, days_str = callback.data.split(":")
        target_id, key_id, days = int(tg_id_str), int(key_id_str), int(days_str)
    except (ValueError, TypeError):
        await callback.answer("Ошибка", show_alert=True)
        return

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    key_row = await keys_repo.get_by_id_for_user(key_id, target_id)
    if not key_row:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    expires_dt = utc_now() + timedelta(days=days)
    expiry_ms = int(expires_dt.timestamp() * 1000)

    # Extend user.expires_at to max(current, new)
    user = await users_repo.get_by_tg_id(target_id)
    user_expires_dt = expires_dt
    raw = (user or {}).get("expires_at")
    if raw:
        try:
            user_expires_dt = max(expires_dt, parse_iso_utc(raw))
        except Exception:
            pass
    await users_repo.set_expiry(
        target_id, expires_at=user_expires_dt.isoformat(),
        is_active=True, plan="monthly", last_activated_at=utc_now().isoformat(),
    )

    # Update XUI + key DB record
    traffic_gb = int(key_row.get("traffic_limit_gb") or settings.vpn_total_gb)
    manager = build_vpn_manager(db, settings)
    xui_ok = False
    try:
        await manager.renew_user_access(target_id, expiry_ms, key_id=key_id, traffic_limit_gb=traffic_gb)
        xui_ok = True
    except Exception:
        logger.exception("setexpiry_key: XUI failed target=%s key=%s", target_id, key_id)
    await keys_repo.update_expires_at(key_id, target_id, expires_dt.isoformat())

    logger.warning("ADMIN SETEXPIRY_KEY | admin=%s target=%s key=%s days=%s", callback.from_user.id, target_id, key_id, days)
    xui_icon = "✅" if xui_ok else "⚠️ XUI не обновлён —"
    await callback.message.edit_text(
        f"✅ Ключ #{key_id} пользователя <code>{target_id}</code> продлён.\n"
        f"До: <b>{to_moscow(expires_dt).strftime('%d.%m.%Y')}</b>\n"
        f"{xui_icon} XUI {'обновлён' if xui_ok else 'не обновлён (ошибка)'}"
    )
    await _notify_user_expiry(callback.bot, target_id, expires_dt, f"#{key_id}")
    await callback.answer()


@router.callback_query(F.data.startswith("sexpall:"))
async def setexpiry_all_cb(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, tg_id_str, days_str = callback.data.split(":")
        target_id, days = int(tg_id_str), int(days_str)
    except (ValueError, TypeError):
        await callback.answer("Ошибка", show_alert=True)
        return

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    expires_dt = utc_now() + timedelta(days=days)
    expiry_ms = int(expires_dt.timestamp() * 1000)

    await users_repo.set_expiry(
        target_id, expires_at=expires_dt.isoformat(),
        is_active=True, plan="monthly", last_activated_at=utc_now().isoformat(),
    )

    keys = [k for k in await keys_repo.list_by_user(target_id) if not k.get("disabled_at")]
    manager = build_vpn_manager(db, settings)
    ok = failed = 0
    for k in keys:
        k_id = int(k.get("id") or 0)
        if not k_id:
            continue
        traffic_gb = int(k.get("traffic_limit_gb") or settings.vpn_total_gb)
        try:
            await manager.renew_user_access(target_id, expiry_ms, key_id=k_id, traffic_limit_gb=traffic_gb)
            await keys_repo.update_expires_at(k_id, target_id, expires_dt.isoformat())
            ok += 1
        except Exception:
            logger.exception("setexpiry_all: key %s failed", k_id)
            failed += 1

    logger.warning("ADMIN SETEXPIRY_ALL | admin=%s target=%s days=%s ok=%s fail=%s", callback.from_user.id, target_id, days, ok, failed)
    await callback.message.edit_text(
        f"✅ Все ключи <code>{target_id}</code> продлены.\n"
        f"До: <b>{to_moscow(expires_dt).strftime('%d.%m.%Y')}</b>\n"
        f"Ключей: {ok} ✅ | Ошибок: {failed} ⚠️"
    )
    await _notify_user_expiry(callback.bot, target_id, expires_dt)
    await callback.answer()


@router.callback_query(F.data.startswith("sexpsub:"))
async def setexpiry_sub_cb(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, tg_id_str, days_str = callback.data.split(":")
        target_id, days = int(tg_id_str), int(days_str)
    except (ValueError, TypeError):
        await callback.answer("Ошибка", show_alert=True)
        return

    users_repo = UsersRepository(db)
    expires_dt = utc_now() + timedelta(days=days)
    await users_repo.set_expiry(
        target_id, expires_at=expires_dt.isoformat(),
        is_active=True, plan="monthly", last_activated_at=utc_now().isoformat(),
    )
    logger.warning("ADMIN SETEXPIRY_SUB | admin=%s target=%s days=%s", callback.from_user.id, target_id, days)
    await callback.message.edit_text(
        f"✅ Подписка <code>{target_id}</code> продлена.\n"
        f"До: <b>{to_moscow(expires_dt).strftime('%d.%m.%Y')}</b>\n"
        "(ключи в XUI не изменены)"
    )
    await _notify_user_expiry(callback.bot, target_id, expires_dt)
    await callback.answer()


@router.callback_query(F.data == "sexpcancel")
async def setexpiry_cancel_cb(callback: CallbackQuery, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()


# ──────────────────────────────────────────
# /givekey <tg_id>  — выдать бесплатный ключ
# ──────────────────────────────────────────
@router.message(Command("givekey"))
async def admin_give_key(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    target_id = _parse_tg_id(message.text or "", "givekey")
    if not target_id:
        await message.answer("Использование: /givekey &lt;tg_id&gt;")
        return

    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(target_id)
    if not user:
        await message.answer(f"❌ Пользователь {target_id} не найден.")
        return

    # Ensure subscription is active for 30 days
    expires_dt = utc_now() + timedelta(days=30)
    await users_repo.set_expiry(
        target_id,
        expires_at=expires_dt.isoformat(),
        is_active=True,
        plan="monthly",
        last_activated_at=utc_now().isoformat(),
    )
    await users_repo.add_traffic_limit(target_id, 60)

    await message.answer(f"⏳ Создаю ключ для <code>{target_id}</code>...")
    try:
        result = await ensure_user_access(
            tg_id=target_id,
            db=db,
            settings=settings,
            require_active=True,
            force_new_key=True,
            action="create",
            traffic_limit_gb=60,
        )
        vpn_key = result.get("vpn_key", "")
        key_id = result.get("key_id", 0)
        logger.warning("ADMIN GIVEKEY | admin=%s target=%s key_id=%s", message.from_user.id, target_id, key_id)
        try:
            await message.bot.send_message(
                target_id,
                f"🎁 <b>Администратор выдал вам VPN-ключ на 30 дней!</b>\n\n"
                f"📅 Действует до: <b>{to_moscow(expires_dt).strftime('%d.%m.%Y')}</b>\n\n"
                "Откройте «Мои ключи» чтобы подключиться.",
            )
        except Exception:
            pass
        await message.answer(
            f"✅ Ключ выдан!\n"
            f"Пользователь: <code>{target_id}</code>\n"
            f"Key ID: <b>{key_id}</b>\n"
            f"До: <b>{to_moscow(expires_dt).strftime('%d.%m.%Y')}</b>\n"
            f"<code>{escape(vpn_key[:80])}...</code>"
        )
    except Exception:
        logger.exception("admin_give_key failed target_id=%s", target_id)
        await message.answer(f"❌ Не удалось создать ключ для {target_id}. Проверьте логи.")


# ──────────────────────────────────────────
# /delkey <tg_id> <key_id>
# ──────────────────────────────────────────
@router.message(Command("delkey"))
async def admin_del_key(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /delkey &lt;tg_id&gt; &lt;key_id&gt;")
        return
    try:
        target_id = int(parts[1])
        key_id = int(parts[2])
    except ValueError:
        await message.answer("❌ Неверные аргументы.")
        return

    keys_repo = KeysRepository(db)
    key_row = await keys_repo.get_by_id_for_user(key_id, target_id)
    if not key_row:
        await message.answer(f"❌ Ключ #{key_id} для пользователя {target_id} не найден.")
        return

    if key_row.get("disabled_at"):
        await message.answer(
            f"⚠️ Ключ #{key_id} уже отключён (disabled_at={key_row['disabled_at'][:10]})."
        )
        return

    # Disable in XUI
    xui_ok = False
    manager = build_vpn_manager(db, settings, bot=message.bot)
    try:
        await manager.disable_key_access(target_id, key_id)
        xui_ok = True
    except Exception:
        logger.exception("admin_del_key: disable_key_access failed key_id=%s", key_id)

    # Mark disabled in DB regardless of XUI result
    await keys_repo.mark_disabled(key_id, target_id)
    logger.warning("ADMIN DELKEY | admin=%s target=%s key_id=%s xui_ok=%s",
                   message.from_user.id, target_id, key_id, xui_ok)

    # Count remaining active keys
    all_keys = await keys_repo.list_by_user(target_id)
    remaining = sum(1 for k in all_keys if not k.get("disabled_at"))

    xui_line = "✅ XUI: клиент отключён" if xui_ok else "⚠️ XUI: не удалось отключить (проверьте вручную)"
    await message.answer(
        f"🗑 <b>Ключ #{key_id}</b> пользователя <code>{target_id}</code> удалён\n\n"
        f"{xui_line}\n"
        f"🗄 БД: disabled_at проставлен\n\n"
        f"Осталось активных ключей: <b>{remaining}</b>"
    )


# ──────────────────────────────────────────
# /payments <tg_id>
# ──────────────────────────────────────────
# ──────────────────────────────────────────
# /reflink <label>  — сгенерировать ссылку с меткой
# ──────────────────────────────────────────
@router.message(Command("reflink"))
async def admin_reflink(message: Message, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование: /reflink &lt;метка&gt;\n\n"
            "Метка — любое слово без пробелов, например: /reflink vixti\n"
            "Получите уникальную ссылку для этого человека."
        )
        return
    label = parts[1].strip().replace(" ", "_")[:50]
    admin_tg_id = message.from_user.id
    bot_info = await message.bot.get_me()
    bot_username = bot_info.username
    link = f"https://t.me/{bot_username}?start=refl_{admin_tg_id}_{label}"
    await message.answer(
        f"🔗 <b>Реферальная ссылка для «{escape(label)}»:</b>\n\n"
        f"<code>{link}</code>\n\n"
        "Все, кто перейдут по этой ссылке, будут помечены меткой "
        f"<b>{escape(label)}</b> в базе. Смотри статистику через /refstats."
    )


# ──────────────────────────────────────────
# /refstats — статистика по меткам
# ──────────────────────────────────────────
@router.message(Command("refstats"))
async def admin_refstats(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    users_repo = UsersRepository(db)
    rows = await users_repo.list_referrals_with_labels(message.from_user.id)
    if not rows:
        await message.answer("📊 Пока нет рефералов по твоим ссылкам.")
        return

    # Group by label (None → «без метки»)
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        label = str(row.get("ref_label") or "без метки")
        groups[label].append(row)

    lines = [f"📊 <b>Реферальная статистика</b> (всего: {len(rows)})\n"]
    for label, members in sorted(groups.items(), key=lambda x: -len(x[1])):
        active = sum(1 for m in members if m.get("is_active"))
        lines.append(f"🏷 <b>{escape(label)}</b>: {len(members)} чел. ({active} активных)")

    await message.answer("\n".join(lines))


@router.message(Command("payments"))
async def admin_payments(message: Message, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    target_id, err = await _resolve_user(message.text or "", "payments", db)
    if not target_id:
        await message.answer(err or "Использование: /payments &lt;tg_id&gt; или @username")
        return

    payments_repo = PaymentsRepository(db)
    rows = await payments_repo.list_by_user(target_id, limit=15)
    if not rows:
        await message.answer(f"❌ Платежи для пользователя {target_id} не найдены.")
        return

    lines = [f"💳 <b>Платежи пользователя {target_id}</b> (последние {len(rows)}):\n"]
    status_icons = {"active": "✅", "paid": "💚", "pending": "⏳", "failed": "❌", "provisioning": "🔄"}
    for row in rows:
        icon = status_icons.get(str(row.get("status") or ""), "❓")
        dt = _format_expiry(row.get("created_at"))
        amount = row.get("amount") or 0
        ptype = row.get("purchase_type") or "—"
        tariff = row.get("tariff_code") or "—"
        lines.append(f"{icon} <b>{amount} ₽</b> | {tariff} | {ptype} | {dt}")

    await message.answer("\n".join(lines))
