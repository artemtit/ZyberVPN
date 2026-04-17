from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

from aiohttp import web

from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, ensure_user_access

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


def _is_vpn_key_valid(vpn_key: str | None) -> bool:
    return bool(vpn_key and str(vpn_key).startswith("vless://"))


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


async def get_subscription(request: web.Request) -> web.Response:
    db = request.app["db"]
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

    if user:
        if bool(user.get("is_active")) is False:
            await users_repo.update_status(int(user["tg_id"]), False)
            logger.info("Subscription result=403 token=%s tg_id=%s reason=inactive", user_token, user.get("tg_id"))
            raise web.HTTPForbidden(text="subscription inactive")
        if _is_expired(user.get("expires_at")):
            await users_repo.update_status(int(user["tg_id"]), False)
            logger.info("Subscription result=403 token=%s tg_id=%s reason=expired", user_token, user.get("tg_id"))
            raise web.HTTPForbidden(text="subscription inactive")
        vpn_key = (user or {}).get("vpn_key")
        if not _is_vpn_key_valid(vpn_key):
            tg_id = int(user["tg_id"])
            logger.warning("Subscription missing vpn_key, trying self-heal tg_id=%s token=%s", tg_id, user_token)
            try:
                healed = await ensure_user_access(
                    tg_id=tg_id,
                    db=db,
                    require_active=True,
                )
                vpn_key = (healed or {}).get("vpn_key")
            except AccessEnsureError:
                logger.exception("Subscription self-heal failed for tg_id=%s", tg_id)
                raise web.HTTPServiceUnavailable(text="vpn key unavailable")
            if not _is_vpn_key_valid(vpn_key):
                logger.error("Subscription self-heal returned no vpn_key for tg_id=%s", tg_id)
                raise web.HTTPServiceUnavailable(text="vpn key unavailable")
        logger.info("Subscription result=200 token=%s tg_id=%s", user_token, user.get("tg_id"))
        return web.Response(text=str(vpn_key), content_type="text/plain")

    if users_repo.has_supabase and not users_repo.last_supabase_error:
        logger.info("Subscription result=404 token=%s reason=user_not_found", user_token)
        raise web.HTTPNotFound(text="subscription not found")

    logger.warning("Supabase unavailable for /sub lookup, using sqlite fallback token=%s", user_token)
    local_user = await users_repo._sqlite_get_by_sub_token(user_token)  # noqa: SLF001
    if not local_user:
        logger.info("Subscription result=404 token=%s reason=local_user_not_found", user_token)
        raise web.HTTPNotFound(text="subscription not found")

    subs_repo = SubscriptionsRepository(db)
    active_sub = await subs_repo.get_active(local_user["id"])
    if not active_sub:
        logger.info("Subscription result=403 token=%s tg_id=%s reason=legacy_inactive", user_token, local_user.get("tg_id"))
        raise web.HTTPForbidden(text="subscription inactive")

    keys_repo = KeysRepository(db)
    rows = await keys_repo.list_by_user(local_user["id"])
    configs = [row["key"] for row in rows if row.get("key")]
    if not configs:
        logger.warning("Legacy subscription rejected: no configs for tg_id=%s", local_user.get("tg_id"))
        raise web.HTTPNotFound(text="no configs")

    payload = "\n".join(configs)
    logger.info("Subscription result=200 token=%s tg_id=%s source=legacy configs=%s", user_token, local_user.get("tg_id"), len(configs))
    return web.Response(text=payload, content_type="text/plain")


def register_subscription_routes(app: web.Application, db: Database) -> None:
    app["db"] = db
    app.router.add_get("/sub/{user_token}", get_subscription)
