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

    async def get_by_user(self, user_id: int) -> dict | None:
        row = await self._get_supabase(user_id)
        if row:
            return row
        return await self._get_sqlite(user_id)

    async def upsert(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        reality_config: str,
        ws_config: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "user_id": user_id,
            "server_id": server_id,
            "reality_uuid": reality_uuid,
            "ws_uuid": ws_uuid or "",
            "reality_config": reality_config,
            "ws_config": ws_config,
            "created_at": now,
            "updated_at": now,
        }
        if self._supabase:
            try:
                self._supabase.table("user_vpn").upsert(payload, on_conflict="user_id").execute()
                return
            except Exception:
                logger.exception("Supabase upsert user_vpn failed, fallback to sqlite")

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO user_vpn (
                    user_id, server_id, reality_uuid, ws_uuid, reality_config, ws_config, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                  server_id=excluded.server_id,
                  reality_uuid=excluded.reality_uuid,
                  ws_uuid=excluded.ws_uuid,
                  reality_config=excluded.reality_config,
                  ws_config=excluded.ws_config,
                  updated_at=excluded.updated_at
                """,
                (user_id, server_id, reality_uuid, ws_uuid or "", reality_config, ws_config, now, now),
            )
            await conn.commit()

    async def count_users_by_server(self) -> dict[int, int]:
        rows = await self._count_supabase()
        if rows:
            return rows
        return await self._count_sqlite()

    async def _get_supabase(self, user_id: int) -> dict | None:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("user_vpn")
                .select("user_id,server_id,reality_uuid,ws_uuid,reality_config,ws_config,created_at,updated_at")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception:
            logger.exception("Supabase get user_vpn failed")
            return None

    async def _get_sqlite(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT user_id, server_id, reality_uuid, ws_uuid, reality_config, ws_config, created_at, updated_at
                FROM user_vpn
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def _count_supabase(self) -> dict[int, int]:
        if not self._supabase:
            return {}
        try:
            response = self._supabase.table("user_vpn").select("server_id").execute()
            rows = response.data or []
            counts: dict[int, int] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                server_id = int(row.get("server_id") or 0)
                if server_id <= 0:
                    continue
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
                SELECT server_id, COUNT(*) AS cnt
                FROM user_vpn
                GROUP BY server_id
                """
            )
            rows = await cursor.fetchall()
        return {int(row["server_id"]): int(row["cnt"]) for row in rows}
