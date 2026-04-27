from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar

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
    ws_supported: bool


class XUIProvider(VPNProvider):
    def __init__(self, timeout_seconds: int = 5, retries: int = 3) -> None:
        self._timeout = ClientTimeout(total=timeout_seconds)
        self._retries = min(3, max(1, retries))

    async def create_client(
        self,
        user_id: int,
        server: ServerInfo,
        limits: ClientLimits,
        reality_uuid: str | None = None,
        ws_uuid: str | None = None,
    ) -> CreateClientResult:
        self._validate_server_security(server)
        reality_email = f"{user_id}-reality"
        ws_email = f"{user_id}-ws"
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            ctx = self._extract_inbound_context(server, inbound)

            existing_reality = self._find_existing_client_uuid(inbound, reality_email)
            final_reality_uuid = reality_uuid or existing_reality or str(uuid4())
            clients_added = 0
            if not existing_reality:
                await self._add_client(session, server, final_reality_uuid, reality_email, limits)
                clients_added += 1
                logger.info("xui reality client created user_id=%s server_id=%s", user_id, server.id)

            final_ws_uuid: str | None = None
            if ctx.ws_supported:
                existing_ws = self._find_existing_client_uuid(inbound, ws_email)
                final_ws_uuid = ws_uuid or existing_ws or str(uuid4())
                if not existing_ws:
                    await self._add_client(session, server, final_ws_uuid, ws_email, limits)
                    clients_added += 1
                    logger.info("xui ws client created user_id=%s server_id=%s", user_id, server.id)

            if clients_added > 0:
                await self._reload_xray(session, server)
                await self._verify_client_visible(session, server, final_reality_uuid)

            profiles = self._build_profiles(server, ctx, final_reality_uuid, final_ws_uuid, user_id)
            return CreateClientResult(
                server_id=server.id,
                reality_uuid=final_reality_uuid,
                ws_uuid=final_ws_uuid,
                profiles=profiles,
            )

    async def delete_client(self, user_id: int, server: ServerInfo, client_uuid: str) -> None:
        self._validate_server_security(server)
        async with self._session() as session:
            await self._login(session, server)
            url = f"{server.api_url}/panel/api/inbounds/delClient"
            payload = {"id": server.inbound_id, "clientId": client_uuid}
            data = await self._request_json(session, "post", url, data=payload)
            if isinstance(data, dict) and data.get("success") is False:
                raise XUIProviderError(str(data.get("msg") or "delClient rejected"))

    async def disable_client(self, server: ServerInfo, client_uuid: str) -> None:
        self._validate_server_security(server)
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            settings_raw = inbound.get("settings")
            settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
            if not isinstance(settings, dict):
                raise XUIProviderError("inbound settings invalid")
            clients = settings.get("clients")
            if not isinstance(clients, list):
                raise XUIProviderError("inbound clients invalid")
            changed = False
            for client in clients:
                if isinstance(client, dict) and str(client.get("id")) == client_uuid:
                    client["enable"] = False
                    changed = True
                    break
            if not changed:
                return
            update_url = f"{server.api_url}/panel/api/inbounds/update/{server.inbound_id}"
            payload = {
                "id": server.inbound_id,
                "settings": json.dumps({"clients": clients}),
            }
            await self._request_json(session, "post", update_url, data=payload)

    async def client_exists(self, server: ServerInfo, client_uuid: str) -> bool:
        self._validate_server_security(server)
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            return self._find_client_by_uuid(inbound, client_uuid)

    async def get_client_config(self, user_id: int, server: ServerInfo, client_uuid: str) -> list[VpnProfile]:
        self._validate_server_security(server)
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            ctx = self._extract_inbound_context(server, inbound)
            return self._build_profiles(server, ctx, client_uuid, None, user_id)

    async def is_healthy(self, server: ServerInfo) -> bool:
        try:
            self._validate_server_security(server)
            port = server.public_port or 443
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(server.host, port), timeout=5.0
                )
                writer.close()
                await writer.wait_closed()
            except Exception as tcp_err:
                logger.warning(
                    "xui port unreachable server_id=%s host=%s port=%s error=%s",
                    server.id, server.host, port, tcp_err,
                )
                return False
            async with self._session() as session:
                await self._login(session, server)
                inbound = await self._get_inbound(session, server)
                self._validate_inbound_clients_readable(inbound)
            logger.info("xui healthcheck ok server_id=%s host=%s port=%s", server.id, server.host, port)
            return True
        except Exception as error:
            logger.warning("xui healthcheck failed server_id=%s error=%s", server.id, error)
            return False

    def _validate_server_security(self, server: ServerInfo) -> None:
        parsed = urlparse(server.api_url)
        host = (parsed.hostname or "").strip().lower()
        if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost"}:
            raise XUIProviderError("Insecure XUI api_url over HTTP is blocked; use localhost tunnel")

    def _session(self) -> ClientSession:
        return ClientSession(timeout=self._timeout, cookie_jar=CookieJar(unsafe=True))

    async def _request_json(self, session: ClientSession, method: str, url: str, **kwargs) -> dict | list:
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                response = await session.request(method=method, url=url, **kwargs)
                if response.status != 200:
                    raise XUIProviderError(f"{method.upper()} request failed status={response.status}")
                return await response.json(content_type=None)
            except (asyncio.TimeoutError, ClientError, XUIProviderError, ValueError) as error:
                last_error = error
                if attempt >= self._retries:
                    break
                await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
        raise XUIProviderError(f"Request failed after retries: {method.upper()} {url}") from last_error

    async def _login(self, session: ClientSession, server: ServerInfo) -> None:
        url = f"{server.api_url}/login"

        async with session.post(
            url,
            json={
                "username": server.username,
                "password": server.password,
            },
        ) as resp:
            payload = await resp.json()

        if isinstance(payload, dict) and payload.get("success") is False:
            raise XUIProviderError(str(payload.get("msg") or "login rejected"))

    async def _get_inbound(self, session: ClientSession, server: ServerInfo) -> dict:
        url = f"{server.api_url}/panel/api/inbounds/list"
        payload = await self._request_json(session, "get", url)
        if not isinstance(payload, dict):
            raise XUIProviderError("inbounds/list returned invalid payload")
        inbounds = payload.get("obj")
        if not isinstance(inbounds, list):
            raise XUIProviderError("inbounds/list returned no inbounds")
        for inbound in inbounds:
            if isinstance(inbound, dict) and str(inbound.get("id")) == str(server.inbound_id):
                return inbound
        raise XUIProviderError(f"inbound id={server.inbound_id} not found")

    def _validate_inbound_clients_readable(self, inbound: dict) -> None:
        settings_raw = inbound.get("settings")
        settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
        if not isinstance(settings, dict):
            raise XUIProviderError("inbound settings unreadable")
        clients = settings.get("clients")
        if clients is not None and not isinstance(clients, list):
            raise XUIProviderError("inbound clients unreadable")

    async def get_client_traffic(self, server: ServerInfo, email: str) -> dict | None:
        """Fetch traffic stats for a client email from 3x-ui panel."""
        self._validate_server_security(server)
        async with self._session() as session:
            await self._login(session, server)
            url = f"{server.api_url}/panel/api/inbounds/getClientTraffics/{email}"
            try:
                data = await self._request_json(session, "get", url)
                if isinstance(data, dict) and data.get("success") is True:
                    return data.get("obj")
            except Exception as error:
                logger.debug("get_client_traffic failed server_id=%s email=%s error=%s", server.id, email, error)
            return None

    async def get_online_count(self, server: ServerInfo, emails: set[str]) -> int:
        """Count how many of the given emails are currently online."""
        self._validate_server_security(server)
        async with self._session() as session:
            await self._login(session, server)
            url = f"{server.api_url}/panel/api/inbounds/onlines"
            try:
                data = await self._request_json(session, "post", url)
                if isinstance(data, dict) and data.get("success") is True:
                    online_list = data.get("obj") or []
                    if isinstance(online_list, list):
                        return sum(1 for e in online_list if e in emails)
            except Exception as error:
                logger.debug("get_online_count failed server_id=%s error=%s", server.id, error)
            return 0

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
                            "limitIp": 3,
                            "expiryTime": int(limits.expiry_time),
                            "totalGB": int(limits.total_gb) * 1024 * 1024 * 1024,
                        }
                    ]
                }
            ),
        }
        data = await self._request_json(session, "post", url, data=payload)
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
            if isinstance(client, dict) and str(client.get("email")) == email:
                value = str(client.get("id") or "").strip()
                if value:
                    return value
        return None

    def _find_client_by_uuid(self, inbound: dict, client_uuid: str) -> bool:
        raw = inbound.get("settings")
        if not raw:
            return False
        try:
            settings = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return False
        if not isinstance(settings, dict):
            return False
        clients = settings.get("clients")
        if not isinstance(clients, list):
            return False
        for client in clients:
            if isinstance(client, dict) and str(client.get("id")) == client_uuid:
                # Disabled clients serve dead configs — treat as non-existent.
                return bool(client.get("enable", True))
        return False

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

        network = str(stream_settings.get("network") or "").lower()
        security = str(stream_settings.get("security") or "").lower()
        ws_supported = network == "ws" and security in {"tls", "reality", "xtls"}
        ws_settings = stream_settings.get("wsSettings") if isinstance(stream_settings.get("wsSettings"), dict) else {}
        ws_path = str(server.ws_path or ws_settings.get("path") or "/ws").strip() or "/ws"
        return InboundContext(
            port=port,
            sni=sni,
            public_key=public_key,
            short_id=short_id,
            ws_path=ws_path,
            ws_supported=ws_supported,
        )

    async def _verify_client_visible(
        self, session: ClientSession, server: ServerInfo, client_uuid: str
    ) -> None:
        """Verify client appears in inbound after reload; retry reload once if missing."""
        await asyncio.sleep(0.5)
        try:
            inbound = await self._get_inbound(session, server)
        except Exception:
            return  # Can't verify — don't block provisioning
        if self._find_client_by_uuid(inbound, client_uuid):
            logger.info("xui client verified server_id=%s uuid=%s", server.id, client_uuid)
            return
        logger.warning("xui client not visible after reload, retrying server_id=%s uuid=%s", server.id, client_uuid)
        await self._reload_xray(session, server)
        await asyncio.sleep(1.0)
        try:
            inbound = await self._get_inbound(session, server)
        except Exception:
            return
        if not self._find_client_by_uuid(inbound, client_uuid):
            raise XUIProviderError(f"Client {client_uuid} not visible after reload retry server_id={server.id}")
        logger.info("xui client verified after retry server_id=%s uuid=%s", server.id, client_uuid)

    async def _reload_xray(self, session: ClientSession, server: ServerInfo) -> None:
        """Reload xray runtime config after adding clients.

        Tries the 3x-ui panel API first, then SIGUSR1, then service restart as last resort.
        """
        if await self._try_api_reload(session, server):
            await asyncio.sleep(1.5)
            return
        reloaded = await asyncio.get_event_loop().run_in_executor(None, self._signal_reload, server.id)
        if reloaded:
            await asyncio.sleep(1.5)
            return
        restarted = await asyncio.get_event_loop().run_in_executor(None, self._restart_xui_service, server.id)
        if restarted:
            await asyncio.sleep(4.0)

    async def _try_api_reload(self, session: ClientSession, server: ServerInfo) -> bool:
        """POST /server/restartXrayService — succeeds on some 3x-ui versions."""
        try:
            url = f"{server.api_url}/server/restartXrayService"
            async with session.post(url) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
                if isinstance(data, dict) and data.get("success") is True:
                    logger.info("xui xray reloaded via API server_id=%s", server.id)
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _signal_reload(server_id: int) -> bool:
        """Send SIGUSR1 to the x-ui process to trigger xray config reload."""
        try:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.name.isdigit():
                    continue
                try:
                    comm = (pid_dir / "comm").read_text().strip()
                    if comm == "x-ui":
                        os.kill(int(pid_dir.name), signal.SIGUSR1)
                        logger.info("xui xray reloaded via SIGUSR1 pid=%s server_id=%s", pid_dir.name, server_id)
                        return True
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
        except Exception as error:
            logger.warning("xui xray reload failed server_id=%s error=%s", server_id, error)
        return False

    @staticmethod
    def _restart_xui_service(server_id: int) -> bool:
        """Restart x-ui via systemctl as last resort when both API and SIGUSR1 fail."""
        import subprocess  # noqa: PLC0415
        try:
            result = subprocess.run(
                ["systemctl", "restart", "x-ui"],
                capture_output=True,
                timeout=20,
            )
            if result.returncode == 0:
                logger.info("xui service restarted via systemctl server_id=%s", server_id)
                return True
            logger.warning(
                "xui systemctl restart failed server_id=%s returncode=%s stderr=%s",
                server_id, result.returncode, result.stderr.decode()[:300],
            )
        except Exception as error:
            logger.warning("xui service restart unavailable server_id=%s error=%s", server_id, error)
        return False

    def _build_profiles(
        self,
        server: ServerInfo,
        ctx: InboundContext,
        reality_uuid: str,
        ws_uuid: str | None,
        user_id: int,
    ) -> list[VpnProfile]:
        profiles: list[VpnProfile] = [self._build_reality_link(server, ctx, reality_uuid, user_id)]
        if ws_uuid:
            profiles.append(self._build_ws_tls_link(server, ctx, ws_uuid, user_id))
        return profiles

    def _build_reality_link(self, server: ServerInfo, ctx: InboundContext, client_uuid: str, user_id: int) -> VpnProfile:
        config = (
            f"vless://{client_uuid}@{server.host}:{ctx.port}"
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
        return VpnProfile(protocol="vless-reality", config=config, server_name=server.name)

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
        return VpnProfile(protocol="vless-ws-tls", config=config, server_name=server.name)
