from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.plans import get_all_plans


def payment_success_keyboard(sub_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📲 Подключить", url=sub_url)],
            [InlineKeyboardButton(text="📱 Показать QR-код", callback_data="payment_show_qr")],
        ]
    )


def main_menu_keyboard(support_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_open")],
            [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="menu_keys")],
            [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="menu_profile")],
            [InlineKeyboardButton(text="🆘 Поддержка", url=support_url)],
            [InlineKeyboardButton(text="📄 Документы", callback_data="legal_docs")],
        ]
    )


def legal_keyboard(privacy_policy_url: str, terms_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=privacy_policy_url)],
            [InlineKeyboardButton(text="📋 Пользовательское соглашение", url=terms_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
        ]
    )


def keys_list_keyboard(key_rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for text, key_id in key_rows:
        rows.append([InlineKeyboardButton(text=text, callback_data=f"key_open:{key_id}")])
    rows.append([InlineKeyboardButton(text="🛒 Купить ключ", callback_data="buy_open")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_card_keyboard(
    key_id: int,
    is_primary: bool = False,
    has_comment: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📲 Подключиться", callback_data=f"key_connect:{key_id}")],
        [InlineKeyboardButton(text="➕ Продлить этот ключ", callback_data=f"key_renew:{key_id}")],
        [InlineKeyboardButton(text="📱 Показать QR-код", callback_data=f"key_qr:{key_id}")],
        [InlineKeyboardButton(text="📝 Комментарии", callback_data=f"key_comment:{key_id}")],
    ]
    if has_comment:
        rows.append([InlineKeyboardButton(text="🗑 Удалить комментарий", callback_data=f"key_comment_delete:{key_id}")])
    if is_primary:
        rows.append([InlineKeyboardButton(text="⭐ (Основной ключ)", callback_data="noop")])
    else:
        rows.append([InlineKeyboardButton(text="⭐ Сделать основным", callback_data=f"key_set_primary:{key_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к списку ключей", callback_data="menu_keys")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariffs_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for plan in get_all_plans():
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan['name']} — {plan['traffic_gb']} ГБ — {plan['price_rub']}₽",
                    callback_data=f"buy_plan:{plan['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_keys")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def email_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Продолжить без почты", callback_data="email_skip")],
            [InlineKeyboardButton(text="⬅️ Назад к тарифам", callback_data="buy_open")],
        ]
    )


def payment_keyboard(platega_enabled: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if platega_enabled:
        rows.append([InlineKeyboardButton(text="💳 СБП / QR (Platega)", callback_data="pay:platega")])
    rows.append([InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay:stars")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="buy_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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


def promo_apply_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Продлить активную подписку", callback_data="promo_apply:active")],
            [InlineKeyboardButton(text="🆕 Активировать и показать подключение", callback_data="promo_apply:new")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_profile")],
        ]
    )


def referral_keyboard(ref_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться в Telegram", url=ref_url)],
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
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="connect_back_devices")],
        ]
    )
