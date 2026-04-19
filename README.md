# ZyberVPN MVP

Python-проект VPN-сервиса: Telegram-бот + backend + Supabase + 3x-ui API.

## Что реализовано в MVP

- Абстракция VPN-провайдера (`VPNProvider`)
- Реализация `XUIProvider` для 3x-ui
- Multi-server через таблицу `servers`
- Балансировка по минимальному числу пользователей на сервере
- Связь `user -> server -> uuid` через таблицу `user_vpn`
- Subscription endpoint:
  - `GET /sub/{token}` (legacy + multi-config payload)
  - `GET /subscription/{user_id}` (новый endpoint)
- Генерация VLESS REALITY c обязательными параметрами:
  - `pbk`, `sid`, `fp=chrome`, `flow=xtls-rprx-vision`
- Fallback протокол:
  - `VLESS + WS + TLS`
- Лимиты при создании клиента:
  - `limitIp`, `expiryTime`, `totalGB`
- Периодический health-check серверов и автоматическое отключение нерабочих
- В боте команда подключения отдает subscription URL и набор конфигов

## Обновленная структура проекта

```text
app/
  api/
    subscription.py
  bot/
    handlers/
      connect.py
      keys.py
      payments.py
      profile.py
      purchase.py
      start.py
      support.py
  db/
    database.py
  repositories/
    keys.py
    payments.py
    promo.py
    servers.py
    subscriptions.py
    user_vpn.py
    users.py
  services/
    access.py
    supabase.py
    vpn/
      __init__.py
      base.py
      manager.py
      xui_provider.py
  main.py
  config.py
migrations/
  2026_04_mvp_supabase.sql
```

## ENV

```env
BOT_TOKEN=
DB_PATH=./data/vpn_bot.sqlite3
SUPPORT_URL=https://t.me/your_support
REFERRAL_BONUS_PERCENT=10

SUPABASE_URL=
SUPABASE_SERVICE_KEY=

PUBLIC_BASE_URL=https://your-domain

XUI_BASE_URL=http://xui-host:54321
XUI_USERNAME=
XUI_PASSWORD=
XUI_INBOUND_ID=1
XUI_PUBLIC_HOST=your-vpn-host
XUI_PUBLIC_PORT=443
XUI_SNI=www.cloudflare.com
XUI_WS_PATH=/ws

VPN_LIMIT_IP=1
VPN_TOTAL_GB=50
VPN_DEFAULT_EXPIRY_DAYS=30
VPN_HEALTHCHECK_INTERVAL_SECONDS=120
```

## Запуск

1. Установить зависимости:
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
```

2. Применить SQL в Supabase:
- `[migrations/2026_04_mvp_supabase.sql](/c:/Users/titiv/OneDrive/Desktop/ZyberVPN/ZyberVPN/migrations/2026_04_mvp_supabase.sql)`

3. Запустить приложение:
```bash
python -m app.main
```

## Важно

- Не храните реальные секреты в репозитории.
- Если секреты уже попали в `.env`/git-историю, сразу ротируйте:
  - `BOT_TOKEN`
  - `SUPABASE_SERVICE_KEY`
  - `XUI_USERNAME/XUI_PASSWORD`
