from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard(support_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="menu_keys")],
            [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="menu_profile")],
            [InlineKeyboardButton(text="🆘 Поддержка", url=support_url)],
        ]
    )


def keys_list_keyboard(key_rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for text, key_id in key_rows:
        rows.append([InlineKeyboardButton(text=text, callback_data=f"key_open:{key_id}")])
    rows.append([InlineKeyboardButton(text="🛒 Купить ключ", callback_data="buy_open")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_card_keyboard(key_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📲 Подключиться", callback_data=f"key_connect:{key_id}")],
            [InlineKeyboardButton(text="➕ Продлить этот ключ", callback_data="buy_open")],
            [InlineKeyboardButton(text="📱 Показать QR-код", callback_data=f"key_qr:{key_id}")],
            [InlineKeyboardButton(text="🔗 Подписка", callback_data=f"key_sub:{key_id}")],
            [InlineKeyboardButton(text="📝 Комментарии", callback_data=f"key_comment:{key_id}")],
            [InlineKeyboardButton(text="⬅️ Назад к списку ключей", callback_data="menu_keys")],
        ]
    )


def tariffs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1 месяц - 49 RUB", callback_data="tariff:m1")],
            [InlineKeyboardButton(text="3 месяца - 129 RUB", callback_data="tariff:m3")],
            [InlineKeyboardButton(text="6 месяцев - 225 RUB", callback_data="tariff:m6")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_keys")],
        ]
    )


def email_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Продолжить без почты", callback_data="email_skip")],
            [InlineKeyboardButton(text="⬅️ Назад к тарифам", callback_data="buy_open")],
        ]
    )


def payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 СБП / Platega", callback_data="pay:sbp")],
            [InlineKeyboardButton(text="🪙 Crypto / Platega", callback_data="pay:crypto")],
            [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay:stars")],
            [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="profile_promo")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="buy_open")],
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Моя подписка", callback_data="profile_subscription")],
            [InlineKeyboardButton(text="🤝 Реферальная программа", callback_data="profile_ref")],
            [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="profile_promo")],
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="profile_topup")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_menu")],
        ]
    )


def subscription_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile")],
        ]
    )


def topup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile")],
        ]
    )


def promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile")],
        ]
    )


def referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", callback_data="ref_share")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile")],
        ]
    )


def connect_devices_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Android", callback_data="device_android")],
            [InlineKeyboardButton(text="🍏 iOS", callback_data="device_ios")],
            [InlineKeyboardButton(text="💻 Windows", callback_data="device_windows")],
            [InlineKeyboardButton(text="🍎 macOS", callback_data="device_macos")],
            [InlineKeyboardButton(text="🐧 Linux", callback_data="device_linux")],
            [InlineKeyboardButton(text="📺 Android TV", callback_data="device_android_tv")],
            [InlineKeyboardButton(text="🍏 Apple TV", callback_data="device_apple_tv")],
            [InlineKeyboardButton(text="⬅️ Назад к ключу", callback_data="menu_keys")],
        ]
    )


def connect_apps_keyboard(apps: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=app_name, callback_data=app_callback)] for app_name, app_callback in apps]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="connect_back_devices")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def connect_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Скопировать ключ", callback_data="connect_copy_key")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="connect_back_devices")],
        ]
    )
