from __future__ import annotations

import logging

from app.db.schema_contract import EXPECTED_SCHEMA
from app.services.supabase import execute_with_retry, get_supabase_client

logger = logging.getLogger(__name__)


async def validate_supabase_schema_or_raise() -> None:
    supabase = get_supabase_client()
    if not supabase:
        logger.info("Supabase schema validation skipped: Supabase is not configured")
        return

    for table_name, expected_columns in EXPECTED_SCHEMA.items():
        response = await execute_with_retry(
            lambda: (
                supabase.schema("information_schema")
                .table("columns")
                .select("column_name")
                .eq("table_schema", "public")
                .eq("table_name", table_name)
                .execute()
            ),
            operation=f"schema_validation.{table_name}",
        )
        actual_rows = response.data or []
        actual_columns = {str(row.get("column_name")) for row in actual_rows if isinstance(row, dict)}
        missing = [col for col in expected_columns if col not in actual_columns]
        if missing:
            logger.error(
                "Supabase schema mismatch table=%s missing_columns=%s expected_columns=%s actual_columns=%s",
                table_name,
                missing,
                expected_columns,
                sorted(actual_columns),
            )
            raise RuntimeError(f"Supabase schema mismatch for {table_name}: missing {missing}")

