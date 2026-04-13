# ZyberVPN Telegram Bot

Полностью рабочий Telegram VPN-бот с монетизацией:
- Python 3.11+
- aiogram 3
- SQLite
- Чистая модульная архитектура (handlers/repositories/services)
- Docker + docker-compose

## Функции

- Главное меню кнопками: `🔑 Мои ключи`, `👤 Личный кабинет`, `🆘 Поддержка`
- Ключи: список, статус, срок, подключение, QR, продление
- Покупка подписки (1/3/6 месяцев) через Telegram Stars
- Email чека (опционально) + `Продолжить без email`
- После оплаты: создание VPN-ключа (заглушка), сохранение в БД, выдача ссылки и QR
- Личный кабинет: ID, статус подписки, срок, баланс
- Реферальная система: deeplink, учёт приглашённых, начисление %

## Структура проекта

```text
app/
  main.py
  config.py
  bot/
    handlers/
      start.py
      keys.py
      purchase.py
      payments.py
      profile.py
      support.py
    keyboards/
      reply.py
      inline.py
    states/
      purchase.py
  db/
    database.py
  repositories/
    users.py
    subscriptions.py
    keys.py
    payments.py
  services/
    vpn.py
    tariffs.py
    payments.py
    referrals.py
  utils/
    datetime.py
requirements.txt
Dockerfile
docker-compose.yml
.env.example
```

## База данных (SQLite)

Создаётся автоматически при старте:
- `users (id, tg_id, balance, ref_id, created_at)`
- `subscriptions (id, user_id, expires_at, status, created_at)`
- `keys (id, user_id, key, created_at)`
- `payments (id, user_id, amount, status, tariff_code, email, payload, telegram_payment_charge_id, created_at)`

## Запуск локально

1. Создать `.env`:
```env
BOT_TOKEN=123456:your_bot_token
DB_PATH=./data/vpn_bot.sqlite3
SUPPORT_URL=https://t.me/your_support
REFERRAL_BONUS_PERCENT=10
```

2. Установить зависимости:
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate
pip install -r requirements.txt
```

3. Запуск:
```bash
python -m app.main
```

## Запуск в Docker

```bash
docker compose up --build -d
```

## Важно по оплате Stars

- Используется `currency="XTR"` и `provider_token=""` (официальный сценарий Telegram Stars).
- Для тестов убедитесь, что бот и аккаунт поддерживают Stars-платежи.
# ZyberVPN
