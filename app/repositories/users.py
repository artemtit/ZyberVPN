from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from app.db.database import Database
from app.services.supabase import get_supabase_client

logger = logging.getLogger(__name__)


class UsersRepository:
    def __init__(self, db: Database) -> None:
        self.db_path = db.db_path
        self._supabase = get_supabase_client()

    # Supabase storage (primary for tg_id/vpn_key/sub_token + subscription fields)
    async def get_by_tg_id(self, tg_id: int) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("users")
                .select("id,tg_id,vpn_key,sub_token,expires_at,is_active,plan,promo_used,created_at")
                .eq("tg_id", tg_id)
                .limit(1)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase get_by_tg_id failed")
            return None

    async def create(
        self,
        tg_id: int,
        vpn_key: str,
        sub_token: str,
        expires_at: str | None = None,
        is_active: bool = True,
        plan: str = "trial",
    ) -> Optional[dict]:
        if not self._supabase:
            return None
        payload: dict = {
            "tg_id": tg_id,
            "vpn_key": vpn_key,
            "sub_token": sub_token,
            "is_active": is_active,
            "plan": plan,
        }
        if expires_at:
            payload["expires_at"] = expires_at
        try:
            response = self._supabase.table("users").insert(payload).execute()
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase create user failed")
            return None

    async def update_key(self, tg_id: int, vpn_key: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("users")
                .update({"vpn_key": vpn_key})
                .eq("tg_id", tg_id)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase update_key failed")
            return None

    async def update_sub_token(self, tg_id: int, sub_token: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("users")
                .update({"sub_token": sub_token})
                .eq("tg_id", tg_id)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase update_sub_token failed")
            return None

    async def get_by_sub_token(self, token: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("users")
                .select("id,tg_id,vpn_key,sub_token,expires_at,is_active,plan,promo_used,created_at")
                .eq("sub_token", token)
                .limit(1)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase get_by_sub_token failed")
            return None

    async def update_status(self, tg_id: int, is_active: bool) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("users")
                .update({"is_active": is_active})
                .eq("tg_id", tg_id)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase update_status failed")
            return None

    async def update_promo_used(self, tg_id: int, promo_used: bool) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("users")
                .update({"promo_used": promo_used})
                .eq("tg_id", tg_id)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase update_promo_used failed")
            return None

    async def set_expiry(
        self,
        tg_id: int,
        expires_at: str,
        is_active: bool | None = None,
        plan: str | None = None,
        promo_used: bool | None = None,
    ) -> Optional[dict]:
        if not self._supabase:
            return None
        payload: dict = {"expires_at": expires_at}
        if is_active is not None:
            payload["is_active"] = is_active
        if plan is not None:
            payload["plan"] = plan
        if promo_used is not None:
            payload["promo_used"] = promo_used
        try:
            response = self._supabase.table("users").update(payload).eq("tg_id", tg_id).execute()
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase set_expiry failed")
            return None

    async def deactivate_expired_users(self) -> int:
        if not self._supabase:
            return 0
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            response = (
                self._supabase.table("users")
                .update({"is_active": False})
                .eq("is_active", True)
                .lt("expires_at", now_iso)
                .execute()
            )
            data = response.data or []
            return len(data)
        except Exception:
            logger.exception("Supabase deactivate_expired_users failed")
            return 0

    @staticmethod
    def is_user_active(user: dict | None) -> bool:
        if not user:
            return False
        if bool(user.get("is_active")) is False:
            return False
        expires_at = user.get("expires_at")
        if not expires_at:
            return True
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except Exception:
            return False
        now = datetime.now(timezone.utc)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > now

    # SQLite compatibility layer for existing business logic
    async def _sqlite_get_by_tg_id(self, tg_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _sqlite_get_by_sub_token(self, sub_token: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM users WHERE sub_token = ?", (sub_token,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_by_id(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_or_create(self, tg_id: int, ref_tg_id: int | None = None) -> dict:
        existing = await self._sqlite_get_by_tg_id(tg_id)
        if existing:
            if not existing.get("sub_token"):
                token = await self._generate_unique_sub_token()
                await self._set_sub_token(existing["id"], token)
                existing["sub_token"] = token
            return existing

        ref_id = None
        if ref_tg_id and ref_tg_id != tg_id:
            ref_user = await self._sqlite_get_by_tg_id(ref_tg_id)
            if ref_user:
                ref_id = ref_user["id"]

        async with aiosqlite.connect(self.db_path) as conn:
            sub_token = await self._generate_unique_sub_token()
            cursor = await conn.execute(
                "INSERT INTO users (tg_id, ref_id, sub_token) VALUES (?, ?, ?)",
                (tg_id, ref_id, sub_token),
            )
            await conn.commit()
            new_id = cursor.lastrowid
        created = await self.get_by_id(new_id)
        if not created:
            raise RuntimeError("Failed to create user")
        return created

    async def ensure_sub_token(self, user_id: int) -> str:
        user = await self.get_by_id(user_id)
        if not user:
            raise RuntimeError("User not found")
        existing = user.get("sub_token")
        if existing:
            return existing
        token = await self._generate_unique_sub_token()
        await self._set_sub_token(user_id, token)
        return token

    async def ensure_sub_token_for_tg(self, tg_id: int) -> str:
        supabase_user = await self.get_by_tg_id(tg_id)
        existing = (supabase_user or {}).get("sub_token")
        if existing:
            return str(existing)

        local_user = await self.get_or_create(tg_id)
        token = local_user.get("sub_token")
        if not token:
            token = await self.ensure_sub_token(local_user["id"])

        if supabase_user:
            updated = await self.update_sub_token(tg_id, token)
            if not updated:
                logger.error("Failed to sync sub_token to Supabase for tg_id=%s", tg_id)
        return str(token)

    async def _set_sub_token(self, user_id: int, sub_token: str) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE users SET sub_token = ? WHERE id = ?",
                (sub_token, user_id),
            )
            await conn.commit()

    async def _generate_unique_sub_token(self) -> str:
        import uuid

        while True:
            candidate = str(uuid.uuid4())
            exists = await self._sqlite_get_by_sub_token(candidate)
            if not exists:
                return candidate

    async def count_referrals(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE ref_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return int(row["cnt"]) if row else 0

    async def add_balance(self, user_id: int, amount: int) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE users SET balance = balance + ? WHERE id = ?",
                (amount, user_id),
            )
            await conn.commit()

    async def is_trial_available(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT trial_used FROM users WHERE id = ? LIMIT 1",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            return int(row["trial_used"]) == 0

    async def mark_trial_used(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE users SET trial_used = 1 WHERE id = ?",
                (user_id,),
            )
            await conn.commit()
