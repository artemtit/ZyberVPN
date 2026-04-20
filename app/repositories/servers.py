from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

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
        await self.update_health(server_id=server_id, is_active=is_active, ok=is_active, error_text=None)

    async def update_health(self, server_id: int, is_active: bool, ok: bool, error_text: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self._supabase:
            try:
                current_errors = 0
                current_response = self._supabase.table("servers").select("health_errors").eq("id", server_id).limit(1).execute()
                current_rows = current_response.data or []
                if current_rows:
                    current_errors = int((current_rows[0] or {}).get("health_errors") or 0)
                payload = {
                    "is_active": is_active,
                    "last_health_check": now,
                    "last_error": error_text or "",
                    "health_errors": 0 if ok else current_errors + 1,
                }
                self._supabase.table("servers").update(payload).eq("id", server_id).execute()
                return
            except Exception:
                logger.exception("Supabase update_health failed, fallback to sqlite")

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                UPDATE servers
                SET is_active = ?,
                    last_health_check = ?,
                    health_errors = CASE WHEN ? = 1 THEN 0 ELSE health_errors + 1 END,
                    last_error = ?
                WHERE id = ?
                """,
                (1 if is_active else 0, now, 1 if ok else 0, error_text or "", server_id),
            )
            await conn.commit()

    async def bootstrap_from_env_if_empty(self, settings: Settings) -> None:
        existing = await self.list_all()
        if existing:
            return
        if not settings.xui_base_url or not settings.xui_username or not settings.xui_password:
            return

        parsed = urlparse(settings.xui_base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            logger.warning("XUI_BASE_URL is not localhost; secure local tunnel is recommended")

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
            "last_health_check": datetime.now(timezone.utc).isoformat(),
            "health_errors": 0,
            "last_error": "",
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
                    name, host, api_url, username, password, inbound_id, public_key, short_id,
                    country, is_active, sni, public_port, ws_path, ws_host,
                    last_health_check, health_errors, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    payload["last_health_check"],
                    payload["health_errors"],
                    payload["last_error"],
                ),
            )
            await conn.commit()

    async def _list_supabase(self, active_only: bool) -> list[ServerInfo]:
        if not self._supabase:
            return []
        try:
            query = self._supabase.table("servers").select(
                "id,name,host,api_url,username,password,inbound_id,public_key,short_id,country,is_active,sni,public_port,ws_path,ws_host,last_health_check,health_errors"
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
            "SELECT id,name,host,api_url,username,password,inbound_id,public_key,short_id,country,is_active,sni,public_port,ws_path,ws_host,last_health_check,health_errors "
            f"FROM servers {where} ORDER BY id ASC"
        )
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query)
            rows = await cursor.fetchall()
        return [self._map_row(dict(row)) for row in rows]

    def _map_row(self, row: dict) -> ServerInfo:
        raw_last = row.get("last_health_check")
        last_check = None
        if raw_last:
            try:
                last_check = datetime.fromisoformat(str(raw_last).replace("Z", "+00:00"))
            except Exception:
                last_check = None
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
            last_health_check=last_check,
            health_errors=int(row.get("health_errors") or 0),
        )
