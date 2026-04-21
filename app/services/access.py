from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiohttp import ClientSession, ClientTimeout, web

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
        await users_repo.get_or_create(tg_id)
        supabase_user = await users_repo.get_by_tg_id(tg_id)
        if users_repo.has_supabase and users_repo.last_supabase_error:
            raise AccessEnsureError("Supabase is unavailable")

        if not supabase_user:
            sub_token = await users_repo.ensure_sub_token(tg_id)
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

        sub_token_hash = supabase_user.get("sub_token")
        if not sub_token_hash or not users_repo.is_valid_sub_token_hash(str(sub_token_hash)):
            sub_token = await users_repo.ensure_sub_token_for_tg(tg_id)
            supabase_user["sub_token"] = users_repo.hash_sub_token(sub_token)

        if require_active and not users_repo.is_user_active(supabase_user):
            await users_repo.update_status(tg_id, False)
            raise AccessEnsureError("Subscription inactive")

        if require_recent_activation_for_key_creation and not _is_recent_activation((supabase_user or {}).get("last_activated_at")):
            raise AccessEnsureError("Activation too old for auto key creation")

        expiry_ms = _expiry_to_ms((supabase_user or {}).get("expires_at"))

        async def _create_vpn() -> dict:
            try:
                configs = await manager.create_user_access(tg_id, expiry_time=expiry_ms, idempotency_key=idem_key)
            except VPNManagerError as error:
                logger.exception("VPNManager failed for tg_id=%s error=%s", tg_id, error)
                raise AccessEnsureError("Failed to create VPN access") from error
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
            updated_key = await users_repo.update_key(tg_id, primary_key)
            if not updated_key:
                raise AccessEnsureError("Failed to persist vpn_key")
            supabase_user["vpn_key"] = primary_key
        if primary_key:
            await keys_repo.create(tg_id, primary_key)
            logger.info("VPN key persisted tg_id=%s", tg_id)

        refreshed = await users_repo.get_by_tg_id(tg_id)
        final_user = refreshed or supabase_user
        final_user["vpn_key"] = primary_key
        final_user["vpn_configs"] = vpn_configs
        return final_user


async def test_full_flow(tg_id: int, db: Database | None = None, settings: Settings | None = None) -> dict:
    settings = settings or load_settings()
    db = db or Database(settings.db_path)
    await db.init()

    users_repo = UsersRepository(db)
    await users_repo.get_or_create(tg_id)
    sub_token = await users_repo.ensure_sub_token(tg_id)

    now = utc_now()
    expires_at = (now + timedelta(days=1)).isoformat()
    supabase_user = await users_repo.get_by_tg_id(tg_id)
    if not supabase_user:
        created = await users_repo.create(
            tg_id=tg_id,
            vpn_key="",
            sub_token=sub_token,
            expires_at=expires_at,
            is_active=True,
            plan="trial",
            last_activated_at=now.isoformat(),
        )
        if not created:
            raise AccessEnsureError("test_full_flow: failed to create Supabase user")
    else:
        if not users_repo.is_valid_sub_token_hash(str(supabase_user.get("sub_token") or "")):
            updated_token = await users_repo.update_sub_token(tg_id, sub_token)
            if not updated_token:
                raise AccessEnsureError("test_full_flow: failed to update sub_token")
        updated_expiry = await users_repo.set_expiry(
            tg_id=tg_id,
            expires_at=expires_at,
            is_active=True,
            last_activated_at=now.isoformat(),
        )
        if not updated_expiry:
            raise AccessEnsureError("test_full_flow: failed to activate subscription")

    access_user = await ensure_user_access(tg_id=tg_id, db=db, settings=settings, require_active=True)
    vpn_key = str((access_user or {}).get("vpn_key") or "")
    token = await users_repo.ensure_sub_token_for_tg(tg_id)
    if not _is_vpn_key_valid(vpn_key):
        raise AccessEnsureError("test_full_flow: vpn_key is invalid")
    if not users_repo.is_valid_sub_token(token):
        raise AccessEnsureError("test_full_flow: sub_token is invalid")

    from app.api.subscription import register_subscription_routes
    from app.services.subscription import build_subscription_service

    app = web.Application()
    app["subscription_service"] = build_subscription_service(db, settings)
    register_subscription_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()

    sockets = list(getattr(site._server, "sockets", []))  # noqa: SLF001
    if not sockets:
        await runner.cleanup()
        raise AccessEnsureError("test_full_flow: failed to bind local subscription server")
    port = sockets[0].getsockname()[1]
    sub_url = f"http://127.0.0.1:{port}/sub/{token}"

    try:
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
            async with session.get(sub_url) as response:
                body = await response.text()
                if response.status != 200:
                    raise AccessEnsureError(f"test_full_flow: /sub returned {response.status}")
                lines = [line.strip() for line in body.splitlines() if line.strip()]
                if not lines or lines[0] != vpn_key:
                    raise AccessEnsureError("test_full_flow: /sub payload does not contain vpn_key")
    finally:
        await runner.cleanup()

    return {
        "tg_id": tg_id,
        "vpn_key": vpn_key,
        "sub_token": token,
        "sub_url": sub_url,
    }
