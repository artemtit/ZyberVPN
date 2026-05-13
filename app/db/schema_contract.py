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
    # "max_users",  # uncomment after running migrations/2026_05_servers_max_users.sql
]
