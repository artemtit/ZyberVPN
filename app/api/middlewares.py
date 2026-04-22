from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore[assignment]


@dataclass(slots=True)
class RateLimitConfig:
    per_minute: int


class InMemoryRateLimiter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._window = 60.0
        self._buckets: dict[str, deque[float]] = {}

    async def hit(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = deque()
            self._buckets[key] = bucket
        while bucket and now - bucket[0] > self._window:
            bucket.popleft()
        bucket.append(now)
        return len(bucket) <= self._limit


class RedisRateLimiter:
    def __init__(self, redis: Redis, limit: int) -> None:
        self._redis = redis
        self._limit = limit

    async def hit(self, key: str) -> bool:
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, 60)
        return int(count) <= self._limit


@web.middleware
async def error_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException as error:
        if error.status >= 400:
            payload = {"error": error.text or error.reason, "code": error.status}
            return web.json_response(payload, status=error.status)
        raise
    except Exception:
        logger.exception("Unhandled server error")
        return web.json_response({"error": "internal error", "code": 500}, status=500)


@web.middleware
async def request_logging_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    started = time.perf_counter()
    trace_id = request.headers.get("X-Request-ID", "")
    ip = request.headers.get("X-Forwarded-For", request.remote or "unknown").split(",")[0].strip()
    response = await handler(request)
    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        json.dumps(
            {
                "event": "http_request",
                "method": request.method,
                "path": request.path,
                "status": response.status,
                "ip": ip,
                "duration_ms": duration_ms,
                "trace_id": trace_id,
            }
        )
    )
    return response


def build_rate_limit_middleware(config: RateLimitConfig) -> web.AbstractMiddleware:
    @web.middleware
    async def rate_limit_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        limiter = request.app["rate_limiter"]
        # Use transport-level remote address — X-Forwarded-For is trivially spoofed.
        ip = request.remote or "unknown"
        key = f"rl:{ip}:{int(time.time() // 60)}"
        try:
            allowed = await limiter.hit(key)
        except Exception:
            logger.exception("Rate limiter backend failed")
            fallback = request.app.get("rate_limiter_fallback")
            if fallback is None:
                fallback = InMemoryRateLimiter(config.per_minute)
                request.app["rate_limiter_fallback"] = fallback
            allowed = await fallback.hit(key)
        if not allowed:
            return web.json_response({"error": "rate limit exceeded", "code": 429}, status=429)
        return await handler(request)

    return rate_limit_middleware
