from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Database:
    db_path: str = ""

    async def init(self) -> None:
        # Supabase is the only source of truth. Local SQLite initialization is intentionally removed.
        return None

