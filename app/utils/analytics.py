from __future__ import annotations

import asyncio
import logging

import httpx

_POSTHOG_URL = "https://eu.i.posthog.com/capture/"
_api_key: str = ""
_client: httpx.AsyncClient | None = None

logger = logging.getLogger(__name__)


def configure(api_key: str) -> None:
    global _api_key, _client
    _api_key = api_key.strip()
    if _api_key:
        _client = httpx.AsyncClient(timeout=5)


async def _send(event: str, tg_id: int, properties: dict) -> None:
    if not _api_key or _client is None:
        return
    try:
        await _client.post(_POSTHOG_URL, json={
            "api_key": _api_key,
            "event": event,
            "distinct_id": str(tg_id),
            "properties": properties,
        })
    except Exception:
        pass


def track(tg_id: int, event: str, properties: dict | None = None) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send(event, tg_id, properties or {}))
    except RuntimeError:
        pass
    except Exception:
        logger.exception("analytics.track failed event=%s tg_id=%s", event, tg_id)
