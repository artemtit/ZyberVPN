from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite

from app.db.database import Database
from app.services.supabase import get_supabase_client

logger = logging.getLogger(__name__)


class UserVpnRepository:
    def __init__(self, db: Database) -> None:
        self.db_path = db.db_path
        self._supabase = get_supabase_client()

    async def upsert(self, user_id: int, server_id: int, uuid: str, protocol: str, config: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "user_id": user_id,
            "server_id": server_id,
            "uuid": uuid,
            "protocol": protocol,
            "config": config,
            "created_at": now,
        }
        if self._supabase:
            try:
                self._supabase.table("user_vpn").upsert(payload, on_conflict="user_id,server_id,protocol").execute()
                return
            except Exception:
                logger.exception("Supabase upsert user_vpn failed, fallback to sqlite")

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO user_vpn (user_id, server_id, uuid, protocol, config, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, server_id, protocol)
                DO UPDATE SET uuid=excluded.uuid, config=excluded.config, created_at=excluded.created_at
                """,
                (user_id, server_id, uuid, protocol, config, now),
            )
            await conn.commit()

    async def list_by_user(self, user_id: int) -> list[dict]:
        rows = await self._list_supabase(user_id)
        if rows:
            return rows
        return await self._list_sqlite(user_id)

    async def count_users_by_server(self) -> dict[int, int]:
        rows = await self._count_supabase()
        if rows:
            return rows
        return await self._count_sqlite()

    async def _list_supabase(self, user_id: int) -> list[dict]:
        if not self._supabase:
            return []
        try:
            response = (
                self._supabase.table("user_vpn")
                .select("user_id,server_id,uuid,protocol,config,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .execute()
            )
            rows = response.data or []
            return [row for row in rows if isinstance(row, dict)]
        except Exception:
            logger.exception("Supabase list user_vpn failed")
            return []

    async def _list_sqlite(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT user_id, server_id, uuid, protocol, config, created_at
                FROM user_vpn
                WHERE user_id = ?
                ORDER BY datetime(created_at) DESC
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _count_supabase(self) -> dict[int, int]:
        if not self._supabase:
            return {}
        try:
            response = self._supabase.table("user_vpn").select("server_id,user_id").execute()
            rows = response.data or []
            counts: dict[int, int] = {}
            seen_users: set[tuple[int, int]] = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                server_id = int(row.get("server_id") or 0)
                user_id = int(row.get("user_id") or 0)
                if server_id <= 0 or user_id <= 0:
                    continue
                key = (server_id, user_id)
                if key in seen_users:
                    continue
                seen_users.add(key)
                counts[server_id] = counts.get(server_id, 0) + 1
            return counts
        except Exception:
            logger.exception("Supabase count user_vpn failed")
            return {}

    async def _count_sqlite(self) -> dict[int, int]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT server_id, COUNT(DISTINCT user_id) AS cnt
                FROM user_vpn
                GROUP BY server_id
                """
            )
            rows = await cursor.fetchall()
        return {int(row["server_id"]): int(row["cnt"]) for row in rows}
