from __future__ import annotations

import logging
from datetime import timedelta
from typing import Awaitable, Callable, TypeVar

from app.config import Settings, load_settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.servers import ServersRepository
from app.repositories.user_vpn import UserVpnRepository
from app.repositories.users import UsersRepository
from app.services.distributed_lock import DistributedLockManager
from app.services.vpn.manager import VPNManager, VPNManagerError
from app.services.vpn.xui_provider import XUIProvider
from app.services.idempotency import IdempotencyService
from app.utils.datetime import parse_iso_utc, utc_diff, utc_now

logger = logging.getLogger(__name__)
MAX_SUB_AUTO_CREATE_AGE = timedelta(days=7)
T = TypeVar("T")


class AccessEnsureError(RuntimeError):
    pass


def _is_vpn_key_valid(vpn_key: str | None) -> bool:
    return bool(vpn_key and str(vpn_key).startswith("vless://"))


def _is_recent_activation(raw_value: str | None) -> bool:
    if not raw_value:
        return False
    try:
        activated_at = parse_iso_utc(raw_value)
    except Exception:
        return False
    return utc_diff(utc_now(), activated_at) <= MAX_SUB_AUTO_CREATE_AGE


def _expiry_to_ms(raw_value: str | None) -> int | None:
    if not raw_value:
        return None
    try:
        parsed_utc = parse_iso_utc(raw_value)
    except Exception:
        return None
    return int(parsed_utc.timestamp() * 1000)


def build_vpn_manager(db: Database, settings: Settings) -> VPNManager:
    servers_repo = ServersRepository(db)
    user_vpn_repo = UserVpnRepository(db)
    providers = {"xui": XUIProvider()}
    return VPNManager(
        providers=providers,
        servers_repo=servers_repo,
        user_vpn_repo=user_vpn_repo,
        settings=settings,
    )


async def _safe_repo_call(
    operation: str,
    action: Callable[[], Awaitable[T]],
    *,
    fallback: T,
    tg_id: int | None = None,
) -> T:
    try:
        return await action()
    except Exception as error:
        logger.error("Repository call failed operation=%s tg_id=%s error=%s", operation, tg_id, error)
        return fallback


async def ensure_user_access(
    tg_id: int,
    db: Database | None = None,
    settings: Settings | None = None,
    require_active: bool = True,
    require_recent_activation_for_key_creation: bool = False,
    idempotency_key: str | None = None,
) -> dict:
    settings = settings or load_settings()
    db = db or Database(settings.db_path)

    users_repo = UsersRepository(db)
    if not users_repo.has_supabase:
        raise AccessEnsureError("Supabase is unavailable")
    keys_repo = KeysRepository(db)
    idem_service = IdempotencyService(IdempotencyRepository())
    manager = build_vpn_manager(db, settings)
    lock_manager = DistributedLockManager(settings.redis_url)

    async with lock_manager.lock(f"access-ensure:{tg_id}", ttl_seconds=45, wait_timeout=10):
        logger.info("Access lock acquired tg_id=%s", tg_id)
        ensured = await _safe_repo_call("users.get_or_create", lambda: users_repo.get_or_create(tg_id), fallback=None, tg_id=tg_id)
        if not ensured:
            raise AccessEnsureError("Failed to initialize user")
        supabase_user = await _safe_repo_call("users.get_by_tg_id", lambda: users_repo.get_by_tg_id(tg_id), fallback=None, tg_id=tg_id)
        if users_repo.has_supabase and users_repo.last_supabase_error:
            raise AccessEnsureError("Supabase is unavailable")

        if not supabase_user:
            sub_token = await _safe_repo_call("users.ensure_sub_token", lambda: users_repo.ensure_sub_token(tg_id), fallback="", tg_id=tg_id)
            if not sub_token:
                raise AccessEnsureError("Failed to create Supabase user")
            created = await _safe_repo_call(
                "users.create",
                lambda: users_repo.create(
                    tg_id=tg_id,
                    vpn_key="",
                    sub_token=str(sub_token),
                    is_active=False,
                    plan="none",
                ),
                fallback=None,
                tg_id=tg_id,
            )
            if not created:
                raise AccessEnsureError("Failed to create Supabase user")
            supabase_user = created

        sub_token_hash = supabase_user.get("sub_token")
        if not sub_token_hash or not users_repo.is_valid_sub_token_hash(str(sub_token_hash)):
            sub_token = await _safe_repo_call(
                "users.ensure_sub_token_for_tg",
                lambda: users_repo.ensure_sub_token_for_tg(tg_id),
                fallback="",
                tg_id=tg_id,
            )
            if not sub_token:
                raise AccessEnsureError("Failed to refresh subscription token")
            supabase_user["sub_token"] = users_repo.hash_sub_token(sub_token)

        if require_active and not users_repo.is_user_active(supabase_user):
            await _safe_repo_call("users.update_status", lambda: users_repo.update_status(tg_id, False), fallback=None, tg_id=tg_id)
            raise AccessEnsureError("Subscription inactive")

        if require_recent_activation_for_key_creation and not _is_recent_activation((supabase_user or {}).get("last_activated_at")):
            raise AccessEnsureError("Activation too old for auto key creation")

        expiry_ms = _expiry_to_ms((supabase_user or {}).get("expires_at"))

        async def _create_vpn() -> dict:
            try:
                configs = await manager.create_user_access(tg_id, expiry_time=expiry_ms, idempotency_key=idem_key)
            except VPNManagerError as error:
                logger.error("VPN creation skipped operation=vpn.create_user_access tg_id=%s error=%s", tg_id, error)
                return {"vpn_configs": []}
            return {"vpn_configs": configs}

        idem_key = idempotency_key or f"vpn-create:{tg_id}"
        idem_result = await idem_service.execute("vpn_create", idem_key, _create_vpn)
        vpn_configs = [str(item) for item in (idem_result.get("vpn_configs") or []) if str(item)]
        logger.info("VPN ensure completed tg_id=%s configs=%s idempotency_key=%s", tg_id, len(vpn_configs), idem_key)

        primary_key = vpn_configs[0] if vpn_configs else ""
        if primary_key and not _is_vpn_key_valid(primary_key):
            raise AccessEnsureError("Invalid vpn_key after ensure")

        current_key = str((supabase_user or {}).get("vpn_key") or "")
        if primary_key and current_key != primary_key:
            updated_key = await _safe_repo_call("users.update_key", lambda: users_repo.update_key(tg_id, primary_key), fallback=None, tg_id=tg_id)
            if not updated_key:
                raise AccessEnsureError("Failed to persist vpn_key")
            supabase_user["vpn_key"] = primary_key
        if primary_key:
            await _safe_repo_call("keys.create", lambda: keys_repo.create(tg_id, primary_key), fallback=None, tg_id=tg_id)
            logger.info("VPN key persisted tg_id=%s", tg_id)

        refreshed = await _safe_repo_call("users.get_by_tg_id.refresh", lambda: users_repo.get_by_tg_id(tg_id), fallback=None, tg_id=tg_id)
        final_user = refreshed or supabase_user
        final_user["vpn_key"] = primary_key
        final_user["vpn_configs"] = vpn_configs
        return final_user


