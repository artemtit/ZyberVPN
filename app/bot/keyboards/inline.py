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
            [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="profile_promo")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="buy_open")],
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤝 Реферальная программа", callback_data="profile_ref")],
            [InlineKeyboardButton(text="🎁 Активировать промокод", callback_data="profile_promo")],
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="profile_topup")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_menu")],
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
