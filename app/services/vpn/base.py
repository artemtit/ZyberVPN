from __future__ import annotations

from dataclasses import dataclass
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
    uuid: str
    email: str
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


class VPNProvider(Protocol):
    async def create_client(self, user_id: int, server: ServerInfo, limits: ClientLimits) -> CreateClientResult: ...

    async def delete_client(self, user_id: int, server: ServerInfo, client_uuid: str) -> None: ...

    async def get_client_config(self, user_id: int, server: ServerInfo, client_uuid: str) -> list[VpnProfile]: ...

    async def is_healthy(self, server: ServerInfo) -> bool: ...
