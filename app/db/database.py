from __future__ import annotations

from pathlib import Path
import aiosqlite


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER NOT NULL UNIQUE,
                    balance INTEGER NOT NULL DEFAULT 0,
                    trial_used INTEGER NOT NULL DEFAULT 0,
                    promo_used INTEGER NOT NULL DEFAULT 0,
                    sub_token TEXT UNIQUE,
                    ref_id INTEGER NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ref_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    "key" TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    tariff_code TEXT NOT NULL,
                    email TEXT NULL,
                    payload TEXT NOT NULL UNIQUE,
                    telegram_payment_charge_id TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    api_url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    inbound_id INTEGER NOT NULL,
                    public_key TEXT,
                    short_id TEXT,
                    country TEXT NOT NULL DEFAULT 'unknown',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    sni TEXT,
                    public_port INTEGER NOT NULL DEFAULT 443,
                    ws_path TEXT NOT NULL DEFAULT '/ws',
                    ws_host TEXT
                );

                CREATE TABLE IF NOT EXISTS user_vpn (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    server_id INTEGER NOT NULL,
                    uuid TEXT NOT NULL,
                    protocol TEXT NOT NULL DEFAULT 'vless-reality',
                    config TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (server_id) REFERENCES servers(id),
                    UNIQUE(user_id, server_id, protocol)
                );
                """
            )
            cursor = await conn.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "trial_used" not in columns:
                await conn.execute("ALTER TABLE users ADD COLUMN trial_used INTEGER NOT NULL DEFAULT 0")
            if "promo_used" not in columns:
                await conn.execute("ALTER TABLE users ADD COLUMN promo_used INTEGER NOT NULL DEFAULT 0")
            if "sub_token" not in columns:
                await conn.execute("ALTER TABLE users ADD COLUMN sub_token TEXT")
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_sub_token ON users(sub_token)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_vpn_user_id ON user_vpn(user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_vpn_server_id ON user_vpn(server_id)")
            await conn.commit()

    async def connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        return conn
