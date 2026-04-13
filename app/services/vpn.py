from __future__ import annotations

from io import BytesIO
import secrets
import qrcode


def qr_png_from_text(text: str) -> bytes:
    buffer = BytesIO()
    image = qrcode.make(text)
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def create_vpn_key(user_id: int) -> tuple[str, bytes]:
    token = secrets.token_urlsafe(18)
    link = f"vless://{token}@vpn.zyber.local:443?type=tcp#ZyberVPN-{user_id}"
    return link, qr_png_from_text(link)
