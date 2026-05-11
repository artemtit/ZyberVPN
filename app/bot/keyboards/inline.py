from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.plans import get_all_plans


def payment_success_keyboard(sub_url: str, key_id: int = 0) -> InlineKeyboardMarkup:
    connect_button = (
        InlineKeyboardButton(text="📲 Подключить", callback_data=f"key_connect:{key_id}")
        if key_id
        else InlineKeyboardButton(text="📲 Подключить", url=sub_url)
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [connect_button],
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



def payment_keyboard(platega_enabled: bool = False, platega_crypto_enabled: bool = False, show_test_pay: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if platega_enabled:
        rows.append([InlineKeyboardButton(text="💳 СБП / QR (Platega)", callback_data="pay:platega")])
    if platega_crypto_enabled:
        rows.append([InlineKeyboardButton(text="🪙 Криптовалюта (Platega)", callback_data="pay:platega_crypto")])
    rows.append([InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay:stars")])
    if show_test_pay:
        rows.append([InlineKeyboardButton(text="🧪 Тестовая оплата [Admin]", callback_data="pay:sbp")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к тарифам", callback_data="buy_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_back_keyboard(redirect_url: str, tariff_code: str = "", purchase_type: str = "new", renew_key_id: str = "0") -> InlineKeyboardMarkup:
    # Encode context in callback_data so back button works even after state.clear().
    back_data = f"payment_back:{tariff_code}:{purchase_type}:{renew_key_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=redirect_url)],
        [InlineKeyboardButton(text="⬅️ Вернуться к способам оплаты", callback_data=back_data)],
    ])


def topup_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к пополнению", callback_data="profile_topup")],
    ])


def stars_back_keyboard(tariff_code: str = "", purchase_type: str = "new", renew_key_id: str = "0") -> InlineKeyboardMarkup:
    back_data = f"payment_back:{tariff_code}:{purchase_type}:{renew_key_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к способам оплаты", callback_data=back_data)],
    ])


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
            [InlineKeyboardButton(text="⭐ 100 Stars → 100 RUB", callback_data="topup_stars:100")],
            [InlineKeyboardButton(text="⭐ 300 Stars → 300 RUB", callback_data="topup_stars:300")],
            [InlineKeyboardButton(text="⭐ 500 Stars → 500 RUB", callback_data="topup_stars:500")],
            [InlineKeyboardButton(text="⭐ 1000 Stars → 1000 RUB", callback_data="topup_stars:1000")],
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
