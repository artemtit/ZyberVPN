from __future__ import annotations

from datetime import datetime, timezone

from app.repositories.users import UsersRepository
from app.services.access import build_vpn_manager


class SubscriptionService:
    def __init__(self, users_repo: UsersRepository, vpn_manager) -> None:
        self._users_repo = users_repo
        self._vpn_manager = vpn_manager

    async def get_payload_by_token(self, token: str) -> str:
        user = await self._users_repo.get_by_sub_token(token)
        if not user:
            raise PermissionError("forbidden")
        if self._is_expired(user.get("expires_at")):
            raise PermissionError("subscription inactive")
        configs = await self._vpn_manager.get_subscription(int(user["tg_id"]), create_if_missing=False)
        lines = [line.strip() for line in configs if str(line).strip().startswith("vless://")]
        payload = "\n".join(lines)
        if not payload:
            raise LookupError("vpn access not found")
        return payload

    @staticmethod
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


def build_subscription_service(db, settings) -> SubscriptionService:
    users_repo = UsersRepository(db)
    vpn_manager = build_vpn_manager(db, settings)
    return SubscriptionService(users_repo=users_repo, vpn_manager=vpn_manager)

