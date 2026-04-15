from dataclasses import dataclass
import os

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


def load_settings() -> Settings:
    load_dotenv()
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")
    return Settings(
        bot_token=bot_token,
        db_path=os.getenv("DB_PATH", "./data/vpn_bot.sqlite3"),
        support_url=os.getenv("SUPPORT_URL", "https://t.me/"),
        referral_bonus_percent=int(os.getenv("REFERRAL_BONUS_PERCENT", "10")),
        public_base_url=(
            os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
            or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        ),
        supabase_url=os.getenv("SUPABASE_URL", "").strip(),
        supabase_service_key=os.getenv("SUPABASE_SERVICE_KEY", "").strip(),
        xui_base_url=os.getenv("XUI_BASE_URL", "").rstrip("/"),
        xui_username=os.getenv("XUI_USERNAME", "").strip(),
        xui_password=os.getenv("XUI_PASSWORD", "").strip(),
        xui_inbound_id=int(os.getenv("XUI_INBOUND_ID", "0")),
        xui_public_host=os.getenv("XUI_PUBLIC_HOST", "").strip(),
        xui_public_port=int(os.getenv("XUI_PUBLIC_PORT", "443")),
        xui_transport=os.getenv("XUI_TRANSPORT", "tcp").strip() or "tcp",
        xui_security=os.getenv("XUI_SECURITY", "tls").strip() or "tls",
        xui_sni=os.getenv("XUI_SNI", "").strip(),
    )
