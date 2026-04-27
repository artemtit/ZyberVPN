from __future__ import annotations

import logging
from urllib.parse import urlparse, urlunparse

from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager
from app.utils.datetime import parse_iso_utc, utc_now

logger = logging.getLogger(__name__)

# ISO-2 country code → (flag emoji, display name)
_COUNTRY_DISPLAY: dict[str, tuple[str, str]] = {
    "NL": ("🇳🇱", "Netherlands"),
    "DE": ("🇩🇪", "Germany"),
    "US": ("🇺🇸", "United States"),
    "GB": ("🇬🇧", "United Kingdom"),
    "FR": ("🇫🇷", "France"),
    "FI": ("🇫🇮", "Finland"),
    "SE": ("🇸🇪", "Sweden"),
    "CH": ("🇨🇭", "Switzerland"),
    "AT": ("🇦🇹", "Austria"),
    "RU": ("🇷🇺", "Russia"),
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

        # Traffic stats — best-effort, zero on failure
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
