from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.keyboards.inline import connect_apps_keyboard, connect_devices_keyboard, connect_result_keyboard
from app.bot.states.connect import ConnectFlowState
from app.config import Settings
from app.db.database import Database
from app.services.access import AccessEnsureError, ensure_user_access

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
    "ios": [("Shadowrocket", "app_shadowrocket"), ("Happ", "app_happ")],
    "apple_tv": [("Shadowrocket", "app_shadowrocket"), ("Happ", "app_happ")],
    "windows": [("v2rayN", "app_v2rayn")],
    "macos": [("V2RayX", "app_v2rayx")],
    "linux": [("CLI", "app_cli")],
}

INSTRUCTIONS: dict[str, str] = {
    "app_v2rayng": "1. Установите v2rayNG\n2. Откройте приложение\n3. Нажмите +\n4. Импорт из буфера\n5. Вставьте ключ\n6. Подключитесь",
    "app_v2raytun": "1. Установите V2RayTun\n2. Откройте приложение\n3. Добавьте новый профиль\n4. Выберите импорт из буфера\n5. Вставьте ключ\n6. Подключитесь",
    "app_shadowrocket": "1. Установите Shadowrocket\n2. Нажмите +\n3. Import from Clipboard\n4. Вставьте ключ\n5. Подключитесь",
    "app_happ": "1. Установите Happ\n2. Откройте приложение\n3. Добавьте конфигурацию\n4. Выберите импорт из буфера\n5. Вставьте ключ\n6. Подключитесь",
    "app_v2rayn": "1. Установите v2rayN\n2. Откройте приложение\n3. Нажмите Add\n4. Выберите import from clipboard\n5. Вставьте ключ\n6. Подключитесь",
    "app_v2rayx": "1. Установите V2RayX\n2. Откройте приложение\n3. Импортируйте конфигурацию из буфера\n4. Вставьте ключ\n5. Активируйте профиль\n6. Подключитесь",
    "app_cli": "1. Установите CLI-клиент V2Ray/Xray\n2. Создайте конфиг-файл\n3. Вставьте ключ\n4. Сохраните файл\n5. Запустите клиент\n6. Проверьте подключение",
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
    sub_token = str(access_user.get("sub_token") or "")
    sub_url = f"{settings.public_base_url}/sub/{sub_token}" if settings.public_base_url and sub_token else ""
    if not vpn_key and not vpn_configs:
        await callback.answer("Не удалось получить VPN-ключ. Попробуйте позже.", show_alert=True)
        return

    await state.clear()
    await state.set_state(ConnectFlowState.choosing_device)
    await state.update_data(vpn_key=vpn_key, sub_url=sub_url, vpn_configs=vpn_configs)

    await callback.message.edit_text(
        "Подключение к ZyberVPN\n\nВыберите устройство:",
        reply_markup=connect_devices_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("device_"))
async def connect_choose_device(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("vpn_key"):
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

    await state.set_state(ConnectFlowState.choosing_app)
    await state.update_data(device_code=device_code, device_name=device_name)

    await callback.message.edit_text(
        f"Подключение к ZyberVPN\n\nУстройство: {device_name}\n\nВыберите приложение:",
        reply_markup=connect_apps_keyboard(apps),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("app_"))
async def connect_choose_app(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    vpn_key = data.get("vpn_key")
    sub_url = data.get("sub_url")
    device_code = data.get("device_code")
    device_name = data.get("device_name")
    vpn_configs = [str(item) for item in (data.get("vpn_configs") or []) if str(item).startswith("vless://")]

    if not vpn_key or not device_code or not device_name:
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
    sub_block = ""
    if sub_url:
        sub_block = f"Subscription: <code>{escape(str(sub_url))}</code>\n\n"
    config_block = ""
    if vpn_configs:
        rendered = "\n".join(f"<code>{escape(item)}</code>" for item in vpn_configs[:6])
        config_block = f"Конфиги:\n{rendered}\n\n"

    await state.set_state(ConnectFlowState.done)
    await state.update_data(app_callback=app_callback, app_name=app_name)

    text = (
        "Подключение к ZyberVPN\n\n"
        f"Устройство: {device_name}\n"
        f"Приложение: {app_name}\n\n"
        f"Ваш ключ: <code>{escape(vpn_key)}</code>\n\n"
        f"{sub_block}"
        f"{config_block}"
        "Инструкция:\n"
        f"{instruction}"
    )

    await callback.message.edit_text(text, reply_markup=connect_result_keyboard())
    await callback.answer()


@router.callback_query(F.data == "connect_copy_key")
async def connect_copy_key(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    vpn_key = data.get("vpn_key")
    sub_url = data.get("sub_url")
    vpn_configs = [str(item) for item in (data.get("vpn_configs") or []) if str(item).startswith("vless://")]
    if not vpn_key:
        await callback.answer("Сессия подключения истекла. Откройте ключ заново.", show_alert=True)
        return

    text = f"Ваш ключ:\n<code>{escape(vpn_key)}</code>"
    if sub_url:
        text += f"\n\nSubscription:\n<code>{escape(str(sub_url))}</code>"
    if vpn_configs:
        rendered = "\n".join(f"<code>{escape(item)}</code>" for item in vpn_configs[:6])
        text += f"\n\nКонфиги:\n{rendered}"
    await callback.message.answer(text)
    await callback.answer("Ключ отправлен")


@router.callback_query(F.data == "connect_back_devices")
async def connect_back_devices(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("vpn_key"):
        await callback.answer("Сессия подключения истекла. Откройте ключ заново.", show_alert=True)
        return

    await state.set_state(ConnectFlowState.choosing_device)
    await callback.message.edit_text(
        "Подключение к ZyberVPN\n\nВыберите устройство:",
        reply_markup=connect_devices_keyboard(),
    )
    await callback.answer()
