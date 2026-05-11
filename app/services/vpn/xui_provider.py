from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import uuid4

import time

from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar

from app.services.vpn.base import ClientLimits, CreateClientResult, ServerInfo, VPNProvider, VpnProfile

logger = logging.getLogger(__name__)

# Per-server reload lock + debounce: prevents concurrent Xray reload storms.
# Without this, 5 simultaneous purchases trigger 5 reloads on the same server.
_RELOAD_LOCKS: dict[int, asyncio.Lock] = {}
_LAST_RELOAD_TS: dict[int, float] = {}
_RELOAD_DEBOUNCE_S = 2.0  # skip reload if one completed within this window


def _reload_lock(server_id: int) -> asyncio.Lock:
    if server_id not in _RELOAD_LOCKS:
        _RELOAD_LOCKS[server_id] = asyncio.Lock()
    return _RELOAD_LOCKS[server_id]


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
        # Maps id(ClientSession) → CSRF token fetched during _login.
        # Used to include X-CSRF-Token in all subsequent POST requests
        # (3x-ui v3 requires it on all API endpoints, not only /login).
        self._session_csrf: dict[int, str] = {}

    async def create_client(
        self,
        user_id: int,
        server: ServerInfo,
        limits: ClientLimits,
        reality_uuid: str | None = None,
        ws_uuid: str | None = None,
        key_id: int | None = None,
    ) -> CreateClientResult:
        self._validate_server_security(server)
        reality_email = self._client_email(user_id, "reality", key_id)
        ws_email = self._client_email(user_id, "ws", key_id)
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            ctx = self._extract_inbound_context(server, inbound)

            existing_reality = self._find_existing_client_uuid(inbound, reality_email)
            if existing_reality and reality_uuid and existing_reality != reality_uuid:
                logger.warning(
                    "UUID mismatch for email=%s: xui_has=%s expected=%s - updating in-place",
                    reality_email, existing_reality, reality_uuid,
                )
                await self._update_client_uuid(session, server, inbound, reality_email, reality_uuid)
                final_reality_uuid = reality_uuid
                logger.info(
                    "xui reality client UUID updated user_id=%s server_id=%s uuid=%s",
                    user_id, server.id, final_reality_uuid,
                )
            elif not existing_reality:
                final_reality_uuid = reality_uuid or str(uuid4())
                await self._add_client(session, server, final_reality_uuid, reality_email, limits)
                logger.info(
                    "xui reality client added user_id=%s server_id=%s uuid=%s",
                    user_id, server.id, final_reality_uuid,
                )
            else:
                final_reality_uuid = existing_reality
                # Re-enable if the client is disabled (e.g. after traffic limit enforcement).
                if not self._is_client_enabled(inbound, final_reality_uuid):
                    logger.info(
                        "xui reality client disabled — re-enabling uuid=%s server_id=%s",
                        final_reality_uuid, server.id,
                    )
                    await self._update_client_record(
                        session, server, final_reality_uuid,
                        lambda c: (c.__setitem__("enable", True), c.__setitem__("expiryTime", int(limits.expiry_time))),
                    )
                # Reset traffic stats so this key doesn't inherit usage from a previous key
                # (e.g. after a DB reset where key IDs restart from 1).
                try:
                    reset_url = (
                        f"{server.api_url}/panel/api/inbounds"
                        f"/{server.inbound_id}/resetClientTraffic/{reality_email}"
                    )
                    await self._request_json(session, "post", reset_url)
                    logger.info(
                        "traffic reset for reused reality client email=%s server_id=%s",
                        reality_email, server.id,
                    )
                except Exception:
                    pass

            final_ws_uuid: str | None = None
            if ctx.ws_supported:
                existing_ws = self._find_existing_client_uuid(inbound, ws_email)
                # WS client gets totalGB=0 (unlimited in XUI) because traffic enforcement
                # sums Reality+WS and disables both clients via our 120-second loop.
                # Setting a per-client limit in XUI would give the user 2× the intended allowance.
                ws_limits = ClientLimits(
                    limit_ip=limits.limit_ip,
                    total_gb=0,
                    expiry_time=limits.expiry_time,
                )
                if existing_ws and ws_uuid and existing_ws != ws_uuid:
                    logger.warning(
                        "WS UUID mismatch for email=%s: xui_has=%s expected=%s - updating in-place",
                        ws_email, existing_ws, ws_uuid,
                    )
                    await self._update_client_uuid(session, server, inbound, ws_email, ws_uuid)
                    final_ws_uuid = ws_uuid
                    logger.info("xui ws client UUID updated user_id=%s server_id=%s", user_id, server.id)
                elif not existing_ws:
                    final_ws_uuid = ws_uuid or str(uuid4())
                    await self._add_client(session, server, final_ws_uuid, ws_email, ws_limits)
                    logger.info("xui ws client added user_id=%s server_id=%s", user_id, server.id)
                else:
                    final_ws_uuid = existing_ws
                    if not self._is_client_enabled(inbound, final_ws_uuid):
                        logger.info("xui ws client disabled — re-enabling uuid=%s", final_ws_uuid)
                        await self._update_client_record(
                            session, server, final_ws_uuid,
                            lambda c: (c.__setitem__("enable", True), c.__setitem__("expiryTime", int(limits.expiry_time))),
                        )
                    try:
                        reset_url = (
                            f"{server.api_url}/panel/api/inbounds"
                            f"/{server.inbound_id}/resetClientTraffic/{ws_email}"
                        )
                        await self._request_json(session, "post", reset_url)
                        logger.info(
                            "traffic reset for reused ws client email=%s server_id=%s",
                            ws_email, server.id,
                        )
                    except Exception:
                        pass

            await self._ensure_client_live(session, server, reality_email, final_reality_uuid)

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
            changed = await self._update_client_record(
                session,
                server,
                client_uuid,
                lambda client: client.__setitem__("enable", False),
            )
            if not changed:
                return
            await self._reload_xray(session, server)

    async def update_client_expiry(
        self,
        server: ServerInfo,
        client_uuid: str,
        expiry_time_ms: int,
        total_gb: int | None = None,
    ) -> bool:
        """Update expiryTime (and optionally totalGB) for a specific client.

        total_gb: accumulated traffic allowance in GB. Pass None or 0 to leave
        the existing totalGB in XUI unchanged (unlimited / not managed).
        Returns True if client was found and updated.
        """
        self._validate_server_security(server)
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            settings_raw = inbound.get("settings")
            settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
            if not isinstance(settings, dict):
                return False
            clients = settings.get("clients")
            if not isinstance(clients, list):
                return False
            changed = await self._update_client_record(
                session,
                server,
                client_uuid,
                lambda client: (
                    client.__setitem__("expiryTime", expiry_time_ms),
                    client.__setitem__("enable", True),
                    client.__setitem__("totalGB", total_gb * 1024 * 1024 * 1024) if total_gb and total_gb > 0 else None,
                ),
            )
            if not changed:
                return False
            await self._reload_xray(session, server)
            return True

    async def client_exists(self, server: ServerInfo, client_uuid: str) -> bool:
        self._validate_server_security(server)
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            return self._find_client_by_uuid(inbound, client_uuid)

    async def get_client(self, server: ServerInfo, user_id: int, key_id: int | None = None) -> str | None:
        self._validate_server_security(server)
        reality_email = self._client_email(user_id, "reality", key_id)
        async with self._session() as session:
            await self._login(session, server)
            inbound = await self._get_inbound(session, server)
            return self._find_existing_client_uuid(inbound, reality_email)

    async def add_client(
        self, server: ServerInfo, user_id: int, reality_uuid: str, expiry_time: int, key_id: int | None = None
    ) -> CreateClientResult:
        limits = ClientLimits(expiry_time=expiry_time)
        return await self.create_client(
            user_id=user_id,
            server=server,
            limits=limits,
            reality_uuid=reality_uuid,
            key_id=key_id,
        )

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
            async with self._session() as session:
                await self._login(session, server)
                inbound = await self._get_inbound(session, server)
                self._validate_inbound_clients_readable(inbound)
            logger.info("xui healthcheck ok server_id=%s host=%s port=%s", server.id, server.host, server.public_port or 443)
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
        if method.lower() == "post":
            csrf = self._session_csrf.get(id(session), "")
            if csrf:
                existing = dict(kwargs.pop("headers", {}))
                existing.setdefault("X-CSRF-Token", csrf)
                kwargs["headers"] = existing
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

        # 3x-ui v3 requires a CSRF token fetched from the login page before posting.
        # Older versions return 200+JSON directly; v3 returns 403 without the token.
        csrf_token = await self._get_csrf_token(session, server)

        headers = {}
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token
            self._session_csrf[id(session)] = csrf_token

        async with session.post(
            url,
            json={
                "username": server.username,
                "password": server.password,
            },
            headers=headers,
        ) as resp:
            if resp.status == 403 and not csrf_token:
                raise XUIProviderError("Login rejected (403) — CSRF token missing or invalid")
            payload = await resp.json(content_type=None)

        if isinstance(payload, dict) and payload.get("success") is False:
            raise XUIProviderError(str(payload.get("msg") or "login rejected"))

    async def _get_csrf_token(self, session: ClientSession, server: ServerInfo) -> str:
        """Fetch the CSRF token from the panel root page (3x-ui v3+).

        Returns empty string for older versions that don't use CSRF.
        """
        try:
            async with session.get(server.api_url, timeout=ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return ""
                html = await resp.text(errors="ignore")
            import re
            m = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html)
            if m:
                return m.group(1)
        except Exception:
            pass
        return ""

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

    async def reset_client_traffic(self, server: ServerInfo, user_id: int, key_id: int | None = None) -> None:
        """Reset traffic counters for the user's reality and ws clients (best-effort)."""
        self._validate_server_security(server)
        emails = [
            self._client_email(user_id, "reality", key_id),
            self._client_email(user_id, "ws", key_id),
        ]
        async with self._session() as session:
            await self._login(session, server)
            for email in emails:
                url = f"{server.api_url}/panel/api/inbounds/{server.inbound_id}/resetClientTraffic/{email}"
                try:
                    data = await self._request_json(session, "post", url)
                    if isinstance(data, dict) and data.get("success") is True:
                        logger.info("traffic reset server_id=%s email=%s", server.id, email)
                except Exception as error:
                    logger.warning("traffic reset failed server_id=%s email=%s error=%s", server.id, email, error)

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
                            "limitIp": int(limits.limit_ip),
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

    async def _update_client_uuid(
        self,
        session: ClientSession,
        server: ServerInfo,
        inbound: dict,  # ignored; fresh data is fetched below
        email: str,
        new_uuid: str,
    ) -> None:
        inbound = await self._get_inbound(session, server)
        settings_raw = inbound.get("settings")
        settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
        if not isinstance(settings, dict):
            raise XUIProviderError("inbound settings invalid for UUID update")
        clients = settings.get("clients")
        if not isinstance(clients, list):
            raise XUIProviderError("inbound clients invalid for UUID update")
        old_uuid: str | None = None
        updated_client: dict | None = None
        for client in clients:
            if isinstance(client, dict) and str(client.get("email")) == email:
                old_uuid = str(client.get("id") or "").strip()
                updated_client = {**client, "id": new_uuid}
                break
        if not old_uuid or updated_client is None:
            raise XUIProviderError(f"Client email={email} not found for UUID update server_id={server.id}")
        url = f"{server.api_url}/panel/api/inbounds/updateClient/{old_uuid}"
        payload = {
            "id": server.inbound_id,
            "settings": json.dumps({"clients": [updated_client]}),
        }
        data = await self._request_json(session, "post", url, data=payload)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise XUIProviderError(f"updateClient returned error: {data}")
        logger.info(
            "xui client UUID updated email=%s old=%s new=%s server_id=%s",
            email, old_uuid, new_uuid, server.id,
        )

    async def _update_client_record(
        self,
        session: ClientSession,
        server: ServerInfo,
        client_uuid: str,
        mutator,
    ) -> bool:
        """Safely update a single client via updateClient endpoint.

        This avoids using /inbounds/update with partial payload, which can
        corrupt inbound fields (port/protocol) on some 3x-ui versions.
        """
        inbound = await self._get_inbound(session, server)
        settings_raw = inbound.get("settings")
        settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
        if not isinstance(settings, dict):
            raise XUIProviderError("inbound settings invalid")
        clients = settings.get("clients")
        if not isinstance(clients, list):
            raise XUIProviderError("inbound clients invalid")
        target: dict | None = None
        for client in clients:
            if isinstance(client, dict) and str(client.get("id")) == client_uuid:
                target = {**client}
                break
        if target is None:
            return False
        mutator(target)
        url = f"{server.api_url}/panel/api/inbounds/updateClient/{client_uuid}"
        payload = {
            "id": server.inbound_id,
            "settings": json.dumps({"clients": [target]}),
        }
        data = await self._request_json(session, "post", url, data=payload)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise XUIProviderError(f"updateClient returned error: {data}")
        return True

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

    @staticmethod
    def _client_email(user_id: int, suffix: str, key_id: int | None = None) -> str:
        """Generate unique x-ui client email for (user_id, key_id, protocol).

        Format: user_{user_id}_{key_id} for reality
                user_{user_id}_{key_id}_ws for WebSocket
        key_id=None is legacy null-slot — returns old format for backward compat
        with any existing null-slot clients still in x-ui.
        """
        if key_id is None:
            # Legacy null-slot path — should not be hit in normal flow
            return f"{user_id}-{suffix}"
        if suffix == "ws":
            return f"user_{user_id}_{key_id}_ws"
        return f"user_{user_id}_{key_id}"

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
                return True
        return False

    def _is_client_enabled(self, inbound: dict, client_uuid: str) -> bool:
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

    async def _ensure_client_live(
        self, session: ClientSession, server: ServerInfo, email: str, client_uuid: str
    ) -> None:
        """Verify client is live in Xray runtime without restart.

        3x-ui v3 automatically propagates addClient changes to the running Xray
        instance via gRPC (HandlerService). getClientTraffics returns a valid obj
        immediately if the client is in the Xray runtime — no restart needed.
        Falls back to full reload only if the gRPC hot-add didn't take effect.
        """
        url = f"{server.api_url}/panel/api/inbounds/getClientTraffics/{email}"
        try:
            data = await self._request_json(session, "get", url)
            # success=true means 3x-ui gRPC hot-add propagated the client to the
            # running Xray instance. obj may be null for a brand-new client with
            # zero traffic — that is NOT an indication the client is missing.
            if isinstance(data, dict) and data.get("success") is True:
                logger.info("client live via gRPC (no restart) email=%s server_id=%s", email, server.id)
                return
        except Exception:
            pass
        logger.info("client not in runtime, falling back to reload email=%s server_id=%s", email, server.id)
        await self._reload_xray(session, server)
        await self._verify_client_visible(session, server, client_uuid)

    async def _verify_client_visible(
        self, session: ClientSession, server: ServerInfo, client_uuid: str
    ) -> None:
        """Verify client appears in inbound after reload; retry reload once if missing."""
        await asyncio.sleep(0.5)
        try:
            inbound = await self._get_inbound(session, server)
        except Exception as err:
            raise XUIProviderError(
                f"Cannot verify client {client_uuid}: inbound unreachable server_id={server.id}"
            ) from err
        if self._find_client_by_uuid(inbound, client_uuid):
            logger.info("xui client verified server_id=%s uuid=%s", server.id, client_uuid)
            return
        logger.warning(
            "xui client NOT visible after first check, re-reloading server_id=%s uuid=%s",
            server.id,
            client_uuid,
        )
        await self._reload_xray(session, server)
        await asyncio.sleep(1.0)
        try:
            inbound = await self._get_inbound(session, server)
        except Exception as err:
            raise XUIProviderError(
                f"Cannot verify client {client_uuid}: inbound unreachable after reload retry server_id={server.id}"
            ) from err
        if not self._find_client_by_uuid(inbound, client_uuid):
            raise XUIProviderError(
                f"Client {client_uuid} NOT in inbound after 2 reloads - provisioning aborted server_id={server.id}"
            )
        logger.info("xui client verified after retry server_id=%s uuid=%s", server.id, client_uuid)

    async def _reload_xray(self, session: ClientSession, server: ServerInfo) -> None:
        """Reload xray runtime config, debounced per server.

        Concurrent callers for the same server serialize through a lock.
        If a reload completed within _RELOAD_DEBOUNCE_S seconds, the reload
        is skipped — the earlier reload already picked up the change.
        """
        lock = _reload_lock(server.id)
        async with lock:
            now = time.monotonic()
            last = _LAST_RELOAD_TS.get(server.id, 0.0)
            if now - last < _RELOAD_DEBOUNCE_S:
                logger.debug(
                    "xray reload skipped (debounce) server_id=%s elapsed=%.2fs",
                    server.id, now - last,
                )
                return
            if await self._try_api_reload(session, server):
                _LAST_RELOAD_TS[server.id] = time.monotonic()
                await asyncio.sleep(1.5)
                return
        raise XUIProviderError(f"xray reload failed: API reload rejected server_id={server.id}")

    async def _try_api_reload(self, session: ClientSession, server: ServerInfo) -> bool:
        """Reload xray via panel API. Tries two endpoint paths for 3x-ui version compatibility."""
        candidates = [
            f"{server.api_url}/panel/api/server/restartXrayService",
            f"{server.api_url}/server/restartXrayService",
        ]
        for url in candidates:
            try:
                data = await self._request_json(session, "post", url)
                if isinstance(data, dict) and data.get("success") is True:
                    logger.info("xui xray reloaded via API url=%s server_id=%s", url, server.id)
                    return True
            except Exception:
                continue
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
