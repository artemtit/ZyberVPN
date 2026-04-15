from __future__ import annotations

from io import BytesIO
import json
import secrets
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout
import qrcode

from app.config import Settings


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


def _validate_xui_config(settings: Settings) -> None:
    if not settings.xui_base_url:
        raise VPNProvisionError("XUI_BASE_URL is not configured")
    if not settings.xui_username or not settings.xui_password:
        raise VPNProvisionError("XUI_USERNAME/XUI_PASSWORD are not configured")
    if settings.xui_inbound_id <= 0:
        raise VPNProvisionError("XUI_INBOUND_ID must be greater than zero")
    if not settings.xui_public_host:
        raise VPNProvisionError("XUI_PUBLIC_HOST is not configured")


def _build_vless_link(settings: Settings, client_uuid: str, tg_id: int) -> str:
    query_parts = [
        f"type={settings.xui_transport}",
        f"security={settings.xui_security}",
        "flow=xtls-rprx-vision",
    ]
    if settings.xui_sni:
        query_parts.append(f"sni={settings.xui_sni}")
    query = "&".join(query_parts)
    return (
        f"vless://{client_uuid}@{settings.xui_public_host}:{settings.xui_public_port}"
        f"?{query}#ZyberVPN-{tg_id}"
    )


async def create_vpn_key_via_3xui(settings: Settings, tg_id: int) -> str:
    _validate_xui_config(settings)

    client_uuid = str(uuid4())
    email = str(tg_id)
    payload = {
        "id": settings.xui_inbound_id,
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": client_uuid,
                        "email": email,
                        "flow": "xtls-rprx-vision",
                        "enable": True,
                        "limitIp": 0,
                        "totalGB": 0,
                        "expiryTime": 0,
                        "subId": "",
                        "tgId": "",
                        "reset": 0,
                    }
                ]
            }
        ),
    }

    timeout = ClientTimeout(total=20)
    try:
        async with ClientSession(timeout=timeout) as session:
            login_response = await session.post(
                f"{settings.xui_base_url}/login",
                data={"username": settings.xui_username, "password": settings.xui_password},
            )
            if login_response.status != 200:
                raise VPNProvisionError("3x-ui login failed")
            login_json = await login_response.json(content_type=None)
            if isinstance(login_json, dict) and login_json.get("success") is False:
                raise VPNProvisionError("3x-ui login rejected credentials")

            create_response = await session.post(
                f"{settings.xui_base_url}/panel/api/inbounds/addClient",
                json=payload,
            )
            if create_response.status != 200:
                raise VPNProvisionError("3x-ui addClient request failed")
            create_json = await create_response.json(content_type=None)
            if not isinstance(create_json, dict) or create_json.get("success") is not True:
                raise VPNProvisionError("3x-ui addClient returned error")
    except VPNProvisionError:
        raise
    except Exception as error:
        raise VPNProvisionError("3x-ui is unavailable") from error

    return _build_vless_link(settings, client_uuid, tg_id)
