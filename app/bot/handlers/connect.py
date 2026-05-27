from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.keyboards.inline import connect_apps_keyboard, connect_devices_keyboard, connect_result_keyboard
from app.bot.keyboards.main import get_main_menu_keyboard
from app.bot.states.connect import ConnectFlowState
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, ensure_user_access
from app.utils.datetime import parse_iso_utc, utc_now

router = Router()
logger = logging.getLogger(__name__)


DEVICES: dict[str, str] = {
    "android": "Android",
    "ios": "iOS",
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
    "android_tv": "Android TV",
    "apple_tv": "Apple TV",
}

APPS: dict[str, list[tuple[str, str]]] = {
    "android": [("v2rayNG", "app_v2rayng"), ("V2RayTun", "app_v2raytun")],
    "android_tv": [("v2rayNG", "app_v2rayng"), ("V2RayTun", "app_v2raytun")],
    "ios": [("Happ", "app_happ"), ("Shadowrocket", "app_shadowrocket")],
    "apple_tv": [("Happ", "app_happ"), ("Shadowrocket", "app_shadowrocket")],
    "windows": [("Happ", "app_happ_win")],
    "macos": [("Happ", "app_happ_mac")],
    "linux": [("CLI", "app_cli")],
}

INSTRUCTIONS: dict[str, str] = {
    "app_v2rayng": (
        "1️⃣ Установите <b>v2rayNG</b> из Google Play\n"
        "2️⃣ Нажмите <b>«+»</b> в правом верхнем углу\n"
        "3️⃣ Выберите <b>«Import config from clipboard»</b>\n"
        "4️⃣ Вставьте ссылку → нажмите ✔️\n"
        "5️⃣ Нажмите <b>▶ Старт</b> — готово!"
    ),
    "app_v2raytun": (
        "1️⃣ Установите <b>V2RayTun</b> из Google Play\n"
        "2️⃣ Откройте приложение → нажмите <b>«+»</b>\n"
        "3️⃣ Выберите <b>«Import from clipboard»</b>\n"
        "4️⃣ Вставьте ссылку и сохраните\n"
        "5️⃣ Нажмите <b>Подключить</b> — готово!"
    ),
    "app_shadowrocket": (
        "1️⃣ Установите <b>Shadowrocket</b> из App Store\n"
        "2️⃣ Нажмите <b>«+»</b> (сверху справа)\n"
        "3️⃣ Выберите <b>«Import from Clipboard»</b>\n"
        "4️⃣ Нажмите на добавленный профиль → <b>Connect</b>"
    ),
    "app_happ": (
        "1️⃣ Установите <b>Happ</b> из App Store\n"
        "2️⃣ Нажмите <b>«+»</b> → <b>«From clipboard»</b>\n"
        "3️⃣ Профиль добавится автоматически\n"
        "4️⃣ Нажмите <b>Connect</b> — готово!"
    ),
    "app_happ_win": (
        "1️⃣ <a href=\"https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe\">Скачайте и установите Happ</a>\n"
        "2️⃣ Нажмите <b>«+»</b> → <b>«From clipboard»</b>\n"
        "3️⃣ Профиль добавится автоматически\n"
        "4️⃣ Нажмите <b>Connect</b> — готово!"
    ),
    "app_happ_mac": (
        "1️⃣ <a href=\"https://github.com/Happ-proxy/happ-desktop/releases/latest\">Скачайте и установите Happ</a>\n"
        "2️⃣ Нажмите <b>«+»</b> → <b>«From clipboard»</b>\n"
        "3️⃣ Профиль добавится автоматически\n"
        "4️⃣ Нажмите <b>Connect</b> — готово!"
    ),
    "app_cli": (
        "1️⃣ <a href=\"https://github.com/XTLS/Xray-core/releases/latest\">Скачайте Xray</a>\n"
        "2️⃣ Создайте файл <code>config.json</code>\n"
        "3️⃣ Добавьте ключ как <code>outbound</code>\n"
        "4️⃣ Запустите: <code>xray run -c config.json</code>"
    ),
}


def _apps_for_device(device_code: str) -> list[tuple[str, str]]:
    return APPS.get(device_code, [])


def _app_name(callback_data: str) -> str | None:
    for apps in APPS.values():
        for name, callback in apps:
            if callback == callback_data:
                return name
    return None


@router.callback_query(F.data.startswith("key_connect:"))
async def connect_open(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    tg_id = callback.from_user.id
    requested_key_id = 0
    try:
        requested_key_id = int(callback.data.split(":", 1)[1])
    except Exception:
        requested_key_id = 0

    if requested_key_id > 0:
        users_repo = UsersRepository(db)
        user = await users_repo.get_by_tg_id(tg_id)
        if not user or not users_repo.is_user_active(user):
            await callback.answer("Подписка истекла", show_alert=True)
            return

        keys_repo = KeysRepository(db)
        key_row = await keys_repo.get_by_id_for_user(requested_key_id, tg_id)
        if not key_row:
            await callback.answer("Ключ не найден", show_alert=True)
            return
        key_expires = key_row.get("expires_at")
        if key_expires:
            try:
                if parse_iso_utc(key_expires) <= utc_now():
                    await callback.answer("Ключ истёк", show_alert=True)
                    return
            except Exception:
                await callback.answer("Ключ истёк", show_alert=True)
                return

        vpn_key = str(key_row.get("key") or "")
        if not vpn_key.startswith("vless://"):
            await callback.answer("Не удалось получить VPN-ключ. Попробуйте позже.", show_alert=True)
            return
        key_sub_token = str(key_row.get("sub_token") or "")
        if not key_sub_token:
            try:
                key_sub_token = await keys_repo.ensure_sub_token(requested_key_id, tg_id)
            except Exception:
                logger.exception("Failed to build subscription URL for tg_id=%s key_id=%s", tg_id, requested_key_id)
        sub_url = f"{settings.public_base_url}/sub/{key_sub_token}" if key_sub_token and settings.public_base_url else ""

        await state.clear()
        await state.set_state(ConnectFlowState.choosing_device)
        await state.update_data(vpn_key=vpn_key, sub_url=sub_url, vpn_configs=[vpn_key])

        await callback.message.edit_text(
            "📲 <b>Подключение к ZyberVPN</b>\n\nНа каком устройстве хотите настроить VPN?",
            reply_markup=connect_devices_keyboard(),
        )
        await callback.answer()
        return

    try:
        access_user = await ensure_user_access(tg_id=tg_id, db=db, settings=settings, require_active=True)
    except AccessEnsureError as error:
        logger.warning("Connect access failed for tg_id=%s: %s", tg_id, error)
        if "inactive" in str(error).lower():
            await callback.answer("Подписка истекла", show_alert=True)
            return
        await callback.answer("Не удалось подготовить доступ. Попробуйте позже.", show_alert=True)
        return

    vpn_configs = [str(item) for item in (access_user.get("vpn_configs") or []) if str(item).startswith("vless://")]
    vpn_key = str(access_user.get("vpn_key") or (vpn_configs[0] if vpn_configs else ""))
    # Build subscription URL from the primary key's per-key sub_token.
    # users.sub_token is NOT resolved by SubscriptionService; only keys.sub_token is.
    sub_url = ""
    try:
        keys_repo = KeysRepository(db)
        user_keys = await keys_repo.list_by_user(tg_id)
        primary_key_row = next((k for k in user_keys if k.get("is_primary")), user_keys[0] if user_keys else None)
        if primary_key_row:
            key_sub_token = str(primary_key_row.get("sub_token") or "")
            if not key_sub_token:
                key_sub_token = await keys_repo.ensure_sub_token(int(primary_key_row["id"]), tg_id)
            if key_sub_token and settings.public_base_url:
                sub_url = f"{settings.public_base_url}/sub/{key_sub_token}"
    except Exception:
        logger.exception("Failed to build subscription URL for tg_id=%s", tg_id)
    if not vpn_key and not vpn_configs:
        await callback.answer("Не удалось получить VPN-ключ. Попробуйте позже.", show_alert=True)
        return

    await state.clear()
    await state.set_state(ConnectFlowState.choosing_device)
    await state.update_data(vpn_key=vpn_key, sub_url=sub_url, vpn_configs=vpn_configs)

    await callback.message.edit_text(
        "📲 <b>Подключение к ZyberVPN</b>\n\nНа каком устройстве хотите настроить VPN?",
        reply_markup=connect_devices_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "connect_instruction")
async def connect_instruction_open(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ConnectFlowState.choosing_device)
    await state.update_data(instruction_only=True)

    await callback.message.edit_text(
        "📲 <b>Подключение к ZyberVPN</b>\n\nНа каком устройстве хотите посмотреть инструкцию?",
        reply_markup=connect_devices_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("device_"))
async def connect_choose_device(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    vpn_key = data.get("vpn_key")
    sub_url = data.get("sub_url")
    vpn_configs = [str(item) for item in (data.get("vpn_configs") or []) if str(item).startswith("vless://")]
    instruction_only = bool(data.get("instruction_only"))

    if not vpn_key and not instruction_only:
        await callback.answer("Сессия подключения истекла. Откройте ключ заново.", show_alert=True)
        return

    device_code = callback.data.removeprefix("device_")
    device_name = DEVICES.get(device_code)
    if not device_name:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    apps = _apps_for_device(device_code)
    if not apps:
        await callback.answer("Для устройства пока нет приложений", show_alert=True)
        return

    app_name, app_callback = apps[0]
    instruction = INSTRUCTIONS.get(app_callback, "Инструкция скоро появится.")

    await state.set_state(ConnectFlowState.done)
    await state.update_data(device_code=device_code, device_name=device_name, app_callback=app_callback, app_name=app_name)

    if instruction_only:
        text = (
            f"📲 <b>Подключение ZyberVPN</b>\n"
            f"📱 <b>{escape(device_name)}</b>\n\n"
            f"📋 <b>Инструкция:</b>\n{instruction}"
        )
    elif app_callback == "app_cli":
        text = (
            f"📲 <b>Подключение ZyberVPN</b>\n"
            f"📱 <b>{escape(device_name)}</b>\n\n"
            f"🔑 Ваш ключ:\n<code>{escape(vpn_key)}</code>\n\n"
            f"📋 <b>Инструкция:</b>\n{instruction}"
        )
    else:
        connection_value = sub_url or vpn_key
        text = (
            f"📲 <b>Подключение ZyberVPN</b>\n"
            f"📱 <b>{escape(device_name)}</b>\n\n"
            f"🔗 <b>Ссылка для подключения:</b>\n<code>{escape(connection_value)}</code>\n\n"
            f"📋 <b>Инструкция:</b>\n{instruction}"
        )

    await callback.message.edit_text(text, reply_markup=connect_result_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("app_"))
async def connect_choose_app(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    vpn_key = data.get("vpn_key")
    sub_url = data.get("sub_url")
    device_code = data.get("device_code")
    device_name = data.get("device_name")
    vpn_configs = [str(item) for item in (data.get("vpn_configs") or []) if str(item).startswith("vless://")]
    instruction_only = bool(data.get("instruction_only"))

    if (not vpn_key and not instruction_only) or not device_code or not device_name:
        await callback.answer("Сессия подключения истекла. Откройте ключ заново.", show_alert=True)
        return

    available_callbacks = {callback_data for _, callback_data in _apps_for_device(device_code)}
    app_callback = callback.data
    if app_callback not in available_callbacks:
        await callback.answer("Это приложение недоступно для выбранного устройства", show_alert=True)
        return

    app_name = _app_name(app_callback)
    if not app_name:
        await callback.answer("Приложение не найдено", show_alert=True)
        return

    instruction = INSTRUCTIONS.get(app_callback, "Инструкция скоро появится.")

    await state.set_state(ConnectFlowState.done)
    await state.update_data(app_callback=app_callback, app_name=app_name)

    if instruction_only:
        text = (
            f"📲 <b>Подключение ZyberVPN</b>\n"
            f"📱 {escape(device_name)} · <b>{escape(app_name)}</b>\n\n"
            f"📋 <b>Инструкция:</b>\n{instruction}"
        )
    elif app_callback == "app_cli":
        text = (
            f"📲 <b>Подключение ZyberVPN</b>\n"
            f"📱 {escape(device_name)} · <b>{escape(app_name)}</b>\n\n"
            f"🔑 Ваш ключ:\n<code>{escape(vpn_key)}</code>\n\n"
            f"📋 <b>Инструкция:</b>\n{instruction}"
        )
    else:
        connection_value = sub_url or vpn_key
        text = (
            f"📲 <b>Подключение ZyberVPN</b>\n"
            f"📱 {escape(device_name)} · <b>{escape(app_name)}</b>\n\n"
            f"🔗 <b>Ссылка для подключения:</b>\n<code>{escape(connection_value)}</code>\n\n"
            f"📋 <b>Инструкция:</b>\n{instruction}"
        )

    await callback.message.edit_text(text, reply_markup=connect_result_keyboard())
    await callback.answer()


@router.callback_query(F.data == "connect_copy_sub")
async def connect_copy_sub(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    data = await state.get_data()
    sub_url = data.get("sub_url")
    if not sub_url:
        try:
            keys_repo = KeysRepository(db)
            user_keys = await keys_repo.list_by_user(callback.from_user.id)
            active_keys = [
                k for k in user_keys
                if not k.get("disabled_at") and str(k.get("key") or "").startswith("vless://")
            ]
            latest_key = active_keys[-1] if active_keys else None
            if latest_key:
                key_sub_token = str(latest_key.get("sub_token") or "")
                if not key_sub_token:
                    key_sub_token = await keys_repo.ensure_sub_token(int(latest_key["id"]), callback.from_user.id)
                if key_sub_token and settings.public_base_url:
                    sub_url = f"{settings.public_base_url}/sub/{key_sub_token}"
        except Exception:
            logger.exception("Failed to fetch subscription URL for copy tg_id=%s", callback.from_user.id)
    if not sub_url:
        await callback.answer("Subscription URL недоступен. Откройте ключ заново.", show_alert=True)
        return
    await callback.message.answer(
        f"🔗 Subscription URL:\n<code>{escape(str(sub_url))}</code>",
        reply_markup=get_main_menu_keyboard(),
    )
    await callback.answer("Ссылка отправлена")


@router.callback_query(F.data == "connect_copy_key")
async def connect_copy_key(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    vpn_key = data.get("vpn_key")
    vpn_configs = [str(item) for item in (data.get("vpn_configs") or []) if str(item).startswith("vless://")]
    if not vpn_key:
        await callback.answer("Сессия подключения истекла. Откройте ключ заново.", show_alert=True)
        return

    text = f"🔑 Ваш ключ:\n<code>{escape(vpn_key)}</code>"
    if vpn_configs and len(vpn_configs) > 1:
        rendered = "\n".join(f"<code>{escape(item)}</code>" for item in vpn_configs[:6])
        text += f"\n\nВсе конфиги:\n{rendered}"
    await callback.message.answer(text, reply_markup=get_main_menu_keyboard())
    await callback.answer("Ключ отправлен")


@router.callback_query(F.data == "connect_back_devices")
async def connect_back_devices(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    instruction_only = bool(data.get("instruction_only"))
    if not data.get("vpn_key") and not instruction_only:
        await callback.answer("Сессия подключения истекла. Откройте ключ заново.", show_alert=True)
        return

    await state.set_state(ConnectFlowState.choosing_device)
    await callback.message.edit_text(
        "📲 <b>Подключение к ZyberVPN</b>\n\n"
        f"На каком устройстве хотите {'посмотреть инструкцию' if instruction_only else 'настроить VPN'}?",
        reply_markup=connect_devices_keyboard(),
    )
    await callback.answer()
