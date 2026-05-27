from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.plans import get_all_plans


def access_activated_text(access: str, traffic: str, sub_url: str) -> str:
    return (
        "✅ <b>Доступ активирован</b>\n\n"
        "Ваш VPN уже готов.\n\n"
        f"⏳ Доступ: <b>{escape(access)}</b>\n"
        f"📊 Лимит: <b>{escape(traffic)}</b>\n\n"
        "🔗 Ваша ссылка создана и готова к использованию.\n\n"
        "Сейчас подробно покажем, как всё настроить — обычно это занимает меньше минуты.\n\n"
        "Для ручного подключения:\n\n"
        f"<code>{escape(sub_url)}</code>"
    )


def payment_success_keyboard(sub_url: str, key_id: int = 0) -> InlineKeyboardMarkup:
    instruction_button = (
        InlineKeyboardButton(text="📘 Инструкция", callback_data=f"key_connect:{key_id}", style="primary")
        if key_id
        else InlineKeyboardButton(text="📘 Инструкция", url=sub_url, style="primary")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [instruction_button],
            [InlineKeyboardButton(text="Скопировать ссылку", copy_text={"text": sub_url})],
        ]
    )


def renewal_success_keyboard(key_id: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if key_id:
        rows.append([InlineKeyboardButton(text="📲 Подключить", callback_data=f"key_connect:{key_id}", style="primary")])
    rows.append([InlineKeyboardButton(text="🔑 Мои ключи", callback_data="menu_keys")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard(support_url: str, show_trial: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if show_trial:
        rows.append([InlineKeyboardButton(text="🎁 Попробовать бесплатно (1 день)", callback_data="trial_start", style="danger")])
    rows.extend([
        [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_open", style="danger")],
        [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="menu_keys", style="primary")],
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="menu_profile", style="success")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=support_url)],
        [InlineKeyboardButton(text="📄 Документы", callback_data="legal_docs")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trial_expired_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1 месяц — 69 ₽", callback_data="tariff:m1", style="success")],
            [InlineKeyboardButton(text="3 месяца — 189 ₽  (−9%)", callback_data="tariff:m3", style="success")],
            [InlineKeyboardButton(text="6 месяцев — 349 ₽  (−16%)", callback_data="tariff:m6", style="success")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")],
        ]
    )


def legal_keyboard(privacy_policy_url: str, terms_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=privacy_policy_url)],
            [InlineKeyboardButton(text="📋 Пользовательское соглашение", url=terms_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu", style="danger")],
        ]
    )


def keys_list_keyboard(
    key_rows: list[tuple[str, str]],
    expired_trial_rows: list[tuple[str, str]] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for text, key_id in key_rows:
        rows.append([InlineKeyboardButton(text=text, callback_data=f"key_open:{key_id}")])
    for text, _ in (expired_trial_rows or []):
        rows.append([InlineKeyboardButton(text=text, callback_data="buy_open")])
    rows.append([InlineKeyboardButton(text="🛒 Купить ключ", callback_data="buy_open", style="primary")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_card_keyboard(
    key_id: int,
    is_primary: bool = False,
    has_comment: bool = False,
    is_trial: bool = False,
) -> InlineKeyboardMarkup:
    renew_btn = (
        InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_open", style="success")
        if is_trial
        else InlineKeyboardButton(text="➕ Продлить этот ключ", callback_data=f"key_renew:{key_id}", style="success")
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📲 Подключиться", callback_data=f"key_connect:{key_id}", style="primary")],
        [renew_btn],
        [InlineKeyboardButton(text="📱 Показать QR-код", callback_data=f"key_qr:{key_id}")],
        [InlineKeyboardButton(text="📝 Комментарии", callback_data=f"key_comment:{key_id}")],
    ]
    if has_comment:
        rows.append([InlineKeyboardButton(text="🗑 Удалить комментарий", callback_data=f"key_comment_delete:{key_id}")])
    if is_primary:
        rows.append([InlineKeyboardButton(text="⭐ (Основной ключ)", callback_data="noop")])
    else:
        rows.append([InlineKeyboardButton(text="⭐ Сделать основным", callback_data=f"key_set_primary:{key_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к списку ключей", callback_data="menu_keys", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariffs_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    base_monthly_price = 69  # m1 price — reference for discount calc
    for plan in get_all_plans(include_admin=is_admin):
        if plan.get("admin_only"):
            label = f"🔧 {plan['name']} — {plan['price_rub']} ₽"
        else:
            months = max(1, plan["duration_days"] // 30)
            per_month = plan["price_rub"] / months
            discount = round((1 - per_month / base_monthly_price) * 100)
            suffix = f"  (−{discount}%)" if discount > 0 else ""
            label = f"{plan['name']} — {plan['price_rub']} ₽{suffix}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy_plan:{plan['id']}", style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)



def payment_keyboard(
    platega_enabled: bool = False,
    platega_crypto_enabled: bool = False,
    show_test_pay: bool = False,
    balance: int = 0,
    price_rub: int = 0,
    admin_plan: bool = False,
    discount_percent: int = 0,
) -> InlineKeyboardMarkup:
    rows = []
    if admin_plan:
        # Admin-only plan: only Platega, no Stars/balance
        if platega_enabled:
            rows.append([InlineKeyboardButton(text="💳 СБП / QR (Platega)", callback_data="pay:platega")])
        if platega_crypto_enabled:
            rows.append([InlineKeyboardButton(text="🪙 Криптовалюта (Platega)", callback_data="pay:platega_crypto")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад к тарифам", callback_data="buy_open", style="danger")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if balance > 0:
        if price_rub > 0 and balance >= price_rub:
            label = f"💰 Оплатить с баланса (бесплатно, −{price_rub} руб.)"
        elif price_rub > 0:
            label = f"💰 Баланс: −{balance} руб. (доплатить {price_rub - balance} руб.)"
        else:
            label = f"💰 Оплатить с баланса ({balance} руб.)"
        rows.append([InlineKeyboardButton(text=label, callback_data="pay:balance", style="success")])
    if platega_enabled:
        rows.append([InlineKeyboardButton(text="💳 СБП / QR (Platega)", callback_data="pay:platega", style="success")])
    if platega_crypto_enabled:
        rows.append([InlineKeyboardButton(text="🪙 Криптовалюта (Platega)", callback_data="pay:platega_crypto", style="success")])
    rows.append([InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay:stars", style="success")])
    if discount_percent > 0:
        rows.append([InlineKeyboardButton(text=f"✅ Промокод −{discount_percent}% применён | Сменить", callback_data="pay:promo")])
    else:
        rows.append([InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="pay:promo", style="primary")])
    if show_test_pay:
        rows.append([InlineKeyboardButton(text="🧪 Тестовая оплата [Admin]", callback_data="pay:sbp")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к тарифам", callback_data="buy_open", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_back_keyboard(redirect_url: str, tariff_code: str = "", purchase_type: str = "new", renew_key_id: str = "0") -> InlineKeyboardMarkup:
    # Encode context in callback_data so back button works even after state.clear().
    back_data = f"payment_back:{tariff_code}:{purchase_type}:{renew_key_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=redirect_url)],
        [InlineKeyboardButton(text="⬅️ Вернуться к способам оплаты", callback_data=back_data, style="danger")],
    ])


def topup_payment_keyboard(platega_enabled: bool = False, platega_crypto_enabled: bool = False) -> InlineKeyboardMarkup:
    rows = []
    rows.append([InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="topup_pay:stars")])
    if platega_enabled:
        rows.append([InlineKeyboardButton(text="💳 СБП / QR (Platega)", callback_data="topup_pay:platega")])
    if platega_crypto_enabled:
        rows.append([InlineKeyboardButton(text="🪙 Криптовалюта (Platega)", callback_data="topup_pay:platega_crypto")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="profile_topup", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topup_stars_keyboard(rub_amount: int, stars: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Оплатить {stars} Stars", pay=True)],
        [InlineKeyboardButton(text="⬅️ Вернуться к способам оплаты", callback_data=f"topup_method_back:{rub_amount}", style="danger")],
    ])


def topup_platega_keyboard(redirect_url: str, rub_amount: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=redirect_url)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"topup_method_back:{rub_amount}", style="danger")],
    ])


def stars_back_keyboard(tariff_code: str = "", purchase_type: str = "new", renew_key_id: str = "0", stars: int = 0) -> InlineKeyboardMarkup:
    back_data = f"payment_back:{tariff_code}:{purchase_type}:{renew_key_id}"
    pay_text = f"⭐ Оплатить {stars} Stars" if stars else "⭐ Оплатить Stars"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=pay_text, pay=True)],
        [InlineKeyboardButton(text="⬅️ Вернуться к способам оплаты", callback_data=back_data, style="danger")],
    ])


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤝 Реферальная программа", callback_data="profile_ref", style="success")],
            [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="profile_promo", style="primary")],
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="profile_topup")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_menu", style="danger")],
        ]
    )


def subscription_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile", style="danger")],
        ]
    )


def topup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="100 ₽", callback_data="topup_rub:100"),
                InlineKeyboardButton(text="300 ₽", callback_data="topup_rub:300"),
                InlineKeyboardButton(text="500 ₽", callback_data="topup_rub:500"),
            ],
            [
                InlineKeyboardButton(text="1 000 ₽", callback_data="topup_rub:1000"),
                InlineKeyboardButton(text="3 000 ₽", callback_data="topup_rub:3000"),
                InlineKeyboardButton(text="5 000 ₽", callback_data="topup_rub:5000"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile", style="danger")],
        ]
    )


def promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile", style="danger")],
        ]
    )


def promo_apply_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Продлить активную подписку", callback_data="promo_apply:active", style="success")],
            [InlineKeyboardButton(text="🆕 Активировать и показать подключение", callback_data="promo_apply:new", style="success")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_profile", style="danger")],
        ]
    )


def referral_keyboard(ref_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться в Telegram", url=ref_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_profile", style="danger")],
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
            [InlineKeyboardButton(text="⬅️ Назад к ключу", callback_data="menu_keys", style="danger")],
        ]
    )


def connect_apps_keyboard(apps: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=app_name, callback_data=app_callback)] for app_name, app_callback in apps]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="connect_back_devices", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_DEEPLINK: dict[str, tuple[str, str]] = {
    "app_happ":         ("happ://add/{url}", "Happ"),
    "app_happ_mac":     ("happ://add/{url}", "Happ"),
    "app_happ_linux":   ("happ://add/{url}", "Happ"),
    "app_happ_appletv": ("happ://add/{url}", "Happ"),
    "app_happ_win":     ("happ://add/{url}", "Happ"),
    "app_v2raytun":     ("v2rayng://install-config?name=ZyberVPN&url={url}", "V2RayTun"),
    "app_v2rayng":      ("v2rayng://install-config?name=ZyberVPN&url={url}", "v2rayNG"),
}


def connect_result_keyboard(
    app_callback: str = "",
    sub_url: str = "",
    show_connected_btn: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if app_callback and sub_url and app_callback in _DEEPLINK:
        tmpl, label = _DEEPLINK[app_callback]
        rows.append([InlineKeyboardButton(
            text=f"📲 Добавить в {label}",
            url=tmpl.format(url=sub_url),
            style="primary",
        )])
    if show_connected_btn:
        rows.append([InlineKeyboardButton(
            text="✅ Я подключился!",
            callback_data="connect_confirmed",
            style="success",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к устройствам", callback_data="connect_back_devices", style="success")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
