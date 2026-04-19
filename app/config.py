from dataclasses import dataclass
import ipaddress
import os
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    bot_token: str
    db_path: str
    support_url: str
    referral_bonus_percent: int
    public_base_url: str
    supabase_url: str
    supabase_service_key: str
    xui_base_url: str
    xui_username: str
    xui_password: str
    xui_inbound_id: int
    xui_public_host: str
    xui_public_port: int
    xui_transport: str
    xui_security: str
    xui_sni: str
    xui_ws_path: str
    vpn_limit_ip: int
    vpn_total_gb: int
    vpn_default_expiry_days: int
    vpn_healthcheck_interval_seconds: int


def load_settings() -> Settings:
    load_dotenv()
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")
    xui_public_host = os.getenv("XUI_PUBLIC_HOST", "").strip()
    public_base_url = (
        os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    )
    if not public_base_url and xui_public_host:
        public_base_url = f"https://{xui_public_host}".rstrip("/")
    if public_base_url and xui_public_host:
        try:
            public_host = (urlparse(public_base_url).hostname or "").strip()
            ipaddress.ip_address(public_host)
        except ValueError:
            pass
        else:
            public_base_url = f"https://{xui_public_host}".rstrip("/")
    return Settings(
        bot_token=bot_token,
        db_path=os.getenv("DB_PATH", "./data/vpn_bot.sqlite3"),
        support_url=os.getenv("SUPPORT_URL", "https://t.me/"),
        referral_bonus_percent=int(os.getenv("REFERRAL_BONUS_PERCENT", "10")),
        public_base_url=public_base_url,
        supabase_url=os.getenv("SUPABASE_URL", "").strip(),
        supabase_service_key=os.getenv("SUPABASE_SERVICE_KEY", "").strip(),
        xui_base_url=os.getenv("XUI_BASE_URL", "").rstrip("/"),
        xui_username=os.getenv("XUI_USERNAME", "").strip(),
        xui_password=os.getenv("XUI_PASSWORD", "").strip(),
        xui_inbound_id=int(os.getenv("XUI_INBOUND_ID", "0")),
        xui_public_host=xui_public_host,
        xui_public_port=int(os.getenv("XUI_PUBLIC_PORT", "443")),
        xui_transport=os.getenv("XUI_TRANSPORT", "tcp").strip() or "tcp",
        xui_security=os.getenv("XUI_SECURITY", "tls").strip() or "tls",
        xui_sni=os.getenv("XUI_SNI", "").strip(),
        xui_ws_path=os.getenv("XUI_WS_PATH", "/ws").strip() or "/ws",
        vpn_limit_ip=max(1, int(os.getenv("VPN_LIMIT_IP", "1"))),
        vpn_total_gb=max(1, int(os.getenv("VPN_TOTAL_GB", "50"))),
        vpn_default_expiry_days=max(1, int(os.getenv("VPN_DEFAULT_EXPIRY_DAYS", "30"))),
        vpn_healthcheck_interval_seconds=max(10, int(os.getenv("VPN_HEALTHCHECK_INTERVAL_SECONDS", "120"))),
    )
