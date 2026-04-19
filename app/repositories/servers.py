from __future__ import annotations

import logging

import aiosqlite

from app.config import Settings
from app.db.database import Database
from app.services.supabase import get_supabase_client
from app.services.vpn.base import ServerInfo

logger = logging.getLogger(__name__)


class ServersRepository:
    def __init__(self, db: Database) -> None:
        self.db_path = db.db_path
        self._supabase = get_supabase_client()

    async def list_active(self) -> list[ServerInfo]:
        rows = await self._list_supabase(active_only=True)
        if rows:
            return rows
        return await self._list_sqlite(active_only=True)

    async def list_all(self) -> list[ServerInfo]:
        rows = await self._list_supabase(active_only=False)
        if rows:
            return rows
        return await self._list_sqlite(active_only=False)

    async def set_active(self, server_id: int, is_active: bool) -> None:
        if self._supabase:
            try:
                self._supabase.table("servers").update({"is_active": is_active}).eq("id", server_id).execute()
                return
            except Exception:
                logger.exception("Supabase set_active failed, fallback to sqlite")
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("UPDATE servers SET is_active = ? WHERE id = ?", (1 if is_active else 0, server_id))
            await conn.commit()

    async def bootstrap_from_env_if_empty(self, settings: Settings) -> None:
        active_servers = await self.list_all()
        if active_servers:
            return
        if not settings.xui_base_url or not settings.xui_username or not settings.xui_password:
            return

        payload = {
            "name": "default",
            "host": settings.xui_public_host,
            "api_url": settings.xui_base_url,
            "username": settings.xui_username,
            "password": settings.xui_password,
            "inbound_id": settings.xui_inbound_id,
            "public_key": "",
            "short_id": "",
            "country": "default",
            "is_active": True,
            "sni": settings.xui_sni,
            "public_port": settings.xui_public_port,
            "ws_path": settings.xui_ws_path,
            "ws_host": settings.xui_public_host,
        }
        if self._supabase:
            try:
                self._supabase.table("servers").insert(payload).execute()
                return
            except Exception:
                logger.exception("Supabase bootstrap server failed, fallback to sqlite")

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO servers (
                    name, host, api_url, username, password, inbound_id, public_key,
                    short_id, country, is_active, sni, public_port, ws_path, ws_host
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"],
                    payload["host"],
                    payload["api_url"],
                    payload["username"],
                    payload["password"],
                    payload["inbound_id"],
                    payload["public_key"],
                    payload["short_id"],
                    payload["country"],
                    1,
                    payload["sni"],
                    payload["public_port"],
                    payload["ws_path"],
                    payload["ws_host"],
                ),
            )
            await conn.commit()

    async def _list_supabase(self, active_only: bool) -> list[ServerInfo]:
        if not self._supabase:
            return []
        try:
            query = self._supabase.table("servers").select(
                "id,name,host,api_url,username,password,inbound_id,public_key,short_id,country,is_active,sni,public_port,ws_path,ws_host"
            )
            if active_only:
                query = query.eq("is_active", True)
            response = query.execute()
            rows = response.data or []
            return [self._map_row(item) for item in rows if isinstance(item, dict)]
        except Exception:
            logger.exception("Supabase list servers failed")
            return []

    async def _list_sqlite(self, active_only: bool) -> list[ServerInfo]:
        where = "WHERE is_active = 1" if active_only else ""
        query = (
            "SELECT id,name,host,api_url,username,password,inbound_id,public_key,short_id,country,is_active,sni,public_port,ws_path,ws_host "
            f"FROM servers {where} ORDER BY id ASC"
        )
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query)
            rows = await cursor.fetchall()
        return [self._map_row(dict(row)) for row in rows]

    def _map_row(self, row: dict) -> ServerInfo:
        return ServerInfo(
            id=int(row["id"]),
            name=str(row.get("name") or f"server-{row['id']}"),
            host=str(row.get("host") or ""),
            api_url=str(row.get("api_url") or "").rstrip("/"),
            username=str(row.get("username") or ""),
            password=str(row.get("password") or ""),
            inbound_id=int(row.get("inbound_id") or 0),
            public_key=str(row.get("public_key") or ""),
            short_id=str(row.get("short_id") or ""),
            country=str(row.get("country") or "unknown"),
            is_active=bool(row.get("is_active")),
            sni=str(row.get("sni") or ""),
            public_port=int(row.get("public_port") or 443),
            ws_path=str(row.get("ws_path") or "/ws"),
            ws_host=str(row.get("ws_host") or ""),
        )
