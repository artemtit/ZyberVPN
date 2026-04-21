from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from app.repositories.idempotency import IdempotencyRepository


class IdempotencyService:
    def __init__(self, repo: IdempotencyRepository) -> None:
        self._repo = repo

    async def execute(
        self,
        operation: str,
        idempotency_key: str,
        handler: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        existing = await self._repo.get_completed(operation, idempotency_key)
        if existing:
            payload = existing.get("response_payload")
            if isinstance(payload, dict):
                return payload

        owner = await self._repo.try_start(operation, idempotency_key)
        if owner:
            result = await handler()
            await self._repo.save_completed(operation, idempotency_key, result)
            return result

        for _ in range(20):
            await asyncio.sleep(0.25)
            existing = await self._repo.get_completed(operation, idempotency_key)
            if existing:
                payload = existing.get("response_payload")
                if isinstance(payload, dict):
                    return payload
        raise RuntimeError(f"Idempotent operation timed out: {operation}")
