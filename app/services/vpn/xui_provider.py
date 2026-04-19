from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, CookieJar

from app.services.vpn.base import ClientLimits, CreateClientResult, ServerInfo, VPNProvider, VpnProfile

logger = logging.getLogger(__name__)


class XUIProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class InboundContext:
    port: int
    sni: str
    public_key: str
    short_id: str
    ws_path: str


class XUIProvider(VPNProvider):
    def __init__(self, timeout_seconds: int = 7) -> None:
        self._timeout = ClientTimeout(total=timeout_seconds)

    async def create_client(self, user_id: int, server: ServerInfo, limits: ClientLimits) -> CreateClientResult:
        email = str(user_id)
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            existing_uuid = self._find_existing_client_uuid(inbound, email)
            client_uuid = existing_uuid or str(uuid4())
            if not existing_uuid:
                await self._add_client(session, server, client_uuid, email, limits)
            profiles = self._build_profiles(server, inbound, client_uuid, user_id)
            return CreateClientResult(
                server_id=server.id,
                uuid=client_uuid,
                email=email,
                profiles=profiles,
            )

    async def delete_client(self, user_id: int, server: ServerInfo, client_uuid: str) -> None:
        async with self._session() as session:
            await self._login(session, server)
            url = f"{server.api_url}/panel/api/inbounds/delClient"
            payload = {"id": server.inbound_id, "clientId": client_uuid}
            response = await session.post(url, data=payload)
            if response.status != 200:
                body = await response.text()
                raise XUIProviderError(f"delClient failed status={response.status} body={body[:200]}")
            data = await response.json(content_type=None)
            if isinstance(data, dict) and data.get("success") is False:
                msg = data.get("msg") or "delClient returned success=false"
                raise XUIProviderError(str(msg))

    async def get_client_config(self, user_id: int, server: ServerInfo, client_uuid: str) -> list[VpnProfile]:
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            return self._build_profiles(server, inbound, client_uuid, user_id)

    async def is_healthy(self, server: ServerInfo) -> bool:
        try:
            async with self._session() as session:
                await self._login(session, server)
                await self._get_inbound(session, server)
            return True
        except Exception:
            logger.warning("xui healthcheck failed server_id=%s", server.id, exc_info=True)
            return False

    def _session(self) -> ClientSession:
        return ClientSession(timeout=self._timeout, cookie_jar=CookieJar(unsafe=True))

    async def _login(self, session: ClientSession, server: ServerInfo) -> None:
        url = f"{server.api_url}/login"
        response = await session.post(
            url,
            json={"username": server.username, "password": server.password},
        )
        if response.status != 200:
            body = await response.text()
            raise XUIProviderError(f"login failed status={response.status} body={body[:200]}")
        payload = await response.json(content_type=None)
        if isinstance(payload, dict) and payload.get("success") is False:
            msg = payload.get("msg") or "login rejected"
            raise XUIProviderError(str(msg))

    async def _get_inbound(self, session: ClientSession, server: ServerInfo) -> dict:
        url = f"{server.api_url}/panel/api/inbounds/list"
        response = await session.get(url)
        if response.status != 200:
            body = await response.text()
            raise XUIProviderError(f"inbounds/list failed status={response.status} body={body[:200]}")

        payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise XUIProviderError("inbounds/list returned invalid payload")
        inbounds = payload.get("obj")
        if not isinstance(inbounds, list):
            raise XUIProviderError("inbounds/list returned no inbounds")
        for inbound in inbounds:
            if not isinstance(inbound, dict):
                continue
            if str(inbound.get("id")) == str(server.inbound_id):
                return inbound
        raise XUIProviderError(f"inbound id={server.inbound_id} not found")

    async def _add_client(
        self,
        session: ClientSession,
        server: ServerInfo,
        client_uuid: str,
        email: str,
        limits: ClientLimits,
    ) -> None:
        url = f"{server.api_url}/panel/api/inbounds/addClient"
        payload = {
            "id": server.inbound_id,
            "settings": json.dumps(
                {
                    "clients": [
                        {
                            "id": client_uuid,
                            "email": email,
                            "flow": "xtls-rprx-vision",
                            "enable": True,
                            "limitIp": max(1, int(limits.limit_ip)),
                            "expiryTime": int(limits.expiry_time),
                            "totalGB": int(limits.total_gb) * 1024 * 1024 * 1024,
                        }
                    ]
                }
            ),
        }
        response = await session.post(url, data=payload)
        if response.status != 200:
            body = await response.text()
            raise XUIProviderError(f"addClient failed status={response.status} body={body[:200]}")
        data = await response.json(content_type=None)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise XUIProviderError(f"addClient returned error: {data}")

    def _find_existing_client_uuid(self, inbound: dict, email: str) -> str | None:
        raw = inbound.get("settings")
        if not raw:
            return None
        try:
            settings = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return None
        if not isinstance(settings, dict):
            return None
        clients = settings.get("clients")
        if not isinstance(clients, list):
            return None
        for client in clients:
            if not isinstance(client, dict):
                continue
            if str(client.get("email")) == email:
                value = str(client.get("id") or "").strip()
                if value:
                    return value
        return None

    def _build_profiles(self, server: ServerInfo, inbound: dict, client_uuid: str, user_id: int) -> list[VpnProfile]:
        context = self._extract_inbound_context(server, inbound)
        reality = self._build_reality_link(server, context, client_uuid, user_id)
        fallback = self._build_ws_tls_link(server, context, client_uuid, user_id)
        return [reality, fallback]

    def _extract_inbound_context(self, server: ServerInfo, inbound: dict) -> InboundContext:
        stream_raw = inbound.get("streamSettings") or {}
        stream_settings = json.loads(stream_raw) if isinstance(stream_raw, str) else stream_raw
        if not isinstance(stream_settings, dict):
            raise XUIProviderError("streamSettings invalid")

        reality = stream_settings.get("realitySettings") or stream_settings.get("securitySettings") or {}
        if not isinstance(reality, dict):
            reality = {}

        public_key = str(server.public_key or reality.get("publicKey") or "").strip()
        if not public_key:
            raise XUIProviderError("publicKey is empty, cannot build reality config")

        short_id = str(server.short_id or "").strip()
        if not short_id:
            short_ids = reality.get("shortIds")
            if isinstance(short_ids, list) and short_ids:
                short_id = str(short_ids[0]).strip()
            elif isinstance(short_ids, str):
                short_id = short_ids.strip()
        if not short_id:
            raise XUIProviderError("shortId is empty, cannot build reality config")

        port = int(inbound.get("port") or server.public_port or 443)
        server_names = reality.get("serverNames")
        inbound_sni = ""
        if isinstance(server_names, list) and server_names:
            inbound_sni = str(server_names[0]).strip()
        sni = str(server.sni or inbound_sni or server.host).strip()

        ws_settings = stream_settings.get("wsSettings") if isinstance(stream_settings.get("wsSettings"), dict) else {}
        ws_path = str(server.ws_path or ws_settings.get("path") or "/ws").strip() or "/ws"
        return InboundContext(
            port=port,
            sni=sni,
            public_key=public_key,
            short_id=short_id,
            ws_path=ws_path,
        )

    def _build_reality_link(self, server: ServerInfo, ctx: InboundContext, client_uuid: str, user_id: int) -> VpnProfile:
        host = server.host
        config = (
            f"vless://{client_uuid}@{host}:{ctx.port}"
            f"?security=reality"
            f"&encryption=none"
            f"&pbk={ctx.public_key}"
            f"&sid={ctx.short_id}"
            f"&fp=chrome"
            f"&type=tcp"
            f"&flow=xtls-rprx-vision"
            f"&sni={ctx.sni}"
            f"#ZyberVPN-{server.country}-REALITY-{user_id}"
        )
        return VpnProfile(
            protocol="vless-reality",
            config=config,
            server_name=server.name,
        )

    def _build_ws_tls_link(self, server: ServerInfo, ctx: InboundContext, client_uuid: str, user_id: int) -> VpnProfile:
        host = server.ws_host or server.host
        config = (
            f"vless://{client_uuid}@{host}:443"
            f"?security=tls"
            f"&encryption=none"
            f"&fp=chrome"
            f"&type=ws"
            f"&host={host}"
            f"&path={ctx.ws_path}"
            f"&sni={ctx.sni}"
            f"#ZyberVPN-{server.country}-WS-{user_id}"
        )
        return VpnProfile(
            protocol="vless-ws-tls",
            config=config,
            server_name=server.name,
        )
