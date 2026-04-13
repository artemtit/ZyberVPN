from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    bot_token: str
    db_path: str
    support_url: str
    referral_bonus_percent: int


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
    )
