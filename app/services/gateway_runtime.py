from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.db.database import Database
from app.repositories.external_upstreams import ExternalUpstreamsRepository
from app.repositories.servers import ServersRepository
from app.services.gateway_config import (
    GatewayConfigRenderer,
    GatewayConfigValidator,
    build_effective_gateway_config,
)
from app.services.vpn.base import ServerInfo
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)
_XUI_DB_PREFIX = "xui-db:"


@dataclass(slots=True)
class GatewayApplyResult:
    changed: bool
    config_hash: str


@dataclass(slots=True)
class GatewaySyncSummary:
    gateways_total: int = 0
    gateways_ready: int = 0
    gateways_failed: int = 0
    changed: int = 0
    upstream_id: int | None = None
    validation_status: str = "unknown"
    validation_error: str = ""


class GatewayRuntimeApplier:
    def __init__(
        self,
        *,
        file_reader=None,
        file_writer=None,
        restart_service=None,
        service_is_active=None,
        config_merger=None,
    ) -> None:
        self._file_reader = file_reader or self._default_file_reader
        self._file_writer = file_writer or self._default_file_writer
        self._restart_service = restart_service or self._default_restart_service
        self._service_is_active = service_is_active or self._default_service_is_active
        self._config_merger = config_merger or build_effective_gateway_config

    async def apply(
        self,
        *,
        server: ServerInfo,
        rendered_config: str,
        config_hash: str,
        force: bool = False,
    ) -> GatewayApplyResult:
        config_path = str(server.gateway_config_path or "").strip()
        service_name = str(server.gateway_service_name or "").strip()
        if not config_path:
            raise RuntimeError(f"Gateway server {server.id} has empty gateway_config_path")
        if not service_name:
            raise RuntimeError(f"Gateway server {server.id} has empty gateway_service_name")

        current = await asyncio.to_thread(self._file_reader, config_path)
        effective_config = await asyncio.to_thread(self._config_merger, current, rendered_config)
        changed = force or current != effective_config
        if changed:
            await asyncio.to_thread(self._file_writer, config_path, effective_config)
            try:
                await self._restart_service(service_name)
                is_active = await self._service_is_active(service_name)
            except Exception:
                await self._rollback_config(
                    config_path=config_path,
                    service_name=service_name,
                    previous_config=current,
                )
                raise
        else:
            is_active = await self._service_is_active(service_name)
        persisted = await asyncio.to_thread(self._file_reader, config_path)
        if persisted != effective_config:
            if changed:
                await self._rollback_config(
                    config_path=config_path,
                    service_name=service_name,
                    previous_config=current,
                )
            raise RuntimeError(f"Gateway persisted config drift detected for '{config_path}'")
        if not is_active:
            if changed:
                await self._rollback_config(
                    config_path=config_path,
                    service_name=service_name,
                    previous_config=current,
                )
            raise RuntimeError(f"Gateway service '{service_name}' is not active after apply")
        return GatewayApplyResult(changed=changed, config_hash=config_hash)

    @staticmethod
    def _default_file_reader(path: str) -> str:
        storage_kind, target_path, setting_key = _parse_config_target(path)
        if storage_kind == "xui-db":
            return _read_xui_db_setting(target_path, setting_key)
        try:
            return Path(target_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _default_file_writer(path: str, content: str) -> None:
        storage_kind, target_path, setting_key = _parse_config_target(path)
        if storage_kind == "xui-db":
            _write_xui_db_setting(target_path, setting_key, content)
            return
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)

    @staticmethod
    async def _default_restart_service(service_name: str) -> None:
        if os.name != "posix":
            raise RuntimeError("Gateway service reload requires a Linux host with systemctl")
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "restart",
            service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "systemctl restart failed")

    @staticmethod
    async def _default_service_is_active(service_name: str) -> bool:
        if os.name != "posix":
            return False
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "is-active",
            "--quiet",
            service_name,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _rollback_config(self, *, config_path: str, service_name: str, previous_config: str) -> None:
        try:
            await asyncio.to_thread(self._file_writer, config_path, previous_config)
            await self._restart_service(service_name)
        except Exception:
            logger.exception("Gateway rollback failed service=%s path=%s", service_name, config_path)


def _parse_config_target(path: str) -> tuple[str, str, str]:
    cleaned = str(path or "").strip()
    if cleaned.startswith(_XUI_DB_PREFIX):
        payload = cleaned[len(_XUI_DB_PREFIX):]
        db_path, _, setting_key = payload.partition("#")
        return "xui-db", db_path.strip(), (setting_key.strip() or "xrayTemplateConfig")
    return "file", cleaned, ""


def _read_xui_db_setting(db_path: str, setting_key: str) -> str:
    if not db_path:
        raise RuntimeError("xui-db target has empty database path")
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (setting_key,))
        row = cursor.fetchone()
        return str(row[0]) if row and row[0] is not None else ""
    finally:
        connection.close()


def _write_xui_db_setting(db_path: str, setting_key: str, content: str) -> None:
    if not db_path:
        raise RuntimeError("xui-db target has empty database path")
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.cursor()
        cursor.execute("UPDATE settings SET value = ? WHERE key = ?", (content, setting_key))
        if cursor.rowcount <= 0:
            raise RuntimeError(f"xui-db setting '{setting_key}' was not found in {db_path}")
        connection.commit()
    finally:
        connection.close()


class GatewayRuntimeManager:
    def __init__(
        self,
        *,
        servers_repo: ServersRepository,
        upstreams_repo: ExternalUpstreamsRepository,
        validator: GatewayConfigValidator,
        renderer: GatewayConfigRenderer,
        applier: GatewayRuntimeApplier,
    ) -> None:
        self._servers_repo = servers_repo
        self._upstreams_repo = upstreams_repo
        self._validator = validator
        self._renderer = renderer
        self._applier = applier
        self._lock = asyncio.Lock()

    async def sync(self, *, force: bool = False) -> GatewaySyncSummary:
        async with self._lock:
            return await self._sync_unlocked(force=force)

    async def _sync_unlocked(self, *, force: bool) -> GatewaySyncSummary:
        gateways = await self._servers_repo.list_gateways()
        summary = GatewaySyncSummary(gateways_total=len(gateways))
        if not gateways:
            return summary

        active_upstream = await self._upstreams_repo.get_active()
        if not active_upstream:
            await self._mark_gateways(gateways, status="idle", error="No active external upstream configured")
            summary.validation_status = "missing"
            summary.validation_error = "No active external upstream configured"
            summary.gateways_failed = len(gateways)
            return summary

        summary.upstream_id = int(active_upstream["id"])
        try:
            raw_text = self._resolve_upstream_raw_text(active_upstream)
        except Exception as error:
            summary.validation_status = "invalid"
            summary.validation_error = str(error)
            await self._upstreams_repo.update_validation(
                int(active_upstream["id"]),
                validation_status="invalid",
                validation_error=str(error),
            )
            await self._mark_gateways(gateways, status="error", error=str(error))
            summary.gateways_failed = len(gateways)
            return summary
        validation = self._validator.validate_text(raw_text)
        validation_error = "; ".join(validation.errors)
        summary.validation_status = "valid" if validation.is_valid else "invalid"
        summary.validation_error = validation_error
        await self._upstreams_repo.update_validation(
            int(active_upstream["id"]),
            validation_status=summary.validation_status,
            validation_error=validation_error,
            config_hash=validation.config_hash,
        )
        if not validation.is_valid or validation.payload is None:
            await self._mark_gateways(gateways, status="error", error=validation_error or "Gateway validation failed")
            summary.gateways_failed = len(gateways)
            return summary

        matching_gateways = [
            gateway
            for gateway in gateways
            if gateway.upstream_id in {None, int(active_upstream["id"])}
        ]
        mismatched_gateways = [gateway for gateway in gateways if gateway not in matching_gateways]
        if mismatched_gateways:
            await self._mark_gateways(
                mismatched_gateways,
                status="idle",
                error=f"Gateway upstream mismatch: active upstream id={active_upstream['id']}",
            )

        for gateway in matching_gateways:
            try:
                rendered = self._renderer.render(gateway, validation.payload)
                result = await self._applier.apply(
                    server=gateway,
                    rendered_config=rendered,
                    config_hash=validation.config_hash,
                    force=force,
                )
                await self._servers_repo.update_gateway_apply(
                    gateway.id,
                    status="ready",
                    error_text="",
                    applied_at=utc_now().isoformat(),
                )
                summary.gateways_ready += 1
                if result.changed:
                    summary.changed += 1
            except Exception as error:
                logger.exception("Gateway apply failed server_id=%s", gateway.id)
                await self._servers_repo.update_gateway_apply(
                    gateway.id,
                    status="error",
                    error_text=str(error),
                    applied_at=None,
                )
                summary.gateways_failed += 1

        if summary.gateways_ready > 0:
            await self._upstreams_repo.mark_applied(
                int(active_upstream["id"]),
                config_hash=validation.config_hash,
            )
        summary.gateways_failed += len(mismatched_gateways)
        return summary

    def _resolve_upstream_raw_text(self, upstream_row: dict) -> str:
        source_kind = str(upstream_row.get("source_kind") or "db").strip().lower()
        if source_kind == "file":
            source_path = str(upstream_row.get("source_path") or "").strip()
            if not source_path:
                raise RuntimeError(f"External upstream {upstream_row.get('id')} has empty source_path")
            try:
                return Path(source_path).read_text(encoding="utf-8")
            except FileNotFoundError as error:
                raise RuntimeError(f"External upstream file not found: {source_path}") from error
        raw_json = str(upstream_row.get("raw_json") or "")
        if not raw_json.strip():
            raise RuntimeError(f"External upstream {upstream_row.get('id')} has empty raw_json")
        return raw_json

    async def _mark_gateways(self, gateways: list[ServerInfo], *, status: str, error: str) -> None:
        for gateway in gateways:
            await self._servers_repo.update_gateway_apply(
                gateway.id,
                status=status,
                error_text=error,
                applied_at=None,
            )


def build_gateway_runtime_manager(db: Database, settings: Settings) -> GatewayRuntimeManager:  # noqa: ARG001
    return GatewayRuntimeManager(
        servers_repo=ServersRepository(db),
        upstreams_repo=ExternalUpstreamsRepository(db),
        validator=GatewayConfigValidator(),
        renderer=GatewayConfigRenderer(),
        applier=GatewayRuntimeApplier(),
    )
