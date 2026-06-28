# Pingly

> Единая система для репетиторов и учеников: Telegram-бот + веб-кабинет на одном backend.

Pingly — это CRM-платформа для частных репетиторов и учеников. Она автоматизирует рутину: напоминания о занятиях, управление расписанием, домашние задания, финансовый учёт и аналитику. Репетитор работает в веб-кабинете, ученик получает уведомления в Telegram или VK — всё работает из одного backend-процесса.

---

## ✨ Возможности

### Для репетитора

| Возможность | Описание |
|-------------|----------|
| 📋 CRM учеников | Добавление, редактирование, поиск, удаление учеников; заметки по каждому ученику |
| 📅 Расписание и календарь | Создание разовых и регулярных занятий (ежедневно, еженедельно, несколько раз в неделю, каждые N дней/недель); календарь на день, неделю, месяц |
| 🔔 Автоматические напоминания | Уведомления ученику за 2 часа до занятия; нудж репетитору за 1 час, если ученик не подтвердил |
| ✅ Подтверждение / отмена занятий | Ученик нажимает «Буду» или «Отменяю» — репетитор получает уведомление в Telegram |
| 📝 Домашние задания | Создание, шаблоны, статусы (новое → в работе → сдано → проверено), уведомления ученику |
| 💰 Финансы | Учёт стоимости занятия, статус оплаты, сводка по заработку за месяц и долги |
| 📦 Абонементы | Пакетные занятия с автоматическим подсчётом оставшихся; уведомления при исчерпании |
| 🌐 Публичная страница | Персональная страница репетитора с формой записи; бейджи, темы оформления |
| 📊 Аналитика | Статистика: посещаемость, выполнение домашних заданий, активность учеников |
| 📨 Заявки от учеников | Входящие заявки через публичную страницу с мгновенным уведомлением в Telegram |
| 🛡 Админ-панель | Обзор платформы, список репетиторов, массовая рассылка, управление подписками |

### Для ученика

| Возможность | Описание |
|-------------|----------|
| 📚 Следующее занятие | Быстрый просмотр ближайшего занятия |
| 📝 Мои задания | Список домашних заданий с возможностью отметить «в работе» или «сдано» |
| 📈 История занятий | Прошедшие занятия, статусы, посещаемость |
| ✅ Подтверждение занятий | Кнопки «Буду» / «Отменяю» прямо в Telegram/VK |
| 💬 Уведомления | Напоминания о занятиях, отменах, переносах, новых заданиях |

---

## 🎯 Для кого создан Pingly

| Аудитория | Почему подходит |
|-----------|-----------------|
| **Репетиторы** | Централизованное управление учениками, расписанием, финансами и коммуникациями в одном месте |
| **Ученики** | Автоматические напоминания, удобное подтверждение занятий, доступ к заданиям |
| **Администраторы платформы** | Полный обзор пользователей, платежей, массовые рассылки через Telegram |

---

## 🏗 Архитектура

```text
┌─────────────────────────────────────────────────────────────┐
│                        Пользователь                         │
│              (Telegram / VK / Веб-браузер)                  │
└──────────────────────┬──────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌──────────┐ ┌───────────────┐
│ Telegram Bot │ │ VK Bot   │ │ FastAPI Web   │
│  handlers/   │ │ vk_bot.py│ │   web/app.py  │
└──────┬───────┘ └────┬─────┘ └───────┬───────┘
       │              │               │
       └──────────────┴───────────────┘
                       │
                       ▼
         ┌─────────────────────────────┐
         │      Application Layer      │
         │  application/services/      │
         │  • accounts, students       │
         │  • lessons, homework        │
         │  • notifications, billing     │
         │  • web_auth, admin, public  │
         └─────────────┬───────────────┘
                       │
         ┌─────────────┴───────────────┐
         │        Domain Layer         │
         │      domain/models.py       │
         │  UserRole, LessonStatus,    │
         │  HomeworkStatus, etc.       │
         └─────────────┬───────────────┘
                       │
         ┌─────────────┴───────────────┐
         │    Infrastructure Layer     │
         │  • Supabase Repository        │
         │  • Email (Resend)            │
         │  • Captcha (Cloudflare)      │
         │  • Payments (Platega)        │
         └─────────────┬───────────────┘
                       │
                       ▼
              ┌───────────────┐
              │   Supabase    │
              │  (PostgreSQL) │
              └───────────────┘
```

### Поток данных

1. **Репетитор** регистрируется через веб-форму (email + пароль) или Telegram Login Widget.
2. **Backend** (FastAPI) обрабатывает запросы через `application/services`.
3. **Supabase** хранит все данные: пользователи, занятия, домашние задания, уведомления, платежи.
4. **Scheduler** (APScheduler) каждую минуту проверяет и отправляет уведомления через Telegram и VK.
5. **Telegram/VK боты** доставляют напоминания и собирают ответы учеников.

---

## 🛠 Стек технологий

| Технология | Назначение |
|------------|------------|
| **Python 3.11+** | Backend |
| **FastAPI** | Веб-сервер, API, SSR-шаблоны (Jinja2) |
| **aiogram 3.x** | Telegram Bot |
| **aiohttp** | VK Bot (Long Poll API напрямую) |
| **APScheduler** | Планировщик уведомлений |
| **Supabase (PostgreSQL)** | База данных |
| **itsdangerous** | Подписанные cookies, токены входа |
| **PBKDF2-HMAC-SHA256** | Хеширование паролей |
| **Resend** | Email-уведомления (коды подтверждения) |
| **Cloudflare Turnstile** | CAPTCHA на регистрации |
| **Platega** | Платёжный шлюз (подписки) |
| **Jinja2** | SSR-шаблоны веб-кабинета |
| **Vanilla JS + CSS** | Frontend (без фреймворков) |

---

## 📂 Структура проекта

```text
Pingly/
├── bot.py                          # Точка входа: бот + веб + scheduler
├── config.py                       # Переменные окружения
├── db.py                           # Инициализация Supabase клиента
├── scheduler.py                    # APScheduler: доставка уведомлений, подписки
├── vk_bot.py                       # VK-бот (Long Poll)
├── requirements.txt                # Зависимости Python
├── supabase_schema.sql             # MVP-схема БД
├── design-decisions.md             # Архитектурные решения
├── design-tokens.css               # Дизайн-система
│
├── application/                    # Слой бизнес-логики
│   ├── __init__.py
│   ├── factory.py                  # Фабрика сервисов (Services)
│   ├── repositories.py           # Протокол репозитория (PinglyRepository)
│   ├── services/                   # Сервисы бизнес-логики
│   │   ├── accounts.py           # Управление аккаунтами, подписки, рефералы
│   │   ├── students.py           # CRUD учеников, приглашения
│   │   ├── lessons.py            # Расписание, занятия, абонементы, финансы
│   │   ├── homework.py           # Домашние задания, шаблоны
│   │   ├── notifications.py      # Планирование уведомлений
│   │   ├── web_auth.py           # Аутентификация (email, Telegram Widget)
│   │   ├── billing.py            # Интеграция с Platega
│   │   ├── public.py             # Публичные страницы репетиторов
│   │   ├── admin.py              # Админ-панель, аналитика
│   │   └── analytics.py          # Статистика (обёртка)
│   └── use_cases/                # Use cases (пока минимальный слой)
│       └── __init__.py
│
├── domain/                         # Domain Layer
│   ├── __init__.py
│   └── models.py                   # Enum'ы и dataclass'ы (UserRole, LessonStatus, etc.)
│
├── infrastructure/                 # Infrastructure Layer
│   ├── __init__.py
│   ├── supabase_repository.py    # Реализация репозитория Supabase
│   ├── email.py                  # Отправка email через Resend
│   ├── captcha.py                # Проверка Cloudflare Turnstile
│   └── platega.py                # Клиент Platega API
│
├── handlers/                       # Telegram Bot Handlers
│   ├── __init__.py
│   ├── tutor.py                    # Команды репетитора (/start, /help, /web)
│   └── student.py                  # Callback'и ученика (Буду/Отменяю, причина отмены)
│
├── web/                            # FastAPI Web Application
│   ├── __init__.py
│   ├── app.py                      # Все маршруты, middleware, rate limiting
│   ├── calendar_view.py            # Построение календаря (день/неделя/месяц)
│   ├── static/                     # CSS, JS, SVG-логотипы, email-изображения
│   │   ├── app.css               # Стили веб-кабинета (~90 KB)
│   │   ├── app.js                # Интерактивность кабинета
│   │   ├── landing.css           # Стили лендинга
│   │   ├── landing.js            # Интерактивность лендинга
│   │   ├── tour.css / tour.js    # Обучающий тур
│   │   └── logo*.svg, logo-email.png
│   └── templates/                  # Jinja2-шаблоны
│       ├── base.html
│       ├── layout.html
│       ├── landing.html
│       ├── login.html / register.html / verify.html
│       ├── tutor.html / student.html / students.html / student_card.html
│       ├── calendar.html / schedule.html / history.html
│       ├── homework_tutor.html / homework_student.html
│       ├── finance.html / settings.html / requests.html
│       ├── public_profile.html / contacts.html
│       ├── admin/
│       │   ├── overview.html
│       │   ├── tutors.html
│       │   └── broadcast.html
│       ├── legal_*.html
│       ├── 404.html
│       ├── macros.html
│       └── partials/
│           └── add_student_modal.html
│
├── migrations/                     # SQL-миграции Supabase
│   ├── 001_product_platform.sql    # v2: users, profiles, lessons_v2, homeworks, etc.
│   ├── 002_server_runtime_policies.sql  # RLS-политики для серверного ключа
│   ├── 003_platform_full.sql       # v3: рекуррентность, CRM-поля
│   ├── 004_web_accounts.sql        # Email + password для веб-регистрации
│   ├── 005_growth_features.sql     # Платежи, шаблоны ДЗ, публичные страницы, рефералы
│   ├── 006_growth_rls_policies.sql # RLS для новых таблиц
│   ├── 007_subscription_payments.sql  # Таблица платежей Platega
│   ├── 008_lesson_packages.sql     # Абонементы (package_size, package_started_at)
│   ├── 009_email_verification.sql  # Коды подтверждения email
│   ├── 013_admin_flag.sql          # Флаг is_admin в users
│   └── 014_referral_reward_on_pay.sql  # Награда рефералу при первой оплате
│
└── prompts/                        # Промпты для AI-ассистентов
    ├── review_codex.md
    └── review_kimi.md
```

---

## ⚙️ Установка

### 1. Клонирование репозитория

```bash
git clone https://github.com/artemtit/Pingly.git
cd Pingly
```

### 2. Создание виртуального окружения

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate  # Windows
```

### 3. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 4. Настройка Supabase

1. Создайте проект в [Supabase](https://supabase.com).
2. Откройте **SQL Editor → New query**.
3. Выполните миграции **последовательно**:

```sql
-- 1. MVP-схема
\i supabase_schema.sql

-- 2. Платформа v2
\i migrations/001_product_platform.sql

-- 3. RLS-политики
\i migrations/002_server_runtime_policies.sql

-- 4. Рекуррентность + CRM
\i migrations/003_platform_full.sql

-- 5. Веб-аккаунты
\i migrations/004_web_accounts.sql

-- 6. Рост: платежи, шаблоны, публичные страницы
\i migrations/005_growth_features.sql

-- 7. RLS для growth-таблиц
\i migrations/006_growth_rls_policies.sql

-- 8. Платежи Platega
\i migrations/007_subscription_payments.sql

-- 9. Абонементы
\i migrations/008_lesson_packages.sql

-- 10. Подтверждение email
\i migrations/009_email_verification.sql

-- 11. Админ-флаг
\i migrations/013_admin_flag.sql

-- 12. Реферальные награды
\i migrations/014_referral_reward_on_pay.sql
```

### 5. Настройка переменных окружения

Создайте файл `.env` в корне проекта:

```bash
cp .env.example .env  # или создайте вручную
```

Заполните обязательные переменные (см. раздел [🔑 Переменные окружения](#-переменные-окружения)).

### 6. Запуск

```bash
python bot.py
```

Один процесс запускает одновременно:
- **Telegram-бота** (polling)
- **VK-бота** (если включён)
- **FastAPI веб-сервер** (uvicorn)
- **APScheduler** (доставка уведомлений, напоминания о подписках)

---

## 🔑 Переменные окружения

| Переменная | Обязательна | Назначение |
|------------|:-----------:|------------|
| `BOT_TOKEN` | ✅ | Токен Telegram-бота от [@BotFather](https://t.me/BotFather) |
| `SUPABASE_URL` | ✅ | URL проекта Supabase |
| `SUPABASE_KEY` | ✅ | Service Role Key Supabase (anon/сервисный) |
| `WEB_BASE_URL` | ❌ | Публичный URL приложения. По умолчанию `http://localhost:8000` |
| `WEB_HOST` | ❌ | Хост веб-сервера. По умолчанию `0.0.0.0` |
| `WEB_PORT` | ❌ | Порт веб-сервера. По умолчанию `8000` |
| `WEB_SECRET` | ❌ | Секрет для подписи cookies. По умолчанию `dev-change-me` |
| `WEB_ENABLED` | ❌ | Включить веб-сервер (`1`/`0`). По умолчанию `1` |
| `SUPPORT_TG_ID` | ❌ | Telegram ID для сообщений поддержки. По умолчанию `2091126912` |
| `SUPPORT_USERNAME` | ❌ | Telegram-юзернейм поддержки. По умолчанию `ligr5` |
| `SUPPORT_EMAIL` | ❌ | Email поддержки. По умолчанию `support@pingly-app.ru` |
| `PAYMENTS_ENABLED` | ❌ | Включить оплату (`1`/`0`). По умолчанию `0` |
| `PAYWALL_ENABLED` | ❌ | Жёсткий paywall (`1`/`0`). По умолчанию `0` |
| `PLANS_ENABLED` | ❌ | Двухуровневая подписка Pro/Max (`1`/`0`). По умолчанию `0` |
| `PLATEGA_API_URL` | ❌ | URL Platega API. По умолчанию `https://app.platega.io` |
| `PLATEGA_MERCHANT_ID` | ❌ | Merchant ID в Platega |
| `PLATEGA_SECRET` | ❌ | Секретный ключ Platega |
| `PLATEGA_PAYMENT_METHOD` | ❌ | ID метода оплаты. По умолчанию `11` (CARD_ACQUIRING) |
| `SUBSCRIPTION_PRICE_RUB` | ❌ | Цена месячной подписки. По умолчанию `990` |
| `SUBSCRIPTION_PRICE_YEAR_RUB` | ❌ | Цена годовой подписки. По умолчанию `9900` |
| `PRICE_PRO_RUB` | ❌ | Цена тарифа Pro. По умолчанию `590` |
| `PRICE_MAX_RUB` | ❌ | Цена тарифа Max. По умолчанию `990` |
| `EMAIL_VERIFICATION_ENABLED` | ❌ | Подтверждение email кодом (`1`/`0`). По умолчанию `0` |
| `RESEND_API_KEY` | ❌ | API-ключ Resend для email |
| `RESEND_FROM` | ❌ | Отправитель email. По умолчанию `Pingly <noreply@pingly-app.ru>` |
| `CAPTCHA_ENABLED` | ❌ | CAPTCHA на регистрации (`1`/`0`). По умолчанию `0` |
| `TURNSTILE_SITE_KEY` | ❌ | Site Key Cloudflare Turnstile |
| `TURNSTILE_SECRET_KEY` | ❌ | Secret Key Cloudflare Turnstile |
| `VK_ENABLED` | ❌ | VK-бот (`1`/`0`). По умолчанию `0` |
| `VK_TOKEN` | ❌ | Токен сообщества VK |
| `VK_APP_ID` | ❌ | VK App ID (для OAuth Фаза 2) |
| `VK_APP_SECRET` | ❌ | VK App Secret (для OAuth Фаза 2) |

---

## 🔐 Авторизация

### Способы входа

| Способ | Механизм | Статус |
|--------|----------|--------|
| **Email + пароль** | PBKDF2-HMAC-SHA256 (200 000 итераций), подписанные cookies (`itsdangerous.URLSafeTimedSerializer`) | ✅ Реализовано |
| **Telegram Login Widget** | HMAC-SHA256 проверка подписи Telegram, `auth_date` с TTL 24ч | ✅ Реализовано |
| **VK ID OAuth** | Подготовлено (флаги `VK_APP_ID`, `VK_APP_SECRET`), не реализовано в маршрутах | ⚠️ Фаза 2 |

### Защита API

- **Rate limiting** (in-memory, per-IP):
  - Бронирование: 5 запросов / 60 сек
  - Логин: 10 запросов / 5 мин
  - Регистрация: 5 запросов / 15 мин
  - Подтверждение кода: 10 запросов / 10 мин
  - Повторная отправка кода: 3 запроса / 10 мин
- **Owner-scoped queries**: все операции с данными фильтруются по `tutor_user_id` / `student_user_id`
- **RLS-политики Supabase**: permissive для серверного ключа (требует замены на service-role key в продакшене)

---

## 👤 Пользовательские роли

| Роль | Описание | Доступ |
|------|----------|--------|
| **tutor** | Репетитор | Полный доступ к веб-кабинету: ученики, расписание, домашние задания, финансы, настройки, публичная страница |
| **student** | Ученик | Телеграм-бот: просмотр занятий, подтверждение/отмена, домашние задания. Веб-кабинет: история, задания |
| **admin** | Администратор платформы | Обзор платформы, список репетиторов, массовые рассылки, ручное продление подписок |

> Роль `admin` определяется флагом `is_admin = true` в таблице `users`. Проверка в маршрутах: `@admin_required`.

---

## 📅 Работа расписания

### Создание занятий

1. **Разовое занятие** — выбирается дата, время, ученик, тема (опционально).
2. **Регулярная серия** — настраивается правило повторения:
   - `weekly` — еженедельно
   - `multiple_weekly` — несколько дней в неделю
   - `daily` — каждый день
   - `every_n_days` — каждые N дней
   - `every_n_weeks` — каждые N недель

### Генерация занятий

- При создании серии backend генерирует **до 60 ближайших занятий** на горизонт **56 дней** вперёд.
- Время хранится в UTC, отображается в московском (UTC+3).

### Изменение и перенос

| Действие | Что происходит |
|----------|----------------|
| **Перенос одного занятия** | Статус → `rescheduled`, новое время, уведомление ученику |
| **Перенос серии** | Меняется `lesson_time` у всех будущих занятий серии |
| **Отмена одного занятия** | Статус → `cancelled`, уведомление ученику |
| **Отмена серии** | `is_active = false`, все будущие занятия → `cancelled` |

### Статусы занятия

| Статус | Значение |
|--------|----------|
| `scheduled` | Запланировано (ожидает подтверждения ученика) |
| `confirmed` | Ученик подтвердил |
| `reschedule_requested` | Ученик попросил перенести |
| `completed` | Проведено (отмечено репетитором) |
| `rescheduled` | Перенесено |
| `cancelled` | Отменено |

---

## 📚 Домашние задания

### Жизненный цикл

```
new → in_progress → submitted → reviewed
```

| Действие | Кто выполняет | Результат |
|----------|-------------|-----------|
| Создание | Репетитор | Уведомление ученику в Telegram/VK |
| «В работе» | Ученик (веб) | — |
| «Сдано» | Ученик (веб) | Уведомление репетитору в Telegram |
| Проверка + комментарий | Репетитор | Уведомление ученику, статус → `reviewed` |

### Шаблоны

Репетитор может сохранить часто используемые задания как шаблоны и быстро создавать из них новые.

---

## 🔔 Уведомления

### Каналы доставки

| Канал | Статус | Описание |
|-------|:------:|----------|
| **Telegram** | ✅ | Основной канал. Бот отправляет сообщения с inline-кнопками |
| **VK** | ✅ | Альтернативный канал для учеников. Callback-кнопки «Буду/Отменяю» |
| **Email** | ⚠️ | Только коды подтверждения регистрации (через Resend) |
| **Push** | ❌ | Не реализовано |

### Типы уведомлений

| Тип | Получатель | Триггер |
|-----|-----------|---------|
| `lesson_day_before` | Ученик | За день до занятия |
| `lesson_hour_before` | Ученик | За 2 часа до занятия |
| `tutor_unconfirmed` | Репетитор | За 1 час до занятия, если ученик не подтвердил |
| `lesson_rescheduled` | Ученик | Перенос/отмена занятия |
| `lesson_reschedule_request` | Репетитор | Ученик нажал «Отменяю» |
| `homework_created` | Ученик | Новое домашнее задание |
| `homework_submitted` | Репетитор | Ученик отметил задание «сдано» |
| `homework_reviewed` | Ученик | Репетитор проверил задание |
| `booking_request` | Репетитор | Новая заявка через публичную страницу |
| `subscription_expiring` | Репетитор | Пробный период / подписка заканчивается (3, 1, 0 дней) |
| `package_ending` | Репетитор + Ученик | Абонемент заканчивается (1 осталось) или закончился (0) |

### Scheduler

- **Доставка**: каждую минуту (`interval`, `max_instances=1`, `coalesce=True`)
- **Напоминания о подписке**: каждые 12 часов
- **Напоминания об абонементах**: каждые 12 часов

---

## 🌐 API

Pingly использует **FastAPI** с серверным рендерингом (Jinja2). Основные маршруты:

### Публичные (без авторизации)

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/` | Лендинг |
| `GET` | `/privacy` | Политика конфиденциальности |
| `GET` | `/terms` | Пользовательское соглашение |
| `GET` | `/contacts` | Контакты поддержки |
| `GET` | `/u/{slug}` | Публичная страница репетитора |
| `POST` | `/u/{slug}/book` | Заявка на занятие |
| `GET` | `/auth/telegram` | Вход через Telegram Login Widget |
| `POST` | `/auth/login` | Email + пароль |
| `POST` | `/auth/register` | Регистрация email |
| `POST` | `/auth/verify` | Подтверждение email-кодом |
| `POST` | `/auth/resend` | Повторная отправка кода |
| `POST` | `/auth/logout` | Выход |
| `GET` | `/auth/link-telegram` | Привязка Telegram к email-аккаунту |

### Веб-кабинет репетитора (требует cookie-сессии)

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/tutor` | Дашборд |
| `GET` | `/tutor/students` | Список учеников |
| `GET` | `/tutor/students/{id}` | Карточка ученика |
| `POST` | `/tutor/students` | Добавить ученика |
| `POST` | `/tutor/students/{id}/delete` | Удалить ученика |
| `POST` | `/tutor/students/{id}/note` | Заметка об ученике |
| `POST` | `/tutor/students/{id}/package` | Установить/сбросить абонемент |
| `GET` | `/tutor/calendar` | Календарь занятий |
| `GET` | `/tutor/schedule` | Правила расписания |
| `POST` | `/tutor/schedule` | Создать серию занятий |
| `POST` | `/tutor/schedule/{id}/cancel` | Отменить серию |
| `POST` | `/tutor/schedule/{id}/reschedule` | Перенести серию |
| `GET` | `/tutor/homework` | Домашние задания |
| `POST` | `/tutor/homework` | Создать задание |
| `POST` | `/tutor/homework/{id}/review` | Проверить задание |
| `GET` | `/tutor/finance` | Финансовая сводка |
| `POST` | `/tutor/lesson/{id}/status` | Изменить статус занятия |
| `POST` | `/tutor/lesson/{id}/paid` | Отметить оплату |
| `POST` | `/tutor/lesson/{id}/delete` | Удалить занятие |
| `GET` | `/tutor/requests` | Заявки на занятия |
| `POST` | `/tutor/requests/{id}/status` | Обновить статус заявки |
| `GET` | `/tutor/settings` | Настройки профиля и подписки |
| `POST` | `/tutor/settings/profile` | Обновить профиль |
| `POST` | `/tutor/settings/public` | Настроить публичную страницу |
| `POST` | `/tutor/settings/password` | Сменить пароль |
| `POST` | `/tutor/subscribe` | Оплатить подписку (редирект на Platega) |

### Веб-кабинет ученика

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/student` | Дашборд ученика |
| `GET` | `/student/homework` | Мои задания |
| `POST` | `/student/homework/{id}/status` | Обновить статус задания |

### Админ-панель

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/admin` | Обзор платформы |
| `GET` | `/admin/tutors` | Список репетиторов |
| `POST` | `/admin/tutors/{id}/plan` | Установить план |
| `POST` | `/admin/tutors/{id}/trial` | Продлить пробный период |
| `GET` | `/admin/broadcast` | Форма рассылки |
| `POST` | `/admin/broadcast` | Отправить массовое сообщение |

### Webhooks

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/webhook/platega` | Callback от Platega (подтверждение платежа) |

---

## 🗄 База данных

### Схема (основные таблицы)

| Таблица | Назначение | Ключевые поля |
|---------|------------|---------------|
| `users` | Учётные записи | `id`, `role`, `tg_id`, `email`, `password_hash`, `trial_ends_at`, `subscription_status`, `referral_code`, `referred_by`, `is_admin` |
| `tutor_profiles` | Профиль репетитора | `user_id`, `display_name`, `slug`, `bio`, `subjects`, `public_enabled`, `page_theme` |
| `student_profiles` | Профиль ученика | `user_id`, `name`, `invite_token`, `subject_summary`, `grade`, `level`, `goal`, `default_price`, `package_size`, `package_started_at` |
| `subjects` | Предметы репетитора | `tutor_user_id`, `name` |
| `tutor_students` | Связь репетитор ↔ ученик | `tutor_user_id`, `student_id`, `status`, `private_tutor_note` |
| `schedule_rules` | Правила регулярных занятий | `tutor_user_id`, `student_id`, `recurrence`, `weekdays`, `interval_n`, `lesson_time`, `is_active` |
| `lessons_v2` | Конкретные занятия | `tutor_user_id`, `student_id`, `starts_at`, `status`, `price`, `paid`, `public_comment`, `schedule_rule_id` |
| `lesson_participants` | Участники групповых занятий (заготовка) | `lesson_id`, `student_id`, `status` |
| `homeworks` | Домашние задания | `tutor_user_id`, `student_id`, `title`, `description`, `status`, `due_at`, `tutor_comment` |
| `homework_templates` | Шаблоны заданий | `tutor_user_id`, `title`, `description` |
| `notifications` | Очередь уведомлений | `user_id`, `type`, `title`, `body`, `payload`, `channel`, `status`, `scheduled_for` |
| `booking_requests` | Заявки через публичную страницу | `tutor_user_id`, `name`, `contact`, `status` |
| `subscription_payments` | Платежи Platega | `user_id`, `transaction_id`, `amount_rub`, `status`, `confirmed_at` |
| `web_login_tokens` | Одноразовые токены входа | `user_id`, `token_hash`, `expires_at`, `used_at` |
| `attachments` | Файлы (заготовка) | `owner_user_id`, `lesson_id`, `homework_id`, `file_url` |
| `progress_snapshots` | Снапшоты прогресса (заготовка) | `student_id`, `attendance_percent`, `homework_completion_percent` |
| `plans` | Тарифные планы (заготовка) | `code`, `title`, `price_rub` |
| `subscriptions` | Подписки (заготовка) | `tutor_user_id`, `plan_id`, `status`, `current_period_end` |
| `payments` | Платежи (заготовка) | `subscription_id`, `amount_rub`, `provider`, `status` |

### Диаграмма связей

```text
users (1) ────► tutor_profiles (1)
   │
   ├───► student_profiles (0..1)  [если role=student]
   │
   ├───► tutor_students (N) ◄──── student_profiles (N)
   │         │
   │         ├───► schedule_rules (N)
   │         │           │
   │         │           └───► lessons_v2 (N)
   │         │
   │         ├───► homeworks (N)
   │         │
   │         └───► booking_requests (N)
   │
   ├───► notifications (N)
   │
   ├───► subscription_payments (N)
   │
   └───► web_login_tokens (N)
```

---

## 🤖 Telegram Bot

### Команды

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие, выбор роли, приём invite-ссылки |
| `/help` | Справка, ссылки на документы и поддержку |
| `/web` | Генерация одноразовой ссылки в веб-кабинет (10 мин) |

### Callback-кнопки ученика

| Callback | Действие |
|----------|----------|
| `lesson_confirm:{id}` | Подтвердить занятие |
| `lesson_cancel:{id}` | Отменить занятие + запросить причину |

### Сценарии

1. **Репетитор** пишет `/start` → получает приветствие → регистрируется на сайте.
2. **Репетитор** добавляет ученика в кабинете → система генерирует invite-ссылку.
3. **Ученик** открывает `t.me/Bot?start=inv_xxx` → бот привязывает Telegram к профилю.
4. **За 2 часа** до занятия ученик получает напоминание с кнопками «Буду / Отменяю».
5. **Ученик** нажимает «Отменяю» → может написать причину → репетитор получает уведомление.
6. **За 1 час** до занятия, если ученик не подтвердил — репетитор получает нудж.

---

## 💳 Оплата

Pingly интегрирован с платёжным шлюзом **Platega** (Россия).

### Модель подписки

| Параметр | Значение |
|----------|----------|
| **Пробный период** | 14 дней |
| **Месячная подписка** | 990 ₽ (или 590 ₽ Pro / 990 ₽ Max при `PLANS_ENABLED=1`) |
| **Годовая подписка** | 9900 ₽ (≈ 2 месяца бесплатно) |
| **Тарифы** | Pro (основной) / Max (+ Домашние задания, Финансы, Заявки) |

### Жизненный цикл платежа

```
Репетитор нажимает «Оформить подписку»
        │
        ▼
Backend создаёт транзакцию в Platega
        │
        ▼
Редирект на страницу оплаты Platega
        │
        ▼
Пользователь оплачивает
        │
        ▼
Platega отправляет webhook → /webhook/platega
        │
        ▼
Backend верифицирует подпись (HMAC) → активирует подписку
        │
        ▼
При первой оплате: +30 дней рефералу и пригласившему
```

### Статус

> **Оплата подготовлена, но отключена по умолчанию.** Для включения установите `PAYMENTS_ENABLED=1` и настройте `PLATEGA_MERCHANT_ID` + `PLATEGA_SECRET`.

---

## 🔒 Безопасность

| Аспект | Реализация |
|--------|------------|
| **Хранение паролей** | PBKDF2-HMAC-SHA256, 200 000 итераций, случайная соль |
| **Сессии** | Подписанные cookies (`itsdangerous.URLSafeTimedSerializer`), TTL 24ч |
| **Токены входа** | SHA256-хеш, TTL 10 мин, одноразовые (mark used) |
| **Telegram Widget** | HMAC-SHA256 проверка, TTL 24ч, защита от replay |
| **Platega webhook** | HMAC-сравнение `X-MerchantId` / `X-Secret` в constant time |
| **Rate limiting** | In-memory buckets per IP (бронирование, логин, регистрация) |
| **CAPTCHA** | Cloudflare Turnstile (опционально, включается флагом) |
| **Email верификация** | 6-значный код, TTL 15 мин, отправка через Resend |
| **RLS** | Supabase Row Level Security (permissive для серверного ключа) |
| **Owner-scoping** | Все запросы к БД фильтруются по `tutor_user_id` |

### Возможные риски

- **In-memory rate limiting** не работает при масштабировании на несколько процессов — требуется Redis.
- **RLS-политики** сейчас permissive (`using (true)`) — в продакшене следует использовать service-role key + строгие политики.
- **Отсутствие HTTPS enforcement** в коде — полагается на reverse proxy (nginx/Cloudflare).
- **Отсутствие audit log** — нет таблицы логов действий пользователей.

---

## 🚀 Roadmap

### Ближайшие шаги (основано на существующей архитектуре)

| Приоритет | Функция | Статус |
|:---------:|---------|:------:|
| 1 | **Включение платежей** (`PAYMENTS_ENABLED=1`) + тестирование Platega | 🔧 Готово, выключено |
| 2 | **VK ID OAuth** для репетиторов (`VK_APP_ID`, `VK_APP_SECRET` подготовлены) | 📋 Фаза 2 |
| 3 | **Групповые занятия** — таблица `lesson_participants` уже существует | 📋 Заготовка |
| 4 | **Прикрепление файлов** — таблица `attachments` уже существует | 📋 Заготовка |
| 5 | **Мобильное приложение** — подготовка схемы в README оригинала | 📋 Идея |
| 6 | **Email-уведомления** — сейчас только коды подтверждения | 📋 Расширение |
| 7 | **Redis для rate limiting** — замена in-memory buckets | 📋 Масштабирование |
| 8 | **Полноценный audit log** — таблица действий пользователей | 📋 Безопасность |
| 9 | **API документация (OpenAPI)** — FastAPI генерирует автоматически | 📋 Документация |
| 10 | **Webhook retries + dead letter queue** — надёжность доставки | 📧 Надёжность |

---

## 🤝 Contributing

Мы рады вкладу в проект! Вот как начать:

1. **Форкните** репозиторий.
2. **Создайте ветку** для вашей фичи: `git checkout -b feature/awesome-thing`.
3. **Следуйте архитектуре**: UI-слои (handlers, web) не обращаются к Supabase напрямую — только через `application/services`.
4. **Добавьте миграцию** в `migrations/`, если меняете схему БД.
5. **Обновите `design-decisions.md`** при значимых архитектурных изменениях.
6. **Сделайте PR** с описанием изменений.

### Стандарты кода

- Python 3.11+, type hints (`from __future__ import annotations`)
- Async/await везде, где есть I/O
- Комментарии на русском для бизнес-логики, на английском для технических деталей
- `hmac.compare_digest` для всех сравнений секретов
- `max_instances=1, coalesce=True` для scheduler-джоб

---

## 📄 License

Лицензия не указана. Проект является приватной разработкой.

---

## 📸 Скриншоты

> Скриншоты не добавлены в репозиторий. Ниже — места для вставки изображений после создания скриншотов.

### Лендинг
```markdown
![Лендинг Pingly](docs/screenshots/landing.png)
```

### Веб-кабинет репетитора
```markdown
![Дашборд репетитора](docs/screenshots/tutor-dashboard.png)
![Календарь занятий](docs/screenshots/calendar.png)
![Карточка ученика](docs/screenshots/student-card.png)
```

### Telegram-бот
```markdown
![Напоминание в Telegram](docs/screenshots/telegram-reminder.png)
![Меню репетитора](docs/screenshots/tutor-menu.png)
```

---

*Pingly — сделано с ❤️ для репетиторов и учеников.*
