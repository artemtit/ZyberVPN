from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse, urlunparse

from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager
from app.utils.datetime import parse_iso_utc, utc_now

logger = logging.getLogger(__name__)

# Static VLESS links appended to every subscription (untracked external servers).
_STATIC_LINKS: list[str] = [
    (
        "vless://2c0c4a3f-1ba1-4ac4-8813-02f720b9c192@84.201.151.157:443"
        "?encryption=none&flow=xtls-rprx-vision&security=reality"
        "&sni=static.rutube.ru&fp=chrome"
        "&pbk=VAmZs6nifGz4osO4rc5z-Fo5NuKN1TOLJ79rOxcfMFk"
        "&sid=edceec74d404e2b0&spx=%2F&type=tcp"
        "#\U0001f1f7\U0001f1fa Авто | Обход"
    ),
    (
        "vless://2c0c4a3f-1ba1-4ac4-8813-02f720b9c192@84.201.151.157:443"
        "?encryption=none&security=reality"
        "&sni=static.rutube.ru&fp=chrome"
        "&pbk=pBN3EZ0V-elep_VRvP1JIN_CHmE1hb4GdeRH_-s89Bo"
        "&sid=2cdb5f5afa3506b5&spx=%2F&type=tcp"
        "#\U0001f1f7\U0001f1fa Авто | Обход-2"
    ),
    (
        "vless://2c0c4a3f-1ba1-4ac4-8813-02f720b9c192@84.201.151.157:443"
        "?encryption=none&security=reality"
        "&sni=static.rutube.ru&fp=chrome"
        "&pbk=396kN6u5K1Hkfm2JcLxjTM0cHjs4T5TwOukzwJir2wM"
        "&sid=b15b0935607a5977&spx=%2Fapi%2Fv7&type=xhttp&mode=auto"
        "#\U0001f1f7\U0001f1fa Авто | Обход-3"
    ),
]

# ISO-2 country code → (flag emoji, display name)
_COUNTRY_DISPLAY: dict[str, tuple[str, str]] = {
    "NL": ("🇳🇱", "Нидерланды"),
    "PL": ("🇵🇱", "Польша"),
    "DE": ("🇩🇪", "Германия"),
    "US": ("🇺🇸", "США"),
    "GB": ("🇬🇧", "Великобритания"),
    "FR": ("🇫🇷", "Франция"),
    "FI": ("🇫🇮", "Финляндия"),
    "SE": ("🇸🇪", "Швеция"),
    "CH": ("🇨🇭", "Швейцария"),
    "AT": ("🇦🇹", "Австрия"),
    "RU": ("🇷🇺", "Россия"),
}


def _server_display_name(link: str) -> str:
    """Derive a display name from the existing link fragment (fallback only).

    Fragment format: ``ZyberVPN-{COUNTRY}-{TYPE}-{user_id}``
    Returns e.g. ``🇳🇱ZyberVPN | Нидерланды``
    """
    fragment = urlparse(link).fragment
    parts = fragment.split("-")
    if len(parts) >= 2:
        country_code = parts[1].upper()
        flag, country_name = _COUNTRY_DISPLAY.get(country_code, ("", country_code))
        return f"{flag}ZyberVPN | {country_name}"
    return "ZyberVPN"


def _display_name_for_country(country_code: str) -> str:
    """Build display name from a known ISO-2 country code (authoritative source)."""
    flag, country_name = _COUNTRY_DISPLAY.get(country_code.upper(), ("", country_code.upper()))
    return f"{flag}ZyberVPN | {country_name}"


def _apply_display_name(link: str, name: str) -> str:
    parsed = urlparse(link)
    return urlunparse(parsed._replace(fragment=name))


class SubscriptionService:
    def __init__(self, users_repo: UsersRepository, vpn_manager, bot_username: str = "ZyberVPNBot") -> None:
        self._users_repo = users_repo
        self._vpn_manager = vpn_manager
        self._bot_username = bot_username

    async def get_payload_by_token(self, token: str) -> dict:
        """Resolve subscription strictly by keys.sub_token.

        No fallback to users.sub_token — each key has its own independent token.
        Raises PermissionError if token is unknown or subscription is inactive.
        """
        from app.repositories.keys import KeysRepository
        from app.services.supabase import get_supabase_client
        _sb = get_supabase_client()
        if not _sb:
            raise PermissionError("forbidden")
        _kr = KeysRepository.__new__(KeysRepository)
        _kr._supabase = _sb
        key_row = await _kr.get_by_sub_token(token)
        if not key_row:
            raise PermissionError("forbidden")
        tg_id = int(key_row["tg_id"])
        key_id = key_row.get("id")
        if key_id is None:
            raise PermissionError("forbidden")
        user = await self._users_repo.get_by_tg_id(tg_id)
        if not user:
            raise PermissionError("forbidden")
        # Check per-key expiry only. Do NOT fall back to users.expires_at —
        # that field reflects the latest purchase and is not representative of this key.
        # If keys.expires_at is not set, the key has no expiry (treat as valid).
        key_expires = key_row.get("expires_at")
        if key_expires and self._is_expired(key_expires):
            return self._build_expired_payload(key_row)
        return await self._build_key_payload(tg_id, int(key_id), user, key_row)

    def _build_expired_payload(self, key_row: dict) -> dict:
        """Return a subscription payload with informational non-connectable entries.

        VPN clients (Sing-box, V2Ray, Clash) display the fragment (#name) as the
        server label. We use RFC 5737 documentation addresses (192.0.2.x) which are
        guaranteed non-routable, so the user cannot accidentally connect through them.
        """
        _dummy = "vless://00000000-0000-0000-0000-000000000000@192.0.2.1"
        servers = [
            f"{_dummy}:1?type=tcp&security=none#⏳ Подписка истекла",
            f"{_dummy}:2?type=tcp&security=none#Купите новую в боте",
            f"{_dummy}:3?type=tcp&security=none#https://t.me/{self._bot_username}",
        ]
        expire_ts = 0
        try:
            expires_raw = key_row.get("expires_at")
            if expires_raw:
                expire_ts = int(parse_iso_utc(expires_raw).timestamp())
        except Exception:
            pass
        return {
            "remarks": "ZyberVPN",
            "upload": 0,
            "download": 0,
            "total": 0,
            "expire": expire_ts,
            "servers": servers,
        }

    async def _build_key_payload(self, tg_id: int, key_id: int, user: dict, key_row: dict) -> dict:
        """Build subscription payload for a specific key_id (per-key subscription).

        key_id is always a real integer — null-slot is never passed here.
        Includes configs from all servers (primary + secondary) in server-id order.
        """
        from app.repositories.user_vpn import UserVpnRepository
        from app.services.supabase import get_supabase_client
        _sb = get_supabase_client()
        # Track (config, server_id) so display name uses the real server country,
        # not the potentially stale country code embedded in the stored URL fragment.
        config_server_pairs: list[tuple[str, int]] = []
        seen: set[str] = set()

        def append_row_configs(row: dict) -> None:
            if (row.get("status") or "ready") != "ready":
                return
            server_id = int(row.get("server_id") or 0)
            reality = str(row.get("reality_config") or "").strip()
            ws = str(row.get("ws_config") or "").strip()
            for config in (reality, ws):
                if config.startswith("vless://") and config not in seen:
                    seen.add(config)
                    config_server_pairs.append((config, server_id))

        if _sb:
            _uvr = UserVpnRepository.__new__(UserVpnRepository)
            _uvr._supabase = _sb
            # Primary server row (server that holds the canonical config).
            vpn_row = await _uvr.get_user_vpn(tg_id, key_id)
            if vpn_row:
                append_row_configs(vpn_row)
            # Secondary server rows (additional servers provisioned for this key).
            for sec_row in await _uvr.list_secondary_for_key(tg_id, key_id):
                append_row_configs(sec_row)
        if not config_server_pairs:
            raise LookupError("vpn access not found for key")

        # Build server_id → country lookup from live server data.
        server_country: dict[int, str] = {}
        try:
            servers = await self._vpn_manager._servers_repo.list_all()
            server_country = {s.id: s.country for s in servers if s.country}
        except Exception:
            pass

        links = []
        for config, server_id in config_server_pairs:
            config = config.strip()
            if not config.startswith("vless://"):
                continue
            country = server_country.get(server_id, "")
            name = _display_name_for_country(country) if country else _server_display_name(config)
            links.append(_apply_display_name(config, name))
        if not links:
            raise LookupError("vpn access not found for key")

        links.extend(_STATIC_LINKS)

        # Provision any active servers the user is missing — runs in background so
        # this request stays fast; the new server appears on the next subscription poll.
        try:
            asyncio.create_task(
                self._vpn_manager.sync_secondary_servers_for_key(tg_id, int(key_id))
            )
        except Exception:
            pass

        download_bytes = 0
        try:
            bytes_used, _ = await self._vpn_manager.get_client_stats(tg_id, key_id=key_id)
            download_bytes = bytes_used
        except Exception:
            pass
        traffic_limit_gb = int(key_row.get("traffic_limit_gb") or 60)
        expire_ts = 0
        try:
            expires_raw = key_row.get("expires_at") or user.get("expires_at")
            if expires_raw:
                expire_ts = int(parse_iso_utc(expires_raw).timestamp())
        except Exception:
            pass
        return {
            "remarks": "ZyberVPN",
            "upload": 0,
            "download": download_bytes,
            "total": traffic_limit_gb * 1024 ** 3,
            "expire": expire_ts,
            "servers": links,
        }

    async def _build_user_payload(self, tg_id: int, user: dict) -> dict:
        """Build subscription payload for all user keys (legacy user-level token)."""
        try:
            configs = await self._vpn_manager.get_subscription(tg_id, create_if_missing=False)
        except Exception as error:
            logger.error("vpn.get_subscription failed tg_id=%s error=%s", tg_id, error)
            configs = []
        links = [
            _apply_display_name(line.strip(), _server_display_name(line.strip()))
            for line in configs
            if str(line).strip().startswith("vless://")
        ]
        if not links:
            raise LookupError("vpn access not found")
        links.extend(_STATIC_LINKS)
        download_bytes = 0
        try:
            bytes_used, _ = await self._vpn_manager.get_client_stats(tg_id)
            download_bytes = bytes_used
        except Exception:
            pass
        traffic_limit_gb = int(user.get("traffic_limit_gb") or 60)
        expire_ts = 0
        try:
            if user.get("expires_at"):
                expire_ts = int(parse_iso_utc(user["expires_at"]).timestamp())
        except Exception:
            pass
        return {
            "remarks": "ZyberVPN",
            "upload": 0,
            "download": download_bytes,
            "total": traffic_limit_gb * 1024 ** 3,
            "expire": expire_ts,
            "servers": links,
        }

    @staticmethod
    def _is_expired(expires_at: object) -> bool:
        if not expires_at:
            return False
        try:
            parsed_utc = parse_iso_utc(expires_at)
        except Exception:
            return True
        return parsed_utc <= utc_now()


def build_subscription_service(db, settings) -> SubscriptionService:
    users_repo = UsersRepository(db)
    vpn_manager = build_vpn_manager(db, settings)
    return SubscriptionService(
        users_repo=users_repo,
        vpn_manager=vpn_manager,
        bot_username=settings.bot_username,
    )
