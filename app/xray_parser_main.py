from __future__ import annotations

import asyncio
import logging

from app.config import load_settings
from app.db.database import Database
from app.main import configure_logging
from app.services.xray_device_parser import XrayDeviceParser


async def run() -> None:
    configure_logging()
    settings = load_settings()
    if not settings.xray_parser_enabled:
        logging.info("xray parser disabled via XRAY_PARSER_ENABLED")
        return
    db = Database(settings.db_path)
    await db.init()
    parser = XrayDeviceParser(db, settings)

    while True:
        try:
            await parser.run_once()
        except Exception:
            logging.exception("xray parser loop failed")
        await asyncio.sleep(settings.xray_parser_interval_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("xray parser stopped")
