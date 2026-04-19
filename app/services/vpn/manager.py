from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.repositories.servers import ServersRepository
from app.repositories.user_vpn import UserVpnRepository
from app.services.vpn.base import ClientLimits, CreateClientResult, ServerInfo, VPNProvider

logger = logging.getLogger(__name__)


class VPNManagerError(RuntimeError):
    pass


def pick_server(servers: list[ServerInfo], user_counts: dict[int, int]) -> ServerInfo:
    if not servers:
        raise VPNManagerError("No active VPN servers")
    return min(servers, key=lambda item: (user_counts.get(item.id, 0), item.id))


class VPNManager:
    def __init__(
        self,
        providers: dict[str, VPNProvider],
        servers_repo: ServersRepository,
        user_vpn_repo: UserVpnRepository,
        settings: Settings,
    ) -> None:
        self._providers = providers
        self._servers_repo = servers_repo
        self._user_vpn_repo = user_vpn_repo
        self._settings = settings

    async def create_user_access(self, user_id: int, expiry_time: int | None = None) -> list[str]:
        await self._servers_repo.bootstrap_from_env_if_empty(self._settings)
        active_servers = await self._servers_repo.list_active()
        if not active_servers:
            raise VPNManagerError("No active VPN servers available")

        existing = await self._user_vpn_repo.list_by_user(user_id)
        if existing:
            return await self.get_subscription(user_id)

        counts = await self._user_vpn_repo.count_users_by_server()
        chosen = pick_server(active_servers, counts)
        provider = self._providers.get("xui")
        if provider is None:
            raise VPNManagerError("VPN provider is not configured")

        limits = ClientLimits(
            limit_ip=self._settings.vpn_limit_ip,
            total_gb=self._settings.vpn_total_gb,
            expiry_time=expiry_time if expiry_time is not None else self._default_expiry_ms(),
        )
        result = await provider.create_client(user_id, chosen, limits)
        await self._save_profiles(user_id, result)
        return [item.config for item in result.profiles]

    async def get_subscription(self, user_id: int) -> list[str]:
        rows = await self._user_vpn_repo.list_by_user(user_id)
        if rows:
            return [str(row["config"]) for row in rows if row.get("config")]

        return await self.create_user_access(user_id)

    async def refresh_server_health(self) -> None:
        servers = await self._servers_repo.list_all()
        provider = self._providers.get("xui")
        if provider is None:
            return
        for server in servers:
            ok = await provider.is_healthy(server)
            if ok != bool(server.is_active):
                await self._servers_repo.set_active(server.id, ok)
                logger.info("Server health changed server_id=%s is_active=%s", server.id, ok)

    async def _save_profiles(self, user_id: int, result: CreateClientResult) -> None:
        for profile in result.profiles:
            await self._user_vpn_repo.upsert(
                user_id=user_id,
                server_id=result.server_id,
                uuid=result.uuid,
                protocol=profile.protocol,
                config=profile.config,
            )

    def _default_expiry_ms(self) -> int:
        expires = datetime.now(timezone.utc) + timedelta(days=self._settings.vpn_default_expiry_days)
        return int(expires.timestamp() * 1000)
