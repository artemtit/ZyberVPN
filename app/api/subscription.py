from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

from aiohttp import web

from app.config import load_settings
from app.db.database import Database
from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager

logger = logging.getLogger(__name__)
_TOKEN_WINDOW_SECONDS = 60
_TOKEN_RATE_LIMIT = 10
_TOKEN_SUSPICIOUS_THRESHOLD = 20
_TOKEN_REQUESTS: dict[str, deque[float]] = {}


def _track_token_rate(token: str) -> tuple[int, bool]:
    now = time.monotonic()
    bucket = _TOKEN_REQUESTS.get(token)
    if bucket is None:
        bucket = deque()
        _TOKEN_REQUESTS[token] = bucket
    while bucket and now - bucket[0] > _TOKEN_WINDOW_SECONDS:
        bucket.popleft()
    bucket.append(now)
    count = len(bucket)
    if count > _TOKEN_SUSPICIOUS_THRESHOLD:
        logger.warning("Suspicious /sub activity token=%s requests_per_min=%s", token, count)
    return count, count > _TOKEN_RATE_LIMIT


def _is_expired(expires_at: object) -> bool:
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except Exception:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc)


def _format_subscription(configs: list[str]) -> str:
    lines = [line.strip() for line in configs if str(line).strip().startswith("vless://")]
    return "\n".join(lines)


async def get_subscription(request: web.Request) -> web.Response:
    db = request.app["db"]
    settings = request.app.get("settings") or load_settings()
    user_token = request.match_info.get("user_token", "").strip()
    if not user_token:
        raise web.HTTPBadRequest(text="missing token")

    users_repo = UsersRepository(db)
    if not users_repo.is_valid_sub_token(user_token):
        logger.info("Subscription result=404 token=%s reason=invalid_token", user_token)
        raise web.HTTPNotFound(text="subscription not found")

    req_count, limited = _track_token_rate(user_token)
    if limited:
        logger.info("Rate limited /sub token=%s requests_per_min=%s", user_token, req_count)
        raise web.HTTPTooManyRequests(text="rate limit exceeded")

    user = await users_repo.get_by_sub_token(user_token)
    if not user:
        raise web.HTTPNotFound(text="subscription not found")
    if _is_expired(user.get("expires_at")):
        raise web.HTTPForbidden(text="subscription inactive")

    manager = build_vpn_manager(db, settings)
    configs = await manager.get_subscription(int(user["tg_id"]), create_if_missing=False)
    payload = _format_subscription(configs)
    if not payload:
        raise web.HTTPNotFound(text="vpn access not found")
    return web.Response(text=payload, content_type="text/plain")


async def get_subscription_by_user_id(request: web.Request) -> web.Response:
    db = request.app["db"]
    settings = request.app.get("settings") or load_settings()
    raw_user_id = request.match_info.get("user_id", "").strip()
    if not raw_user_id.isdigit():
        raise web.HTTPBadRequest(text="invalid user_id")
    user_id = int(raw_user_id)
    users_repo = UsersRepository(db)
    user = await users_repo.get_by_tg_id(user_id)
    if not user:
        raise web.HTTPNotFound(text="subscription not found")
    if _is_expired(user.get("expires_at")):
        raise web.HTTPForbidden(text="subscription inactive")
    manager = build_vpn_manager(db, settings)
    configs = await manager.get_subscription(user_id, create_if_missing=False)
    payload = _format_subscription(configs)
    if not payload:
        raise web.HTTPNotFound(text="vpn access not found")
    return web.Response(text=payload, content_type="text/plain")


def register_subscription_routes(app: web.Application, db: Database) -> None:
    app["db"] = db
    app.router.add_get("/sub/{user_token}", get_subscription)
    app.router.add_get("/subscription/{user_id}", get_subscription_by_user_id)
