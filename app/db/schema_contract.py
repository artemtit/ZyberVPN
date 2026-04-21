from __future__ import annotations

SERVER_COLUMNS = [
    "id",
    "name",
    "host",
    "api_url",
    "username",
    "password",
    "inbound_id",
    "public_key",
    "short_id",
    "country",
    "is_active",
    "sni",
    "public_port",
    "ws_path",
    "ws_host",
    "last_health_check",
    "health_errors",
    "last_error",
]

USERS_COLUMNS = [
    "id",
    "tg_id",
    "ref_tg_id",
    "balance",
    "trial_used",
    "promo_used",
    "vpn_key",
    "sub_token",
    "expires_at",
    "is_active",
    "plan",
    "last_activated_at",
    "created_at",
]

USER_VPN_COLUMNS = [
    "id",
    "user_id",
    "server_id",
    "reality_uuid",
    "ws_uuid",
    "reality_config",
    "ws_config",
    "created_at",
    "updated_at",
]

SUBSCRIPTIONS_COLUMNS = [
    "id",
    "tg_id",
    "expires_at",
    "status",
    "created_at",
]

PAYMENTS_COLUMNS = [
    "id",
    "tg_id",
    "amount",
    "status",
    "tariff_code",
    "email",
    "payload",
    "idempotency_key",
    "telegram_payment_charge_id",
    "created_at",
]

KEYS_COLUMNS = [
    "id",
    "tg_id",
    "key",
    "created_at",
]

PROMO_CODES_COLUMNS = [
    "id",
    "code",
    "days",
    "max_uses",
    "used_count",
    "expires_at",
    "is_active",
    "created_at",
]

IDEMPOTENCY_KEYS_COLUMNS = [
    "operation",
    "idempotency_key",
    "status",
    "response_payload",
    "created_at",
]

EXPECTED_SCHEMA: dict[str, list[str]] = {
    "servers": SERVER_COLUMNS,
    "users": USERS_COLUMNS,
    "user_vpn": USER_VPN_COLUMNS,
    "subscriptions": SUBSCRIPTIONS_COLUMNS,
    "payments": PAYMENTS_COLUMNS,
    "keys": KEYS_COLUMNS,
    "promo_codes": PROMO_CODES_COLUMNS,
    "idempotency_keys": IDEMPOTENCY_KEYS_COLUMNS,
}

