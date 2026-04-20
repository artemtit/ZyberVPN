from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class ClientLimits:
    limit_ip: int = 1
    expiry_time: int = 0
    total_gb: int = 50


@dataclass(slots=True)
class VpnProfile:
    protocol: str
    config: str
    server_name: str


@dataclass(slots=True)
class CreateClientResult:
    server_id: int
    reality_uuid: str
    ws_uuid: str | None
    profiles: list[VpnProfile]


@dataclass(slots=True)
class ServerInfo:
    id: int
    name: str
    host: str
    api_url: str
    username: str
    password: str
    inbound_id: int
    public_key: str
    short_id: str
    country: str
    is_active: bool
    sni: str = ""
    public_port: int = 443
    ws_path: str = "/ws"
    ws_host: str = ""
    last_health_check: datetime | None = None
    health_errors: int = 0


class VPNProvider(Protocol):
    async def create_client(
        self,
        user_id: int,
        server: ServerInfo,
        limits: ClientLimits,
        reality_uuid: str | None = None,
        ws_uuid: str | None = None,
    ) -> CreateClientResult: ...

    async def delete_client(self, user_id: int, server: ServerInfo, client_uuid: str) -> None: ...

    async def get_client_config(self, user_id: int, server: ServerInfo, client_uuid: str) -> list[VpnProfile]: ...

    async def is_healthy(self, server: ServerInfo) -> bool: ...

    async def client_exists(self, server: ServerInfo, client_uuid: str) -> bool: ...

    async def disable_client(self, server: ServerInfo, client_uuid: str) -> None: ...
