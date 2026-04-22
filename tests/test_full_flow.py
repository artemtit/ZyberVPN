"""End-to-end integration test: user creation → VPN provisioning → /sub endpoint."""
from __future__ import annotations

from datetime import timedelta

from aiohttp import ClientSession, ClientTimeout, web

from app.api.subscription import register_subscription_routes
from app.config import Settings, load_settings
from app.db.database import Database
from app.repositories.users import UsersRepository
from app.services.access import AccessEnsureError, ensure_user_access, _is_vpn_key_valid
from app.services.subscription import build_subscription_service
from app.utils.datetime import utc_now


async def test_full_flow(tg_id: int, db: Database | None = None, settings: Settings | None = None) -> dict:
    settings = settings or load_settings()
    db = db or Database(settings.db_path)
    await db.init()

    users_repo = UsersRepository(db)
    await users_repo.get_or_create(tg_id)
    sub_token = await users_repo.ensure_sub_token(tg_id)

    now = utc_now()
    expires_at = (now + timedelta(days=1)).isoformat()
    supabase_user = await users_repo.get_by_tg_id(tg_id)
    if not supabase_user:
        created = await users_repo.create(
            tg_id=tg_id,
            vpn_key="",
            sub_token=sub_token,
            expires_at=expires_at,
            is_active=True,
            plan="trial",
            last_activated_at=now.isoformat(),
        )
        if not created:
            raise AccessEnsureError("test_full_flow: failed to create Supabase user")
    else:
        if not users_repo.is_valid_sub_token_hash(str(supabase_user.get("sub_token") or "")):
            updated_token = await users_repo.update_sub_token(tg_id, sub_token)
            if not updated_token:
                raise AccessEnsureError("test_full_flow: failed to update sub_token")
        updated_expiry = await users_repo.set_expiry(
            tg_id=tg_id,
            expires_at=expires_at,
            is_active=True,
            last_activated_at=now.isoformat(),
        )
        if not updated_expiry:
            raise AccessEnsureError("test_full_flow: failed to activate subscription")

    access_user = await ensure_user_access(tg_id=tg_id, db=db, settings=settings, require_active=True)
    vpn_key = str((access_user or {}).get("vpn_key") or "")
    token = await users_repo.ensure_sub_token_for_tg(tg_id)
    if not _is_vpn_key_valid(vpn_key):
        raise AccessEnsureError("test_full_flow: vpn_key is invalid")
    if not users_repo.is_valid_sub_token(token):
        raise AccessEnsureError("test_full_flow: sub_token is invalid")

    app = web.Application()
    app["subscription_service"] = build_subscription_service(db, settings)
    register_subscription_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()

    sockets = list(getattr(site._server, "sockets", []))  # noqa: SLF001
    if not sockets:
        await runner.cleanup()
        raise AccessEnsureError("test_full_flow: failed to bind local subscription server")
    port = sockets[0].getsockname()[1]
    sub_url = f"http://127.0.0.1:{port}/sub/{token}"

    try:
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
            async with session.get(sub_url) as response:
                body = await response.text()
                if response.status != 200:
                    raise AccessEnsureError(f"test_full_flow: /sub returned {response.status}")
                lines = [line.strip() for line in body.splitlines() if line.strip()]
                if not lines or lines[0] != vpn_key:
                    raise AccessEnsureError("test_full_flow: /sub payload does not contain vpn_key")
    finally:
        await runner.cleanup()

    return {
        "tg_id": tg_id,
        "vpn_key": vpn_key,
        "sub_token": token,
        "sub_url": sub_url,
    }
