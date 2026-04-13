from __future__ import annotations

from app.repositories.users import UsersRepository


class ReferralService:
    def __init__(self, users_repo: UsersRepository, percent: int) -> None:
        self.users_repo = users_repo
        self.percent = percent

    async def accrue_bonus(self, buyer_user: dict, payment_amount_rub: int) -> int:
        inviter_id = buyer_user.get("ref_id")
        if not inviter_id:
            return 0
        bonus = int(payment_amount_rub * self.percent / 100)
        if bonus > 0:
            await self.users_repo.add_balance(inviter_id, bonus)
        return bonus
