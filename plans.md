# ZyberVPN — Планы (не срочно)

## Инфраструктура

### Лимит пользователей на сервер (`max_users`)
Код уже готов, нужно только запустить SQL и раскомментировать колонку.
```sql
ALTER TABLE servers ADD COLUMN IF NOT EXISTS max_users integer DEFAULT 0;
-- Потом установить значения:
UPDATE servers SET max_users = 100 WHERE id IN (1, 2);
```
После этого раскомментировать `"max_users"` в `app/db/schema_contract.py`.

---

## Продукт

### Отправка чека на email
Email сохраняется в `payments.email`, но письмо не отправляется. Нужен SMTP или сервис (Mailgun, Resend, SendGrid).
- Отправлять после `mark_paid`: тариф, сумма, дата, срок подписки
- Добавить `SMTP_HOST / SMTP_USER / SMTP_PASSWORD` в `.env`

### Промокоды на конкретный тариф
Сейчас промокод даёт фиксированный период. Добавить поле `tariff_code` в `promo_codes` — промокод на конкретный план.

### Реферальные выплаты в рублях
Сейчас бонус начисляется в `users.balance`, но тратить его нельзя. Добавить возможность применять баланс как скидку при оплате через Platega.

---

## Надёжность (из аудита — некритично)

### C6 — Failed idempotency блокирует повторные попытки
`app/services/idempotency.py` ~L35  
Если операция завершилась `"failed"`, следующий вызов с тем же ключом навсегда возвращает `failed`. Нужен eviction: удалять `failed`-записи старше N минут (или при явном retry).

### H1 — Утечка памяти: `_promo_attempts`
`app/bot/handlers/profile.py` L39  
`dict[int, list[float]]` растёт вечно. Чистить записи при успехе или по TTL.

### H3 — `set_primary` неатомарна
`app/repositories/keys.py` ~L68  
Два UPDATE вместо одного → между запросами пользователь без primary. Нужна транзакция или RPC.

### H5 — Distributed lock TTL 30s < время XUI-операций
`app/services/distributed_lock.py` ~L47  
Медленный сервер → lock истекает → параллельная операция. Увеличить TTL до 90s или добавить heartbeat.

### M1 — Timeout в distributed lock не освобождает local lock
`app/services/distributed_lock.py` ~L88  
`asyncio.wait_for` timeout не вызывает `finally`. Ключ остаётся в `_local_locks` до рестарта.

### M5 — Несоответствие min_length для sub_token
`app/repositories/keys.py` L185 ищет токены ≥20 символов, а `app/api/schemas.py` L8 требует ≥32. Токены 20–31 символ записываются в БД, но API их отклоняет.

### M8 — UUID-коллизия проверяется только для Reality
`app/services/vpn/manager.py` ~L420  
WS-клиент создаётся без проверки существующего UUID. Добавить аналогичную проверку.

### L1 — `sanitize_log_data()` не используется
`app/utils/security.py` L14  
Функция маскирует чувствительные поля в логах, но нигде не вызывается. Подключить к логированию конфигов VPN.

### L3 — Ban + VPN: если XUI-disable падает, бан всё равно ставится
`app/bot/handlers/admin.py` L251  
`_BANNED_IDS.add()` выполняется безусловно. Если отключение VPN упало — бот блокирует сообщения, но VPN работает.

---

## Мониторинг

### Алерты на исчерпание диска
Основной сервер: 15 GB SSD. Логи растут. Добавить cron или watchdog: если `/` > 85% → уведомление админу.

### Метрики в Telegram
Команда `/stats` показывает базовую инфу. Добавить: активных пользователей за 24ч, топ ошибок за день, загрузку серверов.
