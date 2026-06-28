# ZyberVPN

> Telegram-бот для автоматизированной продажи и управления VPN-подключениями на базе VLESS (Xray / 3x-ui). Поддерживает оплату через Telegram Stars, СБП (Platega) и криптовалюту, реферальную программу, пробный период и мультисерверную архитектуру.

---

## 📋 Возможности

| Категория | Функция |
|-----------|---------|
| **VPN** | Автоматическое создание VLESS-ключей (REALITY + WS+TLS) через 3x-ui API |
| **Серверы** | Мультисерверная архитектура с балансировкой нагрузки и health-check |
| **Пробный период** | Бесплатный триал 24 часа / 10 ГБ для новых пользователей |
| **Оплата** | Telegram Stars, СБП / QR (Platega), криптовалюта, внутренний баланс |
| **Тарифы** | 1/3/6 месяцев с настраиваемым трафиком |
| **Ключи** | До 5 активных ключей на пользователя, переключение primary, комментарии |
| **Подписка** | Персональная subscription-ссылка для каждого ключа |
| **Рефералы** | Бонус пригласившему (% от покупки) + бонус другу при первой покупке |
| **Промокоды** | Скидочные и временные (дни подписки) |
| **Админка** | Статистика, управление пользователями, уведомления о сбоях |
| **Безопасность** | Idempotency-ключи, rate limiting, шифрование credentials серверов |
| **Мониторинг** | PostHog аналитика, метрики /metrics, логирование трафика |

---

## 🏗 Архитектура

```
┌─────────────────┐
│  Telegram User  │
│   (Client App)  │
└────────┬────────┘
         │ VLESS / Subscription URL
         ▼
┌─────────────────┐     ┌─────────────────┐
│  Telegram Bot   │────▶│   FastAPI API   │
│  (aiogram 3.x)  │     │   (aiohttp)     │
└────────┬────────┘     └────────┬────────┘
         │                         │
         ▼                         ▼
┌─────────────────┐     ┌─────────────────┐
│   Supabase DB   │◀────│  VPN Manager    │
│  (PostgreSQL)   │     │  (XUIProvider)  │
└─────────────────┘     └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌─────────┐  ┌─────────┐  ┌─────────┐
              │ 3x-ui   │  │ 3x-ui   │  │ 3x-ui   │
              │ Server 1│  │ Server 2│  │ Server N│
              │ (NL)    │  │ (PL)    │  │ (DE)    │
              └─────────┘  └─────────┘  └─────────┘
                    │            │            │
                    └────────────┴────────────┘
                                 │
                                 ▼
                         ┌─────────────┐
                         │  Xray Core  │
                         │ (VLESS)     │
                         └─────────────┘
```

**Поток данных:**
1. Пользователь взаимодействует с Telegram-ботом
2. Бот обращается к FastAPI backend для subscription-ссылок и webhooks
3. Backend работает с Supabase (PostgreSQL) для хранения данных
4. VPN Manager через XUIProvider управляет 3x-ui панелями
5. 3x-ui настраивает Xray Core для маршрутизации VLESS-трафика

---

## 🛠 Стек технологий

| Технология | Назначение |
|------------|------------|
| **Python 3.11+** | Основной язык разработки |
| **aiogram 3.x** | Telegram Bot API |
| **aiohttp** | HTTP-сервер (FastAPI-like), webhook-эндпоинты |
| **Supabase (PostgreSQL)** | Основная база данных |
| **3x-ui / Xray Core** | VPN-серверная инфраструктура |
| **Platega API** | Платёжный шлюз (СБП, карты, крипта) |
| **python-dotenv** | Управление переменными окружения |
| **PostHog** | Продуктовая аналитика |
| **Docker** *(предполагается)* | Контейнеризация |

---

## 📁 Структура проекта

```
ZyberVPN/
├── app/
│   ├── api/                    # HTTP API эндпоинты
│   │   ├── subscription.py     # Subscription URL (/sub/{token})
│   │   └── platega_webhook.py  # Webhook от Platega
│   ├── bot/
│   │   ├── handlers/           # Обработчики Telegram
│   │   │   ├── start.py        # /start, главное меню
│   │   │   ├── connect.py      # Подключение VPN, инструкции
│   │   │   ├── purchase.py     # Покупка/продление подписки
│   │   │   ├── payments.py     # Обработка платежей Stars
│   │   │   ├── trial.py        # Активация пробного периода
│   │   │   ├── keys.py         # Управление ключами
│   │   │   ├── profile.py      # Профиль, баланс, рефералы
│   │   │   ├── admin.py        # Административные функции
│   │   │   ├── support.py      # Поддержка, юридические документы
│   │   │   ├── cron.py         # Фоновые задачи (health check)
│   │   │   └── watchdog.py     # Деактивация истёкших подписок
│   │   ├── keyboards/          # Inline и reply клавиатуры
│   │   └── states/             # FSM-состояния aiogram
│   ├── config.py               # Загрузка конфигурации из ENV
│   ├── db/
│   │   └── database.py         # Подключение к SQLite (fallback)
│   ├── repositories/           # Слой доступа к данным (Supabase)
│   │   ├── users.py            # Пользователи
│   │   ├── keys.py             # VPN-ключи
│   │   ├── payments.py         # Платежи
│   │   ├── subscriptions.py    # Подписки
│   │   ├── servers.py          # VPN-серверы
│   │   ├── user_vpn.py         # Связь user ↔ VPN
│   │   ├── vpn_devices.py      # Устройства пользователей
│   │   ├── promo.py            # Промокоды
│   │   └── idempotency.py      # Идемпотентность операций
│   ├── services/
│   │   ├── vpn/
│   │   │   ├── manager.py      # VPNManager (логика провижининга)
│   │   │   ├── xui_provider.py # Интеграция с 3x-ui API
│   │   │   └── base.py         # Абстракции VPNProvider
│   │   ├── access.py           # ensure_user_access (основной flow)
│   │   ├── subscription.py     # SubscriptionService (payload для клиентов)
│   │   ├── tariffs.py          # Тарифная сетка
│   │   ├── plans.py            # Планы подписок
│   │   ├── referrals.py        # Реферальная программа
│   │   ├── payments.py         # Генерация payload платежей
│   │   ├── idempotency.py      # Сервис идемпотентности
│   │   ├── platega.py          # Клиент Platega API
│   │   ├── platega_handler.py  # Обработка платежей Platega
│   │   ├── promo.py            # Валидация промокодов
│   │   └── provisioning_failures.py  # Уведомления при сбоях
│   └── utils/                  # Утилиты
│       ├── datetime.py         # Работа с датами/временем
│       ├── security.py          # Шифрование, хеширование
│       ├── admin_notify.py      # Уведомления админам
│       ├── analytics.py         # PostHog трекинг
│       └── tg.py                # Telegram-утилиты
├── migrations/
│   └── 2026_04_mvp_supabase.sql # SQL миграции для Supabase
├── requirements.txt
├── .env.example                # Пример переменных окружения
└── README.md
```

---

## 🚀 Установка

### 1. Клонирование репозитория

```bash
git clone https://github.com/artemtit/ZyberVPN.git
cd ZyberVPN
```

### 2. Создание виртуального окружения

```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
# или
venv\Scripts\activate   # Windows
```

### 3. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 4. Настройка переменных окружения

```bash
cp .env.example .env
# Отредактируйте .env файл
```

### 5. Настройка Supabase

Выполните SQL-скрипт миграций в Supabase SQL Editor:

```bash
# Содержимое файла migrations/2026_04_mvp_supabase.sql
```

Также необходимо создать RPC-функции (если не включены в миграции):
- `increment_user_balance`
- `deduct_user_balance_safe`
- `increment_user_traffic_limit`
- `increment_key_traffic_limit`
- `set_primary_key`
- `claim_user_vpn_creating`
- `update_server_health`

### 6. Запуск Backend (FastAPI/aiohttp)

```bash
python -m app.main
```

### 7. Запуск Bot (Telegram)

```bash
# Бот запускается вместе с backend через app/main.py
# Или отдельно:
python -m app.bot
```

### Docker

> **Примечание:** Dockerfile и docker-compose.yml присутствуют в репозитории, но их содержимое не было полностью проанализировано в рамках данного аудита.

```bash
docker-compose up -d
```

---

## ⚙ Переменные окружения

| Переменная | Обязательна | Описание |
|------------|-------------|----------|
| `BOT_TOKEN` | ✅ | Токен Telegram Bot от @BotFather |
| `BOT_USERNAME` | ❌ | Username бота (по умолчанию: `ZyberVPNBot`) |
| `ADMIN_IDS` | ✅ | Список Telegram ID администраторов через запятую |
| `PUBLIC_BASE_URL` | ✅ | Публичный URL бэкенда (для subscription-ссылок) |
| `SUPABASE_URL` | ✅ | URL проекта Supabase |
| `SUPABASE_SERVICE_KEY` | ✅ | Service Role Key Supabase |
| `XUI_BASE_URL` | ✅ | URL панели 3x-ui (рекомендуется localhost + туннель) |
| `XUI_USERNAME` | ✅ | Логин от панели 3x-ui |
| `XUI_PASSWORD` | ✅ | Пароль от панели 3x-ui |
| `XUI_INBOUND_ID` | ✅ | ID inbound в 3x-ui |
| `XUI_PUBLIC_HOST` | ✅ | Публичный хост VPN-сервера |
| `XUI_PUBLIC_PORT` | ❌ | Публичный порт (по умолчанию: 443) |
| `XUI_SNI` | ❌ | SNI для REALITY |
| `XUI_WS_PATH` | ❌ | Путь WebSocket (по умолчанию: `/ws`) |
| `VPN_LIMIT_IP` | ❌ | Лимит IP-адресов на клиента (по умолчанию: 3) |
| `VPN_TOTAL_GB` | ❌ | Лимит трафика в ГБ (по умолчанию: 0 = безлимит) |
| `VPN_DEFAULT_EXPIRY_DAYS` | ❌ | Срок действия по умолчанию (по умолчанию: 30) |
| `VPN_HEALTHCHECK_INTERVAL_SECONDS` | ❌ | Интервал health-check (по умолчанию: 120) |
| `VPN_CIRCUIT_BREAK_MINUTES` | ❌ | Время блокировки нездорового сервера (по умолчанию: 5) |
| `STARS_RATE` | ❌ | Курс Stars к рублю (по умолчанию: 1.69) |
| `REFERRAL_BONUS_PERCENT` | ❌ | Процент реферального бонуса (по умолчанию: 20) |
| `REFERRAL_FRIEND_BONUS_RUB` | ❌ | Бонус другу при первой покупке (по умолчанию: 0) |
| `PLATEGA_MERCHANT_ID` | ❌ | ID мерчанта Platega (для СБП/крипты) |
| `PLATEGA_API_KEY` | ❌ | API-ключ Platega |
| `PLATEGA_WEBHOOK_SECRET` | ❌ | Секрет для вебхуков Platega |
| `PLATEGA_CRYPTO_METHOD` | ❌ | Код метода оплаты криптой (0 = отключено) |
| `SUPPORT_URL` | ❌ | Ссылка на поддержку |
| `PRIVACY_POLICY_URL` | ❌ | URL политики конфиденциальности |
| `TERMS_URL` | ❌ | URL пользовательского соглашения |
| `POSTHOG_API_KEY` | ❌ | API-ключ PostHog для аналитики |
| `METRICS_TOKEN` | ❌ | Токен для доступа к `/metrics` |
| `TEST_MODE` | ❌ | Режим тестирования (`true`/`false`) |
| `DB_PATH` | ❌ | Путь к SQLite (fallback, по умолчанию: `./data/vpn_bot.sqlite3`) |
| `REDIS_URL` | ❌ | URL Redis (если используется) |
| `API_RATE_LIMIT_PER_MINUTE` | ❌ | Rate limit API (по умолчанию: 60) |
| `XRAY_PARSER_ENABLED` | ❌ | Парсер логов Xray (`true`/`false`) |
| `XRAY_PARSER_SERVER_ID` | ❌ | ID сервера для парсера |
| `XRAY_ACCESS_LOG_PATH` | ❌ | Путь к access.log Xray |
| `XRAY_DEVICE_WINDOW_HOURS` | ❌ | Окно подсчёта устройств (по умолчанию: 168) |

---

## 🔐 Конфигурация VPN

### Создание пользователя

1. Пользователь нажимает **«🔌 Подключиться»** или **«🎁 Пробный период»**
2. Система вызывает `ensure_user_access()` с `action="create"`
3. Предварительно создаётся запись в таблице `keys` (placeholder)
4. `VPNManager.create_user_access()` выбирает лучший сервер (по нагрузке)
5. `XUIProvider.create_client()` создаёт клиента в 3x-ui:
   - REALITY клиент (`flow=xtls-rprx-vision`)
   - WS+TLS клиент (если inbound поддерживает)
6. Конфигурации сохраняются в `user_vpn` со статусом `ready`
7. Генерируется `sub_token` для subscription-ссылки

### Формат ключей

```
vless://{uuid}@{host}:{port}?security=reality&encryption=none&pbk={public_key}&sid={short_id}&fp=chrome&type=tcp&flow=xtls-rprx-vision&sni={sni}#ZyberVPN-{country}-REALITY-{user_id}

vless://{uuid}@{host}:443?security=tls&encryption=none&fp=chrome&type=ws&host={host}&path={ws_path}&sni={sni}#ZyberVPN-{country}-WS-{user_id}
```

### Взаимодействие с X-UI / 3x-ui

- **API:** REST-эндпоинты 3x-ui (`/panel/api/inbounds/*`)
- **Аутентификация:** Cookie-based login + CSRF-токен
- **Операции:** `addClient`, `updateClient`, `delClient`, `resetClientTraffic`, `getClientTraffics`, `onlines`
- **Безопасность:** HTTP запрещён для внешних хостов (только localhost/127.0.0.1)

### Subscription URL

```
{PUBLIC_BASE_URL}/sub/{sub_token}
```

Возвращает JSON-список серверов для импорта в V2Ray, Sing-box, Happ и др.

---

## 🔌 API

### Subscription

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| `GET` | `/sub/{token}` | Получить список конфигов по токену | Токен в URL |

**Пример ответа:**
```json
{
  "remarks": "ZyberVPN",
  "upload": 0,
  "download": 0,
  "total": 0,
  "expire": 1750000000,
  "servers": [
    "vless://...#🇳🇱ZyberVPN | Нидерланды",
    "vless://...#🇵🇱ZyberVPN | Польша"
  ]
}
```

### Platega Webhook

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| `POST` | `/platega/webhook?secret={token}` | Callback от платёжной системы | `X-Webhook-Secret` или query param |

**Тело запроса:**
```json
{
  "id": "transaction-id",
  "amount": 970.0,
  "currency": "RUB",
  "status": "CONFIRMED",
  "paymentMethod": 2
}
```

### Metrics

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| `GET` | `/metrics` | Метрики сервиса (активные серверы, пользователи) | `METRICS_TOKEN` |

---

## 🗄 Работа с базой данных

### Таблицы

| Таблица | Назначение | Ключевые поля |
|---------|------------|---------------|
| `users` | Пользователи бота | `tg_id`, `balance`, `expires_at`, `is_active`, `plan`, `ref_tg_id`, `traffic_limit_gb` |
| `keys` | VPN-ключи пользователей | `tg_id`, `key` (vless://), `is_primary`, `expires_at`, `traffic_limit_gb`, `sub_token` |
| `user_vpn` | Связь пользователя с сервером | `user_id`, `server_id`, `reality_uuid`, `ws_uuid`, `reality_config`, `ws_config`, `key_id`, `status` |
| `servers` | VPN-серверы (3x-ui панели) | `host`, `api_url`, `username`, `password`, `inbound_id`, `country`, `is_active`, `health_errors` |
| `payments` | История платежей | `tg_id`, `amount`, `status`, `tariff_code`, `payload`, `idempotency_key`, `purchase_type` |
| `subscriptions` | Подписки пользователей | `tg_id`, `expires_at`, `status` |
| `idempotency_keys` | Идемпотентность операций | `operation`, `idempotency_key`, `status`, `response_payload` |

### Связи

```
users (1) ───► (N) keys
users (1) ───► (N) payments
users (1) ───► (N) subscriptions
users (1) ───► (N) user_vpn ───► (1) servers
```

### Важные особенности

- **Мультисерверность:** Один ключ может быть provisioned на нескольких серверах (primary + secondary)
- **Per-key expiry:** Каждый ключ имеет независимый срок действия
- **Idempotency:** Все платежи и провижининг защищены от дублирования
- **Atomic operations:** Используются RPC для атомарного изменения баланса и лимитов трафика

---

## 🤖 Telegram Bot

### Команды

| Команда | Описание | Доступ |
|---------|----------|--------|
| `/start` | Главное меню, регистрация | Все |
| `/buy` | Открыть меню покупки подписки | Все |
| `/admin` | Административная панель | Только `ADMIN_IDS` |

### Главное меню (Inline Keyboard)

- **🔌 Подключиться** — Просмотр ключей и инструкций
- **🎁 Пробный период** — Активация 24-часового триала
- **💳 Купить подписку** — Выбор тарифа и оплата
- **🔑 Мои ключи** — Управление ключами (primary, комментарии, QR)
- **👤 Профиль** — Баланс, подписка, рефералы, пополнение
- **📄 Правовая информация** — Политика конфиденциальности

### Сценарий пользователя

1. **Новый пользователь:**
   - Нажимает `/start` → видит главное меню
   - Выбирает «🎁 Пробный период» → получает ключ на 24 часа / 10 ГБ
   - Получает инструкцию по подключению для своего устройства

2. **Покупка подписки:**
   - «💳 Купить подписку» → выбор тарифа (1/3/6 мес.)
   - Выбор способа оплаты: Stars / СБП / Крипта / Баланс
   - После оплаты автоматически создаётся/продлевается ключ

3. **Управление ключами:**
   - «🔑 Мои ключи» → список всех ключей
   - Каждый ключ: статус, срок, трафик, устройства онлайн
   - Возможность сделать ключ primary, добавить комментарий

### Административные функции

| Функция | Описание |
|---------|----------|
| Статистика | Количество пользователей, активных, новых за 24ч/7д |
| Выручка | Общая сумма, Stars, уникальные плательщики |
| Последние пользователи | Список последних 15 регистраций |
| Управление серверами | Health-check, блокировка/разблокировка |
| Уведомления | Оповещения о сбоях серверов, застрявших платежах |

---

## 💳 Система оплаты

### Способы оплаты

| Способ | Провайдер | Валюта | Статус |
|--------|-----------|--------|--------|
| Telegram Stars | Telegram Payments | XTR (Stars) | ✅ Основной |
| СБП / QR | Platega | RUB | ✅ Опционально |
| Криптовалюта | Platega | RUB | ✅ Опционально |
| Внутренний баланс | — | RUB | ✅ Доступен |
| Тестовая оплата (SBP) | — | RUB | ⚠️ Только для админов |

### Процесс оплаты (Stars)

1. Пользователь выбирает тариф → нажимает «⭐ Оплатить Stars»
2. Бот отправляет `answer_invoice` с `currency="XTR"`
3. Telegram показывает диалог оплаты
4. После успешной оплаты приходит `successful_payment`
5. Обработчик `process_successful_payment()`:
   - Проверяет idempotency
   - Обновляет подписку (`users.expires_at`, `subscriptions`)
   - Добавляет трафик (`traffic_limit_gb`)
   - Создаёт/продлевает VPN-ключ в XUI
   - Начисляет реферальный бонус

### Процесс оплаты (Platega)

1. Пользователь выбирает «💳 СБП / QR» или «₿ Криптовалюта»
2. Создаётся платёж через `PlategaClient.create_payment()`
3. Пользователь перенаправляется на `redirect_url`
4. После оплаты Platega отправляет webhook на `/platega/webhook`
5. Вебхук верифицирует статус через API Platega
6. Активируется подписка аналогично Stars

### Пополнение баланса

- Доступно через «👤 Профиль → 💰 Пополнить баланс»
- Минимум: 50 ₽, максимум: 10 000 ₽
- Оплата через Stars или Platega
- Баланс используется при покупке подписок

---

## 🤝 Реферальная система

### Механика

1. **Реферальная ссылка:** `https://t.me/{bot_username}?start=ref_{tg_id}`
2. **Пригласивший:** Получает `REFERRAL_BONUS_PERCENT`% от суммы каждой покупки реферала
3. **Друг:** Получает `REFERRAL_FRIEND_BONUS_RUB` ₽ на баланс при первой покупке

### Пример

- Бонус пригласившему: 20%
- Бонус другу: 50 ₽
- Реферал купил подписку на 189 ₽
- Пригласивший получает: 38 ₽ на баланс
- Друг получает: 50 ₽ на баланс

### Ограничения

- Саморефералы запрещены (проверка `inviter_tg_id != buyer_tg_id`)
- Бонус начисляется только за оплаченные подписки (не topup)

---

## 🔒 Безопасность

| Аспект | Реализация |
|--------|------------|
| **Авторизация** | Telegram ID как primary key; проверка `admin_ids` |
| **Хранение секретов** | Credentials серверов шифруются (`encrypt_credential`) |
| **ENV** | Все секреты через переменные окружения, `.env` в `.gitignore` |
| **Idempotency** | Каждая платёжная операция защищена уникальным ключом |
| **Rate limiting** | `vpn_create_rate_limit_seconds`, `api_rate_limit_per_minute` |
| **Webhook security** | Секретный токен в `X-Webhook-Secret` + IP whitelist |
| **XUI security** | HTTP запрещён для внешних хостов; только localhost/tunnel |
| **SQL Injection** | Используется Supabase SDK (параметризованные запросы) |
| **CSRF** | CSRF-токен при логине в 3x-ui v3+ |

### Возможные риски

- **Platega webhook:** Нет HMAC-подписи, проверка через callback к API
- **XUI credentials:** Хранятся в Supabase (шифрованные), но ключ шифрования в ENV
- **SQLite fallback:** Используется только как fallback, основные данные в Supabase

---

## 📅 Roadmap

### Реализовано

- [x] Мультисерверная архитектура с health-check
- [x] Поддержка REALITY и WS+TLS
- [x] Пробный период 24 часа
- [x] Оплата Stars, СБП, криптовалютой
- [x] Реферальная программа
- [x] Промокоды (скидки + дни)
- [x] Per-key subscription URLs
- [x] Автоматическая деактивация истёкших подписок
- [x] Ограничение трафика с блокировкой
- [x] Административная панель

### Предлагаемые улучшения

- [ ] **Multi-region deployment:** Разделение bot и API на разные инстансы
- [ ] **Webhook mode for bot:** Переход с polling на webhook для production
- [ ] **Database migrations tool:** Alembic или аналог для управления миграциями
- [ ] **Comprehensive tests:** Unit и integration тесты (pytest)
- [ ] **CI/CD pipeline:** GitHub Actions для lint, test, deploy
- [ ] **Docker optimization:** Multi-stage build, healthcheck в Dockerfile
- [ ] **Backup strategy:** Резервное копирование Supabase + SQLite fallback
- [ ] **Rate limiting per user:** Redis-based rate limiting
- [ ] **Audit log:** Логирование всех административных действий
- [ ] **i18n:** Мультиязычная поддержка

---

## 📄 License

Лицензия отсутствует. Проект распространяется как есть (all rights reserved).

---

## 🤝 Contributing

Мы приветствуем вклад в развитие проекта!

1. **Fork** репозитория
2. Создайте **feature branch**: `git checkout -b feature/amazing-feature`
3. Сделайте **commit**: `git commit -m 'Add amazing feature'`
4. **Push** в ветку: `git push origin feature/amazing-feature`
5. Откройте **Pull Request**

### Требования к PR

- Код соответствует PEP 8
- Добавлены type hints
- Логирование на русском/английском
- Не ломает существующую функциональность
