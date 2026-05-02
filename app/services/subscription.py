from __future__ import annotations

import logging
from urllib.parse import urlparse, urlunparse

from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager
from app.utils.datetime import parse_iso_utc, utc_now

logger = logging.getLogger(__name__)

# ISO-2 country code → (flag emoji, display name)
_COUNTRY_DISPLAY: dict[str, tuple[str, str]] = {
    "NL": ("🇳🇱", "Нидерланды"),
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
    """Derive a display name from the existing link fragment.

    Fragment format: ``ZyberVPN-{COUNTRY}-{TYPE}-{user_id}``
    Returns e.g. ``🇳🇱ZyberVPN | Netherlands``
    """
    fragment = urlparse(link).fragment
    parts = fragment.split("-")
    if len(parts) >= 2:
        country_code = parts[1].upper()
        flag, country_name = _COUNTRY_DISPLAY.get(country_code, ("", country_code))
        return f"{flag}ZyberVPN | {country_name}"
    return "ZyberVPN"


def _apply_display_name(link: str, name: str) -> str:
    parsed = urlparse(link)
    return urlunparse(parsed._replace(fragment=name))


class SubscriptionService:
    def __init__(self, users_repo: UsersRepository, vpn_manager) -> None:
        self._users_repo = users_repo
        self._vpn_manager = vpn_manager

    async def get_payload_by_token(self, token: str) -> dict:
        """Resolve subscription by token.

        Resolution order:
        1. keys.sub_token  → per-key subscription (new path)
        2. users.sub_token → user-level subscription (legacy backward compat)
        """
        from app.repositories.keys import KeysRepository
        from app.services.supabase import get_supabase_client
        _sb = get_supabase_client()
        if _sb:
            _kr = KeysRepository.__new__(KeysRepository)
            _kr._supabase = _sb
            try:
                key_row = await _kr.get_by_sub_token(token)
                if key_row:
                    tg_id = int(key_row["tg_id"])
                    key_id = key_row.get("id")
                    user = await self._users_repo.get_by_tg_id(tg_id)
                    if not user:
                        raise PermissionError("forbidden")
                    if self._is_expired(user.get("expires_at")):
                        raise PermissionError("subscription inactive")
                    return await self._build_key_payload(tg_id, key_id, user, key_row)
            except (PermissionError, LookupError):
                raise
            except Exception as error:
                logger.warning("key sub_token resolution failed token_prefix=%s error=%s", token[:8], error)

        # Fall back to user-level token (legacy)
        try:
            user = await self._users_repo.get_by_sub_token(token)
        except Exception as error:
            logger.error("users.get_by_sub_token failed error=%s", error)
            user = None
        if not user:
            raise PermissionError("forbidden")
        if self._is_expired(user.get("expires_at")):
            raise PermissionError("subscription inactive")
        tg_id = int(user["tg_id"])
        return await self._build_user_payload(tg_id, user)

    async def _build_key_payload(self, tg_id: int, key_id, user: dict, key_row: dict) -> dict:
        """Build subscription payload for a specific key_id (per-key subscription)."""
        from app.repositories.user_vpn import UserVpnRepository
        from app.services.supabase import get_supabase_client
        _sb = get_supabase_client()
        vpn_row = None
        if _sb and key_id is not None:
            _uvr = UserVpnRepository.__new__(UserVpnRepository)
            _uvr._supabase = _sb
            vpn_row = await _uvr.get_user_vpn(tg_id, int(key_id))
        configs: list[str] = []
        if vpn_row:
            reality = str(vpn_row.get("reality_config") or "").strip()
            ws = str(vpn_row.get("ws_config") or "").strip()
            if reality.startswith("vless://"):
                configs.append(reality)
            if ws.startswith("vless://") and ws != reality:
                configs.append(ws)
        if not configs:
            raise LookupError("vpn access not found for key")
        links = [
            _apply_display_name(line.strip(), _server_display_name(line.strip()))
            for line in configs
            if str(line).strip().startswith("vless://")
        ]
        if not links:
            raise LookupError("vpn access not found for key")
        download_bytes = 0
        try:
            bytes_used, _ = await self._vpn_manager.get_client_stats(
                tg_id, key_id=int(key_id) if key_id else None
            )
            download_bytes = bytes_used
        except Exception:
            pass
        traffic_limit_gb = int(user.get("traffic_limit_gb") or 60)
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
    return SubscriptionService(users_repo=users_repo, vpn_manager=vpn_manager)
