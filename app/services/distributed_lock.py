from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_UNLOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""

_local_lock_warning_issued = False


class DistributedLockManager:
    def __init__(self, redis_url: str | None) -> None:
        self._redis_url = (redis_url or "").strip()
        self._redis: Redis | None = None
        self._local_locks: dict[str, asyncio.Lock] = {}
        if self._redis_url and Redis is not None:
            self._redis = Redis.from_url(self._redis_url, encoding="utf-8", decode_responses=True)
        elif self._redis_url:
            logger.error("Redis URL configured but redis.asyncio unavailable")

    @asynccontextmanager
    async def lock(self, key: str, ttl_seconds: int = 30, wait_timeout: float = 8.0):
        if self._redis:
            async with self._redis_lock(key, ttl_seconds, wait_timeout):
                yield
        else:
            async with self._local_lock(key, wait_timeout):
                yield

    @asynccontextmanager
    async def _redis_lock(self, key: str, ttl_seconds: int, wait_timeout: float):
        token = uuid4().hex
        lock_key = f"lock:{key}"
        deadline = time.monotonic() + wait_timeout
        acquired = False

        while time.monotonic() < deadline:
            try:
                acquired = bool(await self._redis.set(lock_key, token, ex=ttl_seconds, nx=True))
            except Exception:
                logger.exception("Redis lock acquisition failed key=%s", key)
                await asyncio.sleep(0.1)
                continue
            if acquired:
                break
            await asyncio.sleep(0.1)

        if not acquired:
            logger.error("Redis lock timeout key=%s wait_timeout=%s", key, wait_timeout)
            raise TimeoutError(f"Distributed lock timeout: {key}")

        try:
            yield
        finally:
            try:
                await self._redis.eval(_UNLOCK_LUA, 1, lock_key, token)
            except Exception:
                logger.exception("Redis lock release failed key=%s", key)

    @asynccontextmanager
    async def _local_lock(self, key: str, wait_timeout: float):
        global _local_lock_warning_issued
        if not _local_lock_warning_issued:
            logger.warning("Redis unavailable, using local locks (not safe for multi-instance)")
            _local_lock_warning_issued = True

        if key not in self._local_locks:
            self._local_locks[key] = asyncio.Lock()
        local_lock = self._local_locks[key]

        try:
            await asyncio.wait_for(local_lock.acquire(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Local lock timeout: {key}")

        try:
            yield
        finally:
            local_lock.release()
            # Remove idle lock to prevent unbounded dict growth.
            if not local_lock.locked():
                self._local_locks.pop(key, None)
