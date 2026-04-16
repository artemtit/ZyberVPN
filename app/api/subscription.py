from __future__ import annotations

import logging

from aiohttp import web

from app.db.database import Database
from app.repositories.keys import KeysRepository
from app.repositories.subscriptions import SubscriptionsRepository
from app.repositories.users import UsersRepository

logger = logging.getLogger(__name__)


async def get_subscription(request: web.Request) -> web.Response:
    db = request.app["db"]
    user_token = request.match_info.get("user_token", "").strip()
    if not user_token:
        raise web.HTTPBadRequest(text="missing token")

    users_repo = UsersRepository(db)
    user = await users_repo.get_by_sub_token(user_token)

    if user:
        if not users_repo.is_user_active(user):
            await users_repo.update_status(int(user["tg_id"]), False)
            logger.info("Subscription rejected: inactive tg_id=%s", user.get("tg_id"))
            raise web.HTTPForbidden(text="subscription inactive")
        vpn_key = (user or {}).get("vpn_key")
        if not vpn_key:
            logger.warning("Subscription rejected: missing key for tg_id=%s", user.get("tg_id"))
            raise web.HTTPNotFound(text="no configs")
        logger.info("Subscription issued for tg_id=%s", user.get("tg_id"))
        return web.Response(text=str(vpn_key), content_type="text/plain")

    # Fallback for legacy local data.
    local_user = await users_repo._sqlite_get_by_sub_token(user_token)  # noqa: SLF001
    if not local_user:
        raise web.HTTPNotFound(text="subscription not found")

    subs_repo = SubscriptionsRepository(db)
    active_sub = await subs_repo.get_active(local_user["id"])
    if not active_sub:
        logger.info("Legacy subscription rejected: inactive tg_id=%s", local_user.get("tg_id"))
        raise web.HTTPForbidden(text="subscription inactive")

    keys_repo = KeysRepository(db)
    rows = await keys_repo.list_by_user(local_user["id"])
    configs = [row["key"] for row in rows if row.get("key")]
    if not configs:
        logger.warning("Legacy subscription rejected: no configs for tg_id=%s", local_user.get("tg_id"))
        raise web.HTTPNotFound(text="no configs")

    payload = "\n".join(configs)
    logger.info("Legacy subscription issued for tg_id=%s configs=%s", local_user.get("tg_id"), len(configs))
    return web.Response(text=payload, content_type="text/plain")


def register_subscription_routes(app: web.Application, db: Database) -> None:
    app["db"] = db
    app.router.add_get("/sub/{user_token}", get_subscription)
