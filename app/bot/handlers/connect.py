from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.keyboards.inline import connect_apps_keyboard, connect_devices_keyboard, connect_result_keyboard
from app.bot.states.connect import ConnectFlowState
from app.config import Settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.users import UsersRepository
from app.services.vpn import VPNProvisionError, create_vpn_key_via_3xui

router = Router()
logger = logging.getLogger(__name__)


DEVICES: dict[str, str] = {
    "android": "📱 Android",
    "ios": "🍏 iOS",
    "windows": "💻 Windows",
    "macos": "🍎 macOS",
    "linux": "🐧 Linux",
    "android_tv": "📺 Android TV",
    "apple_tv": "🍏 Apple TV",
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
    "app_v2rayng": "1. Установите v2rayNG\n2. Откройте приложение\n3. Нажмите \"+\"\n4. Импорт из буфера\n5. Вставьте ключ\n6. Подключитесь",
    "app_v2raytun": "1. Установите V2RayTun\n2. Откройте приложение\n3. Добавьте новый профиль\n4. Выберите импорт из буфера\n5. Вставьте ключ\n6. Подключитесь",
    "app_shadowrocket": "1. Установите Shadowrocket\n2. Нажмите \"+\"\n3. Import from Clipboard\n4. Вставьте ключ\n5. Подключитесь",
    "app_happ": "1. Установите Happ\n2. Откройте приложение\n3. Добавьте конфигурацию\n4. Выберите импорт из буфера\n5. Вставьте ключ\n6. Подключитесь",
    "app_v2rayn": "1. Установите v2rayN\n2. Откройте приложение\n3. Нажмите \"Add\"\n4. Выберите import from clipboard\n5. Вставьте ключ\n6. Подключитесь",
    "app_v2rayx": "1. Установите V2RayX\n2. Откройте приложение\n3. Импортируйте конфигурацию из буфера\n4. Вставьте ключ\n5. Активируйте профиль\n6. Подключитесь",
    "app_cli": "1. Установите CLI-клиент V2Ray/Xray\n2. Создайте конфигурационный файл\n3. Вставьте ключ в конфигурацию\n4. Сохраните файл\n5. Запустите клиент\n6. Проверьте подключение",
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
    key_id = int(callback.data.split(":")[1])
    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)

    tg_id = callback.from_user.id
    supabase_user = await users_repo.get_by_tg_id(tg_id)

    if supabase_user:
        if not users_repo.is_user_active(supabase_user):
            await users_repo.update_status(tg_id, False)
            await callback.answer("❌ Подписка истекла", show_alert=True)
            return
        vpn_key = supabase_user.get("vpn_key") or ""
    else:
        vpn_key = ""

    if not vpn_key:
        try:
            vpn_key = await create_vpn_key_via_3xui(settings=settings, tg_id=tg_id)
        except VPNProvisionError:
            logger.exception("Failed to provision VPN key via 3x-ui for tg_id=%s", tg_id)
            local_user = await users_repo.get_or_create(tg_id)
            local_key = await keys_repo.get_by_id_for_user(key_id, local_user["id"])
            if not local_key:
                all_local_keys = await keys_repo.list_by_user(local_user["id"])
                local_key = all_local_keys[0] if all_local_keys else None
            if not local_key:
                await callback.answer("Не удалось создать VPN-ключ. Попробуйте позже.", show_alert=True)
                return
            vpn_key = local_key["key"]
        else:
            if supabase_user:
                await users_repo.update_key(tg_id, vpn_key)
            else:
                expires_at = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
                created = await users_repo.create(
                    tg_id=tg_id,
                    vpn_key=vpn_key,
                    sub_token=str(uuid4()),
                    expires_at=expires_at,
                    is_active=True,
                    plan="trial",
                )
                if not created:
                    logger.error("Supabase create failed in connect flow for tg_id=%s", tg_id)

    await state.clear()
    await state.set_state(ConnectFlowState.choosing_device)
    await state.update_data(vpn_key=vpn_key)

    await callback.message.edit_text(
        "🚀 Подключение к ZyberVPN\n\nВыберите устройство:",
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
        f"🚀 Подключение к ZyberVPN\n\n📱 Устройство: {device_name}\n\nВыберите приложение:",
        reply_markup=connect_apps_keyboard(apps),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("app_"))
async def connect_choose_app(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    vpn_key = data.get("vpn_key")
    device_code = data.get("device_code")
    device_name = data.get("device_name")

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

    await state.set_state(ConnectFlowState.done)
    await state.update_data(app_callback=app_callback, app_name=app_name)

    text = (
        "🚀 Подключение к ZyberVPN\n\n"
        f"📱 Устройство: {device_name}\n"
        f"⚙️ Приложение: {app_name}\n\n"
        f"🔑 Ваш ключ: <code>{escape(vpn_key)}</code>\n\n"
        "📋 Инструкция:\n"
        f"{instruction}"
    )

    await callback.message.edit_text(text, reply_markup=connect_result_keyboard())
    await callback.answer()


@router.callback_query(F.data == "connect_copy_key")
async def connect_copy_key(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    vpn_key = data.get("vpn_key")
    if not vpn_key:
        await callback.answer("Сессия подключения истекла. Откройте ключ заново.", show_alert=True)
        return

    await callback.message.answer(f"🔑 Ваш ключ:\n<code>{escape(vpn_key)}</code>")
    await callback.answer("Ключ отправлен")


@router.callback_query(F.data == "connect_back_devices")
async def connect_back_devices(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("vpn_key"):
        await callback.answer("Сессия подключения истекла. Откройте ключ заново.", show_alert=True)
        return

    await state.set_state(ConnectFlowState.choosing_device)
    await callback.message.edit_text(
        "🚀 Подключение к ZyberVPN\n\nВыберите устройство:",
        reply_markup=connect_devices_keyboard(),
    )
    await callback.answer()