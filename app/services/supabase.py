from __future__ import annotations

import logging
from functools import lru_cache

from app.config import Settings, load_settings

try:
    from supabase import Client, create_client
except Exception:  # pragma: no cover
    Client = object  # type: ignore[assignment]
    create_client = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

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
#   created_at timestamp with time zone NOT NULL DEFAULT now()
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
