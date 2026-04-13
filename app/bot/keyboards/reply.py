from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔑 Мои ключи")],
            [KeyboardButton(text="👤 Личный кабинет")],
            [KeyboardButton(text="🆘 Поддержка")],
        ],
        resize_keyboard=True,
    )
