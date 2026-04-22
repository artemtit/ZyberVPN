"""Fernet-based encryption for sensitive server credentials stored in Supabase.

Generate a key once and set it in the environment:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Set SERVER_ENCRYPT_KEY=<output> in your .env file.

If SERVER_ENCRYPT_KEY is not set, credentials are stored/returned as plaintext
(backward-compatible with existing rows).
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Final

logger = logging.getLogger(__name__)

_ENC_PREFIX: Final = "enc:"

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore[assignment,misc]
    InvalidToken = Exception  # type: ignore[assignment,misc]


@lru_cache(maxsize=1)
def _get_fernet():
    key = os.getenv("SERVER_ENCRYPT_KEY", "").strip()
    if not key:
        return None
    if Fernet is None:
        logger.error("SERVER_ENCRYPT_KEY is set but cryptography package is not installed")
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        logger.exception("Invalid SERVER_ENCRYPT_KEY — credentials will be stored unencrypted")
        return None


def encrypt_credential(value: str) -> str:
    """Encrypt *value* with Fernet if a key is configured; otherwise return as-is."""
    if not value:
        return value
    fernet = _get_fernet()
    if fernet is None:
        return value
    return _ENC_PREFIX + fernet.encrypt(value.encode()).decode()


def decrypt_credential(value: str) -> str:
    """Decrypt *value* if it carries the ``enc:`` prefix; otherwise return as-is."""
    if not value.startswith(_ENC_PREFIX):
        return value  # plaintext (legacy row or key not configured)
    fernet = _get_fernet()
    if fernet is None:
        raise RuntimeError(
            "Credential is encrypted (enc: prefix) but SERVER_ENCRYPT_KEY is not set"
        )
    try:
        return fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Failed to decrypt server credential: invalid token") from exc
