from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.tariffs import TARIFFS


def keys_menu_keyboard(keys: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key_data in keys:
        rows.append([InlineKeyboardButton(text=f"Ключ #{key_data['id']}", callback_data=f"key_open:{key_data['id']}")])
    rows.append([InlineKeyboardButton(text="🛍 Купить подписку", callback_data="buy_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_detail_keyboard(key_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подключиться", callback_data=f"key_connect:{key_id}")],
            [InlineKeyboardButton(text="Показать QR", callback_data=f"key_qr:{key_id}")],
            [InlineKeyboardButton(text="Продлить", callback_data="buy_open")],
            [InlineKeyboardButton(text="Назад", callback_data="keys_back")],
        ]
    )


def tariffs_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{tariff['title']} — {tariff['price_rub']} RUB",
                callback_data=f"tariff:{code}",
            )
        ]
        for code, tariff in TARIFFS.items()
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def email_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продолжить без email", callback_data="email_skip")],
            [InlineKeyboardButton(text="Назад", callback_data="buy_open")],
        ]
    )


def payment_methods_keyboard(tariff_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Telegram Stars", callback_data=f"pay:stars:{tariff_code}")],
            [InlineKeyboardButton(text="Другие методы (скоро)", callback_data="pay:other")],
            [InlineKeyboardButton(text="Назад", callback_data="buy_open")],
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Реферальная программа", callback_data="profile_ref")],
            [InlineKeyboardButton(text="Ввести промокод", callback_data="profile_promo")],
            [InlineKeyboardButton(text="Пополнить баланс", callback_data="buy_open")],
        ]
    )


def support_keyboard(support_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Написать в поддержку", url=support_url)],
        ]
    )
