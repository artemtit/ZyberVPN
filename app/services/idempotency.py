from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.repositories.idempotency import IdempotencyRepository

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.25  # seconds between completion checks
_POLL_MAX_WAIT = 3.0   # seconds before giving up on a live peer


class IdempotencyService:
    def __init__(self, repo: IdempotencyRepository) -> None:
        self._repo = repo

    async def execute(
        self,
        operation: str,
        idempotency_key: str,
        handler: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        # Fast path: already completed.
        existing = await self._repo.get_completed(operation, idempotency_key)
        if existing:
            payload = existing.get("response_payload")
            if isinstance(payload, dict):
                return payload

        owner = await self._repo.try_start(operation, idempotency_key)

        if not owner:
            # Another record is in 'processing'. Check if it is stale (crashed
            # before save_completed / save_failed was reached).
            stale = await self._repo.is_stale_processing(operation, idempotency_key)
            if stale:
                logger.warning(
                    "Stale idempotency lock detected — evicting and retrying "
                    "op=%s key=%s",
                    operation,
                    idempotency_key,
                )
                await self._repo.delete_record(operation, idempotency_key)
                owner = await self._repo.try_start(operation, idempotency_key)

        if not owner:
            # A live peer is processing the same key. Poll briefly; if it does
            # not complete within _POLL_MAX_WAIT we raise so Telegram does not
            # hang until its own timeout.
            poll_steps = int(_POLL_MAX_WAIT / _POLL_INTERVAL)
            for _ in range(poll_steps):
                await asyncio.sleep(_POLL_INTERVAL)
                existing = await self._repo.get_completed(operation, idempotency_key)
                if existing:
                    payload = existing.get("response_payload")
                    if isinstance(payload, dict):
                        return payload
            raise TimeoutError(
                f"Idempotent operation timed out waiting for peer: {operation}"
            )

        # We are the owner — run the handler and persist the outcome either way.
        try:
            result = await handler()
        except Exception as exc:
            try:
                await self._repo.save_failed(operation, idempotency_key, str(exc))
            except Exception:
                logger.exception(
                    "Failed to persist idempotency failure op=%s key=%s",
                    operation,
                    idempotency_key,
                )
            raise

        await self._repo.save_completed(operation, idempotency_key, result)
        return result
