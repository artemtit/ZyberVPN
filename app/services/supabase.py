from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Callable, TypeVar

from app.config import Settings, load_settings

try:
    from supabase import Client, create_client
except Exception:  # pragma: no cover
    Client = object  # type: ignore[assignment]
    create_client = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)
T = TypeVar("T")

# SQL reference (run in Supabase SQL editor):
#
# CREATE TABLE IF NOT EXISTS public.users (
#   id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
#   tg_id bigint NOT NULL UNIQUE,
#   vpn_key text,
#   sub_token text UNIQUE,
#   expires_at timestamp with time zone,
#   is_active boolean NOT NULL DEFAULT true,
#   plan text,
#   promo_used boolean NOT NULL DEFAULT false,
#   last_activated_at timestamp with time zone NULL,
#   created_at timestamp with time zone NOT NULL DEFAULT now()
# );
#
# CREATE TABLE IF NOT EXISTS public.servers (
#   id bigint generated always as identity primary key,
#   name text not null,
#   host text not null,
#   api_url text not null,
#   username text not null,
#   password text not null,
#   inbound_id int not null,
#   public_key text,
#   short_id text,
#   country text not null default 'unknown',
#   is_active boolean not null default true,
#   sni text,
#   public_port int not null default 443,
#   ws_path text not null default '/ws',
#   ws_host text,
#   last_health_check timestamptz,
#   health_errors int not null default 0,
#   last_error text
# );
#
# CREATE TABLE IF NOT EXISTS public.user_vpn (
#   id bigint generated always as identity primary key,
#   user_id bigint not null,
#   server_id bigint not null references public.servers(id),
#   reality_uuid text not null,
#   ws_uuid text,
#   reality_config text not null,
#   ws_config text not null,
#   created_at timestamp with time zone not null default now(),
#   updated_at timestamp with time zone not null default now(),
#   unique(user_id)
# );
#
# CREATE TABLE IF NOT EXISTS public.promo_codes (
#   id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
#   code text NOT NULL UNIQUE,
#   days int NOT NULL DEFAULT 30,
#   max_uses int NULL,
#   used_count int NOT NULL DEFAULT 0,
#   expires_at timestamp with time zone NULL,
#   is_active boolean NOT NULL DEFAULT true,
#   created_at timestamp with time zone NOT NULL DEFAULT now()
# );


@lru_cache(maxsize=1)
def get_supabase_client() -> Client | None:
    settings: Settings = load_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        return None
    if create_client is None:
        logger.error("supabase-py is not installed")
        return None
    try:
        return create_client(settings.supabase_url, settings.supabase_service_key)
    except Exception:
        logger.exception("Failed to initialize Supabase client")
        return None


async def execute_with_retry(
    action: Callable[[], T],
    *,
    operation: str,
    retries: int = 3,
    timeout_seconds: float = 8.0,
) -> T:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await asyncio.wait_for(asyncio.to_thread(action), timeout=timeout_seconds)
        except Exception as error:
            last_error = error
            if attempt >= retries:
                break
            await asyncio.sleep(0.2 * (2 ** (attempt - 1)))
    logger.exception("Supabase operation failed after retries op=%s", operation)
    raise RuntimeError(f"Supabase operation failed: {operation}") from last_error
