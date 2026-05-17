from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiohttp import ClientError
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.repositories.keys import KeysRepository
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
        # Exclude servers at max capacity (max_users=0 means unlimited).
        if server.max_users > 0 and user_counts.get(server.id, 0) >= server.max_users:
            logger.info(
                "server_id=%s excluded: at capacity %s/%s",
                server.id, user_counts.get(server.id, 0), server.max_users,
            )
            continue
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
    # Shared across all instances — updated by the healthcheck loop, read by provisioning and stats.
    _online_counts: dict[int, int] = {}

    def __init__(
        self,
        providers: dict[str, VPNProvider],
        servers_repo: ServersRepository,
        user_vpn_repo: UserVpnRepository,
        vpn_devices_repo: VpnDevicesRepository | None,
        settings: Settings,
        users_repo: UsersRepository | None = None,
        keys_repo: KeysRepository | None = None,
        bot: Bot | None = None,
    ) -> None:
        self._providers = providers
        self._servers_repo = servers_repo
        self._user_vpn_repo = user_vpn_repo
        self._vpn_devices_repo = vpn_devices_repo
        self._settings = settings
        self._users_repo = users_repo
        self._keys_repo = keys_repo
        self._bot = bot

    @classmethod
    def get_online_counts(cls) -> dict[int, int]:
        return dict(cls._online_counts)

    async def create_user_access(
        self,
        user_id: int,
        expiry_time: int | None = None,
        key_id: int | None = None,
        traffic_limit_gb: int | None = None,
    ) -> list[str]:
        """Provision a fresh VPN client for (user_id, key_id).

        key_id must be a pre-allocated ID from the keys table.
        NEVER returns cached configs — always provisions from scratch.
        NEVER falls back to existing rows for a different key.
        """
        if key_id is None:
            raise VPNManagerError("key_id is required — null-slot provisioning is not allowed")

        logger.debug("ACCESS FLOW | user_id=%s key_id=%s action=create", user_id, key_id)

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

        if claim == "ready":
            # Slot already has a ready row — return its configs instead of overwriting.
            # Overwriting would destroy a working VPN key.
            existing = await self._user_vpn_repo.get_user_vpn(user_id, key_id)
            configs = self._row_to_configs(existing)
            if configs:
                logger.info("VPN slot already ready, returning existing user_id=%s key_id=%s", user_id, key_id)
                return configs
            # Row exists but no valid configs yet — fall through and (re)provision.
            logger.warning(
                "VPN slot 'ready' but has no valid configs user_id=%s key_id=%s — re-provisioning",
                user_id, key_id,
            )

        logger.info("VPN creation claimed user_id=%s key_id=%s claim=%s", user_id, key_id, claim)
        try:
            return await self._create_on_best_server(user_id, expiry_time, key_id, traffic_limit_gb=traffic_limit_gb)
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

    async def disable_key_access(self, user_id: int, key_id: int) -> None:
        """Disable XUI clients for one specific key (primary + secondary servers) and delete its DB rows."""
        primary_row = await self._user_vpn_repo.get_user_vpn(user_id, key_id)
        secondary_rows = await self._user_vpn_repo.list_secondary_for_key(user_id, key_id)
        rows = [r for r in ([primary_row] + list(secondary_rows)) if r and int(r.get("server_id") or 0) > 0]
        if not rows:
            return
        servers = await self._servers_repo.list_all()
        provider = self._providers.get("xui")
        if provider is None:
            return
        for row in rows:
            server_id = int(row.get("server_id") or 0)
            row_key_id = row.get("key_id")
            server = next((s for s in servers if s.id == server_id), None)
            if not server:
                logger.warning(
                    "disable_key_access: server not found user_id=%s server_id=%s — deleting row",
                    user_id, server_id,
                )
                await self._user_vpn_repo.delete(user_id, row_key_id)
                continue
            for uuid in filter(None, [
                str(row.get("reality_uuid") or "").strip(),
                str(row.get("ws_uuid") or "").strip(),
            ]):
                try:
                    await provider.disable_client(server, uuid)
                    logger.info(
                        "disable_key_access: client disabled user_id=%s server_id=%s uuid=%s",
                        user_id, server.id, uuid,
                    )
                except Exception:
                    logger.exception(
                        "disable_key_access: disable_client failed user_id=%s server_id=%s uuid=%s",
                        user_id, server.id, uuid,
                    )
            await self._user_vpn_repo.delete(user_id, row_key_id)
            logger.info("disable_key_access: row deleted user_id=%s key_id=%s", user_id, row_key_id)

    async def disable_user_access(self, user_id: int) -> None:
        # Fetch all rows including secondary server slots (key_id >= 9_000_000_000).
        all_rows = await self._user_vpn_repo.list_all_for_user(user_id)
        rows = [r for r in all_rows if int(r.get("server_id") or 0) > 0]
        if not rows:
            return
        servers = await self._servers_repo.list_all()
        provider = self._providers.get("xui")
        if provider is None:
            return
        for row in rows:
            server_id = int(row.get("server_id") or 0)
            key_id = row.get("key_id")
            server = next((item for item in servers if item.id == server_id), None)
            if not server:
                logger.warning("VPN server not found user_id=%s server_id=%s — deleting DB row", user_id, server_id)
                await self._user_vpn_repo.delete(user_id, key_id)
                continue
            reality_uuid = str(row.get("reality_uuid") or "").strip()
            ws_uuid = str(row.get("ws_uuid") or "").strip()
            for uuid in [reality_uuid, ws_uuid]:
                if not uuid:
                    continue
                try:
                    await provider.disable_client(server, uuid)
                    logger.info("VPN client disabled user_id=%s server_id=%s uuid=%s", user_id, server.id, uuid)
                except Exception:
                    logger.exception("disable_client failed user_id=%s server_id=%s uuid=%s", user_id, server.id, uuid)
            await self._user_vpn_repo.delete(user_id, key_id)
            logger.info("VPN user_vpn row deleted user_id=%s key_id=%s", user_id, key_id)

    async def refresh_server_health(self) -> None:
        servers = await self._servers_repo.list_all()
        provider = self._providers.get("xui")
        if provider is None:
            return
        healthy = 0
        for server in servers:
            # Servers that were manually disabled (is_active=False, health_errors=0)
            # must never be auto-enabled by the health loop. Only health failures
            # (health_errors > 0) are eligible for auto-recovery.
            if not server.is_active and server.health_errors == 0:
                logger.info(
                    "server_id=%s name=%s is manually disabled — skipping health check",
                    server.id, server.name,
                )
                continue
            ok = await provider.is_healthy(server)
            new_errors = 0 if ok else server.health_errors + 1
            await self._servers_repo.update_health(
                server.id,
                is_active=ok,
                ok=ok,
                error_text=None if ok else "health check failed",
            )
            logger.info(
                "server health result server_id=%s name=%s ok=%s health_errors=%s",
                server.id, server.name, ok, new_errors,
            )
            if ok:
                healthy += 1
                try:
                    online = await provider.get_all_online_count(server)
                    VPNManager._online_counts[server.id] = online
                    logger.info("online_count server_id=%s name=%s online=%s", server.id, server.name, online)
                except Exception:
                    logger.warning("get_all_online_count failed server_id=%s", server.id)
            # Alert admin when a server first crosses the unhealthy threshold (errors==3).
            if not ok and server.health_errors == 2 and self._bot is not None:
                from app.config import load_settings
                _settings = self._settings
                for admin_id in (_settings.admin_ids or []):
                    try:
                        await self._bot.send_message(
                            admin_id,
                            f"🔴 <b>VPN сервер недоступен!</b>\n\n"
                            f"Сервер: <b>{server.name}</b> ({server.country})\n"
                            f"IP: <code>{server.host}</code>\n"
                            f"Ошибок подряд: {new_errors}\n\n"
                            f"Новые пользователи переключены на другие серверы.",
                        )
                    except Exception:
                        pass
            # Alert admin when server recovers.
            if ok and server.health_errors >= 3 and self._bot is not None:
                for admin_id in (self._settings.admin_ids or []):
                    try:
                        await self._bot.send_message(
                            admin_id,
                            f"🟢 <b>VPN сервер восстановлен</b>\n\n"
                            f"Сервер: <b>{server.name}</b> ({server.country})\n"
                            f"IP: <code>{server.host}</code>",
                        )
                    except Exception:
                        pass
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
            # XUI client has no per-client limit. Check the keys table first,
            # then fall back to the settings default. Never use users.traffic_limit_gb
            # because it accumulates across all purchases and conflates key limits.
            per_key_gb: int | None = None
            if self._keys_repo is not None and key_id is not None:
                try:
                    per_key_gb = await self._keys_repo.get_traffic_limit_gb(key_id, user_id)
                except Exception:
                    logger.warning("enforce_traffic_limit: keys_repo lookup failed user_id=%s key_id=%s", user_id, key_id)
            traffic_limit_gb = per_key_gb if per_key_gb and per_key_gb > 0 else self._settings.vpn_total_gb
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

        # Disable clients on secondary servers (e.g. PL when primary is NL, or vice versa).
        secondary_rows = await self._user_vpn_repo.list_secondary_for_key(user_id, key_id)
        for sec_row in secondary_rows:
            sec_server_id = int(sec_row.get("server_id") or 0)
            sec_server = next((s for s in servers if s.id == sec_server_id), None)
            if not sec_server:
                continue
            for uuid in filter(None, [
                str(sec_row.get("reality_uuid") or "").strip(),
                str(sec_row.get("ws_uuid") or "").strip(),
            ]):
                try:
                    await provider.disable_client(sec_server, uuid)
                    logger.info(
                        "secondary client disabled user_id=%s key_id=%s server_id=%s uuid=%s",
                        user_id, key_id, sec_server_id, uuid,
                    )
                except Exception as error:
                    logger.warning(
                        "secondary disable_client failed user_id=%s server_id=%s error=%s",
                        user_id, sec_server_id, error,
                    )

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

        Runs checks concurrently (up to 8 at a time) to keep the 120 s loop
        comfortably within budget even at 100+ users.
        Returns list of user_ids where at least one key was disabled.
        """
        try:
            vpn_rows = await self._user_vpn_repo.list_ready_vpn_rows()
        except Exception:
            logger.exception("enforce_all_users: failed to list ready vpn rows")
            return []

        logger.info("enforce_all_users: checking %s vpn rows", len(vpn_rows))

        sem = asyncio.Semaphore(8)

        async def _check(row: dict) -> tuple[int, bool]:
            user_id = int(row["user_id"])
            key_id = row.get("key_id")
            async with sem:
                try:
                    disabled = await self.enforce_traffic_limit(user_id, key_id=key_id)
                    return user_id, disabled
                except Exception:
                    logger.exception(
                        "enforce_traffic_limit unexpected error user_id=%s key_id=%s",
                        user_id, key_id,
                    )
                    return user_id, False

        results = await asyncio.gather(*[_check(row) for row in vpn_rows])
        return list({uid for uid, disabled in results if disabled})

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
        logger.debug("ACCESS FLOW | user_id=%s key_id=%s action=renew", user_id, key_id)
        primary_row = await self._user_vpn_repo.get_user_vpn(user_id, key_id)
        secondary_rows = await self._user_vpn_repo.list_secondary_for_key(user_id, key_id)
        rows = [r for r in ([primary_row] + secondary_rows) if r and r.get("server_id")]

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
            for uuid in filter(None, [reality_uuid, ws_uuid]):
                try:
                    ok = await provider.update_client_expiry(
                        server, uuid, expiry_time_ms,
                        total_gb=effective_traffic_limit_gb if effective_traffic_limit_gb > 0 else None,
                    )
                    if ok:
                        updated = True
                        logger.info(
                            "XUI expiry updated user_id=%s uuid=%s expiry_ms=%s total_gb=%s",
                            user_id, uuid, expiry_time_ms, effective_traffic_limit_gb or "unchanged",
                        )
                except Exception:
                    logger.exception("update_client_expiry failed user_id=%s uuid=%s", user_id, uuid)
        return updated

    async def _user_traffic_limit_gb(self, user_id: int) -> int:  # noqa: ARG002
        """Return per-key default traffic limit from settings.

        Do NOT read users.traffic_limit_gb — it accumulates across all purchases
        and would cause new keys to inherit the combined limit of all past keys.
        """
        return self._settings.vpn_total_gb

    def _default_expiry_ms(self) -> int:
        expires = utc_now() + timedelta(days=self._settings.vpn_default_expiry_days)
        return int(expires.timestamp() * 1000)

    async def _create_on_best_server(
        self, user_id: int, expiry_time: int | None, key_id: int | None = None,
        traffic_limit_gb: int | None = None,
    ) -> list[str]:
        await self._servers_repo.bootstrap_from_env_if_empty(self._settings)
        all_servers = await self._servers_repo.list_all()
        # Prefer real-time online counts if the healthcheck has populated them.
        # Fall back to registered-client DB counts on first startup before healthcheck runs.
        counts = VPNManager._online_counts if VPNManager._online_counts else await self._user_vpn_repo.count_users_by_server()
        candidates = pick_server(all_servers, counts, self._settings.vpn_circuit_break_minutes)
        active_count = sum(1 for s in all_servers if s.is_active)
        if not candidates:
            if active_count == 0:
                raise VPNManagerError("No active VPN servers configured — all servers are disabled")
            raise VPNManagerError(
                f"No healthy VPN servers available (active={active_count}, all unhealthy or at capacity)"
            )

        provider = self._providers.get("xui")
        if provider is None:
            raise VPNManagerError("VPN provider is not configured")

        effective_gb = (
            traffic_limit_gb
            if traffic_limit_gb and traffic_limit_gb > 0
            else await self._user_traffic_limit_gb(user_id)
        )
        limits = ClientLimits(
            limit_ip=self._settings.vpn_limit_ip,
            total_gb=effective_gb,
            expiry_time=expiry_time if expiry_time is not None else self._default_expiry_ms(),
        )
        last_error: Exception | None = None
        for server in candidates:
            if not server.is_active:
                logger.error(
                    "GUARD: inactive server reached provisioning user_id=%s server_id=%s name=%s — skipping",
                    user_id, server.id, server.name,
                )
                continue
            logger.info(
                "server assigned user_id=%s server_id=%s name=%s country=%s",
                user_id, server.id, server.name, server.country,
            )
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
                    traffic_limit_gb=effective_gb,
                )
                await self._servers_repo.update_health(server.id, is_active=True, ok=True, error_text=None)
                logger.info("VPN client created user_id=%s server_id=%s key_id=%s", user_id, server.id, key_id)
                # Return ONLY the newly created key's profiles.
                # Do NOT call get_subscription here — it returns all ready rows sorted
                # by created_at ASC, so vpn_configs[0] would be an old key's config.
                new_configs = self._profiles_to_subscription(result.profiles)
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
        traffic_limit_gb: int = 0,
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
                        # Client exists — re-enable if disabled, then ensure secondary DB row exists.
                        effective_sec_gb = traffic_limit_gb if traffic_limit_gb > 0 else self._settings.vpn_total_gb
                        try:
                            await provider.update_client_expiry(
                                server, existing_uuid, expiry_time,
                                total_gb=effective_sec_gb if effective_sec_gb > 0 else None,
                            )
                        except Exception as enable_err:
                            logger.warning(
                                "secondary re-enable failed user_id=%s server_id=%s error=%s",
                                user_id, server.id, enable_err,
                            )
                        try:
                            profiles = await provider.get_client_config(user_id, server, existing_uuid)
                            await self._save_additional_server_access(
                                user_id=user_id,
                                key_id=self._secondary_key_id(key_id, server.id),
                                server_id=server.id,
                                reality_uuid=existing_uuid,
                                ws_uuid=None,
                                profiles=profiles,
                            )
                            logger.info(
                                "secondary VPN row saved (existing client) user_id=%s server_id=%s",
                                user_id, server.id,
                            )
                        except Exception as save_err:
                            logger.warning(
                                "secondary row save failed (existing client) user_id=%s server_id=%s error=%s",
                                user_id, server.id, save_err,
                            )
                        continue
                    effective_sec_gb = traffic_limit_gb if traffic_limit_gb > 0 else self._settings.vpn_total_gb
                    result = await provider.add_client(server, user_id, reality_uuid, expiry_time, key_id=key_id, total_gb=effective_sec_gb)
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

    async def restore_user_keys(
        self,
        user_id: int,
        key_rows: list[dict],
        expiry_time_ms: int,
    ) -> tuple[int, int]:
        """Restore user_vpn rows and re-enable XUI clients after a ban wipe.

        key_rows: list of rows from the keys table (must have id, key, traffic_limit_gb).
        Returns (ok_count, failed_count).
        """
        import re

        servers = await self._servers_repo.list_all()
        active_servers = [s for s in servers if s.is_active]
        provider = self._providers.get("xui")
        if not isinstance(provider, XUIProvider) or not active_servers:
            return 0, len(key_rows)

        ok = failed = 0
        for key_row in key_rows:
            key_id = int(key_row.get("id") or 0)
            if not key_id:
                continue
            vless_url = str(key_row.get("key") or "")
            traffic_gb = int(key_row.get("traffic_limit_gb") or 0)
            if traffic_gb <= 0:
                traffic_gb = self._settings.vpn_total_gb

            # Parse UUID and host from stored vless:// URL.
            uuid_match = re.match(r"vless://([0-9a-f-]{36})@([^:]+):", vless_url)
            parsed_uuid = uuid_match.group(1) if uuid_match else None
            parsed_host = uuid_match.group(2) if uuid_match else None

            # Prefer the server whose host matches the stored URL; fall back to first active.
            target_server = next(
                (s for s in active_servers if parsed_host and s.host == parsed_host),
                active_servers[0],
            )

            limits = ClientLimits(
                limit_ip=self._settings.vpn_limit_ip,
                total_gb=traffic_gb,
                expiry_time=expiry_time_ms,
            )
            try:
                result = await provider.create_client(
                    user_id=user_id,
                    server=target_server,
                    limits=limits,
                    reality_uuid=parsed_uuid,
                    key_id=key_id,
                )
                await self._save_access(
                    user_id, result.server_id, result.reality_uuid,
                    result.ws_uuid, result.profiles, key_id,
                )
                logger.info(
                    "restore_user_keys: key restored user_id=%s key_id=%s server_id=%s",
                    user_id, key_id, target_server.id,
                )
                ok += 1
            except Exception:
                logger.exception(
                    "restore_user_keys: failed user_id=%s key_id=%s", user_id, key_id
                )
                failed += 1
        return ok, failed

    async def reenable_key_access(
        self,
        user_id: int,
        key_id: int,
        expiry_time_ms: int,
        traffic_limit_gb: int = 0,
    ) -> bool:
        """Re-enable a key's XUI clients. Updates expiry+enable; re-creates if missing.

        Returns True if at least one client was successfully updated or re-created.
        """
        primary_row = await self._user_vpn_repo.get_user_vpn(user_id, key_id)
        if not primary_row:
            return False

        await self._user_vpn_repo.set_status(user_id, "ready", key_id=key_id)

        updated = await self.update_user_expiry(
            user_id, expiry_time_ms, key_id=key_id, traffic_limit_gb=traffic_limit_gb
        )
        if updated:
            return True

        # Client not found in XUI — re-create from stored UUIDs.
        server_id = int(primary_row.get("server_id") or 0)
        reality_uuid = str(primary_row.get("reality_uuid") or "").strip()
        ws_uuid = str(primary_row.get("ws_uuid") or "").strip() or None
        if server_id <= 0 or not reality_uuid:
            return False

        servers = await self._servers_repo.list_all()
        server = next((s for s in servers if s.id == server_id), None)
        if not server or not server.is_active:
            return False

        provider = self._providers.get("xui")
        if not isinstance(provider, XUIProvider):
            return False

        effective_gb = traffic_limit_gb if traffic_limit_gb > 0 else self._settings.vpn_total_gb
        limits = ClientLimits(
            limit_ip=self._settings.vpn_limit_ip,
            total_gb=effective_gb,
            expiry_time=expiry_time_ms,
        )
        result = await provider.create_client(
            user_id=user_id,
            server=server,
            limits=limits,
            reality_uuid=reality_uuid,
            ws_uuid=ws_uuid,
            key_id=key_id,
        )
        await self._save_access(
            user_id, result.server_id, result.reality_uuid, result.ws_uuid, result.profiles, key_id
        )
        return True

    async def sync_secondary_servers_for_key(self, user_id: int, key_id: int) -> None:
        """Provision user on any active servers where they don't yet have a client for this key.

        Called in background on subscription requests so existing users automatically
        receive new servers (e.g. PL) without re-purchasing.
        """
        vpn_row = await self._user_vpn_repo.get_user_vpn(user_id, key_id)
        if not vpn_row:
            return
        primary_server_id = int(vpn_row.get("server_id") or 0)
        reality_uuid = str(vpn_row.get("reality_uuid") or "").strip()
        if not primary_server_id or not reality_uuid:
            return

        all_servers = await self._servers_repo.list_all()
        active_secondary = [s for s in all_servers if s.is_active and s.id != primary_server_id]
        if not active_secondary:
            return

        secondary_rows = await self._user_vpn_repo.list_secondary_for_key(user_id, key_id)
        provisioned_ids = {int(r.get("server_id") or 0) for r in secondary_rows}
        missing = [s for s in active_secondary if s.id not in provisioned_ids]
        if not missing:
            return

        expiry_ms = self._default_expiry_ms()
        traffic_limit_gb = 0
        if self._keys_repo is not None:
            try:
                key_row = await self._keys_repo.get_by_id_for_user(key_id, user_id)
                if key_row:
                    if key_row.get("expires_at"):
                        expiry_ms = int(parse_iso_utc(key_row["expires_at"]).timestamp() * 1000)
                    traffic_limit_gb = int(key_row.get("traffic_limit_gb") or 0)
            except Exception:
                pass

        logger.info(
            "sync_secondary: user_id=%s key_id=%s provisioning on %d missing server(s): %s",
            user_id, key_id, len(missing), [s.name for s in missing],
        )
        await self._provision_on_other_servers(
            user_id=user_id,
            primary_server_id=primary_server_id,
            reality_uuid=reality_uuid,
            expiry_time=expiry_ms,
            key_id=key_id,
            traffic_limit_gb=traffic_limit_gb,
        )
