from __future__ import annotations

import logging
from urllib.parse import urlparse

from app.config import Settings
from app.db.database import Database
from app.db.schema_contract import SERVER_COLUMNS
from app.services.supabase import execute_with_retry, get_supabase_client
from app.services.vpn.base import ServerInfo
from app.utils.datetime import parse_iso_utc, utc_now

logger = logging.getLogger(__name__)


class ServersRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def list_active(self) -> list[ServerInfo]:
        return await self._list_supabase(active_only=True)

    async def list_all(self) -> list[ServerInfo]:
        return await self._list_supabase(active_only=False)

    async def set_active(self, server_id: int, is_active: bool) -> None:
        await self.update_health(server_id=server_id, is_active=is_active, ok=is_active, error_text=None)

    async def update_health(self, server_id: int, is_active: bool, ok: bool, error_text: str | None) -> None:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        now = utc_now().isoformat()
        current_response = await execute_with_retry(
            lambda: self._supabase.table("servers").select("health_errors").eq("id", server_id).limit(1).execute(),
            operation="servers.update_health.read",
        )
        current_rows = current_response.data or []
        current_errors = int((current_rows[0] or {}).get("health_errors") or 0) if current_rows else 0
        payload = {
            "is_active": is_active,
            "last_health_check": now,
            "last_error": error_text or "",
            "health_errors": 0 if ok else current_errors + 1,
        }
        await execute_with_retry(
            lambda: self._supabase.table("servers").update(payload).eq("id", server_id).execute(),
            operation="servers.update_health.write",
        )

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
            "last_health_check": utc_now().isoformat(),
            "health_errors": 0,
            "last_error": "",
        }
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        await execute_with_retry(
            lambda: self._supabase.table("servers").insert(payload).execute(),
            operation="servers.bootstrap_from_env_if_empty",
        )

    async def _list_supabase(self, active_only: bool) -> list[ServerInfo]:
        if not self._supabase:
            return []
        select_query = ",".join(SERVER_COLUMNS)
        query = self._supabase.table("servers").select(select_query)
        if active_only:
            query = query.eq("is_active", True)
        try:
            response = await execute_with_retry(
                lambda: query.execute(),
                operation="servers.list",
            )
        except Exception as error:
            logger.error(
                "Supabase servers.list failed query=%s missing_column_likely=%s error=%s",
                select_query,
                "column" in str(error).lower(),
                error,
            )
            raise
        rows = response.data or []
        return [self._map_row(item) for item in rows if isinstance(item, dict)]

    def _map_row(self, row: dict) -> ServerInfo:
        raw_last = row.get("last_health_check")
        last_check = None
        if raw_last:
            try:
                last_check = parse_iso_utc(raw_last)
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
