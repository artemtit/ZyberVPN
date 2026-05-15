from __future__ import annotations

from app.repositories.users import UsersRepository


class ReferralService:
    def __init__(self, users_repo: UsersRepository, percent: int) -> None:
        self.users_repo = users_repo
        self.percent = percent

    async def accrue_bonus(self, buyer_user: dict, payment_amount_rub: int) -> int:
        inviter_tg_id = buyer_user.get("ref_tg_id")
        if not inviter_tg_id:
            return 0
        if int(inviter_tg_id) == int(buyer_user.get("tg_id", 0)):
            return 0  # self-referral
        bonus = int(payment_amount_rub * self.percent / 100)
        if bonus > 0:
            await self.users_repo.add_balance(int(inviter_tg_id), bonus)
        return bonus

    async def accrue_friend_bonus(self, buyer_user: dict, paid_count: int, friend_bonus_rub: int) -> int:
        if not buyer_user.get("ref_tg_id"):
            return 0
        if paid_count != 1 or friend_bonus_rub <= 0:
            return 0
        await self.users_repo.add_balance(int(buyer_user["tg_id"]), friend_bonus_rub)
        return friend_bonus_rub
