from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import Settings, load_settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.users import UsersRepository
from app.services.vpn import VPNProvisionError, create_vpn_key_via_3xui

logger = logging.getLogger(__name__)
_ACCESS_LOCKS: dict[int, asyncio.Lock] = {}
MAX_SUB_AUTO_CREATE_AGE = timedelta(days=7)


class AccessEnsureError(RuntimeError):
    pass


def _lock_for_tg(tg_id: int) -> asyncio.Lock:
    lock = _ACCESS_LOCKS.get(tg_id)
    if lock is None:
        lock = asyncio.Lock()
        _ACCESS_LOCKS[tg_id] = lock
    return lock


def _is_vpn_key_valid(vpn_key: str | None) -> bool:
    return bool(vpn_key and str(vpn_key).startswith("vless://"))


def _is_recent_activation(raw_value: str | None) -> bool:
    if not raw_value:
        return False
    try:
        activated_at = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except Exception:
        return False
    if activated_at.tzinfo is None:
        activated_at = activated_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - activated_at <= MAX_SUB_AUTO_CREATE_AGE


async def ensure_user_access(
    tg_id: int,
    db: Database | None = None,
    settings: Settings | None = None,
    require_active: bool = True,
    require_recent_activation_for_key_creation: bool = False,
) -> dict:
    settings = settings or load_settings()
    db = db or Database(settings.db_path)

    users_repo = UsersRepository(db)
    keys_repo = KeysRepository(db)
    lock = _lock_for_tg(tg_id)

    async with lock:
        local_user = await users_repo.get_or_create(tg_id)
        supabase_user = await users_repo.get_by_tg_id(tg_id)
        if users_repo.has_supabase and users_repo.last_supabase_error:
            raise AccessEnsureError("Supabase is unavailable")

        if not supabase_user:
            sub_token = await users_repo.ensure_sub_token(local_user["id"])
            created = await users_repo.create(
                tg_id=tg_id,
                vpn_key="",
                sub_token=str(sub_token),
                is_active=False,
                plan="none",
            )
            if not created:
                raise AccessEnsureError("Failed to create Supabase user")
            supabase_user = created
            logger.info("Access bootstrap: created Supabase user for tg_id=%s", tg_id)

        sub_token = supabase_user.get("sub_token")
        if not sub_token or not users_repo.is_valid_sub_token(str(sub_token)):
            sub_token = await users_repo.ensure_sub_token(local_user["id"])
            updated = await users_repo.update_sub_token(tg_id, str(sub_token))
            if not updated:
                raise AccessEnsureError("Failed to persist sub_token")
            supabase_user["sub_token"] = str(sub_token)

        if require_active and not users_repo.is_user_active(supabase_user):
            await users_repo.update_status(tg_id, False)
            raise AccessEnsureError("Subscription inactive")

        # Re-read before provisioning to reduce race probability.
        fresh_user = await users_repo.get_by_tg_id(tg_id)
        if fresh_user and _is_vpn_key_valid(fresh_user.get("vpn_key")):
            existing_key = str(fresh_user["vpn_key"])
            if not await keys_repo.exists_for_user(local_user["id"], existing_key):
                await keys_repo.create(local_user["id"], existing_key)
            return fresh_user

        vpn_key = (fresh_user or supabase_user or {}).get("vpn_key")
        if not _is_vpn_key_valid(vpn_key):
            if require_recent_activation_for_key_creation and not _is_recent_activation((fresh_user or supabase_user or {}).get("last_activated_at")):
                raise AccessEnsureError("Activation too old for auto key creation")
            try:
                vpn_key = await create_vpn_key_via_3xui(settings=settings, tg_id=tg_id)
                logger.info("Access bootstrap: created vpn_key for tg_id=%s", tg_id)
            except VPNProvisionError as error:
                logger.exception("Access bootstrap: failed to create vpn_key for tg_id=%s", tg_id)
                raise AccessEnsureError("Failed to create VPN key") from error

            # Re-read again: if another flow persisted key while we were creating, do not overwrite.
            post_create_user = await users_repo.get_by_tg_id(tg_id)
            existing_after_create = (post_create_user or {}).get("vpn_key")
            final_key = str(existing_after_create or vpn_key)
            if not _is_vpn_key_valid(existing_after_create):
                updated = await users_repo.update_key(tg_id, str(vpn_key))
                if not updated:
                    raise AccessEnsureError("Failed to persist vpn_key")
                final_key = str(vpn_key)

            if not await keys_repo.exists_for_user(local_user["id"], final_key):
                await keys_repo.create(local_user["id"], final_key)
            supabase_user["vpn_key"] = final_key

        refreshed = await users_repo.get_by_tg_id(tg_id)
        return refreshed or supabase_user
