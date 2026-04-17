from __future__ import annotations

import asyncio
from io import BytesIO
import json
import logging
import secrets
from uuid import uuid4
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, CookieJar
import qrcode

from app.config import Settings

logger = logging.getLogger(__name__)


def qr_png_from_text(text: str) -> bytes:
    buffer = BytesIO()
    image = qrcode.make(text)
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def create_vpn_key(user_id: int) -> tuple[str, bytes]:
    token = secrets.token_urlsafe(18)
    link = f"vless://{token}@vpn.zyber.local:443?type=tcp#ZyberVPN-{user_id}"
    return link, qr_png_from_text(link)


class VPNProvisionError(RuntimeError):
    pass


class VPNProvisionRetryableError(VPNProvisionError):
    pass


def _sanitize_login_payload(payload: dict[str, str]) -> dict[str, str]:
    return {"username": payload.get("username", ""), "password": "***"}


def _validate_vless_link(link: str, client_uuid: str) -> None:
    if not link.startswith("vless://"):
        raise VPNProvisionError("Generated VPN link has invalid scheme")
    if client_uuid not in link:
        raise VPNProvisionError("Generated VPN link does not contain client UUID")
    if "pbk=" not in link:
        raise VPNProvisionError("Generated VPN link does not contain pbk")
    if "sid=" not in link:
        raise VPNProvisionError("Generated VPN link does not contain sid")


def _validate_xui_config(settings: Settings) -> None:
    if not settings.xui_base_url:
        raise VPNProvisionError("XUI_BASE_URL is not configured")
    parsed = urlparse(settings.xui_base_url)
    if parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
        raise VPNProvisionError("XUI_BASE_URL must be external, localhost is not allowed")
    if not settings.xui_username or not settings.xui_password:
        raise VPNProvisionError("XUI_USERNAME/XUI_PASSWORD are not configured")
    if settings.xui_inbound_id <= 0:
        raise VPNProvisionError("XUI_INBOUND_ID must be greater than zero")
    if not settings.xui_public_host:
        raise VPNProvisionError("XUI_PUBLIC_HOST is not configured")


def _build_vless_link(client_uuid: str, tg_id: int, sni: str, public_key: str, short_id: str) -> str:
    return (
        f"vless://{client_uuid}@sub.zybervpn.ru:443"
        f"?type=tcp"
        f"&security=reality"
        f"&flow=xtls-rprx-vision"
        f"&sni={sni}"
        f"&pbk={public_key}"
        f"&sid={short_id}"
        f"&fp=chrome"
        f"&spx=/"
        f"#ZyberVPN-{tg_id}"
    )


async def create_vpn_key_via_3xui(settings: Settings, tg_id: int) -> str:
    _validate_xui_config(settings)
    logger.info("Provisioning VPN key via 3x-ui for tg_id=%s inbound_id=%s", tg_id, settings.xui_inbound_id)

    email = str(tg_id)

    timeout = ClientTimeout(total=5)
    max_attempts = 3  # 1 original + 2 retries
    last_error: Exception | None = None
    login_url = f"{settings.xui_base_url}/login"
    list_inbounds_url = f"{settings.xui_base_url}/panel/api/inbounds/list"
    add_client_url = f"{settings.xui_base_url}/panel/api/inbounds/addClient"
    login_payload = {"username": settings.xui_username, "password": settings.xui_password}
    for attempt in range(1, max_attempts + 1):
        try:
            async with ClientSession(timeout=timeout, cookie_jar=CookieJar(unsafe=True)) as session:
                logger.info(
                    "3x-ui request url=%s payload=%s tg_id=%s attempt=%s/%s",
                    login_url,
                    _sanitize_login_payload(login_payload),
                    tg_id,
                    attempt,
                    max_attempts,
                )
                login_response = await session.post(
                    login_url,
                    json=login_payload,
                )
                if login_response.status != 200:
                    login_body = await login_response.text()
                    logger.error(
                        "3x-ui login failed url=%s status=%s tg_id=%s attempt=%s/%s body=%s",
                        login_url,
                        login_response.status,
                        tg_id,
                        attempt,
                        max_attempts,
                        login_body,
                    )
                    if login_response.status in {401, 403}:
                        raise VPNProvisionError("3x-ui authorization failed on login")
                    if login_response.status >= 500:
                        raise VPNProvisionRetryableError("3x-ui login failed")
                    raise VPNProvisionError("3x-ui login failed")
                login_json = await login_response.json(content_type=None)
                if isinstance(login_json, dict) and login_json.get("success") is False:
                    logger.error("3x-ui login rejected credentials for tg_id=%s", tg_id)
                    raise VPNProvisionError("3x-ui login rejected credentials")

                logger.info("3x-ui: checking existing client tg_id=%s", tg_id)
                list_response = await session.get(list_inbounds_url)
                if list_response.status != 200:
                    list_body = await list_response.text()
                    logger.error(
                        "3x-ui inbounds list failed url=%s status=%s tg_id=%s attempt=%s/%s body=%s",
                        list_inbounds_url,
                        list_response.status,
                        tg_id,
                        attempt,
                        max_attempts,
                        list_body,
                    )
                    if list_response.status in {401, 403}:
                        raise VPNProvisionError("3x-ui authorization failed for inbounds list")
                    if list_response.status >= 500:
                        raise VPNProvisionRetryableError("3x-ui inbounds list request failed")
                    raise VPNProvisionError("3x-ui inbounds list request failed")

                list_json = await list_response.json(content_type=None)
                inbound_items: list[dict] = []
                if isinstance(list_json, dict):
                    raw_obj = list_json.get("obj")
                    if isinstance(raw_obj, list):
                        inbound_items = [item for item in raw_obj if isinstance(item, dict)]

                target_inbound: dict | None = None
                for inbound in inbound_items:
                    inbound_id = str(inbound.get("id", "")).strip()
                    if inbound_id == str(settings.xui_inbound_id):
                        target_inbound = inbound
                        break
                if not target_inbound:
                    logger.error("3x-ui: inbound not found id=%s", settings.xui_inbound_id)
                    raise VPNProvisionError(f"3x-ui inbound not found id={settings.xui_inbound_id}")

                raw_stream_settings = target_inbound.get("streamSettings")
                if isinstance(raw_stream_settings, str):
                    try:
                        stream_settings = json.loads(raw_stream_settings)
                    except Exception as error:
                        raise VPNProvisionError("3x-ui inbound streamSettings JSON is invalid") from error
                elif isinstance(raw_stream_settings, dict):
                    stream_settings = raw_stream_settings
                else:
                    stream_settings = {}

                reality_settings = stream_settings.get("realitySettings")
                if not isinstance(reality_settings, dict):
                    raise VPNProvisionError("3x-ui inbound streamSettings has no realitySettings")
                public_key = str(reality_settings.get("publicKey") or "").strip()
                short_ids = reality_settings.get("shortIds")
                if isinstance(short_ids, list):
                    short_id = str(short_ids[0]).strip() if short_ids else ""
                else:
                    short_id = str(short_ids or "").strip()
                if not public_key:
                    raise VPNProvisionError("3x-ui inbound realitySettings.publicKey is missing")
                if not short_id:
                    raise VPNProvisionError("3x-ui inbound realitySettings.shortIds is missing")
                logger.info("3x-ui: reality public_key=%s", public_key)
                logger.info("3x-ui: reality short_id=%s", short_id)

                existing_client_id: str | None = None
                raw_settings = target_inbound.get("settings")
                try:
                    inbound_settings = json.loads(raw_settings) if isinstance(raw_settings, str) and raw_settings else {}
                    inbound_clients = inbound_settings.get("clients")
                    clients = inbound_clients if isinstance(inbound_clients, list) else []
                    for client in clients:
                        if isinstance(client, dict) and str(client.get("email")) == email:
                            candidate_id = str(client.get("id") or "").strip()
                            if candidate_id:
                                existing_client_id = candidate_id
                                break
                except Exception:
                    logger.exception("3x-ui inbound settings JSON parse failed inbound_id=%s", settings.xui_inbound_id)

                if existing_client_id:
                    logger.info("3x-ui: found existing client uuid=%s", existing_client_id)
                    vpn_link = _build_vless_link(
                        client_uuid=existing_client_id,
                        tg_id=tg_id,
                        sni=settings.xui_sni,
                        public_key=public_key,
                        short_id=short_id,
                    )
                    _validate_vless_link(vpn_link, existing_client_id)
                    logger.info("3x-ui: generated link for tg_id=%s", tg_id)
                    return vpn_link

                logger.info("3x-ui: creating new client tg_id=%s", tg_id)
                client_id = str(uuid4())
                sub_id = secrets.token_urlsafe(8)
                payload = {
                    "id": settings.xui_inbound_id,
                    "settings": json.dumps(
                        {
                            "clients": [
                                {
                                    "id": client_id,
                                    "email": email,
                                    "flow": "xtls-rprx-vision",
                                    "enable": True,
                                    "limitIp": 0,
                                    "totalGB": 0,
                                    "expiryTime": 0,
                                    "subId": sub_id,
                                    "tgId": "",
                                    "reset": 0,
                                }
                            ]
                        }
                    ),
                }

                logger.info(
                    "3x-ui request url=%s inbound_id=%s client_id=%s sub_id=%s payload=%s tg_id=%s attempt=%s/%s",
                    add_client_url,
                    settings.xui_inbound_id,
                    client_id,
                    sub_id,
                    payload,
                    tg_id,
                    attempt,
                    max_attempts,
                )
                create_response = await session.post(
                    add_client_url,
                    data=payload,
                )
                if create_response.status != 200:
                    create_body = await create_response.text()
                    logger.error(
                        "3x-ui addClient failed url=%s inbound_id=%s client_id=%s sub_id=%s status=%s tg_id=%s attempt=%s/%s body=%s",
                        add_client_url,
                        settings.xui_inbound_id,
                        client_id,
                        sub_id,
                        create_response.status,
                        tg_id,
                        attempt,
                        max_attempts,
                        create_body,
                    )
                    if create_response.status == 404:
                        raise VPNProvisionError("3x-ui addClient endpoint not found (/panel/api/inbounds/addClient)")
                    if create_response.status in {401, 403}:
                        raise VPNProvisionError("3x-ui authorization failed for addClient")
                    if create_response.status >= 500:
                        raise VPNProvisionRetryableError("3x-ui addClient request failed")
                    raise VPNProvisionError("3x-ui addClient request failed")
                logger.info(
                    "3x-ui addClient response url=%s inbound_id=%s client_id=%s sub_id=%s status=%s tg_id=%s attempt=%s/%s",
                    add_client_url,
                    settings.xui_inbound_id,
                    client_id,
                    sub_id,
                    create_response.status,
                    tg_id,
                    attempt,
                    max_attempts,
                )
                create_json = await create_response.json(content_type=None)
                if not isinstance(create_json, dict) or create_json.get("success") is not True:
                    logger.error(
                        "3x-ui addClient returned error tg_id=%s attempt=%s/%s payload=%s",
                        tg_id,
                        attempt,
                        max_attempts,
                        create_json,
                    )
                    raise VPNProvisionError("3x-ui addClient returned error")
                vpn_link = _build_vless_link(
                    client_uuid=client_id,
                    tg_id=tg_id,
                    sni=settings.xui_sni,
                    public_key=public_key,
                    short_id=short_id,
                )
                _validate_vless_link(vpn_link, client_id)
                logger.info("3x-ui: generated link for tg_id=%s", tg_id)
                logger.info(
                    "VPN key created via 3x-ui for tg_id=%s inbound_id=%s client_id=%s sub_id=%s link=%s",
                    tg_id,
                    settings.xui_inbound_id,
                    client_id,
                    sub_id,
                    vpn_link,
                )
                return vpn_link
        except VPNProvisionRetryableError as error:
            last_error = error
            if attempt >= max_attempts:
                break
            await asyncio.sleep(0.2 * attempt)
        except VPNProvisionError as error:
            last_error = error
            break
        except Exception as error:
            last_error = error
            logger.exception("3x-ui unavailable for tg_id=%s attempt=%s/%s", tg_id, attempt, max_attempts)
            if attempt >= max_attempts:
                break
            await asyncio.sleep(0.2 * attempt)

    raise VPNProvisionError("3x-ui is unavailable") from last_error
