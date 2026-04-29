from __future__ import annotations

from aiohttp import web
from pydantic import ValidationError

from app.api.schemas import SubscriptionTokenPath


async def get_subscription(request: web.Request) -> web.Response:
    service = request.app["subscription_service"]
    raw_token = request.match_info.get("user_token", "")
    try:
        model = SubscriptionTokenPath(token=raw_token)
    except ValidationError:
        raise web.HTTPForbidden(text="forbidden")

    try:
        payload = await service.get_payload_by_token(model.token)
    except PermissionError as error:
        raise web.HTTPForbidden(text=str(error))
    except LookupError as error:
        raise web.HTTPNotFound(text=str(error))

    # ВАЖНО: берем список серверов
    servers = payload.get("servers", [])

    if not servers:
        raise web.HTTPNotFound(text="no servers")

    # Формируем plain text (каждая ссылка с новой строки)
    body = "\n".join(servers)

    # Заголовок для клиентов (трафик + лимиты)
    userinfo = (
        f"upload={payload['upload']}; "
        f"download={payload['download']}; "
        f"total={payload['total']}; "
        f"expire={payload['expire']}"
    )

    return web.Response(
        text=body,
        content_type="text/plain",
        headers={
            "Subscription-Userinfo": userinfo,
            "profile-title": "ZyberVPN",
            "profile-update-interval": "12",
        },
    )


def register_subscription_routes(app: web.Application) -> None:
    app.router.add_get("/sub/{user_token}", get_subscription)