from __future__ import annotations

import logging
from datetime import timedelta
from typing import Awaitable, Callable, TypeVar
from uuid import uuid4

from aiogram import Bot

from app.config import Settings, load_settings
from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.servers import ServersRepository
from app.repositories.user_vpn import UserVpnRepository
from app.repositories.vpn_devices import VpnDevicesRepository
from app.repositories.users import UsersRepository
from app.services.vpn.manager import VPNManager, VPNManagerError
from app.services.vpn.xui_provider import XUIProvider
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


def build_vpn_manager(db: Database, settings: Settings, bot: Bot | None = None) -> VPNManager:
    servers_repo = ServersRepository(db)
    user_vpn_repo = UserVpnRepository(db)
    users_repo = UsersRepository(db)
    vpn_devices_repo = VpnDevicesRepository(db)
    providers = {"xui": XUIProvider()}
    return VPNManager(
        providers=providers,
        servers_repo=servers_repo,
        user_vpn_repo=user_vpn_repo,
        vpn_devices_repo=vpn_devices_repo,
        settings=settings,
        users_repo=users_repo,
        bot=bot,
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
    idempotency_key: str | None = None,  # kept for API compatibility; no longer used
    force_new_key: bool = False,
    action: str | None = None,
) -> dict:
    settings = settings or load_settings()
    db = db or Database(settings.db_path)
    access_action = action or ("create" if force_new_key else "existing")
    if force_new_key and access_action != "create":
        raise AccessEnsureError("force_new_key requires action=create")
    if access_action not in {"create", "existing"}:
        raise AccessEnsureError(f"Unsupported access action: {access_action}")
    force_new_key = access_action == "create"

    users_repo = UsersRepository(db)
    if not users_repo.has_supabase:
        raise AccessEnsureError("Supabase is unavailable")
    keys_repo = KeysRepository(db)
    manager = build_vpn_manager(db, settings)

    logger.info("Ensuring access tg_id=%s action=%s force_new_key=%s", tg_id, access_action, force_new_key)
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

    stored_token = str(supabase_user.get("sub_token") or "")
    needs_new_token = (
        not stored_token
        or users_repo.is_valid_sub_token_hash(stored_token)
        or not users_repo.is_valid_sub_token(stored_token)
    )
    if needs_new_token:
        sub_token = await _safe_repo_call(
            "users.ensure_sub_token_for_tg",
            lambda: users_repo.ensure_sub_token_for_tg(tg_id),
            fallback="",
            tg_id=tg_id,
        )
        if not sub_token:
            raise AccessEnsureError("Failed to refresh subscription token")
        supabase_user["sub_token"] = sub_token

    if require_active and not users_repo.is_user_active(supabase_user):
        await _safe_repo_call("users.update_status", lambda: users_repo.update_status(tg_id, False), fallback=None, tg_id=tg_id)
        raise AccessEnsureError("Subscription inactive")

    if require_recent_activation_for_key_creation and not _is_recent_activation((supabase_user or {}).get("last_activated_at")):
        raise AccessEnsureError("Activation too old for auto key creation")

    expiry_ms = _expiry_to_ms((supabase_user or {}).get("expires_at"))

    if force_new_key:
        logger.debug("ACCESS FLOW | user_id=%s key_id=PENDING action=create", tg_id)

        # Pre-allocate a real key_id before VPN creation.
        # Passing key_id=None targets the null-slot which may already hold a 'ready' row,
        # causing the state machine to return stale configs instead of provisioning a new client.
        placeholder = f"creating:{uuid4()}"
        pre_key = await _safe_repo_call(
            "keys.create_placeholder",
            lambda: keys_repo.create(tg_id, placeholder),
            fallback=None,
            tg_id=tg_id,
        )
        if not pre_key:
            raise AccessEnsureError("Failed to allocate key slot")
        new_key_id = int(pre_key["id"])
        if not new_key_id:
            raise AccessEnsureError("Failed to allocate valid key ID")

        logger.debug("ACCESS FLOW | user_id=%s key_id=%s action=create step=allocated", tg_id, new_key_id)
        logger.info("VPN key slot pre-allocated tg_id=%s key_id=%s", tg_id, new_key_id)

        try:
            vpn_configs = await manager.create_user_access(tg_id, expiry_time=expiry_ms, key_id=new_key_id)
        except VPNManagerError as error:
            logger.error("VPN creation failed tg_id=%s key_id=%s error=%s", tg_id, new_key_id, error)
            raise AccessEnsureError(str(error)) from error

        vpn_configs = [str(item) for item in (vpn_configs or []) if str(item)]
        logger.info("VPN ensure (new key) completed tg_id=%s key_id=%s configs=%s", tg_id, new_key_id, len(vpn_configs))

        primary_key = vpn_configs[0] if vpn_configs else ""
        if primary_key and not _is_vpn_key_valid(primary_key):
            raise AccessEnsureError("Invalid vpn_key after ensure")

        if primary_key:
            await _safe_repo_call(
                "keys.update_key_text",
                lambda: keys_repo.update_key_text(new_key_id, tg_id, primary_key),
                fallback=None,
                tg_id=tg_id,
            )

        current_key = str((supabase_user or {}).get("vpn_key") or "")
        if primary_key and current_key != primary_key:
            updated_key = await _safe_repo_call(
                "users.update_key", lambda: users_repo.update_key(tg_id, primary_key), fallback=None, tg_id=tg_id
            )
            if not updated_key:
                raise AccessEnsureError("Failed to persist vpn_key")
            supabase_user["vpn_key"] = primary_key

        if primary_key:
            # Only promote this key to primary if no other key is already primary.
            # A user's first key is always primary; subsequent keys are NOT, unless
            # they explicitly use "Set as primary" in the key card.
            existing_keys = await _safe_repo_call(
                "keys.list_by_user.check_primary",
                lambda: keys_repo.list_by_user(tg_id),
                fallback=[],
                tg_id=tg_id,
            )
            has_existing_primary = any(
                k.get("is_primary") and int(k.get("id") or 0) != new_key_id
                for k in existing_keys
            )
            if not has_existing_primary:
                await _safe_repo_call(
                    "keys.set_primary",
                    lambda: keys_repo.set_primary(tg_id, new_key_id),
                    fallback=None,
                    tg_id=tg_id,
                )
                logger.info("VPN key created and set primary tg_id=%s key_id=%s", tg_id, new_key_id)
            else:
                logger.info("VPN key created (not primary, primary already exists) tg_id=%s key_id=%s", tg_id, new_key_id)

        # Generate per-key sub_token so each key has an independent subscription URL.
        key_sub_token = ""
        try:
            key_sub_token = await keys_repo.ensure_sub_token(new_key_id, tg_id)
            logger.info("sub_token generated for key tg_id=%s key_id=%s", tg_id, new_key_id)
        except Exception:
            logger.warning("Failed to generate sub_token tg_id=%s key_id=%s", tg_id, new_key_id)
    else:
        # Return existing configs without provisioning anything new.
        # Use the primary key's config (not vpn_configs[0] which is oldest by created_at).
        all_keys = await _safe_repo_call(
            "keys.list_by_user", lambda: keys_repo.list_by_user(tg_id), fallback=[], tg_id=tg_id
        )
        primary_key_row = next((k for k in all_keys if k.get("is_primary")), all_keys[0] if all_keys else None)
        primary_key = str((primary_key_row or {}).get("key") or "")
        if not _is_vpn_key_valid(primary_key):
            primary_key = ""
        vpn_configs = await manager.get_subscription(tg_id, create_if_missing=False)
        vpn_configs = [str(item) for item in (vpn_configs or []) if str(item)]
        logger.info("VPN ensure (existing) completed tg_id=%s configs=%s primary=%s", tg_id, len(vpn_configs), bool(primary_key))

    refreshed = await _safe_repo_call("users.get_by_tg_id.refresh", lambda: users_repo.get_by_tg_id(tg_id), fallback=None, tg_id=tg_id)
    final_user = refreshed or supabase_user
    final_user["vpn_key"] = primary_key
    final_user["vpn_configs"] = vpn_configs
    if force_new_key and "new_key_id" in locals():
        final_user["key_id"] = new_key_id
    # Expose per-key sub_token if available (used by payments handler for sub_url)
    if force_new_key and "key_sub_token" in locals() and key_sub_token:
        final_user["key_sub_token"] = key_sub_token
    return final_user
