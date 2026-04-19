from __future__ import annotations

from io import BytesIO
import qrcode


def qr_png_from_text(text: str) -> bytes:
    buffer = BytesIO()
    image = qrcode.make(text)
    image.save(buffer, format="PNG")
    return buffer.getvalue()
