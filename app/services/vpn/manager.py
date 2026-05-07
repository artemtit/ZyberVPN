from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiohttp import ClientError
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.repositories.servers import ServersRepository
from app.repositories.user_vpn import UserVpnRepository
from app.repositories.vpn_devices import VpnDevicesRepository
from app.repositories.users import UsersRepository
from app.services.vpn.base import ClientLimits, ServerInfo, VPNProvider
from app.services.vpn.xui_provider import XUIProvider
from app.utils.datetime import ensure_utc, parse_iso_utc, utc_diff, utc_now

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
        vpn_devices_repo: VpnDevicesRepository | None,
        settings: Settings,
        users_repo: UsersRepository | None = None,
        bot: Bot | None = None,
    ) -> None:
        self._providers = providers
        self._servers_repo = servers_repo
        self._user_vpn_repo = user_vpn_repo
        self._vpn_devices_repo = vpn_devices_repo
        self._settings = settings
        self._users_repo = users_repo
        self._bot = bot

    async def create_user_access(
        self,
        user_id: int,
        expiry_time: int | None = None,
        key_id: int | None = None,
    ) -> list[str]:
        """Provision a fresh VPN client for (user_id, key_id).

        key_id must be a pre-allocated ID from the keys table.
        NEVER returns cached configs — always provisions from scratch.
        NEVER falls back to existing rows for a different key.
        """
        if key_id is None:
            raise VPNManagerError("key_id is required — null-slot provisioning is not allowed")

        logger.warning("ACCESS FLOW | user_id=%s key_id=%s action=create", user_id, key_id)

        logger.warning(
            "FLOW TRACE | step=create_user_access.entry | user_id=%s key_id=%s",
            user_id, key_id,
        )

        # Evict stale "creating" rows before claiming the slot.
        # If the process crashed mid-provisioning, the row can stay in "creating" forever.
        existing = await self._user_vpn_repo.get_user_vpn(user_id, key_id)
        if existing and existing.get("status") == "creating":
            updated_at_raw = existing.get("updated_at") or existing.get("created_at") or ""
            stale = False
            if updated_at_raw:
                try:
                    age = utc_diff(utc_now(), ensure_utc(parse_iso_utc(updated_at_raw)))
                    stale = age.total_seconds() > 300  # 5 minutes
                except Exception:
                    pass
            if stale:
                logger.warning(
                    "Evicting stale 'creating' row user_id=%s key_id=%s age=%s",
                    user_id, key_id, age.total_seconds() if updated_at_raw else "unknown",
                )
                await self._user_vpn_repo.set_failed(user_id, key_id)

        # Reserve the exact key slot. Do not read or reuse existing VPN rows here.
        # A purchase create flow must always provision a fresh client for key_id.
        claim = await self._user_vpn_repo.claim_creating(user_id, key_id)
        if claim == "creating":
            logger.info("VPN claim rejected (creating) user_id=%s key_id=%s", user_id, key_id)
            raise VPNManagerError("VPN creation in progress")

        logger.info("VPN creation claimed user_id=%s key_id=%s claim=%s", user_id, key_id, claim)
        try:
            return await self._create_on_best_server(user_id, expiry_time, key_id)
        except Exception:
            await self._user_vpn_repo.set_failed(user_id, key_id)
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
        """Return configs from all ready per-key rows (null-slot excluded)."""
        return await self.get_subscription(user_id, create_if_missing=False)

    async def get_subscription(self, user_id: int, create_if_missing: bool = False) -> list[str]:
        """Return configs from all ready per-key rows for user_id.

        create_if_missing=True is intentionally disabled — creating a key requires
        an explicit key_id from ensure_user_access (payments flow). Callers that
        previously relied on this path should use ensure_user_access(force_new_key=True).
        """
        rows = await self._user_vpn_repo.list_user_vpns(user_id)
        configs: list[str] = []
        for row in rows:
            if (row.get("status") or "ready") == "ready":
                configs.extend(self._row_to_configs(row))
        if configs:
            logger.info("VPN subscription returned configs user_id=%s count=%s", user_id, len(configs))
        return configs

    async def disable_user_access(self, user_id: int) -> None:
        rows = await self._user_vpn_repo.list_user_vpns(user_id)
        rows = [r for r in rows if int(r.get("server_id") or 0) > 0]
        if not rows:
            return
        servers = await self._servers_repo.list_all()
        provider = self._providers.get("xui")
        if provider is None:
            return
        for row in rows:
            server_id = int(row.get("server_id") or 0)
            server = next((item for item in servers if item.id == server_id), None)
            if not server:
                logger.warning("VPN server not found user_id=%s server_id=%s — skipping disable", user_id, server_id)
                continue
            reality_uuid = str(row.get("reality_uuid") or "").strip()
            ws_uuid = str(row.get("ws_uuid") or "").strip()
            for uuid in [reality_uuid, ws_uuid]:
                if not uuid:
                    continue
                await provider.disable_client(server, uuid)
                logger.info("VPN client disabled user_id=%s server_id=%s uuid=%s", user_id, server.id, uuid)
            key_id = row.get("key_id")
            await self._user_vpn_repo.delete(user_id, key_id)
            logger.info("VPN user_vpn row deleted user_id=%s key_id=%s", user_id, key_id)

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

    async def get_client_stats(self, user_id: int, key_id: int | None = None) -> tuple[int, int]:
        """Return (total_bytes_used, unique_device_count_24h) for the given (user_id, key_id).

        key_id must be a real key ID. Returns (0, 0) if key_id is None or on any failure.
        """
        if key_id is None:
            logger.debug("get_client_stats called without key_id user_id=%s — skipped", user_id)
            return 0, 0
        vpn = await self._user_vpn_repo.get_user_vpn(user_id, key_id)
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
            reality_email = XUIProvider._client_email(user_id, "reality", key_id)
            ws_email = XUIProvider._client_email(user_id, "ws", key_id)
            bytes_used = 0
            reality_traffic = await provider.get_client_traffic(server, reality_email)
            if isinstance(reality_traffic, dict):
                bytes_used += int(reality_traffic.get("up", 0)) + int(reality_traffic.get("down", 0))
            ws_traffic = await provider.get_client_traffic(server, ws_email)
            if isinstance(ws_traffic, dict):
                bytes_used += int(ws_traffic.get("up", 0)) + int(ws_traffic.get("down", 0))
            unique_devices = 0
            if self._vpn_devices_repo is not None:
                unique_devices = await self._vpn_devices_repo.count_recent_devices(
                    user_id=user_id,
                    key_id=key_id,
                    window_hours=self._settings.xray_device_window_hours,
                )
            return bytes_used, unique_devices
        except Exception:
            logger.exception("get_client_stats failed user_id=%s key_id=%s", user_id, key_id)
            return 0, 0

    async def enforce_traffic_limit(self, user_id: int, key_id: int | None = None) -> bool:
        """Disable VPN client for (user_id, key_id) if traffic_limit_gb is exceeded.

        key_id must be a real key ID. Returns False immediately if key_id is None.
        Returns True if the client was disabled during this call.
        Checks all ready user_vpn rows (any key_id) so keyed users are covered.
        """
        if self._users_repo is None:
            return False
        if key_id is None:
            logger.debug("enforce_traffic_limit called without key_id user_id=%s — skipped", user_id)
            return False

        vpn = await self._user_vpn_repo.get_user_vpn(user_id, key_id=key_id)
        if not vpn:
            return False
        if vpn.get("status") != "ready":
            logger.debug(
                "skip (already blocked) user_id=%s key_id=%s status=%s",
                user_id, key_id, vpn.get("status"),
            )
            return False

        row_key_id = vpn.get("key_id")  # int or None
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

        try:
            reality_email = XUIProvider._client_email(user_id, "reality", key_id)
            ws_email = XUIProvider._client_email(user_id, "ws", key_id)
            reality_traffic = await provider.get_client_traffic(server, reality_email)
            if not isinstance(reality_traffic, dict):
                return False
            if not reality_traffic.get("enable", True):
                await self._user_vpn_repo.set_status(user_id, "limit_exceeded", key_id=key_id)
                return False
            # Sum reality + WS traffic so WS usage is not invisible to enforcement.
            bytes_used = int(reality_traffic.get("up", 0)) + int(reality_traffic.get("down", 0))
            ws_traffic = await provider.get_client_traffic(server, ws_email)
            if isinstance(ws_traffic, dict):
                bytes_used += int(ws_traffic.get("up", 0)) + int(ws_traffic.get("down", 0))
            # Use reality client's XUI totalGB as the single shared limit.
            xui_total_bytes = int(reality_traffic.get("total") or 0)
        except Exception as error:
            logger.warning(
                "traffic fetch failed, skipping user_id=%s key_id=%s error=%s",
                user_id, key_id, error,
            )
            return False

        if xui_total_bytes > 0:
            limit_bytes = xui_total_bytes
            traffic_limit_gb = xui_total_bytes // (1024 ** 3) or 1
        else:
            # XUI client has no limit set — fall back to global users field.
            user = await self._users_repo.get_by_tg_id(user_id) if self._users_repo else None
            traffic_limit_gb = int((user or {}).get("traffic_limit_gb") or 60)
            limit_bytes = traffic_limit_gb * 1024 ** 3

        if bytes_used < limit_bytes:
            return False

        logger.info(
            "limit exceeded user_id=%s key_id=%s used_gb=%.2f limit_gb=%s",
            user_id, key_id, bytes_used / 1024 ** 3, traffic_limit_gb,
        )

        reality_uuid = str(vpn.get("reality_uuid") or "").strip()
        ws_uuid = str(vpn.get("ws_uuid") or "").strip()

        if not reality_uuid:
            logger.warning("uuid missing, cannot disable user_id=%s key_id=%s", user_id, key_id)
            return False

        all_disabled = True
        for uuid in filter(None, [reality_uuid, ws_uuid]):
            for attempt in range(2):
                try:
                    await provider.disable_client(server, uuid)
                    logger.info("client disabled user_id=%s key_id=%s uuid=%s", user_id, key_id, uuid)
                    break
                except Exception as error:
                    if attempt == 0:
                        logger.warning(
                            "disable_client failed, retrying user_id=%s key_id=%s uuid=%s error=%s",
                            user_id, key_id, uuid, error,
                        )
                        await asyncio.sleep(1.0)
                    else:
                        logger.error(
                            "disable_client failed after retry user_id=%s key_id=%s uuid=%s error=%s",
                            user_id, key_id, uuid, error,
                        )
                        all_disabled = False

        if all_disabled:
            await self._user_vpn_repo.set_status(user_id, "limit_exceeded", key_id=key_id)
            if self._bot is not None:
                try:
                    await self._bot.send_message(
                        user_id,
                        "🚫 Ваш VPN-ключ заблокирован: исчерпан лимит трафика.\n\n"
                        "Для разблокировки продлите подписку.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(text="🔄 Продлить", callback_data="buy_open")]]
                        ),
                    )
                except TelegramForbiddenError:
                    pass
                except Exception as error:
                    logger.warning(
                        "traffic block notification failed user_id=%s key_id=%s error=%s",
                        user_id, key_id, error,
                    )

        return all_disabled

    async def enforce_all_users(self) -> list[int]:
        """Check traffic limits for all ready (user_id, key_id) rows.

        Returns list of user_ids where at least one key was disabled.
        """
        try:
            vpn_rows = await self._user_vpn_repo.list_ready_vpn_rows()
        except Exception:
            logger.exception("enforce_all_users: failed to list ready vpn rows")
            return []

        logger.info("enforce_all_users: checking %s vpn rows", len(vpn_rows))
        disabled_users: set[int] = set()
        for row in vpn_rows:
            user_id = row["user_id"]
            key_id = row.get("key_id")
            try:
                if await self.enforce_traffic_limit(user_id, key_id=key_id):
                    disabled_users.add(user_id)
            except Exception:
                logger.exception(
                    "enforce_traffic_limit unexpected error user_id=%s key_id=%s",
                    user_id, key_id,
                )
        return list(disabled_users)

    async def renew_user_access(
        self,
        user_id: int,
        expiry_time_ms: int,
        key_id: int | None,
        traffic_limit_gb: int | None = None,
    ) -> bool:
        """Renew exactly one existing key.

        Renewal must never fan out to all user_vpn rows because traffic/expiry are
        isolated per key. Pass traffic_limit_gb to update the XUI per-key limit.
        """
        if key_id is None:
            raise VPNManagerError("key_id is required for renew")
        return await self.update_user_expiry(
            user_id, expiry_time_ms, key_id=key_id, traffic_limit_gb=traffic_limit_gb
        )

    async def update_user_expiry(
        self,
        user_id: int,
        expiry_time_ms: int,
        key_id: int | None = None,
        traffic_limit_gb: int | None = None,
    ) -> bool:
        """Update XUI client expiryTime (and totalGB) after subscription renewal.

        key_id is required. Only that specific key's XUI clients are updated.
        traffic_limit_gb: per-key traffic allowance in GB. If None, XUI totalGB is unchanged.
        Returns True on success.
        """
        if key_id is None:
            raise VPNManagerError("key_id is required for expiry update")
        logger.warning("ACCESS FLOW | user_id=%s key_id=%s action=renew", user_id, key_id)
        rows = [await self._user_vpn_repo.get_user_vpn(user_id, key_id)]
        rows = [r for r in rows if r and r.get("key_id") is not None]

        if not rows:
            return False

        servers = await self._servers_repo.list_all()
        provider = self._providers.get("xui")
        if not isinstance(provider, XUIProvider):
            return False

        # Use the explicitly passed per-key limit; fallback: leave XUI totalGB unchanged (0 = no change).
        effective_traffic_limit_gb: int = traffic_limit_gb if traffic_limit_gb and traffic_limit_gb > 0 else 0

        updated = False
        for vpn in rows:
            server_id = int(vpn.get("server_id") or 0)
            if server_id <= 0:
                continue
            server = next((s for s in servers if s.id == server_id), None)
            if not server:
                continue
            reality_uuid = str(vpn.get("reality_uuid") or "").strip()
            ws_uuid = str(vpn.get("ws_uuid") or "").strip()
            row_updated = False
            for uuid in filter(None, [reality_uuid, ws_uuid]):
                try:
                    ok = await provider.update_client_expiry(
                        server, uuid, expiry_time_ms,
                        total_gb=effective_traffic_limit_gb if effective_traffic_limit_gb > 0 else None,
                    )
                    if ok:
                        updated = True
                        row_updated = True
                        logger.info(
                            "XUI expiry updated user_id=%s uuid=%s expiry_ms=%s total_gb=%s",
                            user_id, uuid, expiry_time_ms, effective_traffic_limit_gb or "unchanged",
                        )
                except Exception:
                    logger.exception("update_client_expiry failed user_id=%s uuid=%s", user_id, uuid)
            if row_updated:
                row_key_id = vpn.get("key_id")
                try:
                    await provider.reset_client_traffic(server, user_id, key_id=row_key_id)
                except Exception:
                    logger.warning(
                        "traffic reset failed user_id=%s server_id=%s key_id=%s",
                        user_id, server_id, row_key_id,
                    )
        return updated

    async def _user_traffic_limit_gb(self, user_id: int) -> int:
        """Return user's traffic_limit_gb from DB; fall back to vpn_total_gb setting."""
        if self._users_repo:
            try:
                user = await self._users_repo.get_by_tg_id(user_id)
                gb = int((user or {}).get("traffic_limit_gb") or 0)
                if gb > 0:
                    return gb
            except Exception:
                pass
        return self._settings.vpn_total_gb

    def _default_expiry_ms(self) -> int:
        expires = utc_now() + timedelta(days=self._settings.vpn_default_expiry_days)
        return int(expires.timestamp() * 1000)

    async def _create_on_best_server(
        self, user_id: int, expiry_time: int | None, key_id: int | None = None
    ) -> list[str]:
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
            total_gb=await self._user_traffic_limit_gb(user_id),
            expiry_time=expiry_time if expiry_time is not None else self._default_expiry_ms(),
        )
        last_error: Exception | None = None
        for server in candidates:
            try:
                result = await provider.create_client(user_id, server, limits, key_id=key_id)
                await self._save_access(
                    user_id, result.server_id, result.reality_uuid, result.ws_uuid, result.profiles, key_id
                )
                await self._provision_on_other_servers(
                    user_id=user_id,
                    primary_server_id=result.server_id,
                    reality_uuid=result.reality_uuid,
                    expiry_time=limits.expiry_time,
                    key_id=key_id,
                )
                await self._servers_repo.update_health(server.id, is_active=True, ok=True, error_text=None)
                logger.info("VPN client created user_id=%s server_id=%s key_id=%s", user_id, server.id, key_id)
                # Return ONLY the newly created key's profiles.
                # Do NOT call get_subscription here — it returns all ready rows sorted
                # by created_at ASC, so vpn_configs[0] would be an old key's config.
                new_configs = self._profiles_to_subscription(result.profiles)
                logger.warning(
                    "FLOW TRACE | step=_create_on_best_server | user_id=%s key_id=%s new_configs=%s",
                    user_id, key_id, new_configs,
                )
                return new_configs
            except (asyncio.TimeoutError, ClientError) as error:
                last_error = error
                logger.warning(
                    "VPN create transient error user_id=%s server_id=%s error=%s",
                    user_id, server.id, error,
                )
            except Exception as error:
                last_error = error
                logger.exception("VPN create failed user_id=%s server_id=%s", user_id, server.id)
                await self._servers_repo.update_health(server.id, is_active=False, ok=False, error_text=str(error)[:500])
        raise VPNManagerError("All VPN servers failed") from last_error

    async def _provision_on_other_servers(
        self,
        user_id: int,
        primary_server_id: int,
        reality_uuid: str,
        expiry_time: int,
        key_id: int | None,
    ) -> None:
        provider = self._providers.get("xui")
        if not isinstance(provider, XUIProvider):
            return
        try:
            servers = await self._servers_repo.list_all()
            for server in servers:
                if not server.is_active or server.id == primary_server_id:
                    continue
                try:
                    existing_uuid = await provider.get_client(server, user_id, key_id=key_id)
                    if existing_uuid:
                        continue
                    result = await provider.add_client(server, user_id, reality_uuid, expiry_time, key_id=key_id)
                    await self._save_additional_server_access(
                        user_id=user_id,
                        key_id=self._secondary_key_id(key_id, server.id),
                        server_id=server.id,
                        reality_uuid=result.reality_uuid,
                        ws_uuid=result.ws_uuid,
                        profiles=result.profiles,
                    )
                    logger.info("secondary VPN client created user_id=%s server_id=%s", user_id, server.id)
                except Exception as error:
                    logger.warning(
                        "secondary provisioning failed user_id=%s server_id=%s error=%s",
                        user_id,
                        server.id,
                        error,
                    )
        except Exception as error:
            logger.warning("secondary provisioning skipped user_id=%s error=%s", user_id, error)

    async def _validate_or_repair_existing_access(
        self, user_id: int, row: dict, expiry_time: int | None, key_id: int | None = None
    ) -> list[str]:
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
            total_gb=await self._user_traffic_limit_gb(user_id),
            expiry_time=expiry_time if expiry_time is not None else self._default_expiry_ms(),
        )
        result = await provider.create_client(
            user_id=user_id,
            server=server,
            limits=limits,
            reality_uuid=reality_uuid or None,
            ws_uuid=ws_uuid or None,
            key_id=key_id,
        )
        await self._save_access(
            user_id, result.server_id, result.reality_uuid, result.ws_uuid, result.profiles, key_id
        )
        logger.info("VPN client repaired user_id=%s server_id=%s", user_id, server.id)
        return self._profiles_to_subscription(result.profiles)

    async def _save_access(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        profiles: list,
        key_id: int | None = None,
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
        # Safety: ensure this UUID is not already stored for a DIFFERENT key_id of the same user.
        collision = await self._user_vpn_repo.uuid_exists_for_different_key(
            user_id, reality_uuid, key_id
        )
        if collision:
            raise VPNManagerError(
                f"UUID collision: reality_uuid={reality_uuid} already assigned to another "
                f"key of user_id={user_id}. Provisioning aborted."
            )
        await self._user_vpn_repo.set_ready(
            user_id=user_id,
            server_id=server_id,
            reality_uuid=reality_uuid,
            ws_uuid=ws_uuid,
            reality_config=reality,
            ws_config=ws,
            key_id=key_id,
        )

    async def _save_additional_server_access(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        profiles: list,
        key_id: int | None = None,
    ) -> None:
        reality = ""
        ws = ""
        for profile in profiles:
            if getattr(profile, "protocol", "") == "vless-reality":
                reality = str(getattr(profile, "config", "")).strip()
            if getattr(profile, "protocol", "") == "vless-ws-tls":
                ws = str(getattr(profile, "config", "")).strip()
        if not reality:
            return
        await self._user_vpn_repo.upsert_server_access(
            user_id=user_id,
            server_id=server_id,
            reality_uuid=reality_uuid,
            ws_uuid=ws_uuid,
            reality_config=reality,
            ws_config=ws,
            key_id=key_id,
        )

    @staticmethod
    def _secondary_key_id(key_id: int | None, server_id: int) -> int:
        """Store secondary server configs in dedicated key slots to avoid (user_id, key_id) collisions."""
        base = 9_000_000_000
        if key_id is None:
            return base + server_id
        return base + (key_id * 10_000) + server_id

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
