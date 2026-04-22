from __future__ import annotations

import logging
import re
import secrets
from typing import Optional

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import parse_iso_utc, utc_now
from app.utils.security import sha256_hex

logger = logging.getLogger(__name__)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class UsersRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()
        self._last_supabase_error = False

    @property
    def has_supabase(self) -> bool:
        return self._supabase is not None

    @property
    def last_supabase_error(self) -> bool:
        return self._last_supabase_error

    async def get_by_tg_id(self, tg_id: int) -> Optional[dict]:
        if not self._supabase:
            self._last_supabase_error = True
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("users")
                    .select(
                        "id,tg_id,ref_tg_id,balance,trial_used,vpn_key,sub_token,expires_at,is_active,plan,promo_used,last_activated_at,created_at"
                    )
                    .eq("tg_id", tg_id)
                    .limit(1)
                    .execute()
                ),
                operation="users.get_by_tg_id",
            )
            data = response.data or []
            self._last_supabase_error = False
            return data[0] if data else None
        except Exception:
            self._last_supabase_error = True
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
        last_activated_at: str | None = None,
        ref_tg_id: int | None = None,
    ) -> Optional[dict]:
        if not self._supabase:
            return None
        payload: dict = {
            "tg_id": tg_id,
            "vpn_key": vpn_key,
            "sub_token": self.hash_sub_token(sub_token),
            "is_active": is_active,
            "plan": plan,
            "ref_tg_id": ref_tg_id,
        }
        if expires_at:
            payload["expires_at"] = expires_at
        if last_activated_at:
            payload["last_activated_at"] = last_activated_at
        try:
            response = await execute_with_retry(
                lambda: self._supabase.table("users").insert(payload).execute(),
                operation="users.create",
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.info("Supabase create user conflict/failure tg_id=%s", tg_id)
            return None

    async def get_or_create(self, tg_id: int, ref_tg_id: int | None = None) -> dict:
        existing = await self.get_by_tg_id(tg_id)
        if existing:
            return existing
        token = await self._generate_unique_sub_token()
        created = await self.create(
            tg_id=tg_id,
            vpn_key="",
            sub_token=token,
            is_active=False,
            plan="none",
            ref_tg_id=ref_tg_id,
        )
        if created:
            return created
        existing_after_conflict = await self.get_by_tg_id(tg_id)
        if existing_after_conflict:
            return existing_after_conflict
        raise RuntimeError("Failed to create user")

    async def update_key(self, tg_id: int, vpn_key: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: self._supabase.table("users").update({"vpn_key": vpn_key}).eq("tg_id", tg_id).execute(),
                operation="users.update_key",
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
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("users")
                    .update({"sub_token": self.hash_sub_token(sub_token)})
                    .eq("tg_id", tg_id)
                    .execute()
                ),
                operation="users.update_sub_token",
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase update_sub_token failed")
            return None

    async def get_by_sub_token(self, token: str) -> Optional[dict]:
        if not self._supabase:
            self._last_supabase_error = True
            return None
        if not self.is_valid_sub_token(token):
            return None
        token_hash = self.hash_sub_token(token)
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("users")
                    .select(
                        "id,tg_id,ref_tg_id,balance,trial_used,vpn_key,sub_token,expires_at,is_active,plan,promo_used,last_activated_at,created_at"
                    )
                    .eq("sub_token", token_hash)
                    .limit(1)
                    .execute()
                ),
                operation="users.get_by_sub_token",
            )
            data = response.data or []
            self._last_supabase_error = False
            return data[0] if data else None
        except Exception:
            self._last_supabase_error = True
            logger.exception("Supabase get_by_sub_token failed")
            return None

    async def ensure_sub_token(self, tg_id: int) -> str:
        token = await self._generate_unique_sub_token()
        updated = await self.update_sub_token(tg_id, token)
        if not updated:
            raise RuntimeError(f"Failed to update sub_token for tg_id={tg_id}")
        return token

    async def ensure_sub_token_for_tg(self, tg_id: int) -> str:
        await self.get_or_create(tg_id)
        return await self.ensure_sub_token(tg_id)

    async def update_status(self, tg_id: int, is_active: bool) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: self._supabase.table("users").update({"is_active": is_active}).eq("tg_id", tg_id).execute(),
                operation="users.update_status",
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
            response = await execute_with_retry(
                lambda: self._supabase.table("users").update({"promo_used": promo_used}).eq("tg_id", tg_id).execute(),
                operation="users.update_promo_used",
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
        last_activated_at: str | None = None,
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
        if last_activated_at is not None:
            payload["last_activated_at"] = last_activated_at
        try:
            response = await execute_with_retry(
                lambda: self._supabase.table("users").update(payload).eq("tg_id", tg_id).execute(),
                operation="users.set_expiry",
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase set_expiry failed")
            return None

    async def deactivate_expired_users(self) -> int:
        if not self._supabase:
            return 0
        now_iso = utc_now().isoformat()
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("users")
                    .update({"is_active": False})
                    .eq("is_active", True)
                    .lt("expires_at", now_iso)
                    .execute()
                ),
                operation="users.deactivate_expired",
            )
            return len(response.data or [])
        except Exception:
            logger.exception("Supabase deactivate_expired_users failed")
            return 0

    async def list_expired_active_tg_ids(self, limit: int = 200) -> list[int]:
        if not self._supabase:
            return []
        now_iso = utc_now().isoformat()
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("users")
                    .select("tg_id")
                    .eq("is_active", True)
                    .not_.is_("expires_at", "null")
                    .lt("expires_at", now_iso)
                    .limit(limit)
                    .execute()
                ),
                operation="users.list_expired_active_tg_ids",
            )
            rows = response.data or []
            return [int(row.get("tg_id")) for row in rows if isinstance(row, dict) and row.get("tg_id") is not None]
        except Exception:
            logger.exception("Supabase list_expired_active_tg_ids failed")
            return []

    async def count_referrals(self, tg_id: int) -> int:
        if not self._supabase:
            return 0
        response = await execute_with_retry(
            lambda: self._supabase.table("users").select("tg_id").eq("ref_tg_id", tg_id).execute(),
            operation="users.count_referrals",
        )
        return len(response.data or [])

    async def add_balance(self, tg_id: int, amount: int) -> None:
        if not self._supabase:
            return
        # Atomic increment via RPC to prevent race condition on concurrent payments.
        await execute_with_retry(
            lambda: self._supabase.rpc(
                "increment_user_balance", {"p_tg_id": tg_id, "p_amount": amount}
            ).execute(),
            operation="users.add_balance",
        )

    async def is_trial_available(self, tg_id: int) -> bool:
        user = await self.get_by_tg_id(tg_id)
        if not user:
            return False
        return not bool(user.get("trial_used"))

    async def mark_trial_used(self, tg_id: int) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: self._supabase.table("users").update({"trial_used": True}).eq("tg_id", tg_id).execute(),
            operation="users.mark_trial_used",
        )

    @staticmethod
    def is_user_active(user: dict | None) -> bool:
        if not user:
            return False
        expires_at = user.get("expires_at")
        if not expires_at:
            return bool(user.get("is_active", False))
        try:
            expiry_utc = parse_iso_utc(expires_at)
        except Exception:
            return False
        return bool(user.get("is_active", False)) and expiry_utc > utc_now()

    @staticmethod
    def is_valid_sub_token(token: str) -> bool:
        value = (token or "").strip()
        return len(value) >= 32

    @staticmethod
    def is_valid_sub_token_hash(token_hash: str) -> bool:
        value = (token_hash or "").strip().lower()
        return bool(_SHA256_HEX_RE.fullmatch(value))

    @staticmethod
    def hash_sub_token(token: str) -> str:
        value = (token or "").strip()
        if not value:
            raise ValueError("sub_token is empty")
        return sha256_hex(value)

    async def _generate_unique_sub_token(self) -> str:
        while True:
            candidate = secrets.token_urlsafe(32)
            candidate_hash = self.hash_sub_token(candidate)
            if not await self._supabase_token_exists(candidate_hash):
                return candidate

    async def _supabase_token_exists(self, token_hash: str) -> bool:
        if not self._supabase:
            return False
        response = await execute_with_retry(
            lambda: self._supabase.table("users").select("id").eq("sub_token", token_hash).limit(1).execute(),
            operation="users.token_exists",
        )
        return bool(response.data)
