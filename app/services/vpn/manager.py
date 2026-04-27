from __future__ import annotations

import logging
from datetime import timedelta

from app.config import Settings
from app.repositories.servers import ServersRepository
from app.repositories.user_vpn import UserVpnRepository
from app.repositories.users import UsersRepository
from app.services.vpn.base import ClientLimits, ServerInfo, VPNProvider
from app.services.vpn.xui_provider import XUIProvider
from app.utils.datetime import ensure_utc, utc_diff, utc_now

logger = logging.getLogger(__name__)


class VPNManagerError(RuntimeError):
    pass


def _health_age_seconds(server: ServerInfo) -> int:
    if not server.last_health_check:
        return 10**9
    return int(utc_diff(utc_now(), ensure_utc(server.last_health_check)).total_seconds())


def pick_server(servers: list[ServerInfo], user_counts: dict[int, int], block_minutes: int) -> list[ServerInfo]:
    active = [item for item in servers if item.is_active]
    if not active:
        return []
    now = utc_now()
    candidates: list[ServerInfo] = []
    for server in active:
        if server.health_errors < 3:
            candidates.append(server)
            continue
        if not server.last_health_check:
            continue
        last = ensure_utc(server.last_health_check)
        if utc_diff(now, last) >= timedelta(minutes=block_minutes):
            candidates.append(server)
    return sorted(
        candidates,
        key=lambda item: (
            user_counts.get(item.id, 0),
            item.health_errors,
            _health_age_seconds(item),
            item.id,
        ),
    )


class VPNManager:
    def __init__(
        self,
        providers: dict[str, VPNProvider],
        servers_repo: ServersRepository,
        user_vpn_repo: UserVpnRepository,
        settings: Settings,
        users_repo: UsersRepository | None = None,
    ) -> None:
        self._providers = providers
        self._servers_repo = servers_repo
        self._user_vpn_repo = user_vpn_repo
        self._settings = settings
        self._users_repo = users_repo

    async def create_user_access(
        self,
        user_id: int,
        expiry_time: int | None = None,
    ) -> list[str]:
        """Return VPN configs for *user_id*, creating or repairing them as needed.

        State machine
        -------------
        ready    → return existing configs immediately (no network call)
        creating → another request owns the slot; raise VPNManagerError
        failed / absent → claim the slot, provision, then set ready or failed
        """
        vpn = await self._user_vpn_repo.get_user_vpn(user_id)

        if vpn:
            # Default to 'ready' for rows that pre-date the status column.
            status = vpn.get("status") or "ready"
            if status == "ready":
                configs = self._row_to_configs(vpn)
                if configs:
                    logger.info("VPN ready, returning cached configs user_id=%s", user_id)
                    return configs
                # status='ready' but configs empty/invalid — treat as failed.
            elif status == "creating":
                logger.info("VPN creation already in progress user_id=%s", user_id)
                raise VPNManagerError("VPN creation in progress")
            # status='failed' (or ready-but-empty): fall through to creation.

        # Atomically claim the creation slot via a single DB transaction.
        claim = await self._user_vpn_repo.claim_creating(user_id)

        if claim == "ready":
            # Another request finished between our read and the claim call.
            vpn = await self._user_vpn_repo.get_user_vpn(user_id)
            configs = self._row_to_configs(vpn) if vpn else []
            if configs:
                return configs

        if claim != "claimed":
            logger.info("VPN claim rejected claim=%s user_id=%s", claim, user_id)
            raise VPNManagerError("VPN creation in progress")

        logger.info("VPN creation claimed user_id=%s", user_id)
        try:
            # If the previous row had a valid server reference, try repair first.
            if vpn and int(vpn.get("server_id") or 0) > 0:
                configs = await self._validate_or_repair_existing_access(user_id, vpn, expiry_time)
                if configs:
                    return configs

            return await self._create_on_best_server(user_id, expiry_time)
        except Exception:
            await self._user_vpn_repo.set_failed(user_id)
            raise

    def _row_to_configs(self, row: dict | None) -> list[str]:
        if not row:
            return []
        reality = str(row.get("reality_config") or "").strip()
        ws = str(row.get("ws_config") or "").strip()
        output: list[str] = []
        if reality.startswith("vless://"):
            output.append(reality)
        if ws.startswith("vless://") and ws != reality:
            output.append(ws)
        return output

    async def get_existing_subscription(self, user_id: int) -> list[str]:
        row = await self._user_vpn_repo.get_user_vpn(user_id)
        if not row or (row.get("status") or "ready") != "ready":
            return []
        configs = self._row_to_configs(row)
        if configs:
            logger.info("VPN subscription returned existing configs user_id=%s count=%s", user_id, len(configs))
        return configs

    async def get_subscription(self, user_id: int, create_if_missing: bool = False) -> list[str]:
        existing = await self.get_existing_subscription(user_id)
        if existing:
            return existing
        if not create_if_missing:
            return []
        return await self.create_user_access(user_id)

    async def disable_user_access(self, user_id: int) -> None:
        row = await self._user_vpn_repo.get_user_vpn(user_id)
        if not row:
            return
        server_id = int(row.get("server_id") or 0)
        if server_id <= 0:
            return
        servers = await self._servers_repo.list_all()
        server = next((item for item in servers if item.id == server_id), None)
        if not server:
            return
        provider = self._providers.get("xui")
        if provider is None:
            return
        reality_uuid = str(row.get("reality_uuid") or "").strip()
        ws_uuid = str(row.get("ws_uuid") or "").strip()
        for uuid in [reality_uuid, ws_uuid]:
            if not uuid:
                continue
            try:
                await provider.disable_client(server, uuid)
                logger.info("VPN client disabled user_id=%s server_id=%s uuid=%s", user_id, server.id, uuid)
            except Exception:
                logger.exception("VPN disable failed user_id=%s server_id=%s uuid=%s", user_id, server.id, uuid)

        try:
            await self._user_vpn_repo.delete(user_id)
            logger.info("VPN user_vpn row deleted user_id=%s", user_id)
        except Exception:
            logger.exception("VPN user_vpn delete failed user_id=%s", user_id)

    async def refresh_server_health(self) -> None:
        servers = await self._servers_repo.list_all()
        provider = self._providers.get("xui")
        if provider is None:
            return
        healthy = 0
        for server in servers:
            ok = await provider.is_healthy(server)
            await self._servers_repo.update_health(
                server.id,
                is_active=ok,
                ok=ok,
                error_text=None if ok else "health check failed",
            )
            logger.info(
                "server health result server_id=%s name=%s ok=%s health_errors=%s",
                server.id, server.name, ok, 0 if ok else server.health_errors + 1,
            )
            if ok:
                healthy += 1
        logger.info("health check done healthy=%s unhealthy=%s total=%s", healthy, len(servers) - healthy, len(servers))

    async def get_metrics(self) -> dict:
        servers = await self._servers_repo.list_all()
        counts = await self._user_vpn_repo.count_users_by_server()
        active_servers = sum(1 for s in servers if s.is_active)
        return {
            "active_servers": active_servers,
            "total_servers": len(servers),
            "unhealthy_servers": sum(1 for s in servers if s.health_errors > 0),
            "active_vpn_users": sum(counts.values()),
        }

    async def get_client_stats(self, user_id: int) -> tuple[int, int]:
        """Return (total_bytes_used, online_device_count). Returns (0, 0) on any failure."""
        vpn = await self._user_vpn_repo.get_user_vpn(user_id)
        if not vpn:
            return 0, 0
        server_id = int(vpn.get("server_id") or 0)
        if server_id <= 0:
            return 0, 0
        servers = await self._servers_repo.list_all()
        server = next((s for s in servers if s.id == server_id), None)
        if not server:
            return 0, 0
        provider = self._providers.get("xui")
        if not isinstance(provider, XUIProvider):
            return 0, 0
        try:
            reality_email = f"{user_id}-reality"
            ws_email = f"{user_id}-ws"
            traffic = await provider.get_client_traffic(server, reality_email)
            bytes_used = 0
            if isinstance(traffic, dict):
                bytes_used = int(traffic.get("up", 0)) + int(traffic.get("down", 0))
            online = await provider.get_online_count(server, {reality_email, ws_email})
            return bytes_used, online
        except Exception:
            logger.exception("get_client_stats failed user_id=%s", user_id)
            return 0, 0

    async def enforce_traffic_limit(self, user_id: int) -> bool:
        """Disable VPN client if user exceeded traffic_limit_gb.

        Returns True if the client was disabled during this call.
        """
        if self._users_repo is None:
            return False

        vpn = await self._user_vpn_repo.get_user_vpn(user_id)
        if not vpn:
            return False
        if vpn.get("status") != "ready":
            logger.debug("skip user (already blocked) user_id=%s status=%s", user_id, vpn.get("status"))
            return False

        server_id = int(vpn.get("server_id") or 0)
        if server_id <= 0:
            return False

        servers = await self._servers_repo.list_all()
        server = next((s for s in servers if s.id == server_id), None)
        if not server:
            return False

        provider = self._providers.get("xui")
        if not isinstance(provider, XUIProvider):
            return False

        # Fetch live traffic — skip user if the 3x-ui API is unavailable
        try:
            reality_email = f"{user_id}-reality"
            traffic = await provider.get_client_traffic(server, reality_email)
            if not isinstance(traffic, dict):
                return False
            if not traffic.get("enable", True):
                # Client already disabled in xray; sync DB state and stop
                await self._user_vpn_repo.set_status(user_id, "limit_exceeded")
                return False
            bytes_used = int(traffic.get("up", 0)) + int(traffic.get("down", 0))
        except Exception as error:
            logger.warning("traffic fetch failed, skipping user_id=%s error=%s", user_id, error)
            return False

        user = await self._users_repo.get_by_tg_id(user_id)
        if not user:
            return False
        traffic_limit_gb = int(user.get("traffic_limit_gb") or 60)
        limit_bytes = traffic_limit_gb * 1024 ** 3

        if bytes_used < limit_bytes:
            return False

        logger.info(
            "limit exceeded user_id=%s used_gb=%.2f limit_gb=%s",
            user_id, bytes_used / 1024 ** 3, traffic_limit_gb,
        )

        reality_uuid = str(vpn.get("reality_uuid") or "").strip()
        ws_uuid = str(vpn.get("ws_uuid") or "").strip()

        if not reality_uuid:
            logger.warning("uuid missing, cannot disable user_id=%s", user_id)
            return False

        all_disabled = True
        for uuid in filter(None, [reality_uuid, ws_uuid]):
            for attempt in range(2):
                try:
                    await provider.disable_client(server, uuid)
                    logger.info("client disabled user_id=%s uuid=%s", user_id, uuid)
                    break
                except Exception as error:
                    if attempt == 0:
                        logger.warning(
                            "disable_client failed, retrying user_id=%s uuid=%s error=%s",
                            user_id, uuid, error,
                        )
                        await asyncio.sleep(1.0)
                    else:
                        logger.error(
                            "disable_client failed after retry user_id=%s uuid=%s error=%s",
                            user_id, uuid, error,
                        )
                        all_disabled = False

        if all_disabled:
            await self._user_vpn_repo.set_status(user_id, "limit_exceeded")

        return all_disabled

    async def enforce_all_users(self) -> list[int]:
        """Check traffic limits for all ready users. Returns list of newly disabled user IDs."""
        try:
            user_ids = await self._user_vpn_repo.list_ready_user_ids()
        except Exception:
            logger.exception("enforce_all_users: failed to list ready users")
            return []

        logger.info("enforce_all_users: checking %s users", len(user_ids))
        disabled: list[int] = []
        for user_id in user_ids:
            try:
                if await self.enforce_traffic_limit(user_id):
                    disabled.append(user_id)
            except Exception:
                logger.exception("enforce_traffic_limit unexpected error user_id=%s", user_id)
        return disabled

    async def update_user_expiry(self, user_id: int, expiry_time_ms: int) -> bool:
        """Update XUI client expiryTime after subscription renewal. Returns True on success."""
        vpn = await self._user_vpn_repo.get_user_vpn(user_id)
        if not vpn:
            return False
        server_id = int(vpn.get("server_id") or 0)
        if server_id <= 0:
            return False
        servers = await self._servers_repo.list_all()
        server = next((s for s in servers if s.id == server_id), None)
        if not server:
            return False
        provider = self._providers.get("xui")
        if not isinstance(provider, XUIProvider):
            return False
        reality_uuid = str(vpn.get("reality_uuid") or "").strip()
        ws_uuid = str(vpn.get("ws_uuid") or "").strip()
        updated = False
        for uuid in filter(None, [reality_uuid, ws_uuid]):
            try:
                ok = await provider.update_client_expiry(server, uuid, expiry_time_ms)
                if ok:
                    updated = True
                    logger.info("XUI expiry updated user_id=%s uuid=%s expiry_ms=%s", user_id, uuid, expiry_time_ms)
            except Exception:
                logger.exception("update_client_expiry failed user_id=%s uuid=%s", user_id, uuid)
        return updated

    def _default_expiry_ms(self) -> int:
        expires = utc_now() + timedelta(days=self._settings.vpn_default_expiry_days)
        return int(expires.timestamp() * 1000)

    async def _create_on_best_server(self, user_id: int, expiry_time: int | None) -> list[str]:
        await self._servers_repo.bootstrap_from_env_if_empty(self._settings)
        all_servers = await self._servers_repo.list_all()
        counts = await self._user_vpn_repo.count_users_by_server()
        candidates = pick_server(all_servers, counts, self._settings.vpn_circuit_break_minutes)
        if not candidates:
            raise VPNManagerError("No healthy VPN servers available")

        provider = self._providers.get("xui")
        if provider is None:
            raise VPNManagerError("VPN provider is not configured")

        limits = ClientLimits(
            limit_ip=self._settings.vpn_limit_ip,
            total_gb=self._settings.vpn_total_gb,
            expiry_time=expiry_time if expiry_time is not None else self._default_expiry_ms(),
        )
        last_error: Exception | None = None
        for server in candidates:
            try:
                result = await provider.create_client(user_id, server, limits)
                await self._save_access(user_id, result.server_id, result.reality_uuid, result.ws_uuid, result.profiles)
                await self._servers_repo.update_health(server.id, is_active=True, ok=True, error_text=None)
                logger.info("VPN client created user_id=%s server_id=%s", user_id, server.id)
                return self._profiles_to_subscription(result.profiles)
            except Exception as error:
                last_error = error
                logger.exception("VPN create failed user_id=%s server_id=%s", user_id, server.id)
                await self._servers_repo.update_health(server.id, is_active=False, ok=False, error_text=str(error)[:500])
        raise VPNManagerError("All VPN servers failed") from last_error

    async def _validate_or_repair_existing_access(self, user_id: int, row: dict, expiry_time: int | None) -> list[str]:
        server_id = int(row.get("server_id") or 0)
        if server_id <= 0:
            return []
        servers = await self._servers_repo.list_all()
        server = next((item for item in servers if item.id == server_id), None)
        if not server or not server.is_active:
            return []

        provider = self._providers.get("xui")
        if provider is None:
            return []
        reality_uuid = str(row.get("reality_uuid") or "").strip()
        ws_uuid = str(row.get("ws_uuid") or "").strip()
        ws_config = str(row.get("ws_config") or "").strip()
        needs_repair = False
        if not reality_uuid:
            needs_repair = True
        else:
            exists = await provider.client_exists(server, reality_uuid)
            needs_repair = not exists
        if ws_uuid and ws_config.startswith("vless://"):
            ws_exists = await provider.client_exists(server, ws_uuid)
            if not ws_exists:
                needs_repair = True
        if not needs_repair:
            return self._row_to_configs(row)

        logger.info("VPN client repair started user_id=%s server_id=%s", user_id, server.id)
        limits = ClientLimits(
            limit_ip=self._settings.vpn_limit_ip,
            total_gb=self._settings.vpn_total_gb,
            expiry_time=expiry_time if expiry_time is not None else self._default_expiry_ms(),
        )
        result = await provider.create_client(
            user_id=user_id,
            server=server,
            limits=limits,
            reality_uuid=reality_uuid or None,
            ws_uuid=ws_uuid or None,
        )
        await self._save_access(user_id, result.server_id, result.reality_uuid, result.ws_uuid, result.profiles)
        logger.info("VPN client repaired user_id=%s server_id=%s", user_id, server.id)
        return self._profiles_to_subscription(result.profiles)

    async def _save_access(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        profiles: list,
    ) -> None:
        reality = ""
        ws = ""
        for profile in profiles:
            if getattr(profile, "protocol", "") == "vless-reality":
                reality = str(getattr(profile, "config", "")).strip()
            if getattr(profile, "protocol", "") == "vless-ws-tls":
                ws = str(getattr(profile, "config", "")).strip()
        if not reality:
            raise VPNManagerError("Reality config is missing")
        await self._user_vpn_repo.set_ready(
            user_id=user_id,
            server_id=server_id,
            reality_uuid=reality_uuid,
            ws_uuid=ws_uuid,
            reality_config=reality,
            ws_config=ws,
        )

    def _profiles_to_subscription(self, profiles: list) -> list[str]:
        reality = ""
        ws = ""
        for profile in profiles:
            if getattr(profile, "protocol", "") == "vless-reality":
                reality = str(getattr(profile, "config", "")).strip()
            if getattr(profile, "protocol", "") == "vless-ws-tls":
                ws = str(getattr(profile, "config", "")).strip()
        output: list[str] = []
        if reality.startswith("vless://"):
            output.append(reality)
        if ws.startswith("vless://") and ws != reality:
            output.append(ws)
        return output
